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
        raw=raw,
    )
