"""test_run_lane.py — deterministic tests for the run-lane slice-3 package.
Style mirrors test_validate_config.py: subprocess.run against the real shim
for end-to-end coverage, tmp_path fixtures, and no touching the real
config.yaml. NO test invokes a real vendor model — every CLI-level test runs
against a small fake `agy` binary written to a tmp bin dir; every substrate-
level test either uses that same fake binary or a pure in-memory
FakeSubstrate. See design-runlane.md §6.
"""
from __future__ import annotations

import dataclasses
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from run_lane import adapters, artifact, config_resolve, envelope, substrate
from run_lane.envelope import LaneError

TOOLS_DIR = Path(__file__).resolve().parent.parent
RUN_LANE = TOOLS_DIR / "run-lane"

# --- shared fixtures ----------------------------------------------------------

MINIMAL_CONFIG = """
cross_provider:
  lanes:
    agy-test-lane:
      transport: agy-print
      model: "Test Model X"
      effort: medium
      vendor: google
      surface: google-antigravity
    agy-noschema-lane:
      transport: agy-print
      model: "Test Model X"
      effort: medium
      capabilities:
        supports_schema: "no"
    agy-noeffort-lane:
      transport: agy-print
      model: "Test Model X"
      effort: medium
      capabilities:
        supports_effort: "no"
    agy-no-transport-lane:
      model: "orphan"
    agy-unquoted-trap-lane:
      transport: agy-print
      model: "Test Model X"
      effort: medium
      capabilities:
        supports_schema: no
    codex-critic-test-lane:            # stand-in for the real, reachable
                                       # codex-critic/codex-code/grok-build/
                                       # kimi-k3 lanes — transport codex-cli
                                       # has NO adapter body in slice 3
                                       # (CAPS is None, a declared stub).
      transport: codex-cli
      model: "gpt-5.6-sol"
      effort: xhigh
availability:
  aliases:
    agy-alias: agy-test-lane
"""

# A fake `agy` binary that speaks exactly the subset of the real CLI's
# observable behaviour AgyAdapter/parse_model_witness depend on: it writes
# the two witness lines to --log-file, and (via a RUN_LANE_ARTIFACT_PATH
# marker it parses out of the prompt, exactly like a compliant agent would)
# writes the declared artifact. Behaviour toggles come from env vars so one
# script covers the happy path, a nonzero exit, a skipped artifact, a
# missing witness line, and the promptLength trap.
_FAKE_AGY_SOURCE = '''#!{python}
import os
import re
import sys
from pathlib import Path

argv = sys.argv[1:]


def opt(flag):
    return argv[argv.index(flag) + 1] if flag in argv else None


model = opt("--model") or ""
log_file = opt("--log-file")
prompt = argv[-1] if argv else ""

if os.environ.get("FAKE_AGY_BROKEN_LOG") == "1":
    log_text = "no witness line in this log\\n"
elif os.environ.get("FAKE_AGY_TRAP") == "1":
    log_text = (
        'INFO Propagating selected model override to backend: label="' + model + '"\\n'
        'INFO Print mode: starting (promptLength=7, model="--model")\\n'
    )
else:
    log_text = (
        'INFO Propagating selected model override to backend: label="' + model + '"\\n'
        'INFO Print mode: starting (promptLength=' + str(len(prompt)) + ', model="' + model + '")\\n'
    )
if log_file:
    Path(log_file).write_text(log_text)

if os.environ.get("FAKE_AGY_SKIP_OUT") != "1":
    match = re.search(r"RUN-LANE-ARTIFACT-PATH: (\\S+)", prompt)
    if match:
        Path(match.group(1)).write_text("fake agy artifact\\n")

sys.stdout.write("fake-agy: ok\\n")
sys.exit(int(os.environ.get("FAKE_AGY_EXIT", "0")))
'''


def _write_fake_agy(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    fake = bin_dir / "agy"
    fake.write_text(_FAKE_AGY_SOURCE.format(python=sys.executable))
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return fake


def _make_workdir(tmp_path: Path, prompt_text: str = "do the thing") -> tuple:
    workdir = tmp_path / "work"
    workdir.mkdir()
    prompt_file = workdir / "prompt.md"
    prompt_file.write_text(prompt_text)
    out = workdir / "out.md"
    return workdir, prompt_file, out


def _run_cli(args_list, env) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(RUN_LANE), *args_list],
        capture_output=True, text=True, env=env,
    )


