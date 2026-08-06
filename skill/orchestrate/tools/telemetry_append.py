#!/usr/bin/env python3
"""telemetry_append.py — the one writer for §7 routing-log records, and the
counter of observed token spend (quality.md §7).

Born from a telemetry-verified failure of prose-only discipline: the
routing-log vocabulary drifted a THIRD time (ts/subtask/lowercase verdicts,
2026-07-15..18) after two documented repair passes — the Step 5 "self-check
before append" rule is prose, and prose loses. The invariants lived in
instructions, not in the write path. This tool IS the write path: every
record enters the log through it, so the vocabulary is enforced and the
run's spend is summed at the exact moment the data is born — not
reconstructed post-factum.

The spend is an OBSERVATION, never a permission: this tool has no ceiling
and never refuses a dispatch (owner's decision 2026-08-06, ADR-0015 —
the v21 guess of 3.0M blocked a healthy run at 3,045,296 tokens, and
raising the number only moves the same wall). It prints what was spent
and exits 0.

Usage:
    telemetry_append.py '<json-object>'          # append one record
    echo '<json-object>' | telemetry_append.py   # same, via stdin
    telemetry_append.py --from-envelope lane.json '<json-object>'
                                                # populate observed facts
    telemetry_append.py --check-only --task T    # report spend, no append
    telemetry_append.py --check-only --run-id R

Options:
    --skill-dir DIR        skill root (default: tools/..)
    --from-envelope PATH   run-lane JSON envelope; its observed model,
                           duration, token usage, and execution provenance
                           populate the supplied §7 record
    --check-only           report the scope's observed spend without
                           appending; needs --task or --run-id

Exit codes:
    0  appended (or --check-only reported the spend)
    1  validation error — NOTHING was appended; fix the record and retry
"""
# Отложенные аннотации: системный python3 на macOS бывает 3.9, а в сигнатурах
# используется синтаксис объединений (str | None) из 3.10.
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import NoReturn

import yaml

DEFAULT_SKILL_DIR = Path(__file__).resolve().parent.parent

# --- §7 contract (references/quality.md §7) ---------------------------------

# The seven-value verdict vocabulary, pinned 2026-07-11. Stored uppercase;
# input is matched case-insensitively and normalized (deterministic, lossless).
VERDICTS = {
    "PASS", "PASS_WITH_NOTES", "FAIL_FIXED", "FAIL",
    "PASS_THEN_FAIL", "MISROUTE", "SKIPPED",
}
CLASSES = {"judgment", "skilled", "mechanical"}
REQUIRED = ("date", "task", "ticket", "class", "agent", "model", "verdict")

# Field aliases from the three documented drift generations (§7 legacy notes +
# the 2026-07-15..18 recurrence). Rejected, not silently renamed: the goal is
# to extinguish the habit, and the retry costs the lead seconds.
ALIASES = {
    "ts": "date", "subtask": "ticket", "subagent": "agent", "role": "agent",
    "outcome": "verdict", "notes": "note", "tools": "tool_uses",
    "wall_s": "duration_ms", "reason": "note", "run": "task",
}

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")



def fail(msg: str) -> NoReturn:
    print(f"telemetry_append: {msg}", file=sys.stderr)
    sys.exit(1)


