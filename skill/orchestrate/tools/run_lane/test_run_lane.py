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
import shlex
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from run_lane import __main__ as run_lane_main
from run_lane import adapters, artifact, config_resolve, envelope, hardening, substrate
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
                                       # codex-critic/codex-code lanes —
                                       # transport codex-cli, CodexAdapter
                                       # (slice 4).
      transport: codex-cli
      model: "gpt-5.6-sol"
      effort: xhigh
      sandbox: read-only
    grok-test-lane:                    # stand-in for grok-build — transport
                                       # grok-cli, GrokAdapter (slice 5).
      transport: grok-cli
      model: "grok-4.5"
      effort: high
      sandbox: orchestrate
    kimi-test-lane:                    # stand-in for kimi-k3 — transport
                                       # kimi-cli-headless, KimiAdapter
                                       # (slice 5, no effort surface).
      transport: kimi-cli-headless
      model: "k3"
    claude-print-test-lane:            # not-Claude-host fallback transport,
                                       # ClaudePrintAdapter (slice 4).
      transport: claude-print
      model: "claude-fable-5"
      effort: high
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


# A fake `codex` binary: reads the WHOLE prompt from stdin (so a test can
# prove the real bytes reached the child, not merely an empty pipe — the
# exact defect the substrate.py stdin_data fix targets), emits a small
# --json JSONL stream (thread.started/turn.completed/item.completed, the
# same event shapes CodexAdapter parses), and writes the declared -o
# artifact. FAKE_CODEX_MODEL_EVENT adds an early event carrying a bare
# "model" field, to exercise the 'stream' branch of parse_model_witness.
_FAKE_CODEX_SOURCE = '''#!%%PYTHON%%
import json
import os
import sys
from pathlib import Path

argv = sys.argv[1:]


def opt(flag):
    return argv[argv.index(flag) + 1] if flag in argv else None


model = opt("-m") or ""
out_path = opt("-o")
stdin_text = sys.stdin.read()

events = []
if os.environ.get("FAKE_CODEX_MODEL_EVENT") == "1":
    events.append({"type": "some.early.event", "model": model})
events.append({"type": "thread.started", "thread_id": "thread-fake-123"})
events.append({"type": "turn.completed", "usage": {"input_tokens": 11, "output_tokens": 22}})
events.append({"type": "item.completed", "item": {"type": "agent_message", "text": "codex final response"}})

for ev in events:
    sys.stdout.write(json.dumps(ev) + "\\n")

if out_path and os.environ.get("FAKE_CODEX_SKIP_OUT") != "1":
    Path(out_path).write_text("fake codex artifact\\nstdin-length=" + str(len(stdin_text)) + "\\n")

sys.exit(int(os.environ.get("FAKE_CODEX_EXIT", "0")))
'''


def _write_fake_codex(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    fake = bin_dir / "codex"
    fake.write_text(_FAKE_CODEX_SOURCE.replace("%%PYTHON%%", sys.executable))
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return fake


# A fake `claude` binary that dumps the environment it actually received
# (via FAKE_CLAUDE_ENV_DUMP_PATH) — used to prove SubprocessSubstrate's
# env_unset really erases CLAUDECODE/CLAUDE_CODE_ENTRYPOINT rather than
# merely failing to override them — and prints a minimal
# `--output-format json`-shaped envelope.
_FAKE_CLAUDE_SOURCE = '''#!%%PYTHON%%
import json
import os
import sys
from pathlib import Path

dump_path = os.environ.get("FAKE_CLAUDE_ENV_DUMP_PATH")
if dump_path:
    Path(dump_path).write_text(json.dumps(dict(os.environ)))

result = {
    "modelUsage": {"claude-fable-5": {"inputTokens": 5, "outputTokens": 6}},
    "usage": {"input_tokens": 5, "output_tokens": 6},
    "session_id": "sess-fake-1",
    "result": "claude final response",
}
sys.stdout.write(json.dumps(result))
sys.exit(0)
'''


def _write_fake_claude(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    fake = bin_dir / "claude"
    fake.write_text(_FAKE_CLAUDE_SOURCE.replace("%%PYTHON%%", sys.executable))
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


class _DummyStubAdapter(adapters.LaneAdapter):
    """A locally-defined stand-in for "an adapter class registered with
    CAPS is None" — every real adapter is implemented as of slice 5, so
    this regression (originally reproduced live via CodexAdapter in slice
    3) needs its own fixture class rather than borrowing a now-implemented
    one."""

    transport = "dummy-stub"
    CAPS = None

    def build_invocation(self, lane, req):
        raise NotImplementedError("dummy stub — not implemented on purpose")

    def parse_model_witness(self, res, inv):
        raise NotImplementedError

    def parse_usage(self, res):
        raise NotImplementedError

    def parse_session_id(self, res):
        raise NotImplementedError

    def parse_printed_text(self, res):
        raise NotImplementedError


def test_effective_capabilities_on_stub_adapter_raises_not_implemented():
    """CAPS is None on any adapter class not yet filled in —
    effective_capabilities must fail the SAME way build_invocation does
    (NotImplementedError), not crash inside dataclasses.replace(None, ...)
    with an unclassified TypeError. This is the exact call __main__.run()
    makes BEFORE build_invocation, on a real, reachable lane — critic-gate
    finding, originally reproduced live via CodexAdapter before it was
    implemented (slice 3); CodexAdapter itself is implemented as of slice
    4, so the regression fixture moves to a dedicated dummy class."""
    lane = config_resolve.LaneRecord(
        name="dummy-lane", transport="dummy-stub", model="M",
        effort="medium", vendor=None, surface=None, sandbox=None, roles=(),
        status=None, capabilities_override={}, raw={})
    with pytest.raises(NotImplementedError):
        _DummyStubAdapter().effective_capabilities(lane)


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
    ("agy-print", adapters.AgyAdapter),
])
def test_get_adapter_returns_the_right_class_for_every_registered_transport(transport, cls):
    """As of slice 5 every registered transport has a real adapter body —
    the slice-3/4 placeholder version of this test (asserting
    NotImplementedError on the four not-yet-shipped adapters) is retired;
    this is its replacement, covering the full registry."""
    adapter = adapters.get_adapter(transport)
    assert isinstance(adapter, cls)


# =============================================================================
# 7. OrcaTerminalSubstrate — deterministic control-plane fake
# =============================================================================


def test_strip_terminal_control_restores_jsonl_from_osc_and_ansi_fixture():
    json_line = json.dumps({"type": "thread.started", "thread_id": "fixture"})
    noisy = "\x1b]133;C\x07\x1b[32m" + json_line + "\x1b[0m\n"

    cleaned = substrate.strip_terminal_control(noisy)

    assert cleaned == json_line + "\n"
    assert json.loads(cleaned) == {"type": "thread.started", "thread_id": "fixture"}


