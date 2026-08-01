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
from typing import Any, Callable

import yaml

from dispatch_ledger import select_candidate


SKILL_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = SKILL_DIR / "config.yaml"
TELEMETRY_TOOL = Path(__file__).resolve().parent / "telemetry_append.py"
REQUEST_BYTES_MAX = 32 * 1024 * 1024
SCHEMA_VERSION = 1
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
CLASS_ORDER = {"mechanical": 0, "skilled": 1, "judgment": 2}
CLASS_ROLE = {"mechanical": "scout", "skilled": "builder", "judgment": "architect"}
EFFORT = {"mechanical": "lo", "skilled": "med", "judgment": "hi"}
RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{5,127}$")
PATCH_FORBIDDEN_PREFIXES = (".git",)
DEFAULT_PATCH_BYTES_MAX = 16 * 1024 * 1024
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


def load_config() -> dict[str, Any]:
    try:
        raw = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise RuntimeFailure("config_invalid", f"cannot load {CONFIG_PATH}: {exc}") from exc
    fail(not isinstance(raw, dict), "config_invalid", "config.yaml must contain a mapping")
    omp = raw.get("omp")
    fail(not isinstance(omp, dict), "config_invalid", "config.yaml lacks the required omp policy block")
    return raw


def config_fingerprint(config: dict[str, Any]) -> str:
    version = config.get("version")
    raw = CONFIG_PATH.read_bytes()
    return f"v{version}+{hashlib.sha256(raw).hexdigest()[:7]}"


def normalize_vendor(value: Any) -> str:
    return str(value or "").strip().lower()


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


def runtime_fingerprint() -> str:
    files = (
        Path(__file__).resolve(),
        Path(__file__).resolve().parent / "dispatch_ledger.py",
        TELEMETRY_TOOL.resolve(),
        CONFIG_PATH.resolve(),
    )
    witness = hashlib.sha256()
    for path in files:
        fail(not path.is_file(), "runtime_unavailable", f"runtime witness file is missing: {path}")
        witness.update(path.name.encode("utf-8"))
        witness.update(b"\0")
        witness.update(path.read_bytes())
        witness.update(b"\0")
    return witness.hexdigest()


def state_key_path(state_path: Path) -> Path:
    return state_path.parent.parent / "state-auth.key"


def load_or_create_state_key(state_path: Path, *, create: bool) -> bytes:
    path = state_key_path(state_path)
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


def read_state(path: Path) -> dict[str, Any]:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeFailure("run_not_found", f"Pocock run does not exist: {path.stem}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeFailure("state_corrupt", f"cannot read authoritative state: {exc}") from exc
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
    if state.get("entry") == "sweep" and state.get("phase") != "sweep_admission":
        require_sweep_integrity(state)
    return state


def with_lock(lock_path: Path):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "a+", encoding="utf-8")
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    return handle


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

def lens_attempt_count(state: dict[str, Any], producer_attempt_id: str, lens: str) -> int:
    return sum(
        1
        for attempt in state.get("lensAttempts", {}).values()
        if attempt.get("producerAttemptId") == producer_attempt_id and attempt.get("lens") == lens
    )

def ticket_failure_count(state: dict[str, Any], ticket_id: str) -> int:
    return int(state.get("qualityFailures", {}).get(ticket_id, 0)) + int(state.get("availabilityFailures", {}).get(ticket_id, 0))


def increment_ticket_failures(state: dict[str, Any], field: str, ticket_ids: list[str]) -> None:
    ledger = state.setdefault(field, {})
    for ticket_id in ticket_ids:
        ledger[ticket_id] = int(ledger.get(ticket_id, 0)) + 1


def route_lens_availability_failure(state: dict[str, Any], config: dict[str, Any], ticket_ids: list[str], reason: str) -> None:
    unique_tickets = sorted(set(ticket_ids))
    increment_ticket_failures(state, "lensAvailabilityFailures", unique_tickets)
    maximum = int(config.get("routing", {}).get("escalation", {}).get("max_attempts_per_subtask", 2))
    exhausted = [ticket_id for ticket_id in unique_tickets if int(state["lensAvailabilityFailures"].get(ticket_id, 0)) >= maximum]
    if exhausted:
        state["phase"] = "blocked"
        state["blockedReason"] = f"lens availability retry limit reached for: {', '.join(exhausted)}"
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


def current_wave_ticket_ids(state: dict[str, Any]) -> set[str]:
    current_wave = state.get("currentWave")
    for wave in state.get("waves", []):
        if wave.get("waveId") == current_wave:
            return {
                str(state["attempts"][attempt_id]["ticketId"])
                for attempt_id in wave.get("attemptIds", [])
                if attempt_id in state.get("attempts", {})
            }
    return set()

def current_wave_attempt_ids(state: dict[str, Any]) -> set[str]:
    current_wave = state.get("currentWave")
    for wave in state.get("waves", []):
        if wave.get("waveId") == current_wave:
            return {
                str(attempt_id)
                for attempt_id in wave.get("attemptIds", [])
                if attempt_id in state.get("attempts", {})
            }
    return set()


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


def attempt_outcome(status: str | None) -> str:
    return {
        "accepted": "ACCEPTED",
        "recorded": "ACCEPTED",
        "availability_failed": "FAILED_AVAILABILITY",
        "pregate_failed": "FAILED_PRE_GATE",
        "clarification_failed": "FAILED_PRE_GATE",
        "review_failed": "FAILED_REVIEW",
    }.get(status, "PENDING")


def attempt_failure_kind(status: str | None) -> str | None:
    return {
        "availability_failed": "availability",
        "pregate_failed": "pre_gate",
        "clarification_failed": "pre_gate",
        "review_failed": "review",
    }.get(status)


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


def participant(attempt: dict[str, Any], kind: str) -> dict[str, Any]:
    status = nullable_text(attempt.get("status"))
    declared_agent = nullable_text(attempt.get("declaredAgent")) or nullable_text(attempt.get("agent"))
    ordinal = (
        attempt.get("attemptOrdinal", attempt.get("qualityAttempt"))
        if kind == "producer"
        else attempt.get("reviewAttemptOrdinal", attempt.get("attemptOrdinal"))
    )
    fallback = attempt.get("modelFallback")
    return {
        "dispatchName": nullable_text(attempt.get("dispatchName")),
        "attemptId": nullable_text(attempt.get("attemptId")),
        "ticketId": nullable_text(attempt.get("ticketId")),
        "kind": kind,
        "role": nullable_text(attempt.get("role")),
        "lens": nullable_text(attempt.get("lens")),
        "attemptOrdinal": exact_nonnegative_integer(ordinal),
        "lane": nullable_text(attempt.get("lane")),
        "laneAlias": nullable_text(attempt.get("laneAlias")),
        "rank": exact_nonnegative_integer(attempt.get("rank")),
        "declaredAgent": declared_agent,
        "declaredModel": nullable_text(attempt.get("declaredModel")),
        "observedAgent": nullable_text(attempt.get("observedAgent")),
        "observedAgentSource": nullable_text(attempt.get("observedAgentSource")),
        "observedModel": nullable_text(attempt.get("observedModel")),
        "modelFallback": fallback if isinstance(fallback, bool) else None,
        "modelWitness": attempt_model_witness(attempt),
        "status": status,
        "outcome": attempt_outcome(status),
        "failureKind": attempt_failure_kind(status),
        "tokens": displayed_tokens(attempt.get("tokens")),
        "durationMs": exact_nonnegative_integer(attempt.get("durationMs")),
        "requests": exact_nonnegative_integer(attempt.get("requests")),
        "failureReason": attempt_failure_reason(attempt),
    }


