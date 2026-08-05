"""End-to-end regression coverage for the portable OMP machine bootstrap.

Each test drives the repository-root bootstrap against a local Git repository,
a fake OMP CLI, and a temporary HOME.  Nothing here needs a network connection
or reads the developer's actual OMP configuration.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP_MACHINE = REPOSITORY_ROOT / "bootstrap-machine.sh"
INSTALLER = REPOSITORY_ROOT / "install.sh"
PORTABLE_PROFILE = REPOSITORY_ROOT / "scripts" / "omp-portable-profile.yml"
PORTABLE_WATCHDOG = REPOSITORY_ROOT / "scripts" / "omp-portable-WATCHDOG.md"
PROFILE_VALIDATOR = REPOSITORY_ROOT / "scripts" / "validate_portable_omp_profile.py"
PROFILE_EXPORTER = REPOSITORY_ROOT / "scripts" / "export-portable-omp-profile.py"
RUNTIME_CONFIG_VALIDATOR = REPOSITORY_ROOT / "scripts" / "validate_omp_runtime_config.py"
POCOCK_AGENT_MANIFESTS = REPOSITORY_ROOT / ".omp" / "agents"


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
    fixture_agents = source / ".omp" / "agents"
    fixture_agents.mkdir(parents=True)
    for manifest in POCOCK_AGENT_MANIFESTS.glob("pocock-*.md"):
        (fixture_agents / manifest.name).write_bytes(manifest.read_bytes())

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
    for command in ("rsync", "gh"):
        executable = fake_bin / command
        executable.write_text("#!/bin/sh\nexit 0\n")
        executable.chmod(0o755)


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


def test_bootstrap_rejects_broken_readlink_preflight_before_replacing_config(
    bootstrap_fixture: dict[str, Path],
) -> None:
    """All bootstrap-mac prerequisites pass before portable files are replaced."""
    fixture = bootstrap_fixture
    initialize_fixture_repository(fixture["source"])

    existing_config = fixture["omp_base"] / "config.yml"
    existing_config.parent.mkdir()
    previous_contents = b"legacy-setting: must-survive-preflight-failure\n"
    existing_config.write_bytes(previous_contents)

    broken_readlink = fixture["fake_bin"] / "readlink"
    broken_readlink.write_text("#!/bin/sh\nexit 1\n")
    broken_readlink.chmod(0o755)

    result = run_bootstrap(fixture)

    assert result.returncode != 0
    assert "readlink -f" in result.stderr
    assert existing_config.read_bytes() == previous_contents
    assert not fixture["bootstrap_log"].exists()


def test_bootstrap_staging_failure_preserves_existing_config(
    bootstrap_fixture: dict[str, Path],
) -> None:
    """The portable replacement is prepared before the active config is changed."""
    fixture = bootstrap_fixture
    initialize_fixture_repository(fixture["source"])

    existing_config = fixture["omp_base"] / "config.yml"
    existing_config.parent.mkdir()
    previous_contents = b"legacy-setting: must-survive-staging-failure\n"
    existing_config.write_bytes(previous_contents)
    failing_install = fixture["fake_bin"] / "install"
    failing_install.write_text("#!/bin/sh\nexit 42\n")
    failing_install.chmod(0o755)

    result = run_bootstrap(fixture)

    assert result.returncode != 0
    assert existing_config.read_bytes() == previous_contents
    assert not fixture["bootstrap_log"].exists()


def test_bootstrap_preserves_relative_config_symlink(
    bootstrap_fixture: dict[str, Path],
) -> None:
    """Installing the profile updates a symlink target without replacing the link."""
    fixture = bootstrap_fixture
    initialize_fixture_repository(fixture["source"])

    config_link = fixture["omp_base"] / "config.yml"
    config_target = fixture["omp_base"].parent / "owner-config.yml"
    config_link.parent.mkdir()
    previous_contents = b"legacy-setting: preserve-symlink-target\n"
    config_target.write_bytes(previous_contents)
    config_link.symlink_to("../owner-config.yml")

    result = run_bootstrap(fixture)

    assert result.returncode == 0, result.stderr
    assert config_link.is_symlink()
    assert os.readlink(config_link) == "../owner-config.yml"
    assert config_target.read_bytes() == (
        fixture["source"] / "scripts" / PORTABLE_PROFILE.name
    ).read_bytes()
    backups = list(
        (fixture["state_home"] / "claude-orchestrate" / "machine-bootstrap-backups").glob("*")
    )
    assert any(path.read_bytes() == previous_contents for path in backups)


def test_install_configures_pocock_roles_only_with_explicit_flag(tmp_path: Path) -> None:
    """A normal artifact install leaves the owner config untouched."""
    home = tmp_path / "home"
    omp_base = tmp_path / "omp-base"
    config_path = omp_base / "config.yml"
    config_target = tmp_path / "owner-config.yml"
    home.mkdir()
    omp_base.mkdir()
    original_config = yaml.safe_dump(
        {
            "unrelated": "preserve",
            "modelRoles": {"pocock-retired": "retired/model"},
            "retry": {"fallbackChains": {"pocock-retired": ["retired/fallback"]}},
        },
        sort_keys=False,
    )
    config_target.write_text(original_config, encoding="utf-8")
    config_path.symlink_to("../owner-config.yml")
    environment = {
        **os.environ,
        "HOME": str(home),
        "PI_CODING_AGENT_DIR": str(omp_base),
        "XDG_STATE_HOME": str(tmp_path / "state"),
    }

    ordinary_install = subprocess.run(
        ["bash", str(INSTALLER)],
        cwd=REPOSITORY_ROOT,
        env=environment,
        capture_output=True,
        text=True,
    )

    assert ordinary_install.returncode == 0, ordinary_install.stderr
    assert config_path.is_symlink()
    assert config_target.read_text(encoding="utf-8") == original_config

    role_install = subprocess.run(
        ["bash", str(INSTALLER), "--configure-pocock-roles"],
        cwd=REPOSITORY_ROOT,
        env=environment,
        capture_output=True,
        text=True,
    )

    assert role_install.returncode == 0, role_install.stderr
    assert config_path.is_symlink()
    configured = yaml.safe_load(config_target.read_text(encoding="utf-8"))
    expected_roles = {
        manifest.stem
        for manifest in POCOCK_AGENT_MANIFESTS.glob("pocock-*.md")
        if manifest.is_file()
    }
    assert configured["unrelated"] == "preserve"
    assert {
        role for role in configured["modelRoles"] if role.startswith("pocock-")
    } == expected_roles
    assert {
        role
        for role in configured["retry"]["fallbackChains"]
        if role.startswith("pocock-")
    } == expected_roles


def test_install_copy_failure_preserves_existing_artifacts(tmp_path: Path) -> None:
    """Copy-mode staging keeps existing files intact when copying the source fails."""
    home = tmp_path / "home"
    omp_base = tmp_path / "omp-base"
    agent_directory = omp_base / "agents"
    fake_bin = tmp_path / "fake-bin"
    home.mkdir()
    agent_directory.mkdir(parents=True)
    fake_bin.mkdir()
    original_contents = {}
    for manifest in POCOCK_AGENT_MANIFESTS.glob("*.md"):
        destination = agent_directory / manifest.name
        contents = f"existing {manifest.name}\n"
        destination.write_text(contents, encoding="utf-8")
        original_contents[destination] = contents
    source_to_fail = sorted(POCOCK_AGENT_MANIFESTS.glob("*.md"))[0]
    failing_copy = fake_bin / "cp"
    failing_copy.write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        '  *"$FAIL_COPY_SOURCE"*) exit 42 ;;\n'
        '  *) exec /bin/cp "$@" ;;\n'
        "esac\n"
    )
    failing_copy.chmod(0o755)
    environment = {
        **os.environ,
        "HOME": str(home),
        "PI_CODING_AGENT_DIR": str(omp_base),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "FAIL_COPY_SOURCE": str(source_to_fail),
    }

    result = subprocess.run(
        ["bash", str(INSTALLER)],
        cwd=REPOSITORY_ROOT,
        env=environment,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert {
        destination: destination.read_text(encoding="utf-8")
        for destination in original_contents
    } == original_contents
    assert not list(agent_directory.glob("*.staging.*"))
    stage_root = omp_base / ".claude-orchestrate-staging"
    assert not list(stage_root.iterdir())


def test_install_stages_telemetry_before_publishing(tmp_path: Path) -> None:
    """A failed telemetry copy leaves the active runtime and its history intact."""
    home = tmp_path / "home"
    omp_base = tmp_path / "omp-base"
    runtime_destination = home / ".agents" / "skills" / "orchestrate"
    telemetry = runtime_destination / "telemetry"
    fake_bin = tmp_path / "fake-bin"
    telemetry.mkdir(parents=True)
    omp_base.mkdir()
    home.mkdir(exist_ok=True)
    fake_bin.mkdir()
    telemetry_file = telemetry / "routing.jsonl"
    telemetry_file.write_text("existing runtime history\n", encoding="utf-8")
    failing_copy = fake_bin / "cp"
    failing_copy.write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        '  *"/telemetry"*) exit 42 ;;\n'
        '  *) exec /bin/cp "$@" ;;\n'
        "esac\n"
    )
    failing_copy.chmod(0o755)
    environment = {
        **os.environ,
        "HOME": str(home),
        "PI_CODING_AGENT_DIR": str(omp_base),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
    }

    result = subprocess.run(
        ["bash", str(INSTALLER)],
        cwd=REPOSITORY_ROOT,
        env=environment,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert telemetry_file.read_text(encoding="utf-8") == "existing runtime history\n"
    assert runtime_destination.is_dir()
    stage_root = home / ".agents" / ".claude-orchestrate-staging"
    assert not list(stage_root.iterdir())


def test_install_migrates_legacy_telemetry_when_runtime_is_current(tmp_path: Path) -> None:
    """A legacy pocock-run history is migrated even when runtime code is current."""
    home = tmp_path / "home"
    omp_base = tmp_path / "omp-base"
    runtime_source = REPOSITORY_ROOT / "skill" / "orchestrate"
    runtime_destination = home / ".agents" / "skills" / "orchestrate"
    legacy_telemetry = home / ".agents" / "skills" / "pocock-run" / "telemetry"
    home.mkdir()
    omp_base.mkdir()
    runtime_destination.parent.mkdir(parents=True)
    shutil.copytree(runtime_source, runtime_destination, symlinks=True)
    legacy_telemetry.mkdir(parents=True)
    (legacy_telemetry / "routing.jsonl").write_text("legacy history\n", encoding="utf-8")
    environment = {
        **os.environ,
        "HOME": str(home),
        "PI_CODING_AGENT_DIR": str(omp_base),
        "XDG_STATE_HOME": str(tmp_path / "state"),
    }

    result = subprocess.run(
        ["bash", str(INSTALLER)],
        cwd=REPOSITORY_ROOT,
        env=environment,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert (runtime_destination / "telemetry" / "routing.jsonl").read_text(
        encoding="utf-8"
    ) == "legacy history\n"


def test_install_recovers_ready_stage_after_interrupted_agent_publish(tmp_path: Path) -> None:
    """A rerun publishes a complete ready stage left between backup and rename."""
    home = tmp_path / "home"
    omp_base = tmp_path / "omp-base"
    agent_directory = omp_base / "agents"
    fake_bin = tmp_path / "fake-bin"
    manifest = sorted(POCOCK_AGENT_MANIFESTS.glob("*.md"))[0]
    destination = agent_directory / manifest.name
    stage_root = omp_base / ".claude-orchestrate-staging"
    home.mkdir()
    agent_directory.mkdir(parents=True)
    fake_bin.mkdir()
    destination.write_text("old agent\n", encoding="utf-8")
    interrupting_move = fake_bin / "mv"
    interrupting_move.write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        '  *"$INTERRUPT_STAGING"*) kill -KILL "$PPID"; exit 137 ;;\n'
        '  *) exec /bin/mv "$@" ;;\n'
        "esac\n"
    )
    interrupting_move.chmod(0o755)
    interrupted_environment = {
        **os.environ,
        "HOME": str(home),
        "PI_CODING_AGENT_DIR": str(omp_base),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "INTERRUPT_STAGING": str(stage_root / f"{manifest.name}.staging."),
    }

    interrupted = subprocess.run(
        ["bash", str(INSTALLER)],
        cwd=REPOSITORY_ROOT,
        env=interrupted_environment,
        capture_output=True,
        text=True,
    )

    assert interrupted.returncode != 0
    assert not destination.exists()
    normal_environment = {
        **interrupted_environment,
        "PATH": os.environ["PATH"],
    }
    recovered = subprocess.run(
        ["bash", str(INSTALLER)],
        cwd=REPOSITORY_ROOT,
        env=normal_environment,
        capture_output=True,
        text=True,
    )

    assert recovered.returncode == 0, recovered.stderr
    assert "recovered interrupted copy" in recovered.stdout
    assert destination.read_bytes() == manifest.read_bytes()
    assert not list(agent_directory.glob(f"{manifest.name}.staging.*"))
    assert not list(stage_root.glob(f"{manifest.name}.staging.*"))


def test_install_publishes_runtime_before_adapter_failure(tmp_path: Path) -> None:
    """A failed strict-adapter update cannot leave it ahead of the runtime."""
    home = tmp_path / "home"
    omp_base = tmp_path / "omp-base"
    runtime_destination = home / ".agents" / "skills" / "orchestrate"
    extension_destination = omp_base / "extensions" / "pocock-control"
    extension_source = REPOSITORY_ROOT / ".omp" / "extensions" / "pocock-control"
    fake_bin = tmp_path / "fake-bin"
    runtime_destination.mkdir(parents=True)
    extension_destination.mkdir(parents=True)
    omp_base.mkdir(exist_ok=True)
    fake_bin.mkdir()
    (runtime_destination / "old-runtime").write_text("old\n", encoding="utf-8")
    adapter_marker = extension_destination / "adapter-version"
    adapter_marker.write_text("old adapter\n", encoding="utf-8")
    failing_copy = fake_bin / "cp"
    failing_copy.write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        '  *"$FAIL_COPY_SOURCE"*) exit 42 ;;\n'
        '  *) exec /bin/cp "$@" ;;\n'
        "esac\n"
    )
    failing_copy.chmod(0o755)
    environment = {
        **os.environ,
        "HOME": str(home),
        "PI_CODING_AGENT_DIR": str(omp_base),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "FAIL_COPY_SOURCE": str(extension_source),
    }

    result = subprocess.run(
        ["bash", str(INSTALLER)],
        cwd=REPOSITORY_ROOT,
        env=environment,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert (runtime_destination / "SKILL.md").read_bytes() == (
        REPOSITORY_ROOT / "skill" / "orchestrate" / "SKILL.md"
    ).read_bytes()
    assert (runtime_destination / "tools" / "omp_runtime.py").read_bytes() == (
        REPOSITORY_ROOT / "skill" / "orchestrate" / "tools" / "omp_runtime.py"
    ).read_bytes()
    assert adapter_marker.read_text(encoding="utf-8") == "old adapter\n"


def test_portable_profile_declares_pocock_agent_roles_and_rejects_missing_role(
    tmp_path: Path,
) -> None:
    """The portable profile and validator track the current Pocock manifests."""
    pocock_roles = {
        manifest.stem
        for manifest in POCOCK_AGENT_MANIFESTS.glob("pocock-*.md")
        if manifest.is_file()
    }
    assert len(pocock_roles) == 5

    profile = yaml.safe_load(PORTABLE_PROFILE.read_text(encoding="utf-8"))
    model_roles = profile["modelRoles"]
    declared_pocock_roles = {
        role for role in model_roles if role.startswith("pocock-")
    }
    assert declared_pocock_roles == pocock_roles
    assert all(
        isinstance(model_roles[role], str) and model_roles[role].strip()
        for role in pocock_roles
    )
    fallback_chains = profile["retry"]["fallbackChains"]
    assert {
        role for role in fallback_chains if role.startswith("pocock-")
    } == pocock_roles
    assert all(fallback_chains[role] for role in pocock_roles)

    missing_role = sorted(pocock_roles)[0]
    del model_roles[missing_role]
    missing_role_profile = tmp_path / "missing-pocock-role.yml"
    missing_role_profile.write_text(
        yaml.safe_dump(profile, sort_keys=False),
        encoding="utf-8",
    )

    validation = subprocess.run(
        [sys.executable, str(PROFILE_VALIDATOR), str(missing_role_profile)],
        capture_output=True,
        text=True,
    )
    assert validation.returncode != 0
    assert missing_role in validation.stderr

    profile = yaml.safe_load(PORTABLE_PROFILE.read_text(encoding="utf-8"))
    del profile["retry"]["fallbackChains"][missing_role]
    missing_chain_profile = tmp_path / "missing-pocock-chain.yml"
    missing_chain_profile.write_text(
        yaml.safe_dump(profile, sort_keys=False),
        encoding="utf-8",
    )
    chain_validation = subprocess.run(
        [sys.executable, str(PROFILE_VALIDATOR), str(missing_chain_profile)],
        capture_output=True,
        text=True,
    )
    assert chain_validation.returncode != 0
    assert missing_role in chain_validation.stderr


@pytest.mark.parametrize("section", ["modelRoles", "fallbackChains"])
def test_portable_profile_rejects_retired_pocock_backup_routes(
    tmp_path: Path,
    section: str,
) -> None:
    profile = yaml.safe_load(PORTABLE_PROFILE.read_text(encoding="utf-8"))
    target = profile["modelRoles"] if section == "modelRoles" else profile["retry"]["fallbackChains"]
    target["pocock-retired-backup"] = (
        "retired/model" if section == "modelRoles" else ["retired/fallback"]
    )
    invalid_profile = tmp_path / f"retired-{section}.yml"
    invalid_profile.write_text(yaml.safe_dump(profile, sort_keys=False), encoding="utf-8")

    validation = subprocess.run(
        [sys.executable, str(PROFILE_VALIDATOR), str(invalid_profile)],
        capture_output=True,
        text=True,
    )

    assert validation.returncode != 0
    assert "pocock-retired-backup" in validation.stderr


def isolated_exporter(tmp_path: Path) -> tuple[Path, Path]:
    """Copy the real exporter into a throwaway checkout.

    The exporter writes beside itself, so an isolated copy is what keeps a test
    from overwriting the committed profile. The validator it imports resolves the
    agent manifests relative to the same checkout, so those travel too.
    """
    scripts = tmp_path / "scripts"
    scripts.mkdir(exist_ok=True)
    for source in (PROFILE_EXPORTER, PROFILE_VALIDATOR):
        (scripts / source.name).write_bytes(source.read_bytes())
    manifests = tmp_path / ".omp" / "agents"
    manifests.mkdir(parents=True, exist_ok=True)
    for manifest in POCOCK_AGENT_MANIFESTS.glob("pocock-*.md"):
        (manifests / manifest.name).write_bytes(manifest.read_bytes())
    return scripts / PROFILE_EXPORTER.name, scripts / PORTABLE_PROFILE.name


def test_exporter_mirrors_the_live_config_without_machine_local_keys(tmp_path: Path) -> None:
    """The snapshot must equal the live config minus what cannot travel.

    Hand-maintaining this file is what let the committed snapshot fall behind the
    live routes, so the exporter's whole value is that the two cannot diverge.
    """
    exporter, destination = isolated_exporter(tmp_path)
    live = yaml.safe_load(PORTABLE_PROFILE.read_text(encoding="utf-8"))
    live["setupVersion"] = 1
    live["dev"] = {"autoqaConsent": "granted"}
    live_path = tmp_path / "live-config.yml"
    live_path.write_text(yaml.safe_dump(live, sort_keys=False), encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(exporter), str(live_path)],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    exported = yaml.safe_load(destination.read_text(encoding="utf-8"))
    expected = {key: value for key, value in live.items() if key not in {"dev", "setupVersion"}}
    assert exported == expected
    assert list(exported) == list(expected)
    assert "dev" not in exported and "setupVersion" not in exported

    validation = subprocess.run(
        [sys.executable, str(tmp_path / "scripts" / PROFILE_VALIDATOR.name), str(destination)],
        capture_output=True,
        text=True,
    )
    assert validation.returncode == 0, validation.stderr


def test_exporter_refuses_a_secret_bearing_config_and_keeps_the_previous_snapshot(
    tmp_path: Path,
) -> None:
    """A rejected export must never replace a good snapshot."""
    exporter, destination = isolated_exporter(tmp_path)
    destination.write_text("# committed snapshot\n", encoding="utf-8")
    live = yaml.safe_load(PORTABLE_PROFILE.read_text(encoding="utf-8"))
    live["providers"]["anthropicApiKey"] = "sk-ant-fixture"
    live_path = tmp_path / "live-with-secret.yml"
    live_path.write_text(yaml.safe_dump(live, sort_keys=False), encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(exporter), str(live_path)],
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "anthropicApiKey" in result.stderr
    assert destination.read_text(encoding="utf-8") == "# committed snapshot\n"


def test_runtime_config_validator_accepts_yaml_mapping_keys_with_trailing_space(tmp_path: Path) -> None:
    profile = yaml.safe_load(PORTABLE_PROFILE.read_text(encoding="utf-8"))
    serialized = yaml.safe_dump(profile, sort_keys=False)
    serialized = serialized.replace("task:\n", "task: \n").replace(
        "  isolation:\n", "  isolation: \n"
    ).replace("retry:\n", "retry: \n")
    config = tmp_path / "config.yml"
    config.write_text(serialized, encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(RUNTIME_CONFIG_VALIDATOR), str(config)],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "task-инварианты явно заданы" in result.stdout


def test_runtime_config_validator_reports_missing_explicit_invariant(tmp_path: Path) -> None:
    profile = yaml.safe_load(PORTABLE_PROFILE.read_text(encoding="utf-8"))
    del profile["display"]["showTokenUsage"]
    config = tmp_path / "config.yml"
    config.write_text(yaml.safe_dump(profile, sort_keys=False), encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(RUNTIME_CONFIG_VALIDATOR), str(config)],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "display.showTokenUsage" in result.stderr


@pytest.mark.parametrize(
    ("section", "key", "value", "expected_path"),
    [
        ("retry", "enabled", False, "retry.enabled"),
        ("task", "maxRuntimeMs", 0, "task.maxRuntimeMs"),
    ],
)
def test_runtime_config_validator_rejects_invalid_explicit_invariants(
    tmp_path: Path,
    section: str,
    key: str,
    value: object,
    expected_path: str,
) -> None:
    profile = yaml.safe_load(PORTABLE_PROFILE.read_text(encoding="utf-8"))
    profile[section][key] = value
    config = tmp_path / "config.yml"
    config.write_text(yaml.safe_dump(profile, sort_keys=False), encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(RUNTIME_CONFIG_VALIDATOR), str(config)],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert expected_path in result.stderr


def test_portable_profile_rejects_integer_for_boolean_enabled_value(tmp_path: Path) -> None:
    profile = yaml.safe_load(PORTABLE_PROFILE.read_text(encoding="utf-8"))
    profile["async"]["enabled"] = 0
    invalid_profile = tmp_path / "integer-enabled.yml"
    invalid_profile.write_text(yaml.safe_dump(profile, sort_keys=False), encoding="utf-8")

    validation = subprocess.run(
        [sys.executable, str(PROFILE_VALIDATOR), str(invalid_profile)],
        capture_output=True,
        text=True,
    )

    assert validation.returncode != 0
    assert "async.enabled" in validation.stderr


@pytest.mark.parametrize("section", ["modelRoles", "fallbackChains"])
def test_portable_profile_rejects_unexpected_pocock_route_names(
    tmp_path: Path,
    section: str,
) -> None:
    profile = yaml.safe_load(PORTABLE_PROFILE.read_text(encoding="utf-8"))
    target = profile["modelRoles"] if section == "modelRoles" else profile["retry"]["fallbackChains"]
    target["pocock-typo"] = "unexpected/model" if section == "modelRoles" else ["unexpected/model"]
    invalid_profile = tmp_path / f"unexpected-{section}.yml"
    invalid_profile.write_text(yaml.safe_dump(profile, sort_keys=False), encoding="utf-8")

    validation = subprocess.run(
        [sys.executable, str(PROFILE_VALIDATOR), str(invalid_profile)],
        capture_output=True,
        text=True,
    )

    assert validation.returncode != 0
    assert "pocock-typo" in validation.stderr


def test_portable_profile_rejects_null_fallback_chain_entry(tmp_path: Path) -> None:
    profile = yaml.safe_load(PORTABLE_PROFILE.read_text(encoding="utf-8"))
    pocock_role = sorted(
        manifest.stem
        for manifest in POCOCK_AGENT_MANIFESTS.glob("pocock-*.md")
        if manifest.is_file()
    )[0]
    profile["retry"]["fallbackChains"][pocock_role] = [None]
    invalid_profile = tmp_path / "null-fallback-chain.yml"
    invalid_profile.write_text(yaml.safe_dump(profile, sort_keys=False), encoding="utf-8")

    validation = subprocess.run(
        [sys.executable, str(PROFILE_VALIDATOR), str(invalid_profile)],
        capture_output=True,
        text=True,
    )

    assert validation.returncode != 0
    assert pocock_role in validation.stderr


def test_portable_profile_rejects_noncanonical_isolation_mode(tmp_path: Path) -> None:
    profile = yaml.safe_load(PORTABLE_PROFILE.read_text(encoding="utf-8"))
    profile["task"]["isolation"]["mode"] = "rcpoy"
    invalid_profile = tmp_path / "invalid-isolation-mode.yml"
    invalid_profile.write_text(yaml.safe_dump(profile, sort_keys=False), encoding="utf-8")

    validation = subprocess.run(
        [sys.executable, str(PROFILE_VALIDATOR), str(invalid_profile)],
        capture_output=True,
        text=True,
    )

    assert validation.returncode != 0
    assert "task.isolation.mode" in validation.stderr
