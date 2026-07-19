#!/usr/bin/env python3
"""telemetry_append.py — the one writer for §7 routing-log records, and the
session-budget fuse (config `session_budget`, quality.md §3/§7).

Born from two telemetry-verified failures of prose-only discipline:
  (a) 2026-07-18, first live run under config v17: session_budget.tokens_max
      3.0M was crossed at ~3.9M and noticed post-factum — the lead's manual
      "sum at every dispatch point" check did not happen (its own run summary
      records the lesson);
  (b) the routing-log vocabulary drifted a THIRD time (ts/subtask/lowercase
      verdicts, 2026-07-15..18) after two documented repair passes — the
      Step 5 "self-check before append" rule is prose, and prose loses.

Both failures share one root cause: the invariants lived in instructions, not
in the write path. This tool IS the write path: every record enters the log
through it, so vocabulary is enforced and the running budget is computed at
the exact moment data is born — not reconstructed post-factum.

Usage:
    telemetry_append.py '<json-object>'          # append one record
    echo '<json-object>' | telemetry_append.py   # same, via stdin
    telemetry_append.py --check-only --task T    # budget verdict, no append
    telemetry_append.py --check-only --run-id R

Options:
    --skill-dir DIR        skill root (default: tools/..)
    --tokens-max N         run-declared ceiling overriding config
                           session_budget.tokens_max (a ticket/plan-declared
                           decision, made up front — mirrors max_diff_lines'
                           override: ticket-declared-only)
    --ack-over-budget R    named lead decision to continue past the ceiling;
                           R is the reason and must be non-empty (config:
                           "никогда молча")

Exit codes:
    0  appended / check passed (or breach explicitly acknowledged)
    1  validation error — NOTHING was appended; fix the record and retry
    2  budget exceeded — the record IS appended (the spend already happened;
       the fuse stops future dispatches, not the bookkeeping), or --check-only
       found the scope over budget. Per config session_budget.on_exceed:
       stop-dispatch-finish-gates-report.
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
    the per-ticket contract — they are a different §7 citizen (config
    telemetry.provider_health_events).
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

    tokens = row.get("tokens")
    if not (tokens is None or isinstance(tokens, int) or tokens == "n/a"):
        fail(f"tokens must be an integer, null, or \"n/a\", got {tokens!r}")

    if (config.get("telemetry", {}).get("stamp_records_with_config")
            and "config" not in row):
        fail("record lacks the config stamp (field 'config': \"v<N>+<sha:7>\") "
             "— required by telemetry.stamp_records_with_config")
    return row


# --- budget (config session_budget, quality.md §3) ---------------------------

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
                # silently shrink the budget sum.
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


def budget_verdict(total: int, tokens_max: int, scope_label: str,
                   ack: str | None) -> int:
    print(f"budget: {total:,} / {tokens_max:,} tokens ({scope_label})")
    if total <= tokens_max:
        return 0
    print(
        f"SESSION BUDGET EXCEEDED ({total - tokens_max:,} tokens over).\n"
        f"on_exceed: stop-dispatch-finish-gates-report — dispatch NO new\n"
        f"tickets; let in-flight tickets finish their gates; report the\n"
        f"remaining frontier as not-done with clean tracker state\n"
        f"(quality.md §3, config session_budget)."
    )
    if ack:
        print(f"over-budget acknowledged — named lead decision: {ack}\n"
              f"(record the decision and reason in the run-summary note)")
        return 0
    print("To continue anyway: re-run with --ack-over-budget \"<reason>\" — "
          "a named decision, never silent.")
    return 2


def main(argv: list) -> int:
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("record", nargs="?")
    ap.add_argument("--skill-dir", type=Path, default=DEFAULT_SKILL_DIR)
    ap.add_argument("--check-only", action="store_true")
    ap.add_argument("--task")
    ap.add_argument("--run-id")
    ap.add_argument("--tokens-max", type=int)
    ap.add_argument("--ack-over-budget", metavar="REASON")
    args = ap.parse_args(argv)

    if args.ack_over_budget is not None and not args.ack_over_budget.strip():
        fail("--ack-over-budget requires a non-empty reason (never silent)")

    config = load_config(args.skill_dir)
    tokens_max = args.tokens_max or (
        config.get("session_budget", {}).get("tokens_max"))
    if not isinstance(tokens_max, int):
        fail("session_budget.tokens_max not found in config.yaml "
             "and no --tokens-max given")
    log_path = args.skill_dir / config.get("telemetry", {}).get(
        "log", "telemetry/routing-log.jsonl")

    if args.check_only:
        if not (args.task or args.run_id):
            fail("--check-only needs --task or --run-id to scope the run")
        rows = run_scope(read_log(log_path), args.task, args.run_id)
        label = f"run_id={args.run_id}" if args.run_id else f"task={args.task}"
        return budget_verdict(spent(rows), tokens_max, label,
                              args.ack_over_budget)

    raw = args.record if args.record is not None else sys.stdin.read()
    try:
        row = json.loads(raw)
    except json.JSONDecodeError as exc:
        fail(f"record is not valid JSON: {exc}")
    if not isinstance(row, dict):
        fail("record must be a single JSON object")

    row = validate_record(row, config)

    # Append BEFORE the budget verdict: the spend already happened, and a
    # record must never be lost to its own fuse. Exit code 2 then stops the
    # lead's NEXT dispatch, which is the only thing still preventable.
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"appended: {row.get('ticket') or row.get('event')} -> {log_path}")

    scope = run_scope(read_log(log_path), row.get("task"), row.get("run_id"))
    label = (f"run_id={row['run_id']}" if row.get("run_id")
             else f"task={row.get('task')}")
    return budget_verdict(spent(scope), tokens_max, label,
                          args.ack_over_budget)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