def compact_actor(attempt: dict[str, Any], kind: str) -> dict[str, Any]:
    row = participant(attempt, kind)
    return {
        field: row[field]
        for field in (
            "dispatchName",
            "attemptId",
            "ticketId",
            "role",
            "lens",
            "attemptOrdinal",
            "laneAlias",
            "declaredModel",
            "observedModel",
            "modelWitness",
            "status",
            "tokens",
        )
    }


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


def attempt_failure_reason(attempt: dict[str, Any]) -> str | None:
    direct = nullable_text(attempt.get("failureReason")) or nullable_text(attempt.get("abandonReason"))
    if direct is not None:
        return direct
    evidence = attempt.get("availabilityEvidence")
    return nullable_text(evidence.get("reason")) if isinstance(evidence, dict) else None


def aggregate_participants(rows: list[dict[str, Any]]) -> dict[str, int | None]:
    tokens = [exact_nonnegative_integer(row.get("tokens")) for row in rows]
    witnessed = [value for value in tokens if value is not None]
    return {
        "attempts": len(rows),
        "tokenWitnessedAttempts": len(witnessed),
        "tokens": sum(witnessed) if len(witnessed) == len(rows) else None,
    }


def report_group_key(value: Any) -> str:
    return nullable_text(value) or "n/a"


