"""run-lane detect — probe vendor CLIs for presence + auth (no model quota).

Orchestrates one probe per *distinct transport* (not per lane): two codex
lanes share one login, so `codex login status` runs once and both lanes are
labelled with that result. Transport-specific argv and success-token parsing
live on the adapters (`probe_command` / `parse_probe`); this module only
collects transports from config, runs probes through the substrate axis, and
prints the machine-readable map.

Does NOT run the `run()` pipeline — no prompts, no artifacts, no model
witnesses, no model quota.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from types import SimpleNamespace

from . import adapters, config_resolve
from .adapters import Invocation
from .envelope import LaneError
from .substrate import RunLimits, SubprocessSubstrate

# skill/orchestrate/config.yaml — parents: run_lane -> tools -> orchestrate
DEFAULT_CONFIG = Path(__file__).resolve().parents[2] / "config.yaml"
PROBE_TIMEOUT_SECONDS = 30


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="run-lane detect")
    ap.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help="path to config.yaml (default: skill config.yaml)",
    )
    return ap


def lanes_by_transport(config: dict) -> dict[str, list[str]]:
    """Map each transport string to the sorted list of lane names on it."""
    lanes = (config.get("cross_provider") or {}).get("lanes") or {}
    by: dict[str, list[str]] = {}
    for name, raw in lanes.items():
        if not isinstance(raw, dict):
            continue
        transport = raw.get("transport")
        if not isinstance(transport, str) or not transport:
            continue
        by.setdefault(transport, []).append(name)
    for names in by.values():
        names.sort()
    return dict(sorted(by.items()))


def _probe_invocation(argv: list) -> Invocation:
    """Minimal Invocation for a presence/auth probe (no prompt, no cwd)."""
    return Invocation(
        argv=list(argv),
        env={},
        stdin_policy="devnull",
        cwd=None,
        prompt_addendum=None,
        log_file=None,
    )


def detect_transports(config: dict, substrate=None) -> dict:
    """Probe each distinct transport once; return the availability map.

    `substrate` is injectable for deterministic tests (fake runner that
    counts calls and returns canned `RunResult`s).
    """
    substrate = substrate or SubprocessSubstrate()
    by_transport = lanes_by_transport(config)
    result: dict = {}

    for transport, lane_names in by_transport.items():
        if transport not in adapters.ADAPTERS:
            result[transport] = {
                "cli": transport,
                "present": False,
                "logged_in": False,
                "evidence": f"no adapter registered for transport {transport!r}",
                "lanes": list(lane_names),
            }
            continue

        adapter = adapters.get_adapter(transport)
        lane_stub = SimpleNamespace(name=lane_names[0], transport=transport)
        argv = adapter.probe_command(lane_stub)
        if not isinstance(argv, list) or not argv:
            result[transport] = {
                "cli": transport,
                "present": False,
                "logged_in": False,
                "evidence": f"adapter {type(adapter).__name__} returned empty probe_command",
                "lanes": list(lane_names),
            }
            continue

        inv = _probe_invocation(argv)
        res = substrate.run(inv, RunLimits(idle_s=PROBE_TIMEOUT_SECONDS,
                                           max_s=PROBE_TIMEOUT_SECONDS))
        parsed = adapter.parse_probe(res)
        result[transport] = {
            "cli": str(argv[0]),
            "present": bool(parsed.get("present")),
            "logged_in": bool(parsed.get("logged_in")),
            "evidence": str(parsed.get("evidence") or ""),
            "lanes": list(lane_names),
        }

    return result


def main(argv: list | None = None, substrate=None) -> int:
    """CLI entry: parse args, probe transports, print JSON map to stdout.

    `substrate` is only for tests — production always uses SubprocessSubstrate.
    Unknown argv tokens are ignored so a bare `run-lane detect` (and callers
    that pass leftover flags) still works against the default config.
    """
    parser = build_parser()
    try:
        args, _unknown = parser.parse_known_args(list(argv) if argv is not None else [])
    except SystemExit as exc:
        code = exc.code
        if code is None:
            return 0
        return int(code) if isinstance(code, int) else 2

    try:
        config = config_resolve.load_config(args.config)
    except LaneError as exc:
        print(json.dumps({"error": exc.to_dict()}, ensure_ascii=False), file=sys.stderr)
        return 1

    try:
        mapping = detect_transports(config, substrate=substrate)
    except LaneError as exc:
        print(json.dumps({"error": exc.to_dict()}, ensure_ascii=False), file=sys.stderr)
        return 1

    print(json.dumps(mapping, ensure_ascii=False, indent=2))
    return 0
