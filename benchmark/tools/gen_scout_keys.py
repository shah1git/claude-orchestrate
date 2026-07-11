#!/usr/bin/env python3
"""Deterministic key generator + grader for the scout discipline.

Ground truth is computed here, by rule, from the reference fixture — never
by any candidate. Run with `gen` to (re)build the answer keys before the
runs; run with `grade <candidate.json>` after a run to score against them.

The two scout tasks:
  t1 — inventory every ``raise DepotError("DEP-###", "<msg>")`` site in
       depot/*.py: {file, line, code, message}.
  t2 — inventory every module-level tariff constant (name starting RATE_,
       COEF_ or CAP_) in tariff.py and booking.py: {name, category, value}.

Both are pure mechanical extraction: an exact regex over the source, so the
key is reproducible and the grade is precision/recall against it.
"""
from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

DEPOT = Path(__file__).resolve().parent.parent / "fixtures" / "depot" / "depot"
KEYS = Path(__file__).resolve().parent.parent / "tasks"

# t1: single-line `raise DepotError("DEP-###", "message")`. Message is a
# plain double-quoted or f-string literal; we capture the raw text between
# the second argument's quotes up to the closing paren.
RAISE_RE = re.compile(
    r'raise\s+DepotError\(\s*"(?P<code>DEP-\d{3})"\s*,\s*f?"(?P<msg>.*?)"\s*\)'
)

# t2: module-level assignment whose name begins RATE_/COEF_/CAP_.
CONST_RE = re.compile(r'^(?P<name>(RATE_|COEF_|CAP_)[A-Z0-9_]+)\s*=\s*(?P<val>.+?)\s*$')


def gen_t1() -> list[dict]:
    rows = []
    for py in sorted(DEPOT.glob("*.py")):
        for i, line in enumerate(py.read_text().splitlines(), 1):
            m = RAISE_RE.search(line)
            if m:
                rows.append({"file": py.name, "line": i,
                             "code": m.group("code"), "message": m.group("msg")})
    return rows


def gen_t2() -> list[dict]:
    rows = []
    for name in ("tariff.py", "booking.py"):
        py = DEPOT / name
        for i, line in enumerate(py.read_text().splitlines(), 1):
            m = CONST_RE.match(line)
            if m:
                cat = m.group("name").split("_", 1)[0]  # RATE / COEF / CAP
                rows.append({"file": name, "name": m.group("name"),
                             "category": cat, "value": m.group("val")})
    return rows


def _key_t1(r): return (r["file"], r["code"])
def _key_t2(r): return (r["file"], r["name"])


def grade(task: str, cand_path: str) -> None:
    key = json.loads((KEYS / "scout" / task / "hidden" / "key.json").read_text())
    cand = json.loads(Path(cand_path).read_text())
    keyfn = _key_t1 if task == "t1" else _key_t2
    kset = {keyfn(r): r for r in key}
    cset = {keyfn(r): r for r in cand}
    tp = kset.keys() & cset.keys()
    # A true positive must also match on the value fields, not just identity.
    fields = ("line", "code", "message") if task == "t1" else ("category", "value")
    correct = {k for k in tp if all(str(kset[k].get(f)) == str(cset[k].get(f)) for f in fields)}
    precision = len(correct) / len(cset) if cset else 0.0
    recall = len(correct) / len(kset) if kset else 0.0
    print(json.dumps({
        "task": task, "key_size": len(kset), "cand_size": len(cset),
        "correct": len(correct), "precision": round(precision, 4),
        "recall": round(recall, 4),
        "bar": "PASS" if precision >= 0.95 and recall >= 0.95 else "FAIL",
        "missed": [kset[k] for k in (kset.keys() - correct)][:10],
        "wrong_or_extra": [cset[k] for k in (cset.keys() - correct)][:10],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    if sys.argv[1] == "gen":
        (KEYS / "scout" / "t1" / "hidden" / "key.json").write_text(
            json.dumps(gen_t1(), ensure_ascii=False, indent=2))
        (KEYS / "scout" / "t2" / "hidden" / "key.json").write_text(
            json.dumps(gen_t2(), ensure_ascii=False, indent=2))
        print(f"t1: {len(gen_t1())} raise sites; t2: {len(gen_t2())} constants")
    elif sys.argv[1] == "grade":
        grade(sys.argv[2], sys.argv[3])
