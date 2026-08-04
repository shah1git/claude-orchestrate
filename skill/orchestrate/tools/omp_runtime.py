#!/usr/bin/env python3
"""Deterministic control plane for the OMP-native Pocock skill heads.

The module owns policy, persisted Run/Wave/Attempt/Evidence state, task sealing,
gates, and telemetry.  It deliberately never launches a model: OMP's native
``task`` tool remains the only worker transport, while the pocock-control
extension adapts structured OMP observations to this CLI.

Every successful invocation writes exactly one JSON object to stdout.  Every
failure writes exactly one machine-readable diagnostic to stderr and exits
non-zero.  Mutations use an fcntl lock, optimistic revisions, atomic replace,
and a hash-linked state snapshot.
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import fcntl
import hashlib
import heapq
import hmac
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import secrets
import time
import uuid
from pathlib import Path, PurePosixPath

import yaml


SKILL_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = SKILL_DIR / "config.yaml"
TELEMETRY_TOOL = Path(__file__).resolve().parent / "telemetry_append.py"
REQUEST_BYTES_MAX = 32 * 1024 * 1024
SCHEMA_VERSION = 2
PROTOCOL_VERSION = 1
TICKET_FIELDS = (
    "OBJECTIVE",
    "CONTEXT",
    "INPUTS",
    "OUTPUT",
    "TOOLS",
    "BOUNDARIES",
    "ACCEPTANCE",
)
TERMINAL_PHASES = {"completed", "cancelled"}
REPLACEMENT_STAGING_PHASE = "replacement_staging"
CLASS_ORDER = {"mechanical": 0, "skilled": 1, "judgment": 2}
RUNTIME_CHANGED_MESSAGE = (
    "effective Pocock runtime differs from the runtime that created this run; "
    "inspect it with status and start a new run"
)
RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{5,127}$")
PATCH_FORBIDDEN_PREFIXES = (".git",)
DEFAULT_PATCH_BYTES_MAX = 16 * 1024 * 1024
# Every child process the runtime spawns carries an explicit wall clock
# (code.md §2.9): a hung git, omp, or telemetry writer must fail the command,
# never block the control plane indefinitely.
GIT_TIMEOUT_SECONDS = 60
OMP_CONFIG_TIMEOUT_SECONDS = 30
TELEMETRY_TIMEOUT_SECONDS = 60
ISOLATED_OMP_MODES = frozenset(
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
        # OMP keeps these legacy spellings for backward-compatible profiles.
        "worktree",
        "fuse-overlay",
        "fuse-projfs",
    }
)

SWEEP_INTEGRATIONS = frozenset({"aggregate", "disjoint_patches"})

LENS_NAMES = ("Standards", "Spec", "Critic")
INCOMPLETE_TRACKER_REFERENCE_RE = re.compile(r"(?<![A-Za-z0-9_./:-])#\d+(?![A-Za-z0-9_])")


class RuntimeFailure(Exception):
    """A refusal that must leave the authoritative state unchanged."""

    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def diagnostic(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, **self.details}


def fail(condition: bool, code: str, message: str, **details: Any) -> None:
    if condition:
        raise RuntimeFailure(code, message, **details)


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()

def contains_lone_unicode_surrogate(value: str) -> bool:
    """Return whether a JSON string contains an unpaired UTF-16 surrogate."""
    return any("\ud800" <= character <= "\udfff" for character in value)


def require_evidence_text(value: Any, field: str) -> str:
    fail(
        not isinstance(value, str) or value == "" or contains_lone_unicode_surrogate(value),
        "evidence_invalid",
        f"{field} must be a non-empty Unicode string without lone surrogates",
    )
    return value


def normalize_ui_probe(value: Any) -> dict[str, str]:
    """Validate and canonically order the closed v1 browser probe schema."""
    fail(not isinstance(value, dict), "evidence_invalid", "witness.probe must be an object")
    kind = value.get("kind")
    fail(not isinstance(kind, str), "evidence_invalid", "witness.probe.kind must be a string")
    fields = {
        "url": ("expected", "kind"),
        "dom": ("expected", "kind", "selector"),
    }.get(kind)
    fail(fields is None, "evidence_invalid", "witness.probe.kind is unknown")
    fail(set(value) != set(fields), "evidence_invalid", "witness.probe has unsupported or missing fields")
    normalized = {field: require_evidence_text(value[field], f"witness.probe.{field}") for field in fields}
    fail(normalized["kind"] != kind, "evidence_invalid", "witness.probe.kind is invalid")
    return {field: normalized[field] for field in sorted(normalized)}


def normalize_ui_witness(value: Any) -> dict[str, Any]:
    """Validate the adapter-owned, closed v1 UI witness envelope."""
    fail(not isinstance(value, dict), "evidence_invalid", "witness must be an object")
    fields = {"version", "witnessId", "attemptId", "challengeToken", "criterion", "probe", "probeHash"}
    fail(set(value) != fields, "evidence_invalid", "witness has unsupported or missing fields")
    fail(value.get("version") != 1 or isinstance(value.get("version"), bool), "evidence_invalid", "witness.version must be exactly 1")
    return {
        "attemptId": require_evidence_text(value["attemptId"], "witness.attemptId"),
        "challengeToken": require_evidence_text(value["challengeToken"], "witness.challengeToken"),
        "criterion": require_evidence_text(value["criterion"], "witness.criterion"),
        "probe": normalize_ui_probe(value["probe"]),
        "probeHash": require_evidence_text(value["probeHash"], "witness.probeHash"),
        "version": 1,
        "witnessId": require_evidence_text(value["witnessId"], "witness.witnessId"),
    }


def declared_slot(mapping: Any, key: str, label: str) -> str:
    """Return the single Pocock slot bound to a producer class or lens."""
    definition = mapping.get(key) if isinstance(mapping, dict) else None
    fail(not isinstance(definition, dict), "config_invalid", f"{label} declares no slot mapping")
    slot = definition.get("slot")
    fail(not isinstance(slot, str) or not slot, "config_invalid", f"{label} declares no slot")
    effort = definition.get("effort")
    fail(not isinstance(effort, str) or not effort, "config_invalid", f"{label} declares no effort")
    return slot


def validate_slot_disjointness(omp: dict[str, Any]) -> None:
    """The config invariant that replaces the whole vendor/family apparatus.

    A review is independent because the lens runs on a slot no producer can
    occupy and no other lens shares. That is a statement about slot names
    alone, so it is settled here once instead of being recomputed per wave from
    model metadata. It does not, and cannot, catch two slots that resolve to the
    same model; `assert_reviewers_are_independent` checks the known witnesses at
    dispatch and `observed_reviewer_collisions` checks runtime fallbacks after
    settlement.
    """
    producers: set[str] = set()
    producer_map = omp.get("producers")
    fail(not isinstance(producer_map, dict) or not producer_map, "config_invalid", "omp.producers declares no classes")
    for cls in producer_map:
        capability = producer_map[cls].get("capability") if isinstance(producer_map[cls], dict) else None
        fail(not isinstance(capability, str) or not capability, "config_invalid", f"omp.producers.{cls} declares no capability")
        producers.add(declared_slot(producer_map, cls, f"omp.producers.{cls}"))

    lens_config = omp.get("lenses")
    fail(not isinstance(lens_config, dict), "config_invalid", "omp.lenses is missing")
    fail(
        not isinstance(lens_config.get("capability"), str) or not lens_config["capability"],
        "config_invalid",
        "omp.lenses declares no reviewer capability",
    )
    lens_map = lens_config.get("slots")
    fail(not isinstance(lens_map, dict) or not lens_map, "config_invalid", "omp.lenses.slots declares no lenses")
    lenses: set[str] = set()
    for lens in lens_map:
        slot = declared_slot(lens_map, lens, f"omp.lenses.slots.{lens}")
        fail(
            slot in lenses,
            "config_invalid",
            f"lens {lens} shares a slot with another lens: {slot}",
        )
        lenses.add(slot)

    overlap = sorted(producers & lenses)
    fail(
        bool(overlap),
        "config_invalid",
        f"producer and lens slots must be disjoint; shared: {', '.join(overlap)}",
    )
    declared = set(omp.get("slots", {}))
    missing = sorted((producers | lenses) - declared)
    fail(bool(missing), "config_invalid", f"slots are not declared in omp.slots: {', '.join(missing)}")
    unbound = sorted(declared - (producers | lenses))
    fail(bool(unbound), "config_invalid", f"omp.slots declares unbound slots: {', '.join(unbound)}")


def load_config() -> dict[str, Any]:
    try:
        raw = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise RuntimeFailure("config_invalid", f"cannot load {CONFIG_PATH}: {exc}") from exc
    fail(not isinstance(raw, dict), "config_invalid", "config.yaml must contain a mapping")
    omp = raw.get("omp")
    fail(not isinstance(omp, dict), "config_invalid", "config.yaml lacks the required omp policy block")
    validate_slot_disjointness(omp)
    return raw


def active_policy_snapshot(config: dict[str, Any]) -> dict[str, Any]:
    """The exact config subset the native contour consumes.

    The fingerprint measures behaviour changes, not file bytes: archived
    `run_lane` blocks and comment edits in config.yaml must not invalidate a
    live run, while any semantic change below must.
    """
    gates = config.get("gates")
    shape = config.get("shape")
    return {
        "version": config.get("version"),
        "omp": config.get("omp"),
        "session_budget": config.get("session_budget"),
        "shape_values": shape.get("values") if isinstance(shape, dict) else None,
        "routing": config.get("routing"),
        "gates_pre_gate": gates.get("pre_gate") if isinstance(gates, dict) else None,
        "telemetry": config.get("telemetry"),
    }


def config_fingerprint(config: dict[str, Any]) -> str:
    version = config.get("version")
    policy = hashlib.sha256(canonical(active_policy_snapshot(config)).encode("utf-8")).hexdigest()[:7]
    return f"v{version}+{policy}"


def state_base(cwd: Path, explicit: str | None) -> Path:
    if explicit:
        base = Path(explicit).expanduser()
    elif os.environ.get("POCOCK_STATE_DIR"):
        base = Path(os.environ["POCOCK_STATE_DIR"]).expanduser()
    else:
        xdg = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
        base = xdg / "pocock-omp"
    repo_key = hashlib.sha256(str(cwd.resolve()).encode("utf-8")).hexdigest()[:20]
    return base / repo_key


def validate_run_id(run_id: Any) -> str:
    fail(not isinstance(run_id, str) or not RUN_ID_RE.fullmatch(run_id), "invalid_run_id", "runId has an invalid format")
    return run_id



def model_base(value: Any) -> str:
    model = str(value or "").strip()
    head, separator, suffix = model.rpartition(":")
    if separator and suffix.lower() in {"none", "minimal", "low", "medium", "high", "xhigh", "max"}:
        return head
    return model


def state_paths(cwd: Path, explicit: str | None, run_id: str) -> tuple[Path, Path]:
    root = state_base(cwd, explicit)
    return root / "runs" / f"{run_id}.json", root / "locks" / f"{run_id}.lock"


def start_lock_path(cwd: Path, explicit: str | None) -> Path:
    return state_base(cwd, explicit) / "locks" / "start.lock"


def find_active_run(cwd: Path, explicit: str | None) -> dict[str, Any] | None:
    runs_path = state_base(cwd, explicit) / "runs"
    try:
        entries = sorted(runs_path.iterdir())
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise RuntimeFailure("state_corrupt", f"cannot scan authoritative runs: {exc}") from exc

    active: list[dict[str, Any]] = []
    for path in entries:
        try:
            is_artifact_directory = not path.is_symlink() and path.is_dir() and path.suffix == ".artifacts"
            is_valid_state_file = not path.is_symlink() and path.is_file() and path.suffix == ".json"
        except OSError as exc:
            raise RuntimeFailure("state_corrupt", f"cannot inspect authoritative run entry {path}: {exc}") from exc
        if is_artifact_directory:
            continue
        fail(
            not is_valid_state_file,
            "state_corrupt",
            f"authoritative runs directory contains an invalid entry: {path}",
        )
        state = read_state(path)
        run_id = state.get("runId")
        phase = state.get("phase")
        fail(
            not isinstance(run_id, str) or path.name != f"{run_id}.json" or not isinstance(phase, str),
            "state_corrupt",
            f"authoritative run state has an invalid identity: {path}",
        )
        if phase not in TERMINAL_PHASES and phase != REPLACEMENT_STAGING_PHASE:
            active.append(state)

    fail(
        len(active) > 1,
        "state_corrupt",
        "repository contains more than one nonterminal Pocock run",
        runIds=sorted(str(state["runId"]) for state in active),
    )
    return active[0] if active else None


def require_start_slot(
    cwd: Path,
    explicit: str | None,
    replacement_manifest_fingerprint: str,
) -> dict[str, Any] | None:
    active = find_active_run(cwd, explicit)
    if active is None:
        return None
    mismatch = run_runtime_mismatch(active, replacement_manifest_fingerprint)
    if mismatch is None:
        raise RuntimeFailure(
            "active_run_exists",
            f"repository already has a nonterminal Pocock run: {active['runId']} ({active['phase']})",
            runId=active["runId"],
            phase=active["phase"],
        )
    return active


def retire_runtime_mismatched_run(
    cwd: Path,
    explicit: str | None,
    run_id: str,
    replacement_run_id: str,
    replacement_manifest_fingerprint: str,
) -> None:
    state_path, lock_path = state_paths(cwd, explicit, run_id)
    with with_lock(lock_path):
        active = read_state(state_path)
        if active.get("phase") in TERMINAL_PHASES:
            superseded_by = nullable_text(active.get("supersededBy"))
            fail(
                superseded_by is not None and superseded_by != replacement_run_id,
                "replacement_transaction_corrupt",
                f"terminal run {run_id} names another replacement",
            )
            return
        mismatch = run_runtime_mismatch(active, replacement_manifest_fingerprint)
        fail(
            mismatch is None,
            "active_run_exists",
            f"repository already has a nonterminal Pocock run: {active['runId']} ({active['phase']})",
            runId=active["runId"],
            phase=active["phase"],
        )
        rollback_orphaned_patch_journals(cwd, explicit, active)
        if active.get("entry") == "sweep" and active.get("phase") != "sweep_admission":
            accepted = set(require_sweep_integrity(active)["acceptedTicketIds"])
            rollback_ids = {
                attempt_id
                for attempt_id, attempt in active.get("attempts", {}).items()
                if isinstance(attempt.get("appliedPatch"), dict) and attempt.get("ticketId") not in accepted
            }
        else:
            rollback_ids = {
                attempt_id
                for attempt_id, attempt in active.get("attempts", {}).items()
                if isinstance(attempt.get("appliedPatch"), dict)
                and attempt.get("status") not in {"accepted", "recorded"}
            }
        rollback_attempt_patches(cwd, active, rollback_ids)
        previous_hash = active["stateHash"]
        active["phase"] = "cancelled"
        active["blockedReason"] = None
        active["cancelReason"] = "superseded after runtime mismatch"
        active["runtimeMismatch"] = mismatch
        active["supersededBy"] = replacement_run_id
        active["revision"] += 1
        seal_state(active, state_path, previous_hash)
        write_state(state_path, active)


def replacement_transaction_path(cwd: Path, explicit: str | None, replacement_run_id: str) -> Path:
    return state_base(cwd, explicit) / "replacement-transactions" / f"{replacement_run_id}.json"


def write_replacement_transaction(path: Path, transaction: dict[str, Any]) -> None:
    body = {field: value for field, value in transaction.items() if field != "mac"}
    body["mac"] = record_mac(body, transaction_auth_key(path, create=True))
    write_bytes_atomic(path, (canonical(body) + "\n").encode("utf-8"))


def recover_replacement_transactions(cwd: Path, explicit: str | None) -> None:
    directory = state_base(cwd, explicit) / "replacement-transactions"
    try:
        entries = sorted(directory.iterdir())
    except FileNotFoundError:
        return
    except OSError as exc:
        raise RuntimeFailure("replacement_transaction_corrupt", f"cannot scan replacement transactions: {exc}") from exc

    for transaction_path in entries:
        fail(
            transaction_path.is_symlink() or not transaction_path.is_file() or transaction_path.suffix != ".json",
            "replacement_transaction_corrupt",
            f"replacement transaction directory contains an invalid entry: {transaction_path}",
        )
        try:
            transaction = json.loads(transaction_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeFailure("replacement_transaction_corrupt", f"cannot read replacement transaction: {exc}") from exc
        fail(not isinstance(transaction, dict), "replacement_transaction_corrupt", "replacement transaction is not an object")
        status = transaction.get("status")
        fail(
            status not in {"prepared", "committed"},
            "replacement_transaction_corrupt",
            f"replacement transaction has an invalid status: {transaction_path}",
        )
        if status == "committed":
            continue
        # A prepared transaction retires a live run, so it executes only with a
        # valid authentication witness. Records without a mac are pre-auth
        # legacy residue: skipped, never executed.
        if "mac" not in transaction or not record_mac_valid(transaction, transaction_auth_key(transaction_path, create=False)):
            continue

        old_run_id = validate_run_id(transaction.get("oldRunId"))
        new_run_id = validate_run_id(transaction.get("newRunId"))
        fail(
            transaction_path.name != f"{new_run_id}.json",
            "replacement_transaction_corrupt",
            f"replacement transaction filename does not match {new_run_id}",
        )
        manifest_fingerprint = require_text(
            transaction.get("manifestFingerprint"),
            "replacementTransaction.manifestFingerprint",
        )
        target_phase = require_text(transaction.get("targetPhase"), "replacementTransaction.targetPhase")
        staged_value = transaction.get("replacementState")
        fail(not isinstance(staged_value, dict), "replacement_transaction_corrupt", "replacement state is not an object")
        staged = copy.deepcopy(staged_value)
        new_state_path, new_lock_path = state_paths(cwd, explicit, new_run_id)
        validate_state_snapshot(staged, new_state_path)
        fail(
            staged.get("runId") != new_run_id
            or staged.get("phase") != REPLACEMENT_STAGING_PHASE
            or staged.get("replacementPhase") != target_phase,
            "replacement_transaction_corrupt",
            "replacement staging state does not match its transaction",
        )

        with with_lock(new_lock_path):
            if new_state_path.exists():
                replacement = read_state(new_state_path)
            else:
                write_state(new_state_path, staged)
                replacement = read_state(new_state_path)
        fail(
            replacement.get("runId") != new_run_id
            or replacement.get("phase") not in {REPLACEMENT_STAGING_PHASE, target_phase},
            "replacement_transaction_corrupt",
            "durable replacement state does not match its transaction",
        )

        retire_runtime_mismatched_run(
            cwd,
            explicit,
            old_run_id,
            new_run_id,
            manifest_fingerprint,
        )

        with with_lock(new_lock_path):
            replacement = read_state(new_state_path)
            if replacement.get("phase") == REPLACEMENT_STAGING_PHASE:
                previous_hash = replacement["stateHash"]
                replacement["phase"] = replacement.pop("replacementPhase")
                replacement["revision"] += 1
                seal_state(replacement, new_state_path, previous_hash)
                write_state(new_state_path, replacement)
            else:
                fail(
                    replacement.get("phase") != target_phase,
                    "replacement_transaction_corrupt",
                    "replacement state became active in an unexpected phase",
                )

        transaction["status"] = "committed"
        transaction["committedAt"] = dt.datetime.now(dt.timezone.utc).isoformat()
        write_replacement_transaction(transaction_path, transaction)


def create_replacement_transaction(
    cwd: Path,
    explicit: str | None,
    active: dict[str, Any],
    replacement: dict[str, Any],
    replacement_path: Path,
) -> dict[str, Any]:
    target_phase = require_text(replacement.get("phase"), "replacement.phase")
    staged = copy.deepcopy(replacement)
    staged["phase"] = REPLACEMENT_STAGING_PHASE
    staged["replacementPhase"] = target_phase
    seal_state(staged, replacement_path)
    transaction_path = replacement_transaction_path(cwd, explicit, str(staged["runId"]))
    fail(transaction_path.exists(), "run_collision", "generated replacement transaction already exists")
    transaction = {
        "schemaVersion": 1,
        "oldRunId": active["runId"],
        "newRunId": staged["runId"],
        "manifestFingerprint": staged["manifestFingerprint"],
        "targetPhase": target_phase,
        "replacementState": staged,
        "status": "prepared",
    }
    write_replacement_transaction(transaction_path, transaction)
    recover_replacement_transactions(cwd, explicit)
    return read_state(replacement_path)


def runtime_fingerprint(config: dict[str, Any] | None = None) -> str:
    # Witness of what the native contour actually executes: the two runtime
    # modules plus the normalized active policy subset. `dispatch_ledger.py`
    # was dropped when slot binding replaced round-robin slot claiming, and
    # archived config blocks stay out for the same reason: hashing bytes the
    # contour cannot reach would invalidate live runs for inert edits.
    if config is None:
        config = load_config()
    files = (
        Path(__file__).resolve(),
        TELEMETRY_TOOL.resolve(),
    )
    witness = hashlib.sha256()
    for path in files:
        fail(not path.is_file(), "runtime_unavailable", f"runtime witness file is missing: {path}")
        witness.update(path.name.encode("utf-8"))
        witness.update(b"\0")
        witness.update(path.read_bytes())
        witness.update(b"\0")
    witness.update(canonical(active_policy_snapshot(config)).encode("utf-8"))
    return witness.hexdigest()


def agent_manifest_definitions(directory: Path) -> dict[str, tuple[Path, str]]:
    definitions: dict[str, tuple[Path, str]] = {}
    try:
        paths = sorted(directory.glob("*.md"))
    except OSError:
        return definitions
    for path in paths:
        try:
            content = path.read_bytes()
            text = content.decode("utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        lines = text.splitlines()
        if not lines or lines[0] != "---":
            continue
        try:
            end = lines.index("---", 1)
            frontmatter = yaml.safe_load("\n".join(lines[1:end]))
        except (ValueError, yaml.YAMLError):
            continue
        name = frontmatter.get("name") if isinstance(frontmatter, dict) else None
        if isinstance(name, str) and name and name not in definitions:
            definitions[name] = (path, hashlib.sha256(content).hexdigest())
    return definitions


def nearest_project_agent_directory(cwd: Path) -> Path | None:
    current = cwd.resolve()
    while True:
        candidate = current / ".omp" / "agents"
        if candidate.is_dir():
            return candidate
        if current.parent == current:
            return None
        current = current.parent


def trusted_manifest_fingerprint(cwd: Path, config: dict[str, Any]) -> str:
    agent_home = Path(os.environ.get("PI_CODING_AGENT_DIR", "")).expanduser() if os.environ.get("PI_CODING_AGENT_DIR", "").strip() else Path.home() / ".omp" / "agent"
    installed = agent_home / "agents"
    fallback = SKILL_DIR.parents[1] / ".omp" / "agents"
    trusted = agent_manifest_definitions(installed if installed.is_dir() else fallback)
    project_directory = nearest_project_agent_directory(cwd)
    project = agent_manifest_definitions(project_directory) if project_directory is not None else {}
    names = {
        agent
        for role in config["omp"].get("roles", {}).values()
        if isinstance(role, dict)
        for agent in role.get("agents", {}).values()
        if isinstance(agent, str) and agent
    }
    witness = hashlib.sha256()
    for name in sorted(names):
        fail(name not in trusted, "manifest_unavailable", f"trusted Pocock agent manifest is missing for {name}")
        trusted_path, trusted_digest = trusted[name]
        effective_path, effective_digest = project.get(name, (trusted_path, trusted_digest))
        fail(
            effective_digest != trusted_digest,
            "manifest_shadowed",
            f"project agent {name} shadows the trusted Pocock manifest: {effective_path}",
        )
        witness.update(name.encode("utf-8"))
        witness.update(b"\0")
        witness.update(trusted_digest.encode("utf-8"))
        witness.update(b"\0")
    return witness.hexdigest()


def state_key_path(state_path: Path) -> Path:
    return state_path.parent.parent / "state-auth.key"


def _load_or_create_auth_key(path: Path, *, create: bool) -> bytes:
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, "wb") as handle:
                handle.write(secrets.token_bytes(32))
                handle.flush()
                os.fsync(handle.fileno())
    try:
        key = path.read_bytes()
    except FileNotFoundError as exc:
        raise RuntimeFailure("state_auth_failed", f"state authentication key is missing: {path}") from exc
    fail(path.stat().st_mode & 0o777 != 0o600, "state_auth_failed", f"state authentication key permissions must be 0600: {path}")
    fail(len(key) < 32, "state_auth_failed", f"state authentication key is invalid: {path}")
    return key


def load_or_create_state_key(state_path: Path, *, create: bool) -> bytes:
    return _load_or_create_auth_key(state_key_path(state_path), create=create)


def journal_auth_key(artifact_dir: Path, *, create: bool) -> bytes:
    """The run's state-auth key, addressed from its artifact directory."""
    return _load_or_create_auth_key(artifact_dir.parent.parent / "state-auth.key", create=create)


