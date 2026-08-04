#!/usr/bin/env python3
"""Validate the explicit global OMP invariants required by Pocock."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import yaml


EXPECTED_VALUES = {
    "async.enabled": False,
    "display.showTokenUsage": True,
    "task.batch": True,
    "task.enableEffort": True,
    "task.showResolvedModelBadge": True,
    "task.isolation.apply": False,
    "task.isolation.merge": "patch",
    "task.maxRecursionDepth": 1,
    "task.maxConcurrency": 6,
    "retry.modelFallback": True,
}

ISOLATING_BACKENDS = {
    "auto",
    "apfs",
    "btrfs",
    "zfs",
    "linux-reflink",
    "overlayfs",
    "windows-blockclone",
    "projfs",
    "rcopy",
    "worktree",
    "fuse-overlay",
    "fuse-projfs",
}


def nested_value(config: dict[str, Any], dotted_key: str) -> Any:
    value: Any = config
    for segment in dotted_key.split("."):
        if not isinstance(value, dict) or segment not in value:
            return None
        value = value[segment]
    return value


def validation_errors(config: Any) -> list[str]:
    if not isinstance(config, dict):
        return ["основной конфиг OMP должен быть YAML mapping"]

    errors = []
    for dotted_key, expected in EXPECTED_VALUES.items():
        observed = nested_value(config, dotted_key)
        if type(observed) is not type(expected) or observed != expected:
            errors.append(f"{dotted_key} должен быть {expected!r}, получено {observed!r}")

    isolation_mode = nested_value(config, "task.isolation.mode")
    if isolation_mode not in ISOLATING_BACKENDS:
        errors.append(
            "task.isolation.mode должен включать известный backend OMP и не может быть none; "
            f"получено {isolation_mode!r}"
        )
    return errors


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"usage: {Path(argv[0]).name} CONFIG_YML", file=sys.stderr)
        return 2

    path = Path(argv[1])
    try:
        config = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        print(f"не удалось прочитать основной конфиг OMP {path}: {error}", file=sys.stderr)
        return 1

    errors = validation_errors(config)
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1

    print(f"глобальные OMP task-инварианты явно заданы в {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
