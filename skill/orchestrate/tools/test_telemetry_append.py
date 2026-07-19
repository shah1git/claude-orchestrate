"""Tests for telemetry_append.py at its one seam: CLI invocation.

argv (record JSON, --check-only/--task/--tokens-max/--ack-over-budget)
-> process exit code + stdout/stderr + the log file's resulting content.
Every fixture lives in a pytest tmp_path; the real skill/orchestrate
config and telemetry are never touched.
"""
import json
import subprocess
import sys
from pathlib import Path
from textwrap import dedent

TOOL = Path(__file__).parent / "telemetry_append.py"

# Only the keys the tool reads — deliberately independent of the full
# validate_config.py schema, so the two tools' tests don't couple.
MINI_CONFIG = dedent("""\
    version: 21
    session_budget:
      tokens_max: 1000
      on_exceed: stop-dispatch-finish-gates-report
    telemetry:
      log: telemetry/routing-log.jsonl
      stamp_records_with_config: true
""")


def make_skill_dir(tmp_path, config=MINI_CONFIG, log_lines=()):
    (tmp_path / "config.yaml").write_text(config, encoding="utf-8")
    log = tmp_path / "telemetry" / "routing-log.jsonl"
    log.parent.mkdir()
    log.write_text("".join(json.dumps(r) + "\n" for r in log_lines),
                   encoding="utf-8")
    return tmp_path


def row(**over):
    base = {"date": "2026-07-19", "task": "demo", "ticket": "t1",
            "class": "skilled", "agent": "builder", "model": "sonnet",
            "verdict": "PASS", "config": "v21+0000000", "tokens": 100}
    base.update(over)
    return base


def run(skill_dir, *argv):
    return subprocess.run(
        [sys.executable, str(TOOL), "--skill-dir", str(skill_dir), *argv],
        capture_output=True, text=True)


def log_rows(skill_dir):
    text = (skill_dir / "telemetry" / "routing-log.jsonl").read_text()
    return [json.loads(l) for l in text.splitlines() if l.strip()]


def test_valid_record_appends_and_reports_budget(tmp_path):
    d = make_skill_dir(tmp_path)
    p = run(d, json.dumps(row()))
    assert p.returncode == 0, p.stderr
    assert "budget: 100 / 1,000 tokens" in p.stdout
    assert log_rows(d) == [row()]


