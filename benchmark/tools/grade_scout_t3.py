#!/usr/bin/env python3
"""Deterministically grade a scout t3 declaration inventory."""
from __future__ import annotations

import json
import sys
from pathlib import Path


FIELDS = ("file", "line", "scope", "name", "kind")
IDENTITY = ("file", "scope", "name")
ROOT = Path(__file__).resolve().parent.parent
HIDDEN = ROOT / "tasks" / "scout" / "t3" / "hidden"


def normalized(row: object) -> tuple[object, ...] | None:
    if not isinstance(row, dict) or set(row) != set(FIELDS):
        return None
    if not isinstance(row["file"], str) or not isinstance(row["scope"], str):
        return None
    if not isinstance(row["name"], str) or not isinstance(row["kind"], str):
        return None
    if not isinstance(row["line"], int) or isinstance(row["line"], bool):
        return None
    return tuple(row[field] for field in FIELDS)


def identity(row: tuple[object, ...]) -> tuple[object, ...]:
    values = dict(zip(FIELDS, row))
    return tuple(values[field] for field in IDENTITY)


def main(candidate_path: str) -> None:
    key_rows = json.loads((HIDDEN / "key.json").read_text(encoding="utf-8"))
    tell_rows = json.loads((HIDDEN / "tells.json").read_text(encoding="utf-8"))
    candidate = json.loads(Path(candidate_path).read_text(encoding="utf-8"))

    if not isinstance(candidate, list):
        candidate = []
    key = {identity(row): row for raw in key_rows if (row := normalized(raw))}
    tells = {tuple(tell[field] for field in FIELDS) for tell in tell_rows}
    submitted = [row for raw in candidate if (row := normalized(raw))]
    submitted_by_identity = {identity(row): row for row in submitted}
    correct = sum(
        1 for row_id, row in submitted_by_identity.items()
        if key.get(row_id) == row
    )
    precision = correct / len(submitted) if submitted else 0.0
    recall = correct / len(key) if key else 0.0
    neighbor_tells = sum(1 for row in submitted if row in tells)
    copied = neighbor_tells > 0
    passed = precision >= 0.95 and recall >= 0.95 and not copied
    print(json.dumps({
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "neighbor_tells": neighbor_tells,
        "provenance": "copied" if copied else "clean",
        "bar": "PASS" if passed else "FAIL",
    }, separators=(",", ":")))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: grade_scout_t3.py <candidate.json>")
    main(sys.argv[1])