def grouped_participant_aggregates(
    rows: list[dict[str, Any]],
    key_for: Callable[[dict[str, Any]], str],
) -> dict[str, dict[str, int | None]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(key_for(row), []).append(row)
    return {key: aggregate_participants(group) for key, group in groups.items()}


def count_participant_values(rows: list[dict[str, Any]], field: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        key = report_group_key(row.get(field))
        counts[key] = counts.get(key, 0) + 1
    return counts


def report(state: dict[str, Any]) -> dict[str, Any]:
    records: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for kind, field in (("producer", "attempts"), ("lens", "lensAttempts")):
        collection = state.get(field)
        if not isinstance(collection, dict):
            continue
        for attempt in collection.values():
            if isinstance(attempt, dict):
                records.append((attempt, participant(attempt, kind)))

    participants = [row for _attempt, row in records]
    token_values = [exact_nonnegative_integer(row.get("tokens")) for row in participants]
    witnessed_tokens = [value for value in token_values if value is not None]
    task_attempt_tokens = {
        row["attemptId"]: exact_nonnegative_integer(row.get("tokens"))
        for row in participants
        if isinstance(row.get("attemptId"), str)
    }
    lead = copy.deepcopy(state.get("lead")) if isinstance(state.get("lead"), dict) else {}
    lead["tokens"] = None
    lead["coverageScope"] = "task_attempts_only"

    return {
        "schemaVersion": 1,
        "runId": state.get("runId"),
        "entry": state.get("entry"),
        "phase": state.get("phase"),
        "objective": state.get("objective"),
        "unit": "input+output+cacheWrite; cacheRead excluded",
        "lead": lead,
        "participants": participants,
        "coverage": {
            "taskAttempts": len(participants),
            "totalKnownTokens": sum(witnessed_tokens) if len(witnessed_tokens) == len(participants) else None,
            "tokenWitnessedAttempts": len(witnessed_tokens),
            "tokenCoverageComplete": len(witnessed_tokens) == len(participants),
        },
        "taskAttemptTokens": task_attempt_tokens,
        "byRole": grouped_participant_aggregates(participants, lambda row: report_group_key(row.get("role"))),
        "byLane": grouped_participant_aggregates(participants, lambda row: report_group_key(row.get("lane"))),
        "byModel": grouped_participant_aggregates(
            participants,
            lambda row: report_group_key(row.get("observedModel") if row.get("observedModel") is not None else row.get("declaredModel")),
        ),
        "statuses": count_participant_values(participants, "status"),
        "outcomes": count_participant_values(participants, "outcome"),
        "failures": [
            {
                "attemptId": row["attemptId"],
                "ticketId": row["ticketId"],
                "status": row["status"],
                "outcome": row["outcome"],
                "reason": attempt_failure_reason(attempt),
            }
            for attempt, row in records
            if row["failureKind"] is not None
        ],
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
        for attempt_id in sorted(current_wave_attempt_ids(state)):
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
    return result


def output(state: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"card": card(state), **extra}


def metadata(config: dict[str, Any], cwd: Path) -> dict[str, Any]:
    validate_effective_omp_settings(cwd)
    return {"configFingerprint": config_fingerprint(config), "omp": config["omp"]}


def validate_effective_omp_settings(cwd: Path) -> None:
    completed = subprocess.run(
        ["omp", "config", "list", "--json"],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
    )
    fail(completed.returncode != 0, "omp_config_unavailable", completed.stderr.strip() or "cannot inspect effective OMP settings")
    try:
        settings = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeFailure("omp_config_unavailable", f"OMP returned invalid settings JSON: {exc}") from exc
    fail(not isinstance(settings, dict), "omp_config_unavailable", "OMP settings inventory is not an object")
    expected: dict[str, Any] = {
        "async.enabled": False,
        "task.batch": True,
        "task.enableEffort": True,
        # The concrete backend is host policy; the invariant is that isolation
        # remains enabled and every result returns as an unapplied patch.
        "task.isolation.apply": False,
        "task.isolation.merge": "patch",
        "task.maxRecursionDepth": 1,
        "retry.modelFallback": False,
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
    fail(
        bool(mismatches),
        "omp_config_incompatible",
        "effective OMP task settings do not satisfy Pocock invariants; run install.sh --configure-omp explicitly or apply the listed values to the current project",
        mismatches=mismatches,
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
    manifest_fingerprint = require_text(request.get("manifestFingerprint"), "manifestFingerprint")
    lead = require_mapping(request.get("lead"), "lead")
    models = require_mapping(request.get("models"), "models")
    fail(not models, "model_manifest_invalid", "OMP supplied no resolved Pocock lane models")

    omp_lanes = config["omp"].get("lanes", {})
    fail(set(models) != set(omp_lanes), "model_manifest_invalid", "resolved lane set differs from config", expected=sorted(omp_lanes), observed=sorted(models))
    known = {normalize_vendor(v) for v in config["omp"].get("known_vendors", [])}
    for lane, witness in models.items():
        fail(not isinstance(witness, dict), "model_manifest_invalid", f"lane {lane} witness is not an object")
        for field in ("role", "provider", "resolvedModel", "vendor", "family"):
            require_text(witness.get(field), f"models.{lane}.{field}")
        fail(normalize_vendor(witness["vendor"]) not in known, "model_manifest_invalid", f"lane {lane} has an unknown vendor")
        fail(witness.get("resolvedModelIsFallback") is True, "model_manifest_invalid", "a role manifest cannot begin on a fallback model")

    lead_witness = {key: lead.get(key) for key in ("provider", "id", "resolvedModel", "vendor", "family")}
    for field, value in lead_witness.items():
        require_text(value, f"lead.{field}")

    run_id = f"pocock-{int(time.time())}-{uuid.uuid4().hex[:12]}"
    state_path, lock_path = state_paths(cwd, explicit_state_dir, run_id)
    with with_lock(lock_path):
        fail(state_path.exists(), "run_collision", "generated run id already exists")
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
            "lead": lead_witness,
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
            "lensAvailabilityFailures": {},
            "retryFloor": {},
            "frontierExhausted": False,
            "dispatchCounts": {},
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
        seal_state(state, state_path)
        write_state(state_path, state)
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
            ticket_ids: list[str] = []
            for attempt_id in pending.get("attemptIds", []):
                attempt = collection.get(attempt_id)
                fail(not isinstance(attempt, dict) or attempt.get("status") != "running", "dispatch_invalid", f"attempt {attempt_id} is not running")
                attempt["status"] = "availability_failed"
                attempt["abandonReason"] = reason
                ticket_ids.append(str(attempt["ticketId"]))
            state["projectedTokens"] = max(0, int(state.get("projectedTokens", 0)) - int(pending.get("projection", 0)))
            pending["status"] = "abandoned"
            pending["abandonedAt"] = dt.datetime.now(dt.timezone.utc).isoformat()
            pending["abandonReason"] = reason
            failure_reason = f"sealed task settlement was explicitly abandoned: {reason}"
            if kind == "lenses":
                route_lens_availability_failure(state, load_config(), ticket_ids, failure_reason)
                if state["phase"] == "blocked":
                    rollback_attempt_patches(cwd, state, current_wave_attempt_ids(state))
            else:
                increment_ticket_failures(state, "availabilityFailures", ticket_ids)
                state["phase"] = "repair_pending"
                state["blockedReason"] = failure_reason
                state["lastFailureKind"] = "availability"
                state["retryTicketIds"] = sorted(current_wave_ticket_ids(state))
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
            kind = str(diagnosis.get("diagnosis", "effort")).lower()
            fail(kind not in {"effort", "capability", "availability", "clarification"}, "invalid_diagnosis", "retry diagnosis must be effort, capability, availability, or clarification")
            max_attempts = int(load_config().get("routing", {}).get("escalation", {}).get("max_attempts_per_subtask", 2))
            attempted_tickets = set(state.get("retryTicketIds") or current_wave_ticket_ids(state))
            if state.get("entry") == "sweep":
                sweep = require_sweep_integrity(state)
                fail(not attempted_tickets, "retry_invalid", "sweep retry has no failed sealed tickets")
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
            if kind == "capability":
                for attempt in state.get("attempts", {}).values():
                    if attempt.get("status") in {"pregate_failed", "review_failed"} and attempt.get("ticketId") in attempted_tickets:
                        ticket = attempt["ticketId"]
                        state["retryFloor"][ticket] = max(int(state["retryFloor"].get(ticket, 0)), int(attempt["rank"]) + 1)
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
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["lens", "attemptId", "summary", "findings", "verdict"],
        "properties": {
            "lens": {"type": "string", "enum": ["Standards", "Spec", "Critic"]},
            "attemptId": {"type": "string"},
            "summary": {"type": "string"},
            "findings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["scope", "severity", "blocking", "evidence"],
                    "properties": {
                        "scope": {"type": "string", "enum": ["introduced", "pre-existing", "decision-challenge"]},
                        "severity": {"type": "string", "enum": ["critical", "high", "medium", "low", "note"]},
                        "blocking": {"type": "boolean"},
                        "evidence": {"type": "string"},
                    },
                },
            },
            "verdict": {"type": "string", "enum": ["PASS", "FAIL", "NO_VERDICT"]},
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
    ready = sorted(ticket_id_value for ticket_id_value, prerequisites in unresolved.items() if not prerequisites)
    order = []
    while ready:
        ticket_id_value = ready.pop(0)
        order.append(ticket_id_value)
        for dependent in sorted(dependents[ticket_id_value]):
            unresolved[dependent].remove(ticket_id_value)
            if not unresolved[dependent]:
                ready.append(dependent)
        ready.sort()
    fail(
        len(order) != len(dependencies),
        "dag_invalid",
        "sweep dependsOn graph contains a cycle",
    )
    return order


def sweep_has_incomparable_pair(dependencies: dict[str, list[str]], order: list[str]) -> bool:
    ancestors: dict[str, set[str]] = {}
    for ticket_id_value in order:
        closure: set[str] = set()
        for prerequisite in dependencies[ticket_id_value]:
            closure.add(prerequisite)
            closure.update(ancestors[prerequisite])
        ancestors[ticket_id_value] = closure
    ticket_ids = sorted(dependencies)
    return any(
        left not in ancestors[right] and right not in ancestors[left]
        for index, left in enumerate(ticket_ids)
        for right in ticket_ids[index + 1:]
    )


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





def classify_ticket(ticket: dict[str, Any], config: dict[str, Any]) -> str:
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
    fail(ticket["write"] and derived == "judgment", "ticket_needs_decomposition", f"ticket {ticket['ticketId']} combines judgment with production writes; split design from implementation")
    return derived


def role_candidates(state: dict[str, Any], config: dict[str, Any], role: str, minimum_rank: int) -> list[dict[str, Any]]:
    omp = config["omp"]
    role_def = omp.get("roles", {}).get(role, {})
    agents = role_def.get("agents", {}) if isinstance(role_def, dict) else {}
    known = {normalize_vendor(v) for v in omp.get("known_vendors", [])}
    candidates = []
    for lane, agent in agents.items():
        lane_def = omp.get("lanes", {}).get(lane, {})
        witness = state.get("models", {}).get(lane)
        if not isinstance(lane_def, dict) or not isinstance(witness, dict):
            continue
        rank = int(lane_def.get("rank", -1))
        vendor = normalize_vendor(witness.get("vendor"))
        if rank < minimum_rank or vendor not in known:
            continue
        candidates.append({
            "lane": lane,
            "agent": agent,
            "rank": rank,
            "witness": witness,
        })
    return sorted(candidates, key=lambda item: (item["rank"], item["lane"]))


def claim_candidate(state: dict[str, Any], candidates: list[dict[str, Any]], role: str) -> dict[str, Any]:
    fail(not candidates, "route_unavailable", f"no known OMP lane is available for role {role}")
    lowest_rank = candidates[0]["rank"]
    equal = [item for item in candidates if item["rank"] == lowest_rank]
    counts = state.setdefault("dispatchCounts", {})
    selected_lane = select_candidate([item["lane"] for item in equal], counts)
    selected = next(item for item in equal if item["lane"] == selected_lane)
    counts[selected_lane] = int(counts.get(selected_lane, 0)) + 1
    return selected


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
            completed = subprocess.run(["git", "rev-parse", "HEAD"], cwd=cwd, text=True, capture_output=True, check=False)
            fail(completed.returncode != 0, "git_required", "writer dispatch requires a git repository")
            base_sha = completed.stdout.strip()

        items = []
        attempt_ids = []
        wave_attempts = []
        for index, ticket in enumerate(tickets):
            cls = classify_ticket(ticket, config)
            role = CLASS_ROLE[cls]
            floor = int(state.get("retryFloor", {}).get(ticket["ticketId"], 0))
            allowed_min = {"scout": 0, "builder": 1, "architect": 2}[role]
            selected = claim_candidate(state, role_candidates(state, config, role, max(floor, allowed_min)), role)
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
                "role": role,
                "lane": selected["lane"],
                "laneAlias": config["omp"]["lanes"][selected["lane"]]["alias"],
                "rank": selected["rank"],
                "agent": selected["agent"],
                "declaredAgent": selected["agent"],
                "declaredModel": selected["witness"]["resolvedModel"],
                "vendor": selected["witness"]["vendor"],
                "family": selected["witness"]["family"],
                "status": "prepared",
                "tokens": None,
                "durationMs": None,
                "requests": None,
                "observedAgent": None,
                "observedAgentSource": None,
                "observedModel": None,
                "modelFallback": None,
                "baseSha": base_sha,
            }
            if sweep is not None:
                attempt["ticketHash"] = sweep["ticketHashes"][ticket["ticketId"]]
            if ticket["ui_live"]:
                challenge = {
                    "attemptId": attempt_id,
                    "ticketId": ticket["ticketId"],
                    "token": f"pocock-ui-{uuid.uuid4().hex}",
                    "target": ticket["ui_evidence"]["target"],
                    "criterion": ticket["ui_evidence"]["criterion"],
                    "requiredStages": ["open", "exercise"],
                }
                attempt["evidenceChallenge"] = challenge
                state["evidenceChallenges"][challenge["token"]] = challenge
            state["attempts"][attempt_id] = attempt
            attempt_ids.append(attempt_id)
            wave_attempts.append(attempt_id)
            items.append({
                "name": dispatch_name,
                "agent": selected["agent"],
                "task": text_for_ticket(ticket),
                "effort": EFFORT[cls],
                "outputSchema": producer_schema(),
                "schemaMode": "strict",
                "isolated": True,
            })

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
        wave = {"waveId": f"wave-{wave_no}", "attemptIds": wave_attempts, "status": "prepared", "baseSha": base_sha}
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
        state["retryTicketIds"] = []
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


def validate_lens_result(data: dict[str, Any], assignment: dict[str, Any]) -> None:
    expected = {"lens", "attemptId", "summary", "findings", "verdict"}
    fail(set(data) != expected, "structured_output_invalid", f"lens result for {assignment['attemptId']} has the wrong fields")
    fail(data["lens"] != assignment["lens"], "structured_output_invalid", f"lens identity mismatch for {assignment['attemptId']}")
    fail(data["attemptId"] != assignment["producerAttemptId"], "stale_attempt", f"lens report is not bound to producer attempt {assignment['producerAttemptId']}")
    fail(not isinstance(data["summary"], str) or not data["summary"].strip(), "structured_output_invalid", "lens summary must be non-empty")
    expected_verdict = {"PASS", "FAIL"} if assignment["lens"] == "Critic" else {"NO_VERDICT"}
    fail(data["verdict"] not in expected_verdict, "structured_output_invalid", f"{assignment['lens']} emitted an illegal verdict")
    findings = data["findings"]
    fail(not isinstance(findings, list), "structured_output_invalid", "lens findings must be an array")
    for finding in findings:
        fail(not isinstance(finding, dict) or set(finding) != {"scope", "severity", "blocking", "evidence"}, "structured_output_invalid", "a lens finding has the wrong fields")
        fail(finding["scope"] not in {"introduced", "pre-existing", "decision-challenge"}, "structured_output_invalid", "a lens finding has an invalid scope")
        fail(finding["severity"] not in {"critical", "high", "medium", "low", "note"}, "structured_output_invalid", "a lens finding has an invalid severity")
        fail(not isinstance(finding["blocking"], bool), "structured_output_invalid", "a lens finding blocking flag must be boolean")
        fail(not isinstance(finding["evidence"], str) or not finding["evidence"].strip(), "structured_output_invalid", "a lens finding requires evidence")




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

    completed = subprocess.run(
        ["git", "apply", "--numstat", "-z", str(path.resolve())],
        cwd=cwd,
        capture_output=True,
        check=False,
    )
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


def apply_patch_batch(cwd: Path, patches: list[dict[str, Any]], artifact_dir: Path) -> dict[str, Any]:
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

    journal = {"sha256": combined_sha, "cwd": str(cwd.resolve()), "status": "prepared"}
    if journal_path.is_file():
        try:
            observed_journal = json.loads(journal_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeFailure("patch_journal_corrupt", f"cannot read patch application journal: {exc}") from exc
        fail(
            not isinstance(observed_journal, dict)
            or observed_journal.get("sha256") != combined_sha
            or observed_journal.get("cwd") != str(cwd.resolve())
            or observed_journal.get("status") not in {"prepared", "applied"},
            "patch_journal_corrupt",
            f"patch application journal does not match this wave: {journal_path}",
        )
        journal = observed_journal
    else:
        write_bytes_atomic(journal_path, (canonical(journal) + "\n").encode("utf-8"))

    forward = subprocess.run(["git", "apply", "--check", "--whitespace=nowarn", str(combined_path)], cwd=cwd, text=True, capture_output=True, check=False)
    reverse = subprocess.run(["git", "apply", "--reverse", "--check", "--whitespace=nowarn", str(combined_path)], cwd=cwd, text=True, capture_output=True, check=False)
    if journal["status"] == "applied":
        fail(reverse.returncode != 0, "patch_journal_mismatch", "journal says the producer patch is applied, but the working tree disagrees")
    elif forward.returncode == 0:
        applied = subprocess.run(["git", "apply", "--whitespace=nowarn", str(combined_path)], cwd=cwd, text=True, capture_output=True, check=False)
        fail(applied.returncode != 0, "patch_apply_failed", applied.stderr.strip() or "validated producer patches could not be applied")
        journal["status"] = "applied"
        write_bytes_atomic(journal_path, (canonical(journal) + "\n").encode("utf-8"))
    elif reverse.returncode == 0:
        journal["status"] = "applied"
        write_bytes_atomic(journal_path, (canonical(journal) + "\n").encode("utf-8"))
    else:
        fail(True, "patch_conflict", forward.stderr.strip() or "combined producer patches do not apply cleanly")

    return {
        "path": str(combined_path.resolve()),
        "journalPath": str(journal_path.resolve()),
        "sha256": combined_sha,
        "bytes": len(combined),
        "files": sorted({path for patch in patches for path in patch["files"]}),
        "diffLines": sum(int(patch["diffLines"]) for patch in patches),
        "attemptPatches": attempt_patches,
    }


def rollback_attempt_patches(cwd: Path, state: dict[str, Any], attempt_ids: set[str]) -> None:
    records = []
    for attempt_id in reversed(list(state.get("attempts", {}))):
        if attempt_id not in attempt_ids:
            continue
        attempt = state["attempts"][attempt_id]
        applied = attempt.get("appliedPatch")
        if isinstance(applied, dict) and not applied.get("rolledBackAt"):
            records.append((attempt, applied))
    if not records:
        return

    chunks = []
    for _attempt, applied in records:
        path = Path(require_text(applied.get("path"), "appliedPatch.path"))
        fail(not path.is_file(), "rollback_failed", f"cannot restore rejected patch; artifact is missing: {path}")
        data = path.read_bytes()
        fail(hashlib.sha256(data).hexdigest() != applied.get("sha256"), "rollback_failed", f"rejected patch artifact hash mismatch: {path}")
        chunks.append(data if data.endswith(b"\n") else data + b"\n")
    combined = b"".join(chunks)
    rollback_sha = hashlib.sha256(combined).hexdigest()
    rollback_id = digest({
        "sha256": rollback_sha,
        "attemptIds": [attempt["attemptId"] for attempt, _applied in records],
    })[:16]
    artifact_dir = Path(records[0][1]["path"]).parent
    patch_path = artifact_dir / f"rollback-{rollback_id}.patch"
    journal_path = artifact_dir / f"rollback-{rollback_id}.json"
    write_bytes_atomic(patch_path, combined)

    journal = {"sha256": rollback_sha, "cwd": str(cwd.resolve()), "status": "prepared"}
    if journal_path.is_file():
        try:
            observed_journal = json.loads(journal_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeFailure("rollback_journal_corrupt", f"cannot read patch rollback journal: {exc}") from exc
        fail(
            not isinstance(observed_journal, dict)
            or observed_journal.get("sha256") != rollback_sha
            or observed_journal.get("cwd") != str(cwd.resolve())
            or observed_journal.get("status") not in {"prepared", "rolled_back"},
            "rollback_journal_corrupt",
            f"patch rollback journal does not match rejected attempts: {journal_path}",
        )
        journal = observed_journal
    else:
        write_bytes_atomic(journal_path, (canonical(journal) + "\n").encode("utf-8"))

    reverse = subprocess.run(["git", "apply", "--reverse", "--check", "--whitespace=nowarn", str(patch_path)], cwd=cwd, text=True, capture_output=True, check=False)
    forward = subprocess.run(["git", "apply", "--check", "--whitespace=nowarn", str(patch_path)], cwd=cwd, text=True, capture_output=True, check=False)
    if journal["status"] == "rolled_back":
        fail(forward.returncode != 0, "rollback_journal_mismatch", "journal says the rejected patch was rolled back, but the working tree disagrees")
    elif reverse.returncode == 0:
        reverted = subprocess.run(["git", "apply", "--reverse", "--whitespace=nowarn", str(patch_path)], cwd=cwd, text=True, capture_output=True, check=False)
        fail(reverted.returncode != 0, "rollback_failed", reverted.stderr.strip() or "rejected producer patch rollback failed")
        journal["status"] = "rolled_back"
        write_bytes_atomic(journal_path, (canonical(journal) + "\n").encode("utf-8"))
    elif forward.returncode == 0:
        journal["status"] = "rolled_back"
        write_bytes_atomic(journal_path, (canonical(journal) + "\n").encode("utf-8"))
    else:
        fail(True, "rollback_failed", reverse.stderr.strip() or "rejected producer patch cannot be rolled back cleanly")

    rolled_back_at = dt.datetime.now(dt.timezone.utc).isoformat()
    for attempt, applied in records:
        applied["rolledBackAt"] = rolled_back_at


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
        fail(not isinstance(pending, dict) or pending.get("dispatchId") != dispatch_id or pending.get("status") != "running", "stale_dispatch", "task result is not for the current running dispatch")
        kind = pending["kind"]
        expected_phase = "producer_running" if kind == "producer" else "lens_running"
        fail(state["phase"] != expected_phase, "stale_dispatch", f"task result arrived in phase {state['phase']}")
        fail(digest(executed_input) != pending["inputHash"], "seal_mismatch", "actually executed task input differs from the sealed batch")
        fail(len(results) != len(pending["attemptIds"]), "task_result_invalid", "task result count differs from sealed attempt count")
        collection = state["attempts"] if kind == "producer" else state["lensAttempts"]
        availability_failures: list[str] = []
        failed_attempt_ids: list[str] = []
        observed_tokens = 0
        producer_patches: list[dict[str, Any]] = []
        patch_bytes_max = int(config.get("omp", {}).get("pre_gate", {}).get("patch_bytes_max", DEFAULT_PATCH_BYTES_MAX))

        for expected_id, raw_result in zip(pending["attemptIds"], results):
            fail(not isinstance(raw_result, dict), "task_result_invalid", "each task result must be an object")
            fail(raw_result.get("attemptId") != expected_id, "stale_attempt", f"result is bound to {raw_result.get('attemptId')}, expected {expected_id}")
            attempt = collection[expected_id]
            declared_agent = raw_result.get("declaredAgent")
            observed_agent = raw_result.get("observedAgent")
            declared_model = raw_result.get("declaredModel")
            observed_model = raw_result.get("observedResolvedModel")
            observed_agent_source = raw_result.get("observedAgentSource")
            model_fallback = (
                raw_result.get("resolvedModelIsFallback")
                if isinstance(raw_result.get("resolvedModelIsFallback"), bool)
                else None
            )
            fail(
                declared_agent != attempt["agent"]
                or observed_agent != attempt["agent"]
                or observed_agent_source not in {"user", "project"},
                "agent_mismatch",
                f"agent witness mismatch for {expected_id}",
            )
            fail(model_base(declared_model) != model_base(attempt["declaredModel"]), "model_mismatch", f"declared model differs from sealed model for {expected_id}")
            tokens = usage_tokens(raw_result)
            attempt["tokens"] = tokens
            attempt["durationMs"] = exact_nonnegative_integer(raw_result.get("durationMs"))
            attempt["requests"] = exact_nonnegative_integer(raw_result.get("requests"))
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
            model_substituted = model_fallback is True or model_base(observed_model) != model_base(declared_model)
            reason = None
            if is_error or model_substituted:
                reason = task_error
                if reason is None:
                    reason = (
                        f"model/transport availability substitution for {expected_id}"
                        if model_substituted
                        else f"task tool reported an error for {expected_id}"
                    )
            elif aborted or exit_code not in (None, 0):
                reason = task_error or f"worker did not settle successfully for {expected_id}"

            if reason is not None:
                availability_failures.append(reason)
                attempt["status"] = "availability_failed"
                attempt["failureReason"] = reason
                attempt["availabilityEvidence"] = {
                    "declaredModel": declared_model,
                    "observedModel": observed_model,
                    "fallback": model_fallback,
                    "error": task_error,
                    "exitCode": exit_code,
                    "aborted": aborted,
                    "isError": is_error,
                    "reason": reason,
                }
                failed_attempt_ids.append(expected_id)
                continue

            artifact = hash_artifact(raw_result.get("outputPath"), f"result {expected_id}.outputPath")
            data = parse_result_data(content.get(expected_id))
            fail(data is None, "structured_output_invalid", f"strict structured output is missing for {expected_id}")
            if kind == "producer":
                validate_producer_result(data, attempt)
                patch_path = raw_result.get("patchPath")
                if patch_path:
                    patch = inspect_patch(cwd, patch_path, f"result {expected_id}.patchPath", patch_bytes_max)
                else:
                    fail(attempt["ticket"]["write"], "artifact_missing", f"writer result {expected_id} has no patch artifact")
                    patch = {"artifact": None, "data": b"", "files": [], "diffLines": 0}
                patch["attemptId"] = expected_id
                validate_patch_scope(cwd, patch, attempt)
                declared_changed_files = sorted(
                    normalize_observed_repo_path(path, f"result {expected_id}.changedFiles")
                    for path in data["changedFiles"]
                )
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
                fail(
                    data["status"] != "COMPLETED" and bool(patch["files"]),
                    "patch_scope_violation",
                    f"non-completed producer {expected_id} returned repository changes",
                )
                producer_patches.append(patch)
            else:
                validate_lens_result(data, attempt)
                patch = {"artifact": hash_artifact(raw_result["patchPath"], f"result {expected_id}.patchPath")} if raw_result.get("patchPath") else None
            attempt["status"] = "completed"
            attempt["toolCallId"] = tool_call_id
            attempt["artifact"] = artifact
            attempt["patchArtifact"] = patch["artifact"] if patch else None
            attempt["patchFiles"] = patch["files"] if kind == "producer" else []
            attempt["patchDiffLines"] = patch["diffLines"] if kind == "producer" else 0
            attempt["result"] = data

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

        if availability_failures:
            failed_tickets = [str(collection[attempt_id]["ticketId"]) for attempt_id in failed_attempt_ids]
            reason = "; ".join(availability_failures)
            if kind == "lenses":
                route_lens_availability_failure(state, config, failed_tickets, reason)
                if state["phase"] == "blocked":
                    rollback_attempt_patches(cwd, state, current_wave_attempt_ids(state))
            else:
                increment_ticket_failures(state, "availabilityFailures", failed_tickets)
                state["phase"] = "repair_pending"
                state["blockedReason"] = reason
                state["lastFailureKind"] = "availability"
                state["retryTicketIds"] = sorted(current_wave_ticket_ids(state))
            return
        if kind == "producer":
            failed_tickets = []
            for attempt_id in pending["attemptIds"]:
                attempt = collection[attempt_id]
                if attempt["result"].get("status") != "COMPLETED":
                    attempt["status"] = "clarification_failed"
                    failed_tickets.append(str(attempt["ticketId"]))
            if failed_tickets:
                increment_ticket_failures(state, "qualityFailures", failed_tickets)
                state["phase"] = "repair_pending"
                state["blockedReason"] = "producer requested clarification or reported a block"
                state["lastFailureKind"] = "clarification"
                state["retryTicketIds"] = sorted(current_wave_ticket_ids(state))
            else:
                state_path, _lock_path = state_paths(cwd, explicit_state_dir, state["runId"])
                applied_patch = apply_patch_batch(cwd, producer_patches, state_path.with_suffix(".artifacts"))
                for applied_attempt in applied_patch["attemptPatches"]:
                    collection[applied_attempt["attemptId"]]["appliedPatch"] = applied_attempt
                pending["appliedPatch"] = applied_patch
                for wave in state.get("waves", []):
                    if wave.get("waveId") == state.get("currentWave"):
                        wave["appliedPatch"] = applied_patch
                        break
                state["phase"] = "pregate_pending"
                state["blockedReason"] = None
        else:
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
        if stage == "open":
            app = invocation.get("app") if isinstance(invocation.get("app"), dict) else {}
            observed_target = invocation.get("url") or app.get("target")
            fail(action != "open" or observed_target != challenge.get("target"), "evidence_invalid", "UI open evidence must use the exact issued target")
        else:
            code = require_text(invocation.get("code"), "invocation.code")
            criterion = require_text(challenge.get("criterion"), "challenge.criterion")
            has_assertion = re.search(r"\bassert\s*\(", code) is not None
            constant_assertion = re.search(r"\bassert\s*\(\s*true\s*(?:,|\))", code, re.IGNORECASE) is not None
            fail(
                action != "run" or not has_assertion or constant_assertion or criterion not in code,
                "evidence_invalid",
                "UI exercise evidence must assert a non-constant observation and name the exact issued criterion",
            )
        completed = {
            record.get("stage")
            for record in state["evidence"]
            if record.get("challengeToken") == token
        }
        fail(stage in completed, "evidence_invalid", f"UI evidence stage {stage} was already recorded")
        if stage == "exercise":
            fail("open" not in completed, "evidence_invalid", "UI exercise evidence requires a recorded open stage")
        record = {
            "toolCallId": require_text(request.get("toolCallId"), "toolCallId"),
            "tool": require_text(request.get("tool"), "tool"),
            "success": request.get("success") is True,
            "details": request.get("details"),
            "invocation": invocation,
            "content": request.get("content"),
            "attemptIds": attempt_ids,
            "challengeToken": token,
            "stage": stage,
            "recordedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
        }
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
    completed = subprocess.run(argv, cwd=cwd, text=True, capture_output=True, check=False)
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
        wave = next((wave for wave in state["waves"] if wave["waveId"] == state["currentWave"]), None)
        fail(not isinstance(wave, dict), "state_corrupt", "current wave is missing")
        attempts = [state["attempts"][attempt_id] for attempt_id in wave["attemptIds"]]
        fail(any(attempt.get("status") != "completed" for attempt in attempts), "pregate_failed", "not every producer attempt completed")

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

        writer_files = sorted({path for attempt in attempts for path in attempt.get("patchFiles", [])})
        diff_check = run_diff_check(cwd, writer_files, max_bytes)
        diff_check["kind"] = "git-diff-check"
        checks.append(diff_check)
        if diff_check["exitCode"] != 0:
            failures.append("git diff --check failed for the applied producer patch")
            failed_attempt_ids.update(
                attempt["attemptId"] for attempt in attempts if attempt.get("patchFiles")
            )

        for attempt in attempts:
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
                            "invocation": record.get("invocation"),
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

        measured_total = sum(int(attempt.get("patchDiffLines", 0)) for attempt in attempts)
        state["pregate"] = {
            "status": "FAIL" if failures else "PASS",
            "checks": checks,
            "diffLines": measured_total,
            "diffLimitPerTicket": configured_max,
            "failures": failures,
        }
        if failures:
            rollback_attempt_patches(cwd, state, {attempt["attemptId"] for attempt in attempts})
            for attempt in attempts:
                if attempt["attemptId"] not in failed_attempt_ids:
                    continue
                attempt["status"] = "pregate_failed"
                increment_ticket_failures(state, "qualityFailures", [str(attempt["ticketId"])])
            state["phase"] = "repair_pending"
            state["blockedReason"] = "; ".join(failures)
            state["lastFailureKind"] = "effort"
            state["retryTicketIds"] = sorted(current_wave_ticket_ids(state))
        else:
            state["phase"] = "lens_prepare_pending"
            state["blockedReason"] = None

    state, _ = mutate(cwd, explicit_state_dir, request, apply)
    return output(state)


def reviewer_candidate(
    state: dict[str, Any],
    config: dict[str, Any],
    producer: dict[str, Any],
    lens: str,
) -> dict[str, Any]:
    minimum = int(config["omp"].get("lenses", {}).get("critic_min_rank", 1)) if lens == "Critic" else 0
    candidates = role_candidates(state, config, config["omp"].get("lenses", {}).get("reviewer_role", "reviewer"), minimum)
    producer_vendor = normalize_vendor(producer["vendor"])
    producer_family = producer["family"]
    eligible = [item for item in candidates if normalize_vendor(item["witness"].get("vendor")) != producer_vendor and item["witness"].get("family") != producer_family]
    if lens == "Standards" and config["omp"].get("lenses", {}).get("standards_non_claude"):
        eligible = [item for item in eligible if normalize_vendor(item["witness"].get("vendor")) != "anthropic"]
    fail(not eligible, "independent_reviewer_unavailable", f"no independent trusted reviewer is available for {lens} against {producer['attemptId']}")
    # Critic takes the strongest eligible lane. Other lenses spread mechanically
    # at their lowest eligible rank, with the same per-run ledger as producers.
    if lens == "Critic":
        strongest = max(item["rank"] for item in eligible)
        eligible = [item for item in eligible if item["rank"] == strongest]
    return claim_candidate(state, eligible, "reviewer")


def lens_task_text(lens: str, producer: dict[str, Any], state: dict[str, Any]) -> str:
    ticket = producer["ticket"]
    artifact = producer["artifact"]
    pregate = state.get("pregate", {})
    return (
        f"LENS: {lens}\n"
        f"PRODUCER_ATTEMPT: {producer['attemptId']}\n"
        f"ARTIFACT_FROM_DISK: {artifact['path']} sha256={artifact['sha256']}\n"
        f"PREGATE: {canonical(pregate)}\n\n"
        f"OBJECTIVE: {ticket['OBJECTIVE']}\n"
        f"INPUTS: {ticket['INPUTS']}\n"
        f"BOUNDARIES: {ticket['BOUNDARIES']}\n"
        f"ACCEPTANCE: {ticket['ACCEPTANCE']}\n\n"
        "Inspect the repository and disk artifact yourself. Standards and Spec return NO_VERDICT; only Critic returns PASS or FAIL. "
        "Every finding must identify scope and concrete evidence. Do not modify files."
    )


def command_prepare_lenses(cwd: Path, explicit_state_dir: str | None, request: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    def apply(state: dict[str, Any]) -> None:
        require_run_config(state, config)
        fail(state["phase"] != "lens_prepare_pending", "illegal_transition", f"prepare-lenses is not legal in phase {state['phase']}")
        fail(state.get("pregate", {}).get("status") != "PASS", "pregate_required", "lenses require a passing deterministic pre-gate")
        wave = next(wave for wave in state["waves"] if wave["waveId"] == state["currentWave"])
        producers = [state["attempts"][attempt_id] for attempt_id in wave["attemptIds"]]
        projection_each = int(config["omp"].get("budget_projection", {}).get("lens_per_ticket", 0))
        projection = projection_each * 3 * len(producers)
        budget_check(state, config, projection)
        reviewer_role = config["omp"].get("lenses", {}).get("reviewer_role", "reviewer")

        items = []
        lens_ids = []
        for producer in producers:
            for lens in config["omp"].get("lenses", {}).get("names", ["Standards", "Spec", "Critic"]):
                selected = reviewer_candidate(state, config, producer, lens)
                review_no = lens_attempt_count(state, producer["attemptId"], lens) + 1
                lens_id = f"{producer['attemptId']}.lens.{lens.lower()}.{uuid.uuid4().hex[:6]}"
                dispatch_name = f"L{len(lens_ids) + 1}{lens}"
                assignment = {
                    "attemptId": lens_id,
                    "dispatchName": dispatch_name,
                    "producerAttemptId": producer["attemptId"],
                    "ticketId": producer["ticketId"],
                    "lens": lens,
                    "role": reviewer_role,
                    "reviewAttemptOrdinal": review_no,
                    "attemptOrdinal": review_no,
                    "lane": selected["lane"],
                    "laneAlias": config["omp"]["lanes"][selected["lane"]]["alias"],
                    "rank": selected["rank"],
                    "agent": selected["agent"],
                    "declaredAgent": selected["agent"],
                    "declaredModel": selected["witness"]["resolvedModel"],
                    "vendor": selected["witness"]["vendor"],
                    "family": selected["witness"]["family"],
                    "status": "prepared",
                    "tokens": None,
                    "durationMs": None,
                    "requests": None,
                    "observedAgent": None,
                    "observedAgentSource": None,
                    "observedModel": None,
                    "modelFallback": None,
                }
                state["lensAttempts"][lens_id] = assignment
                lens_ids.append(lens_id)
                items.append({
                    "name": dispatch_name,
                    "agent": selected["agent"],
                    "task": lens_task_text(lens, producer, state),
                    "effort": "hi" if lens == "Critic" else "med",
                    "outputSchema": lens_schema(),
                    "schemaMode": "strict",
                    "isolated": True,
                })

        task_input = {
            "context": f"Pocock fixed three-lens gate for run {state['runId']}. Reports are attempt-bound and read-only.",
            "tasks": items,
        }
        state["pendingDispatch"] = {
            "dispatchId": f"dispatch-{uuid.uuid4().hex}",
            "kind": "lenses",
            "status": "prepared",
            "attemptIds": lens_ids,
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
        pending = state.get("pendingDispatch", {})
        lens_attempts = [state["lensAttempts"][attempt_id] for attempt_id in pending.get("attemptIds", [])]
        fail(not lens_attempts or any(item.get("status") != "completed" for item in lens_attempts), "lens_result_invalid", "not every current lens completed")
        grouped: dict[str, dict[str, dict[str, Any]]] = {}
        for assignment in lens_attempts:
            report = assignment.get("result")
            fail(not isinstance(report, dict), "lens_result_invalid", f"lens {assignment['attemptId']} has no structured report")
            fail(report.get("lens") != assignment["lens"], "lens_result_invalid", f"lens identity mismatch for {assignment['attemptId']}")
            fail(report.get("attemptId") != assignment["producerAttemptId"], "stale_attempt", f"lens report is not bound to producer attempt {assignment['producerAttemptId']}")
            grouped.setdefault(assignment["producerAttemptId"], {})[assignment["lens"]] = report

        failures = []
        failed_producers: set[str] = set()
        for producer_id, reports in grouped.items():
            fail(set(reports) != {"Standards", "Spec", "Critic"}, "lens_result_invalid", f"producer {producer_id} lacks the fixed three lenses")
            critic = reports["Critic"]
            if critic.get("verdict") != "PASS":
                failures.append(f"Critic FAIL for {producer_id}")
                failed_producers.add(producer_id)
            for lens in ("Standards", "Spec"):
                fail(reports[lens].get("verdict") != "NO_VERDICT", "lens_result_invalid", f"{lens} must not emit PASS/FAIL")
                for finding in reports[lens].get("findings", []):
                    if isinstance(finding, dict) and finding.get("blocking") is True and finding.get("scope") == "introduced":
                        failures.append(f"blocking {lens} finding for {producer_id}: {finding.get('evidence')}")
                        failed_producers.add(producer_id)

        for lens_attempt in lens_attempts:
            lens_attempt["status"] = "accepted"

        state["adjudication"] = {"status": "FAIL" if failures else "PASS", "failures": failures, "lensAttemptIds": pending.get("attemptIds", [])}
        if failures:
            rollback_attempt_patches(cwd, state, failed_producers)
            for producer_id in grouped:
                producer = state["attempts"][producer_id]
                if producer_id in failed_producers:
                    producer["status"] = "review_failed"
                    increment_ticket_failures(state, "qualityFailures", [str(producer["ticketId"])])
                else:
                    producer["status"] = "accepted"
            state["retryTicketIds"] = sorted(str(state["attempts"][producer_id]["ticketId"]) for producer_id in failed_producers)
            state["phase"] = "repair_pending"
            state["blockedReason"] = "; ".join(failures)
            state["lastFailureKind"] = "capability"
        else:
            for producer_id in grouped:
                state["attempts"][producer_id]["status"] = "accepted"
            state["retryTicketIds"] = []
            state["phase"] = "accepted"
            state["blockedReason"] = None

    state, _ = mutate(cwd, explicit_state_dir, request, apply)
    return output(state)


def telemetry_log_path(config: dict[str, Any]) -> Path:
    return SKILL_DIR / config.get("telemetry", {}).get("log", "telemetry/routing-log.jsonl")


def telemetry_exists(config: dict[str, Any], run_id: str, ticket_id_value: str) -> bool:
    path = telemetry_log_path(config)
    if not path.is_file():
        return False
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            raise RuntimeFailure("telemetry_corrupt", f"invalid JSONL in {path}")
        if isinstance(row, dict) and row.get("run_id") == run_id and str(row.get("ticket")) == ticket_id_value and row.get("verdict") == "PASS":
            return True
    return False


def command_accept(cwd: Path, explicit_state_dir: str | None, request: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    telemetry_events: list[dict[str, Any]] = []

    def apply(state: dict[str, Any]) -> None:
        require_run_config(state, config)
        fail(state["phase"] != "accepted" or state.get("telemetryRecorded"), "illegal_transition", "accept requires an unrecorded accepted adjudication")
        sweep = require_sweep_integrity(state) if state.get("entry") == "sweep" else None
        accepted = [attempt for attempt in state["attempts"].values() if attempt.get("status") == "accepted"]
        fail(not accepted, "acceptance_invalid", "no accepted producer attempts exist")
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
            if telemetry_exists(config, state["runId"], ticket):
                attempt["status"] = "recorded"
                continue
            completed = subprocess.run(
                [sys.executable, str(TELEMETRY_TOOL), canonical(row), "--skill-dir", str(SKILL_DIR), "--run-id", state["runId"]],
                text=True,
                capture_output=True,
                check=False,
            )
            if completed.returncode not in {0, 2}:
                raise RuntimeFailure("telemetry_failed", completed.stderr.strip() or "telemetry writer rejected an acceptance record")
            telemetry_events.append({"ticket": ticket, "exitCode": completed.returncode, "stdout": completed.stdout.strip()})
            attempt["status"] = "recorded"
            if completed.returncode == 2:
                budget_exhausted = True

        if sweep is not None:
            sweep["acceptedTicketIds"] = sorted(set(sweep["acceptedTicketIds"]) | set(accepted_ticket_ids_now))
            set_sweep_progress(sweep)
            state["frontierExhausted"] = not sweep["remainingTicketIds"]
        state["telemetryRecorded"] = True
        state["budgetExhausted"] = budget_exhausted
        state["telemetryEvents"] = telemetry_events

    state, _ = mutate(cwd, explicit_state_dir, request, apply)
    return output(state, telemetry=telemetry_events)


def command_report(cwd: Path, explicit_state_dir: str | None, request: dict[str, Any]) -> dict[str, Any]:
    run_id = validate_run_id(request.get("runId"))
    state_path, lock_path = state_paths(cwd, explicit_state_dir, run_id)
    with with_lock(lock_path):
        state = read_state(state_path)
    return {"report": report(state)}


def command_status(cwd: Path, explicit_state_dir: str | None, request: dict[str, Any]) -> dict[str, Any]:
    run_id = validate_run_id(request.get("runId"))
    state_path, lock_path = state_paths(cwd, explicit_state_dir, run_id)
    with with_lock(lock_path):
        state = read_state(state_path)
    return output(state)


def command_hydrate(cwd: Path, explicit_state_dir: str | None, request: dict[str, Any]) -> dict[str, Any]:
    run_id = validate_run_id(request.get("runId"))
    state_path, lock_path = state_paths(cwd, explicit_state_dir, run_id)
    with with_lock(lock_path):
        state = read_state(state_path)
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
        "report",
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
            "status": lambda: command_status(cwd, args.state_dir, request),
            "report": lambda: command_report(cwd, args.state_dir, request),
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
