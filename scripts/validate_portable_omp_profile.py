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
SECRET_VALUE = re.compile(
    r"(?:"
    r"\bgh[pousr]_[A-Za-z0-9]{20,}\b|"
    r"\bgithub_pat_[A-Za-z0-9_]{20,}\b|"
    r"\bsk-(?:ant-)?[A-Za-z0-9_-]{20,}\b|"
    r"\bAIza[0-9A-Za-z_-]{30,}\b|"
    r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b|"
    r"\bnpm_[A-Za-z0-9]{20,}\b|"
    r"\bAKIA[0-9A-Z]{16}\b|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r")"
)
MACHINE_ONLY_ROOT_KEYS = frozenset({"dev", "setupVersion"})
REQUIRED_MODEL_ROLES = frozenset({"default", "smol", "slow", "task", "advisor"})
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
POCOCK_AGENT_MANIFESTS_DIR = REPOSITORY_ROOT / ".omp" / "agents"
ISOLATING_BACKENDS = frozenset(
    {
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
)



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
        return
    if isinstance(value, str) and SECRET_VALUE.search(value):
        fail(f"secret-like scalar at {'.'.join(path) or '<root>'} is forbidden")


def require_mapping(value: object, path: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        fail(f"{path} must be a mapping")
    return value


def require_value(mapping: Mapping[str, object], key: str, expected: object, path: str) -> None:
    actual = mapping.get(key)
    if not (type(actual) is type(expected) and actual == expected):
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

    configured_pocock_roles = frozenset(
        role for role in roles if role.startswith("pocock-")
    )
    if configured_pocock_roles != required_pocock_roles:
        missing_roles = sorted(required_pocock_roles.difference(configured_pocock_roles))
        unexpected_roles = sorted(configured_pocock_roles.difference(required_pocock_roles))
        details = []
        if missing_roles:
            details.append(f"missing: {', '.join(missing_roles)}")
        if unexpected_roles:
            details.append(f"unexpected: {', '.join(unexpected_roles)}")
        fail(f"modelRoles Pocock routes must exactly match agent manifests ({'; '.join(details)})")

    empty_pocock_roles = sorted(
        role
        for role in required_pocock_roles
        if not isinstance(roles.get(role), str) or not roles[role].strip()
    )
    if empty_pocock_roles:
        fail(f"modelRoles has empty Pocock roles: {', '.join(empty_pocock_roles)}")


    retry = require_mapping(root.get("retry"), "retry")
    require_value(retry, "enabled", True, "retry")
    require_value(retry, "modelFallback", True, "retry")
    fallback_chains = require_mapping(retry.get("fallbackChains"), "retry.fallbackChains")
    configured_pocock_chains = frozenset(
        role for role in fallback_chains if role.startswith("pocock-")
    )
    if configured_pocock_chains != required_pocock_roles:
        missing_chains = sorted(required_pocock_roles.difference(configured_pocock_chains))
        unexpected_chains = sorted(configured_pocock_chains.difference(required_pocock_roles))
        details = []
        if missing_chains:
            details.append(f"missing: {', '.join(missing_chains)}")
        if unexpected_chains:
            details.append(f"unexpected: {', '.join(unexpected_chains)}")
        fail(
            "retry.fallbackChains Pocock routes must exactly match agent manifests "
            f"({'; '.join(details)})"
        )
    empty_chains = sorted(
        role
        for role in required_pocock_roles
        if not isinstance(fallback_chains.get(role), Sequence)
        or isinstance(fallback_chains[role], (str, bytes, bytearray))
        or not fallback_chains[role]
    )
    if empty_chains:
        fail(f"retry.fallbackChains has empty Pocock chains: {', '.join(empty_chains)}")

    invalid_chain_entries = []
    for role, chain in fallback_chains.items():
        if not isinstance(chain, Sequence) or isinstance(chain, (str, bytes, bytearray)):
            invalid_chain_entries.append(f"{role} (not a sequence)")
            continue
        invalid_chain_entries.extend(
            f"{role}[{index}]"
            for index, model in enumerate(chain)
            if not isinstance(model, str) or not model.strip()
        )
    if invalid_chain_entries:
        fail(
            "retry.fallbackChains entries must be non-empty strings: "
            + ", ".join(invalid_chain_entries)
        )

    async_settings = require_mapping(root.get("async"), "async")
    require_value(async_settings, "enabled", False, "async")

    task = require_mapping(root.get("task"), "task")
    require_value(task, "batch", True, "task")
    require_value(task, "enableEffort", True, "task")
    require_value(task, "maxRecursionDepth", 1, "task")
    require_value(task, "maxConcurrency", 6, "task")
    require_value(task, "maxRuntimeMs", 1800000, "task")
    require_value(task, "showResolvedModelBadge", True, "task")

    isolation = require_mapping(task.get("isolation"), "task.isolation")
    isolation_mode = isolation.get("mode")
    if not isinstance(isolation_mode, str) or isolation_mode not in ISOLATING_BACKENDS:
        fail(
            "task.isolation.mode must select a canonical isolated backend, "
            f"got {isolation_mode!r}"
        )
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