def load_config(skill_dir: Path) -> dict:
    cfg_path = skill_dir / "config.yaml"
    if not cfg_path.is_file():
        fail(f"config.yaml not found under {skill_dir}")
    with open(cfg_path, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def validate_record(row: dict, config: dict) -> dict:
    """Enforce the §7 contract; returns the row with verdict normalized.

    Provider-health rows (they carry `event` and, by design, no verdict) skip
    the per-ticket contract entirely — including the `entry` checks below —
    because they are a different §7 citizen (config
    telemetry.provider_health_events) and take no part in the entry comparison.

    `entry` is validated against config telemetry.entry_values, and is REQUIRED
    on new appends when telemetry.require_entry_on_append is set. The two rules
    are not in conflict: records already in the log legitimately predate the
    field and are read as `full` (quality.md §7), but a record being written
    now can always state its entrance. `sweep` is a separate ADR-0011 audit
    form, not a member of the historical full-versus-frontier pilot; its rows
    must carry the sealed ledger and DAG SHA-256 hashes.
    """
    if "event" in row:
        return row

    for key in row:
        if key in ALIASES:
            fail(f"field '{key}' is a known drift alias — the canonical §7 "
                 f"name is '{ALIASES[key]}' (quality.md §7, legacy notes)")

    missing = [k for k in REQUIRED if k not in row]
    if missing:
        fail(f"missing required §7 field(s): {', '.join(missing)}")

    if not (isinstance(row["date"], str) and DATE_RE.match(row["date"])):
        fail(f"date must be YYYY-MM-DD, got {row['date']!r}")

    if row["class"] not in CLASSES:
        fail(f"class must be one of {sorted(CLASSES)}, got {row['class']!r}")

    verdict = str(row["verdict"]).upper()
    if verdict not in VERDICTS:
        fail(f"verdict {row['verdict']!r} is not in the seven-value vocabulary "
             f"{sorted(VERDICTS)} — an unusual outcome goes into note "
             f"('[was: …]' style), never into a new spelling (quality.md §7)")
    row["verdict"] = verdict

    # v22: pre-ADR-0004 config snapshots have no entry_values; retain their
    # backward-compatible interpretation that an omitted entry is `full`.
    entry_values = config.get("telemetry", {}).get("entry_values", ["full"])
    if not (isinstance(entry_values, list)
            and all(isinstance(value, str) for value in entry_values)):
        fail("telemetry.entry_values must be a list of strings in config.yaml")
    # The entry->shape constraint is data, not code: config owns both the
    # vocabulary and which entry demands which shape, so widening the axis
    # never needs a code change (ADR-0004's zero-duplication rule).
    requires_shape = config.get("telemetry", {}).get("entry_requires_shape", {})
    if not (isinstance(requires_shape, dict)
            and all(isinstance(k, str) and isinstance(v, str)
                    for k, v in requires_shape.items())):
        fail("telemetry.entry_requires_shape must be a mapping of "
             "entry -> required shape in config.yaml")
    if "entry" not in row and config.get("telemetry", {}).get(
            "require_entry_on_append"):
        fail("record lacks the entry stamp (field 'entry': "
             f"one of {entry_values}) — required by "
             "telemetry.require_entry_on_append: an omitted entry defaults to "
             "'full' on read, silently contaminating the ADR-0004 comparison "
             "the field exists to make")
    if "entry" in row:
        if row["entry"] not in entry_values:
            fail(f"entry must be one of {entry_values}, got {row['entry']!r}")
        needed = requires_shape.get(row["entry"])
        if needed is not None and row.get("shape") != needed:
            explanation = (
                "a frontier exists only after tickets have been cut (ADR-0004)"
                if row["entry"] == "frontier"
                else "the entry-to-shape policy is config-owned"
            )
            fail(f"entry {row['entry']!r} requires shape {needed!r} — {explanation}")
        if row["entry"] == "sweep":
            missing_sweep_hashes = [
                field for field in ("ledger_hash", "dag_hash") if field not in row
            ]
            if missing_sweep_hashes:
                fail("sweep record lacks required sealed hash field(s): "
                     f"{', '.join(missing_sweep_hashes)}")
            malformed_sweep_hashes = [
                field for field in ("ledger_hash", "dag_hash")
                if not isinstance(row[field], str) or not SHA256_RE.fullmatch(row[field])
            ]
            if malformed_sweep_hashes:
                fail("sweep sealed hash field(s) must be lowercase SHA-256 hex: "
                     f"{', '.join(malformed_sweep_hashes)}")

    tokens = row.get("tokens")
    if not (tokens is None or isinstance(tokens, int) or tokens == "n/a"):
        fail(f"tokens must be an integer, null, or \"n/a\", got {tokens!r}")

    if (config.get("telemetry", {}).get("stamp_records_with_config")
            and "config" not in row):
        fail("record lacks the config stamp (field 'config': \"v<N>+<sha:7>\") "
             "— required by telemetry.stamp_records_with_config")
    return row


# --- run-lane envelope intake -------------------------------------------------

def envelope_tokens(usage) -> int | str:
    """Return the truthful §7 token total exposed by a run-lane envelope —
    NOT-CACHED tokens only (owner's decision, 2026-07-30, config v32).

    A live конверт (2026-07-30) showed `input_tokens: 783472` /
    `cached_input_tokens: 667648`: summing both at face value counted the
    same tokens roughly twice over (a cached read is billed at roughly a
    tenth of a fresh token), so the run's reported spend was off by nearly
    2x — a plainly false number about a perfectly healthy run. This
    function's old docstring called including `cached_input_tokens`
    "deliberate" — that reasoning no longer holds; it is exactly the bug.

    Which counter is a SUBSET of another, versus a SIBLING counted
    separately, differs by vendor naming — so the convention is picked BY
    THE NAME of the counter actually present in this particular envelope,
    never guessed from the lane/vendor elsewhere in the record:

      (a) `cached_input_tokens` present (OpenAI/codex naming): it is a
          SUBSET of `input_tokens` (live witness above: 667648 < 783472,
          drawn from it). The not-cached input is therefore
          `input_tokens - cached_input_tokens`. A `cache_write_input_tokens`
          / `cache_creation_input_tokens` counter under this SAME naming
          convention would, by the same subset logic, already be folded
          into `input_tokens` — it is never added a second time.

      (b) `cache_read_input_tokens` present (Anthropic/xAI/Moonshot
          naming): cached reads are a SIBLING counter, never a subset of
          `input_tokens` (live witness: `input_tokens` 92302 +
          `cache_read_input_tokens` 701568 + `output_tokens` 16054 =
          `total_tokens` 809924 exactly — the three add up to the total,
          so `input_tokens` plainly does not already contain the cached
          count). A `cache_write_input_tokens` / `cache_creation_input_tokens`
          counter here is fresh (never-before-cached) work and IS added.
          When this envelope also states an explicit `total_tokens`, the
          not-cached total is `total_tokens - cache_read_input_tokens` —
          the one case where an early return by subtraction is still
          correct, because (unlike convention (a)) the cached count was
          never already inside another counter being kept.

    `output_tokens`, `reasoning_output_tokens`, and `reasoning_tokens` are
    always fresh work and are always counted, under either convention.

    When NEITHER cache counter is named, this falls back to the pre-v32
    behaviour unchanged: prefer an explicit total (`total_tokens`/`total`/
    `tokens`), else sum whatever counters are present — there is no cache
    breakdown to get wrong. A missing or entirely unrecognised usage
    witness is `n/a`, matching §7's data-honesty rule rather than
    manufacturing a zero.
    """
    if not isinstance(usage, dict):
        return "n/a"

    def as_int(key: str) -> int | None:
        value = usage.get(key)
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    fresh_output = [v for v in (as_int("output_tokens"),
                                 as_int("reasoning_output_tokens"),
                                 as_int("reasoning_tokens")) if v is not None]

    if "cached_input_tokens" in usage:
        # Convention (a): cached is a SUBSET of input_tokens — never trust a
        # bare total_tokens here (it cannot be told apart from one that
        # double-counted the cached slice); always compute by subtraction.
        input_tokens = as_int("input_tokens")
        cached = as_int("cached_input_tokens")
        if input_tokens is None or cached is None:
            return "n/a"
        if cached > input_tokens:
            # 2026-07-30 critic-gate finding 3: the subset invariant
            # (`cached_input_tokens <= input_tokens`) is broken — a live
            # witness reporting {"input_tokens": 10, "cached_input_tokens":
            # 500000, "output_tokens": 1} would otherwise subtract to -499989
            # and `spent()` would happily add a NEGATIVE number to the
            # run's reported spend, under-reporting the very number this
            # function exists to state truthfully. A broken invariant
            # is not "zero" and not "a negative number of tokens spent"; it
            # is an UNTRUSTWORTHY witness, so `"n/a"` (never clamped to 0 —
            # that would quietly relabel a broken counter as free work).
            return "n/a"
        return (input_tokens - cached) + sum(fresh_output)

    if "cache_read_input_tokens" in usage:
        # Convention (b): cache_read is a SIBLING of input_tokens, never a
        # subset — an explicit total is trustworthy here, but only with the
        # subtraction (the cached slice is not already excluded from it).
        cache_read = as_int("cache_read_input_tokens")
        if cache_read is None:
            return "n/a"
        total = as_int("total_tokens")
        if total is not None:
            if cache_read > total:
                # Same broken-invariant guard as convention (a) above
                # (finding 3): cache_read can never exceed the reported
                # total it is a component of.
                return "n/a"
            return total - cache_read
        input_tokens = as_int("input_tokens")
        cache_write = as_int("cache_write_input_tokens")
        if cache_write is None:
            cache_write = as_int("cache_creation_input_tokens")
        if input_tokens is None and cache_write is None and not fresh_output:
            # 2026-07-30 critic-gate finding 4: the ONLY counter this usage
            # witness names is the cached-read count we deliberately
            # exclude — there is no witness at all for the not-cached work,
            # so this docstring's own promise ("rather than manufacturing a
            # zero") applies here too: `"n/a"`, not a fabricated `0`.
            return "n/a"
        return (input_tokens or 0) + (cache_write or 0) + sum(fresh_output)

    # Neither cache convention is named: nothing to get wrong by caching —
    # unchanged pre-v32 behaviour.
    for key in ("total_tokens", "total", "tokens"):
        value = as_int(key)
        if value is not None:
            return value

    counters = ("input_tokens", "output_tokens", "reasoning_output_tokens")
    values = [as_int(key) for key in counters]
    values = [v for v in values if v is not None]
    return sum(values) if values else "n/a"


def apply_envelope(row: dict, envelope: dict) -> dict:
    """Overlay run-lane's observed facts onto a lead-classified §7 record.

    The lead retains the classification and verdict in ``row``. The envelope
    is the witness for runtime facts, so it intentionally wins over any
    hand-entered model, duration, or token values.
    """
    row = dict(row)
    row["model"] = envelope.get("model_observed")
    row["duration_ms"] = envelope.get("durationMs")
    row["tokens"] = envelope_tokens(envelope.get("usage"))

    artifact = envelope.get("artifact")
    provenance = []
    if envelope.get("transport") is not None:
        provenance.append(f"transport={envelope['transport']}")
    if envelope.get("substrate") is not None:
        provenance.append(f"substrate={envelope['substrate']}")
    if isinstance(artifact, dict) and artifact.get("sha256") is not None:
        provenance.append(f"artifact_sha256={artifact['sha256']}")
    if provenance:
        note = row.get("note")
        row["note"] = "; ".join(
            part for part in (note, "run-lane: " + ", ".join(provenance))
            if part)
    return row


def load_envelope(path: Path) -> dict:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        fail(f"cannot read --from-envelope {path}: {exc}")
    try:
        envelope = json.loads(raw)
    except json.JSONDecodeError as exc:
        fail(f"--from-envelope is not valid JSON: {exc}")
    if not isinstance(envelope, dict):
        fail("--from-envelope must contain a single JSON object")
    return envelope


# --- observed spend (quality.md §7) ------------------------------------------

def read_log(log_path: Path) -> list:
    if not log_path.is_file():
        return []
    rows = []
    with open(log_path, encoding="utf-8") as fh:
        for n, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                # A corrupt line is somebody's bypass of this tool; it must not
                # silently shrink the observed spend.
                fail(f"{log_path}:{n} is not valid JSON — repair the log first")
    return rows


def run_scope(rows: list, task: str | None, run_id: str | None) -> list:
    """Rows belonging to the current run: by run_id when known (v20 field),
    else by task — the same key the log has used for runs since v14."""
    if run_id:
        return [r for r in rows if r.get("run_id") == run_id]
    return [r for r in rows if r.get("task") == task]


def spent(rows: list) -> int:
    return sum(r["tokens"] for r in rows
               if isinstance(r.get("tokens"), int))


def report_spend(total: int, scope_label: str) -> int:
    """Print the scope's observed spend. An observation, not a verdict: the
    return value is always 0 (ADR-0015 — no ceiling lives here)."""
    print(f"spend: {total:,} tokens ({scope_label})")
    return 0


def main(argv: list) -> int:
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("record", nargs="?")
    ap.add_argument("--skill-dir", type=Path, default=DEFAULT_SKILL_DIR)
    ap.add_argument("--from-envelope", type=Path)
    ap.add_argument("--check-only", action="store_true")
    ap.add_argument("--task")
    ap.add_argument("--run-id")
    args = ap.parse_args(argv)

    config = load_config(args.skill_dir)
    log_path = args.skill_dir / config.get("telemetry", {}).get(
        "log", "telemetry/routing-log.jsonl")

    if args.check_only:
        if not (args.task or args.run_id):
            fail("--check-only needs --task or --run-id to scope the run")
        rows = run_scope(read_log(log_path), args.task, args.run_id)
        label = f"run_id={args.run_id}" if args.run_id else f"task={args.task}"
        return report_spend(spent(rows), label)

    raw = args.record if args.record is not None else sys.stdin.read()
    try:
        row = json.loads(raw)
    except json.JSONDecodeError as exc:
        fail(f"record is not valid JSON: {exc}")
    if not isinstance(row, dict):
        fail("record must be a single JSON object")

    if args.from_envelope is not None:
        row = apply_envelope(row, load_envelope(args.from_envelope))

    row = validate_record(row, config)

    # Append BEFORE summing: the spend already happened, and the record is the
    # only witness of it — the sum is read back from the log afterwards.
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"appended: {row.get('ticket') or row.get('event')} -> {log_path}")

    scope = run_scope(read_log(log_path), row.get("task"), row.get("run_id"))
    label = (f"run_id={row['run_id']}" if row.get("run_id")
             else f"task={row.get('task')}")
    return report_spend(spent(scope), label)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
