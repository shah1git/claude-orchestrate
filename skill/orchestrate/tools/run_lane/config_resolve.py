"""config_resolve.py — `--lane` + `availability.aliases` -> `LaneRecord`.

`load_config` mirrors the load-yaml idiom every other tool in `tools/` uses
(telemetry_append.py:81-91, validate_config.py's own `open()+yaml.safe_load`)
— open, `yaml.safe_load`, `or {}` against an empty file. The one deliberate
difference: `run-lane`'s contract names an explicit `--config <config.yaml>`
file path (ADR-0005), not a skill-dir default the way the other two tools
do, so `load_config` takes the file path directly.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from .envelope import LaneError

# Last-resort liveness floor (ADR-0007): the maximum SILENCE tolerated when
# neither the lane's `latency_envelope` nor `cross_provider.execution` names an
# idle default. A conservative dead-transport detector, not a per-lane arbitrary
# wall clock — `validate_config` still requires the execution block so this floor
# is a safety net, not the configured value.
_IDLE_FLOOR_S = 300.0


def load_config(config_path: Path) -> dict:
    config_path = Path(config_path)
    if not config_path.is_file():
        raise LaneError("config", f"config not found: {config_path}")
    with open(config_path, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


@dataclass(frozen=True)
class LaneRecord:
    """The slice of a `cross_provider.lanes.<name>` entry run-lane needs.
    `capabilities_override` is the F-Caps hybrid (ADR-0005 поправка B): empty
    on every lane today, but the mechanism is load-bearing from slice 3 on —
    a lane MAY carry a `capabilities:` mapping that overrides one or more of
    the adapter's CAPS constants (see `adapters.LaneAdapter.effective_capabilities`)."""

    name: str
    transport: str
    model: str | None
    effort: str | None
    vendor: str | None
    surface: str | None
    sandbox: str | None
    roles: tuple
    status: str | None
    capabilities_override: dict
    raw: dict
    # Liveness bounds (ADR-0007), resolved from the lane's `latency_envelope`
    # over `cross_provider.execution` defaults. `idle_timeout_s` — max silence
    # tolerated; `max_envelope_s` — the lane's recorded latency ceiling (`None`
    # = no ceiling, idle alone governs). A CLI `--idle-timeout`/`--timeout`
    # overrides these downstream (__main__). Defaulted so test constructors and
    # any non-resolve builder need not restate them; resolve_lane always sets them.
    idle_timeout_s: float = _IDLE_FLOOR_S
    max_envelope_s: float | None = None


def resolve_lane(config: dict, lane_name: str) -> LaneRecord:
    """`name = aliases.get(lane, lane)` (the same alias-then-lookup idiom as
    validate_config.py:257) then `cross_provider.lanes[name]`. An alias is an
    effort/model display name for a canonical lane, never a second lane of
    its own (availability.aliases, slice 1) — resolution always lands on one
    real `cross_provider.lanes` entry or fails loudly."""
    aliases = (config.get("availability") or {}).get("aliases") or {}
    resolved_name = aliases.get(lane_name, lane_name)
    lanes = (config.get("cross_provider") or {}).get("lanes") or {}
    raw = lanes.get(resolved_name)
    if raw is None:
        raise LaneError(
            "config",
            f"lane '{lane_name}' does not resolve (alias -> '{resolved_name}') "
            f"against cross_provider.lanes")
    if not isinstance(raw, dict):
        raise LaneError("config", f"lane '{resolved_name}' is not a mapping in config.yaml")

    transport = raw.get("transport")
    if not transport:
        raise LaneError("config", f"lane '{resolved_name}' has no transport field")

    capabilities_override = raw.get("capabilities") or {}
    if not isinstance(capabilities_override, dict):
        raise LaneError(
            "config", f"lane '{resolved_name}'.capabilities must be a mapping, "
            f"got {type(capabilities_override).__name__}")

    # Liveness bounds: lane `latency_envelope` over `cross_provider.execution`
    # defaults over the last-resort idle floor (ADR-0007).
    execution = (config.get("cross_provider") or {}).get("execution") or {}
    envelope = raw.get("latency_envelope") or {}
    if not isinstance(envelope, dict):
        raise LaneError(
            "config", f"lane '{resolved_name}'.latency_envelope must be a mapping, "
            f"got {type(envelope).__name__}")
    idle_timeout_s = envelope.get(
        "idle_s", execution.get("idle_default_s", _IDLE_FLOOR_S))
    max_envelope_s = envelope.get("max_s", execution.get("max_default_s"))

    return LaneRecord(
        name=resolved_name,
        transport=transport,
        model=raw.get("model"),
        effort=raw.get("effort"),
        vendor=raw.get("vendor"),
        surface=raw.get("surface"),
        sandbox=raw.get("sandbox"),
        roles=tuple(raw.get("roles") or ()),
        status=raw.get("status"),
        capabilities_override=capabilities_override,
        idle_timeout_s=float(idle_timeout_s),
        max_envelope_s=None if max_envelope_s is None else float(max_envelope_s),
        raw=raw,
    )