def _cli_env(tmp_path: Path, extra: dict | None = None) -> dict:
    fake_agy = _write_fake_agy(tmp_path)
    env = os.environ.copy()
    env["PATH"] = f"{fake_agy.parent}{os.pathsep}{env.get('PATH', '')}"
    if extra:
        env.update(extra)
    return env


# =============================================================================
# 1. build_invocation — pure function, argv shape (design-runlane.md §6)
# =============================================================================


def test_agy_build_invocation_flag_order_and_stdin(tmp_path):
    workdir, prompt_file, out = _make_workdir(tmp_path)
    adapter = adapters.AgyAdapter()
    req = adapters.InvocationRequest(
        prompt_file=prompt_file, workdir=workdir, out=out,
        effort="high", model="Gemini 3.6 Flash (High)", timeout=120,
    )
    inv = adapter.build_invocation(lane=None, req=req)

    assert inv.argv[0] == "agy"
    assert inv.stdin_policy == "devnull"
    assert "--add-dir" in inv.argv
    assert inv.argv[inv.argv.index("--add-dir") + 1] == str(workdir)
    assert "--log-file" in inv.argv
    assert "--model" in inv.argv
    assert inv.argv[inv.argv.index("--model") + 1] == "Gemini 3.6 Flash (High)"
    assert "--effort" in inv.argv
    assert inv.argv[inv.argv.index("--effort") + 1] == "high"
    assert "--print-timeout" in inv.argv
    assert inv.argv[inv.argv.index("--print-timeout") + 1] == "120s"

    # every flag before -p; the prompt text is the very last argv element
    p_index = inv.argv.index("-p")
    assert p_index == len(inv.argv) - 2
    assert inv.argv[-1] != "-p"
    for flag in ("--add-dir", "--log-file", "--model", "--effort", "--print-timeout"):
        assert inv.argv.index(flag) < p_index

    assert f"RUN-LANE-ARTIFACT-PATH: {out}" in inv.prompt_addendum
    assert f"RUN-LANE-ARTIFACT-PATH: {out}" in inv.argv[-1]
    assert inv.prompt_length == len(inv.argv[-1])


def test_agy_build_invocation_omits_optional_flags_when_absent(tmp_path):
    workdir, prompt_file, out = _make_workdir(tmp_path)
    adapter = adapters.AgyAdapter()
    req = adapters.InvocationRequest(prompt_file=prompt_file, workdir=workdir, out=out)
    inv = adapter.build_invocation(lane=None, req=req)
    assert "--effort" not in inv.argv
    assert "--model" not in inv.argv
    assert "--print-timeout" not in inv.argv


def test_agy_build_invocation_missing_prompt_file_is_config_error(tmp_path):
    workdir = tmp_path / "work"
    workdir.mkdir()
    adapter = adapters.AgyAdapter()
    req = adapters.InvocationRequest(
        prompt_file=workdir / "missing.md", workdir=workdir, out=workdir / "out.md")
    with pytest.raises(LaneError) as excinfo:
        adapter.build_invocation(lane=None, req=req)
    assert excinfo.value.error_class == "config"


# =============================================================================
# 2. model witness — fixture log lines (design-runlane.md §4/§6)
# =============================================================================


def test_parse_model_witness_from_fixture_log(tmp_path):
    log_file = tmp_path / "witness.log"
    log_file.write_text(
        'INFO Propagating selected model override to backend: label="Gemini 3.6 Flash (Low)"\n'
        'INFO Print mode: starting (promptLength=357, model="Gemini 3.6 Flash (Low)")\n'
    )
    inv = adapters.Invocation(argv=[], env={}, stdin_policy="devnull", cwd=None,
                               prompt_addendum=None, log_file=str(log_file),
                               prompt_length=400)
    obs = adapters.AgyAdapter().parse_model_witness(res=None, inv=inv)
    assert obs.observed == "Gemini 3.6 Flash (Low)"
    assert obs.verification == "log"
    assert obs.error is None
    assert "Propagating" in obs.evidence


def test_parse_model_witness_prompt_length_trap(tmp_path):
    log_file = tmp_path / "witness.log"
    log_file.write_text(
        'INFO Propagating selected model override to backend: label=""\n'
        'INFO Print mode: starting (promptLength=7, model="--model")\n'
    )
    inv = adapters.Invocation(argv=[], env={}, stdin_policy="devnull", cwd=None,
                               prompt_addendum=None, log_file=str(log_file),
                               prompt_length=512)
    obs = adapters.AgyAdapter().parse_model_witness(res=None, inv=inv)
    assert obs.error is not None
    assert "promptLength trap" in obs.error


