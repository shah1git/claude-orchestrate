"""envelope.py — the ADR-0005 error taxonomy, the `ok` formula, and the
conveyor-belt assembly of the one JSON envelope `run-lane` ever prints.

Deliberately the one module every other module in this package is allowed to
depend on (config_resolve/adapters/substrate all raise `LaneError` from
here): the error classes are a single vocabulary, not a per-module
invention, for the exact reason ADR-0005 exists — a silently-diverging
vocabulary is how the two documented 2026-07-21 incidents happened.
"""
from __future__ import annotations

# The five-value vocabulary (ADR-0005 §5 / design-runlane.md §5, +
# `artifact-too-small` 2026-07-30). A sixth spelling is refused at
# construction time (ValueError) rather than silently accepted — the whole
# point of a class taxonomy is that it stays closed. `artifact-too-small` is,
# like `transport-death`, built as a plain dict at its point of origin
# (`build()` below) rather than raised through `LaneError` — it is listed
# here anyway because the vocabulary this module documents covers every
# value the envelope's `error.class` can carry, not only the ones that
# happen to travel via an exception.
ERROR_CLASSES = {"config", "transport-death", "hardening-gate", "quality",
                  "artifact-too-small"}


class LaneError(Exception):
    """Raised by config_resolve/adapters/substrate for any run-lane-detected
    failure that must reach the caller as a classified `error`, never as a
    bare Python traceback (ADR-0005: "ГРОМКИЙ отказ, не тихое
    отбрасывание")."""

    def __init__(self, error_class: str, message: str, evidence: str | None = None):
        if error_class not in ERROR_CLASSES:
            raise ValueError(
                f"unknown error.class {error_class!r} — must be one of "
                f"{sorted(ERROR_CLASSES)}")
        super().__init__(message)
        self.error_class = error_class
        self.message = message
        self.evidence = evidence

    def to_dict(self) -> dict:
        out = {"class": self.error_class, "message": self.message}
        if self.evidence is not None:
            out["evidence"] = self.evidence
        return out


def compute_ok(*, exit_code: int | None, artifact_bytes: int,
                model_verification: str, model_declared, model_observed,
                error: dict | None, min_artifact_bytes: int = 0) -> bool:
    """ADR-0005 §"ok:true требует ОДНОВРЕМЕННО": zero exit code AND a usable
    artifact AND (model_observed == model_declared, when a witness exists).

    "Usable artifact" (2026-07-30 fix): a zero-byte file is treated as
    ABSENT, not present — прогон 30.07 showed why a bare presence check is
    not enough on its own (see the 369-byte case below), but the trivial
    empty-file case is folded into the very same rule rather than kept as a
    separate exception, so "present" can never again mean merely "a dentry
    exists on disk". `min_artifact_bytes` raises that usability floor
    further: it is a run-lane flag (`--min-artifact-bytes N`, N declared by
    the TICKET up front — override: ticket-declared-only, the same
    discipline as `gates.pre_gate.max_diff_lines`), never guessed or
    tightened after the fact. Live incident this exists to catch: прогон
    30.07 wrote a 369-byte artifact whose entire content was "записать файл
    невозможно: песочница read-only" — the file existed, so the old
    presence-only check awarded `ok:true` to a lane that could not actually
    write its deliverable.

    The model-witness conjunct is dropped exactly when
    `model_verification == "none"` (design §5): a lane with no witness at
    all must never be structurally incapable of `ok:true` — that would make
    the weak-witness case look like a permanent failure rather than an
    honestly-unverified pass. An already-classified `error` (hardening-gate,
    a substrate-level config failure, an oversize-artifact classification, …)
    always forces `ok:false`, independent of the other conjuncts — evidence
    collected after a hard failure is not trustworthy enough to award
    `ok:true` on top of it.
    """
    if error is not None:
        return False
    if exit_code != 0:
        return False
    # max(1, ...): the zero-byte-is-absent floor applies even when no ticket
    # has declared a --min-artifact-bytes threshold (the default 0 must not
    # be read as "any size, including empty, passes").
    if artifact_bytes < max(1, min_artifact_bytes):
        return False
    if model_verification != "none" and model_observed != model_declared:
        return False
    return True


def build(*, lane: str, transport: str | None, substrate: str,
          model_declared, model_observed, model_verification: str,
          effort, artifact: dict, printed_text: str, printed_truncated: bool,
          schema_enforcement: str | None, duration_ms: int, usage,
          session_id, sandbox, command, evidence, exit_code: int | None,
          error: dict | None = None) -> dict:
    """Assemble the one envelope shape (ADR-0005 §"Ответ — один конверт
    JSON"), plus `substrate` (поправка B) sitting next to `transport`.

    `min_artifact_bytes` travels on `artifact` itself (`artifact.capture`
    stamps the threshold it was checked against as `artifact["min_bytes"]`)
    rather than as a separate `build()` parameter — the envelope carries the
    threshold it actually used without any extra plumbing, so a routing-log
    record built from this envelope is self-contained (quality.md §7): it
    never has to be read next to the ticket that declared the flag to know
    what "too small" meant for this particular run.
    """
    artifact_bytes = int((artifact or {}).get("bytes") or 0)
    min_artifact_bytes = int((artifact or {}).get("min_bytes") or 0)
    if error is None and min_artifact_bytes > 0 and artifact_bytes < min_artifact_bytes:
        # 2026-07-30: the ticket-declared floor (--min-artifact-bytes,
        # override: ticket-declared-only) was crossed — a classified error,
        # not merely a quiet `ok:false`, so the routing-log record names
        # exactly what was wrong with the deliverable.
        error = {
            "class": "artifact-too-small",
            "message": (f"artifact is {artifact_bytes} bytes, below the "
                        f"ticket-declared minimum of {min_artifact_bytes} bytes"),
            "bytes": artifact_bytes,
            "min_artifact_bytes": min_artifact_bytes,
        }
    ok = compute_ok(
        exit_code=exit_code,
        artifact_bytes=artifact_bytes,
        min_artifact_bytes=min_artifact_bytes,
        model_verification=model_verification,
        model_declared=model_declared,
        model_observed=model_observed,
        error=error,
    )
    return {
        "lane": lane,
        "transport": transport,
        "substrate": substrate,
        "ok": ok,
        "model_declared": model_declared,
        "model_observed": model_observed,
        "effort": effort,
        "artifact": artifact,
        "printed_text": printed_text,
        "printed_truncated": printed_truncated,
        "schema_enforcement": schema_enforcement,
        "durationMs": duration_ms,
        "usage": usage,
        "sessionId": session_id,
        "sandbox": sandbox,
        "command": command,
        "evidence": evidence,
        "error": error,
    }
