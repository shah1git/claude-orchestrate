#!/usr/bin/env python3
"""run_lane.__main__ — the CLI: parse -> resolve lane -> build invocation ->
run substrate -> capture artifact + witness -> print one envelope.

Contract (ADR-0005, поправка B):
    run-lane --lane <name> --config <config.yaml> --prompt-file <file>
             --workdir <abs INPUTS dir> --out <abs artifact path>
             [--role <name>] [--schema <file>] [--timeout <s>] [--resume <id>]
             [--substrate subprocess|orca-terminal] [--effort <level>]
             [--model <slug>]

`--effort`/`--model` default to the resolved lane's own `effort`/`model`
fields and only win when explicitly given — the lestница эскалации
(codex-code.escalation and friends) is what needs the override, not a new
per-lane alias for every model×effort combination (that is exactly the N×M
duplication ADR-0005 exists to remove).

Every branch below prints exactly ONE JSON envelope and never a bare
traceback (ADR-0005: "ГРОМКИЙ отказ, не тихое отбрасывание") — a `LaneError`
raised anywhere in the pipeline is caught once, here, and turned into the
envelope's `error` field.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import adapters, artifact, config_resolve, envelope
from .envelope import LaneError
from .substrate import OrcaTerminalSubstrate, SubprocessSubstrate

DEFAULT_TIMEOUT_SECONDS = 300

# Substrate registry keyed by the CLI's own vocabulary (--substrate
# subprocess|orca-terminal) — tests substitute this mapping to inject a
# FakeSubstrate without touching argument parsing.
SUBSTRATES = {
    "subprocess": SubprocessSubstrate,
    "orca-terminal": OrcaTerminalSubstrate,
}

_SCHEMA_ENFORCEMENT = {"strict": "enforced", "prompt": "prompt_asked", "no": "none"}


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="run-lane", add_help=False)
    ap.add_argument("--lane", required=True)
    ap.add_argument("--config", type=Path, required=True)
    ap.add_argument("--prompt-file", type=Path, required=True)
    ap.add_argument("--workdir", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--role")
    ap.add_argument("--schema", type=Path)
    ap.add_argument("--timeout", type=int)
    ap.add_argument("--resume")
    ap.add_argument("--substrate", choices=sorted(SUBSTRATES), default="subprocess")
    ap.add_argument("--effort")
    ap.add_argument("--model")
    return ap


def _classify_run_result(res) -> dict | None:
    """§5: a nonzero exit or a timeout is `transport-death`, not `quality` —
    run-lane only classifies; re-dispatch is the lead's decision, and the
    escalation ladder must not be spent on a transport failure."""
    if res.timed_out:
        return {"class": "transport-death",
                "message": f"lane timed out after {res.duration_ms}ms without exiting"}
    if res.exit_code != 0:
        stderr_tail = (res.stderr or "").strip()
        return {"class": "transport-death",
                "message": f"lane exited {res.exit_code}",
                "evidence": stderr_tail[-400:] if stderr_tail else None}
    return None


def run(args: argparse.Namespace) -> dict:
    config = config_resolve.load_config(args.config)
    lane = config_resolve.resolve_lane(config, args.lane)

    adapter = adapters.get_adapter(lane.transport)
    try:
        caps = adapter.effective_capabilities(lane)
    except NotImplementedError as exc:
        raise LaneError("config", str(exc)) from exc

    resolved_effort = args.effort if args.effort is not None else lane.effort
    resolved_model = args.model if args.model is not None else lane.model

    if resolved_effort and caps.supports_effort == "no":
        raise LaneError(
            "config", f"lane '{lane.name}' does not support --effort "
            f"(capabilities.supports_effort: no)")
    if args.schema and caps.supports_schema == "no":
        raise LaneError(
            "config", f"lane '{lane.name}' does not support --schema "
            f"(capabilities.supports_schema: no)")

    req = adapters.InvocationRequest(
        prompt_file=args.prompt_file,
        workdir=args.workdir,
        out=args.out,
        role=args.role,
        schema=args.schema,
        timeout=args.timeout,
        resume=args.resume,
        effort=resolved_effort,
        model=resolved_model,
    )

    try:
        inv = adapter.build_invocation(lane, req)
    except NotImplementedError as exc:
        raise LaneError("config", str(exc)) from exc

    substrate_impl = SUBSTRATES[args.substrate]()
    res = substrate_impl.run(inv, args.timeout or DEFAULT_TIMEOUT_SECONDS)

    art = artifact.capture(args.out)
    model_obs = adapter.parse_model_witness(res, inv)
    usage = adapter.parse_usage(res)
    session_id = adapter.parse_session_id(res)
    printed_text, printed_truncated = adapter.parse_printed_text(res)
    schema_enforcement = _SCHEMA_ENFORCEMENT.get(caps.supports_schema, "none")

    error = _classify_run_result(res)
    if error is None and model_obs.error:
        # A witness that failed to confirm the model is a transport-level
        # defect (wrong argv/flag-order reached the backend), not a quality
        # judgment about the deliverable — same bucket as a nonzero exit.
        error = {"class": "transport-death", "message": model_obs.error}

    return envelope.build(
        lane=lane.name,
        transport=lane.transport,
        substrate=args.substrate,
        model_declared=resolved_model,
        model_observed=model_obs.observed,
        model_verification=caps.model_verification,
        effort=resolved_effort,
        artifact=art,
        printed_text=printed_text,
        printed_truncated=printed_truncated,
        schema_enforcement=schema_enforcement,
        duration_ms=res.duration_ms,
        usage=usage,
        session_id=session_id,
        sandbox={"profile": lane.sandbox, "applied": None, "violations": []},
        command=inv.argv,
        evidence={"log_path": inv.log_file, "model_line": model_obs.evidence},
        exit_code=res.exit_code,
        error=error,
    )


def _error_envelope(args: argparse.Namespace, exc: LaneError) -> dict:
    """A LaneError raised before (or instead of) a real run still prints the
    one envelope shape — fields we never got to compute are honestly null,
    not omitted (ADR-0005: never silent)."""
    return {
        "lane": args.lane,
        "transport": None,
        "substrate": args.substrate,
        "ok": False,
        "model_declared": args.model,
        "model_observed": None,
        "effort": args.effort,
        "artifact": {"path": str(args.out), "present": False, "bytes": 0, "sha256": None},
        "printed_text": "",
        "printed_truncated": False,
        "schema_enforcement": None,
        "durationMs": 0,
        "usage": None,
        "sessionId": None,
        "sandbox": None,
        "command": None,
        "evidence": None,
        "error": exc.to_dict(),
    }


def main(argv: list) -> int:
    args = build_parser().parse_args(argv)
    try:
        env = run(args)
    except LaneError as exc:
        env = _error_envelope(args, exc)
    print(json.dumps(env, ensure_ascii=False, indent=2))
    return 0 if env.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