def test_orca_terminal_shell_and_control_commands_preserve_invocation_recipe(tmp_path):
    inv = adapters.Invocation(
        argv=["opaque-command", "argument with spaces"],
        env={"ADDED": "value with spaces"}, env_unset=("REMOVED",),
        stdin_policy="pipe", cwd=str(tmp_path), prompt_addendum=None, log_file=None)
    shell_command = substrate.build_orca_shell_command(
        inv, tmp_path / "stdout", tmp_path / "stderr", tmp_path / "rc",
        tmp_path / "stdin")
    words = shlex.split(shell_command)

    assert words[:5] == ["env", "-u", "REMOVED", "ADDED=value with spaces", "opaque-command"]
    assert words[5] == "argument with spaces"
    assert words[words.index(">") + 1] == str(tmp_path / "stdout")
    assert words[words.index("2>") + 1] == str(tmp_path / "stderr")
    assert words[words.index("<") + 1] == str(tmp_path / "stdin")
    # exit-code sentinel written last (the "finished" signal the substrate polls)
    assert shell_command.rstrip().endswith(f'echo "$?" > {shlex.quote(str(tmp_path / "rc"))}')
    assert substrate.build_orca_create_command("path:/work", "run-lane", shell_command) == [
        "orca", "terminal", "create", "--worktree", "path:/work", "--title",
        "run-lane", "--command", shell_command, "--json"]
    assert substrate.build_orca_close_command("term_fixture") == [
        "orca", "terminal", "close", "--terminal", "term_fixture", "--json"]


def test_orca_terminal_substrate_reads_clean_disk_output_and_closes(tmp_path):
    calls = []
    created_paths = {}

    def fake_orca_runner(command, **kwargs):
        calls.append(command)
        assert kwargs == {"capture_output": True, "text": True}
        action = command[1:3]
        if action == ["terminal", "create"]:
            assert command[command.index("--worktree") + 1] == f"path:{tmp_path}"
            shell_words = shlex.split(command[command.index("--command") + 1])
            stdout_path = Path(shell_words[shell_words.index(">") + 1])
            stderr_path = Path(shell_words[shell_words.index("2>") + 1])
            stdin_path = Path(shell_words[shell_words.index("<") + 1])
            # rc sentinel is the target of the trailing `echo "$?" > <rc>`
            rc_path = Path(shell_words[-1])
            created_paths.update(stdout=stdout_path, stderr=stderr_path,
                                 stdin=stdin_path, rc=rc_path)
            assert stdin_path.read_text() == "prompt via stdin"
            stdout_path.write_text("\x1b]133;C\x07\x1b[36m{\"type\": \"event\"}\x1b[0m\n")
            stderr_path.write_text("\x1b]133;D\x1b\\warning\x1b[31m!\x1b[0m\n")
            rc_path.write_text("17\n")  # command finished with exit code 17
            payload = {"ok": True, "result": {"terminal": {"handle": "term_fixture"}}}
        elif action == ["terminal", "close"]:
            payload = {"ok": True, "result": {}}
        else:
            raise AssertionError(command)
        return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")

    ticks = iter((100.0, 100.0, 100.123))
    inv = adapters.Invocation(
        argv=["opaque-command", "--json"], env={"EXTRA": "yes"},
        env_unset=("INHERITED",), stdin_policy="pipe", stdin_data="prompt via stdin",
        cwd=str(tmp_path), prompt_addendum=None, log_file=None)

    result = substrate.OrcaTerminalSubstrate(
        runner=fake_orca_runner, clock=lambda: next(ticks),
        sleep=lambda _s: None).run(inv, timeout=2.5)

    assert result == substrate.RunResult(
        exit_code=17, stdout='{"type": "event"}\n', stderr="warning!\n",
        duration_ms=123, timed_out=False)
    # exit code comes from the on-disk sentinel poll — no `terminal wait` call
    assert [command[1:3] for command in calls] == [
        ["terminal", "create"], ["terminal", "close"]]
    assert all(not path.exists() for path in created_paths.values())


def test_orca_terminal_substrate_marks_missing_sentinel_as_timeout(tmp_path):
    calls = []

    def fake_orca_runner(command, **kwargs):
        calls.append(command)
        if command[1:3] == ["terminal", "create"]:
            shell_words = shlex.split(command[command.index("--command") + 1])
            # command wrote partial stdout but NEVER the rc sentinel → still running
            Path(shell_words[shell_words.index(">") + 1]).write_text("partial\n")
            payload = {"ok": True, "result": {"terminal": {"handle": "term_timeout"}}}
        else:
            payload = {"ok": True, "result": {}}
        return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")

    # clock crosses wait_started + budget (1.0 + 1) so the poll loop times out
    ticks = iter((1.0, 1.0, 1.6, 2.1, 2.2))
    inv = adapters.Invocation(argv=["opaque-command"], env={}, stdin_policy="devnull",
                               cwd=str(tmp_path), prompt_addendum=None, log_file=None)
    result = substrate.OrcaTerminalSubstrate(
        runner=fake_orca_runner, clock=lambda: next(ticks),
        sleep=lambda _s: None).run(inv, timeout=1)

    assert result.exit_code == -1
    assert result.stdout == "partial\n"   # partial disk output preserved
    assert result.timed_out is True
    assert [command[1:3] for command in calls][-1] == ["terminal", "close"]


def test_orca_terminal_substrate_degrades_nonnumeric_sentinel_to_timeout(tmp_path):
    # Defensive: `echo "$?"` can only ever write digits, but if a complete
    # (newline-terminated) rc file somehow held a non-numeric line, the poll
    # must NOT crash with an uncaught ValueError — it degrades to a clean
    # timeout so the failure still flows through the envelope error taxonomy.
    calls = []

    def fake_orca_runner(command, **kwargs):
        calls.append(command)
        if command[1:3] == ["terminal", "create"]:
            shell_words = shlex.split(command[command.index("--command") + 1])
            Path(shell_words[-1]).write_text("not-a-number\n")  # whole but garbage
            payload = {"ok": True, "result": {"terminal": {"handle": "term_junk"}}}
        else:
            payload = {"ok": True, "result": {}}
        return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")

    ticks = iter((1.0, 1.0, 1.6, 2.1, 2.2))
    inv = adapters.Invocation(argv=["opaque-command"], env={}, stdin_policy="devnull",
                               cwd=str(tmp_path), prompt_addendum=None, log_file=None)
    result = substrate.OrcaTerminalSubstrate(
        runner=fake_orca_runner, clock=lambda: next(ticks),
        sleep=lambda _s: None).run(inv, timeout=1)

    assert result.exit_code == -1        # never parsed a bogus exit code
    assert result.timed_out is True      # degraded to a clean timeout, not a crash
    assert [command[1:3] for command in calls][-1] == ["terminal", "close"]


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
    assert "claude" not in src


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


def test_cli_parser_accepts_orca_terminal_substrate_without_running_orca(tmp_path):
    workdir, prompt_file, out = _make_workdir(tmp_path)
    args = run_lane_main.build_parser().parse_args([
        "--lane", "fixture", "--config", str(tmp_path / "config.yaml"),
        "--prompt-file", str(prompt_file), "--workdir", str(workdir),
        "--out", str(out), "--substrate", "orca-terminal",
    ])
    assert args.substrate == "orca-terminal"


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