def test_parse_model_witness_missing_propagating_line_is_unavailable(tmp_path):
    log_file = tmp_path / "witness.log"
    log_file.write_text("nothing useful in this log\n")
    inv = adapters.Invocation(argv=[], env={}, stdin_policy="devnull", cwd=None,
                               prompt_addendum=None, log_file=str(log_file),
                               prompt_length=10)
    obs = adapters.AgyAdapter().parse_model_witness(res=None, inv=inv)
    assert obs.observed is None
    assert obs.verification == "unavailable"
    assert obs.error is not None


def test_parse_model_witness_missing_log_file_is_unavailable(tmp_path):
    inv = adapters.Invocation(argv=[], env={}, stdin_policy="devnull", cwd=None,
                               prompt_addendum=None, log_file=str(tmp_path / "nope.log"),
                               prompt_length=10)
    obs = adapters.AgyAdapter().parse_model_witness(res=None, inv=inv)
    assert obs.verification == "unavailable"
    assert obs.error is not None


# =============================================================================
# 3. envelope.compute_ok — table-driven (exit code x artifact x model match)
# =============================================================================


@pytest.mark.parametrize(
    "exit_code,artifact_present,verification,declared,observed,error,expected", [
        (0, True, "log", "M", "M", None, True),
        (0, True, "log", "M", "OTHER", None, False),      # model mismatch
        (1, True, "log", "M", "M", None, False),           # nonzero exit
        (0, False, "log", "M", "M", None, False),          # missing artifact
        (0, True, "none", "M", "OTHER", None, True),        # no witness: conjunct dropped
        (0, True, "log", "M", "M", {"class": "hardening-gate", "message": "x"}, False),
    ])
def test_compute_ok_table(exit_code, artifact_present, verification, declared,
                           observed, error, expected):
    assert envelope.compute_ok(
        exit_code=exit_code, artifact_present=artifact_present,
        model_verification=verification, model_declared=declared,
        model_observed=observed, error=error) is expected


def test_envelope_build_carries_substrate_field_and_all_adr_keys():
    env = envelope.build(
        lane="agy-test-lane", transport="agy-print", substrate="subprocess",
        model_declared="M", model_observed="M", model_verification="log",
        effort="medium", artifact={"path": "/x", "present": True, "bytes": 1, "sha256": "a"},
        printed_text="hi", printed_truncated=False, schema_enforcement="prompt_asked",
        duration_ms=10, usage=None, session_id=None,
        sandbox={"profile": None, "applied": None, "violations": []},
        command=["agy"], evidence={"log_path": "/x.log", "model_line": "..."},
        exit_code=0, error=None,
    )
    expected_keys = {
        "lane", "transport", "substrate", "ok", "model_declared", "model_observed",
        "effort", "artifact", "printed_text", "printed_truncated", "schema_enforcement",
        "durationMs", "usage", "sessionId", "sandbox", "command", "evidence", "error",
    }
    assert set(env) == expected_keys
    assert env["substrate"] == "subprocess"
    assert env["ok"] is True


def test_lane_error_rejects_unknown_class():
    with pytest.raises(ValueError):
        LaneError("not-a-real-class", "boom")


# =============================================================================
# 4. config_resolve — alias resolution, missing lane, capabilities override
# =============================================================================


def test_resolve_lane_direct_name(tmp_path):
    config = {"cross_provider": {"lanes": {"agy-test-lane": {
        "transport": "agy-print", "model": "M", "effort": "medium"}}}}
    lane = config_resolve.resolve_lane(config, "agy-test-lane")
    assert lane.name == "agy-test-lane"
    assert lane.transport == "agy-print"
    assert lane.effort == "medium"


def test_resolve_lane_via_alias():
    config = {
        "cross_provider": {"lanes": {"codex-critic": {
            "transport": "codex-cli", "model": "gpt-5.6-sol", "effort": "xhigh"}}},
        "availability": {"aliases": {"sol": "codex-critic"}},
    }
    lane = config_resolve.resolve_lane(config, "sol")
    assert lane.name == "codex-critic"
    assert lane.transport == "codex-cli"


