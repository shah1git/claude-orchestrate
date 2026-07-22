#!/usr/bin/env python3
"""Atomic, per-run least-dispatched selector backed by an append-only JSONL.

The caller supplies the already eligible candidates.  This module only keeps
the mechanical dispatch history that makes equal-weight rotation deterministic.
"""

import argparse
import fcntl
import json
import sys
import time
from pathlib import Path


DEFAULT_LEDGER = Path(__file__).resolve().parent.parent / "telemetry" / "dispatch-ledger.jsonl"


class LedgerError(ValueError):
    """The ledger cannot be safely used until its corrupt data is repaired."""


def _load_rows(handle, ledger_path):
    """Read JSONL from an open handle, refusing to conceal corrupt history."""
    handle.seek(0)
    rows = []
    for line_number, line in enumerate(handle, 1):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise LedgerError(
                f"{ledger_path}:{line_number} is not valid JSON — repair the ledger first"
            ) from exc
        if not isinstance(row, dict):
            raise LedgerError(
                f"{ledger_path}:{line_number} must be a JSON object — repair the ledger first"
            )
        rows.append(row)
    return rows


def load_counts(ledger_path, run_id):
    """Return candidate -> dispatch count for one run without modifying a ledger."""
    ledger_path = Path(ledger_path)
    if not ledger_path.is_file():
        return {}
    with ledger_path.open(encoding="utf-8") as handle:
        rows = _load_rows(handle, ledger_path)
    return _counts_for_run(rows, run_id)


def _counts_for_run(rows, run_id):
    counts = {}
    for row in rows:
        if row.get("run_id") == run_id and isinstance(row.get("candidate"), str):
            candidate = row["candidate"]
            counts[candidate] = counts.get(candidate, 0) + 1
    return counts


def select_candidate(candidates, counts):
    """Choose the least-dispatched candidate; list order breaks equal counts."""
    candidates = list(candidates)
    if not candidates:
        raise ValueError("--among must name at least one candidate")
    return min(candidates, key=lambda candidate: counts.get(candidate, 0))


def append_dispatch(handle, run_id, candidate, role=None):
    """Append exactly one dispatch row to an already exclusively locked handle."""
    row = {
        "run_id": run_id,
        "candidate": candidate,
        "role": role,
        "ts": time.time(),
    }
    handle.seek(0, 2)
    handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    handle.flush()
    return row


def claim_dispatch(ledger_path, run_id, candidates, role=None):
    """Atomically select and record one candidate for ``run_id``."""
    ledger_path = Path(ledger_path)
    candidates = list(candidates)
    # Validate before creating a ledger for a malformed command invocation.
    if not candidates:
        raise ValueError("--among must name at least one candidate")
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    with ledger_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            selected = select_candidate(candidates, _counts_for_run(
                _load_rows(handle, ledger_path), run_id))
            append_dispatch(handle, run_id, selected, role)
            return selected
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def record_dispatch(ledger_path, run_id, candidate, role=None):
    """Atomically append a forced dispatch, after validating existing history."""
    ledger_path = Path(ledger_path)
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    with ledger_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            _load_rows(handle, ledger_path)
            append_dispatch(handle, run_id, candidate, role)
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def parse_candidates(value):
    candidates = value.split(",")
    if not all(candidates):
        raise argparse.ArgumentTypeError("--among must be a comma-separated non-empty list")
    return candidates


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)

    def add_ledger_option(command):
        command.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)

    claim = subcommands.add_parser("claim")
    claim.add_argument("--run-id", required=True)
    claim.add_argument("--among", required=True, type=parse_candidates)
    claim.add_argument("--role")
    add_ledger_option(claim)

    peek = subcommands.add_parser("peek")
    peek.add_argument("--run-id", required=True)
    peek.add_argument("--among", required=True, type=parse_candidates)
    add_ledger_option(peek)

    record = subcommands.add_parser("record")
    record.add_argument("--run-id", required=True)
    record.add_argument("--candidate", required=True)
    record.add_argument("--role")
    add_ledger_option(record)
    return parser


def main(argv):
    args = build_parser().parse_args(argv)
    if args.command == "claim":
        print(claim_dispatch(args.ledger, args.run_id, args.among, args.role))
    elif args.command == "peek":
        print(select_candidate(args.among, load_counts(args.ledger, args.run_id)))
    else:
        record_dispatch(args.ledger, args.run_id, args.candidate, args.role)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except (LedgerError, ValueError) as exc:
        print(f"dispatch_ledger: {exc}", file=sys.stderr)
        sys.exit(1)