# =============================================================================
# 10. CodexAdapter — argv shape, strictify, --json stream parsing (slice 4)
# =============================================================================


def test_codex_build_invocation_argv_shape(tmp_path):
    workdir, prompt_file, out = _make_workdir(tmp_path)
    lane = config_resolve.LaneRecord(
        name="codex-critic-test-lane", transport="codex-cli", model="gpt-5.6-sol",
        effort="xhigh", vendor=None, surface=None, sandbox="read-only", roles=(),
        status=None, capabilities_override={}, raw={})
    adapter = adapters.CodexAdapter()
    req = adapters.InvocationRequest(
        prompt_file=prompt_file, workdir=workdir, out=out,
        effort="xhigh", model="gpt-5.6-sol")
    inv = adapter.build_invocation(lane=lane, req=req)

    assert inv.argv[:2] == ["codex", "exec"]
    assert "-m" in inv.argv
    assert inv.argv[inv.argv.index("-m") + 1] == "gpt-5.6-sol"
    # NOT --effort — the flag does not exist on the live CLI.
    assert "--effort" not in inv.argv
    assert "-c" in inv.argv
    assert inv.argv[inv.argv.index("-c") + 1] == "model_reasoning_effort=xhigh"
    assert "-s" in inv.argv
    assert inv.argv[inv.argv.index("-s") + 1] == "read-only"
    assert "-C" in inv.argv
    assert inv.argv[inv.argv.index("-C") + 1] == str(workdir)
    assert "--json" in inv.argv
    assert "-o" in inv.argv
    assert inv.argv[inv.argv.index("-o") + 1] == str(out)

    # the prompt travels over stdin, never as an argv element
    assert inv.stdin_policy == "pipe"
    assert inv.stdin_data == prompt_file.read_text()
    assert not any("do the thing" in str(a) for a in inv.argv)


def test_codex_build_invocation_omits_optional_flags_when_absent(tmp_path):
    workdir, prompt_file, out = _make_workdir(tmp_path)
    adapter = adapters.CodexAdapter()
    req = adapters.InvocationRequest(prompt_file=prompt_file, workdir=workdir, out=out)
    inv = adapter.build_invocation(lane=None, req=req)
    assert "-m" not in inv.argv
    assert "-c" not in inv.argv
    assert "-s" not in inv.argv
    assert "--output-schema" not in inv.argv


def test_codex_build_invocation_missing_prompt_file_is_config_error(tmp_path):
    workdir = tmp_path / "work"
    workdir.mkdir()
    adapter = adapters.CodexAdapter()
    req = adapters.InvocationRequest(
        prompt_file=workdir / "missing.md", workdir=workdir, out=workdir / "out.md")
    with pytest.raises(LaneError) as excinfo:
        adapter.build_invocation(lane=None, req=req)
    assert excinfo.value.error_class == "config"


def test_codex_build_invocation_schema_is_strictified_and_written_to_disk(tmp_path):
    workdir, prompt_file, out = _make_workdir(tmp_path)
    schema_file = workdir / "schema.json"
    schema_file.write_text(json.dumps({
        "type": "object",
        "properties": {"verdict": {"type": "string"}, "score": {"type": "number"}},
    }))
    adapter = adapters.CodexAdapter()
    req = adapters.InvocationRequest(
        prompt_file=prompt_file, workdir=workdir, out=out, schema=schema_file)
    inv = adapter.build_invocation(lane=None, req=req)

    assert "--output-schema" in inv.argv
    written_path = Path(inv.argv[inv.argv.index("--output-schema") + 1])
    assert written_path.is_file()
    strict = json.loads(written_path.read_text())
    assert strict["additionalProperties"] is False
    assert set(strict["required"]) == {"verdict", "score"}


def test_strictify_for_codex_sets_additional_properties_false_and_required_all_keys():
    schema = {
        "type": "object",
        "properties": {
            "a": {"type": "string"},
            "b": {"type": "object", "properties": {"c": {"type": "number"}}},
        },
    }
    strict = adapters.strictify_for_codex(schema)
    assert strict["additionalProperties"] is False
    assert set(strict["required"]) == {"a", "b"}
    # recursive: the nested object gets the same treatment
    assert strict["properties"]["b"]["additionalProperties"] is False
    assert strict["properties"]["b"]["required"] == ["c"]


def test_strictify_for_codex_leaves_non_object_nodes_untouched():
    schema = {"type": "array", "items": {"type": "string"}}
    strict = adapters.strictify_for_codex(schema)
    assert "additionalProperties" not in strict
    assert strict == schema


def test_codex_parse_model_witness_uses_stream_event_when_present():
    res = substrate.RunResult(
        exit_code=0, stdout=(
            '{"type": "thread.started", "thread_id": "t1", "model": "gpt-5.6-sol"}\n'
            '{"type": "turn.completed", "usage": {"input_tokens": 1}}\n'
        ), stderr="", duration_ms=5, timed_out=False)
    inv = adapters.Invocation(argv=["codex", "exec", "-m", "gpt-5.6-sol"], env={},
                               stdin_policy="pipe", cwd=None, prompt_addendum=None,
                               log_file=None)
    obs = adapters.CodexAdapter().parse_model_witness(res, inv)
    assert obs.observed == "gpt-5.6-sol"
    assert obs.verification == "stream"
    assert obs.error is None


def test_codex_parse_model_witness_falls_back_to_pinned_flag_when_no_stream_event():
    res = substrate.RunResult(
        exit_code=0, stdout='{"type": "thread.started", "thread_id": "t1"}\n',
        stderr="", duration_ms=5, timed_out=False)
    inv = adapters.Invocation(argv=["codex", "exec", "-m", "gpt-5.6-sol"], env={},
                               stdin_policy="pipe", cwd=None, prompt_addendum=None,
                               log_file=None)
    obs = adapters.CodexAdapter().parse_model_witness(res, inv)
    assert obs.observed == "gpt-5.6-sol"
    assert obs.verification == "pin-validated"
    assert obs.error is None


def test_codex_parse_usage_session_id_and_printed_text_from_jsonl_fixture():
    stdout = "\n".join([
        json.dumps({"type": "thread.started", "thread_id": "thread-abc"}),
        json.dumps({"type": "turn.completed", "usage": {"input_tokens": 7, "output_tokens": 3}}),
        json.dumps({"type": "item.completed",
                    "item": {"type": "agent_message", "text": " final answer "}}),
    ])
    res = substrate.RunResult(exit_code=0, stdout=stdout, stderr="", duration_ms=5,
                               timed_out=False)
    adapter = adapters.CodexAdapter()
    assert adapter.parse_session_id(res) == "thread-abc"
    assert adapter.parse_usage(res) == {"input_tokens": 7, "output_tokens": 3}
    text, truncated = adapter.parse_printed_text(res)
    assert text == "final answer"
    assert truncated is False