def transaction_auth_key(transaction_path: Path, *, create: bool) -> bytes:
    return _load_or_create_auth_key(transaction_path.parent.parent / "state-auth.key", create=create)


def record_mac(payload: dict[str, Any], key: bytes) -> str:
    """HMAC over every field except the mac itself; journals and transactions
    carry it so recovery paths never execute self-declared forged records."""
    body = {field: value for field, value in payload.items() if field != "mac"}
    return hmac.new(key, canonical(body).encode("utf-8"), hashlib.sha256).hexdigest()


def record_mac_valid(payload: dict[str, Any], key: bytes) -> bool:
    observed = payload.get("mac")
    if not isinstance(observed, str):
        return False
    return hmac.compare_digest(observed, record_mac(payload, key))


def public_snapshot(state: dict[str, Any]) -> dict[str, Any]:
    snapshot = copy.deepcopy(state)
    snapshot.pop("stateHash", None)
    snapshot.pop("stateMac", None)
    return snapshot


def authenticated_snapshot(state: dict[str, Any]) -> dict[str, Any]:
    snapshot = copy.deepcopy(state)
    snapshot.pop("stateMac", None)
    return snapshot


def seal_state(state: dict[str, Any], state_path: Path, previous_hash: str | None = None) -> None:
    if previous_hash is not None:
        state["previousStateHash"] = previous_hash
    state["updatedAt"] = dt.datetime.now(dt.timezone.utc).isoformat()
    state["stateHash"] = digest(public_snapshot(state))
    key = load_or_create_state_key(state_path, create=True)
    state["stateMac"] = hmac.new(key, canonical(authenticated_snapshot(state)).encode("utf-8"), hashlib.sha256).hexdigest()


def write_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def validate_state_snapshot(state: dict[str, Any], path: Path) -> dict[str, Any]:
    fail(not isinstance(state, dict), "state_corrupt", "authoritative state is not an object")
    observed = state.get("stateHash")
    fail(not isinstance(observed, str), "state_corrupt", "authoritative state has no hash")
    expected = digest(public_snapshot(state))
    fail(observed != expected, "state_corrupt", "authoritative state hash does not match its contents")
    observed_mac = state.get("stateMac")
    fail(not isinstance(observed_mac, str), "state_auth_failed", "authoritative state has no authentication witness")
    key = load_or_create_state_key(path, create=False)
    expected_mac = hmac.new(key, canonical(authenticated_snapshot(state)).encode("utf-8"), hashlib.sha256).hexdigest()
    fail(not hmac.compare_digest(observed_mac, expected_mac), "state_auth_failed", "authoritative state authentication witness does not match its contents")
    if (
        state.get("entry") == "sweep"
        # Terminal runs are immutable history: re-deriving the sealed ledger
        # on every scan buys nothing, so integrity is enforced only while the
        # run can still mutate.
        and state.get("phase") not in TERMINAL_PHASES
        and state.get("phase") not in {"sweep_admission", REPLACEMENT_STAGING_PHASE}
    ):
        require_sweep_integrity(state)
    return state


def read_state(path: Path) -> dict[str, Any]:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeFailure("run_not_found", f"Pocock run does not exist: {path.stem}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeFailure("state_corrupt", f"cannot read authoritative state: {exc}") from exc
    return validate_state_snapshot(state, path)


def with_lock(lock_path: Path):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "a+", encoding="utf-8")
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    return handle



def run_runtime_mismatch(
    state: dict[str, Any],
    observed_manifest_fingerprint: str | None = None,
) -> dict[str, str | None] | None:
    expected = nullable_text(state.get("runtimeFingerprint"))
    observed = runtime_fingerprint()
    mismatch: dict[str, str | None] = {}
    if expected != observed:
        mismatch.update({"expected": expected, "observed": observed})
    if observed_manifest_fingerprint is not None:
        expected_manifest = nullable_text(state.get("manifestFingerprint"))
        if expected_manifest != observed_manifest_fingerprint:
            mismatch.update({
                "expectedManifestFingerprint": expected_manifest,
                "observedManifestFingerprint": observed_manifest_fingerprint,
            })
    return mismatch or None


def require_run_runtime(state: dict[str, Any]) -> None:
    mismatch = run_runtime_mismatch(state)
    fail(
        mismatch is not None,
        "runtime_changed",
        RUNTIME_CHANGED_MESSAGE,
        **(mismatch or {}),
    )

