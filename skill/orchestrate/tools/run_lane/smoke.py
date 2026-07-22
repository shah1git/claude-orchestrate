"""run-lane smoke — one trivial real call per selected, available lane.

Confirms the end-to-end combat path (resolve → build_invocation → substrate
→ artifact-from-disk → envelope) without inventing a second pipeline.
Availability is decided by an injectable `detect_fn` (default: wrap
`run_lane.detect`); the combat path is an injectable `run_fn` (default:
`run_lane.__main__.run`). Tests supply both fakes so no live vendor is
touched and detect can stay a stub while S2b lands in parallel.

Quota guardrail: without explicit `--lanes` or `--all`, nothing is executed.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

from . import adapters, config_resolve
from .envelope import LaneError

# skill/orchestrate/config.yaml — parents: run_lane -> tools -> orchestrate
DEFAULT_CONFIG = Path(__file__).resolve().parents[2] / "config.yaml"

TRIVIAL_PROMPT = (
    "Write exactly the word OK into the artifact file named in this prompt. "
    "Do nothing else.\n"
)

# Compact table columns for the human-readable report.
_TABLE_HEADERS = ("lane", "ok", "model_observed", "durationMs", "error.class")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="run-lane smoke", add_help=True)
    group = ap.add_mutually_exclusive_group()
    group.add_argument(
        "--lanes",
        metavar="NAMES",
        help="comma-separated lane names to smoke (aliases allowed)",
    )
    group.add_argument(
        "--all",
        action="store_true",
        help="smoke every configured lane that has a transport",
    )
    ap.add_argument(
        "--config",
        type=Path,
        default=None,
        help="path to config.yaml (required for a real smoke run)",
    )
    ap.add_argument(
        "--timeout",
        type=int,
        default=None,
        help="per-lane timeout in seconds (passed through to run)",
    )
    return ap


def list_configured_lanes(config: dict) -> list[str]:
    """Canonical lane names from cross_provider.lanes that declare a transport."""
    lanes = (config.get("cross_provider") or {}).get("lanes") or {}
    names: list[str] = []
    for name, raw in lanes.items():
        if isinstance(raw, dict) and raw.get("transport"):
            names.append(name)
    return sorted(names)


def parse_lane_selector(args: argparse.Namespace, config: dict) -> list[str]:
    """Resolve --lanes / --all into an ordered list of canonical lane names."""
    if args.all:
        return list_configured_lanes(config)
    assert args.lanes is not None
    selected: list[str] = []
    seen: set[str] = set()
    for part in args.lanes.split(","):
        token = part.strip()
        if not token:
            continue
        # resolve_lane applies availability.aliases; keep first-seen order.
        record = config_resolve.resolve_lane(config, token)
        if record.name not in seen:
            seen.add(record.name)
            selected.append(record.name)
    return selected


def _default_detect_fn(config: dict) -> dict[str, bool]:
    """Lane-name → available, derived from `run_lane.detect` when real.

    detect's public map is transport-keyed (`present` + `logged_in` + `lanes`).
    A lane is available only when its transport is both present and logged in.
    If detect is still the S2a stub (no `detect_transports`), every lane is
    reported unavailable — never invent a green light that would burn quota.
    """
    from . import detect

    lanes = list_configured_lanes(config)
    if not hasattr(detect, "detect_transports"):
        return {name: False for name in lanes}

    mapping = detect.detect_transports(config)
    out: dict[str, bool] = {name: False for name in lanes}
    if not isinstance(mapping, dict):
        return out
    for _transport, info in mapping.items():
        if not isinstance(info, dict):
            continue
        available = bool(info.get("present") and info.get("logged_in"))
        for lane_name in info.get("lanes") or []:
            out[str(lane_name)] = available
    return out


def _default_run_fn(args: argparse.Namespace) -> dict:
    """Combat path: the same `run()` used by `run-lane run` (artifact-from-disk)."""
    from . import __main__ as run_lane_main

    return run_lane_main.run(args)


def lane_model_verification(config: dict, lane_name: str) -> str:
    """CAPS.model_verification for a resolved lane (default 'none' on errors)."""
    try:
        lane = config_resolve.resolve_lane(config, lane_name)
        adapter = adapters.get_adapter(lane.transport)
        caps = adapter.effective_capabilities(lane)
        return caps.model_verification
    except (LaneError, NotImplementedError, AttributeError, TypeError):
        return "none"


def grade_envelope(envelope: dict, verification: str) -> bool:
    """Smoke pass: envelope.ok (compute_ok) AND witness present when required."""
    if not envelope.get("ok"):
        return False
    if verification != "none" and not envelope.get("model_observed"):
        return False
    return True


def _is_lane_available(availability: dict, lane_name: str) -> bool:
    """Accept bool values or transport-style dicts with present/logged_in."""
    if lane_name not in availability:
        return False
    value = availability[lane_name]
    if isinstance(value, bool):
        return value
    if isinstance(value, dict):
        return bool(value.get("present") and value.get("logged_in"))
    return bool(value)


def _error_class(envelope: dict | None) -> str:
    if not envelope:
        return ""
    err = envelope.get("error")
    if isinstance(err, dict):
        return str(err.get("class") or "")
    return ""


def smoke_lane(
    *,
    lane_name: str,
    config: dict,
    config_path: Path,
    timeout: int | None,
    availability: dict,
    run_fn,
) -> dict:
    """Run (or skip) one lane; return a compact row dict for the report."""
    verification = lane_model_verification(config, lane_name)

    if not _is_lane_available(availability, lane_name):
        return {
            "lane": lane_name,
            "ok": "SKIPPED",
            "model_observed": "",
            "durationMs": "",
            "error.class": "",
            "status": "SKIPPED",
            "skipped": True,
            "passed": None,
            "verification": verification,
            "envelope": None,
        }

    with tempfile.TemporaryDirectory(prefix="run-lane-smoke-") as tmp:
        tmp_path = Path(tmp)
        workdir = tmp_path / "workdir"
        workdir.mkdir()
        prompt_file = tmp_path / "prompt.md"
        prompt_file.write_text(TRIVIAL_PROMPT, encoding="utf-8")
        out_path = tmp_path / "artifact.out"

        args = SimpleNamespace(
            lane=lane_name,
            config=Path(config_path),
            prompt_file=prompt_file,
            workdir=workdir,
            out=out_path,
            role=None,
            schema=None,
            timeout=timeout,          # per-lane max ceiling (ADR-0007); None → lane envelope
            idle_timeout=None,        # liveness bound falls to the lane's resolved idle
            resume=None,
            substrate="subprocess",
            effort=None,
            model=None,
        )

        try:
            envelope = run_fn(args)
        except LaneError as exc:
            envelope = {
                "lane": lane_name,
                "ok": False,
                "model_observed": None,
                "durationMs": 0,
                "error": exc.to_dict(),
            }
        except Exception as exc:  # noqa: BLE001 — surface unexpected failures in the row
            envelope = {
                "lane": lane_name,
                "ok": False,
                "model_observed": None,
                "durationMs": 0,
                "error": {"class": "config", "message": f"{type(exc).__name__}: {exc}"},
            }

    passed = grade_envelope(envelope, verification)
    model_obs = envelope.get("model_observed")
    duration = envelope.get("durationMs", "")
    return {
        "lane": lane_name,
        "ok": "true" if passed else "false",
        "model_observed": "" if model_obs is None else str(model_obs),
        "durationMs": "" if duration is None or duration == "" else str(duration),
        "error.class": _error_class(envelope),
        "status": "ok" if passed else "FAIL",
        "skipped": False,
        "passed": passed,
        "verification": verification,
        "envelope": envelope,
    }


def format_table(rows: list[dict]) -> str:
    """Fixed-column table: lane | ok | model_observed | durationMs | error.class."""
    cells = [_TABLE_HEADERS] + [
        (
            str(r.get("lane", "")),
            str(r.get("ok", "")),
            str(r.get("model_observed", "")),
            str(r.get("durationMs", "")),
            str(r.get("error.class", "")),
        )
        for r in rows
    ]
    widths = [max(len(row[i]) for row in cells) for i in range(len(_TABLE_HEADERS))]
    lines: list[str] = []
    for idx, row in enumerate(cells):
        line = "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row))
        lines.append(line.rstrip())
        if idx == 0:
            lines.append("  ".join("-" * widths[i] for i in range(len(_TABLE_HEADERS))))
    return "\n".join(lines)


def _quota_guardrail_message() -> str:
    return (
        "run-lane smoke: refusing to burn model quota without explicit "
        "--lanes NAME[,NAME...] or --all\n"
        "usage: run-lane smoke (--lanes NAMES | --all) --config CONFIG [--timeout N]"
    )


def run_smoke(
    *,
    lane_names: list[str],
    config: dict,
    config_path: Path,
    timeout: int | None,
    detect_fn,
    run_fn,
) -> list[dict]:
    """Probe availability once, then smoke each selected lane in order."""
    availability = detect_fn(config) or {}
    if not isinstance(availability, dict):
        availability = {}

    # If detect_fn returned a transport-keyed map (detect.detect_transports
    # shape), expand it to lane_name → bool so callers can inject either form.
    sample_val = next(iter(availability.values()), None)
    if availability and isinstance(sample_val, dict) and (
        "present" in sample_val or "lanes" in sample_val or "logged_in" in sample_val
    ):
        expanded: dict[str, bool] = {}
        for _key, info in availability.items():
            if not isinstance(info, dict):
                continue
            if "lanes" in info:
                ok = bool(info.get("present") and info.get("logged_in"))
                for lane_name in info.get("lanes") or []:
                    expanded[str(lane_name)] = ok
            else:
                # Already lane-keyed dict values with present/logged_in.
                # Keys of the outer map are lane names in this branch only when
                # values look like probe results without a nested lanes list —
                # keep per-key interpretation in _is_lane_available instead.
                pass
        if expanded:
            availability = expanded

    rows: list[dict] = []
    for name in lane_names:
        rows.append(
            smoke_lane(
                lane_name=name,
                config=config,
                config_path=config_path,
                timeout=timeout,
                availability=availability,
                run_fn=run_fn,
            )
        )
    return rows


def main(
    argv: list | None = None,
    *,
    detect_fn=None,
    run_fn=None,
) -> int:
    """CLI entry for `run-lane smoke`.

    `detect_fn` / `run_fn` are test injection points; production uses
    `_default_detect_fn` and `_default_run_fn` (the real detect + run path).
    """
    parser = build_parser()
    try:
        args, _unknown = parser.parse_known_args(list(argv) if argv is not None else [])
    except SystemExit as exc:
        code = exc.code
        if code is None:
            return 0
        return int(code) if isinstance(code, int) else 2

    detect_fn = detect_fn if detect_fn is not None else _default_detect_fn
    run_fn = run_fn if run_fn is not None else _default_run_fn

    # --- quota guardrail: no selector → never call detect or run ------------
    if not args.all and args.lanes is None:
        # No lane selector → refuse to burn quota, with or without --config.
        # Both detect and smoke are shipped (not stubs); a bare `run-lane smoke`
        # is a misuse, answered with the guardrail hint rather than a stub blob.
        print(_quota_guardrail_message(), file=sys.stderr)
        return 0

    if args.config is None:
        print(
            "run-lane smoke: --config CONFIG is required when using --lanes or --all",
            file=sys.stderr,
        )
        return 2

    try:
        config = config_resolve.load_config(args.config)
    except LaneError as exc:
        print(json.dumps({"error": exc.to_dict()}, ensure_ascii=False), file=sys.stderr)
        return 1

    try:
        lane_names = parse_lane_selector(args, config)
    except LaneError as exc:
        print(json.dumps({"error": exc.to_dict()}, ensure_ascii=False), file=sys.stderr)
        return 1

    if not lane_names:
        print("run-lane smoke: no lanes selected", file=sys.stderr)
        return 1

    rows = run_smoke(
        lane_names=lane_names,
        config=config,
        config_path=Path(args.config),
        timeout=args.timeout,
        detect_fn=detect_fn,
        run_fn=run_fn,
    )

    # Human table first; machine-readable JSON after a blank line.
    print(format_table(rows))
    report = {
        "mode": "smoke",
        "lanes": [
            {
                "lane": r["lane"],
                "ok": r["ok"],
                "model_observed": r["model_observed"],
                "durationMs": r["durationMs"],
                "error.class": r["error.class"],
                "status": r["status"],
            }
            for r in rows
        ],
    }
    print()
    print(json.dumps(report, ensure_ascii=False, indent=2))

    # Non-zero only when at least one executed lane failed. SKIPPED is not fail.
    any_fail = any(r.get("passed") is False for r in rows)
    return 1 if any_fail else 0
