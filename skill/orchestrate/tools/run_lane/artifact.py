"""artifact.py — snapshot the declared `--out` file strictly from disk.

The single rule this module exists to make mechanical (config.yaml v25,
ADR-0005 §"Что уже сделано"): the artifact is graded from the FILE, never
from a model's own printed account of it. Measured consequence of getting
this wrong once: by printed text, 48/450 correct; by file, 450/450 — the
files were byte-identical (config.yaml:754-767).
"""
from __future__ import annotations

import hashlib
from pathlib import Path


def capture(out_path: Path) -> dict:
    out_path = Path(out_path)
    if not out_path.is_file():
        return {"path": str(out_path), "present": False, "bytes": 0, "sha256": None}
    data = out_path.read_bytes()
    return {
        "path": str(out_path),
        "present": True,
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }
