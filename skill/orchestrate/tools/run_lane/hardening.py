"""hardening.py — F6 (ADR-0005 §"План миграции срезами", срез 5): applies
`grok_hardening`/`kimi_hardening` (config.yaml:903-930, siblings of
`cross_provider.lanes`) to a built `Invocation`, and enforces the
fail-closed sandbox gate that has to run AFTER a grok invocation
(config.yaml `grok_hardening.preflight_verify`/`dispatch_gate`,
`config.yaml:914-917`) as code, not as a comment a lead has to remember to
honour by hand.

Two independent responsibilities, deliberately kept as separate functions
so a caller — a future `__main__.run()` wiring, or a test — can exercise
either without the other:

  - `apply(lane, inv, config)` — mixes the declared env switches into
    `inv.env` via `dataclasses.replace`, the same idiom every other
    `Invocation` mutation in this package uses. Pure, side-effect-free,
    never touches `inv.argv`.
  - `ensure_config_file(lane, config, repo_root, home)` — bootstraps the
    hardening's own `config_file` into its target location under `home`
    IF it is not already there (`~/.grok/config.toml` /
    `~/.kimi-code/config.toml`) — never overwrites an operator's own edits.
  - `gate(lane, ...)` — the fail-closed POST-RUN check: no positive
    evidence of an applied sandbox profile -> `LaneError('hardening-gate',
    ...)`, `ok:false`, never a silent pass (ADR-0005 §5: "ГРОМКИЙ отказ, не
    тихое отбрасывание"). Only `grok_hardening` declares a
    `preflight_verify`/`dispatch_gate` in config.yaml — `kimi_hardening`
    carries no such field, because kimi has no filesystem sandbox of its
    own to apply in the first place (config.yaml:928, `isolation` note:
    "своей песочницы нет -> worktree + OS/контейнер"). `gate()` is
    therefore a deliberate no-op for any lane whose `hardening:` key is not
    `grok_hardening` — not an oversight.

GAP (flagged loudly, per the ticket's own boundary clause): this module is
NOT YET WIRED into `run_lane.__main__.run()`. Wiring `apply()` between
`adapter.build_invocation()` and `substrate.run()` (design-runlane.md §2
layer 4), and `gate()` right after `substrate.run()` for a grok/kimi lane,
is a change to the pipeline's own orchestration (`__main__.py`), which sits
outside this slice's edit boundary (`adapters.py` + this module + tests
only). This module is complete and independently tested against the
config.yaml shapes it reads; the two call sites are a follow-up.
"""
from __future__ import annotations

import dataclasses
from pathlib import Path

from .envelope import LaneError

# `lane.raw["hardening"]` values this module knows how to apply
# (config.yaml `cross_provider.grok_hardening` / `.kimi_hardening`).
_KNOWN_HARDENING_KEYS = ("grok_hardening", "kimi_hardening")

# Directory a hardening key's config_file bootstraps into, under `home`.
_TARGET_DIR_NAMES = {"grok_hardening": ".grok", "kimi_hardening": ".kimi-code"}

# Only grok_hardening declares a fail-closed post-run check (config.yaml:
# 914-917) — see the module docstring for why kimi is correctly exempt.
_GATED_HARDENING_KEY = "grok_hardening"


def hardening_key(lane) -> str | None:
    """The `hardening:` key the lane declares. `LaneRecord`'s typed fields
    (config_resolve.py) don't surface it separately — it lives in
    `LaneRecord.raw`, the untyped pass-through of the lane's own config.yaml
    mapping — so this reads `raw` directly rather than waiting on a
    dedicated `LaneRecord.hardening` field slice 1 never added."""
    raw = getattr(lane, "raw", None) or {}
    key = raw.get("hardening")
    return key if key in _KNOWN_HARDENING_KEYS else None


def applies_to(lane) -> bool:
    return hardening_key(lane) is not None


def apply(lane, inv, config: dict):
    """Mix `cross_provider.<hardening_key>.env` into `inv.env`. Hardening's
    own values WIN on a key collision — an adapter's own env is a
    good-faith default (e.g. `GrokAdapter` already sets `GROK_SANDBOX` from
    the lane's declared sandbox profile), but hardening is meant to survive
    even a swapped/tampered config file (config.yaml's own phrasing:
    "дублируют config, переживают подмену файла") — it has to be the last
    word, not the first. A no-op — returns `inv` unchanged — for any lane
    without a recognised `hardening:` key."""
    key = hardening_key(lane)
    if key is None:
        return inv
    block = (config.get("cross_provider") or {}).get(key) or {}
    hardening_env = block.get("env") or {}
    if not isinstance(hardening_env, dict):
        raise LaneError(
            "config", f"cross_provider.{key}.env must be a mapping, got "
            f"{type(hardening_env).__name__}")
    merged_env = {**inv.env, **hardening_env}
    return dataclasses.replace(inv, env=merged_env)


def ensure_config_file(lane, config: dict, repo_root, home) -> tuple:
    """Bootstrap `cross_provider.<hardening_key>.config_file` into its
    target location under `home` IF it is not already there. Never
    overwrites an existing file — an operator's own edits at the target win
    over the repo's shipped default. `home`/`repo_root` are injected
    parameters (never `Path.home()`/a hardcoded repo path read directly),
    so a test never touches the real machine's home directory. Returns
    `(target_path, installed: bool)`; `(None, False)` for a lane with no
    recognised hardening key."""
    key = hardening_key(lane)
    if key is None:
        return None, False
    block = (config.get("cross_provider") or {}).get(key) or {}
    config_file = block.get("config_file")
    if not config_file:
        raise LaneError("config", f"cross_provider.{key} has no config_file field")

    target = Path(home) / _TARGET_DIR_NAMES[key] / "config.toml"
    if target.is_file():
        return target, False

    source = Path(repo_root) / config_file
    if not source.is_file():
        raise LaneError(
            "config", f"cross_provider.{key}.config_file not found: {source}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    return target, True


def gate(lane, *, sandbox_events_path=None, stderr: str = "") -> None:
    """Fail-closed post-run check (config.yaml `grok_hardening.
    preflight_verify`/`dispatch_gate`, `config.yaml:914-917`): raises
    `LaneError('hardening-gate', ...)` — never a silent pass — unless the
    sandbox-events journal positively carries `ProfileApplied`. A stderr
    line matching `ApplyFailed`/"sandbox could not be applied" fails the
    same way even if the journal happens to be stale from a previous run.
    A no-op for any lane whose hardening key is not `grok_hardening` (see
    module docstring — kimi has no sandbox of its own to gate on)."""
    if hardening_key(lane) != _GATED_HARDENING_KEY:
        return
    if "ApplyFailed" in stderr or "sandbox could not be applied" in stderr:
        raise LaneError(
            "hardening-gate",
            "grok sandbox ApplyFailed reported in stderr — fail-closed",
            evidence=stderr[-400:])
    if sandbox_events_path is None or not Path(sandbox_events_path).is_file():
        raise LaneError(
            "hardening-gate",
            f"no sandbox-events evidence at {sandbox_events_path} — cannot "
            f"confirm a sandbox profile was applied (fail-closed)")
    text = Path(sandbox_events_path).read_text(encoding="utf-8", errors="replace")
    if "ProfileApplied" not in text:
        raise LaneError(
            "hardening-gate",
            f"{sandbox_events_path} carries no ProfileApplied event — "
            f"fail-closed")