def test_resolve_lane_unknown_raises_config_error():
    with pytest.raises(LaneError) as excinfo:
        config_resolve.resolve_lane({}, "does-not-exist")
    assert excinfo.value.error_class == "config"


def test_resolve_lane_without_transport_raises_config_error():
    config = {"cross_provider": {"lanes": {"broken": {"model": "M"}}}}
    with pytest.raises(LaneError) as excinfo:
        config_resolve.resolve_lane(config, "broken")
    assert excinfo.value.error_class == "config"


def test_load_config_missing_file_raises_config_error(tmp_path):
    with pytest.raises(LaneError) as excinfo:
        config_resolve.load_config(tmp_path / "nope.yaml")
    assert excinfo.value.error_class == "config"


# =============================================================================
# 5. capabilities — CAPS constant + per-lane hybrid override (F-Caps)
# =============================================================================


def test_effective_capabilities_defaults_to_class_caps():
    lane = config_resolve.LaneRecord(
        name="agy-test-lane", transport="agy-print", model="M", effort="medium",
        vendor="google", surface="google-antigravity", sandbox=None, roles=(),
        status=None, capabilities_override={}, raw={})
    caps = adapters.AgyAdapter().effective_capabilities(lane)
    assert caps == adapters.AgyAdapter.CAPS


def test_effective_capabilities_lane_override_wins():
    lane = config_resolve.LaneRecord(
        name="agy-noschema-lane", transport="agy-print", model="M", effort="medium",
        vendor=None, surface=None, sandbox=None, roles=(), status=None,
        capabilities_override={"supports_schema": "no"}, raw={})
    caps = adapters.AgyAdapter().effective_capabilities(lane)
    assert caps.supports_schema == "no"
    # the class constant itself is untouched — the override is per-call
    assert adapters.AgyAdapter.CAPS.supports_schema == "prompt"


def test_effective_capabilities_rejects_unknown_override_key():
    lane = config_resolve.LaneRecord(
        name="bad-lane", transport="agy-print", model="M", effort="medium",
        vendor=None, surface=None, sandbox=None, roles=(), status=None,
        capabilities_override={"not_a_real_capability": "no"}, raw={})
    with pytest.raises(LaneError) as excinfo:
        adapters.AgyAdapter().effective_capabilities(lane)
    assert excinfo.value.error_class == "config"


def test_effective_capabilities_rejects_unquoted_yaml_bool_value():
    """The actual failure mode: `capabilities: {supports_schema: no}` with
    NO quotes is parsed by PyYAML as `{"supports_schema": False}` — the
    override must be rejected loudly, not merged in as a non-string that
    every `caps.field == "no"` check downstream would silently fail to
    match (a quiet capability grant, exactly what ADR-0005 forbids)."""
    lane = config_resolve.LaneRecord(
        name="unquoted-trap-lane", transport="agy-print", model="M", effort="medium",
        vendor=None, surface=None, sandbox=None, roles=(), status=None,
        capabilities_override={"supports_schema": False}, raw={})
    with pytest.raises(LaneError) as excinfo:
        adapters.AgyAdapter().effective_capabilities(lane)
    assert excinfo.value.error_class == "config"
    assert "supports_schema" in excinfo.value.message
    assert "bool" in excinfo.value.message


def test_effective_capabilities_on_stub_adapter_raises_not_implemented():
    """CAPS is None on every not-yet-implemented adapter (codex/grok/kimi/
    claude-print) — effective_capabilities must fail the SAME way
    build_invocation does (NotImplementedError), not crash inside
    dataclasses.replace(None, ...) with an unclassified TypeError. This is
    the exact call __main__.run() makes BEFORE build_invocation, on a real,
    reachable lane (codex-critic et al.)."""
    lane = config_resolve.LaneRecord(
        name="codex-critic", transport="codex-cli", model="gpt-5.6-sol",
        effort="xhigh", vendor=None, surface=None, sandbox=None, roles=(),
        status=None, capabilities_override={}, raw={})
    with pytest.raises(NotImplementedError):
        adapters.CodexAdapter().effective_capabilities(lane)


# =============================================================================
# 6. get_adapter registry + future-slice stubs stay unimplemented
# =============================================================================


def test_get_adapter_returns_agy_adapter():
    adapter = adapters.get_adapter("agy-print")
    assert isinstance(adapter, adapters.AgyAdapter)


def test_get_adapter_unknown_transport_is_config_error():
    with pytest.raises(LaneError) as excinfo:
        adapters.get_adapter("some-future-cli")
    assert excinfo.value.error_class == "config"