def mutate(
    cwd: Path,
    explicit_state_dir: str | None,
    request: dict[str, Any],
    operation: Callable[[dict[str, Any]], dict[str, Any] | None],
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    run_id = validate_run_id(request.get("runId"))
    state_path, lock_path = state_paths(cwd, explicit_state_dir, run_id)
    with with_lock(lock_path):
        state = read_state(state_path)
        require_run_runtime(state)
        revision = request.get("revision")
        fail(
            not isinstance(revision, int) or revision != state.get("revision"),
            "stale_revision",
            f"expected revision {state.get('revision')}, got {revision!r}",
            runId=run_id,
            currentRevision=state.get("revision"),
        )
        fail(
            request.get("stateHash") != state.get("stateHash"),
            "state_mismatch",
            "mutation stateHash does not match authoritative run state",
            runId=run_id,
            currentRevision=state.get("revision"),
            currentStateHash=state.get("stateHash"),
        )
        previous_hash = state["stateHash"]
        extra = operation(state)
        state["revision"] += 1
        seal_state(state, state_path, previous_hash)
        write_state(state_path, state)
        return state, extra


def ticket_attempt_count(state: dict[str, Any], ticket_id: str) -> int:
    return sum(1 for attempt in state.get("attempts", {}).values() if attempt.get("ticketId") == ticket_id)

def lens_attempt_count(state: dict[str, Any], wave_id: str, lens: str) -> int:
    return sum(
        1
        for attempt in state.get("lensAttempts", {}).values()
        if attempt.get("waveId") == wave_id and attempt.get("lens") == lens
    )


def current_wave(state: dict[str, Any]) -> dict[str, Any]:
    wave_id = state.get("currentWave")
    wave = next((item for item in state.get("waves", []) if item.get("waveId") == wave_id), None)
    fail(not isinstance(wave, dict), "state_corrupt", "current wave is missing")
    return wave


def wave_attempt_ids(state: dict[str, Any], wave: dict[str, Any], field: str = "attemptIds") -> list[str]:
    attempt_ids = wave.get(field, [])
    fail(
        not isinstance(attempt_ids, list)
        or any(not isinstance(attempt_id, str) or attempt_id not in state.get("attempts", {}) for attempt_id in attempt_ids),
        "state_corrupt",
        f"wave {wave.get('waveId')} has invalid {field}",
    )
    return list(attempt_ids)


def retry_ticket_ids(state: dict[str, Any]) -> set[str]:
    raw = state.get("retryTicketIds", [])
    fail(
        not isinstance(raw, list) or any(not isinstance(ticket_id_value, str) or not ticket_id_value for ticket_id_value in raw),
        "state_corrupt",
        "retryTicketIds is invalid",
    )
    return set(raw)


def set_retry_ticket_ids(state: dict[str, Any], ticket_ids: set[str]) -> None:
    state["retryTicketIds"] = sorted(ticket_ids)


def ordered_lens_names(lenses: set[str]) -> list[str]:
    fail(not lenses.issubset(set(LENS_NAMES)), "state_corrupt", "unknown lens retry name")
    return [lens for lens in LENS_NAMES if lens in lenses]


def retry_lens_names(state: dict[str, Any]) -> set[str]:
    raw = state.get("retryLensNames", [])
    fail(
        not isinstance(raw, list)
        or any(not isinstance(lens, str) or lens not in LENS_NAMES for lens in raw)
        or len(raw) != len(set(raw)),
        "state_corrupt",
        "retryLensNames is invalid",
    )
    return set(raw)


def set_retry_lens_names(state: dict[str, Any], lenses: set[str]) -> None:
    state["retryLensNames"] = ordered_lens_names(lenses)


def route_ticket_retry(state: dict[str, Any], attempts: dict[str, dict[str, Any]], kind: str) -> list[str]:
    """Bind each failed ticket to the axis its own failure calls for.

    Insufficient depth is answered with depth: the ticket class rises and the
    class selects a deeper slot. Availability never changes a slot; OMP owns
    model replacement through the role's fallback chain. A writing ticket may
    never become `judgment`, so exhausting `skilled` is terminal.

    Returns tickets whose capability depth is exhausted. The caller blocks
    them rather than hiding the failure behind an equal-depth model change.
    """
    exhausted: list[str] = []
    floors = state.setdefault("classFloor", {})
    for ticket_id_value, attempt in attempts.items():
        status = attempt.get("status")
        if kind == "capability" and status in {"pregate_failed", "review_failed"}:
            current = str(attempt.get("class") or "mechanical")
            ticket_body = attempt.get("ticket")
            writes = isinstance(ticket_body, dict) and bool(ticket_body.get("write"))
            deeper = next(
                (
                    name
                    for name, order in sorted(CLASS_ORDER.items(), key=lambda item: item[1])
                    if order > CLASS_ORDER.get(current, 0) and not (writes and name == "judgment")
                ),
                None,
            )
            if deeper is not None:
                floors[ticket_id_value] = max(
                    deeper,
                    str(floors.get(ticket_id_value) or deeper),
                    key=lambda name: CLASS_ORDER[name],
                )
            else:
                exhausted.append(ticket_id_value)
    return sorted(exhausted)


def latest_completed_lens_attempts(state: dict[str, Any], wave_id: str) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for assignment in state.get("lensAttempts", {}).values():
        if assignment.get("waveId") != wave_id or assignment.get("status") != "completed":
            continue
        lens = assignment.get("lens")
        if lens not in LENS_NAMES:
            continue
        previous = latest.get(lens)
        if previous is None or int(assignment.get("reviewAttemptOrdinal", 0)) > int(previous.get("reviewAttemptOrdinal", 0)):
            latest[lens] = assignment
    return latest


def ticket_failure_count(state: dict[str, Any], ticket_id: str) -> int:
    return int(state.get("qualityFailures", {}).get(ticket_id, 0)) + int(state.get("availabilityFailures", {}).get(ticket_id, 0))


def increment_ticket_failures(state: dict[str, Any], field: str, ticket_ids: list[str]) -> None:
    ledger = state.setdefault(field, {})
    for ticket_id in ticket_ids:
        ledger[ticket_id] = int(ledger.get(ticket_id, 0)) + 1


def schedule_lens_retry(
    cwd: Path,
    state: dict[str, Any],
    config: dict[str, Any],
    failed_lenses: set[str],
    reason: str,
) -> None:
    fail(not failed_lenses, "state_corrupt", "lens retry requires at least one failed lens")
    wave = current_wave(state)
    wave_id = require_text(wave.get("waveId"), "wave.waveId")
    set_retry_lens_names(state, failed_lenses)
    maximum = int(config.get("routing", {}).get("escalation", {}).get("max_attempts_per_subtask", 2))
    exhausted = [
        lens
        for lens in ordered_lens_names(failed_lenses)
        if lens_attempt_count(state, wave_id, lens) >= maximum
    ]
    if exhausted:
        rollback_attempt_patches(cwd, state, set(wave_attempt_ids(state, wave, "candidateAttemptIds")))
        state["phase"] = "blocked"
        state["blockedReason"] = f"lens retry limit reached for wave {wave_id}: {', '.join(exhausted)}"
    else:
        state["phase"] = "lens_prepare_pending"
        state["blockedReason"] = reason
    state["lastFailureKind"] = "availability"


def accepted_ticket_ids(state: dict[str, Any]) -> set[str]:
    return {
        str(attempt["ticketId"])
        for attempt in state.get("attempts", {}).values()
        if attempt.get("status") in {"accepted", "recorded"}
    }




def require_run_config(state: dict[str, Any], config: dict[str, Any]) -> None:
    observed = config_fingerprint(config)
    fail(
        state.get("configFingerprint") != observed,
        "config_changed",
        "effective Pocock config differs from the config that created this run",
        expected=state.get("configFingerprint"),
        observed=observed,
    )
def telemetry_shape_for_entry(config: dict[str, Any], entry: str) -> str | None:
    telemetry = config.get("telemetry", {})
    fail(not isinstance(telemetry, dict), "config_invalid", "telemetry config must be an object")
    entry_values = telemetry.get("entry_values")
    fail(
        not isinstance(entry_values, list) or any(not isinstance(value, str) or not value.strip() for value in entry_values),
        "config_invalid",
        "telemetry.entry_values must be a non-empty string array",
    )
    fail(entry not in entry_values, "config_invalid", f"telemetry.entry_values does not include runtime entry {entry!r}")
    mapping = telemetry.get("entry_requires_shape", {})
    fail(
        not isinstance(mapping, dict) or any(not isinstance(key, str) or not isinstance(value, str) for key, value in mapping.items()),
        "config_invalid",
        "telemetry.entry_requires_shape must map entries to shapes",
    )
    shape = mapping.get(entry)
    if shape is None:
        fail(entry == "sweep", "config_invalid", "telemetry.entry_requires_shape must define sweep")
        return None
    normalized = shape.strip()
    fail(not normalized, "config_invalid", f"telemetry shape for {entry!r} must be non-empty")
    known_shapes = config.get("shape", {}).get("values", {})
    fail(not isinstance(known_shapes, dict) or normalized not in known_shapes, "config_invalid", f"telemetry shape {normalized!r} is not defined by shape.values")
    return normalized


def next_actions(state: dict[str, Any]) -> list[str]:
    phase = state["phase"]
    if phase == "preparation":
        sequence = ["record_triage", "record_clarification", "record_plan", "approve_plan", "publish_tickets"]
        step = int(state.get("preparationStep", 0))
        return ([sequence[step]] if step < len(sequence) else []) + ["cancel"]
    mapping = {
        "frontier_admission": ["admit_frontier", "cancel"],
        "sweep_admission": ["admit_sweep", "cancel"],
        "ready": ["prepare", "cancel"],
        "producer_dispatch_pending": ["native_task", "cancel"],
        "producer_running": ["abandon_dispatch"],
        "pregate_pending": ["pregate", "cancel"],
        "lens_prepare_pending": ["prepare_lenses", "cancel"],
        "lens_dispatch_pending": ["native_task", "cancel"],
        "lens_running": ["abandon_dispatch"],
        "adjudication_pending": ["adjudicate", "cancel"],
        "repair_pending": ["retry", "cancel"],
        "blocked": ["cancel"],
        "synthesizing": ["complete", "cancel"],
        "completed": [],
        "cancelled": [],
    }
    if phase == "accepted":
        if not state.get("telemetryRecorded"):
            return ["accept", "cancel"]
        if state.get("frontierExhausted"):
            return ["begin_synthesis", "cancel"]
        return ["continue_wave", "cancel"]
    return mapping.get(phase, [])


def exact_nonnegative_integer(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def nullable_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def displayed_tokens(value: Any) -> int | str:
    tokens = exact_nonnegative_integer(value)
    return tokens if tokens is not None else "n/a"


def attempt_model_witness(attempt: dict[str, Any]) -> str:
    observed = nullable_text(attempt.get("observedModel"))
    declared = nullable_text(attempt.get("declaredModel"))
    fallback = attempt.get("modelFallback")
    if observed is None:
        return "DECLARED_ONLY"
    if fallback is True:
        return "OBSERVED_FALLBACK"
    if fallback is False and declared is not None and model_base(observed) == model_base(declared):
        return "OBSERVED_MATCH"
    return "OBSERVED_MISMATCH"


def compact_actor(attempt: dict[str, Any], kind: str) -> dict[str, Any]:
    ordinal = (
        attempt.get("attemptOrdinal", attempt.get("qualityAttempt"))
        if kind == "producer"
        else attempt.get("reviewAttemptOrdinal", attempt.get("attemptOrdinal"))
    )
    actor = {
        "dispatchName": nullable_text(attempt.get("dispatchName")),
        "attemptId": nullable_text(attempt.get("attemptId")),
        "ticketId": nullable_text(attempt.get("ticketId")),
        "role": nullable_text(attempt.get("role")),
        "lens": nullable_text(attempt.get("lens")),
        "attemptOrdinal": exact_nonnegative_integer(ordinal),
        "slotRole": nullable_text(attempt.get("slotRole")),
        "declaredModel": nullable_text(attempt.get("declaredModel")),
        "observedModel": nullable_text(attempt.get("observedModel")),
        "modelWitness": attempt_model_witness(attempt),
        "status": nullable_text(attempt.get("status")),
        "tokens": displayed_tokens(attempt.get("tokens")),
    }
    if kind == "lens":
        producer_attempt_ids = attempt.get("producerAttemptIds")
        actor["producerAttemptIds"] = list(producer_attempt_ids) if isinstance(producer_attempt_ids, list) else []
    return actor


def dispatch_card(state: dict[str, Any]) -> dict[str, Any] | None:
    pending = state.get("pendingDispatch")
    if not isinstance(pending, dict) or pending.get("kind") not in {"producer", "lenses"}:
        return None
    kind = "producer" if pending["kind"] == "producer" else "lens"
    collection = state.get("attempts") if kind == "producer" else state.get("lensAttempts")
    attempts = collection if isinstance(collection, dict) else {}
    attempt_ids = pending.get("attemptIds")
    actors = [
        compact_actor(attempt, kind)
        for attempt_id in (attempt_ids if isinstance(attempt_ids, list) else [])
        if isinstance((attempt := attempts.get(attempt_id)), dict)
    ]
    return {
        "dispatchId": nullable_text(pending.get("dispatchId")),
        "kind": pending["kind"],
        "status": nullable_text(pending.get("status")),
        "actors": actors,
    }


def card(state: dict[str, Any]) -> dict[str, Any]:
    result = {
        "runId": state["runId"],
        "revision": state["revision"],
        "stateHash": state["stateHash"],
        "entry": state["entry"],
        "phase": state["phase"],
        "nextActions": next_actions(state),
        "configFingerprint": state["configFingerprint"],
        "manifestFingerprint": state["manifestFingerprint"],
    }
    dispatch = dispatch_card(state)
    if dispatch is not None:
        result["dispatch"] = dispatch
    if state.get("phase") == "pregate_pending":
        requests = []
        wave = current_wave(state)
        for attempt_id in sorted(wave_attempt_ids(state, wave, "candidateAttemptIds")):
            attempt = state["attempts"][attempt_id]
            challenge = attempt.get("evidenceChallenge")
            if not isinstance(challenge, dict):
                continue
            completed = {
                record.get("stage")
                for record in state.get("evidence", [])
                if record.get("challengeToken") == challenge.get("token")
            }
            requests.append({**challenge, "completedStages": sorted(stage for stage in completed if isinstance(stage, str))})
        if requests:
            result["evidenceRequests"] = requests
    if state.get("entry") == "sweep" and isinstance(state.get("sweep"), dict):
        sweep = state["sweep"]
        result["ledgerHash"] = sweep.get("ledgerHash")
        result["dagHash"] = sweep.get("dagHash")
        result["acceptedTicketIds"] = list(sweep.get("acceptedTicketIds", []))
        result["remainingTicketIds"] = list(sweep.get("remainingTicketIds", []))
        result["readyTicketIds"] = list(sweep.get("readyTicketIds", []))
        result["blockedTicketIds"] = list(sweep.get("blockedTicketIds", []))
    if state.get("blockedReason"):
        result["blockedReason"] = state["blockedReason"]
    if state.get("budgetExhausted"):
        result["budgetExhausted"] = True
    for field in ("cancelReason", "supersededBy"):
        value = nullable_text(state.get(field))
        if value is not None:
            result[field] = value
    retry_tickets = state.get("retryTicketIds", [])
    if retry_tickets:
        result["retryTicketIds"] = list(retry_tickets)
    retry_lenses = state.get("retryLensNames", [])
    if retry_lenses:
        result["retryLensNames"] = list(retry_lenses)
    return result


def output(state: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"protocolVersion": PROTOCOL_VERSION, "card": card(state), **extra}


def metadata(config: dict[str, Any], cwd: Path) -> dict[str, Any]:
    validate_effective_omp_settings(cwd)
    return {"protocolVersion": PROTOCOL_VERSION, "configFingerprint": config_fingerprint(config), "omp": config["omp"]}


def validate_effective_omp_settings(cwd: Path) -> None:
    try:
        completed = subprocess.run(
            ["omp", "config", "list", "--json"],
            cwd=cwd,
            text=True,
            capture_output=True,
            check=False,
            timeout=OMP_CONFIG_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeFailure("omp_config_unavailable", f"OMP settings inventory exceeded {OMP_CONFIG_TIMEOUT_SECONDS}s") from exc
    fail(completed.returncode != 0, "omp_config_unavailable", completed.stderr.strip() or "cannot inspect effective OMP settings")
    try:
        settings = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeFailure("omp_config_unavailable", f"OMP returned invalid settings JSON: {exc}") from exc
    fail(not isinstance(settings, dict), "omp_config_unavailable", "OMP settings inventory is not an object")
    expected: dict[str, Any] = {
        "async.enabled": False,
        "retry.enabled": True,
        "task.batch": True,
        "task.enableEffort": True,
        # The concrete backend is host policy; the invariant is that isolation
        # remains enabled and every result returns as an unapplied patch.
        "task.isolation.apply": False,
        "task.isolation.merge": "patch",
        "task.maxRecursionDepth": 1,
        "retry.modelFallback": True,
    }
    mismatches = []
    for key, value in expected.items():
        observed = settings.get(key, {}).get("value") if isinstance(settings.get(key), dict) else None
        if observed != value:
            mismatches.append({"key": key, "expected": value, "observed": observed})
    isolation_mode = (
        settings.get("task.isolation.mode", {}).get("value")
        if isinstance(settings.get("task.isolation.mode"), dict)
        else None
    )
    if isolation_mode not in ISOLATED_OMP_MODES:
        mismatches.append(
            {
                "key": "task.isolation.mode",
                "expected": sorted(ISOLATED_OMP_MODES),
                "observed": isolation_mode,
            }
        )
    concurrency = settings.get("task.maxConcurrency", {}).get("value") if isinstance(settings.get("task.maxConcurrency"), dict) else None
    if not isinstance(concurrency, int) or isinstance(concurrency, bool) or not 1 <= concurrency <= 6:
        mismatches.append({"key": "task.maxConcurrency", "expected": "integer 1..6", "observed": concurrency})
    max_runtime = settings.get("task.maxRuntimeMs", {}).get("value") if isinstance(settings.get("task.maxRuntimeMs"), dict) else None
    if not isinstance(max_runtime, int) or isinstance(max_runtime, bool) or max_runtime <= 0:
        mismatches.append({"key": "task.maxRuntimeMs", "expected": "positive integer", "observed": max_runtime})
    fail(
        bool(mismatches),
        "omp_config_incompatible",
        "effective OMP task settings do not satisfy Pocock invariants; run install.sh --configure-omp explicitly or apply the listed values to the current project",
        mismatches=mismatches,
    )


def discover_omp_nested_repositories(repo_root: Path, submodule_paths: set[str]) -> list[Path]:
    """Mirror OMP's non-submodule nested-repository discovery."""
    nested: list[Path] = []

    def walk(directory: Path) -> None:
        try:
            entries = list(os.scandir(directory))
        except OSError:
            return
        for entry in entries:
            if entry.name in {"node_modules", ".git"} or not entry.is_dir(follow_symlinks=False):
                continue
            path = Path(entry.path)
            relative = path.relative_to(repo_root).as_posix()
            if os.access(path / ".git", os.F_OK) and relative not in submodule_paths:
                nested.append(path)
                continue
            walk(path)

    walk(repo_root)
    return sorted(nested)


def validate_omp_isolation_baseline(cwd: Path) -> None:
    """Reject repository shapes that OMP cannot capture as an isolation baseline."""
    try:
        root_result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeFailure(
            "omp_isolation_baseline_invalid",
            "cannot inspect the Git repositories required by OMP task isolation",
            repositories=[{"path": ".", "reason": f"git rev-parse exceeded {GIT_TIMEOUT_SECONDS}s"}],
        ) from exc
    except OSError as exc:
        raise RuntimeFailure(
            "omp_isolation_baseline_invalid",
            "cannot inspect the Git repositories required by OMP task isolation",
            repositories=[{"path": ".", "reason": str(exc)}],
        ) from exc
    fail(
        root_result.returncode != 0 or not root_result.stdout.strip(),
        "omp_isolation_baseline_invalid",
        "OMP task isolation requires a Git checkout with a resolvable root",
        repositories=[{"path": ".", "reason": "Git repository root is not resolvable"}],
    )
    repo_root = Path(root_result.stdout.strip()).resolve()
    try:
        submodules = subprocess.run(
            ["git", "submodule", "--quiet", "foreach", "--recursive", "echo $sm_path"],
            cwd=repo_root,
            check=False,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeFailure(
            "omp_isolation_baseline_invalid",
            "OMP cannot enumerate the repositories required for task isolation",
            repositories=[{"path": ".", "reason": f"git submodule enumeration exceeded {GIT_TIMEOUT_SECONDS}s"}],
        ) from exc
    fail(
        submodules.returncode != 0,
        "omp_isolation_baseline_invalid",
        "OMP cannot enumerate the repositories required for task isolation",
        repositories=[{"path": ".", "reason": "initialized Git submodules are not enumerable"}],
    )
    submodule_paths = {line.strip() for line in submodules.stdout.splitlines() if line.strip()}
    repositories = [repo_root, *discover_omp_nested_repositories(repo_root, submodule_paths)]
    broken: list[dict[str, str]] = []
    for repository in repositories:
        try:
            head = subprocess.run(
                ["git", "rev-parse", "--verify", "HEAD^{commit}"],
                cwd=repository,
                check=False,
                capture_output=True,
                text=True,
                timeout=GIT_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            relative = "." if repository == repo_root else repository.relative_to(repo_root).as_posix()
            broken.append({"path": relative, "reason": f"git rev-parse exceeded {GIT_TIMEOUT_SECONDS}s"})
            continue
        if head.returncode != 0 or not head.stdout.strip():
            relative = "." if repository == repo_root else repository.relative_to(repo_root).as_posix()
            broken.append({"path": relative, "reason": "HEAD does not resolve to a commit"})
    fail(
        bool(broken),
        "omp_isolation_baseline_invalid",
        "OMP cannot capture task isolation patches from repositories without a resolvable HEAD",
        repositories=broken,
    )


def require_mapping(value: Any, field: str) -> dict[str, Any]:
    fail(not isinstance(value, dict), "invalid_request", f"{field} must be an object")
    return value


def require_text(value: Any, field: str) -> str:
    fail(not isinstance(value, str) or not value.strip(), "invalid_request", f"{field} must be a non-empty string")
    return value.strip()


def command_start(cwd: Path, explicit_state_dir: str | None, request: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    entry = request.get("entry")
    fail(entry not in {"full", "frontier", "sweep"}, "invalid_entry", "entry must be full, frontier, or sweep")
    telemetry_shape_for_entry(config, entry)
    objective = require_text(request.get("objective"), "objective")
    session_id = require_text(request.get("sessionId"), "sessionId")
    requested_manifest_fingerprint = require_text(request.get("manifestFingerprint"), "manifestFingerprint")
    fail("lead" in request, "invalid_request", "lead is no longer accepted")
    models = require_mapping(request.get("models"), "models")
    fail(not models, "model_manifest_invalid", "OMP supplied no resolved Pocock role models")

    omp_slots = config["omp"].get("slots", {})
    fail(set(models) != set(omp_slots), "model_manifest_invalid", "resolved slot set differs from config", expected=sorted(omp_slots), observed=sorted(models))
    for slot, witness in models.items():
        fail(not isinstance(witness, dict), "model_manifest_invalid", f"slot {slot} witness is not an object")
        for field in ("role", "provider", "resolvedModel"):
            require_text(witness.get(field), f"models.{slot}.{field}")
    manifest_fingerprint = trusted_manifest_fingerprint(cwd, config)
    fail(
        requested_manifest_fingerprint != manifest_fingerprint,
        "manifest_witness_mismatch",
        "adapter-supplied Pocock manifest witness does not match the core-observed manifests",
        expected=manifest_fingerprint,
        observed=requested_manifest_fingerprint,
    )


    with with_lock(start_lock_path(cwd, explicit_state_dir)):
        recover_replacement_transactions(cwd, explicit_state_dir)
        run_id = f"pocock-{int(time.time())}-{uuid.uuid4().hex[:12]}"
        state_path, lock_path = state_paths(cwd, explicit_state_dir, run_id)
        state: dict[str, Any] = {
            "schemaVersion": SCHEMA_VERSION,
            "runId": run_id,
            "revision": 0,
            "previousStateHash": None,
            "entry": entry,
            "phase": "preparation" if entry == "full" else "sweep_admission" if entry == "sweep" else "frontier_admission",
            "objective": objective,
            "cwd": str(cwd.resolve()),
            "sessionId": session_id,
            "models": models,
            "manifestFingerprint": manifest_fingerprint,
            "configFingerprint": config_fingerprint(config),
            "runtimeFingerprint": runtime_fingerprint(),
            "preparationStep": 0,
            "preparation": {},
            "provenance": None,
            "sweep": None,
            "waves": [],
            "currentWave": None,
            "qualityFailures": {},
            "availabilityFailures": {},
            "classFloor": {},
            "retryTicketIds": [],
            "retryLensNames": [],
            "frontierExhausted": False,
            "evidenceChallenges": {},
            "attempts": {},
            "lensAttempts": {},
            "evidence": [],
            "tokensSpent": 0,
            "projectedTokens": 0,
            "telemetryRecorded": False,
            "budgetExhausted": False,
            "blockedReason": None,
        }
        active = require_start_slot(cwd, explicit_state_dir, manifest_fingerprint)
        if active is None:
            with with_lock(lock_path):
                fail(state_path.exists(), "run_collision", "generated run id already exists")
                seal_state(state, state_path)
                write_state(state_path, state)
        else:
            state = create_replacement_transaction(
                cwd,
                explicit_state_dir,
                active,
                state,
                state_path,
            )
    return output(state)


def validate_provenance(payload: Any, *, allow_attestation: bool) -> dict[str, Any]:
    provenance = require_mapping(payload, "payload")
    unavailable = provenance.get("trackerUnavailable") is True or provenance.get("localTracker") is True
    attestation = provenance.get("ownerAttestation")
    if allow_attestation and unavailable:
        require_text(attestation, "payload.ownerAttestation")
        require_text(provenance.get("unavailableReason"), "payload.unavailableReason")
        return provenance
    fail(provenance.get("durable") is not True and provenance.get("published") is not True, "provenance_missing", "tracker provenance is not marked durable and published")
    for field in ("tracker", "spec", "approval"):
        require_text(provenance.get(field), f"payload.{field}")
    for field in ("tickets", "dependencies"):
        value = provenance.get(field)
        fail(not isinstance(value, (list, dict)) or not value, "provenance_missing", f"payload.{field} must identify published records")
    return provenance


def command_transition(cwd: Path, explicit_state_dir: str | None, request: dict[str, Any]) -> dict[str, Any]:
    action = require_text(request.get("action"), "action")
    payload = request.get("payload")

    def apply(state: dict[str, Any]) -> None:
        phase = state["phase"]
        if action == "cancel":
            fail(phase in TERMINAL_PHASES, "illegal_transition", f"cannot cancel terminal phase {phase}")
            if state.get("entry") == "sweep" and phase != "sweep_admission":
                sweep = require_sweep_integrity(state)
                accepted = set(sweep["acceptedTicketIds"])
                rollback_ids = {
                    attempt_id
                    for attempt_id, attempt in state.get("attempts", {}).items()
                    if isinstance(attempt.get("appliedPatch"), dict) and attempt.get("ticketId") not in accepted
                }
                if payload is not None:
                    cancellation = require_mapping(payload, "payload")
                    fail(set(cancellation) != {"reason"}, "invalid_request", "sweep cancellation payload must contain only reason")
                    state["cancelReason"] = require_text(cancellation.get("reason"), "payload.reason")
            else:
                rollback_ids = {
                    attempt_id
                    for attempt_id, attempt in state.get("attempts", {}).items()
                    if isinstance(attempt.get("appliedPatch"), dict)
                    and attempt.get("status") not in {"accepted", "recorded"}
                }
            rollback_orphaned_patch_journals(cwd, explicit_state_dir, state)
            rollback_attempt_patches(cwd, state, rollback_ids)
            state["phase"] = "cancelled"
            state["blockedReason"] = None
            return

        if phase in {"producer_running", "lens_running"} and action == "abandon_dispatch":
            abandonment = require_mapping(payload, "payload")
            fail(
                abandonment.get("confirmedLostSettlement") is not True,
                "dispatch_still_ambiguous",
                "abandon_dispatch requires confirmedLostSettlement=true",
            )
            reason = require_text(abandonment.get("reason"), "payload.reason")
            pending = state.get("pendingDispatch")
            fail(
                not isinstance(pending, dict) or pending.get("status") != "running",
                "dispatch_invalid",
                "no running sealed dispatch can be abandoned",
            )
            kind = pending.get("kind")
            collection = state["attempts"] if kind == "producer" else state["lensAttempts"]
            if kind == "producer":
                rollback_orphaned_patch_journals(
                    cwd,
                    explicit_state_dir,
                    state,
                    {require_text(pending.get("dispatchId"), "pendingDispatch.dispatchId")},
                )
            ticket_ids: list[str] = []
            failed_lenses: set[str] = set()
            for attempt_id in pending.get("attemptIds", []):
                attempt = collection.get(attempt_id)
                fail(not isinstance(attempt, dict) or attempt.get("status") != "running", "dispatch_invalid", f"attempt {attempt_id} is not running")
                attempt["status"] = "availability_failed"
                attempt["failureCode"] = "settlement_abandoned"
                attempt["failureReason"] = reason
                attempt["abandonReason"] = reason
                if kind == "producer":
                    ticket_ids.append(str(attempt["ticketId"]))
                else:
                    failed_lenses.add(str(attempt.get("lens")))
            state["projectedTokens"] = max(0, int(state.get("projectedTokens", 0)) - int(pending.get("projection", 0)))
            pending["status"] = "abandoned"
            pending["abandonedAt"] = dt.datetime.now(dt.timezone.utc).isoformat()
            pending["abandonReason"] = reason
            failure_reason = f"sealed task settlement was explicitly abandoned: {reason}"
            if kind == "lenses":
                schedule_lens_retry(cwd, state, load_config(), failed_lenses, failure_reason)
            else:
                increment_ticket_failures(state, "availabilityFailures", ticket_ids)
                state["phase"] = "repair_pending"
                state["blockedReason"] = failure_reason
                state["lastFailureKind"] = "availability"
                set_retry_ticket_ids(state, set(ticket_ids))
            return

        if phase == "preparation":
            sequence = ["record_triage", "record_clarification", "record_plan", "approve_plan", "publish_tickets"]
            step = int(state.get("preparationStep", 0))
            expected = sequence[step] if step < len(sequence) else None
            fail(action != expected, "illegal_transition", f"preparation expects {expected}, not {action}")
            fail(not isinstance(payload, dict) or not payload, "invalid_request", f"{action} requires a non-empty factual payload")
            if action == "approve_plan":
                approval = payload if isinstance(payload, dict) else {}
                approved = approval.get("approved") is True or str(approval.get("decision", "")).lower() in {"approve", "approved"}
                fail(not approved, "approval_missing", "approve_plan requires explicit owner approval")
            if action == "publish_tickets":
                provenance = validate_provenance(payload, allow_attestation=False)
                state["provenance"] = provenance
                state["phase"] = "ready"
            state["preparation"][action] = payload
            state["preparationStep"] = step + 1
            return

        if phase == "frontier_admission" and action == "admit_frontier":
            provenance = validate_provenance(payload, allow_attestation=True)
            state["provenance"] = provenance
            state["phase"] = "ready"
            return

        if phase == "sweep_admission" and action == "admit_sweep":
            state["sweep"] = normalize_sweep_admission(cwd, payload)
            state["frontierExhausted"] = False
            state["authorizedNextTickets"] = None
            state["blockedReason"] = None
            state["phase"] = "ready"
            return

        if phase == "repair_pending" and action == "retry":
            diagnosis = payload if isinstance(payload, dict) else {}
            # A bare `retry` must not silently pick the one diagnosis that
            # changes nothing. The core already recorded why the wave failed,
            # so that recorded kind is the default.
            kind = str(diagnosis.get("diagnosis") or state.get("lastFailureKind") or "effort").lower()
            fail(kind not in {"effort", "capability", "availability", "clarification"}, "invalid_diagnosis", "retry diagnosis must be effort, capability, availability, or clarification")
            max_attempts = int(load_config().get("routing", {}).get("escalation", {}).get("max_attempts_per_subtask", 2))
            attempted_tickets = retry_ticket_ids(state)
            fail(not attempted_tickets, "retry_invalid", "retry has no failed tickets")
            if state.get("entry") == "sweep":
                sweep = require_sweep_integrity(state)
                fail(
                    not attempted_tickets.issubset(set(sweep["remainingTicketIds"])),
                    "sweep_ledger_drift",
                    "sweep retry names a ticket outside the remaining sealed ledger",
                )
                fail(
                    not attempted_tickets.issubset(set(sweep["readyTicketIds"])),
                    "sweep_scheduler_invalid",
                    "sweep retry names a ticket that is not ready in the sealed DAG",
                )
            exhausted = sorted(ticket for ticket in attempted_tickets if ticket_failure_count(state, ticket) >= max_attempts)
            if exhausted:
                state["phase"] = "blocked"
                state["blockedReason"] = f"retry limit reached for: {', '.join(exhausted)}"
                return
            attempted_again = {
                str(attempt["ticketId"]): attempt
                for attempt in state.get("attempts", {}).values()
                if attempt.get("ticketId") in attempted_tickets
            }
            exhausted_depth = route_ticket_retry(state, attempted_again, kind)
            if exhausted_depth:
                state["phase"] = "blocked"
                state["blockedReason"] = (
                    "capability escalation is exhausted at the deepest class for: "
                    f"{', '.join(exhausted_depth)}; decompose them instead"
                )
                state["lastRetry"] = {"diagnosis": kind, "payload": diagnosis}
                return
            state["lastRetry"] = {"diagnosis": kind, "payload": diagnosis}
            state["authorizedNextTickets"] = sorted(attempted_tickets)
            state["pendingDispatch"] = None
            state["blockedReason"] = None
            state["phase"] = "ready"
            return

        if phase == "accepted" and state.get("telemetryRecorded") and action == "continue_wave":
            if state.get("entry") == "sweep":
                fail(payload is not None, "sweep_payload_forbidden", "sweep continue_wave computes progress from the sealed ledger and accepts no payload")
                sweep = require_sweep_integrity(state)
                remaining_ids = sweep["remainingTicketIds"]
                ready_ids = sweep["readyTicketIds"]
                fail(not remaining_ids, "sweep_exhausted", "sweep has no remaining tickets; begin synthesis instead")
                if state.get("budgetExhausted"):
                    state["phase"] = "blocked"
                    state["blockedReason"] = f"token budget exhausted with remaining sealed tickets: {', '.join(remaining_ids)}"
                    return
                if not ready_ids:
                    state["phase"] = "blocked"
                    state["blockedReason"] = f"sealed DAG has no ready tickets: {', '.join(remaining_ids)}"
                    return
                max_attempts = int(
                    load_config().get("routing", {}).get("escalation", {}).get(
                        "max_attempts_per_subtask",
                        2,
                    )
                )
                exhausted_ready = sorted(
                    ticket_id_value
                    for ticket_id_value in ready_ids
                    if ticket_failure_count(state, ticket_id_value) >= max_attempts
                )
                if exhausted_ready:
                    state["phase"] = "blocked"
                    state["blockedReason"] = f"retry limit reached for: {', '.join(exhausted_ready)}"
                    return
                state["authorizedNextTickets"] = None
                state["pendingDispatch"] = None
                state["pregate"] = {}
                state["adjudication"] = {}
                state["telemetryRecorded"] = False
                state["telemetryEvents"] = []
                state["blockedReason"] = None
                state["phase"] = "ready"
                return

            continuation = require_mapping(payload, "payload")

            def ticket_ids(field: str) -> list[str]:
                raw = continuation.get(field)
                fail(
                    not isinstance(raw, list)
                    or any(not isinstance(item, str) or not item.strip() for item in raw),
                    "invalid_request",
                    f"continue_wave requires payload.{field} as a string array",
                )
                normalized = [item.strip() for item in raw]
                fail(len(normalized) != len(set(normalized)), "invalid_request", f"payload.{field} must be unique")
                return normalized

            remaining_ids = ticket_ids("remainingTicketIds")
            next_ids = ticket_ids("nextTicketIds")
            blocked_ids = ticket_ids("blockedTicketIds")
            evidence = require_text(continuation.get("evidence"), "payload.evidence")
            fail(set(remaining_ids) != set(next_ids) | set(blocked_ids), "invalid_request", "remainingTicketIds must equal the union of nextTicketIds and blockedTicketIds")
            fail(set(next_ids) & set(blocked_ids), "invalid_request", "nextTicketIds and blockedTicketIds must be disjoint")
            overlap = accepted_ticket_ids(state) & set(remaining_ids)
            fail(bool(overlap), "ticket_already_accepted", f"accepted tickets cannot remain in the tracker frontier: {', '.join(sorted(overlap))}")
            observation = {
                "afterWave": state.get("currentWave"),
                "remainingTicketIds": remaining_ids,
                "nextTicketIds": next_ids,
                "blockedTicketIds": blocked_ids,
                "evidence": evidence,
            }
            state.setdefault("waveContinuations", []).append(observation)
            state["frontierObservation"] = observation
            if not remaining_ids:
                state["frontierExhausted"] = True
                state["authorizedNextTickets"] = None
                state["blockedReason"] = None
                return
            if not next_ids:
                state["phase"] = "blocked"
                state["blockedReason"] = f"tracker frontier has only blocked remaining tickets: {', '.join(blocked_ids)}"
                return
            if state.get("budgetExhausted"):
                state["phase"] = "blocked"
                state["blockedReason"] = f"token budget exhausted with remaining tickets: {', '.join(remaining_ids)}"
                return
            state["authorizedNextTickets"] = next_ids
            state["pendingDispatch"] = None
            state["pregate"] = {}
            state["adjudication"] = {}
            state["telemetryRecorded"] = False
            state["telemetryEvents"] = []
            state["blockedReason"] = None
            state["phase"] = "ready"
            return

        if phase == "accepted" and state.get("telemetryRecorded") and action == "begin_synthesis":
            if state.get("entry") == "sweep":
                fail(payload is not None, "sweep_payload_forbidden", "sweep synthesis reads only the sealed ledger")
                sweep = require_sweep_integrity(state)
                fail(bool(sweep["remainingTicketIds"]), "sweep_not_exhausted", "synthesis requires an empty runtime-owned sweep remaining set")
            else:
                fail(not state.get("frontierExhausted"), "frontier_not_exhausted", "synthesis requires an explicit empty tracker frontier observation")
            state["phase"] = "synthesizing"
            return

        if phase == "synthesizing" and action == "complete":
            state["phase"] = "completed"
            return

        raise RuntimeFailure("illegal_transition", f"action {action} is not legal in phase {phase}")

    state, _ = mutate(cwd, explicit_state_dir, request, apply)
    return output(state)


def text_for_ticket(ticket: dict[str, Any]) -> str:
    sections = [f"{field}:\n{ticket[field]}" for field in TICKET_FIELDS]
    sections.append(f"WRITABLE_PATHS:\n{canonical(ticket['writablePaths'])}")
    sections.append(f"VERIFICATION:\n{canonical(ticket['verification'])}")
    if ticket["ui_live"]:
        sections.append(f"UI_EVIDENCE:\n{canonical(ticket['ui_evidence'])}")
    sections.append("If the instructions do not match what you find or require an unplanned judgment call, return NEEDS_CLARIFICATION with evidence. Do not guess.")
    return "\n\n".join(sections)


def producer_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["status", "summary", "evidence", "changedFiles", "verification"],
        "properties": {
            "status": {"type": "string", "enum": ["COMPLETED", "NEEDS_CLARIFICATION", "BLOCKED"]},
            "summary": {"type": "string"},
            "evidence": {"type": "array", "items": {"type": "string"}},
            "changedFiles": {"type": "array", "items": {"type": "string"}},
            "verification": {"type": "array", "items": {"type": "string"}},
        },
    }


def lens_schema() -> dict[str, Any]:
    finding = {
        "type": "object",
        "additionalProperties": False,
        "required": ["scope", "severity", "blocking", "evidence"],
        "properties": {
            "scope": {"type": "string", "enum": ["introduced", "pre-existing", "decision-challenge"]},
            "severity": {"type": "string", "enum": ["critical", "high", "medium", "low", "note"]},
            "blocking": {"type": "boolean"},
            "evidence": {"type": "string"},
        },
    }
    report = {
        "type": "object",
        "additionalProperties": False,
        "required": ["attemptId", "summary", "findings", "verdict"],
        "properties": {
            "attemptId": {"type": "string"},
            "summary": {"type": "string"},
            "findings": {"type": "array", "items": finding},
            "verdict": {"type": "string", "enum": ["PASS", "FAIL", "NO_VERDICT"]},
        },
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["lens", "summary", "reports"],
        "properties": {
            "lens": {"type": "string", "enum": list(LENS_NAMES)},
            "summary": {"type": "string"},
            "reports": {"type": "array", "items": report},
        },
    }


def ticket_id(ticket: dict[str, Any], index: int) -> str:
    value = ticket.get("ticketId", ticket.get("id"))
    if isinstance(value, (str, int)) and str(value).strip():
        return str(value).strip()
    return f"ticket-{index + 1}-{digest(ticket.get('OBJECTIVE'))[:8]}"

def normalize_repo_path(value: Any, label: str, *, directory_allowed: bool) -> str:
    text = require_text(value, label)
    fail("\\" in text or "\x00" in text or any(ord(character) < 32 for character in text), "ticket_invalid", f"{label} must use a POSIX repository path without control characters")
    directory = text.endswith("/")
    fail(directory and not directory_allowed, "ticket_invalid", f"{label} must name a file")
    raw = text[:-1] if directory else text
    path = PurePosixPath(raw)
    normalized = path.as_posix()
    fail(
        path.is_absolute()
        or normalized in {"", "."}
        or any(part in {"", ".", ".."} for part in path.parts)
        or raw != normalized,
        "ticket_invalid",
        f"{label} must be a normalized repository-relative path",
    )
    fail(
        any(normalized == prefix or normalized.startswith(f"{prefix}/") for prefix in PATCH_FORBIDDEN_PREFIXES),
        "ticket_invalid",
        f"{label} cannot name repository metadata",
    )
    return f"{normalized}/" if directory else normalized

def repo_path_uses_symlink(cwd: Path, value: str) -> bool:
    current = cwd.resolve()
    for part in PurePosixPath(value.rstrip("/")).parts:
        current = current / part
        if current.is_symlink():
            return True
    return False


def normalize_verification_item(value: Any, label: str) -> dict[str, Any]:
    fail(not isinstance(value, dict), "ticket_invalid", f"{label} must be an object")
    fail(
        not set(value).issubset({"argv", "cwd", "timeoutSeconds"}),
        "ticket_invalid",
        f"{label} has unknown fields",
    )
    argv = value.get("argv")
    fail(
        not isinstance(argv, list)
        or not argv
        or any(not isinstance(argument, str) or not argument or "\x00" in argument for argument in argv),
        "ticket_invalid",
        f"{label}.argv must be a non-empty string array",
    )
    command_cwd = value.get("cwd", ".")
    fail(not isinstance(command_cwd, str) or not command_cwd.strip(), "ticket_invalid", f"{label}.cwd must be a string")
    timeout = value.get("timeoutSeconds")
    fail(
        timeout is not None and (not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0),
        "ticket_invalid",
        f"{label}.timeoutSeconds must be a positive integer",
    )
    return {"argv": list(argv), "cwd": command_cwd.strip(), "timeoutSeconds": timeout}


def writable_path_allows(allowed: str, observed: str) -> bool:
    return observed.startswith(allowed) if allowed.endswith("/") else observed == allowed


def writable_policies_overlap(left: list[str], right: list[str]) -> bool:
    return any(writable_path_allows(a, b.rstrip("/")) or writable_path_allows(b, a.rstrip("/")) for a in left for b in right)


def normalize_ui_evidence(value: Any, label: str) -> dict[str, str]:
    fail(not isinstance(value, dict) or set(value) != {"target", "criterion"}, "ticket_invalid", f"{label} must contain exactly target and criterion")
    return {
        "target": require_text(value.get("target"), f"{label}.target"),
        "criterion": require_text(value.get("criterion"), f"{label}.criterion"),
    }


def normalize_ticket(raw: Any, index: int) -> dict[str, Any]:
    fail(not isinstance(raw, dict), "ticket_invalid", f"ticket {index + 1} must be an object")
    ticket = copy.deepcopy(raw)
    for field in TICKET_FIELDS:
        fail(field not in ticket, "ticket_invalid", f"ticket {index + 1} lacks {field}")
        value = ticket[field]
        if isinstance(value, (list, dict)):
            value = json.dumps(value, ensure_ascii=False, sort_keys=True)
        ticket[field] = require_text(value, f"ticket {index + 1}.{field}")
    fail(
        INCOMPLETE_TRACKER_REFERENCE_RE.search(ticket["INPUTS"]) is not None,
        "incomplete_tracker_reference",
        f"ticket {index + 1}.INPUTS contains an incomplete tracker reference; use a full URL, issue://owner/repo/N, or repository path",
    )
    fail(not isinstance(ticket.get("signals"), list) or not ticket["signals"] or not all(isinstance(v, str) and v.strip() for v in ticket["signals"]), "ticket_invalid", f"ticket {index + 1}.signals must be a non-empty string array")
    fail(not isinstance(ticket.get("write"), bool), "ticket_invalid", f"ticket {index + 1}.write must be boolean")
    verification = ticket.get("verification")
    fail(not isinstance(verification, list), "ticket_invalid", f"ticket {index + 1}.verification must be an array")
    ticket["verification"] = [
        normalize_verification_item(item, f"ticket {index + 1}.verification[{command_index}]")
        for command_index, item in enumerate(verification)
    ]
    writable_paths = ticket.get("writablePaths")
    fail(not isinstance(writable_paths, list), "ticket_invalid", f"ticket {index + 1}.writablePaths must be an array")
    ticket["writablePaths"] = [
        normalize_repo_path(path, f"ticket {index + 1}.writablePaths[{path_index}]", directory_allowed=True)
        for path_index, path in enumerate(writable_paths)
    ]
    fail(len(ticket["writablePaths"]) != len(set(ticket["writablePaths"])), "ticket_invalid", f"ticket {index + 1}.writablePaths must be unique")
    fail(ticket["write"] and not ticket["writablePaths"], "ticket_invalid", f"ticket {index + 1} writer requires writablePaths")
    fail(not ticket["write"] and bool(ticket["writablePaths"]), "ticket_invalid", f"ticket {index + 1} read-only work cannot declare writablePaths")
    ticket["ui_live"] = ticket.get("ui_live") is True
    if ticket["ui_live"]:
        ticket["ui_evidence"] = normalize_ui_evidence(ticket.get("ui_evidence"), f"ticket {index + 1}.ui_evidence")
    else:
        fail(ticket.get("ui_evidence") is not None, "ticket_invalid", f"ticket {index + 1}.ui_evidence requires ui_live=true")
        ticket["ui_evidence"] = None
    ticket["ticketId"] = ticket_id(ticket, index)
    return ticket


def normalize_sweep_witness(value: Any) -> dict[str, Any]:
    witness = require_mapping(value, "payload.witness")
    expected = {"closed", "acceptancePredecided", "integration", "evidence"}
    fail(set(witness) != expected, "sweep_witness_invalid", "payload.witness has unknown or missing fields")
    fail(witness["closed"] is not True, "sweep_witness_invalid", "payload.witness.closed must be true")
    fail(
        witness["acceptancePredecided"] is not True,
        "sweep_witness_invalid",
        "payload.witness.acceptancePredecided must be true",
    )
    integration = witness["integration"]
    fail(
        not isinstance(integration, str) or integration not in SWEEP_INTEGRATIONS,
        "sweep_witness_invalid",
        f"payload.witness.integration must be one of {sorted(SWEEP_INTEGRATIONS)}",
    )
    evidence = witness["evidence"]
    fail(
        not isinstance(evidence, str) or not evidence.strip(),
        "sweep_witness_invalid",
        "payload.witness.evidence must be a non-empty factual string",
    )
    return {
        "closed": True,
        "acceptancePredecided": True,
        "integration": integration,
        "evidence": evidence.strip(),
    }


def normalize_sweep_dependencies(value: Any, label: str) -> list[str]:
    fail(not isinstance(value, list), "dag_invalid", f"{label} must be a string array")
    dependencies = []
    for dependency in value:
        fail(
            not isinstance(dependency, str) or not dependency.strip(),
            "dag_invalid",
            f"{label} must contain only non-empty ticket ids",
        )
        dependencies.append(dependency.strip())
    fail(len(dependencies) != len(set(dependencies)), "dag_invalid", f"{label} must not contain duplicate ticket ids")
    return sorted(dependencies)


def sweep_topological_order(dependencies: dict[str, list[str]]) -> list[str]:
    ticket_ids = set(dependencies)
    for ticket_id_value, prerequisites in dependencies.items():
        fail(
            any(prerequisite not in ticket_ids for prerequisite in prerequisites),
            "dag_invalid",
            f"ticket {ticket_id_value} depends on a ticket outside the sealed ledger",
        )
        fail(ticket_id_value in prerequisites, "dag_invalid", f"ticket {ticket_id_value} cannot depend on itself")

    unresolved = {ticket_id_value: set(prerequisites) for ticket_id_value, prerequisites in dependencies.items()}
    dependents = {ticket_id_value: [] for ticket_id_value in dependencies}
    for ticket_id_value, prerequisites in dependencies.items():
        for prerequisite in prerequisites:
            dependents[prerequisite].append(ticket_id_value)
    ready = [ticket_id_value for ticket_id_value, prerequisites in unresolved.items() if not prerequisites]
    heapq.heapify(ready)
    order = []
    while ready:
        # heapq replaces the old pop(0)+sort pair: deterministic lexicographic
        # order, O(log T) per step instead of O(T log T) re-sort.
        ticket_id_value = heapq.heappop(ready)
        order.append(ticket_id_value)
        for dependent in dependents[ticket_id_value]:
            unresolved[dependent].remove(ticket_id_value)
            if not unresolved[dependent]:
                heapq.heappush(ready, dependent)
    fail(
        len(order) != len(dependencies),
        "dag_invalid",
        "sweep dependsOn graph contains a cycle",
    )
    return order


def sweep_has_incomparable_pair(dependencies: dict[str, list[str]], _order: list[str]) -> bool:
    """Return whether the sealed DAG contains mutually incomparable tickets.

    Kahn layers answer this in O(T+E): tickets in one layer cannot descend
    from each other, and a DAG whose every layer has width one is a chain.
    """
    unresolved = {ticket_id_value: set(prerequisites) for ticket_id_value, prerequisites in dependencies.items()}
    dependents = {ticket_id_value: [] for ticket_id_value in dependencies}
    for ticket_id_value, prerequisites in dependencies.items():
        for prerequisite in prerequisites:
            dependents[prerequisite].append(ticket_id_value)
    current = [ticket_id_value for ticket_id_value, prerequisites in unresolved.items() if not prerequisites]
    while current:
        if len(current) >= 2:
            return True
        advanced: list[str] = []
        for ticket_id_value in current:
            for dependent in dependents[ticket_id_value]:
                unresolved[dependent].remove(ticket_id_value)
                if not unresolved[dependent]:
                    advanced.append(dependent)
        current = advanced
    return False


def validate_sweep_writer_contract(cwd: Path | None, ledger: dict[str, dict[str, Any]]) -> None:
    writers = []
    for ticket_id_value, ticket in ledger.items():
        fail(not isinstance(ticket.get("write"), bool), "sweep_ledger_drift", f"ticket {ticket_id_value} has an invalid write declaration")
        verification = ticket.get("verification")
        writable_paths = ticket.get("writablePaths")
        fail(not isinstance(verification, list), "sweep_ledger_drift", f"ticket {ticket_id_value} has an invalid verification declaration")
        fail(not isinstance(writable_paths, list), "sweep_ledger_drift", f"ticket {ticket_id_value} has invalid writablePaths")
        if ticket["write"]:
            fail(not verification, "verification_missing", f"writer ticket {ticket_id_value} requires deterministic verification")
            fail(not writable_paths, "ticket_invalid", f"writer ticket {ticket_id_value} requires writablePaths")
            if cwd is not None:
                for writable_path in writable_paths:
                    fail(
                        repo_path_uses_symlink(cwd, writable_path),
                        "ticket_invalid",
                        f"ticket {ticket_id_value} writablePaths traverses a symbolic link: {writable_path}",
                    )
            writers.append((ticket_id_value, writable_paths))

    for left_index, (left_id, left_paths) in enumerate(writers):
        for right_id, right_paths in writers[left_index + 1:]:
            fail(
                writable_policies_overlap(left_paths, right_paths),
                "ticket_overlap",
                f"writer tickets {left_id} and {right_id} have overlapping writablePaths",
            )


def scheduled_sweep_sets(
    ledger: dict[str, dict[str, Any]],
    dependencies: dict[str, list[str]],
    accepted: set[str],
) -> tuple[list[str], list[str], list[str]]:
    remaining = sorted(set(ledger) - accepted)
    ready = sorted(
        ticket_id_value
        for ticket_id_value in remaining
        if set(dependencies[ticket_id_value]).issubset(accepted)
    )
    ui_ready = [ticket_id_value for ticket_id_value in ready if ledger[ticket_id_value].get("ui_live") is True]
    current = ui_ready[:1] if ui_ready else ready
    blocked = sorted(set(remaining) - set(current))
    return remaining, current, blocked


def set_sweep_progress(sweep: dict[str, Any]) -> None:
    accepted = set(sweep["acceptedTicketIds"])
    remaining, ready, blocked = scheduled_sweep_sets(sweep["ledger"], sweep["dependencies"], accepted)
    sweep["remainingTicketIds"] = remaining
    sweep["readyTicketIds"] = ready
    sweep["blockedTicketIds"] = blocked


def normalize_sweep_admission(cwd: Path, payload: Any) -> dict[str, Any]:
    admission = require_mapping(payload, "payload")
    fail(
        set(admission) != {"witness", "tickets"},
        "sweep_admission_invalid",
        "admit_sweep requires exactly payload.witness and payload.tickets",
    )
    witness = normalize_sweep_witness(admission["witness"])
    raw_tickets = admission["tickets"]
    fail(not isinstance(raw_tickets, list) or not raw_tickets, "sweep_ledger_invalid", "payload.tickets must be a non-empty array")

    ledger: dict[str, dict[str, Any]] = {}
    dependencies: dict[str, list[str]] = {}
    for index, raw_ticket in enumerate(raw_tickets):
        fail(not isinstance(raw_ticket, dict), "sweep_ledger_invalid", f"ticket {index + 1} must be an object")
        raw_ticket_id = raw_ticket.get("ticketId")
        fail(
            not isinstance(raw_ticket_id, str) or not raw_ticket_id.strip(),
            "sweep_ledger_invalid",
            f"ticket {index + 1} requires an explicit non-empty ticketId",
        )
        fail("dependsOn" not in raw_ticket, "dag_invalid", f"ticket {raw_ticket_id.strip()} requires dependsOn")
        ticket_dependencies = normalize_sweep_dependencies(raw_ticket["dependsOn"], f"ticket {raw_ticket_id.strip()}.dependsOn")
        body = copy.deepcopy(raw_ticket)
        body.pop("dependsOn")
        ticket = normalize_ticket(body, index)
        ticket_id_value = ticket["ticketId"]
        fail(ticket_id_value != raw_ticket_id.strip(), "sweep_ledger_invalid", f"ticket {index + 1} ticketId must be canonical")
        fail(ticket_id_value in ledger, "sweep_ledger_invalid", f"duplicate ticketId in sweep ledger: {ticket_id_value}")
        ledger[ticket_id_value] = ticket
        dependencies[ticket_id_value] = ticket_dependencies

    ledger = {ticket_id_value: ledger[ticket_id_value] for ticket_id_value in sorted(ledger)}
    dependencies = {ticket_id_value: dependencies[ticket_id_value] for ticket_id_value in sorted(dependencies)}
    order = sweep_topological_order(dependencies)
    fail(
        not sweep_has_incomparable_pair(dependencies, order),
        "sweep_not_parallel",
        "sweep DAG must contain at least one incomparable ticket pair",
    )
    validate_sweep_writer_contract(cwd, ledger)
    writer_count = sum(ticket["write"] for ticket in ledger.values())
    fail(
        witness["integration"] == "aggregate" and writer_count != 0,
        "sweep_witness_invalid",
        "aggregate sweep integration cannot admit writer tickets",
    )
    fail(
        witness["integration"] == "disjoint_patches" and writer_count == 0,
        "sweep_witness_invalid",
        "disjoint_patches sweep integration requires at least one writer ticket",
    )
    ticket_hashes = {ticket_id_value: digest(ticket) for ticket_id_value, ticket in ledger.items()}
    sweep = {
        "witness": witness,
        "ledger": ledger,
        "dependencies": dependencies,
        "ticketHashes": ticket_hashes,
        "ledgerHash": digest(ledger),
        "dagHash": digest(dependencies),
        "acceptedTicketIds": [],
        "remainingTicketIds": [],
        "readyTicketIds": [],
        "blockedTicketIds": [],
    }
    set_sweep_progress(sweep)
    return sweep


def require_sweep_integrity(state: dict[str, Any]) -> dict[str, Any]:
    fail(state.get("entry") != "sweep", "sweep_state_invalid", "run is not a sweep")
    sweep = state.get("sweep")
    expected_fields = {
        "witness",
        "ledger",
        "dependencies",
        "ticketHashes",
        "ledgerHash",
        "dagHash",
        "acceptedTicketIds",
        "remainingTicketIds",
        "readyTicketIds",
        "blockedTicketIds",
    }
    fail(not isinstance(sweep, dict) or set(sweep) != expected_fields, "sweep_state_invalid", "sealed sweep state has an invalid schema")
    normalized_witness = normalize_sweep_witness(sweep["witness"])
    fail(digest(normalized_witness) != digest(sweep["witness"]), "sweep_ledger_drift", "sealed sweep witness drifted")

    ledger = sweep["ledger"]
    dependencies = sweep["dependencies"]
    ticket_hashes = sweep["ticketHashes"]
    fail(not isinstance(ledger, dict) or not ledger, "sweep_state_invalid", "sealed sweep ledger is missing")
    fail(not isinstance(dependencies, dict) or set(dependencies) != set(ledger), "sweep_state_invalid", "sealed sweep DAG does not match its ledger")
    fail(not isinstance(ticket_hashes, dict) or set(ticket_hashes) != set(ledger), "sweep_state_invalid", "sealed sweep ticket hashes do not match its ledger")

    for index, ticket_id_value in enumerate(sorted(ledger)):
        ticket = ledger[ticket_id_value]
        fail(
            not isinstance(ticket_id_value, str) or not ticket_id_value or not isinstance(ticket, dict) or ticket.get("ticketId") != ticket_id_value,
            "sweep_ledger_drift",
            "sealed sweep ledger contains an invalid ticket identity",
        )
        normalized_ticket = normalize_ticket(copy.deepcopy(ticket), index)
        fail(digest(normalized_ticket) != digest(ticket), "sweep_ledger_drift", f"sealed ticket body drifted: {ticket_id_value}")
        fail(ticket_hashes.get(ticket_id_value) != digest(ticket), "sweep_ledger_drift", f"sealed ticket hash drifted: {ticket_id_value}")
        normalized_dependencies = normalize_sweep_dependencies(
            dependencies[ticket_id_value],
            f"sealed ticket {ticket_id_value}.dependsOn",
        )
        fail(
            dependencies[ticket_id_value] != normalized_dependencies,
            "sweep_ledger_drift",
            f"sealed dependency order drifted: {ticket_id_value}",
        )

    fail(sweep.get("ledgerHash") != digest(ledger), "sweep_ledger_drift", "sealed ledger hash does not match its canonical ledger")
    fail(sweep.get("dagHash") != digest(dependencies), "sweep_ledger_drift", "sealed DAG hash does not match its canonical dependencies")
    order = sweep_topological_order(dependencies)
    fail(
        not sweep_has_incomparable_pair(dependencies, order),
        "sweep_not_parallel",
        "sealed sweep DAG no longer contains an incomparable ticket pair",
    )
    validate_sweep_writer_contract(None, ledger)

    accepted_raw = sweep["acceptedTicketIds"]
    fail(
        not isinstance(accepted_raw, list) or any(not isinstance(ticket_id_value, str) or not ticket_id_value for ticket_id_value in accepted_raw),
        "sweep_progress_drift",
        "sealed acceptedTicketIds must be a string array",
    )
    accepted = set(accepted_raw)
    fail(accepted_raw != sorted(accepted) or not accepted.issubset(ledger), "sweep_progress_drift", "sealed acceptedTicketIds drifted")
    expected_remaining, expected_ready, expected_blocked = scheduled_sweep_sets(ledger, dependencies, accepted)
    fail(sweep["remainingTicketIds"] != expected_remaining, "sweep_progress_drift", "sealed remainingTicketIds drifted")
    fail(sweep["readyTicketIds"] != expected_ready, "sweep_progress_drift", "sealed readyTicketIds drifted")
    fail(sweep["blockedTicketIds"] != expected_blocked, "sweep_progress_drift", "sealed blockedTicketIds drifted")
    fail(
        bool(state.get("frontierExhausted")) != (not expected_remaining),
        "sweep_progress_drift",
        "sweep exhaustion flag does not match the runtime-owned remaining set",
    )
    authorized = state.get("authorizedNextTickets")
    if authorized is not None:
        fail(
            not isinstance(authorized, list)
            or any(not isinstance(ticket_id_value, str) or not ticket_id_value for ticket_id_value in authorized)
            or authorized != sorted(set(authorized)),
            "sweep_scheduler_invalid",
            "authorized sweep retry tickets must be a canonical string array",
        )
        fail(
            not set(authorized).issubset(set(expected_ready)),
            "sweep_scheduler_invalid",
            "authorized sweep retry tickets are not ready in the sealed DAG",
        )

    attempts = state.get("attempts", {})
    fail(not isinstance(attempts, dict), "sweep_state_invalid", "sweep attempt ledger is invalid")
    recorded_ticket_ids = set()
    for attempt_id, attempt in attempts.items():
        fail(not isinstance(attempt, dict), "sweep_state_invalid", f"sweep attempt {attempt_id} is invalid")
        ticket_id_value = attempt.get("ticketId")
        fail(ticket_id_value not in ledger, "sweep_ledger_drift", f"attempt {attempt_id} is not bound to the sealed ledger")
        ticket = attempt.get("ticket")
        fail(
            not isinstance(ticket, dict) or digest(ticket) != ticket_hashes[ticket_id_value],
            "sweep_ledger_drift",
            f"attempt {attempt_id} ticket body differs from the sealed ledger",
        )
        if "ticketHash" in attempt:
            fail(
                attempt["ticketHash"] != ticket_hashes[ticket_id_value],
                "sweep_ledger_drift",
                f"attempt {attempt_id} ticket hash differs from the sealed ledger",
            )
        if attempt.get("status") == "recorded":
            recorded_ticket_ids.add(ticket_id_value)
    fail(recorded_ticket_ids != accepted, "sweep_progress_drift", "recorded tickets do not match acceptedTicketIds")
    return sweep





def classify_ticket(ticket: dict[str, Any], config: dict[str, Any], floor: str | None = None) -> str:
    classes = config.get("routing", {}).get("classes", {})
    normalized = {str(signal).strip().lower() for signal in ticket["signals"]}
    pools = {name: {str(signal).strip().lower() for signal in definition.get("signals", [])} for name, definition in classes.items() if isinstance(definition, dict)}
    if normalized & pools.get("judgment", set()):
        derived = "judgment"
    elif ticket["write"] or normalized & pools.get("skilled", set()):
        derived = "skilled"
    elif normalized and normalized <= pools.get("mechanical", set()):
        derived = "mechanical"
    else:
        derived = "judgment"
    requested = ticket.get("class")
    if requested is not None:
        fail(requested not in CLASS_ORDER, "ticket_invalid", f"ticket {ticket['ticketId']} has an unknown class")
        fail(CLASS_ORDER[requested] < CLASS_ORDER[derived], "under_routing", f"ticket {ticket['ticketId']} requests {requested} below derived {derived}")
        derived = requested
    if floor is not None:
        fail(floor not in CLASS_ORDER, "state_corrupt", f"ticket {ticket['ticketId']} carries an unknown class floor")
        if CLASS_ORDER[floor] > CLASS_ORDER[derived]:
            derived = floor
    fail(ticket["write"] and derived == "judgment", "ticket_needs_decomposition", f"ticket {ticket['ticketId']} combines judgment with production writes; split design from implementation")
    return derived


def bind_slot(state: dict[str, Any], config: dict[str, Any], capability: str, slot: str) -> dict[str, Any]:
    """Resolve one Pocock slot into the agent and model witness sealed at start.

    There is no search here on purpose. A slot IS an OMP role name; which model
    stands behind it, and which model replaces it when that one is unavailable,
    are decisions that belong to the OMP config and to OMP's own retry, not to
    this contour.
    """
    omp = config["omp"]
    role_def = omp.get("roles", {}).get(capability, {})
    agents = role_def.get("agents", {}) if isinstance(role_def, dict) else {}
    agent = agents.get(slot)
    fail(
        not isinstance(agent, str) or not agent,
        "config_invalid",
        f"capability {capability} declares no agent for slot {slot}",
    )
    fail(slot not in omp.get("slots", {}), "config_invalid", f"slot {slot} is not declared in omp.slots")
    witness = state.get("models", {}).get(slot)
    fail(not isinstance(witness, dict), "route_unavailable", f"no model witness was sealed for slot {slot}")
    return {"slot": slot, "agent": agent, "witness": witness}


def slot_for(mapping: dict[str, Any], key: str, label: str) -> dict[str, Any]:
    """Pick the single slot definition declared for a producer class or lens."""
    definition = mapping.get(key)
    fail(not isinstance(definition, dict), "config_invalid", f"no slot mapping is declared for {key}")
    slot = declared_slot(mapping, key, label)
    return {"slot": slot, "definition": definition}


def budget_check(state: dict[str, Any], config: dict[str, Any], projection: int) -> None:
    maximum = int(config.get("session_budget", {}).get("tokens_max", 0))
    spent = int(state.get("tokensSpent", 0))
    reserved = int(state.get("projectedTokens", 0))
    fail(maximum <= 0, "config_invalid", "session_budget.tokens_max must be positive")
    fail(spent + reserved + projection > maximum, "budget_exceeded", f"dispatch projection {spent + reserved + projection} exceeds token budget {maximum}", spent=spent, reserved=reserved, projection=projection, maximum=maximum)


def command_prepare(cwd: Path, explicit_state_dir: str | None, request: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    def apply(state: dict[str, Any]) -> None:
        require_run_config(state, config)
        fail(state["phase"] != "ready", "illegal_transition", f"prepare is not legal in phase {state['phase']}")
        sweep: dict[str, Any] | None = None
        if state.get("entry") == "sweep":
            fail("tickets" in request, "sweep_payload_forbidden", "sweep prepare reads canonical tickets from the sealed ledger")
            sweep = require_sweep_integrity(state)
            authorized = state.get("authorizedNextTickets")
            ids = list(authorized) if authorized is not None else list(sweep["readyTicketIds"])
            fail(ids != sorted(set(ids)) or not ids, "sweep_scheduler_invalid", "sealed sweep scheduler produced no unique ready tickets")
            tickets = [copy.deepcopy(sweep["ledger"][ticket_id_value]) for ticket_id_value in ids]
        else:
            raw_tickets = request.get("tickets")
            fail(not isinstance(raw_tickets, list) or not raw_tickets, "ticket_invalid", "prepare requires at least one ticket")
            tickets = [normalize_ticket(raw, index) for index, raw in enumerate(raw_tickets)]
            ids = [ticket["ticketId"] for ticket in tickets]
            fail(len(ids) != len(set(ids)), "ticket_invalid", "ticket ids must be unique within a wave")

        configured_max_diff_lines = int(config.get("gates", {}).get("pre_gate", {}).get("max_diff_lines", 1200))
        for ticket in tickets:
            override = ticket.get("max_diff_lines_override")
            fail(
                override is not None
                and (not isinstance(override, int) or isinstance(override, bool) or override < configured_max_diff_lines),
                "ticket_invalid",
                f"ticket {ticket['ticketId']} diff override must be an integer at least as large as the configured ceiling",
            )
        for ticket in tickets:
            for writable_path in ticket["writablePaths"]:
                fail(
                    repo_path_uses_symlink(cwd, writable_path),
                    "ticket_invalid",
                    f"ticket {ticket['ticketId']} writablePaths traverses a symbolic link: {writable_path}",
                )
        fail(len(tickets) > 1 and any(ticket.get("ui_live") for ticket in tickets), "ticket_invalid", "a ui_live ticket must be dispatched alone so host-observed evidence has one unambiguous producer")
        for left_index, left in enumerate(tickets):
            if not left["write"]:
                continue
            for right in tickets[left_index + 1:]:
                if right["write"]:
                    fail(
                        writable_policies_overlap(left["writablePaths"], right["writablePaths"]),
                        "ticket_overlap",
                        f"writer tickets {left['ticketId']} and {right['ticketId']} have overlapping writablePaths",
                    )

        overlap = accepted_ticket_ids(state) & set(ids)
        fail(bool(overlap), "ticket_already_accepted", f"accepted tickets cannot be dispatched again: {', '.join(sorted(overlap))}")
        authorized = state.get("authorizedNextTickets")
        if authorized is not None:
            fail(set(ids) != set(authorized), "frontier_changed", "prepare ticket ids differ from the core-authorized next frontier", expected=sorted(authorized), observed=sorted(ids))
        if sweep is not None:
            expected_ids = authorized if authorized is not None else sweep["readyTicketIds"]
            fail(ids != expected_ids, "sweep_scheduler_invalid", "sweep prepare differs from the runtime-owned ready wave")

        max_attempts = int(config.get("routing", {}).get("escalation", {}).get("max_attempts_per_subtask", 2))
        exhausted = sorted(ticket_id_value for ticket_id_value in ids if ticket_failure_count(state, ticket_id_value) >= max_attempts)
        fail(bool(exhausted), "retry_limit", f"retry limit reached for: {', '.join(exhausted)}")
        per_ticket = int(config["omp"].get("budget_projection", {}).get("producer_per_ticket", 0))
        projection = per_ticket * len(tickets)
        budget_check(state, config, projection)
        wave_no = len(state["waves"]) + 1
        base_sha = None
        if any(ticket["write"] for ticket in tickets):
            try:
                completed = subprocess.run(["git", "rev-parse", "HEAD"], cwd=cwd, text=True, capture_output=True, check=False, timeout=GIT_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired as exc:
                raise RuntimeFailure("git_required", f"git rev-parse exceeded {GIT_TIMEOUT_SECONDS}s") from exc
            fail(completed.returncode != 0, "git_required", "writer dispatch requires a git repository")
            base_sha = completed.stdout.strip()

        items = []
        attempt_ids = []
        prepared_attempts = []
        for index, ticket in enumerate(tickets):
            floor = state.get("classFloor", {}).get(ticket["ticketId"])
            cls = classify_ticket(ticket, config, floor if isinstance(floor, str) else None)
            producers_map = config["omp"].get("producers", {})
            picked = slot_for(producers_map, cls, f"omp.producers.{cls}")
            capability = picked["definition"].get("capability")
            fail(not isinstance(capability, str) or not capability, "config_invalid", f"producer class {cls} declares no capability")
            selected = bind_slot(state, config, capability, picked["slot"])
            quality_no = ticket_attempt_count(state, ticket["ticketId"]) + 1
            attempt_id = f"{state['runId']}.w{wave_no}.{ticket['ticketId']}.a{quality_no}.{uuid.uuid4().hex[:6]}"
            dispatch_name = f"P{wave_no}T{index + 1}A{quality_no}"
            attempt = {
                "attemptId": attempt_id,
                "dispatchName": dispatch_name,
                "ticketId": ticket["ticketId"],
                "qualityAttempt": quality_no,
                "attemptOrdinal": quality_no,
                "ticket": ticket,
                "class": cls,
                "role": capability,
                "slot": selected["slot"],
                "slotRole": config["omp"]["slots"][selected["slot"]]["alias"],
                "agent": selected["agent"],
                "declaredAgent": selected["agent"],
                "declaredModel": selected["witness"]["resolvedModel"],
                "status": "prepared",
                "tokens": None,
                "observedAgent": None,
                "observedAgentSource": None,
                "observedModel": None,
                "modelFallback": None,
                "baseSha": base_sha,
            }
            if sweep is not None:
                attempt["ticketHash"] = sweep["ticketHashes"][ticket["ticketId"]]
            if ticket["ui_live"]:
                attempt["evidenceChallenge"] = {
                    "attemptId": attempt_id,
                    "ticketId": ticket["ticketId"],
                    "token": f"pocock-ui-{uuid.uuid4().hex}",
                    "target": ticket["ui_evidence"]["target"],
                    "criterion": ticket["ui_evidence"]["criterion"],
                    "requiredStages": ["open", "witness"],
                }
            prepared_attempts.append(attempt)
            attempt_ids.append(attempt_id)
            items.append({
                "name": dispatch_name,
                "agent": selected["agent"],
                "task": text_for_ticket(ticket),
                "effort": require_text(picked["definition"].get("effort"), f"omp.producers.{cls}.effort"),
                "outputSchema": producer_schema(),
                "schemaMode": "strict",
                "isolated": True,
            })

        for attempt in prepared_attempts:
            state["attempts"][attempt["attemptId"]] = attempt
            challenge = attempt.get("evidenceChallenge")
            if isinstance(challenge, dict):
                state["evidenceChallenges"][challenge["token"]] = challenge

        task_input = {
            "context": f"Pocock run {state['runId']}, wave {wave_no}. Config {state['configFingerprint']}. Each item is sealed; return only its strict structured result.",
            "tasks": items,
        }
        dispatch_id = f"dispatch-{uuid.uuid4().hex}"
        pending = {
            "dispatchId": dispatch_id,
            "kind": "producer",
            "status": "prepared",
            "attemptIds": attempt_ids,
            "taskInput": task_input,
            "inputHash": digest(task_input),
            "createdAt": dt.datetime.now(dt.timezone.utc).isoformat(),
            "projection": projection,
        }
        wave = {
            "waveId": f"wave-{wave_no}",
            "attemptIds": attempt_ids,
            "candidateAttemptIds": [],
            "failedAttemptIds": [],
            "status": "prepared",
            "baseSha": base_sha,
            "producerSlots": sorted({attempt["slot"] for attempt in prepared_attempts}),
        }
        if sweep is not None:
            wave.update({
                "ticketIds": ids,
                "ledgerHash": sweep["ledgerHash"],
                "dagHash": sweep["dagHash"],
            })
        state["waves"].append(wave)
        state["currentWave"] = wave["waveId"]
        state["pendingDispatch"] = pending
        state["projectedTokens"] = int(state.get("projectedTokens", 0)) + projection
        state["blockedReason"] = None
        state["authorizedNextTickets"] = None
        set_retry_ticket_ids(state, set())
        set_retry_lens_names(state, set())
        state["phase"] = "producer_dispatch_pending"

    state, _ = mutate(cwd, explicit_state_dir, request, apply)
    return output(state)


def command_seal(cwd: Path, explicit_state_dir: str | None, request: dict[str, Any]) -> dict[str, Any]:
    kind = request.get("kind")
    fail(kind not in {"producer", "lenses"}, "invalid_request", "seal-task kind must be producer or lenses")

    def apply(state: dict[str, Any]) -> dict[str, Any]:
        expected_phase = "producer_dispatch_pending" if kind == "producer" else "lens_dispatch_pending"
        fail(state["phase"] != expected_phase, "illegal_transition", f"cannot seal {kind} in phase {state['phase']}")
        pending = state.get("pendingDispatch")
        fail(not isinstance(pending, dict) or pending.get("kind") != kind or pending.get("status") != "prepared", "dispatch_invalid", "no matching prepared dispatch exists")
        validate_omp_isolation_baseline(cwd)
        collection = state["attempts"] if kind == "producer" else state["lensAttempts"]
        pending["status"] = "running"
        pending["sealedAt"] = dt.datetime.now(dt.timezone.utc).isoformat()
        pending["sealNonce"] = uuid.uuid4().hex
        for attempt_id in pending["attemptIds"]:
            collection[attempt_id]["status"] = "running"
        state["phase"] = "producer_running" if kind == "producer" else "lens_running"
        return {"dispatchId": pending["dispatchId"], "attemptIds": pending["attemptIds"], "taskInput": pending["taskInput"]}

    state, extra = mutate(cwd, explicit_state_dir, request, apply)
    return output(state, **(extra or {}))


def parse_result_data(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None
def validate_producer_result(data: dict[str, Any], attempt: dict[str, Any]) -> None:
    expected = {"status", "summary", "evidence", "changedFiles", "verification"}
    fail(set(data) != expected, "structured_output_invalid", f"producer result for {attempt['attemptId']} has the wrong fields")
    fail(data["status"] not in {"COMPLETED", "NEEDS_CLARIFICATION", "BLOCKED"}, "structured_output_invalid", "producer status is invalid")
    fail(not isinstance(data["summary"], str) or not data["summary"].strip(), "structured_output_invalid", "producer summary must be non-empty")
    for field in ("evidence", "changedFiles", "verification"):
        fail(not isinstance(data[field], list) or not all(isinstance(value, str) and value.strip() for value in data[field]), "structured_output_invalid", f"producer {field} must be a string array")
    if data["status"] == "COMPLETED":
        fail(not data["evidence"], "structured_output_invalid", "a completed producer result requires evidence")
        fail(attempt["ticket"]["write"] and not data["changedFiles"], "structured_output_invalid", "a completed writer result must name at least one changed file")


def validate_lens_findings(findings: Any, label: str) -> None:
    fail(not isinstance(findings, list), "structured_output_invalid", f"{label} findings must be an array")
    for finding in findings:
        fail(not isinstance(finding, dict) or set(finding) != {"scope", "severity", "blocking", "evidence"}, "structured_output_invalid", f"{label} has a finding with the wrong fields")
        fail(finding["scope"] not in {"introduced", "pre-existing", "decision-challenge"}, "structured_output_invalid", f"{label} has a finding with an invalid scope")
        fail(finding["severity"] not in {"critical", "high", "medium", "low", "note"}, "structured_output_invalid", f"{label} has a finding with an invalid severity")
        fail(not isinstance(finding["blocking"], bool), "structured_output_invalid", f"{label} finding blocking flag must be boolean")
        fail(not isinstance(finding["evidence"], str) or not finding["evidence"].strip(), "structured_output_invalid", f"{label} finding requires evidence")


def validate_lens_result(data: dict[str, Any], assignment: dict[str, Any]) -> None:
    expected = {"lens", "summary", "reports"}
    fail(set(data) != expected, "structured_output_invalid", f"lens result for {assignment['attemptId']} has the wrong fields")
    fail(data["lens"] != assignment["lens"], "structured_output_invalid", f"lens identity mismatch for {assignment['attemptId']}")
    fail(not isinstance(data["summary"], str) or not data["summary"].strip(), "structured_output_invalid", "lens summary must be non-empty")
    reports = data["reports"]
    fail(not isinstance(reports, list), "structured_output_invalid", "lens reports must be an array")
    expected_attempt_ids = assignment.get("producerAttemptIds")
    fail(
        not isinstance(expected_attempt_ids, list) or not expected_attempt_ids or any(not isinstance(value, str) or not value for value in expected_attempt_ids),
        "state_corrupt",
        f"lens assignment {assignment['attemptId']} has invalid producer bindings",
    )
    report_ids: list[str] = []
    expected_verdict = {"PASS", "FAIL"} if assignment["lens"] == "Critic" else {"NO_VERDICT"}
    for report in reports:
        fail(
            not isinstance(report, dict) or set(report) != {"attemptId", "summary", "findings", "verdict"},
            "structured_output_invalid",
            f"lens report for {assignment['attemptId']} has the wrong fields",
        )
        report_id = report["attemptId"]
        fail(not isinstance(report_id, str) or not report_id, "structured_output_invalid", "lens report attemptId must be non-empty")
        report_ids.append(report_id)
        fail(not isinstance(report["summary"], str) or not report["summary"].strip(), "structured_output_invalid", "lens report summary must be non-empty")
        fail(report["verdict"] not in expected_verdict, "structured_output_invalid", f"{assignment['lens']} emitted an illegal verdict")
        validate_lens_findings(report["findings"], f"lens report for {report_id}")
    fail(
        len(report_ids) != len(set(report_ids)) or set(report_ids) != set(expected_attempt_ids),
        "stale_attempt",
        f"lens {assignment['attemptId']} does not cover exactly its sealed producer attempts",
    )




def usage_alias_value(usage: dict[str, Any], aliases: tuple[str, ...]) -> tuple[bool, int | None]:
    values = [usage[key] for key in aliases if key in usage]
    if not values:
        return False, None
    normalized = [exact_nonnegative_integer(value) for value in values]
    if any(value is None for value in normalized):
        return True, None
    first = normalized[0]
    if any(value != first for value in normalized[1:]):
        return True, None
    return True, first


def usage_tokens(result: dict[str, Any]) -> int | None:
    usage = result.get("usage")
    if isinstance(usage, dict):
        categories = {
            "input": ("input", "inputTokens", "input_tokens"),
            "output": ("output", "outputTokens", "output_tokens"),
            "cacheWrite": (
                "cacheWrite",
                "cacheWriteTokens",
                "cacheWriteInputTokens",
                "cache_write",
                "cache_write_tokens",
                "cache_write_input_tokens",
                "cacheCreationInputTokens",
                "cache_creation_input_tokens",
            ),
            "cacheRead": (
                "cacheRead",
                "cacheReadTokens",
                "cacheReadInputTokens",
                "cache_read",
                "cache_read_tokens",
                "cache_read_input_tokens",
                "cachedInputTokens",
                "cached_input_tokens",
            ),
        }
        observed = {
            name: usage_alias_value(usage, aliases)
            for name, aliases in categories.items()
        }
        if any(present and value is None for present, value in observed.values()):
            return None
        if all(present for present, _value in observed.values()):
            return sum(observed[name][1] for name in ("input", "output", "cacheWrite"))

    tokens = exact_nonnegative_integer(result.get("tokens"))
    return tokens if tokens is not None and tokens > 0 else None


def hash_artifact(path_value: Any, label: str) -> dict[str, Any]:
    path = Path(require_text(path_value, label)).expanduser()
    fail(not path.is_file(), "artifact_missing", f"{label} does not name a readable file: {path}")
    data = path.read_bytes()
    return {"path": str(path.resolve()), "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}

def normalize_observed_repo_path(value: str, label: str) -> str:
    try:
        return normalize_repo_path(value, label, directory_allowed=False)
    except RuntimeFailure as exc:
        raise RuntimeFailure("patch_invalid", exc.message, **exc.details) from exc


def inspect_patch(cwd: Path, path_value: Any, label: str, max_bytes: int) -> dict[str, Any]:
    path = Path(require_text(path_value, label)).expanduser()
    fail(not path.is_file(), "artifact_missing", f"{label} does not name a readable file: {path}")
    data = path.read_bytes()
    fail(len(data) > max_bytes, "patch_too_large", f"{label} exceeds the {max_bytes}-byte patch ceiling")
    artifact = {"path": str(path.resolve()), "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
    if not data.strip():
        return {"artifact": artifact, "data": b"", "files": [], "diffLines": 0}

    forbidden_headers = (
        b"rename from ",
        b"rename to ",
        b"copy from ",
        b"copy to ",
        b"deleted file mode ",
        b"new file mode 120000",
        b"old mode 120000",
        b"new mode 120000",
        b"new file mode 160000",
        b"old mode 160000",
        b"new mode 160000",
    )
    offending_header = next(
        (line for line in data.splitlines() if any(line.startswith(prefix) for prefix in forbidden_headers)),
        None,
    )
    fail(
        offending_header is not None,
        "patch_operation_forbidden",
        f"{label} contains a forbidden delete, rename, copy, symlink, or gitlink operation",
    )
    fail(
        any(line == b"+++ /dev/null" for line in data.splitlines()),
        "patch_operation_forbidden",
        f"{label} deletes a file",
    )

    # Parse the captured bytes through stdin, never the artifact path a second
    # time: metadata must describe exactly the data that will later be applied,
    # so swapping the artifact file between read and parse cannot bypass scope.
    try:
        completed = subprocess.run(
            ["git", "apply", "--numstat", "-z", "-"],
            cwd=cwd,
            input=data,
            capture_output=True,
            check=False,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeFailure("subprocess_timeout", f"{label} numstat parse exceeded {GIT_TIMEOUT_SECONDS}s") from exc
    fail(completed.returncode != 0, "patch_invalid", completed.stderr.decode("utf-8", errors="replace").strip() or f"{label} is not a valid patch")
    files: list[str] = []
    diff_lines = 0
    for record in completed.stdout.split(b"\0"):
        if not record:
            continue
        fields = record.split(b"\t", 2)
        fail(len(fields) != 3 or not fields[2], "patch_invalid", f"{label} contains an unsupported path record")
        try:
            observed = fields[2].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RuntimeFailure("patch_invalid", f"{label} contains a non-UTF-8 path") from exc
        normalized = normalize_observed_repo_path(observed, label)
        fail(normalized in files, "patch_invalid", f"{label} modifies {normalized} more than once")
        files.append(normalized)
        try:
            additions = 0 if fields[0] == b"-" else int(fields[0])
            deletions = 0 if fields[1] == b"-" else int(fields[1])
        except ValueError as exc:
            raise RuntimeFailure("patch_invalid", f"{label} contains an invalid numstat record") from exc
        diff_lines += max(1, additions + deletions)
    fail(not files, "patch_invalid", f"{label} is non-empty but names no changed files")
    return {"artifact": artifact, "data": data, "files": sorted(files), "diffLines": diff_lines}


def validate_patch_scope(cwd: Path, patch: dict[str, Any], attempt: dict[str, Any]) -> None:
    ticket = attempt["ticket"]
    observed = patch["files"]
    fail(not ticket["write"] and bool(observed), "patch_scope_violation", f"read-only ticket {attempt['ticketId']} produced repository changes")
    unauthorized = [
        path
        for path in observed
        if not any(writable_path_allows(allowed, path) for allowed in ticket["writablePaths"])
    ]
    fail(
        bool(unauthorized),
        "patch_scope_violation",
        f"ticket {attempt['ticketId']} modified paths outside writablePaths: {', '.join(unauthorized)}",
    )
    symlinks = [path for path in observed if repo_path_uses_symlink(cwd, path)]
    fail(bool(symlinks), "patch_operation_forbidden", f"ticket {attempt['ticketId']} modifies paths through symbolic links: {', '.join(symlinks)}")


def validate_patch_applicability(cwd: Path, patch: dict[str, Any], label: str) -> None:
    """Reject one captured patch without making sibling settlements collateral.

    Settlement accepts only a forward-applicable patch. A reverse-applicable
    patch is pre-existing working-tree content, not work this attempt may
    claim; rejecting it here prevents one conflicting attempt from failing a
    later whole-wave application.
    """
    if not patch["data"]:
        return
    try:
        forward = subprocess.run(
            ["git", "apply", "--check", "--whitespace=nowarn", "-"],
            cwd=cwd,
            input=patch["data"],
            capture_output=True,
            check=False,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeFailure("subprocess_timeout", f"{label} applicability check exceeded {GIT_TIMEOUT_SECONDS}s") from exc
    fail(
        forward.returncode != 0,
        "patch_conflict",
        forward.stderr.decode("utf-8", errors="replace").strip() or f"{label} does not apply cleanly",
    )


def write_bytes_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def apply_patch_batch(
    cwd: Path,
    patches: list[dict[str, Any]],
    artifact_dir: Path,
    *,
    run_id: str,
    dispatch_id: str,
) -> dict[str, Any]:
    chunks = [patch["data"] for patch in patches if patch["data"]]
    if not chunks:
        return {"sha256": hashlib.sha256(b"").hexdigest(), "bytes": 0, "files": [], "diffLines": 0, "attemptPatches": []}
    combined = b"".join(chunk if chunk.endswith(b"\n") else chunk + b"\n" for chunk in chunks)
    combined_sha = hashlib.sha256(combined).hexdigest()
    batch_id = digest({"sha256": combined_sha, "attemptIds": [patch["attemptId"] for patch in patches]})[:16]
    combined_path = artifact_dir / f"wave-{batch_id}.patch"
    journal_path = artifact_dir / f"wave-{batch_id}.apply.json"
    write_bytes_atomic(combined_path, combined)

    attempt_patches = []
    for patch in patches:
        if not patch["data"]:
            continue
        patch_path = artifact_dir / f"attempt-{digest(patch['attemptId'])[:16]}.patch"
        write_bytes_atomic(patch_path, patch["data"])
        attempt_patches.append({
            "attemptId": patch["attemptId"],
            "path": str(patch_path.resolve()),
            "sha256": patch["artifact"]["sha256"],
            "bytes": patch["artifact"]["bytes"],
            "files": patch["files"],
            "diffLines": patch["diffLines"],
        })

    auth_key = journal_auth_key(artifact_dir, create=True)

    def write_journal(body: dict[str, Any]) -> None:
        body.pop("mac", None)
        body["mac"] = record_mac(body, auth_key)
        write_bytes_atomic(journal_path, (canonical(body) + "\n").encode("utf-8"))

    journal = {
        "sha256": combined_sha,
        "cwd": str(cwd.resolve()),
        "runId": run_id,
        "dispatchId": dispatch_id,
        "patchPath": str(combined_path.resolve()),
        "attemptPatches": attempt_patches,
        "status": "prepared",
    }
    if journal_path.is_file():
        try:
            observed_journal = json.loads(journal_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeFailure("patch_journal_corrupt", f"cannot read patch application journal: {exc}") from exc
        fail(
            not isinstance(observed_journal, dict)
            or observed_journal.get("sha256") != combined_sha
            or observed_journal.get("cwd") != str(cwd.resolve())
            or observed_journal.get("runId") != run_id
            or observed_journal.get("dispatchId") != dispatch_id
            or observed_journal.get("patchPath") != str(combined_path.resolve())
            or observed_journal.get("attemptPatches") != attempt_patches
            or observed_journal.get("status") not in {"prepared", "applied", "rolled_back"},
            "patch_journal_corrupt",
            f"patch application journal does not match this wave: {journal_path}",
        )
        fail(
            "mac" in observed_journal and not record_mac_valid(observed_journal, auth_key),
            "patch_journal_corrupt",
            f"patch application journal authentication witness is invalid: {journal_path}",
        )
        journal = observed_journal
    else:
        write_journal(journal)
    if journal["status"] == "rolled_back":
        # Orphan recovery proved and restored the preimage. Re-arm the same
        # sealed journal for the retried settlement; this is a fresh forward
        # apply, not ownership of a pre-existing postimage.
        journal["status"] = "prepared"
        journal.pop("rolledBackAt", None)
        write_journal(journal)

    def git_apply(*extra: str) -> subprocess.CompletedProcess[bytes]:
        try:
            return subprocess.run(
                ["git", "apply", "--whitespace=nowarn", *extra, "-"],
                cwd=cwd,
                input=combined,
                capture_output=True,
                check=False,
                timeout=GIT_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeFailure("patch_apply_failed", f"git apply exceeded {GIT_TIMEOUT_SECONDS}s") from exc

    if journal["status"] == "applied":
        # Idempotent re-entry: only verify the working tree still carries the
        # applied patch; never re-apply or re-claim it here.
        reverse = git_apply("--reverse", "--check")
        fail(reverse.returncode != 0, "patch_journal_mismatch", "journal says the producer patch is applied, but the working tree disagrees")
    else:
        forward = git_apply("--check")
        # Forward-only for a prepared journal: a patch that does not apply
        # forward conflicts with the working tree. Reverse-applicability means
        # the tree already holds this postimage — possibly a pre-existing user
        # change — and claiming it would attribute foreign work to this run.
        fail(forward.returncode != 0, "patch_conflict", forward.stderr.decode("utf-8", errors="replace").strip() or "combined producer patches do not apply cleanly")
        applied = git_apply()
        fail(applied.returncode != 0, "patch_apply_failed", applied.stderr.decode("utf-8", errors="replace").strip() or "validated producer patches could not be applied")
        journal["status"] = "applied"
        write_journal(journal)

    return {
        "path": str(combined_path.resolve()),
        "journalPath": str(journal_path.resolve()),
        "sha256": combined_sha,
        "bytes": len(combined),
        "files": sorted({path for patch in patches for path in patch["files"]}),
        "diffLines": sum(int(patch["diffLines"]) for patch in patches),
        "attemptPatches": attempt_patches,
    }


def rollback_orphaned_patch_journals(
    cwd: Path,
    explicit_state_dir: str | None,
    state: dict[str, Any],
    dispatch_ids: set[str] | None = None,
) -> None:
    state_path, _lock_path = state_paths(cwd, explicit_state_dir, str(state["runId"]))
    artifact_dir = state_path.with_suffix(".artifacts")
    if not artifact_dir.is_dir():
        return

    committed_journals: set[Path] = set()
    containers = [state.get("pendingDispatch"), *state.get("waves", [])]
    for container in containers:
        if not isinstance(container, dict):
            continue
        applied = container.get("appliedPatch")
        if isinstance(applied, dict):
            journal_value = nullable_text(applied.get("journalPath"))
            if journal_value is not None:
                committed_journals.add(Path(journal_value).resolve())

    try:
        auth_key = journal_auth_key(artifact_dir, create=False)
    except RuntimeFailure:
        # No authentication key means no orphan journal can prove the runtime
        # wrote it; recovery executes nothing rather than trusting them.
        return

    for journal_path in sorted(artifact_dir.glob("wave-*.apply.json")):
        resolved_journal = journal_path.resolve()
        if resolved_journal in committed_journals:
            continue
        try:
            journal = json.loads(journal_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeFailure("patch_journal_corrupt", f"cannot read orphan patch journal: {exc}") from exc
        fail(not isinstance(journal, dict), "patch_journal_corrupt", f"orphan patch journal is not an object: {journal_path}")
        fail(
            journal.get("cwd") != str(cwd.resolve()) or journal.get("runId") != state["runId"],
            "patch_journal_corrupt",
            f"orphan patch journal belongs to another run: {journal_path}",
        )
        dispatch_id = require_text(journal.get("dispatchId"), "patchJournal.dispatchId")
        if dispatch_ids is not None and dispatch_id not in dispatch_ids:
            continue
        status = journal.get("status")
        fail(
            status not in {"prepared", "applied", "rolled_back"},
            "patch_journal_corrupt",
            f"orphan patch journal has an invalid status: {journal_path}",
        )
        if status == "rolled_back":
            continue
        # Only the runtime's own authenticated journal may drive a reverse
        # apply: a forged or legacy record is skipped, never executed.
        if "mac" not in journal or not record_mac_valid(journal, auth_key):
            continue

        patch_path = Path(require_text(journal.get("patchPath"), "patchJournal.patchPath"))
        fail(
            patch_path.parent.resolve() != artifact_dir.resolve() or not patch_path.is_file(),
            "patch_journal_corrupt",
            f"orphan patch artifact is missing or outside its run: {patch_path}",
        )
        patch_data = patch_path.read_bytes()
        fail(
            hashlib.sha256(patch_data).hexdigest() != journal.get("sha256"),
            "patch_journal_corrupt",
            f"orphan patch artifact hash mismatch: {patch_path}",
        )
        def run_git(*extra: str) -> subprocess.CompletedProcess[bytes]:
            try:
                return subprocess.run(
                    ["git", "apply", "--whitespace=nowarn", *extra, "-"],
                    cwd=cwd,
                    input=patch_data,
                    capture_output=True,
                    check=False,
                    timeout=GIT_TIMEOUT_SECONDS,
                )
            except subprocess.TimeoutExpired as exc:
                raise RuntimeFailure("rollback_failed", f"orphan rollback exceeded {GIT_TIMEOUT_SECONDS}s") from exc

        reverse = run_git("--reverse", "--check")
        if reverse.returncode == 0:
            reverted = run_git("--reverse")
            fail(
                reverted.returncode != 0,
                "rollback_failed",
                reverted.stderr.decode("utf-8", errors="replace").strip() or "orphan producer patch rollback failed",
            )
        else:
            # Neither pre- nor post-image is provable: the tree must hold
            # either the preimage (nothing to undo) or nothing verifiable
            # (corruption). Only the forward-applicable preimage is safe to
            # record as rolled back.
            forward = run_git("--check")
            fail(
                forward.returncode != 0,
                "rollback_failed",
                reverse.stderr.decode("utf-8", errors="replace").strip() or "orphan producer patch cannot be rolled back cleanly",
            )
        journal["status"] = "rolled_back"
        journal["rolledBackAt"] = dt.datetime.now(dt.timezone.utc).isoformat()
        journal.pop("mac", None)
        journal["mac"] = record_mac(journal, auth_key)
        write_bytes_atomic(journal_path, (canonical(journal) + "\n").encode("utf-8"))


def rollback_attempt_patch(cwd: Path, attempt: dict[str, Any], applied: dict[str, Any]) -> None:
    path = Path(require_text(applied.get("path"), "appliedPatch.path"))
    fail(not path.is_file(), "rollback_failed", f"cannot restore rejected patch; artifact is missing: {path}")
    data = path.read_bytes()
    fail(
        hashlib.sha256(data).hexdigest() != applied.get("sha256"),
        "rollback_failed",
        f"rejected patch artifact hash mismatch: {path}",
    )
    patch_data = data if data.endswith(b"\n") else data + b"\n"
    rollback_sha = hashlib.sha256(patch_data).hexdigest()
    attempt_id = require_text(attempt.get("attemptId"), "attempt.attemptId")
    rollback_id = digest({"sha256": rollback_sha, "attemptId": attempt_id})[:16]
    artifact_dir = path.parent
    patch_path = artifact_dir / f"rollback-{rollback_id}.patch"
    journal_path = artifact_dir / f"rollback-{rollback_id}.json"
    write_bytes_atomic(patch_path, patch_data)

    auth_key = journal_auth_key(artifact_dir, create=True)

    def write_rollback_journal(body: dict[str, Any]) -> None:
        body.pop("mac", None)
        body["mac"] = record_mac(body, auth_key)
        write_bytes_atomic(journal_path, (canonical(body) + "\n").encode("utf-8"))

    journal = {
        "sha256": rollback_sha,
        "cwd": str(cwd.resolve()),
        "attemptId": attempt_id,
        "status": "prepared",
    }
    if journal_path.is_file():
        try:
            observed_journal = json.loads(journal_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeFailure("rollback_journal_corrupt", f"cannot read patch rollback journal: {exc}") from exc
        fail(
            not isinstance(observed_journal, dict)
            or observed_journal.get("sha256") != rollback_sha
            or observed_journal.get("cwd") != str(cwd.resolve())
            or observed_journal.get("attemptId") != attempt_id
            or observed_journal.get("status") not in {"prepared", "rolled_back"},
            "rollback_journal_corrupt",
            f"patch rollback journal does not match rejected attempt: {journal_path}",
        )
        fail(
            "mac" in observed_journal and not record_mac_valid(observed_journal, auth_key),
            "rollback_journal_corrupt",
            f"patch rollback journal authentication witness is invalid: {journal_path}",
        )
        journal = observed_journal
    else:
        write_rollback_journal(journal)

    def git_check(*extra: str) -> subprocess.CompletedProcess[bytes]:
        try:
            return subprocess.run(
                ["git", "apply", "--whitespace=nowarn", *extra, "-"],
                cwd=cwd,
                input=patch_data,
                capture_output=True,
                check=False,
                timeout=GIT_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeFailure("rollback_failed", f"git apply exceeded {GIT_TIMEOUT_SECONDS}s") from exc

    if journal["status"] == "rolled_back":
        forward = git_check("--check")
        fail(
            forward.returncode != 0,
            "rollback_journal_mismatch",
            "journal says the rejected patch was rolled back, but the working tree disagrees",
        )
    else:
        reverse = git_check("--reverse", "--check")
        if reverse.returncode == 0:
            reverted = git_check("--reverse")
            fail(reverted.returncode != 0, "rollback_failed", reverted.stderr.decode("utf-8", errors="replace").strip() or "rejected producer patch rollback failed")
        else:
            forward = git_check("--check")
            fail(
                forward.returncode != 0,
                "rollback_failed",
                reverse.stderr.decode("utf-8", errors="replace").strip() or "rejected producer patch cannot be rolled back cleanly",
            )
        journal["status"] = "rolled_back"
        write_rollback_journal(journal)

    applied["rolledBackAt"] = dt.datetime.now(dt.timezone.utc).isoformat()


def rollback_attempt_patches(cwd: Path, state: dict[str, Any], attempt_ids: set[str]) -> None:
    records = []
    for attempt_id in reversed(list(state.get("attempts", {}))):
        if attempt_id not in attempt_ids:
            continue
        attempt = state["attempts"][attempt_id]
        applied = attempt.get("appliedPatch")
        if isinstance(applied, dict) and not applied.get("rolledBackAt"):
            records.append((attempt, applied))
    for attempt, applied in records:
        rollback_attempt_patch(cwd, attempt, applied)


def command_record_result(cwd: Path, explicit_state_dir: str | None, request: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    dispatch_id = require_text(request.get("dispatchId"), "dispatchId")
    tool_call_id = require_text(request.get("toolCallId"), "toolCallId")
    executed_input = require_mapping(request.get("input"), "input")
    details = require_mapping(request.get("details"), "details")
    fail(details.get("async") is not None, "async_dispatch_rejected", "Pocock accepts only settled blocking task results")
    results = details.get("results")
    fail(not isinstance(results, list), "task_result_invalid", "details.results must be an array")
    content = require_mapping(request.get("content"), "content")

    def apply(state: dict[str, Any]) -> None:
        require_run_config(state, config)
        pending = state.get("pendingDispatch")
        fail(
            not isinstance(pending, dict) or pending.get("dispatchId") != dispatch_id or pending.get("status") != "running",
            "stale_dispatch",
            "task result is not for the current running dispatch",
        )
        kind = pending["kind"]
        expected_phase = "producer_running" if kind == "producer" else "lens_running"
        fail(state["phase"] != expected_phase, "stale_dispatch", f"task result arrived in phase {state['phase']}")
        fail(digest(executed_input) != pending["inputHash"], "seal_mismatch", "actually executed task input differs from the sealed batch")
        expected_ids = pending.get("attemptIds")
        fail(
            not isinstance(expected_ids, list) or any(not isinstance(attempt_id, str) or not attempt_id for attempt_id in expected_ids),
            "dispatch_invalid",
            "sealed dispatch has invalid attempt ids",
        )
        fail(len(results) != len(expected_ids), "task_result_invalid", "task result count differs from sealed attempt count")
        collection = state["attempts"] if kind == "producer" else state["lensAttempts"]

        # Dispatch identity, ordering, and task-to-attempt binding are all-or-nothing.
        # Everything after this loop is safely attributable to the individual attempt.
        for expected_id, raw_result in zip(expected_ids, results):
            fail(not isinstance(raw_result, dict), "task_result_invalid", "each task result must be an object")
            fail(
                raw_result.get("attemptId") != expected_id,
                "stale_attempt",
                f"result is bound to {raw_result.get('attemptId')}, expected {expected_id}",
            )
            attempt = collection.get(expected_id)
            fail(
                not isinstance(attempt, dict) or attempt.get("status") != "running",
                "dispatch_invalid",
                f"sealed attempt {expected_id} is not running",
            )

        if kind == "producer":
            # A previous process may have applied a prepared journal and died
            # before persisting status=applied. Reverse that authenticated
            # orphan before validating the retried settlement; never reclaim
            # an already-present postimage as this attempt's work.
            rollback_orphaned_patch_journals(cwd, explicit_state_dir, state)
        observed_tokens = 0
        producer_patches: list[dict[str, Any]] = []
        successful_producer_ids: list[str] = []
        failed_producer_ids: list[str] = []
        availability_ticket_ids: list[str] = []
        quality_ticket_ids: list[str] = []
        failed_lenses: set[str] = set()
        failure_reasons: list[str] = []
        patch_bytes_max = int(config.get("omp", {}).get("pre_gate", {}).get("patch_bytes_max", DEFAULT_PATCH_BYTES_MAX))

        def record_failure(
            attempt: dict[str, Any],
            *,
            code: str,
            reason: str,
            failure_kind: str,
            availability_evidence: dict[str, Any] | None = None,
        ) -> None:
            attempt["status"] = "availability_failed" if failure_kind == "availability" else "clarification_failed" if failure_kind == "clarification" else "result_invalid"
            attempt["failureCode"] = code
            attempt["failureReason"] = reason
            attempt["failureKind"] = failure_kind
            if availability_evidence is not None:
                attempt["availabilityEvidence"] = availability_evidence
            failure_reasons.append(f"{attempt['attemptId']}: {reason}")
            if kind == "producer":
                failed_producer_ids.append(attempt["attemptId"])
                if failure_kind == "availability":
                    availability_ticket_ids.append(str(attempt["ticketId"]))
                else:
                    quality_ticket_ids.append(str(attempt["ticketId"]))
            else:
                failed_lenses.add(str(attempt["lens"]))

        for expected_id, raw_result in zip(expected_ids, results):
            attempt = collection[expected_id]
            declared_agent = raw_result.get("declaredAgent")
            observed_agent = raw_result.get("observedAgent")
            declared_model = raw_result.get("declaredModel")
            observed_model = raw_result.get("observedResolvedModel")
            observed_agent_source = raw_result.get("observedAgentSource")
            model_fallback = raw_result.get("resolvedModelIsFallback") if isinstance(raw_result.get("resolvedModelIsFallback"), bool) else None
            tokens = usage_tokens(raw_result)
            attempt["toolCallId"] = tool_call_id
            attempt["tokens"] = tokens
            attempt["observedAgent"] = nullable_text(observed_agent)
            attempt["observedAgentSource"] = nullable_text(observed_agent_source)
            attempt["observedModel"] = nullable_text(observed_model)
            attempt["modelFallback"] = model_fallback
            if tokens is not None:
                observed_tokens += tokens

            is_error = request.get("isError") is True
            task_error = nullable_text(raw_result.get("error"))
            aborted = raw_result.get("aborted") is True
            exit_code = raw_result.get("exitCode")
            if is_error or task_error is not None or aborted or exit_code not in (None, 0):
                reason = task_error or (
                    f"task tool reported an error for {expected_id}"
                    if is_error
                    else f"worker did not settle successfully for {expected_id}"
                )
                record_failure(
                    attempt,
                    code="worker_unavailable",
                    reason=reason,
                    failure_kind="availability",
                    availability_evidence={
                        "declaredModel": declared_model,
                        "observedModel": observed_model,
                        "fallback": model_fallback,
                        "error": task_error,
                        "exitCode": exit_code,
                        "aborted": aborted,
                        "isError": is_error,
                        "reason": reason,
                    },
                )
                continue

            try:
                fail(
                    declared_agent != attempt["agent"]
                    or observed_agent != attempt["agent"]
                    or observed_agent_source not in {"user", "project"},
                    "agent_mismatch",
                    f"agent witness mismatch for {expected_id}",
                )
                fail(
                    model_base(declared_model) != model_base(attempt["declaredModel"]),
                    "model_mismatch",
                    f"declared model differs from sealed model for {expected_id}",
                )
                fail(
                    nullable_text(observed_model) is None,
                    "model_witness_missing",
                    f"observed model is missing for successful result {expected_id}",
                )
                artifact = hash_artifact(raw_result.get("outputPath"), f"result {expected_id}.outputPath")
                attempt["artifact"] = artifact
                patch_path = raw_result.get("patchPath")
                attempt["patchArtifact"] = (
                    hash_artifact(patch_path, f"result {expected_id}.patchPath")
                    if patch_path
                    else None
                )
                data = parse_result_data(content.get(expected_id))
                fail(data is None, "structured_output_invalid", f"strict structured output is missing for {expected_id}")
                reported_changed_files = data.get("changedFiles")
                if isinstance(reported_changed_files, list) and all(isinstance(path, str) for path in reported_changed_files):
                    attempt["reportedChangedFiles"] = list(reported_changed_files)
                if kind == "producer":
                    validate_producer_result(data, attempt)
                    attempt["result"] = data
                    attempt["patchFiles"] = []
                    attempt["patchDiffLines"] = 0
                    if data["status"] != "COMPLETED":
                        record_failure(
                            attempt,
                            code="producer_not_completed",
                            reason=f"producer returned {data['status']}",
                            failure_kind="clarification",
                        )
                        continue
                    if patch_path:
                        patch = inspect_patch(cwd, patch_path, f"result {expected_id}.patchPath", patch_bytes_max)
                    else:
                        fail(attempt["ticket"]["write"], "artifact_missing", f"writer result {expected_id} has no patch artifact")
                        patch = {"artifact": None, "data": b"", "files": [], "diffLines": 0}
                    patch["attemptId"] = expected_id
                    validate_patch_scope(cwd, patch, attempt)
                    validate_patch_applicability(cwd, patch, f"result {expected_id}.patchPath")
                    declared_changed_files = sorted(
                        normalize_observed_repo_path(path, f"result {expected_id}.changedFiles")
                        for path in data["changedFiles"]
                    )
                    attempt["changedFiles"] = declared_changed_files
                    fail(
                        len(declared_changed_files) != len(set(declared_changed_files)),
                        "changed_files_mismatch",
                        f"producer {expected_id} declared duplicate changedFiles",
                    )
                    fail(
                        declared_changed_files != patch["files"],
                        "changed_files_mismatch",
                        f"producer {expected_id} changedFiles differ from its patch",
                        expected=patch["files"],
                        observed=declared_changed_files,
                    )
                    attempt["status"] = "completed"
                    attempt["patchArtifact"] = patch["artifact"]
                    attempt["patchFiles"] = patch["files"]
                    attempt["patchDiffLines"] = patch["diffLines"]
                    producer_patches.append(patch)
                    successful_producer_ids.append(expected_id)
                else:
                    validate_lens_result(data, attempt)
                    attempt["status"] = "completed"
                    attempt["result"] = data
            except RuntimeFailure as exc:
                if exc.code in {"state_corrupt", "subprocess_timeout"}:
                    raise
                record_failure(attempt, code=exc.code, reason=exc.message, failure_kind="invalid")

        pending["status"] = "settled"
        pending["toolCallId"] = tool_call_id
        pending["executedInputHash"] = digest(executed_input)
        state["tokensSpent"] = int(state.get("tokensSpent", 0)) + observed_tokens
        projection = int(pending.get("projection", 0))
        if projection <= 0:
            projection_key = "producer_per_ticket" if kind == "producer" else "lens_per_ticket"
            projection_each = int(config["omp"].get("budget_projection", {}).get(projection_key, 0))
            projection = projection_each * len(results)
        state["projectedTokens"] = max(0, int(state.get("projectedTokens", 0)) - projection)

        if kind == "producer":
            wave = current_wave(state)
            wave["candidateAttemptIds"] = list(successful_producer_ids)
            wave["failedAttemptIds"] = list(failed_producer_ids)
            wave["failedTicketIds"] = sorted(
                {str(collection[attempt_id]["ticketId"]) for attempt_id in failed_producer_ids}
            )
            if availability_ticket_ids:
                increment_ticket_failures(state, "availabilityFailures", availability_ticket_ids)
            if quality_ticket_ids:
                increment_ticket_failures(state, "qualityFailures", quality_ticket_ids)
            set_retry_ticket_ids(state, set(wave["failedTicketIds"]))

            if successful_producer_ids:
                # Partial success: the wave moves on, so the failed siblings
                # never reach the explicit `retry` transition. Route them here
                # by their own recorded failure kind, or their next attempt
                # repeats the identical dispatch and burns the retry budget.
                if failed_producer_ids:
                    failed_by_ticket = {
                        str(collection[attempt_id]["ticketId"]): collection[attempt_id]
                        for attempt_id in failed_producer_ids
                    }
                    route_ticket_retry(state, failed_by_ticket, "availability")
                    route_ticket_retry(state, failed_by_ticket, "capability")
                state_path, _lock_path = state_paths(cwd, explicit_state_dir, state["runId"])
                applied_patch = apply_patch_batch(
                    cwd,
                    producer_patches,
                    state_path.with_suffix(".artifacts"),
                    run_id=str(state["runId"]),
                    dispatch_id=dispatch_id,
                )
                for applied_attempt in applied_patch["attemptPatches"]:
                    collection[applied_attempt["attemptId"]]["appliedPatch"] = applied_attempt
                pending["appliedPatch"] = applied_patch
                wave["appliedPatch"] = applied_patch
                wave["status"] = "candidates_settled"
                state["phase"] = "pregate_pending"
                state["blockedReason"] = None
            else:
                wave["status"] = "repair_pending"
                state["phase"] = "repair_pending"
                state["blockedReason"] = "; ".join(failure_reasons) or "no producer attempt completed"
                state["lastFailureKind"] = "availability" if availability_ticket_ids else "clarification"
            return

        wave = current_wave(state)
        completed_lenses = latest_completed_lens_attempts(state, require_text(wave.get("waveId"), "wave.waveId"))
        missing_lenses = set(LENS_NAMES) - set(completed_lenses)
        if not failed_lenses and not missing_lenses:
            for lens, reason in observed_reviewer_collisions(state, completed_lenses).items():
                record_failure(
                    completed_lenses[lens],
                    code="independent_reviewer_unavailable",
                    reason=reason,
                    failure_kind="invalid",
                )
        if failed_lenses:
            schedule_lens_retry(
                cwd,
                state,
                config,
                failed_lenses,
                "; ".join(failure_reasons) or "one or more lens reports could not be settled",
            )
            return
        if missing_lenses:
            schedule_lens_retry(
                cwd,
                state,
                config,
                missing_lenses,
                f"wave is missing completed lens reports: {', '.join(ordered_lens_names(missing_lenses))}",
            )
            return
        set_retry_lens_names(state, set())
        state["phase"] = "adjudication_pending"
        state["blockedReason"] = None

    state, _ = mutate(cwd, explicit_state_dir, request, apply)
    return output(state)


def command_record_evidence(cwd: Path, explicit_state_dir: str | None, request: dict[str, Any]) -> dict[str, Any]:
    def apply(state: dict[str, Any]) -> None:
        fail(state["phase"] != "pregate_pending", "illegal_transition", "host UI evidence is accepted only before the deterministic pre-gate")
        attempt_ids = request.get("attemptIds")
        fail(
            not isinstance(attempt_ids, list)
            or len(attempt_ids) != 1
            or not isinstance(attempt_ids[0], str)
            or attempt_ids[0] not in state["attempts"],
            "evidence_invalid",
            "host UI evidence must name exactly one existing producer attemptId",
        )
        token = require_text(request.get("challengeToken"), "challengeToken")
        challenge = state.get("evidenceChallenges", {}).get(token)
        fail(not isinstance(challenge, dict), "evidence_invalid", "host UI evidence does not match an active challenge token")
        fail(challenge.get("attemptId") != attempt_ids[0], "evidence_invalid", "host UI evidence challenge is bound to another attempt")
        stage = require_text(request.get("stage"), "stage")
        fail(stage not in challenge.get("requiredStages", []), "evidence_invalid", f"unsupported UI evidence stage: {stage}")
        invocation = require_mapping(request.get("invocation"), "invocation")
        fail(invocation.get("name") != token, "evidence_invalid", "host UI invocation is not bound to the challenge token")
        action = invocation.get("action")
        witness: dict[str, Any] | None = None
        if stage == "open":
            fail("witness" in request, "evidence_invalid", "UI open evidence must not include a witness")
            app = invocation.get("app") if isinstance(invocation.get("app"), dict) else {}
            observed_target = invocation.get("url") or app.get("target")
            fail(action != "open" or observed_target != challenge.get("target"), "evidence_invalid", "UI open evidence must use the exact issued target")
        else:
            fail(action != "run" or "code" in invocation, "evidence_invalid", "UI witness invocation must be declarative")
            witness = normalize_ui_witness(request.get("witness"))
            fail(witness["attemptId"] != attempt_ids[0], "evidence_invalid", "witness is bound to another attempt")
            fail(witness["challengeToken"] != token, "evidence_invalid", "witness is bound to another challenge token")
            fail(witness["criterion"] != challenge.get("criterion"), "evidence_invalid", "witness is bound to another criterion")
            probe_hash = digest(witness["probe"])
            fail(witness["probeHash"] != probe_hash, "evidence_invalid", "witness probeHash does not match the canonical probe")
            witness_id = digest({
                "attemptId": witness["attemptId"],
                "challengeToken": witness["challengeToken"],
                "criterion": witness["criterion"],
                "probeHash": probe_hash,
                "runId": state["runId"],
                "version": witness["version"],
            })
            fail(witness["witnessId"] != witness_id, "evidence_invalid", "witnessId does not match the current challenge binding")
        completed = {
            record.get("stage")
            for record in state["evidence"]
            if record.get("challengeToken") == token
        }
        fail(stage in completed, "evidence_invalid", f"UI evidence stage {stage} was already recorded")
        if stage == "witness":
            fail("open" not in completed, "evidence_invalid", "UI witness evidence requires a recorded open stage")
        record = {
            "toolCallId": require_text(request.get("toolCallId"), "toolCallId"),
            "tool": require_text(request.get("tool"), "tool"),
            "success": request.get("success") is True,
            "details": request.get("details"),
            "content": request.get("content"),
            "attemptIds": attempt_ids,
            "challengeToken": token,
            "stage": stage,
            "recordedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
        }
        if witness is not None:
            record["witness"] = witness
        fail(record["tool"] not in {"browser", "xdev"}, "evidence_invalid", "only host browser results satisfy a UI challenge")
        fail(not record["success"], "evidence_invalid", "only successful host-observed tool results are evidence")
        state["evidence"].append(record)

    state, _ = mutate(cwd, explicit_state_dir, request, apply)
    return output(state)


def verification_cwd(repo: Path, value: str) -> Path:
    fail("\\" in value or "\x00" in value, "ticket_invalid", "verification cwd must use a POSIX repository path")
    if value == ".":
        candidate = repo.resolve()
    else:
        path = PurePosixPath(value)
        fail(path.is_absolute() or path.as_posix() != value or any(part in {"", ".", ".."} for part in path.parts), "ticket_invalid", "verification cwd must be normalized and repository-relative")
        candidate = (repo / value).resolve()
    try:
        candidate.relative_to(repo.resolve())
    except ValueError as exc:
        raise RuntimeFailure("ticket_invalid", "verification cwd resolves outside the repository") from exc
    fail(not candidate.is_dir(), "pregate_failed", f"verification cwd is not a directory: {value}")
    return candidate


def bounded_process_output(stdout: str | bytes | None, stderr: str | bytes | None, max_bytes: int) -> str:
    def encoded(value: str | bytes | None) -> bytes:
        if value is None:
            return b""
        return value if isinstance(value, bytes) else value.encode("utf-8", errors="replace")

    return (encoded(stdout) + encoded(stderr))[:max_bytes].decode("utf-8", errors="replace")


def verification_environment() -> dict[str, str]:
    allowed = ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "TERM", "CI", "XDG_CACHE_HOME", "VIRTUAL_ENV")
    environment = {key: os.environ[key] for key in allowed if os.environ.get(key)}
    environment.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    return environment


def run_verification(repo: Path, command: dict[str, Any], timeout_default: int, max_bytes: int) -> dict[str, Any]:
    argv = command["argv"]
    timeout = min(command.get("timeoutSeconds") or timeout_default, timeout_default)
    command_cwd = verification_cwd(repo, command["cwd"])
    try:
        completed = subprocess.run(argv, cwd=command_cwd, env=verification_environment(), text=True, capture_output=True, timeout=timeout, check=False)
        return {
            "argv": argv,
            "cwd": command["cwd"],
            "exitCode": completed.returncode,
            "output": bounded_process_output(completed.stdout, completed.stderr, max_bytes),
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "argv": argv,
            "cwd": command["cwd"],
            "exitCode": None,
            "timeout": True,
            "output": bounded_process_output(exc.stdout, exc.stderr, max_bytes),
        }


def run_diff_check(cwd: Path, files: list[str], max_bytes: int) -> dict[str, Any]:
    if not files:
        return {"argv": ["git", "diff", "--check", "--"], "files": [], "exitCode": 0, "output": ""}
    argv = ["git", "diff", "--check", "--", *files]
    try:
        completed = subprocess.run(argv, cwd=cwd, text=True, capture_output=True, check=False, timeout=GIT_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        return {
            "argv": argv,
            "files": files,
            "exitCode": None,
            "timeout": True,
            "output": bounded_process_output(exc.stdout, exc.stderr, max_bytes),
        }
    return {
        "argv": argv,
        "files": files,
        "exitCode": completed.returncode,
        "output": bounded_process_output(completed.stdout, completed.stderr, max_bytes),
    }


def command_pregate(cwd: Path, explicit_state_dir: str | None, request: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    def apply(state: dict[str, Any]) -> None:
        require_run_config(state, config)
        fail(state["phase"] != "pregate_pending", "illegal_transition", f"pregate is not legal in phase {state['phase']}")
        wave = current_wave(state)
        candidate_ids = wave_attempt_ids(state, wave, "candidateAttemptIds")
        fail(not candidate_ids, "pregate_failed", "current wave has no completed producer candidates")
        attempts = [state["attempts"][attempt_id] for attempt_id in candidate_ids]
        fail(any(attempt.get("status") != "completed" for attempt in attempts), "pregate_failed", "a pregate candidate is not completed")

        timeout_default = int(config["omp"].get("pre_gate", {}).get("command_timeout_seconds", 600))
        max_bytes = int(config["omp"].get("pre_gate", {}).get("max_result_bytes", 262144))
        configured_max = int(config.get("gates", {}).get("pre_gate", {}).get("max_diff_lines", 1200))
        checks: list[dict[str, Any]] = []
        failures: list[str] = []
        failed_attempt_ids: set[str] = set()

        for attempt in attempts:
            for command in attempt["ticket"]["verification"]:
                check = run_verification(cwd, command, timeout_default, max_bytes)
                check["attemptId"] = attempt["attemptId"]
                checks.append(check)
                if check.get("exitCode") != 0:
                    failures.append(f"verification failed for {attempt['ticketId']}: {shlex.join(command['argv'])}")
                    failed_attempt_ids.add(attempt["attemptId"])

            measured = int(attempt.get("patchDiffLines", 0))
            override = attempt["ticket"].get("max_diff_lines_override")
            if override is None:
                allowed = configured_max
            else:
                fail(
                    not isinstance(override, int) or isinstance(override, bool) or override < configured_max,
                    "pregate_failed",
                    f"ticket {attempt['ticketId']} diff override must be an integer at least as large as the configured ceiling",
                )
                allowed = override
            checks.append({"attemptId": attempt["attemptId"], "kind": "diff-ceiling", "diffLines": measured, "diffLimit": allowed})
            if measured > allowed:
                failures.append(f"diff ceiling exceeded for {attempt['ticketId']}: {measured} > {allowed}")
                failed_attempt_ids.add(attempt["attemptId"])

            diff_check = run_diff_check(cwd, list(attempt.get("patchFiles", [])), max_bytes)
            diff_check["attemptId"] = attempt["attemptId"]
            diff_check["kind"] = "git-diff-check"
            checks.append(diff_check)
            if diff_check["exitCode"] != 0:
                failures.append(f"git diff --check failed for {attempt['ticketId']}")
                failed_attempt_ids.add(attempt["attemptId"])

            challenge = attempt.get("evidenceChallenge")
            if not isinstance(challenge, dict):
                continue
            evidence_records = [
                record
                for record in state["evidence"]
                if record.get("challengeToken") == challenge["token"]
                and record.get("attemptIds") == [attempt["attemptId"]]
            ]
            completed = {record.get("stage") for record in evidence_records}
            missing = sorted(set(challenge["requiredStages"]) - completed)
            checks.append({
                "attemptId": attempt["attemptId"],
                "kind": "ui-evidence",
                "challengeToken": challenge["token"],
                "completedStages": sorted(stage for stage in completed if isinstance(stage, str)),
                "target": challenge["target"],
                "criterion": challenge["criterion"],
                "records": [
                    bounded_process_output(
                        canonical({
                            "stage": record.get("stage"),
                            "tool": record.get("tool"),
                            "toolCallId": record.get("toolCallId"),
                            "witness": record.get("witness"),
                            "details": record.get("details"),
                            "content": record.get("content"),
                        }),
                        None,
                        max_bytes,
                    )
                    for record in evidence_records
                ],
                "missingStages": missing,
            })
            if missing:
                failures.append(f"live UI evidence missing for {attempt['ticketId']}: {', '.join(missing)}")
                failed_attempt_ids.add(attempt["attemptId"])

        passed_attempt_ids = [attempt_id for attempt_id in candidate_ids if attempt_id not in failed_attempt_ids]
        failed_attempt_ids_ordered = [attempt_id for attempt_id in candidate_ids if attempt_id in failed_attempt_ids]
        status = "PASS" if not failed_attempt_ids else "PARTIAL" if passed_attempt_ids else "FAIL"
        state["pregate"] = {
            "status": status,
            "candidateAttemptIds": candidate_ids,
            "passedAttemptIds": passed_attempt_ids,
            "failedAttemptIds": failed_attempt_ids_ordered,
            "checks": checks,
            "diffLines": sum(int(attempt.get("patchDiffLines", 0)) for attempt in attempts),
            "diffLimitPerTicket": configured_max,
            "failures": failures,
        }
        wave["pregatePassedAttemptIds"] = passed_attempt_ids
        wave["pregateFailedAttemptIds"] = failed_attempt_ids_ordered
        if failed_attempt_ids:
            rollback_attempt_patches(cwd, state, set(failed_attempt_ids))
            failed_tickets = set(retry_ticket_ids(state))
            for attempt in attempts:
                if attempt["attemptId"] not in failed_attempt_ids:
                    continue
                attempt["status"] = "pregate_failed"
                failed_tickets.add(str(attempt["ticketId"]))
                increment_ticket_failures(state, "qualityFailures", [str(attempt["ticketId"])])
            set_retry_ticket_ids(state, failed_tickets)

        if passed_attempt_ids:
            if failed_attempt_ids:
                # The wave proceeds to the lenses on its passing subset, so the
                # failed tickets skip the explicit `retry` transition. A failed
                # pre-gate is a capability verdict, so route them on that axis.
                route_ticket_retry(
                    state,
                    {
                        str(attempt["ticketId"]): attempt
                        for attempt in attempts
                        if attempt["attemptId"] in failed_attempt_ids
                    },
                    "capability",
                )
            wave["status"] = "pregate_passed" if not failed_attempt_ids else "pregate_partial"
            state["phase"] = "lens_prepare_pending"
            state["blockedReason"] = None
        else:
            wave["status"] = "repair_pending"
            state["phase"] = "repair_pending"
            state["blockedReason"] = "; ".join(failures) or "all producer candidates failed the deterministic pre-gate"
            state["lastFailureKind"] = "effort"

    state, _ = mutate(cwd, explicit_state_dir, request, apply)
    return output(state)


def select_wave_reviewers(
    state: dict[str, Any],
    config: dict[str, Any],
    lenses: list[str],
) -> dict[str, dict[str, Any]]:
    """Bind each lens to its own declared slot.

    No search and no independence arithmetic: `omp.lenses.slots` and
    `omp.producers` name disjoint slot sets, and `validate_slot_disjointness`
    proves it once at config load. Which model is behind a slot, and what
    replaces it when that model is unavailable, is OMP's business.
    """
    fail(not lenses or len(lenses) != len(set(lenses)) or any(lens not in LENS_NAMES for lens in lenses), "state_corrupt", "lens dispatch has invalid names")
    lens_config = config["omp"].get("lenses", {})
    capability = lens_config.get("capability")
    fail(not isinstance(capability, str) or not capability, "config_invalid", "omp.lenses declares no reviewer capability")
    mapping = lens_config.get("slots", {})
    chosen: dict[str, dict[str, Any]] = {}
    for lens in lenses:
        picked = slot_for(mapping, lens, f"omp.lenses.slots.{lens}")
        bound = bind_slot(state, config, capability, picked["slot"])
        bound["definition"] = picked["definition"]
        chosen[lens] = bound
    return chosen


def observed_attempt_model(attempt: dict[str, Any], label: str) -> str:
    """Return the host-observed model used by a successfully settled attempt."""
    observed = nullable_text(attempt.get("observedModel"))
    fail(observed is None, "model_witness_missing", f"{label} has no observed model witness")
    return model_base(observed)


def assert_reviewers_are_independent(
    chosen: dict[str, dict[str, Any]],
    producers: list[dict[str, Any]],
) -> None:
    """Refuse a wave with a known producer/reviewer or reviewer collision.

    Disjoint slots guarantee different roles, not different models: the owner
    may point two roles at one model. Producer results have already settled, so
    their observed witnesses are authoritative. Reviewer witnesses are the
    models OMP resolves before dispatch; runtime fallback is checked again when
    the reviewer results settle.
    """
    producing = {
        observed_attempt_model(producer, f"producer {producer.get('attemptId', 'unknown')}")
        for producer in producers
    }
    reviewer_models = {
        lens: model_base(str(bound["witness"]["resolvedModel"]))
        for lens, bound in chosen.items()
    }
    producer_collisions = sorted(
        f"{lens} ({bound['witness']['resolvedModel']})"
        for lens, bound in chosen.items()
        if reviewer_models[lens] in producing
    )
    reviewers_by_model: dict[str, list[str]] = {}
    for lens, model in reviewer_models.items():
        reviewers_by_model.setdefault(model, []).append(lens)
    reviewer_collisions = sorted(
        f"{', '.join(sorted(lenses))} ({model})"
        for model, lenses in reviewers_by_model.items()
        if len(lenses) > 1
    )
    collisions = producer_collisions + reviewer_collisions
    fail(
        bool(collisions),
        "independent_reviewer_unavailable",
        "reviewer model independence is unavailable: " + "; ".join(collisions),
    )


def observed_reviewer_collisions(
    state: dict[str, Any],
    completed_lenses: dict[str, dict[str, Any]],
) -> dict[str, str]:
    """Return lens-specific failures after OMP has applied runtime fallbacks."""
    producer_models: set[str] = set()
    for attempt in completed_lenses.values():
        for producer_attempt_id in attempt["producerAttemptIds"]:
            producer = state["attempts"].get(producer_attempt_id)
            fail(not isinstance(producer, dict), "state_corrupt", f"lens references unknown producer {producer_attempt_id}")
            producer_models.add(observed_attempt_model(producer, f"producer {producer_attempt_id}"))

    reviewers_by_model: dict[str, list[str]] = {}
    for lens, attempt in completed_lenses.items():
        model = observed_attempt_model(attempt, f"lens {lens}")
        reviewers_by_model.setdefault(model, []).append(lens)

    failures: dict[str, str] = {}
    for model, lenses in reviewers_by_model.items():
        if model in producer_models:
            for lens in lenses:
                failures[lens] = f"lens {lens} used producer model {model} after runtime fallback"
        if len(lenses) > 1:
            names = ", ".join(sorted(lenses))
            for lens in lenses:
                failures[lens] = f"lenses {names} used the same model {model} after runtime fallback"
    return failures


def lens_task_text(lens: str, producers: list[dict[str, Any]], state: dict[str, Any]) -> str:
    bindings = []
    for producer in producers:
        artifact = producer.get("artifact")
        fail(not isinstance(artifact, dict), "state_corrupt", f"pregate producer {producer['attemptId']} lacks a disk artifact")
        bindings.append({
            "attemptId": producer["attemptId"],
            "artifact": {
                "path": artifact.get("path"),
                "sha256": artifact.get("sha256"),
            },
            "objective": producer["ticket"]["OBJECTIVE"],
            "inputs": producer["ticket"]["INPUTS"],
            "boundaries": producer["ticket"]["BOUNDARIES"],
            "acceptance": producer["ticket"]["ACCEPTANCE"],
        })
    return (
        f"LENS: {lens}\n"
        f"WAVE_PRODUCERS: {canonical(bindings)}\n"
        f"PREGATE: {canonical(state.get('pregate', {}))}\n\n"
        "Inspect the repository and every listed disk artifact yourself. Return exactly "
        "{lens, summary, reports:[{attemptId, summary, findings, verdict}]}; reports must cover "
        "every listed producer attempt exactly once. Standards and Spec emit NO_VERDICT in every "
        "report; Critic emits PASS or FAIL in every report. Every finding must identify scope and "
        "concrete evidence. Do not modify files."
    )


def command_prepare_lenses(cwd: Path, explicit_state_dir: str | None, request: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    def apply(state: dict[str, Any]) -> None:
        require_run_config(state, config)
        fail(state["phase"] != "lens_prepare_pending", "illegal_transition", f"prepare-lenses is not legal in phase {state['phase']}")
        pregate = state.get("pregate")
        fail(
            not isinstance(pregate, dict) or pregate.get("status") not in {"PASS", "PARTIAL"},
            "pregate_required",
            "lenses require at least one producer that passed the deterministic pre-gate",
        )
        wave = current_wave(state)
        candidate_ids = wave_attempt_ids(state, wave, "candidateAttemptIds")
        passed_ids = pregate.get("passedAttemptIds")
        fail(
            not isinstance(passed_ids, list)
            or not passed_ids
            or any(not isinstance(attempt_id, str) for attempt_id in passed_ids)
            or len(passed_ids) != len(set(passed_ids))
            or not set(passed_ids).issubset(set(candidate_ids)),
            "pregate_required",
            "pregate does not seal a valid passed producer subset",
        )
        producers = [state["attempts"][attempt_id] for attempt_id in passed_ids]
        fail(any(producer.get("status") != "completed" for producer in producers), "pregate_required", "a passed producer is no longer completed")
        configured_names = config["omp"].get("lenses", {}).get("names")
        fail(
            not isinstance(configured_names, list) or tuple(configured_names) != LENS_NAMES,
            "config_invalid",
            "OMP lenses.names must define the fixed Standards, Spec, Critic wave gate",
        )
        requested_retry_names = retry_lens_names(state)
        lens_names = ordered_lens_names(requested_retry_names) if requested_retry_names else list(LENS_NAMES)
        wave_id = require_text(wave.get("waveId"), "wave.waveId")
        projection_each = int(config["omp"].get("budget_projection", {}).get("lens_per_ticket", 0))
        projection = projection_each * len(lens_names) * len(producers)
        budget_check(state, config, projection)
        capability = config["omp"].get("lenses", {}).get("capability")
        selected_reviewers = select_wave_reviewers(state, config, lens_names)
        assert_reviewers_are_independent(selected_reviewers, producers)

        items = []
        lens_ids = []
        reviewer_slots = wave.setdefault("reviewerSlots", {})
        for index, lens in enumerate(lens_names):
            selected = selected_reviewers[lens]
            review_no = lens_attempt_count(state, wave_id, lens) + 1
            lens_id = f"{state['runId']}.{wave_id}.lens.{lens.lower()}.a{review_no}.{uuid.uuid4().hex[:6]}"
            dispatch_name = f"{wave_id}.L{index + 1}.{lens}.A{review_no}"
            assignment = {
                "attemptId": lens_id,
                "dispatchName": dispatch_name,
                "waveId": wave_id,
                "producerAttemptIds": list(passed_ids),
                "ticketIds": [producer["ticketId"] for producer in producers],
                "lens": lens,
                "role": capability,
                "reviewAttemptOrdinal": review_no,
                "attemptOrdinal": review_no,
                "slot": selected["slot"],
                "slotRole": config["omp"]["slots"][selected["slot"]]["alias"],
                "agent": selected["agent"],
                "declaredAgent": selected["agent"],
                "declaredModel": selected["witness"]["resolvedModel"],
                "status": "prepared",
                "tokens": None,
                "observedAgent": None,
                "observedAgentSource": None,
                "observedModel": None,
                "modelFallback": None,
            }
            state["lensAttempts"][lens_id] = assignment
            lens_ids.append(lens_id)
            reviewer_slots[lens] = selected["slot"]
            items.append({
                "name": dispatch_name,
                "agent": selected["agent"],
                "task": lens_task_text(lens, producers, state),
                "effort": require_text(selected["definition"].get("effort"), f"omp.lenses.slots.{lens}.effort"),
                "outputSchema": lens_schema(),
                "schemaMode": "strict",
                "isolated": True,
            })

        task_input = {
            "context": f"Pocock fixed three-lens wave gate for run {state['runId']}. Reports are producer-attempt-bound and read-only.",
            "tasks": items,
        }
        state["pendingDispatch"] = {
            "dispatchId": f"dispatch-{uuid.uuid4().hex}",
            "kind": "lenses",
            "status": "prepared",
            "attemptIds": lens_ids,
            "lensNames": lens_names,
            "producerAttemptIds": list(passed_ids),
            "waveId": wave_id,
            "taskInput": task_input,
            "inputHash": digest(task_input),
            "createdAt": dt.datetime.now(dt.timezone.utc).isoformat(),
            "projection": projection,
        }
        state["projectedTokens"] = int(state.get("projectedTokens", 0)) + projection
        state["phase"] = "lens_dispatch_pending"
        state["blockedReason"] = None

    state, _ = mutate(cwd, explicit_state_dir, request, apply)
    return output(state)


def command_adjudicate(cwd: Path, explicit_state_dir: str | None, request: dict[str, Any]) -> dict[str, Any]:
    def apply(state: dict[str, Any]) -> None:
        fail(state["phase"] != "adjudication_pending", "illegal_transition", f"adjudicate is not legal in phase {state['phase']}")
        wave = current_wave(state)
        pregate = state.get("pregate")
        fail(isinstance(pregate, dict) is False, "lens_result_invalid", "adjudication has no pregate record")
        passed_ids = pregate.get("passedAttemptIds")
        fail(
            not isinstance(passed_ids, list) or not passed_ids or len(passed_ids) != len(set(passed_ids)),
            "lens_result_invalid",
            "adjudication has no sealed pregate-passed producer subset",
        )
        wave_id = require_text(wave.get("waveId"), "wave.waveId")
        lens_attempts = latest_completed_lens_attempts(state, wave_id)
        fail(
            set(lens_attempts) != set(LENS_NAMES),
            "lens_result_invalid",
            "adjudication requires the latest completed report from every fixed wave lens",
        )
        grouped: dict[str, dict[str, dict[str, Any]]] = {producer_id: {} for producer_id in passed_ids}
        for lens in LENS_NAMES:
            assignment = lens_attempts[lens]
            report = assignment.get("result")
            fail(not isinstance(report, dict), "lens_result_invalid", f"lens {assignment['attemptId']} has no structured report")
            validate_lens_result(report, assignment)
            for producer_report in report["reports"]:
                grouped[producer_report["attemptId"]][lens] = producer_report
            assignment["status"] = "accepted"

        failures = []
        failed_producers: set[str] = set()
        for producer_id in passed_ids:
            reports = grouped[producer_id]
            fail(set(reports) != set(LENS_NAMES), "lens_result_invalid", f"producer {producer_id} lacks the fixed three lenses")
            if reports["Critic"]["verdict"] != "PASS":
                failures.append(f"Critic FAIL for {producer_id}")
                failed_producers.add(producer_id)
            for lens in ("Standards", "Spec"):
                for finding in reports[lens]["findings"]:
                    if finding["blocking"] is True and finding["scope"] == "introduced":
                        failures.append(f"blocking {lens} finding for {producer_id}: {finding['evidence']}")
                        failed_producers.add(producer_id)

        accepted_producers = [producer_id for producer_id in passed_ids if producer_id not in failed_producers]
        retry_tickets = retry_ticket_ids(state)
        if failed_producers:
            rollback_attempt_patches(cwd, state, failed_producers)
        for producer_id in passed_ids:
            producer = state["attempts"][producer_id]
            if producer_id in failed_producers:
                producer["status"] = "review_failed"
                retry_tickets.add(str(producer["ticketId"]))
                increment_ticket_failures(state, "qualityFailures", [str(producer["ticketId"])])
            else:
                producer["status"] = "accepted"
        set_retry_ticket_ids(state, retry_tickets)
        set_retry_lens_names(state, set())
        wave["acceptedAttemptIds"] = accepted_producers
        wave["reviewFailedAttemptIds"] = [producer_id for producer_id in passed_ids if producer_id in failed_producers]
        adjudication_status = "PASS" if not retry_tickets else "PARTIAL" if accepted_producers else "FAIL"
        state["adjudication"] = {
            "status": adjudication_status,
            "failures": failures,
            "lensAttemptIds": [lens_attempts[lens]["attemptId"] for lens in LENS_NAMES],
            "acceptedAttemptIds": accepted_producers,
            "failedAttemptIds": [producer_id for producer_id in passed_ids if producer_id in failed_producers],
            "retryTicketIds": sorted(retry_tickets),
        }
        if accepted_producers:
            wave["status"] = "accepted" if not retry_tickets else "partial_acceptance"
            state["phase"] = "accepted"
            state["blockedReason"] = None
        else:
            wave["status"] = "repair_pending"
            state["phase"] = "repair_pending"
            state["blockedReason"] = "; ".join(failures) or "every pregate-passed producer failed adjudication"
            state["lastFailureKind"] = "capability"

    state, _ = mutate(cwd, explicit_state_dir, request, apply)
    return output(state)


def telemetry_log_path(config: dict[str, Any]) -> Path:
    return SKILL_DIR / config.get("telemetry", {}).get("log", "telemetry/routing-log.jsonl")


def recorded_telemetry_keys(config: dict[str, Any], run_id: str) -> set[tuple[str, str]]:
    """One scan of the routing log yields every (run, ticket) PASS key.

    Acceptance records K tickets per wave; the old per-ticket
    `telemetry_exists` re-read the whole log for each of them (O(K*L)). The
    set below is built once per accept command.
    """
    path = telemetry_log_path(config)
    if not path.is_file():
        return set()
    keys: set[tuple[str, str]] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            raise RuntimeFailure("telemetry_corrupt", f"invalid JSONL in {path}")
        if isinstance(row, dict) and row.get("run_id") == run_id and row.get("verdict") == "PASS":
            keys.add((run_id, str(row.get("ticket"))))
    return keys


def telemetry_exists(config: dict[str, Any], run_id: str, ticket_id_value: str) -> bool:
    return (run_id, ticket_id_value) in recorded_telemetry_keys(config, run_id)


def command_accept(cwd: Path, explicit_state_dir: str | None, request: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    telemetry_events: list[dict[str, Any]] = []

    def apply(state: dict[str, Any]) -> None:
        require_run_config(state, config)
        fail(state["phase"] != "accepted" or state.get("telemetryRecorded"), "illegal_transition", "accept requires an unrecorded accepted adjudication")
        sweep = require_sweep_integrity(state) if state.get("entry") == "sweep" else None
        wave = current_wave(state)
        accepted_attempt_ids = wave.get("acceptedAttemptIds")
        fail(
            not isinstance(accepted_attempt_ids, list)
            or not accepted_attempt_ids
            or len(accepted_attempt_ids) != len(set(accepted_attempt_ids))
            or any(not isinstance(attempt_id, str) or attempt_id not in state["attempts"] for attempt_id in accepted_attempt_ids),
            "acceptance_invalid",
            "current adjudication has no valid accepted producer subset",
        )
        accepted = [state["attempts"][attempt_id] for attempt_id in accepted_attempt_ids]
        fail(any(attempt.get("status") != "accepted" for attempt in accepted), "acceptance_invalid", "accepted producer subset drifted before acceptance")
        accepted_ticket_ids_now = [str(attempt["ticketId"]) for attempt in accepted]
        if sweep is not None:
            accepted_set = set(sweep["acceptedTicketIds"])
            fail(
                len(accepted_ticket_ids_now) != len(set(accepted_ticket_ids_now)),
                "sweep_state_invalid",
                "more than one accepted attempt is bound to the same sealed ticket",
            )
            fail(
                not set(accepted_ticket_ids_now).issubset(set(sweep["remainingTicketIds"])),
                "sweep_progress_drift",
                "accepted attempt is outside the remaining sealed sweep ledger",
            )
            for ticket_id_value in accepted_ticket_ids_now:
                fail(
                    not set(sweep["dependencies"][ticket_id_value]).issubset(accepted_set),
                    "sweep_scheduler_invalid",
                    f"accepted ticket {ticket_id_value} was not ready under the sealed DAG",
                )

        shape = telemetry_shape_for_entry(config, state["entry"])
        budget_exhausted = False
        already_recorded = recorded_telemetry_keys(config, state["runId"])
        for attempt in accepted:
            ticket = str(attempt["ticketId"])
            row: dict[str, Any] = {
                "date": dt.date.today().isoformat(),
                "task": state["objective"],
                "ticket": ticket,
                "class": attempt["class"],
                "agent": attempt["agent"],
                "model": nullable_text(attempt.get("observedModel")),
                "verdict": "PASS",
                "tokens": displayed_tokens(attempt.get("tokens")),
                "entry": state["entry"],
                "run_id": state["runId"],
                "config": state["configFingerprint"],
                "attempt": attempt["qualityAttempt"],
            }
            if shape is not None:
                row["shape"] = shape
            if sweep is not None:
                row["ledger_hash"] = sweep["ledgerHash"]
                row["dag_hash"] = sweep["dagHash"]
            if (state["runId"], ticket) in already_recorded:
                attempt["status"] = "recorded"
                continue
            try:
                completed = subprocess.run(
                    [sys.executable, str(TELEMETRY_TOOL), canonical(row), "--skill-dir", str(SKILL_DIR), "--run-id", state["runId"]],
                    text=True,
                    capture_output=True,
                    check=False,
                    timeout=TELEMETRY_TIMEOUT_SECONDS,
                )
            except subprocess.TimeoutExpired as exc:
                raise RuntimeFailure("telemetry_failed", f"telemetry writer exceeded {TELEMETRY_TIMEOUT_SECONDS}s") from exc
            if completed.returncode not in {0, 2}:
                raise RuntimeFailure("telemetry_failed", completed.stderr.strip() or "telemetry writer rejected an acceptance record")
            telemetry_events.append({"ticket": ticket, "exitCode": completed.returncode, "stdout": completed.stdout.strip()})
            attempt["status"] = "recorded"
            if completed.returncode == 2:
                budget_exhausted = True
        # Recompute the canonical run budget even when every PASS row was
        # deduplicated after a prior writer timed out post-append. Exit code 2
        # is part of the persisted budget verdict, not merely this process's
        # transient writer result.
        try:
            budget_check = subprocess.run(
                [sys.executable, str(TELEMETRY_TOOL), "--check-only", "--skill-dir", str(SKILL_DIR), "--run-id", state["runId"]],
                text=True,
                capture_output=True,
                check=False,
                timeout=TELEMETRY_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeFailure("telemetry_failed", f"telemetry budget check exceeded {TELEMETRY_TIMEOUT_SECONDS}s") from exc
        if budget_check.returncode not in {0, 2}:
            raise RuntimeFailure("telemetry_failed", budget_check.stderr.strip() or "telemetry writer could not reconstruct the run budget")
        budget_exhausted = budget_check.returncode == 2

        if sweep is not None:
            sweep["acceptedTicketIds"] = sorted(set(sweep["acceptedTicketIds"]) | set(accepted_ticket_ids_now))
            set_sweep_progress(sweep)
            state["frontierExhausted"] = not sweep["remainingTicketIds"]
        state["telemetryRecorded"] = True
        state["budgetExhausted"] = budget_exhausted
        state["telemetryEvents"] = telemetry_events

    state, _ = mutate(cwd, explicit_state_dir, request, apply)
    return output(state, telemetry=telemetry_events)




def command_status(
    cwd: Path,
    explicit_state_dir: str | None,
    request: dict[str, Any],
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    requested_run_id = request.get("runId")
    if requested_run_id is None:
        with with_lock(start_lock_path(cwd, explicit_state_dir)):
            recover_replacement_transactions(cwd, explicit_state_dir)
            state = find_active_run(cwd, explicit_state_dir)
        if state is None:
            return {"protocolVersion": PROTOCOL_VERSION, "active": False}
    else:
        run_id = validate_run_id(requested_run_id)
        state_path, lock_path = state_paths(cwd, explicit_state_dir, run_id)
        with with_lock(lock_path):
            state = read_state(state_path)

    effective_config = config if config is not None else load_config()
    observed_manifest = trusted_manifest_fingerprint(cwd, effective_config)
    requested_manifest = nullable_text(request.get("manifestFingerprint"))
    fail(
        requested_manifest is not None and requested_manifest != observed_manifest,
        "manifest_witness_mismatch",
        "adapter-supplied Pocock manifest witness does not match the core-observed manifests",
        expected=observed_manifest,
        observed=requested_manifest,
    )
    mismatch = run_runtime_mismatch(state, observed_manifest)
    response = output(state)
    if mismatch is not None:
        response["card"]["nextActions"] = []
        response["card"]["blockedReason"] = RUNTIME_CHANGED_MESSAGE
        response["card"]["runtimeMismatch"] = mismatch
    return response


def command_hydrate(cwd: Path, explicit_state_dir: str | None, request: dict[str, Any]) -> dict[str, Any]:
    run_id = validate_run_id(request.get("runId"))
    state_path, lock_path = state_paths(cwd, explicit_state_dir, run_id)
    with with_lock(lock_path):
        state = read_state(state_path)
        require_run_runtime(state)
    fail(request.get("revision") != state["revision"] or request.get("stateHash") != state["stateHash"], "state_mismatch", "OMP session mirror does not match authoritative run state", currentRevision=state["revision"], currentStateHash=state["stateHash"])
    return output(state)


def parse_request(raw: str) -> dict[str, Any]:
    try:
        request = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeFailure("invalid_request", f"request is not valid JSON: {exc}") from exc
    fail(not isinstance(request, dict), "invalid_request", "request must be one JSON object")
    return request


def load_request(request_value: str | None, request_file: str | None) -> dict[str, Any]:
    fail(request_value is not None and request_file is not None, "invalid_request", "provide exactly one request source")
    if request_file is None:
        return parse_request(request_value or "")
    path = Path(request_file)
    fail(not path.is_file(), "invalid_request", f"request file is not readable: {path}")
    fail(path.stat().st_size > REQUEST_BYTES_MAX, "invalid_request", f"request file exceeds {REQUEST_BYTES_MAX} bytes")
    return parse_request(path.read_text(encoding="utf-8"))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=(
        "metadata",
        "start",
        "transition",
        "prepare",
        "seal-task",
        "record-task-result",
        "record-evidence",
        "pregate",
        "prepare-lenses",
        "adjudicate",
        "accept",
        "status",
        "hydrate",
    ))
    request_group = parser.add_mutually_exclusive_group(required=True)
    request_group.add_argument("--request")
    request_group.add_argument("--request-file")
    parser.add_argument("--state-dir")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        request = load_request(args.request, args.request_file)
        config_commands = {
            "metadata",
            "start",
            "prepare",
            "record-task-result",
            "pregate",
            "prepare-lenses",
            "accept",
            "status",
        }
        config = load_config() if args.command in config_commands else None
        cwd = Path.cwd().resolve()
        handlers: dict[str, Callable[[], dict[str, Any]]] = {
            "metadata": lambda: metadata(config, cwd),
            "start": lambda: command_start(cwd, args.state_dir, request, config),
            "transition": lambda: command_transition(cwd, args.state_dir, request),
            "prepare": lambda: command_prepare(cwd, args.state_dir, request, config),
            "seal-task": lambda: command_seal(cwd, args.state_dir, request),
            "record-task-result": lambda: command_record_result(cwd, args.state_dir, request, config),
            "record-evidence": lambda: command_record_evidence(cwd, args.state_dir, request),
            "pregate": lambda: command_pregate(cwd, args.state_dir, request, config),
            "prepare-lenses": lambda: command_prepare_lenses(cwd, args.state_dir, request, config),
            "adjudicate": lambda: command_adjudicate(cwd, args.state_dir, request),
            "accept": lambda: command_accept(cwd, args.state_dir, request, config),
            "status": lambda: command_status(cwd, args.state_dir, request, config),
            "hydrate": lambda: command_hydrate(cwd, args.state_dir, request),
        }
        result = handlers[args.command]()
        print(canonical(result))
        return 0
    except RuntimeFailure as exc:
        print(canonical(exc.diagnostic()), file=sys.stderr)
        return 1
    except Exception as exc:  # Unexpected failures are still machine-readable and fail closed.
        print(canonical({"code": "internal_error", "message": f"{type(exc).__name__}: {exc}"}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
