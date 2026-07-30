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


# The two PERMANENT (config-level) reasons detect_transports can attach
# below, kept as a set so verify-install.sh's report (and any other reader)
# can tell them apart from a TRANSIENT probe failure without re-deriving the
# distinction: "no adapter" / "empty probe_command" will not go away on a
# retry (2026-07-30 critic-gate finding 5 — "повторите" is misleading advice
# for a config error, and misleading in the other direction is exactly what
# this whole fix exists to stop).
PERMANENT_LOGIN_PROBE_REASONS = frozenset({"no adapter registered", "empty probe_command"})


def _login_probe_status(res, parsed: dict) -> tuple:
    """Classify whether the transport-level probe itself answered at all,
    separately from what it answered.

    2026-07-30 00:27:53 бинарь agy перезаписал сам себя (самообновление)
    ровно во время пробы; проба упала, и это было записано как достоверное
    "не залогинен" — работающий лейн выглядел мёртвым. Правило владельца:
    лейн отключается только живым сигналом диспетча, не упавшей пробой.
    That incident is exactly the gap this function closes.

    2026-07-30 critic-gate finding 1 (introduced, load-bearing): the first
    cut of this fix tried to tell a failed probe apart from a genuine
    negative using only `RunResult` (timeout / empty output / `exit_code ==
    -1`), on the theory that "any nonzero exit is a failed probe". That
    theory is WRONG and was rejected here: a CLI that is honestly present
    and honestly prints a login invite often exits nonzero too, and treating
    every nonzero exit as a probe failure would throw away that confirmed
    negative. The three-way answer instead lives on the adapter, which is
    the only place that actually parsed the text — `parse_probe` now names
    its own outcome via `login_signal: "invite" | "success" | "none"`
    (`adapters.py`, "detect probes" section, has the full vocabulary).
    `logged_in` keeps meaning "the adapter parsed a confirmed logged-in
    state" (unchanged); `login_probe`/`login_probe_reason` record,
    independently, whether the probe produced anything to parse at all:
      - a timeout is always a failed probe (no answer arrived at all);
      - `parsed["present"] is False` is a real, reliable adapter finding
        (the CLI genuinely is not on PATH) — a legitimate negative, not a
        probe malfunction, so it stays `"ok"`;
      - an `exit_code == -1` (the substrate's sentinel for "the subprocess
        itself could not be run to completion", e.g. the self-overwrite
        case) remains a SUFFICIENT signal of failure on its own — but, per
        finding 1, no longer the ONLY one;
      - completely empty output, or a `login_signal` of `"none"` (or
        missing/unrecognized — an adapter that has not yet been updated to
        report it must not be silently read as a confident answer either),
        means the probe gave nothing trustworthy to read, so `"failed"`;
      - `login_signal` of `"invite"` or `"success"` is a confirmed answer
        either way, so `"ok"`.
    """
    if getattr(res, "timed_out", False):
        return "failed", "probe timed out"
    if not parsed.get("present"):
        return "ok", None
    if getattr(res, "exit_code", None) == -1:
        return "failed", "probe exited nonzero with no parseable output"
    text = f"{getattr(res, 'stdout', None) or ''}{getattr(res, 'stderr', None) or ''}"
    if not text.strip():
        return "failed", "empty output"
    if parsed.get("login_signal") in ("invite", "success"):
        return "ok", None
    return "failed", "probe produced no recognizable login signal"


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
                "login_probe": "failed",
                "login_probe_reason": "no adapter registered",
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
                "login_probe": "failed",
                "login_probe_reason": "empty probe_command",
                "evidence": f"adapter {type(adapter).__name__} returned empty probe_command",
                "lanes": list(lane_names),
            }
            continue

        inv = _probe_invocation(argv)
        res = substrate.run(inv, RunLimits(idle_s=PROBE_TIMEOUT_SECONDS,
                                           max_s=PROBE_TIMEOUT_SECONDS))
        parsed = adapter.parse_probe(res)
        login_probe, login_probe_reason = _login_probe_status(res, parsed)
        # Finding 2 (defensive, critic-gate 2026-07-30): a failed probe can
        # never prove a login. Enforced HERE rather than trusted from the
        # adapter, so a future adapter bug (or one not yet updated to set
        # `login_signal`) cannot leak `logged_in: True` out of a probe that
        # never actually confirmed anything.
        logged_in = bool(parsed.get("logged_in")) and login_probe != "failed"
        result[transport] = {
            "cli": str(argv[0]),
            "present": bool(parsed.get("present")),
            "logged_in": logged_in,
            "login_probe": login_probe,
            "login_probe_reason": login_probe_reason,
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
