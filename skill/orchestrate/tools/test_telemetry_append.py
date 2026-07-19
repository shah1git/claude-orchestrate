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
      entry_values: [full, frontier]
      entry_requires_shape:
        frontier: сборка
      require_entry_on_append: true
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
    # `entry` sits in the base because config.yaml requires it on every new
    # append (v22); a test that needs it absent passes entry=None, which drops
    # the key rather than writing a null.
    base = {"date": "2026-07-19", "task": "demo", "ticket": "t1",
            "class": "skilled", "agent": "builder", "model": "sonnet",
            "verdict": "PASS", "config": "v21+0000000", "tokens": 100,
            "entry": "full"}
    base.update(over)
    return {k: v for k, v in base.items() if v is not None}


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


def test_frontier_entry_with_assembly_shape_appends(tmp_path):
    d = make_skill_dir(tmp_path)
    record = row(entry="frontier", shape="сборка")
    p = run(d, json.dumps(record))
    assert p.returncode == 0, p.stderr
    assert log_rows(d) == [record]


def test_full_entry_with_investigation_shape_appends(tmp_path):
    d = make_skill_dir(tmp_path)
    record = row(entry="full", shape="разбирательство")
    p = run(d, json.dumps(record))
    assert p.returncode == 0, p.stderr
    assert log_rows(d) == [record]


def test_omitted_entry_is_rejected_when_config_requires_it(tmp_path):
    # Legacy rows already in the log may lack `entry` (read as `full`), but a
    # record written now can always state its entrance — and the pilot's
    # falsifiability depends on it doing so.
    d = make_skill_dir(tmp_path)
    p = run(d, json.dumps(row(entry=None, shape="разбирательство")))
    assert p.returncode == 1
    assert "lacks the entry stamp" in p.stderr
    assert log_rows(d) == []


def test_omitted_entry_passes_when_config_does_not_require_it(tmp_path):
    # Backward compatibility for pre-v22 config snapshots: no requirement key,
    # no requirement — the reader's `full` default still applies.
    legacy = MINI_CONFIG.replace("  require_entry_on_append: true\n", "")
    assert "require_entry_on_append" not in legacy
    d = make_skill_dir(tmp_path, config=legacy)
    record = row(entry=None, shape="разбирательство")
    p = run(d, json.dumps(record))
    assert p.returncode == 0, p.stderr
    assert log_rows(d) == [record]


def test_unknown_entry_is_rejected_without_appending(tmp_path):
    d = make_skill_dir(tmp_path)
    p = run(d, json.dumps(row(entry="solo", shape="сборка")))
    assert p.returncode == 1
    assert "entry must be one of" in p.stderr
    assert log_rows(d) == []


def test_frontier_entry_requires_assembly_shape(tmp_path):
    d = make_skill_dir(tmp_path)
    p = run(d, json.dumps(row(entry="frontier", shape="разбирательство")))
    assert p.returncode == 1
    assert "only after tickets have been cut" in p.stderr
    assert log_rows(d) == []


def test_malformed_entry_values_in_config_is_rejected(tmp_path):
    # The guard in validate_record only helps if a drifted config actually
    # trips it: a scalar where the vocabulary belongs must fail loudly, not
    # silently degrade to "every entry is valid".
    broken = MINI_CONFIG.replace("entry_values: [full, frontier]",
                                 "entry_values: full")
    d = make_skill_dir(tmp_path, config=broken)
    p = run(d, json.dumps(row(entry="full", shape="сборка")))
    assert p.returncode == 1
    assert "entry_values must be a list of strings" in p.stderr
    assert log_rows(d) == []