@pytest.mark.parametrize("transport,cls", [
    ("codex-cli", adapters.CodexAdapter),
    ("grok-cli", adapters.GrokAdapter),
    ("kimi-cli-headless", adapters.KimiAdapter),
    ("claude-print", adapters.ClaudePrintAdapter),
])
def test_unimplemented_adapters_raise_not_implemented(transport, cls):
    adapter = adapters.get_adapter(transport)
    assert isinstance(adapter, cls)
    with pytest.raises(NotImplementedError):
        adapter.build_invocation(lane=None, req=None)


# =============================================================================
# 7. OrcaTerminalSubstrate — declared stub, not a real implementation
# =============================================================================


def test_orca_terminal_substrate_raises_config_error(tmp_path):
    inv = adapters.Invocation(argv=["agy"], env={}, stdin_policy="devnull",
                               cwd=str(tmp_path), prompt_addendum=None,
                               log_file=None)
    with pytest.raises(LaneError) as excinfo:
        substrate.OrcaTerminalSubstrate().run(inv, timeout=10)
    assert excinfo.value.error_class == "config"
    assert "orca-terminal" in excinfo.value.message
    assert "не реализован" in excinfo.value.message


# =============================================================================
# 8. axis separation — ONE AgyAdapter, TWO substrates (N+M, not NxM)
# =============================================================================


class FakeSubstrate:
    """A pure in-memory Substrate double: it never spawns anything and
    knows nothing about agy or any other CLI — it just returns whatever
    RunResult the test handed it, and records the Invocation it was given
    so the test can assert the SAME object crossed both substrates."""

    def __init__(self, result):
        self.result = result
        self.received_invocation = None

    def run(self, inv, timeout):
        self.received_invocation = inv
        return self.result


def test_one_adapter_runs_through_fake_and_subprocess_substrate(tmp_path):
    workdir, prompt_file, out = _make_workdir(tmp_path)
    lane = config_resolve.LaneRecord(
        name="agy-test-lane", transport="agy-print", model="Test Model X",
        effort="medium", vendor="google", surface="google-antigravity",
        sandbox=None, roles=(), status=None, capabilities_override={}, raw={})
    adapter = adapters.AgyAdapter()
    req = adapters.InvocationRequest(
        prompt_file=prompt_file, workdir=workdir, out=out,
        effort="medium", model="Test Model X")

    # Built exactly ONCE — this is the artefact both substrates are handed.
    inv = adapter.build_invocation(lane, req)

    # --- substrate A: FakeSubstrate, purely in-memory ---
    canned = substrate.RunResult(exit_code=0, stdout="fake-agy: ok\n", stderr="",
                                  duration_ms=1, timed_out=False)
    fake_sub = FakeSubstrate(canned)
    res_fake = fake_sub.run(inv, timeout=30)
    assert res_fake is canned
    assert fake_sub.received_invocation is inv

    # --- substrate B: the real SubprocessSubstrate, against a fake `agy` ---
    fake_agy = _write_fake_agy(tmp_path)
    inv_for_subprocess = dataclasses.replace(
        inv, argv=[str(fake_agy)] + inv.argv[1:], env={})
    real_sub = substrate.SubprocessSubstrate()
    res_real = real_sub.run(inv_for_subprocess, timeout=30)
    assert res_real.exit_code == 0
    assert res_real.timed_out is False
    assert "fake-agy: ok" in res_real.stdout

    # the SAME adapter's parsers work off either substrate's RunResult,
    # against the SAME Invocation it built once — the adapter never had to
    # change, or know, which substrate ran it.
    obs = adapter.parse_model_witness(res_real, inv)
    assert obs.observed == "Test Model X"
    assert obs.error is None

    art = artifact.capture(out)
    assert art["present"] is True


def test_adapters_module_never_imports_subprocess():
    """Structural guard for the physical separation claim (design-runlane.md
    §8): the transport axis must not be able to reach into process
    execution even by accident."""
    src = Path(adapters.__file__).read_text()
    assert "import subprocess" not in src
    assert "subprocess.run" not in src


def test_substrate_module_never_hardcodes_a_cli_name():
    src = Path(substrate.__file__).read_text().lower()
    assert "agy" not in src
    assert "codex" not in src
    assert "grok" not in src
    assert "kimi" not in src


# =============================================================================
# 9. CLI end-to-end — subprocess.run against the real shim (no live model)
# =============================================================================