def test_codex_adapter_never_imports_subprocess_directly():
    # covered globally by test_adapters_module_never_imports_subprocess below,
    # kept local as a fast sanity check tied to this section.
    assert "subprocess" not in adapters.CodexAdapter.__module__


def test_codex_cli_end_to_end_stdin_reaches_child_and_events_parse(tmp_path):
    """Proves the substrate.py stdin_data fix: without it, `stdin=PIPE` with
    no `input=` closes stdin immediately and codex would receive an EMPTY
    prompt. The fake binary reports the stdin length it actually read into
    the artifact file, so this is a real end-to-end check, not just an argv
    shape assertion."""
    workdir, prompt_file, out = _make_workdir(tmp_path, prompt_text="a real prompt body")
    config = tmp_path / "config.yaml"
    config.write_text(MINIMAL_CONFIG)
    fake_codex = _write_fake_codex(tmp_path)
    env = os.environ.copy()
    env["PATH"] = f"{fake_codex.parent}{os.pathsep}{env.get('PATH', '')}"

    result = _run_cli([
        "--lane", "codex-critic-test-lane", "--config", str(config),
        "--prompt-file", str(prompt_file), "--workdir", str(workdir),
        "--out", str(out),
    ], env=env)
    envelope_out = json.loads(result.stdout)
    assert envelope_out["ok"] is True, envelope_out
    assert envelope_out["sessionId"] == "thread-fake-123"
    assert envelope_out["usage"] == {"input_tokens": 11, "output_tokens": 22}
    assert envelope_out["printed_text"] == "codex final response"
    assert envelope_out["artifact"]["present"] is True
    artifact_text = out.read_text()
    assert f"stdin-length={len(prompt_file.read_text())}" in artifact_text


def test_codex_cli_end_to_end_model_event_yields_stream_verification(tmp_path):
    workdir, prompt_file, out = _make_workdir(tmp_path)
    config = tmp_path / "config.yaml"
    config.write_text(MINIMAL_CONFIG)
    fake_codex = _write_fake_codex(tmp_path)
    env = os.environ.copy()
    env["PATH"] = f"{fake_codex.parent}{os.pathsep}{env.get('PATH', '')}"
    env["FAKE_CODEX_MODEL_EVENT"] = "1"

    result = _run_cli([
        "--lane", "codex-critic-test-lane", "--config", str(config),
        "--prompt-file", str(prompt_file), "--workdir", str(workdir),
        "--out", str(out),
    ], env=env)
    envelope_out = json.loads(result.stdout)
    assert envelope_out["ok"] is True, envelope_out
    assert envelope_out["model_observed"] == "gpt-5.6-sol"


# =============================================================================
# 11. GrokAdapter — argv shape, prompt-file addendum, JSON envelope parsing
# =============================================================================


def test_grok_build_invocation_argv_shape(tmp_path):
    workdir, prompt_file, out = _make_workdir(tmp_path)
    schema_file = workdir / "schema.json"
    schema_file.write_text('{"type": "object"}')
    lane = config_resolve.LaneRecord(
        name="grok-test-lane", transport="grok-cli", model="grok-4.5", effort="high",
        vendor="xai", surface="xai", sandbox="orchestrate", roles=(), status=None,
        capabilities_override={}, raw={})
    adapter = adapters.GrokAdapter()
    req = adapters.InvocationRequest(
        prompt_file=prompt_file, workdir=workdir, out=out, effort="high",
        model="grok-4.5", schema=schema_file, resume="sess-1")
    inv = adapter.build_invocation(lane=lane, req=req)

    assert inv.argv[0] == "grok"
    assert "--prompt-file" in inv.argv
    prompt_path = Path(inv.argv[inv.argv.index("--prompt-file") + 1])
    assert prompt_path.is_file()
    assert f"RUN-LANE-ARTIFACT-PATH: {out}" in prompt_path.read_text()
    assert "--output-format" in inv.argv
    assert inv.argv[inv.argv.index("--output-format") + 1] == "json"
    assert "-m" in inv.argv
    assert inv.argv[inv.argv.index("-m") + 1] == "grok-4.5"
    assert "--reasoning-effort" in inv.argv
    assert inv.argv[inv.argv.index("--reasoning-effort") + 1] == "high"
    assert "--sandbox" in inv.argv
    assert inv.argv[inv.argv.index("--sandbox") + 1] == "orchestrate"
    assert inv.env["GROK_SANDBOX"] == "orchestrate"
    assert "--cwd" in inv.argv
    assert inv.argv[inv.argv.index("--cwd") + 1] == str(workdir)
    assert "--resume" in inv.argv
    assert inv.argv[inv.argv.index("--resume") + 1] == "sess-1"
    assert "--json-schema" in inv.argv
    assert inv.argv[inv.argv.index("--json-schema") + 1] == '{"type": "object"}'
    assert inv.stdin_policy == "devnull"


def test_grok_build_invocation_omits_optional_flags_when_absent(tmp_path):
    workdir, prompt_file, out = _make_workdir(tmp_path)
    adapter = adapters.GrokAdapter()
    req = adapters.InvocationRequest(prompt_file=prompt_file, workdir=workdir, out=out)
    inv = adapter.build_invocation(lane=None, req=req)
    assert "-m" not in inv.argv
    assert "--reasoning-effort" not in inv.argv
    assert "--sandbox" not in inv.argv
    assert "--resume" not in inv.argv
    assert "--json-schema" not in inv.argv
    assert inv.env == {}


def test_grok_build_invocation_missing_prompt_file_is_config_error(tmp_path):
    workdir = tmp_path / "work"
    workdir.mkdir()
    adapter = adapters.GrokAdapter()
    req = adapters.InvocationRequest(
        prompt_file=workdir / "missing.md", workdir=workdir, out=workdir / "out.md")
    with pytest.raises(LaneError) as excinfo:
        adapter.build_invocation(lane=None, req=req)
    assert excinfo.value.error_class == "config"


def test_grok_parse_model_witness_stream_from_model_usage_key():
    res = substrate.RunResult(
        exit_code=0,
        stdout=json.dumps({"modelUsage": {"grok-4.5-build": {"x": 1}}, "usage": {"x": 1}}),
        stderr="", duration_ms=1, timed_out=False)
    obs = adapters.GrokAdapter().parse_model_witness(res, inv=None)
    assert obs.observed == "grok-4.5-build"
    assert obs.verification == "stream"
    assert obs.error is None


def test_grok_parse_model_witness_honestly_none_when_model_usage_is_missing_or_empty():
    res = substrate.RunResult(
        exit_code=0, stdout=json.dumps({"usage": {"x": 1}}),
        stderr="", duration_ms=1, timed_out=False)
    obs = adapters.GrokAdapter().parse_model_witness(res, inv=None)
    assert obs.observed is None
    assert obs.verification == "none"
    assert obs.error is None                      # not an error — an honest ceiling

    empty = substrate.RunResult(
        exit_code=0, stdout=json.dumps({"modelUsage": {}}), stderr="", duration_ms=1,
        timed_out=False)
    assert adapters.GrokAdapter().parse_model_witness(empty, inv=None).verification == "none"


