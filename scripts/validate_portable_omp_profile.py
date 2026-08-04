#!/usr/bin/env python3
"""Reject non-portable or secret-bearing OMP profile snapshots."""

from __future__ import annotations

import re
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import NoReturn

import yaml


SENSITIVE_KEY = re.compile(
    r"(?:api[_-]?key|access[_-]?token|oauth[_-]?token|password|passwd|secret|credential)",
    re.IGNORECASE,
)
MACHINE_ONLY_ROOT_KEYS = frozenset({"dev", "setupVersion"})
REQUIRED_MODEL_ROLES = frozenset({"default", "smol", "slow", "task", "advisor"})
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
POCOCK_AGENT_MANIFESTS_DIR = REPOSITORY_ROOT / ".omp" / "agents"


def required_pocock_model_roles() -> frozenset[str]:
    roles = frozenset(
        manifest.stem
        for manifest in POCOCK_AGENT_MANIFESTS_DIR.glob("pocock-*.md")
        if manifest.is_file()
    )
    if not roles:
        fail(f"no Pocock agent manifests in {POCOCK_AGENT_MANIFESTS_DIR}")
    return roles


def fail(message: str) -> NoReturn:
    raise SystemExit(f"invalid portable OMP profile: {message}")


def walk_keys(value: object, path: tuple[str, ...] = ()) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str):
                fail(f"non-string key at {'.'.join(path) or '<root>'}")
            child_path = (*path, key)
            if SENSITIVE_KEY.search(key):
                fail(f"secret-like key {'.'.join(child_path)} is forbidden")
            walk_keys(child, child_path)
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, child in enumerate(value):
            walk_keys(child, (*path, str(index)))


def require_mapping(value: object, path: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        fail(f"{path} must be a mapping")
    return value


def require_value(mapping: Mapping[str, object], key: str, expected: object, path: str) -> None:
    actual = mapping.get(key)
    if actual != expected:
        fail(f"{path}.{key} must be {expected!r}, got {actual!r}")


def validate_profile(profile: object) -> None:
    root = require_mapping(profile, "<root>")
    if not root:
        fail("root mapping is empty")

    machine_only = MACHINE_ONLY_ROOT_KEYS.intersection(root)
    if machine_only:
        fail(f"machine-only root keys are forbidden: {', '.join(sorted(machine_only))}")

    walk_keys(root)

    roles = require_mapping(root.get("modelRoles"), "modelRoles")
    required_pocock_roles = required_pocock_model_roles()
    missing_roles = REQUIRED_MODEL_ROLES.union(required_pocock_roles).difference(roles)
    if missing_roles:
        fail(f"modelRoles is missing: {', '.join(sorted(missing_roles))}")

    empty_pocock_roles = sorted(
        role
        for role in required_pocock_roles
        if not isinstance(roles.get(role), str) or not roles[role].strip()
    )
    if empty_pocock_roles:
        fail(f"modelRoles has empty Pocock roles: {', '.join(empty_pocock_roles)}")


    retry = require_mapping(root.get("retry"), "retry")
    require_value(retry, "modelFallback", True, "retry")
    fallback_chains = require_mapping(retry.get("fallbackChains"), "retry.fallbackChains")
    missing_chains = required_pocock_roles.difference(fallback_chains)
    if missing_chains:
        fail(f"retry.fallbackChains is missing: {', '.join(sorted(missing_chains))}")
    empty_chains = sorted(
        role
        for role in required_pocock_roles
        if not isinstance(fallback_chains.get(role), Sequence)
        or isinstance(fallback_chains[role], (str, bytes, bytearray))
        or not fallback_chains[role]
    )
    if empty_chains:
        fail(f"retry.fallbackChains has empty Pocock chains: {', '.join(empty_chains)}")

    async_settings = require_mapping(root.get("async"), "async")
    require_value(async_settings, "enabled", False, "async")

    task = require_mapping(root.get("task"), "task")
    require_value(task, "batch", True, "task")
    require_value(task, "enableEffort", True, "task")
    require_value(task, "maxRecursionDepth", 1, "task")
    require_value(task, "maxConcurrency", 6, "task")
    require_value(task, "showResolvedModelBadge", True, "task")

    isolation = require_mapping(task.get("isolation"), "task.isolation")
    if isolation.get("mode") in {None, "none"}:
        fail("task.isolation.mode must select an isolated backend")
    require_value(isolation, "apply", False, "task.isolation")
    require_value(isolation, "merge", "patch", "task.isolation")


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"usage: {Path(argv[0]).name} PROFILE.yml", file=sys.stderr)
        return 2

    profile_path = Path(argv[1])
    try:
        profile = yaml.safe_load(profile_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        fail(f"cannot read {profile_path}: {error}")

    validate_profile(profile)
    print(f"portable OMP profile valid: {profile_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
