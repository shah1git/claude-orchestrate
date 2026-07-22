"""envelope.py — the ADR-0005 error taxonomy, the `ok` formula, and the
conveyor-belt assembly of the one JSON envelope `run-lane` ever prints.

Deliberately the one module every other module in this package is allowed to
depend on (config_resolve/adapters/substrate all raise `LaneError` from
here): the error classes are a single vocabulary, not a per-module
invention, for the exact reason ADR-0005 exists — a silently-diverging
vocabulary is how the two documented 2026-07-21 incidents happened.
"""
from __future__ import annotations

# The four-value vocabulary (ADR-0005 §5 / design-runlane.md §5). A fifth
# spelling is refused at construction time (ValueError) rather than silently
# accepted — the whole point of a class taxonomy is that it stays closed.
ERROR_CLASSES = {"config", "transport-death", "hardening-gate", "quality"}


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


def compute_ok(*, exit_code: int | None, artifact_present: bool,
                model_verification: str, model_declared, model_observed,
                error: dict | None) -> bool:
    """ADR-0005 §"ok:true требует ОДНОВРЕМЕННО": zero exit code AND artifact
    present AND (model_observed == model_declared, when a witness exists).

    The third conjunct is dropped exactly when `model_verification == "none"`
    (design §5): a lane with no witness at all must never be structurally
    incapable of `ok:true` — that would make the weak-witness case look like
    a permanent failure rather than an honestly-unverified pass. An
    already-classified `error` (hardening-gate, a substrate-level config
    failure, …) always forces `ok:false`, independent of the three
    conjuncts — evidence collected after a hard failure is not trustworthy
    enough to award `ok:true` on top of it.
    """
    if error is not None:
        return False
    if exit_code != 0:
        return False
    if not artifact_present:
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
    JSON"), plus `substrate` (поправка B) sitting next to `transport`."""
    ok = compute_ok(
        exit_code=exit_code,
        artifact_present=bool(artifact and artifact.get("present")),
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
