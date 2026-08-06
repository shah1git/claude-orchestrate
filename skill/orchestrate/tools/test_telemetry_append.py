"""Tests for telemetry_append.py at its one seam: CLI invocation.

argv (record JSON, --check-only/--task/--run-id)
-> process exit code + stdout/stderr + the log file's resulting content.
Every fixture lives in a pytest tmp_path; the real skill/orchestrate
config and telemetry are never touched.
"""
import json
import subprocess
import sys
import pytest
from pathlib import Path
from textwrap import dedent

sys.path.insert(0, str(Path(__file__).parent))
import telemetry_append  # noqa: E402 — must follow the sys.path insert above

TOOL = Path(__file__).parent / "telemetry_append.py"

# Only the keys the tool reads — deliberately independent of the full
# validate_config.py schema, so the two tools' tests don't couple.
MINI_CONFIG = dedent("""\
    version: 21
    telemetry:
      log: telemetry/routing-log.jsonl
      entry_values: [full, frontier, sweep]
      entry_requires_shape:
        frontier: сборка
        sweep: прочёс
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


def sweep_row(**over):
    base = row(entry="sweep", shape="прочёс", ledger_hash="a" * 64,
               dag_hash="b" * 64)
    base.update(over)
    return {k: v for k, v in base.items() if v is not None}


def run(skill_dir, *argv):
    return subprocess.run(
        [sys.executable, str(TOOL), "--skill-dir", str(skill_dir), *argv],
        capture_output=True, text=True)


def log_rows(skill_dir):
    text = (skill_dir / "telemetry" / "routing-log.jsonl").read_text()
    return [json.loads(l) for l in text.splitlines() if l.strip()]


def envelope(**over):
    base = {
        "lane": "codex-builder", "transport": "codex-cli",
        "substrate": "subprocess", "ok": True,
        "model_declared": "gpt-5.6-sol", "model_observed": "gpt-5.6-terra",
        "artifact": {"path": "/tmp/result.md", "present": True,
                     "bytes": 42, "sha256": "a" * 64},
        "durationMs": 3210,
        "usage": {"input_tokens": 100, "cached_input_tokens": 20,
                  "output_tokens": 30, "reasoning_output_tokens": 40},
    }
    base.update(over)
    return base


def write_envelope(tmp_path, **over):
    path = tmp_path / "lane.json"
    path.write_text(json.dumps(envelope(**over)), encoding="utf-8")
    return path


def test_valid_record_appends_and_reports_spend(tmp_path):
    d = make_skill_dir(tmp_path)
    p = run(d, json.dumps(row()))
    assert p.returncode == 0, p.stderr
    assert "spend: 100 tokens (task=demo)" in p.stdout
    assert log_rows(d) == [row()]


def test_record_via_stdin(tmp_path):
    d = make_skill_dir(tmp_path)
    p = subprocess.run([sys.executable, str(TOOL), "--skill-dir", str(d)],
                       input=json.dumps(row()), capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    assert len(log_rows(d)) == 1


def test_envelope_populates_observed_measurements_and_provenance(tmp_path):
    d = make_skill_dir(tmp_path)
    source = write_envelope(tmp_path)
    p = run(d, "--from-envelope", str(source), json.dumps(row(
        model="hand-entered", tokens=999, duration_ms=1, note="gate accepted")))
    assert p.returncode == 0, p.stderr
    record = log_rows(d)[0]
    assert record["model"] == "gpt-5.6-terra"  # witness, never declared alias
    assert record["duration_ms"] == 3210
    # v32: cached_input_tokens (20) is a SUBSET of input_tokens (100), not a
    # sibling to sum on top — (100-20) + 30 + 40 = 150, not the old 190.
    assert record["tokens"] == 150
    assert record["note"] == (
        "gate accepted; run-lane: transport=codex-cli, substrate=subprocess, "
        "artifact_sha256=" + "a" * 64)


def test_envelope_null_usage_becomes_na_tokens(tmp_path):
    d = make_skill_dir(tmp_path)
    source = write_envelope(tmp_path, usage=None)
    p = run(d, "--from-envelope", str(source), json.dumps(row()))
    assert p.returncode == 0, p.stderr
    assert log_rows(d)[0]["tokens"] == "n/a"


def test_failed_envelope_is_still_logged(tmp_path):
    d = make_skill_dir(tmp_path)
    source = write_envelope(tmp_path, ok=False, usage={"total_tokens": 12})
    p = run(d, "--from-envelope", str(source), json.dumps(row(verdict="FAIL")))
    assert p.returncode == 0, p.stderr
    record = log_rows(d)[0]
    assert record["verdict"] == "FAIL"
    assert record["tokens"] == 12


def test_envelope_path_still_rejects_drift_aliases(tmp_path):
    d = make_skill_dir(tmp_path)
    source = write_envelope(tmp_path)
    bad = {k if k != "date" else "ts": v for k, v in row().items()}
    p = run(d, "--from-envelope", str(source), json.dumps(bad))
    assert p.returncode == 1
    assert "'ts'" in p.stderr and "'date'" in p.stderr
    assert log_rows(d) == []


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


def test_spend_far_over_the_old_ceiling_still_exits_0(tmp_path):
    # ADR-0015: spend is an observation, never a permission. A total many
    # times the retired 3,000,000 ceiling is printed as a fact — it stops
    # neither the append nor the next dispatch.
    d = make_skill_dir(tmp_path,
                       log_lines=[row(ticket="t1", tokens=3_045_296)])
    p = run(d, json.dumps(row(ticket="t2", tokens=6_954_704)))
    assert p.returncode == 0, p.stderr
    assert "spend: 10,000,000 tokens (task=demo)" in p.stdout
    assert len(log_rows(d)) == 2


def test_spend_scoped_by_task(tmp_path):
    d = make_skill_dir(tmp_path, log_lines=[row(task="other", tokens=900)])
    p = run(d, json.dumps(row(task="demo", tokens=100)))
    assert p.returncode == 0, p.stdout  # other run's 900 must not count
    assert "spend: 100 tokens (task=demo)" in p.stdout


def test_spend_scoped_by_run_id_when_present(tmp_path):
    d = make_skill_dir(tmp_path, log_lines=[
        row(ticket="t1", tokens=900, run_id="r1"),
        row(ticket="t2", tokens=900, run_id="r2")])
    p = run(d, json.dumps(row(ticket="t3", tokens=50, run_id="r1")))
    assert p.returncode == 0, p.stdout  # r2's spend must not count into r1
    assert "spend: 950 tokens (run_id=r1)" in p.stdout


def test_check_only_reports_without_appending(tmp_path):
    d = make_skill_dir(tmp_path, log_lines=[row(tokens=600)])
    p = run(d, "--check-only", "--task", "demo")
    assert p.returncode == 0
    assert "spend: 600 tokens (task=demo)" in p.stdout
    assert len(log_rows(d)) == 1


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
    assert "spend: 300 tokens (task=demo)" in p.stdout


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


def test_sweep_entry_with_required_shape_and_hashes_appends(tmp_path):
    d = make_skill_dir(tmp_path)
    record = sweep_row()
    p = run(d, json.dumps(record))
    assert p.returncode == 0, p.stderr
    assert log_rows(d) == [record]


@pytest.mark.parametrize("missing_hash", ["ledger_hash", "dag_hash"])
def test_sweep_entry_missing_required_hash_is_rejected_without_appending(
        tmp_path, missing_hash):
    d = make_skill_dir(tmp_path)
    p = run(d, json.dumps(sweep_row(**{missing_hash: None})))
    assert p.returncode == 1
    assert missing_hash in p.stderr
    assert log_rows(d) == []


@pytest.mark.parametrize(
    ("malformed_hash", "value"),
    [("ledger_hash", "A" * 64), ("dag_hash", "b" * 63)],
)
def test_sweep_entry_malformed_hash_is_rejected_without_appending(
        tmp_path, malformed_hash, value):
    d = make_skill_dir(tmp_path)
    p = run(d, json.dumps(sweep_row(**{malformed_hash: value})))
    assert p.returncode == 1
    assert malformed_hash in p.stderr
    assert log_rows(d) == []


def test_sweep_entry_with_incorrect_shape_is_rejected_without_appending(tmp_path):
    d = make_skill_dir(tmp_path)
    p = run(d, json.dumps(sweep_row(shape="сборка")))
    assert p.returncode == 1
    assert "requires shape 'прочёс'" in p.stderr
    assert log_rows(d) == []


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
    broken = MINI_CONFIG.replace("entry_values: [full, frontier, sweep]",
                                 "entry_values: full")
    d = make_skill_dir(tmp_path, config=broken)
    p = run(d, json.dumps(row(entry="full", shape="сборка")))
    assert p.returncode == 1
    assert "entry_values must be a list of strings" in p.stderr
    assert log_rows(d) == []


# =============================================================================
# envelope_tokens — v32 not-cached-tokens-only convention (owner's decision,
# 2026-07-30): direct unit tests against the two live-witness shapes named in
# the ticket, plus the fallback/n-a paths. See telemetry_append.envelope_tokens
# docstring for the full rationale.
# =============================================================================


def test_envelope_tokens_convention_a_openai_codex_subtracts_cached_subset():
    """(a) `cached_input_tokens` present — it is a SUBSET of `input_tokens`
    (OpenAI/codex naming). Live witness: input_tokens 783472,
    cached_input_tokens 667648, output_tokens 7728,
    reasoning_output_tokens 801 -> (783472-667648)+7728+801 = 124353."""
    usage = {
        "input_tokens": 783472,
        "cached_input_tokens": 667648,
        "output_tokens": 7728,
        "reasoning_output_tokens": 801,
    }
    assert telemetry_append.envelope_tokens(usage) == 124353


def test_envelope_tokens_convention_b_anthropic_style_total_minus_cache_read():
    """(b) `cache_read_input_tokens` present and an explicit `total_tokens`
    is also given — the not-cached total is total_tokens - cache_read
    (never input_tokens - cache_read: it was never a subset). Live witness:
    input_tokens 92302 + cache_read_input_tokens 701568 + output_tokens
    16054 = total_tokens 809924 -> not-cached = 809924 - 701568 = 108356."""
    usage = {
        "input_tokens": 92302,
        "cache_read_input_tokens": 701568,
        "output_tokens": 16054,
        "total_tokens": 809924,
    }
    assert telemetry_append.envelope_tokens(usage) == 108356


def test_envelope_tokens_convention_b_without_total_does_not_subtract_from_input():
    """Anthropic-shaped usage with NO explicit total_tokens: input_tokens
    does NOT already include cache_read_input_tokens (they are siblings),
    so the not-cached total is input_tokens + output_tokens (+ any fresh
    cache-write counter) — cache_read_input_tokens must never be subtracted
    from input_tokens here, unlike convention (a)."""
    usage = {
        "input_tokens": 92302,
        "cache_read_input_tokens": 701568,
        "output_tokens": 16054,
    }
    assert telemetry_append.envelope_tokens(usage) == 92302 + 16054


def test_envelope_tokens_convention_b_counts_fresh_cache_write_tokens():
    usage = {
        "input_tokens": 100,
        "cache_read_input_tokens": 50,
        "cache_creation_input_tokens": 25,
        "output_tokens": 10,
    }
    assert telemetry_append.envelope_tokens(usage) == 100 + 25 + 10


def test_envelope_tokens_convention_a_ignores_cache_write_already_in_input():
    """Under convention (a), a cache_write-style counter would already be
    folded into input_tokens by the same naming convention's subset logic —
    it must not be added a second time."""
    usage = {
        "input_tokens": 200,
        "cached_input_tokens": 50,
        "cache_write_input_tokens": 30,
        "output_tokens": 10,
    }
    assert telemetry_append.envelope_tokens(usage) == (200 - 50) + 10


def test_envelope_tokens_missing_usage_witness_is_na():
    assert telemetry_append.envelope_tokens(None) == "n/a"
    assert telemetry_append.envelope_tokens({}) == "n/a"


def test_envelope_tokens_unrecognised_shape_falls_back_to_pre_v32_sum():
    """Neither cache convention named: unchanged pre-v32 behaviour — prefer
    an explicit total, else sum whatever counters are present."""
    assert telemetry_append.envelope_tokens({"total_tokens": 12}) == 12
    assert telemetry_append.envelope_tokens(
        {"input_tokens": 5, "output_tokens": 6}) == 11


# --- critic-gate findings 3/4 (2026-07-30): a broken witness invariant is an
# UNKNOWN ("n/a"), never a negative number or a manufactured zero. --------


def test_envelope_tokens_convention_a_broken_invariant_is_na_not_negative():
    """Critic's exact reproduction: cached_input_tokens (500000) > input_tokens
    (10) violates the subset invariant convention (a) depends on — must be
    "n/a", never a negative number (which `spent()` would silently add to the
    running budget total, DELAYING the very fuse this fix exists to protect)."""
    usage = {"input_tokens": 10, "cached_input_tokens": 500000, "output_tokens": 1}
    result = telemetry_append.envelope_tokens(usage)
    assert result == "n/a"
    assert not isinstance(result, int) or result >= 0


def test_envelope_tokens_convention_a_cached_equal_to_input_is_zero_not_na():
    """The boundary of the invariant (cached == input, not >) is still a
    valid, if degenerate, reading: zero not-cached input tokens."""
    usage = {"input_tokens": 100, "cached_input_tokens": 100, "output_tokens": 5}
    assert telemetry_append.envelope_tokens(usage) == 5


def test_envelope_tokens_convention_b_cache_read_exceeding_total_is_na():
    usage = {"cache_read_input_tokens": 900, "total_tokens": 800}
    assert telemetry_append.envelope_tokens(usage) == "n/a"


def test_envelope_tokens_convention_b_lone_cache_read_counter_is_na():
    """Critic's exact reproduction: {"cache_read_input_tokens": 701568} alone
    (no input_tokens, no total_tokens, no cache-write/output counter) must be
    "n/a" — the docstring's own promise ("rather than manufacturing a zero")
    applies here: there is no witness at all for the not-cached work."""
    assert telemetry_append.envelope_tokens({"cache_read_input_tokens": 701568}) == "n/a"


def test_envelope_tokens_live_ticket_numbers_unaffected_by_the_na_guards():
    """Regression guard: the two live-witness acceptance numbers from the
    original ticket must still compute exactly, after the findings-3/4 fix."""
    convention_a = {
        "input_tokens": 783472, "cached_input_tokens": 667648,
        "output_tokens": 7728, "reasoning_output_tokens": 801,
    }
    assert telemetry_append.envelope_tokens(convention_a) == 124353

    convention_b = {
        "input_tokens": 92302, "cache_read_input_tokens": 701568,
        "output_tokens": 16054, "total_tokens": 809924,
    }
    assert telemetry_append.envelope_tokens(convention_b) == 108356
