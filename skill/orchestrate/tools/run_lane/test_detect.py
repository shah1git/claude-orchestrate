"""test_detect.py — deterministic tests for `run-lane detect`.

No real vendor CLI is invoked: every test injects a FakeSubstrate (or calls
adapter.parse_probe with a canned RunResult). Style mirrors test_run_lane.py
(tmp_path fixtures, no live config.yaml edits).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from run_lane import adapters, detect
from run_lane.adapters import (
    AgyAdapter,
    ClaudePrintAdapter,
    CodexAdapter,
    GrokAdapter,
    KimiAdapter,
)
from run_lane.substrate import RunResult

# --- fixtures ----------------------------------------------------------------

DETECT_CONFIG = """
cross_provider:
  lanes:
    codex-code:
      transport: codex-cli
      model: "gpt-5.6-terra"
    codex-critic:
      transport: codex-cli
      model: "gpt-5.6-sol"
    gemini-flash:
      transport: agy-print
      model: "Gemini 3.6 Flash (High)"
    grok-build:
      transport: grok-cli
      model: "grok-4.5"
    kimi-k3:
      transport: kimi-cli-headless
      model: "k3"
    claude-fallback:
      transport: claude-print
      model: "claude-fable-5"
availability:
  aliases: {}
"""


def _write_config(tmp_path: Path, body: str = DETECT_CONFIG) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(body)
    return path


class FakeSubstrate:
    """Records every Invocation and returns canned RunResults by argv[0]."""

    def __init__(self, results_by_cli: dict | None = None, default: RunResult | None = None):
        self.calls: list = []
        self.results_by_cli = results_by_cli or {}
        self.default = default or RunResult(0, "ok", "", 1, False)

    def run(self, inv, timeout=None) -> RunResult:
        self.calls.append(inv)
        cli = inv.argv[0] if inv.argv else None
        if cli in self.results_by_cli:
            return self.results_by_cli[cli]
        return self.default


def _rr(exit_code=0, stdout="", stderr="", timed_out=False) -> RunResult:
    return RunResult(exit_code, stdout, stderr, 5, timed_out)


# --- acceptance 1: one probe per distinct transport --------------------------

def test_probe_once_per_transport_two_codex_lanes(tmp_path):
    """Two codex lanes share one transport — substrate runs codex once."""
    config_path = _write_config(tmp_path)
    fake = FakeSubstrate(results_by_cli={
        "codex": _rr(0, "Logged in as user@example.com\n"),
        "agy": _rr(0, "gemini-3.6-flash-high\ngemini-3.5-flash-high\n"),
        "grok": _rr(0, "grok 0.2.101 (abc) [stable]\n"),
        "kimi": _rr(0, "0.27.0\n"),
        "claude": _rr(0, json.dumps({"loggedIn": True, "authMethod": "oauth"})),
    })

    mapping = detect.detect_transports(
        __import__("yaml").safe_load(config_path.read_text()),
        substrate=fake,
    )

    codex_calls = [c for c in fake.calls if c.argv and c.argv[0] == "codex"]
    assert len(codex_calls) == 1, f"expected 1 codex probe, got {len(codex_calls)}"
    assert codex_calls[0].argv == ["codex", "login", "status"]

    # All five transports probed once each (config has 5 distinct transports).
    assert len(fake.calls) == 5

    assert set(mapping["codex-cli"]["lanes"]) == {"codex-code", "codex-critic"}
    assert mapping["codex-cli"]["present"] is True
    assert mapping["codex-cli"]["logged_in"] is True


# --- acceptance 2: map shape -------------------------------------------------

def test_map_contains_required_fields(tmp_path):
    config = __import__("yaml").safe_load(DETECT_CONFIG)
    fake = FakeSubstrate(results_by_cli={
        "codex": _rr(1, "", "Not logged in\n"),
        "agy": _rr(0, "gemini-3.6-flash-high\n"),
        "grok": _rr(0, "grok 0.2.101\n"),
        "kimi": _rr(0, "0.27.0\n"),
        "claude": _rr(1, json.dumps({"loggedIn": False})),
    })
    mapping = detect.detect_transports(config, substrate=fake)

    required = {"cli", "present", "logged_in", "evidence", "lanes"}
    for transport, entry in mapping.items():
        assert required <= set(entry), f"{transport} missing keys: {required - set(entry)}"
        assert isinstance(entry["present"], bool)
        assert isinstance(entry["logged_in"], bool)
        assert isinstance(entry["evidence"], str)
        assert isinstance(entry["lanes"], list)
        assert entry["lanes"], f"{transport} should list at least one lane"


# --- acceptance 3: login invite + exit 0 → logged_in false -------------------

@pytest.mark.parametrize("adapter_cls,stdout,stderr", [
    (CodexAdapter, "Not logged in\n", ""),
    (CodexAdapter, "Please log in to continue\n", ""),
    (GrokAdapter, "Please log in to use grok\n", ""),
    (KimiAdapter, "Not logged in — run `kimi login`\n", ""),
    (AgyAdapter, "Please log in to Google\n", ""),
    (ClaudePrintAdapter, "Please log in to your Anthropic account\n", ""),
])
def test_login_invite_exit0_is_not_logged_in(adapter_cls, stdout, stderr):
    adapter = adapter_cls()
    res = _rr(0, stdout, stderr)  # exit 0 but invite text
    parsed = adapter.parse_probe(res)
    assert parsed["present"] is True
    assert parsed["logged_in"] is False, (
        f"{adapter_cls.__name__} must not treat exit 0 + login invite as logged_in"
    )


def test_codex_success_token_not_mere_exit_zero():
    adapter = CodexAdapter()
    # exit 0, no login invite, but also no "Logged in" success token
    parsed = adapter.parse_probe(_rr(0, "status: unknown\n"))
    assert parsed["present"] is True
    assert parsed["logged_in"] is False

    parsed_ok = adapter.parse_probe(_rr(0, "Logged in using ChatGPT\n"))
    assert parsed_ok["logged_in"] is True


def test_claude_logged_in_from_json_token_not_exit_code():
    adapter = ClaudePrintAdapter()
    # exit 1 with loggedIn:false is still present, not logged in
    parsed = adapter.parse_probe(_rr(1, json.dumps({
        "loggedIn": False, "authMethod": "none", "apiProvider": "firstParty",
    })))
    assert parsed["present"] is True
    assert parsed["logged_in"] is False

    # exit 0 with loggedIn:true
    parsed_ok = adapter.parse_probe(_rr(0, json.dumps({
        "loggedIn": True, "authMethod": "oauth",
    })))
    assert parsed_ok["logged_in"] is True

    # exit 0 but loggedIn:false (the false-positive class)
    parsed_false = adapter.parse_probe(_rr(0, json.dumps({
        "loggedIn": False, "authMethod": "none",
    })))
    assert parsed_false["logged_in"] is False


# --- acceptance 4: CLI missing on PATH → present false, no crash -------------

def test_cli_missing_on_path_present_false():
    missing = _rr(-1, "", "[Errno 2] No such file or directory: 'codex'")
    for cls in (CodexAdapter, AgyAdapter, GrokAdapter, KimiAdapter, ClaudePrintAdapter):
        parsed = cls().parse_probe(missing)
        assert parsed["present"] is False
        assert parsed["logged_in"] is False


def test_detect_missing_cli_does_not_crash(tmp_path):
    config = __import__("yaml").safe_load(DETECT_CONFIG)
    fake = FakeSubstrate(default=_rr(
        -1, "", "[Errno 2] No such file or directory: 'missing'"))
    mapping = detect.detect_transports(config, substrate=fake)
    assert mapping
    for entry in mapping.values():
        assert entry["present"] is False
        assert entry["logged_in"] is False


# --- acceptance 5: probe argv never spends model quota -----------------------

@pytest.mark.parametrize("adapter_cls,expected_argv", [
    (CodexAdapter, ["codex", "login", "status"]),
    (AgyAdapter, ["agy", "models"]),
    (GrokAdapter, ["grok", "--version"]),
    (KimiAdapter, ["kimi", "--version"]),
    (ClaudePrintAdapter, ["claude", "auth", "status"]),
])
def test_probe_argv_is_presence_auth_only(adapter_cls, expected_argv):
    argv = adapter_cls().probe_command(None)
    assert argv == expected_argv
    joined = " ".join(argv)
    # No prompt / task execution surfaces
    assert "-p" not in argv
    assert "--prompt" not in joined
    assert "--prompt-file" not in joined
    assert "exec" not in argv  # codex login status, not codex exec
    assert "single" not in joined


def test_detect_substrate_calls_use_only_probe_argv(tmp_path):
    config = __import__("yaml").safe_load(DETECT_CONFIG)
    fake = FakeSubstrate(default=_rr(0, "grok 1.0.0\n0.1.0\nLogged in\nmodel-a\n"))
    # per-cli canned results so parse_probe gets plausible text
    fake.results_by_cli = {
        "codex": _rr(0, "Logged in as x\n"),
        "agy": _rr(0, "model-a\n"),
        "grok": _rr(0, "grok 1.0.0\n"),
        "kimi": _rr(0, "0.1.0\n"),
        "claude": _rr(0, json.dumps({"loggedIn": True})),
    }
    detect.detect_transports(config, substrate=fake)

    forbidden = {"-p", "--prompt", "--prompt-file", "exec"}
    for inv in fake.calls:
        assert not (forbidden & set(inv.argv)), f"quota-spending argv: {inv.argv}"
        # probe is a short argv: version/login/models/auth only
        assert len(inv.argv) <= 3


# --- agy catalog token -------------------------------------------------------

def test_agy_nonempty_catalog_is_logged_in():
    adapter = AgyAdapter()
    parsed = adapter.parse_probe(_rr(0, "gemini-3.6-flash-high\ngemini-3.5-flash-high\n"))
    assert parsed["present"] is True
    assert parsed["logged_in"] is True

    empty = adapter.parse_probe(_rr(0, "\n\n"))
    assert empty["present"] is True
    assert empty["logged_in"] is False


# --- main() wiring -----------------------------------------------------------

def test_main_prints_json_map(tmp_path, capsys):
    config_path = _write_config(tmp_path)
    fake = FakeSubstrate(results_by_cli={
        "codex": _rr(0, "Logged in as user@example.com\n"),
        "agy": _rr(0, "gemini-3.6-flash-high\n"),
        "grok": _rr(0, "grok 0.2.101\n"),
        "kimi": _rr(0, "0.27.0\n"),
        "claude": _rr(0, json.dumps({"loggedIn": True})),
    })
    rc = detect.main(["--config", str(config_path)], substrate=fake)
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert "codex-cli" in out
    assert out["codex-cli"]["cli"] == "codex"
    assert out["codex-cli"]["lanes"] == ["codex-code", "codex-critic"]
    assert len(fake.calls) == 5


def test_main_missing_config_returns_error(tmp_path, capsys):
    missing = tmp_path / "no-such-config.yaml"
    rc = detect.main(["--config", str(missing)], substrate=FakeSubstrate())
    assert rc == 1
    err = capsys.readouterr().err
    assert "error" in err or "not found" in err.lower()


def test_lanes_by_transport_stable_and_sorted():
    config = __import__("yaml").safe_load(DETECT_CONFIG)
    by = detect.lanes_by_transport(config)
    assert list(by.keys()) == sorted(by.keys())
    assert by["codex-cli"] == ["codex-code", "codex-critic"]


def test_unknown_transport_recorded_without_probe():
    config = {
        "cross_provider": {
            "lanes": {
                "future-lane": {"transport": "future-cli"},
            }
        }
    }
    fake = FakeSubstrate()
    mapping = detect.detect_transports(config, substrate=fake)
    assert fake.calls == []
    assert mapping["future-cli"]["present"] is False
    assert "no adapter" in mapping["future-cli"]["evidence"]
    assert mapping["future-cli"]["lanes"] == ["future-lane"]