def test_cli_happy_path_ok_true(tmp_path):
    workdir, prompt_file, out = _make_workdir(tmp_path)
    config = tmp_path / "config.yaml"
    config.write_text(MINIMAL_CONFIG)
    env = _cli_env(tmp_path)

    result = _run_cli([
        "--lane", "agy-test-lane", "--config", str(config),
        "--prompt-file", str(prompt_file), "--workdir", str(workdir),
        "--out", str(out),
    ], env=env)

    envelope_out = json.loads(result.stdout)
    assert result.returncode == 0, result.stderr
    assert envelope_out["ok"] is True
    assert envelope_out["transport"] == "agy-print"
    assert envelope_out["substrate"] == "subprocess"
    assert envelope_out["model_declared"] == "Test Model X"
    assert envelope_out["model_observed"] == "Test Model X"
    assert envelope_out["effort"] == "medium"          # defaulted from the lane
    assert envelope_out["artifact"]["present"] is True
    assert envelope_out["artifact"]["sha256"] is not None
    assert envelope_out["error"] is None


def test_cli_alias_resolves_to_canonical_lane(tmp_path):
    workdir, prompt_file, out = _make_workdir(tmp_path)
    config = tmp_path / "config.yaml"
    config.write_text(MINIMAL_CONFIG)
    env = _cli_env(tmp_path)

    result = _run_cli([
        "--lane", "agy-alias", "--config", str(config),
        "--prompt-file", str(prompt_file), "--workdir", str(workdir),
        "--out", str(out),
    ], env=env)
    envelope_out = json.loads(result.stdout)
    assert envelope_out["lane"] == "agy-test-lane"
    assert envelope_out["ok"] is True


def test_cli_effort_and_model_override_win_over_lane_defaults(tmp_path):
    workdir, prompt_file, out = _make_workdir(tmp_path)
    config = tmp_path / "config.yaml"
    config.write_text(MINIMAL_CONFIG)
    env = _cli_env(tmp_path)

    result = _run_cli([
        "--lane", "agy-test-lane", "--config", str(config),
        "--prompt-file", str(prompt_file), "--workdir", str(workdir),
        "--out", str(out), "--effort", "high", "--model", "Another Model",
    ], env=env)
    envelope_out = json.loads(result.stdout)
    assert envelope_out["effort"] == "high"
    assert envelope_out["model_declared"] == "Another Model"
    assert envelope_out["model_observed"] == "Another Model"
    assert envelope_out["ok"] is True


def test_cli_unknown_lane_is_config_error(tmp_path):
    workdir, prompt_file, out = _make_workdir(tmp_path)
    config = tmp_path / "config.yaml"
    config.write_text(MINIMAL_CONFIG)
    env = _cli_env(tmp_path)

    result = _run_cli([
        "--lane", "no-such-lane", "--config", str(config),
        "--prompt-file", str(prompt_file), "--workdir", str(workdir),
        "--out", str(out),
    ], env=env)
    envelope_out = json.loads(result.stdout)
    assert result.returncode == 1
    assert envelope_out["ok"] is False
    assert envelope_out["error"]["class"] == "config"


def test_cli_nonzero_exit_is_transport_death(tmp_path):
    workdir, prompt_file, out = _make_workdir(tmp_path)
    config = tmp_path / "config.yaml"
    config.write_text(MINIMAL_CONFIG)
    env = _cli_env(tmp_path, extra={"FAKE_AGY_EXIT": "3"})

    result = _run_cli([
        "--lane", "agy-test-lane", "--config", str(config),
        "--prompt-file", str(prompt_file), "--workdir", str(workdir),
        "--out", str(out),
    ], env=env)
    envelope_out = json.loads(result.stdout)
    assert result.returncode == 1
    assert envelope_out["ok"] is False
    assert envelope_out["error"]["class"] == "transport-death"


def test_cli_missing_artifact_with_zero_exit_has_no_error_class(tmp_path):
    """§5: ambiguity (exit clean, но артефакта нет) resolves TOWARD quality —
    run-lane reports ok:false without diagnosing further; it does not slap
    transport-death on a case that may simply be the model's own failure to
    comply with the artefact-channel instruction."""
    workdir, prompt_file, out = _make_workdir(tmp_path)
    config = tmp_path / "config.yaml"
    config.write_text(MINIMAL_CONFIG)
    env = _cli_env(tmp_path, extra={"FAKE_AGY_SKIP_OUT": "1"})

    result = _run_cli([
        "--lane", "agy-test-lane", "--config", str(config),
        "--prompt-file", str(prompt_file), "--workdir", str(workdir),
        "--out", str(out),
    ], env=env)
    envelope_out = json.loads(result.stdout)
    assert envelope_out["ok"] is False
    assert envelope_out["artifact"]["present"] is False
    assert envelope_out["error"] is None


