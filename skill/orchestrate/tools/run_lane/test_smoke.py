"""test_smoke.py — deterministic tests for `run-lane smoke`.

No live vendor is invoked: every test injects `detect_fn` and `run_fn`.
Style mirrors test_run_lane.py (tmp_path fixtures, no real config.yaml).
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from run_lane import smoke
from run_lane.envelope import LaneError

# --- fixtures ----------------------------------------------------------------

SMOKE_CONFIG = """
cross_provider:
  lanes:
    agy-test-lane:
      transport: agy-print
      model: "Test Model X"
      effort: medium
    codex-test-lane:
      transport: codex-cli
      model: "gpt-5.6-sol"
      effort: xhigh
    kimi-test-lane:
      transport: kimi-cli-headless
      model: "k3"
    orphan-no-transport:
      model: "orphan"
availability:
  aliases:
    agy-alias: agy-test-lane
"""


def _write_config(tmp_path: Path, body: str = SMOKE_CONFIG) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(body)
    return path


def _ok_envelope(
    *,
    lane: str = "agy-test-lane",
    model_observed: str | None = "Test Model X",
    duration_ms: int = 42,
    ok: bool = True,
    error: dict | None = None,
) -> dict:
    return {
        "lane": lane,
        "transport": "agy-print",
        "substrate": "subprocess",
        "ok": ok,
        "model_declared": "Test Model X",
        "model_observed": model_observed,
        "effort": "medium",
        "artifact": {"path": "/tmp/x", "present": True, "bytes": 3, "sha256": "abc"},
        "printed_text": "",
        "printed_truncated": False,
        "schema_enforcement": "none",
        "durationMs": duration_ms,
        "usage": None,
        "sessionId": None,
        "sandbox": None,
        "command": ["fake"],
        "evidence": None,
        "error": error,
    }


class RecordingRunner:
    """Fake combat-path run_fn: records calls, returns canned envelopes."""

    def __init__(self, envelopes_by_lane: dict | None = None, default: dict | None = None):
        self.calls: list = []
        self.envelopes_by_lane = envelopes_by_lane or {}
        self.default = default or _ok_envelope()

    def __call__(self, args) -> dict:
        self.calls.append(args)
        lane = getattr(args, "lane", None)
        if lane in self.envelopes_by_lane:
            return self.envelopes_by_lane[lane]
        env = dict(self.default)
        env["lane"] = lane
        return env


# --- acceptance 3: quota guardrail ------------------------------------------


def test_quota_guardrail_without_lanes_or_all_does_not_call_runner(tmp_path, capsys):
    cfg = _write_config(tmp_path)
    runner = RecordingRunner()
    detect_calls: list = []

    def detect_fn(config):
        detect_calls.append(config)
        return {"agy-test-lane": True}

    code = smoke.main(
        ["--config", str(cfg)],
        detect_fn=detect_fn,
        run_fn=runner,
    )
    assert code == 0
    assert runner.calls == []
    assert detect_calls == []
    err = capsys.readouterr().err
    assert "refusing to burn model quota" in err
    assert "--lanes" in err and "--all" in err


def test_quota_guardrail_bare_argv_shows_guardrail_no_runner(capsys):
    """Bare `run-lane smoke` (no --config, no selector) → guardrail, no work."""
    runner = RecordingRunner()
    code = smoke.main(
        ["--future-argument"],
        detect_fn=lambda c: {"x": True},
        run_fn=runner,
    )
    assert code == 0
    assert runner.calls == []
    captured = capsys.readouterr()
    assert captured.out == ""                       # no stub blob on stdout
    assert "refusing to burn model quota" in captured.err


# --- acceptance 1 + 4: available lane runs and is reported ------------------


def test_available_lane_calls_run_and_reports_ok(tmp_path, capsys):
    cfg = _write_config(tmp_path)
    seen_at_call: dict = {}

    def run_fn(args):
        # Capture filesystem state *during* the combat call (temps vanish after).
        seen_at_call["lane"] = args.lane
        seen_at_call["prompt_is_file"] = Path(args.prompt_file).is_file()
        seen_at_call["workdir_is_dir"] = Path(args.workdir).is_dir()
        seen_at_call["prompt_text"] = Path(args.prompt_file).read_text()
        seen_at_call["out"] = args.out
        return _ok_envelope(
            lane="agy-test-lane",
            model_observed="Test Model X",
            duration_ms=99,
        )

    code = smoke.main(
        ["--lanes", "agy-test-lane", "--config", str(cfg)],
        detect_fn=lambda c: {"agy-test-lane": True, "codex-test-lane": False},
        run_fn=run_fn,
    )
    assert code == 0
    assert seen_at_call["lane"] == "agy-test-lane"
    assert seen_at_call["prompt_is_file"] is True
    assert seen_at_call["workdir_is_dir"] is True
    assert "OK" in seen_at_call["prompt_text"]
    assert seen_at_call["out"] is not None

    out = capsys.readouterr().out
    assert "agy-test-lane" in out
    assert "Test Model X" in out
    assert "99" in out
    # Machine-readable block.
    assert '"mode": "smoke"' in out
    assert "true" in out


def test_available_lane_with_verification_requires_model_observed(tmp_path, capsys):
    """agy-print has model_verification=log: empty model_observed → fail row."""
    cfg = _write_config(tmp_path)
    runner = RecordingRunner(
        envelopes_by_lane={
            # ok:true in envelope, but witness missing — smoke must fail the grade.
            "agy-test-lane": _ok_envelope(
                lane="agy-test-lane",
                model_observed=None,
                ok=True,
            ),
        }
    )

    code = smoke.main(
        ["--lanes", "agy-test-lane", "--config", str(cfg)],
        detect_fn=lambda c: {"agy-test-lane": True},
        run_fn=runner,
    )
    assert code == 1
    out = capsys.readouterr().out
    assert "agy-test-lane" in out
    assert "false" in out


def test_kimi_verification_none_allows_empty_model_observed(tmp_path, capsys):
    """kimi-cli-headless CAPS.model_verification is 'none' — empty witness is fine."""
    cfg = _write_config(tmp_path)
    runner = RecordingRunner(
        envelopes_by_lane={
            "kimi-test-lane": _ok_envelope(
                lane="kimi-test-lane",
                model_observed=None,
                ok=True,
                duration_ms=7,
            ),
        }
    )

    code = smoke.main(
        ["--lanes", "kimi-test-lane", "--config", str(cfg)],
        detect_fn=lambda c: {"kimi-test-lane": True},
        run_fn=runner,
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "kimi-test-lane" in out
    # Row ok column is the string "true".
    assert "true" in out.splitlines()[2] or any(
        "kimi-test-lane" in line and "true" in line for line in out.splitlines()
    )


# --- acceptance 2: unavailable → SKIPPED, no run ----------------------------


def test_unavailable_lane_is_skipped_without_calling_runner(tmp_path, capsys):
    cfg = _write_config(tmp_path)
    runner = RecordingRunner()

    code = smoke.main(
        ["--lanes", "codex-test-lane,agy-test-lane", "--config", str(cfg)],
        detect_fn=lambda c: {"codex-test-lane": False, "agy-test-lane": True},
        run_fn=runner,
    )
    assert code == 0
    # Only the available lane is executed.
    assert [c.lane for c in runner.calls] == ["agy-test-lane"]
    out = capsys.readouterr().out
    assert "SKIPPED" in out
    assert "codex-test-lane" in out
    assert "agy-test-lane" in out


def test_detect_transport_map_form_is_accepted(tmp_path, capsys):
    """detect_fn may return detect.detect_transports shape (transport-keyed)."""
    cfg = _write_config(tmp_path)
    runner = RecordingRunner(
        envelopes_by_lane={
            "agy-test-lane": _ok_envelope(lane="agy-test-lane", model_observed="Test Model X"),
        }
    )

    def detect_fn(config):
        return {
            "agy-print": {
                "cli": "agy",
                "present": True,
                "logged_in": True,
                "evidence": "ok",
                "lanes": ["agy-test-lane"],
            },
            "codex-cli": {
                "cli": "codex",
                "present": True,
                "logged_in": False,
                "evidence": "Not logged in",
                "lanes": ["codex-test-lane"],
            },
        }

    code = smoke.main(
        ["--all", "--config", str(cfg)],
        detect_fn=detect_fn,
        run_fn=runner,
    )
    assert code == 0
    assert [c.lane for c in runner.calls] == ["agy-test-lane"]
    out = capsys.readouterr().out
    assert "codex-test-lane" in out and "SKIPPED" in out
    assert "kimi-test-lane" in out  # present in config, not in detect map → skipped


# --- acceptance 4: compact report has one row per lane ----------------------


def test_report_has_one_row_per_selected_lane(tmp_path, capsys):
    cfg = _write_config(tmp_path)
    runner = RecordingRunner(
        default=_ok_envelope(model_observed="M", duration_ms=1),
    )

    smoke.main(
        ["--lanes", "agy-test-lane,codex-test-lane", "--config", str(cfg)],
        detect_fn=lambda c: {"agy-test-lane": True, "codex-test-lane": True},
        run_fn=runner,
    )
    out = capsys.readouterr().out
    # Header + separator + 2 data rows at minimum.
    lines = [ln for ln in out.splitlines() if ln.strip()]
    assert any(ln.startswith("lane") for ln in lines)
    assert sum(1 for ln in lines if ln.startswith("agy-test-lane")) == 1
    assert sum(1 for ln in lines if ln.startswith("codex-test-lane")) == 1
    # JSON block lists both.
    payload = json.loads(out[out.index("{") :])
    assert payload["mode"] == "smoke"
    names = [row["lane"] for row in payload["lanes"]]
    assert names == ["agy-test-lane", "codex-test-lane"]


# --- acceptance 5: reuses __main__.run combat path --------------------------


def test_default_run_fn_delegates_to_main_run(tmp_path, monkeypatch):
    """Without run_fn injection, smoke calls run_lane.__main__.run."""
    cfg = _write_config(tmp_path)
    received: list = []

    def fake_run(args):
        received.append(args)
        return _ok_envelope(lane=args.lane, model_observed="Test Model X")

    monkeypatch.setattr("run_lane.__main__.run", fake_run)

    code = smoke.main(
        ["--lanes", "agy-test-lane", "--config", str(cfg)],
        detect_fn=lambda c: {"agy-test-lane": True},
        # run_fn deliberately omitted → default path
    )
    assert code == 0
    assert len(received) == 1
    assert received[0].lane == "agy-test-lane"
    assert received[0].prompt_file is not None
    assert received[0].out is not None


def test_run_fn_receives_timeout_and_config_path(tmp_path):
    cfg = _write_config(tmp_path)
    runner = RecordingRunner()

    smoke.main(
        ["--lanes", "agy-test-lane", "--config", str(cfg), "--timeout", "17"],
        detect_fn=lambda c: {"agy-test-lane": True},
        run_fn=runner,
    )
    assert runner.calls[0].timeout == 17
    assert Path(runner.calls[0].config) == cfg


# --- alias resolution + --all -----------------------------------------------


def test_lanes_alias_resolves_to_canonical_name(tmp_path):
    cfg = _write_config(tmp_path)
    runner = RecordingRunner()

    smoke.main(
        ["--lanes", "agy-alias", "--config", str(cfg)],
        detect_fn=lambda c: {"agy-test-lane": True},
        run_fn=runner,
    )
    assert [c.lane for c in runner.calls] == ["agy-test-lane"]


def test_all_skips_lanes_without_transport(tmp_path):
    cfg = _write_config(tmp_path)
    runner = RecordingRunner(
        default=_ok_envelope(model_observed="M"),
    )

    smoke.main(
        ["--all", "--config", str(cfg)],
        detect_fn=lambda c: {
            "agy-test-lane": True,
            "codex-test-lane": True,
            "kimi-test-lane": True,
            "orphan-no-transport": True,
        },
        run_fn=runner,
    )
    called = {c.lane for c in runner.calls}
    assert "orphan-no-transport" not in called
    assert called == {"agy-test-lane", "codex-test-lane", "kimi-test-lane"}


# --- failure reporting ------------------------------------------------------


def test_failed_envelope_reports_error_class_and_exit_1(tmp_path, capsys):
    cfg = _write_config(tmp_path)
    runner = RecordingRunner(
        envelopes_by_lane={
            "agy-test-lane": _ok_envelope(
                lane="agy-test-lane",
                ok=False,
                model_observed=None,
                error={"class": "transport-death", "message": "exit 1"},
                duration_ms=3,
            ),
        }
    )

    code = smoke.main(
        ["--lanes", "agy-test-lane", "--config", str(cfg)],
        detect_fn=lambda c: {"agy-test-lane": True},
        run_fn=runner,
    )
    assert code == 1
    out = capsys.readouterr().out
    assert "transport-death" in out
    assert "false" in out


def test_lane_error_from_run_fn_becomes_row_not_crash(tmp_path, capsys):
    cfg = _write_config(tmp_path)

    def boom(args):
        raise LaneError("config", "lane exploded")

    code = smoke.main(
        ["--lanes", "agy-test-lane", "--config", str(cfg)],
        detect_fn=lambda c: {"agy-test-lane": True},
        run_fn=boom,
    )
    assert code == 1
    out = capsys.readouterr().out
    assert "agy-test-lane" in out
    assert "config" in out


# --- unit helpers -----------------------------------------------------------


def test_grade_envelope_witness_rules():
    assert smoke.grade_envelope({"ok": True, "model_observed": "M"}, "log") is True
    assert smoke.grade_envelope({"ok": True, "model_observed": None}, "log") is False
    assert smoke.grade_envelope({"ok": True, "model_observed": ""}, "log") is False
    assert smoke.grade_envelope({"ok": True, "model_observed": None}, "none") is True
    assert smoke.grade_envelope({"ok": False, "model_observed": "M"}, "log") is False


def test_format_table_includes_headers_and_skipped():
    rows = [
        {
            "lane": "a",
            "ok": "true",
            "model_observed": "M",
            "durationMs": "1",
            "error.class": "",
        },
        {
            "lane": "b",
            "ok": "SKIPPED",
            "model_observed": "",
            "durationMs": "",
            "error.class": "",
        },
    ]
    table = smoke.format_table(rows)
    assert "lane" in table.splitlines()[0]
    assert "model_observed" in table.splitlines()[0]
    assert "a" in table and "SKIPPED" in table and "b" in table


def test_list_configured_lanes_sorted_with_transport_only():
    config = {
        "cross_provider": {
            "lanes": {
                "z-lane": {"transport": "agy-print"},
                "a-lane": {"transport": "codex-cli"},
                "nope": {"model": "x"},
            }
        }
    }
    assert smoke.list_configured_lanes(config) == ["a-lane", "z-lane"]


def test_default_detect_fn_with_stub_detect_marks_all_unavailable(monkeypatch):
    """When detect has no detect_transports (S2a stub), default is all False."""
    config = {
        "cross_provider": {
            "lanes": {
                "agy-test-lane": {"transport": "agy-print", "model": "M"},
            }
        }
    }
    # Ensure we hit the stub branch (no detect_transports on the real stub module).
    import run_lane.detect as detect_mod

    if hasattr(detect_mod, "detect_transports"):
        monkeypatch.delattr(detect_mod, "detect_transports", raising=False)

    avail = smoke._default_detect_fn(config)
    assert avail == {"agy-test-lane": False}


def test_default_detect_fn_expands_transport_map(monkeypatch):
    import run_lane.detect as detect_mod

    def fake_detect_transports(config, substrate=None):
        return {
            "agy-print": {
                "present": True,
                "logged_in": True,
                "lanes": ["agy-test-lane"],
            },
            "codex-cli": {
                "present": True,
                "logged_in": False,
                "lanes": ["codex-test-lane"],
            },
        }

    monkeypatch.setattr(detect_mod, "detect_transports", fake_detect_transports, raising=False)
    config = {
        "cross_provider": {
            "lanes": {
                "agy-test-lane": {"transport": "agy-print"},
                "codex-test-lane": {"transport": "codex-cli"},
            }
        }
    }
    avail = smoke._default_detect_fn(config)
    assert avail["agy-test-lane"] is True
    assert avail["codex-test-lane"] is False


def test_missing_config_with_selector_exits_2(capsys):
    code = smoke.main(
        ["--all"],
        detect_fn=lambda c: {},
        run_fn=RecordingRunner(),
    )
    assert code == 2
    assert "--config" in capsys.readouterr().err


def test_unknown_lane_in_selector_is_config_error(tmp_path, capsys):
    cfg = _write_config(tmp_path)
    code = smoke.main(
        ["--lanes", "does-not-exist", "--config", str(cfg)],
        detect_fn=lambda c: {},
        run_fn=RecordingRunner(),
    )
    assert code == 1
    err = capsys.readouterr().err
    assert "does not resolve" in err or "error" in err


def test_trivial_prompt_file_contents_are_minimal(tmp_path):
    """run_fn sees a short prompt (adapter adds artifact addendum itself)."""
    cfg = _write_config(tmp_path)
    runner = RecordingRunner()

    smoke.main(
        ["--lanes", "agy-test-lane", "--config", str(cfg)],
        detect_fn=lambda c: {"agy-test-lane": True},
        run_fn=runner,
    )
    # Temp dir is cleaned after smoke_lane returns, but we can re-read what was
    # written by inspecting the prompt path... it's gone after the with-block.
    # Instead assert the constant is tiny and the call used a .md prompt file.
    assert "OK" in smoke.TRIVIAL_PROMPT
    assert len(smoke.TRIVIAL_PROMPT) < 200
    assert str(runner.calls[0].prompt_file).endswith("prompt.md")