def test_record_via_stdin(tmp_path):
    d = make_skill_dir(tmp_path)
    p = subprocess.run([sys.executable, str(TOOL), "--skill-dir", str(d)],
                       input=json.dumps(row()), capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    assert len(log_rows(d)) == 1


def test_drift_alias_rejected_nothing_appended(tmp_path):
    d = make_skill_dir(tmp_path)
    bad = {k if k != "date" else "ts": v for k, v in row().items()}
    p = run(d, json.dumps(bad))
    assert p.returncode == 1
    assert "'ts'" in p.stderr and "'date'" in p.stderr
    assert log_rows(d) == []


def test_unknown_verdict_rejected(tmp_path):
    d = make_skill_dir(tmp_path)
    p = run(d, json.dumps(row(verdict="pass_with_caveats")))
    assert p.returncode == 1
    assert "vocabulary" in p.stderr
    assert log_rows(d) == []


def test_lowercase_verdict_normalized_to_canonical(tmp_path):
    d = make_skill_dir(tmp_path)
    p = run(d, json.dumps(row(verdict="fail_fixed")))
    assert p.returncode == 0, p.stderr
    assert log_rows(d)[0]["verdict"] == "FAIL_FIXED"


def test_missing_config_stamp_rejected_when_flag_on(tmp_path):
    d = make_skill_dir(tmp_path)
    r = row()
    del r["config"]
    p = run(d, json.dumps(r))
    assert p.returncode == 1
    assert "stamp" in p.stderr


def test_breach_appends_row_but_exits_2(tmp_path):
    d = make_skill_dir(tmp_path, log_lines=[row(ticket="t1", tokens=950)])
    p = run(d, json.dumps(row(ticket="t2", tokens=100)))
    assert p.returncode == 2
    assert "SESSION BUDGET EXCEEDED" in p.stdout
    assert "stop-dispatch-finish-gates-report" in p.stdout
    assert len(log_rows(d)) == 2  # the spend happened; the record must survive


def test_ack_over_budget_downgrades_to_exit_0(tmp_path):
    d = make_skill_dir(tmp_path, log_lines=[row(ticket="t1", tokens=950)])
    p = run(d, json.dumps(row(ticket="t2", tokens=100)),
            "--ack-over-budget", "final gate of the last in-flight ticket")
    assert p.returncode == 0
    assert "acknowledged" in p.stdout


def test_empty_ack_reason_rejected(tmp_path):
    d = make_skill_dir(tmp_path)
    p = run(d, json.dumps(row()), "--ack-over-budget", "  ")
    assert p.returncode == 1
    assert "never silent" in p.stderr


def test_budget_scoped_by_task(tmp_path):
    d = make_skill_dir(tmp_path, log_lines=[row(task="other", tokens=900)])
    p = run(d, json.dumps(row(task="demo", tokens=100)))
    assert p.returncode == 0, p.stdout  # other run's 900 must not count
    assert "budget: 100 / 1,000" in p.stdout


def test_budget_scoped_by_run_id_when_present(tmp_path):
    d = make_skill_dir(tmp_path, log_lines=[
        row(ticket="t1", tokens=900, run_id="r1"),
        row(ticket="t2", tokens=900, run_id="r2")])
    p = run(d, json.dumps(row(ticket="t3", tokens=50, run_id="r1")))
    assert p.returncode == 0, p.stdout  # r2's spend must not count into r1
    assert "budget: 950 / 1,000" in p.stdout


def test_check_only_reports_without_appending(tmp_path):
    d = make_skill_dir(tmp_path, log_lines=[row(tokens=600)])
    p = run(d, "--check-only", "--task", "demo")
    assert p.returncode == 0
    assert "budget: 600 / 1,000" in p.stdout
    assert len(log_rows(d)) == 1


def test_check_only_exit_2_on_breach(tmp_path):
    d = make_skill_dir(tmp_path, log_lines=[row(tokens=1200)])
    p = run(d, "--check-only", "--task", "demo")
    assert p.returncode == 2


def test_tokens_max_flag_overrides_config(tmp_path):
    d = make_skill_dir(tmp_path, log_lines=[row(tokens=1200)])
    p = run(d, "--check-only", "--task", "demo", "--tokens-max", "5000000")
    assert p.returncode == 0
    assert "5,000,000" in p.stdout


def test_health_event_row_skips_ticket_contract(tmp_path):
    d = make_skill_dir(tmp_path)
    ev = {"event": "provider_health", "provider": "openai", "state": "open",
          "task": "demo", "config": "v21+0000000"}
    p = run(d, json.dumps(ev))
    assert p.returncode == 0, p.stderr
    assert log_rows(d)[0]["event"] == "provider_health"


def test_na_tokens_accepted_ints_still_summed(tmp_path):
    d = make_skill_dir(tmp_path, log_lines=[row(ticket="t1", tokens=300)])
    p = run(d, json.dumps(row(ticket="t2", tokens="n/a")))
    assert p.returncode == 0, p.stderr
    assert "budget: 300 / 1,000" in p.stdout


def test_corrupt_log_line_is_a_hard_error(tmp_path):
    d = make_skill_dir(tmp_path)
    log = d / "telemetry" / "routing-log.jsonl"
    log.write_text('{"broken\n', encoding="utf-8")
    p = run(d, json.dumps(row()))
    # validation passed, append happened, but the sum must refuse to lie
    assert p.returncode == 1
    assert "not valid JSON" in p.stderr