def test_cli_prompt_length_trap_surfaces_as_transport_death(tmp_path):
    workdir, prompt_file, out = _make_workdir(tmp_path)
    config = tmp_path / "config.yaml"
    config.write_text(MINIMAL_CONFIG)
    env = _cli_env(tmp_path, extra={"FAKE_AGY_TRAP": "1"})

    result = _run_cli([
        "--lane", "agy-test-lane", "--config", str(config),
        "--prompt-file", str(prompt_file), "--workdir", str(workdir),
        "--out", str(out),
    ], env=env)
    envelope_out = json.loads(result.stdout)
    assert envelope_out["ok"] is False
    assert envelope_out["error"]["class"] == "transport-death"
    assert "promptLength trap" in envelope_out["error"]["message"]


def test_cli_broken_witness_log_surfaces_as_transport_death(tmp_path):
    workdir, prompt_file, out = _make_workdir(tmp_path)
    config = tmp_path / "config.yaml"
    config.write_text(MINIMAL_CONFIG)
    env = _cli_env(tmp_path, extra={"FAKE_AGY_BROKEN_LOG": "1"})

    result = _run_cli([
        "--lane", "agy-test-lane", "--config", str(config),
        "--prompt-file", str(prompt_file), "--workdir", str(workdir),
        "--out", str(out),
    ], env=env)
    envelope_out = json.loads(result.stdout)
    assert envelope_out["ok"] is False
    assert envelope_out["error"]["class"] == "transport-death"
    assert envelope_out["model_observed"] is None


def test_cli_unsupported_schema_request_is_config_error(tmp_path):
    workdir, prompt_file, out = _make_workdir(tmp_path)
    config = tmp_path / "config.yaml"
    config.write_text(MINIMAL_CONFIG)
    schema_file = workdir / "schema.json"
    schema_file.write_text("{}")
    env = _cli_env(tmp_path)

    result = _run_cli([
        "--lane", "agy-noschema-lane", "--config", str(config),
        "--prompt-file", str(prompt_file), "--workdir", str(workdir),
        "--out", str(out), "--schema", str(schema_file),
    ], env=env)
    envelope_out = json.loads(result.stdout)
    assert envelope_out["ok"] is False
    assert envelope_out["error"]["class"] == "config"


def test_cli_unsupported_effort_request_is_config_error(tmp_path):
    workdir, prompt_file, out = _make_workdir(tmp_path)
    config = tmp_path / "config.yaml"
    config.write_text(MINIMAL_CONFIG)
    env = _cli_env(tmp_path)

    result = _run_cli([
        "--lane", "agy-noeffort-lane", "--config", str(config),
        "--prompt-file", str(prompt_file), "--workdir", str(workdir),
        "--out", str(out), "--effort", "high",
    ], env=env)
    envelope_out = json.loads(result.stdout)
    assert envelope_out["ok"] is False
    assert envelope_out["error"]["class"] == "config"


def test_cli_lane_without_transport_is_config_error(tmp_path):
    workdir, prompt_file, out = _make_workdir(tmp_path)
    config = tmp_path / "config.yaml"
    config.write_text(MINIMAL_CONFIG)
    env = _cli_env(tmp_path)

    result = _run_cli([
        "--lane", "agy-no-transport-lane", "--config", str(config),
        "--prompt-file", str(prompt_file), "--workdir", str(workdir),
        "--out", str(out),
    ], env=env)
    envelope_out = json.loads(result.stdout)
    assert envelope_out["ok"] is False
    assert envelope_out["error"]["class"] == "config"


