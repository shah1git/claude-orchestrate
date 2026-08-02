"""End-to-end regression coverage for the portable OMP machine bootstrap.

Each test drives the repository-root bootstrap against a local Git repository,
a fake OMP CLI, and a temporary HOME.  Nothing here needs a network connection
or reads the developer's actual OMP configuration.
"""
from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP_MACHINE = REPOSITORY_ROOT / "bootstrap-machine.sh"
PORTABLE_PROFILE = REPOSITORY_ROOT / "scripts" / "omp-portable-profile.yml"
PORTABLE_WATCHDOG = REPOSITORY_ROOT / "scripts" / "omp-portable-WATCHDOG.md"
PROFILE_VALIDATOR = REPOSITORY_ROOT / "scripts" / "validate_portable_omp_profile.py"


@pytest.fixture
def bootstrap_fixture(tmp_path: Path) -> dict[str, Path]:
    """Create an isolated checkout source, OMP config location, and fake CLI."""
    source = tmp_path / "source"
    source.mkdir()
    scripts = source / "scripts"
    scripts.mkdir()

    (tmp_path / "home").mkdir()
    # The source checkout deliberately contains the same profile and validator
    # that production bootstrap consumes; the tested root script must validate
    # this checkout rather than an unrelated fixture-only approximation.
    (scripts / PORTABLE_PROFILE.name).write_bytes(PORTABLE_PROFILE.read_bytes())
    (scripts / PORTABLE_WATCHDOG.name).write_bytes(PORTABLE_WATCHDOG.read_bytes())
    (scripts / PROFILE_VALIDATOR.name).write_bytes(PROFILE_VALIDATOR.read_bytes())
    (scripts / PROFILE_VALIDATOR.name).chmod(0o755)

    (source / "bootstrap-mac.sh").write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        'script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)\n'
        'printf "%s\\n" "$script_dir" >> "$BOOTSTRAP_MAC_LOG"\n'
    )
    (source / "bootstrap-mac.sh").chmod(0o755)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "omp").write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        'if [ "$#" -eq 2 ] && [ "$1" = config ] && [ "$2" = path ]; then\n'
        '    printf "%s\\n" "$OMP_CONFIG_BASE"\n'
        "    exit 0\n"
        "fi\n"
        'printf "%s\\n" "unexpected fake omp invocation: $*" >&2\n'
        "exit 64\n"
    )
    (fake_bin / "omp").chmod(0o755)

    return {
        "source": source,
        "checkout": tmp_path / "checkout",
        "home": tmp_path / "home",
        "data_home": tmp_path / "data",
        "state_home": tmp_path / "state",
        "omp_base": tmp_path / "omp-base",
        "fake_bin": fake_bin,
        "bootstrap_log": tmp_path / "bootstrap-mac.log",
    }


def git(cwd: Path, *args: str) -> None:
    """Run local-only Git setup without consulting the real user configuration."""
    environment = {
        **os.environ,
        "HOME": str(cwd.parent / "git-home"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
    }
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )


def commit_fixture_repository(source: Path, message: str) -> None:
    """Commit the fixture state so bootstrap exercises clone and fast-forward."""
    git(source, "add", ".")
    git(source, "-c", "user.name=Bootstrap test", "-c", "user.email=bootstrap@example.test", "commit", "-m", message)


def bootstrap_environment(fixture: dict[str, Path]) -> dict[str, str]:
    """Supply every mutable machine location explicitly to the root bootstrap."""
    return {
        **os.environ,
        "HOME": str(fixture["home"]),
        "XDG_DATA_HOME": str(fixture["data_home"]),
        "XDG_STATE_HOME": str(fixture["state_home"]),
        "ORCH_REPO_URL": str(fixture["source"]),
        "CLAUDE_ORCHESTRATE_DIR": str(fixture["checkout"]),
        "OMP_CONFIG_BASE": str(fixture["omp_base"]),
        "BOOTSTRAP_MAC_LOG": str(fixture["bootstrap_log"]),
        "PATH": f'{fixture["fake_bin"]}{os.pathsep}{os.environ["PATH"]}',
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
    }


def run_bootstrap(fixture: dict[str, Path]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(BOOTSTRAP_MACHINE)],
        cwd=REPOSITORY_ROOT,
        env=bootstrap_environment(fixture),
        capture_output=True,
        text=True,
    )


def initialize_fixture_repository(source: Path) -> None:
    git(source, "init")
    commit_fixture_repository(source, "initial portable profile")


def test_bootstrap_clones_updates_and_installs_portable_profile(
    bootstrap_fixture: dict[str, Path],
) -> None:
    """A local repository is cloned, refreshed, installed, and handed to macOS bootstrap."""
    fixture = bootstrap_fixture
    initialize_fixture_repository(fixture["source"])

    existing_config = fixture["omp_base"] / "config.yml"
    existing_config.parent.mkdir()
    previous_contents = b"legacy-setting: retain-only-in-backup\n"
    existing_config.write_bytes(previous_contents)
    existing_config.chmod(0o644)

    first_run = run_bootstrap(fixture)
    assert first_run.returncode == 0, first_run.stderr

    installed_profile = fixture["omp_base"] / "config.yml"
    expected_profile = (fixture["source"] / "scripts" / PORTABLE_PROFILE.name).read_bytes()
    assert installed_profile.read_bytes() == expected_profile
    assert stat.S_IMODE(installed_profile.stat().st_mode) == 0o600
    installed_watchdog = fixture["omp_base"] / "WATCHDOG.md"
    assert installed_watchdog.read_bytes() == PORTABLE_WATCHDOG.read_bytes()
    assert stat.S_IMODE(installed_watchdog.stat().st_mode) == 0o600

    backups = list(
        (fixture["state_home"] / "claude-orchestrate" / "machine-bootstrap-backups").glob("*")
    )
    assert backups
    assert any(path.is_file() and path.read_bytes() == previous_contents for path in backups)

    (fixture["source"] / "checkout-update-marker").write_text("second revision\n")
    commit_fixture_repository(fixture["source"], "exercise checkout update")

    second_run = run_bootstrap(fixture)
    assert second_run.returncode == 0, second_run.stderr
    assert (fixture["checkout"] / "checkout-update-marker").read_text() == "second revision\n"
    assert installed_profile.read_bytes() == expected_profile

    invocations = fixture["bootstrap_log"].read_text().splitlines()
    assert invocations == [str(fixture["checkout"]), str(fixture["checkout"])]


def test_bootstrap_refuses_secret_like_profile_before_replacing_config(
    bootstrap_fixture: dict[str, Path],
) -> None:
    """The real validator rejects a secret-shaped key without touching OMP config."""
    fixture = bootstrap_fixture
    profile = fixture["source"] / "scripts" / PORTABLE_PROFILE.name
    profile.write_bytes(profile.read_bytes() + b"\napi_key: must-not-be-portable\n")
    initialize_fixture_repository(fixture["source"])

    existing_config = fixture["omp_base"] / "config.yml"
    existing_config.parent.mkdir()
    previous_contents = b"legacy-setting: must-survive-validation-failure\n"
    existing_config.write_bytes(previous_contents)

    result = run_bootstrap(fixture)
    assert result.returncode != 0
    assert existing_config.read_bytes() == previous_contents
    assert not fixture["bootstrap_log"].exists()