def test_grok_parse_usage_session_and_printed_text_from_fixture_json():
    stdout = json.dumps({
        "usage": {"input_tokens": 4}, "session_id": "grok-sess-9",
        "result": "grok final response",
    })
    res = substrate.RunResult(exit_code=0, stdout=stdout, stderr="", duration_ms=1,
                               timed_out=False)
    adapter = adapters.GrokAdapter()
    assert adapter.parse_usage(res) == {"input_tokens": 4}
    assert adapter.parse_session_id(res) == "grok-sess-9"
    text, truncated = adapter.parse_printed_text(res)
    assert text == "grok final response"
    assert truncated is False


def test_grok_caps_default_to_agent_writes_file_and_stream_witness():
    caps = adapters.GrokAdapter.CAPS
    assert caps.artifact_channel == "agent-writes-file"
    assert caps.model_verification == "stream"
    assert caps.supports_effort == "flag"
    assert caps.supports_schema == "strict"
    assert caps.has_own_sandbox == "strong"


def test_one_grok_invocation_runs_through_fake_and_subprocess_substrate(tmp_path):
    """Same axis-separation proof as the agy test in section 8, run against
    a second adapter — one Invocation, two substrates, adapter never knows
    which ran it."""
    workdir, prompt_file, out = _make_workdir(tmp_path)
    lane = config_resolve.LaneRecord(
        name="grok-test-lane", transport="grok-cli", model="grok-4.5", effort="high",
        vendor="xai", surface="xai", sandbox="orchestrate", roles=(), status=None,
        capabilities_override={}, raw={})
    adapter = adapters.GrokAdapter()
    req = adapters.InvocationRequest(
        prompt_file=prompt_file, workdir=workdir, out=out, effort="high", model="grok-4.5")
    inv = adapter.build_invocation(lane, req)

    canned = substrate.RunResult(exit_code=0, stdout='{"result": "ok"}', stderr="",
                                  duration_ms=1, timed_out=False)
    fake_sub = FakeSubstrate(canned)
    assert fake_sub.run(inv, timeout=30) is canned
    assert fake_sub.received_invocation is inv

    # a fake `grok` binary that just writes the addendum-declared artifact
    bin_dir = tmp_path / "bin2"
    bin_dir.mkdir()
    fake_grok = bin_dir / "grok"
    prompt_path_repr = repr(str(inv.argv[inv.argv.index("--prompt-file") + 1]))
    fake_grok_source = (
        "#!%%PYTHON%%\n"
        "import re, sys\n"
        f"prompt = open({prompt_path_repr}).read()\n"
        r"m = re.search(r'RUN-LANE-ARTIFACT-PATH: (\S+)', prompt)" "\n"
        "if m:\n"
        "    open(m.group(1), 'w').write('fake grok artifact\\n')\n"
        "sys.stdout.write('{}')\n"
    ).replace("%%PYTHON%%", sys.executable)
    fake_grok.write_text(fake_grok_source)
    fake_grok.chmod(fake_grok.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    inv_for_subprocess = dataclasses.replace(inv, argv=[str(fake_grok)] + inv.argv[1:])
    real_sub = substrate.SubprocessSubstrate()
    res_real = real_sub.run(inv_for_subprocess, timeout=30)
    assert res_real.exit_code == 0
    art = artifact.capture(out)
    assert art["present"] is True


# =============================================================================
# 12. KimiAdapter — argv shape (no effort surface), stream-json parsing
# =============================================================================


def test_kimi_build_invocation_argv_shape(tmp_path):
    workdir, prompt_file, out = _make_workdir(tmp_path)
    adapter = adapters.KimiAdapter()
    req = adapters.InvocationRequest(prompt_file=prompt_file, workdir=workdir, out=out, model="k3")
    inv = adapter.build_invocation(lane=None, req=req)

    assert inv.argv[0] == "kimi"
    assert "--add-dir" in inv.argv
    assert inv.argv[inv.argv.index("--add-dir") + 1] == str(workdir)
    assert "--output-format" in inv.argv
    assert inv.argv[inv.argv.index("--output-format") + 1] == "stream-json"
    assert "-m" in inv.argv
    assert inv.argv[inv.argv.index("-m") + 1] == "k3"
    assert "-p" in inv.argv
    assert inv.argv[-1] != "-p"
    assert f"RUN-LANE-ARTIFACT-PATH: {out}" in inv.argv[-1]
    # kimi's own --help carries no effort flag at all, regardless of what
    # the request asks for
    assert "--effort" not in inv.argv
    assert "--reasoning-effort" not in inv.argv


def test_kimi_build_invocation_omits_model_when_absent(tmp_path):
    workdir, prompt_file, out = _make_workdir(tmp_path)
    adapter = adapters.KimiAdapter()
    req = adapters.InvocationRequest(prompt_file=prompt_file, workdir=workdir, out=out)
    inv = adapter.build_invocation(lane=None, req=req)
    assert "-m" not in inv.argv


def test_kimi_caps_supports_effort_no():
    assert adapters.KimiAdapter.CAPS.supports_effort == "no"
    assert adapters.KimiAdapter.CAPS.has_own_sandbox == "none"
    assert adapters.KimiAdapter.CAPS.model_verification == "none"


def test_kimi_parse_model_witness_from_stream_json_event():
    stdout = "\n".join([
        json.dumps({"type": "start"}),
        json.dumps({"model": "k3", "usage": {"input_tokens": 2}}),
    ])
    res = substrate.RunResult(exit_code=0, stdout=stdout, stderr="", duration_ms=1,
                               timed_out=False)
    obs = adapters.KimiAdapter().parse_model_witness(res, inv=None)
    assert obs.observed == "k3"
    assert obs.verification == "stream"


def test_kimi_parse_model_witness_honestly_none_when_absent():
    res = substrate.RunResult(exit_code=0, stdout=json.dumps({"type": "start"}),
                               stderr="", duration_ms=1, timed_out=False)
    obs = adapters.KimiAdapter().parse_model_witness(res, inv=None)
    assert obs.observed is None
    assert obs.verification == "none"
    assert obs.error is None


def test_kimi_parse_usage_session_and_printed_text_from_stream_json_fixture():
    stdout = "\n".join([
        json.dumps({"session_id": "kimi-sess-1"}),
        json.dumps({"usage": {"input_tokens": 9}}),
        json.dumps({"text": "hello "}),
        json.dumps({"text": "world"}),
    ])
    res = substrate.RunResult(exit_code=0, stdout=stdout, stderr="", duration_ms=1,
                               timed_out=False)
    adapter = adapters.KimiAdapter()
    assert adapter.parse_session_id(res) == "kimi-sess-1"
    assert adapter.parse_usage(res) == {"input_tokens": 9}
    text, truncated = adapter.parse_printed_text(res)
    assert text == "hello world"
    assert truncated is False


def test_cli_kimi_unsupported_effort_request_is_config_error(tmp_path):
    """__main__'s existing capability gate (unchanged) already refuses
    --effort on a supports_effort:no lane BEFORE build_invocation runs —
    this proves KimiAdapter's declared CAPS actually reaches that gate."""
    workdir, prompt_file, out = _make_workdir(tmp_path)
    config = tmp_path / "config.yaml"
    config.write_text(MINIMAL_CONFIG)
    env = _cli_env(tmp_path)

    result = _run_cli([
        "--lane", "kimi-test-lane", "--config", str(config),
        "--prompt-file", str(prompt_file), "--workdir", str(workdir),
        "--out", str(out), "--effort", "high",
    ], env=env)
    envelope_out = json.loads(result.stdout)
    assert envelope_out["ok"] is False
    assert envelope_out["error"]["class"] == "config"


# =============================================================================
# 13. ClaudePrintAdapter — argv shape, env erasure, JSON envelope parsing
# =============================================================================


def test_claude_print_build_invocation_argv_shape(tmp_path):
    workdir, prompt_file, out = _make_workdir(tmp_path, prompt_text="review this diff")
    adapter = adapters.ClaudePrintAdapter()
    req = adapters.InvocationRequest(
        prompt_file=prompt_file, workdir=workdir, out=out, role="critic",
        effort="high", model="claude-fable-5")
    inv = adapter.build_invocation(lane=None, req=req)

    assert inv.argv[0] == "claude"
    assert inv.argv[1] == "-p"
    assert "--model" in inv.argv
    assert inv.argv[inv.argv.index("--model") + 1] == "claude-fable-5"
    assert "--agent" in inv.argv
    assert inv.argv[inv.argv.index("--agent") + 1] == "critic"
    assert "--effort" in inv.argv
    assert inv.argv[inv.argv.index("--effort") + 1] == "high"
    assert "--add-dir" in inv.argv
    assert inv.argv[inv.argv.index("--add-dir") + 1] == str(workdir)
    assert "--output-format" in inv.argv
    assert inv.argv[inv.argv.index("--output-format") + 1] == "json"
    # the prompt is a plain positional argument — claude's own `-p` is a
    # boolean print-mode flag, unlike agy's consuming `-p`
    assert inv.argv[-1] == "review this diff"
    assert inv.stdin_policy == "devnull"


def test_claude_print_build_invocation_omits_optional_flags_when_absent(tmp_path):
    workdir, prompt_file, out = _make_workdir(tmp_path)
    adapter = adapters.ClaudePrintAdapter()
    req = adapters.InvocationRequest(prompt_file=prompt_file, workdir=workdir, out=out)
    inv = adapter.build_invocation(lane=None, req=req)
    assert "--model" not in inv.argv
    assert "--agent" not in inv.argv
    assert "--effort" not in inv.argv


def test_claude_print_build_invocation_missing_prompt_file_is_config_error(tmp_path):
    workdir = tmp_path / "work"
    workdir.mkdir()
    adapter = adapters.ClaudePrintAdapter()
    req = adapters.InvocationRequest(
        prompt_file=workdir / "missing.md", workdir=workdir, out=workdir / "out.md")
    with pytest.raises(LaneError) as excinfo:
        adapter.build_invocation(lane=None, req=req)
    assert excinfo.value.error_class == "config"


def test_claude_print_build_invocation_declares_nested_spawn_env_unset(tmp_path):
    """ADR-0005 §"Claude-работники": CLAUDECODE/CLAUDE_CODE_ENTRYPOINT must
    be ERASED, not merely overridden — this is the field the
    SubprocessSubstrate fix (section 13's env test below) actually acts on."""
    workdir, prompt_file, out = _make_workdir(tmp_path)
    adapter = adapters.ClaudePrintAdapter()
    req = adapters.InvocationRequest(prompt_file=prompt_file, workdir=workdir, out=out)
    inv = adapter.build_invocation(lane=None, req=req)
    assert set(inv.env_unset) == {"CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"}


def test_claude_print_env_unset_actually_strips_nested_spawn_keys_end_to_end(tmp_path, monkeypatch):
    """The behavioural half of the ADR-0005 requirement: run the built
    Invocation through the REAL SubprocessSubstrate with CLAUDECODE/
    CLAUDE_CODE_ENTRYPOINT set in the parent process env (as they would be
    inside a nested Claude Code spawn) and prove the fake `claude` child
    never sees them — while ordinary inherited vars (PATH) still arrive,
    proving this is a targeted erasure, not env=={}."""
    fake_claude = _write_fake_claude(tmp_path)
    dump_path = tmp_path / "env-dump.json"
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")
    monkeypatch.setenv("FAKE_CLAUDE_ENV_DUMP_PATH", str(dump_path))

    workdir, prompt_file, out = _make_workdir(tmp_path)
    adapter = adapters.ClaudePrintAdapter()
    req = adapters.InvocationRequest(
        prompt_file=prompt_file, workdir=workdir, out=out, model="claude-fable-5")
    inv = adapter.build_invocation(lane=None, req=req)
    inv = dataclasses.replace(inv, argv=[str(fake_claude)] + inv.argv[1:])

    res = substrate.SubprocessSubstrate().run(inv, timeout=30)
    assert res.exit_code == 0, res.stderr
    dumped_env = json.loads(dump_path.read_text())
    assert "CLAUDECODE" not in dumped_env
    assert "CLAUDE_CODE_ENTRYPOINT" not in dumped_env
    assert "PATH" in dumped_env


def test_claude_print_parse_model_usage_session_printed_text_from_fixture_json():
    stdout = json.dumps({
        "model": None,
        "modelUsage": {"claude-fable-5": {"inputTokens": 5, "outputTokens": 6}},
        "usage": {"input_tokens": 5, "output_tokens": 6},
        "session_id": "sess-fake-1", "result": "claude final response",
    })
    res = substrate.RunResult(exit_code=0, stdout=stdout, stderr="", duration_ms=1,
                               timed_out=False)
    adapter = adapters.ClaudePrintAdapter()
    obs = adapter.parse_model_witness(res, inv=None)
    assert obs.observed == "claude-fable-5"
    assert obs.verification == "stream"
    assert obs.error is None
    assert adapter.parse_usage(res) == {"input_tokens": 5, "output_tokens": 6}
    assert adapter.parse_session_id(res) == "sess-fake-1"
    text, truncated = adapter.parse_printed_text(res)
    assert text == "claude final response"
    assert truncated is False


def test_claude_print_parse_model_witness_missing_model_usage_is_an_error():
    """CAPS.model_verification is fixed 'stream' for claude-print — unlike
    grok's honest 'none' when modelUsage is absent — so a missing modelUsage
    field here IS a
    transport-level defect, not an accepted ceiling."""
    res = substrate.RunResult(exit_code=0, stdout=json.dumps({"result": "ok"}),
                               stderr="", duration_ms=1, timed_out=False)
    obs = adapters.ClaudePrintAdapter().parse_model_witness(res, inv=None)
    assert obs.observed is None
    assert obs.error is not None


def test_claude_materialize_artifact_writes_result_from_json(tmp_path):
    out = tmp_path / "result.md"
    res = substrate.RunResult(
        exit_code=0,
        stdout=json.dumps({"result": "captured Claude result"}),
        stderr="", duration_ms=1, timed_out=False)

    adapters.ClaudePrintAdapter().materialize_artifact(res, out)

    assert out.read_text() == "captured Claude result"


@pytest.mark.parametrize("adapter_cls", [
    adapters.AgyAdapter, adapters.CodexAdapter, adapters.GrokAdapter, adapters.KimiAdapter,
])
def test_native_artifact_adapters_materialize_hook_is_a_noop(tmp_path, adapter_cls):
    out = tmp_path / "already-written.md"
    out.write_text("native artifact")
    res = substrate.RunResult(exit_code=0, stdout='{"result": "replacement"}', stderr="",
                              duration_ms=1, timed_out=False)

    adapter_cls().materialize_artifact(res, out)

    assert out.read_text() == "native artifact"


def test_claude_print_caps_are_a_declared_degradation():
    caps = adapters.ClaudePrintAdapter.CAPS
    assert caps.artifact_channel == "stdout-capture"
    assert caps.model_verification == "stream"
    assert caps.has_own_sandbox == "none"


# =============================================================================
# 14. hardening.py — F6 env mix-in, config bootstrap, fail-closed gate
# =============================================================================

CONFIG_WITH_HARDENING = {
    "cross_provider": {
        "grok_hardening": {
            "config_file": "references/grok/config.toml",
            "env": {"GROK_SANDBOX": "orchestrate", "GROK_TELEMETRY_ENABLED": "0"},
        },
        "kimi_hardening": {
            "config_file": "references/kimi/config.toml",
            "env": {"KIMI_DISABLE_TELEMETRY": "1"},
        },
    },
}


def _hardened_lane(hardening_key_value):
    return config_resolve.LaneRecord(
        name="grok-build", transport="grok-cli", model="grok-4.5", effort="high",
        vendor="xai", surface="xai", sandbox="orchestrate", roles=(), status=None,
        capabilities_override={}, raw={"hardening": hardening_key_value})


def _plain_lane():
    return config_resolve.LaneRecord(
        name="agy-test-lane", transport="agy-print", model="M", effort="medium",
        vendor=None, surface=None, sandbox=None, roles=(), status=None,
        capabilities_override={}, raw={})


def test_hardening_key_and_applies_to():
    assert hardening.hardening_key(_hardened_lane("grok_hardening")) == "grok_hardening"
    assert hardening.hardening_key(_hardened_lane("kimi_hardening")) == "kimi_hardening"
    assert hardening.hardening_key(_plain_lane()) is None
    assert hardening.applies_to(_hardened_lane("grok_hardening")) is True
    assert hardening.applies_to(_plain_lane()) is False


def _bare_invocation():
    return adapters.Invocation(
        argv=["grok"], env={"GROK_SANDBOX": "adapter-default"}, stdin_policy="devnull",
        cwd=None, prompt_addendum=None, log_file=None)


def test_hardening_apply_merges_env_and_hardening_wins_on_conflict():
    inv = _bare_invocation()
    merged = hardening.apply(_hardened_lane("grok_hardening"), inv, CONFIG_WITH_HARDENING)
    assert merged.env["GROK_SANDBOX"] == "orchestrate"      # hardening's own value wins
    assert merged.env["GROK_TELEMETRY_ENABLED"] == "0"
    assert merged.argv == inv.argv                          # never touches argv


def test_hardening_apply_is_noop_for_a_lane_without_a_hardening_key():
    inv = _bare_invocation()
    result = hardening.apply(_plain_lane(), inv, CONFIG_WITH_HARDENING)
    assert result is inv


def test_hardening_apply_rejects_non_mapping_env_block():
    bad_config = {"cross_provider": {"grok_hardening": {"env": "not-a-mapping"}}}
    with pytest.raises(LaneError) as excinfo:
        hardening.apply(_hardened_lane("grok_hardening"), _bare_invocation(), bad_config)
    assert excinfo.value.error_class == "config"


def test_hardening_ensure_config_file_installs_when_missing(tmp_path):
    repo_root = tmp_path / "repo"
    (repo_root / "references" / "grok").mkdir(parents=True)
    (repo_root / "references" / "grok" / "config.toml").write_text("sandbox = true\n")
    home = tmp_path / "home"

    target, installed = hardening.ensure_config_file(
        _hardened_lane("grok_hardening"), CONFIG_WITH_HARDENING, repo_root, home)
    assert installed is True
    assert target == home / ".grok" / "config.toml"
    assert target.read_text() == "sandbox = true\n"


def test_hardening_ensure_config_file_never_overwrites_existing(tmp_path):
    repo_root = tmp_path / "repo"
    (repo_root / "references" / "grok").mkdir(parents=True)
    (repo_root / "references" / "grok" / "config.toml").write_text("sandbox = true\n")
    home = tmp_path / "home"
    (home / ".grok").mkdir(parents=True)
    (home / ".grok" / "config.toml").write_text("# operator's own edits\n")

    target, installed = hardening.ensure_config_file(
        _hardened_lane("grok_hardening"), CONFIG_WITH_HARDENING, repo_root, home)
    assert installed is False
    assert target.read_text() == "# operator's own edits\n"


def test_hardening_gate_passes_on_fictitious_profile_applied_evidence(tmp_path):
    events = tmp_path / "sandbox-events.jsonl"
    events.write_text('{"event": "ProfileApplied", "profile": "orchestrate"}\n')
    hardening.gate(_hardened_lane("grok_hardening"), sandbox_events_path=events)  # no raise


def test_hardening_gate_fails_closed_when_no_evidence_file_at_all(tmp_path):
    with pytest.raises(LaneError) as excinfo:
        hardening.gate(_hardened_lane("grok_hardening"),
                        sandbox_events_path=tmp_path / "does-not-exist.jsonl")
    assert excinfo.value.error_class == "hardening-gate"


def test_hardening_gate_fails_closed_when_file_lacks_profile_applied(tmp_path):
    events = tmp_path / "sandbox-events.jsonl"
    events.write_text('{"event": "SomethingElse"}\n')
    with pytest.raises(LaneError) as excinfo:
        hardening.gate(_hardened_lane("grok_hardening"), sandbox_events_path=events)
    assert excinfo.value.error_class == "hardening-gate"


def test_hardening_gate_fails_closed_on_apply_failed_in_stderr_even_with_stale_evidence(tmp_path):
    events = tmp_path / "sandbox-events.jsonl"
    events.write_text('{"event": "ProfileApplied"}\n')   # stale from a PREVIOUS run
    with pytest.raises(LaneError) as excinfo:
        hardening.gate(_hardened_lane("grok_hardening"), sandbox_events_path=events,
                        stderr="fatal: sandbox could not be applied")
    assert excinfo.value.error_class == "hardening-gate"


def test_hardening_gate_is_a_noop_for_kimi_hardening_lanes():
    """kimi has no filesystem sandbox of its own to apply (config.yaml
    kimi_hardening carries no preflight_verify/dispatch_gate field) — the
    gate must not fail-closed on a lane that was never meant to produce
    ProfileApplied evidence in the first place."""
    hardening.gate(_hardened_lane("kimi_hardening"), sandbox_events_path=None)  # no raise


def test_hardening_gate_is_a_noop_for_a_lane_without_any_hardening_key():
    hardening.gate(_plain_lane(), sandbox_events_path=None)  # no raise


# =============================================================================
# 15. __main__ tail wiring — hardening, materialization, witnessed aliases
# =============================================================================


def _main_args(tmp_path, config_text, lane, out_name="out.md"):
    workdir, prompt_file, _ = _make_workdir(tmp_path)
    config = tmp_path / "config.yaml"
    config.write_text(config_text)
    out = workdir / out_name
    args = run_lane_main.build_parser().parse_args([
        "--lane", lane, "--config", str(config), "--prompt-file", str(prompt_file),
        "--workdir", str(workdir), "--out", str(out),
    ])
    return args, out


class _CannedSubstrate:
    result = None
    received_invocation = None

    def run(self, inv, timeout):
        type(self).received_invocation = inv
        return type(self).result


def test_main_applies_hardening_then_returns_hardening_gate_envelope(tmp_path, monkeypatch, capsys):
    config_text = """
cross_provider:
  grok_hardening:
    env: {GROK_SANDBOX: orchestrate, GROK_TELEMETRY_ENABLED: "0"}
  lanes:
    grok-hardened:
      transport: grok-cli
      model: grok-4.5
      effort: high
      hardening: grok_hardening
"""
    args, _ = _main_args(tmp_path, config_text, "grok-hardened")
    _CannedSubstrate.result = substrate.RunResult(
        exit_code=0, stdout='{"modelUsage": {"grok-4.5-build": {}}}', stderr="",
        duration_ms=1, timed_out=False)
    monkeypatch.setitem(run_lane_main.SUBSTRATES, "subprocess", _CannedSubstrate)
    monkeypatch.setattr(run_lane_main, "GROK_SANDBOX_EVENTS_PATH",
                        tmp_path / "no-ProfileApplied-evidence.jsonl")

    assert run_lane_main.main([
        "--lane", "grok-hardened", "--config", str(args.config),
        "--prompt-file", str(args.prompt_file), "--workdir", str(args.workdir),
        "--out", str(args.out),
    ]) == 1

    env = json.loads(capsys.readouterr().out)
    assert env["ok"] is False
    assert env["error"]["class"] == "hardening-gate"
    assert _CannedSubstrate.received_invocation.env["GROK_SANDBOX"] == "orchestrate"
    assert _CannedSubstrate.received_invocation.env["GROK_TELEMETRY_ENABLED"] == "0"


def test_main_claude_materializes_result_before_artifact_capture(tmp_path, monkeypatch):
    config_text = """
cross_provider:
  lanes:
    claude-fixture:
      transport: claude-print
      model: claude-fable-5
      effort: high
"""
    args, out = _main_args(tmp_path, config_text, "claude-fixture")
    _CannedSubstrate.result = substrate.RunResult(
        exit_code=0,
        stdout=json.dumps({
            "result": "materialized from stdout",
            "modelUsage": {"claude-fable-5": {}},
            "session_id": "sess-materialize",
        }),
        stderr="", duration_ms=1, timed_out=False)
    monkeypatch.setitem(run_lane_main.SUBSTRATES, "subprocess", _CannedSubstrate)

    env = run_lane_main.run(args)

    assert out.read_text() == "materialized from stdout"
    assert env["artifact"]["present"] is True
    assert env["ok"] is True


@pytest.mark.parametrize(("observed", "ok"), [
    ("grok-4.5-build", True),
    ("sonnet", False),
])
def test_main_grok_build_witness_alias_is_normalized_but_real_mismatch_fails(
        tmp_path, monkeypatch, observed, ok):
    config_text = """
cross_provider:
  lanes:
    grok-fixture:
      transport: grok-cli
      model: grok-4.5
      effort: high
"""
    args, out = _main_args(tmp_path, config_text, "grok-fixture")

    class WritesArtifactSubstrate:
        def run(self, inv, timeout):
            out.write_text("fixture grok artifact")
            return substrate.RunResult(
                exit_code=0,
                stdout=json.dumps({"modelUsage": {observed: {}}, "usage": {"total_tokens": 12}}),
                stderr="", duration_ms=1, timed_out=False)

    monkeypatch.setitem(run_lane_main.SUBSTRATES, "subprocess", WritesArtifactSubstrate)
    env = run_lane_main.run(args)

    assert env["model_observed"] == observed
    assert env["ok"] is ok


def test_main_grok_without_model_usage_remains_an_honest_unverified_pass(tmp_path, monkeypatch):
    config_text = """
cross_provider:
  lanes:
    grok-fixture:
      transport: grok-cli
      model: grok-4.5
      effort: high
"""
    args, out = _main_args(tmp_path, config_text, "grok-fixture")

    class WritesArtifactSubstrate:
        def run(self, inv, timeout):
            out.write_text("fixture grok artifact")
            return substrate.RunResult(
                exit_code=0, stdout=json.dumps({"modelUsage": {}}), stderr="",
                duration_ms=1, timed_out=False)

    monkeypatch.setitem(run_lane_main.SUBSTRATES, "subprocess", WritesArtifactSubstrate)
    env = run_lane_main.run(args)

    assert env["model_observed"] is None
    assert env["ok"] is True


# =============================================================================
# 16. Command-verb dispatcher — flag form remains the legacy run contract
# =============================================================================


def test_main_run_verb_uses_the_legacy_run_path(tmp_path, monkeypatch):
    workdir, prompt_file, out = _make_workdir(tmp_path)
    legacy_argv = [
        "--lane", "fixture", "--config", str(tmp_path / "config.yaml"),
        "--prompt-file", str(prompt_file), "--workdir", str(workdir),
        "--out", str(out),
    ]
    received = []

    def fake_run(args):
        received.append(args)
        return {"ok": True}

    monkeypatch.setattr(run_lane_main, "run", fake_run)

    assert run_lane_main.main(legacy_argv) == 0
    assert run_lane_main.main(["run", *legacy_argv]) == 0
    assert len(received) == 2
    assert vars(received[0]) == vars(received[1])


@pytest.mark.parametrize(("verb", "expected"), [
    ("detect", {"mode": "detect", "status": "stub"}),
    ("smoke", {"mode": "smoke", "status": "stub"}),
])
def test_main_dispatches_stub_verbs(verb, expected, capsys):
    assert run_lane_main.main([verb, "--future-argument"]) == 0
    assert json.loads(capsys.readouterr().out) == expected


def test_main_rejects_unknown_bare_verb(capsys):
    assert run_lane_main.main(["frobnicate"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "unknown command 'frobnicate'" in captured.err
    assert "run, detect, smoke" in captured.err