def test_cli_stub_transport_lane_prints_one_config_envelope_not_a_traceback(tmp_path):
    """Critic-gate finding (major): `run-lane --lane codex-critic ...` is a
    REAL, reachable input (codex-critic/codex-code/grok-build/kimi-k3 are
    live config lanes) whose transport has no adapter body in slice 3.
    Before the fix, `effective_capabilities` blew up inside
    `dataclasses.replace(None, ...)` with an unclassified TypeError that
    escaped both `except NotImplementedError` and `except LaneError` in
    __main__ — bare traceback on stderr, EMPTY stdout, unparseable by the
    lead. Must now print exactly one JSON envelope with error.class:config,
    never a traceback, never empty stdout."""
    workdir, prompt_file, out = _make_workdir(tmp_path)
    config = tmp_path / "config.yaml"
    config.write_text(MINIMAL_CONFIG)
    env = _cli_env(tmp_path)

    result = _run_cli([
        "--lane", "codex-critic-test-lane", "--config", str(config),
        "--prompt-file", str(prompt_file), "--workdir", str(workdir),
        "--out", str(out),
    ], env=env)

    assert result.stdout.strip() != "", (
        f"stdout was empty — a bare traceback escaped to stderr instead of "
        f"an envelope; stderr was:\n{result.stderr}")
    assert "Traceback" not in result.stderr
    envelope_out = json.loads(result.stdout)      # must be exactly one JSON object
    assert envelope_out["ok"] is False
    assert envelope_out["error"]["class"] == "config"
    assert result.returncode == 1


def test_cli_unquoted_yaml_no_capability_override_is_loud_config_error(tmp_path):
    """Critic-gate finding (minor): an UNQUOTED `capabilities: {supports_schema:
    no}` parses as `{"supports_schema": False}` (YAML 1.1 bool keyword);
    `False == "no"` is False, so the old code silently let --schema through
    with schema_enforcement staying wrong instead of refusing the request —
    a quiet capability grant. Must now be a loud error.class:config, never
    a silent pass-through."""
    workdir, prompt_file, out = _make_workdir(tmp_path)
    config = tmp_path / "config.yaml"
    config.write_text(MINIMAL_CONFIG)
    schema_file = workdir / "schema.json"
    schema_file.write_text("{}")
    env = _cli_env(tmp_path)

    result = _run_cli([
        "--lane", "agy-unquoted-trap-lane", "--config", str(config),
        "--prompt-file", str(prompt_file), "--workdir", str(workdir),
        "--out", str(out), "--schema", str(schema_file),
    ], env=env)
    envelope_out = json.loads(result.stdout)
    assert envelope_out["ok"] is False
    assert envelope_out["error"]["class"] == "config"
    assert "supports_schema" in envelope_out["error"]["message"]


def test_cli_substrate_orca_terminal_is_config_error_stub(tmp_path):
    workdir, prompt_file, out = _make_workdir(tmp_path)
    config = tmp_path / "config.yaml"
    config.write_text(MINIMAL_CONFIG)
    env = _cli_env(tmp_path)

    result = _run_cli([
        "--lane", "agy-test-lane", "--config", str(config),
        "--prompt-file", str(prompt_file), "--workdir", str(workdir),
        "--out", str(out), "--substrate", "orca-terminal",
    ], env=env)
    envelope_out = json.loads(result.stdout)
    assert envelope_out["ok"] is False
    assert envelope_out["error"]["class"] == "config"
    assert "orca-terminal" in envelope_out["error"]["message"]


def test_cli_substrate_defaults_to_subprocess(tmp_path):
    workdir, prompt_file, out = _make_workdir(tmp_path)
    config = tmp_path / "config.yaml"
    config.write_text(MINIMAL_CONFIG)
    env = _cli_env(tmp_path)

    result = _run_cli([
        "--lane", "agy-test-lane", "--config", str(config),
        "--prompt-file", str(prompt_file), "--workdir", str(workdir),
        "--out", str(out),
    ], env=env)
    envelope_out = json.loads(result.stdout)
    assert envelope_out["substrate"] == "subprocess"


def test_cli_missing_config_file_is_config_error(tmp_path):
    workdir, prompt_file, out = _make_workdir(tmp_path)
    env = _cli_env(tmp_path)

    result = _run_cli([
        "--lane", "agy-test-lane", "--config", str(tmp_path / "no-such-config.yaml"),
        "--prompt-file", str(prompt_file), "--workdir", str(workdir),
        "--out", str(out),
    ], env=env)
    envelope_out = json.loads(result.stdout)
    assert envelope_out["ok"] is False
    assert envelope_out["error"]["class"] == "config"


def test_cli_requires_all_mandatory_flags():
    result = subprocess.run(
        [sys.executable, str(RUN_LANE)], capture_output=True, text=True)
    assert result.returncode != 0
    assert result.stdout == ""
