#!/usr/bin/env python3
"""validate_config.py — validator for config.yaml and the /orchestrate skill's prose.

Fails red on two independent classes of drift:
  (a) config.yaml structurally departs from its schema (missing required
      block, wrong value type);
  (b) prose (SKILL.md, references/*.md) references a `block.key`-shaped
      config path in backticks that does not resolve against config.yaml.

Usage:
    validate_config.py [--skill-dir DIR] [--exceptions FILE]

DIR defaults to the skill/orchestrate directory this script lives under
(tools/.. ); FILE defaults to tools/config-ref-exceptions.txt next to this
script. Exit 0 and silent on a clean pair; exit 1 with one violation per
stderr line (each naming its file) otherwise.
"""
import argparse
import re
import sys
from pathlib import Path

import yaml

DEFAULT_SKILL_DIR = Path(__file__).resolve().parent.parent
DEFAULT_EXCEPTIONS = Path(__file__).resolve().parent / "config-ref-exceptions.txt"

# -----------------------------------------------------------------------------
# Schema: SCHEMA's top-level keys (below) are exactly the required-block set —
# chosen at ticket #12's dispatch as config.yaml's stable core (version,
# routing, gates, cross_provider, availability, thresholds, telemetry).
# config.yaml carries no such "stable" marking itself; this is a validator-side
# choice, not a provenance claim about the config. It deliberately excludes
# `stakes`/`shape` and other still-evolving top-level keys — `stakes` was
# renamed to `shape` in v10, exactly the kind of rename this validator must
# not hard-code around. Nested `children` are listed only for keys the prose
# (SKILL.md/references) actually names as operative knobs; an unlisted nested
# key is neither required nor rejected — this stays a floor, not a mirror of
# every field. `schema_violations()` requires every SCHEMA top-level key by
# passing `required=True` to `_walk_schema` only at that top call; no separate
# required-block list is needed or kept.
# -----------------------------------------------------------------------------

NUMBER = (int, float)  # thresholds mix ints and floats; bool is excluded explicitly below

SCHEMA = {
    "version": {"type": int},
    "routing": {"type": dict, "children": {
        "tie_break": {"type": str},
        "classes": {"type": dict},
        "hard_floors": {"type": list},
        "escalation": {"type": dict, "children": {
            "ladder": {"type": list},
            "failure_diagnosis": {"type": str},
            "max_attempts_per_subtask": {"type": int},
            "tiers": {"type": list},
            "one_way": {"type": bool},
        }},
        "overrides": {"type": dict},
    }},
    "gates": {"type": dict, "children": {
        # v11 (slice 3, issue #7): dual_lens_triggers, dual_lens_correctness_model, and
        # cross_model_third_lens are retired along with the mechanism they configured —
        # the schema does not carry a dead canon's vocabulary forward. `lenses` is the
        # fixed three-lens gate that replaced it (gates.lenses.{standards,spec,critic}).
        "lenses": {"type": dict},
        "ui_live_check": {"type": str},
        "probe_responder_identity": {"type": str},
        "whole_diff_review_from_writers": {"type": int},
        "review_loop": {"type": dict, "children": {
            "decision_context_pack": {"type": str},
            "pack_max_lines": {"type": int},
            "finding_scopes": {"type": list},
            "blocking_scope": {"type": str},
            # v11 (slice 3, issue #7): full_rounds_max/proportionality_stop moved from a
            # per-profile dict ({low:.., standard:.., high:..}) to a flat scalar ceiling —
            # ADR-0002, verification depth no longer scales by any axis. Strict replacement,
            # not an additive widening: the schema is the floor that catches drift, and a
            # type accepting both shapes forever would silently let the retired dict form
            # come back (lead decision, slice 3 review).
            "full_rounds_max": {"type": NUMBER},
            "reverify": {"type": str},
            "proportionality_stop": {"type": NUMBER},
        }},
    }},
    "cross_provider": {"type": dict, "children": {
        "enabled": {"type": str},
        "staging_inputs_only": {"type": str},
        "auth": {"type": str},
        "lanes": {"type": dict},
        "pilots": {"type": dict},
        "never_shift": {"type": list},
        "ultra_policy": {"type": dict},
    }},
    "availability": {"type": dict, "children": {
        "fallbacks": {"type": dict},
        "reasons": {"type": list},
        # v27 (slice 1, ADR-0005 §4): the alias dictionary a routable name resolves
        # through before map-completeness is checked — see map_completeness_violations.
        "aliases": {"type": dict},
    }},
    "thresholds": {"type": dict, "children": {
        "first_try_pass_min": {"type": NUMBER},
        "recalibrate_after_records_per_tier": {"type": int},
        "critic_fail_rate_review_below": {"type": NUMBER},
        "incident_review_signal": {"type": int},
    }},
    "telemetry": {"type": dict, "children": {
        "log": {"type": str},
        "stamp_records_with_config": {"type": bool},
        "snapshot_dir": {"type": str},
    }},
}


def _type_matches(value, expected) -> bool:
    """isinstance with the bool/int trap closed: bool is a Python int
    subclass, so an explicit `type: int` must reject True/False, and an
    explicit `type: bool` must reject plain ints."""
    types = expected if isinstance(expected, tuple) else (expected,)
    if bool in types:
        return isinstance(value, bool)
    if isinstance(value, bool):
        return False
    return isinstance(value, types)


def _walk_schema(data, schema, path, config_path, violations, required):
    """Recursively check `data` against `schema`. `required` is `True` only
    at the top-level call (SCHEMA's top-level keys are the required-block
    set); every recursive call into a `children` dict passes `required=False`
    — a block's inner keys are never mandatory on their own, only type-checked
    when present. A block's own presence is what's mandatory, checked one
    level up, before recursion happens at all."""
    for key, node in schema.items():
        full_path = f"{path}.{key}" if path else key
        if key not in data:
            if required:
                violations.append(
                    f"{config_path}: missing required block '{full_path}'")
            continue
        value = data[key]
        if not _type_matches(value, node["type"]):
            expected = node["type"]
            expected_name = (
                "/".join(t.__name__ for t in expected)
                if isinstance(expected, tuple) else expected.__name__
            )
            violations.append(
                f"{config_path}: '{full_path}' has type {type(value).__name__}, "
                f"expected {expected_name}")
            continue
        children = node.get("children")
        if children and isinstance(value, dict):
            _walk_schema(value, children, full_path, config_path, violations,
                         required=False)


def load_config(config_path: Path):
    """Parse config.yaml. Returns (data, violations) — a parse failure is
    itself a violation, not a crash."""
    try:
        text = config_path.read_text()
    except OSError as exc:
        return None, [f"{config_path}: cannot read config ({exc})"]
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return None, [f"{config_path}: invalid YAML ({exc})"]
    if not isinstance(data, dict):
        return None, [f"{config_path}: top level must be a mapping, got {type(data).__name__}"]
    return data, []


def schema_violations(config_data: dict, config_path: Path) -> list:
    """Only SCHEMA's top-level keys are mandatory; nested keys are validated
    by type when present and are never required on their own — see
    `_walk_schema`'s `required` handling."""
    violations = []
    _walk_schema(config_data, SCHEMA, "", config_path, violations, required=True)
    return violations


# Terminal chain elements are honest behaviours, not routable lanes/agents: they
# have no vendor and no surface, so the completeness check below skips them.
_CHAIN_TERMINALS = frozenset({
    "report-blocker", "lead-inline", "skip-third-lens", "sonnet-inline-note",
})

# Claude-side agent/model identifiers that legitimately appear as fallback-chain
# targets without being either a cross_provider.lanes entry or an
# availability.aliases entry — routing.classes' own vocabulary (its `route`
# values) plus the escalation ladder's tiers, plus the two roles no chain
# enumerates elsewhere (spec-lens is a lens name; lead is the session model).
# Slice 1's third condition (ADR-0005 §4) treats these as resolved; anything
# else that isn't a lane or an alias is a dead or mistyped routing target.
_CLAUDE_AGENTS = frozenset({
    "architect", "builder", "scout", "critic",
    "opus", "fable", "sonnet", "haiku",
    "spec-lens", "lead",
})


def _chain_members(chain) -> list:
    """Flatten a fallback chain into the names it can route to. An element is
    either a bare name (str) or an equal-weight pool `{"equal": [...]}`."""
    names = []
    for element in chain:
        if isinstance(element, dict) and "equal" in element:
            names.extend(element["equal"])
        elif isinstance(element, str):
            names.append(element)
    return names


def map_completeness_violations(config_data: dict, config_path: Path) -> list:
    """Every name that a fallback chain can route to must resolve to a vendor
    (availability.provider_map) AND a surface (availability.surface_map) — else
    the independence_filter and the provider breaker silently skip it. A third,
    independent condition (slice 1, ADR-0005 §4) checks that the name resolves
    to something REAL in the first place — a cross_provider.lanes entry, an
    availability.aliases entry, or a known Claude agent — regardless of whether
    it happens to be listed in the vendor/surface maps.

    This is the check config.yaml's own comment has long *promised* ("Валидатор
    проверяет полноту") but that never existed in code — the exact gap that let
    the sol-xhigh / grok-4.5 aliases sit in judging chains outside provider_map,
    so a critic could grade its own vendor's work undetected. Making it a
    deterministic gate means the drift cannot recur silently.

    Aliases (e.g. sol-xhigh, grok-4.5) are effort/model display names for a
    canonical lane, not second lanes of their own (availability.aliases, slice
    1) — vendor/surface completeness is checked against the LANE an alias
    resolves to, not a second map entry for the alias itself."""
    violations = []
    availability = config_data.get("availability")
    if not isinstance(availability, dict):
        return violations
    fallbacks = availability.get("fallbacks") or {}
    provider_map = availability.get("provider_map") or {}
    surface_map = availability.get("surface_map") or {}
    aliases = availability.get("aliases") or {}
    lanes = (config_data.get("cross_provider") or {}).get("lanes") or {}

    in_providers = {name for names in provider_map.values() for name in names}
    in_surfaces = {name for names in surface_map.values() for name in names}

    routable = set()
    for chain in fallbacks.values():
        if isinstance(chain, list):
            routable.update(_chain_members(chain))
    routable -= _CHAIN_TERMINALS

    for name in sorted(routable):
        resolved = aliases.get(name, name)
        if resolved not in in_providers:
            violations.append(
                f"{config_path}: routable lane '{name}' is absent from "
                f"availability.provider_map — independence_filter cannot resolve "
                f"its vendor and will silently skip it")
        if surface_map and resolved not in in_surfaces:
            violations.append(
                f"{config_path}: routable lane '{name}' is absent from "
                f"availability.surface_map — the provider breaker cannot resolve "
                f"its surface and will silently skip it")
        if (name not in lanes and name not in aliases
                and name not in _CLAUDE_AGENTS):
            violations.append(
                f"{config_path}: routable name '{name}' resolves to neither a "
                f"cross_provider.lanes entry, an availability.aliases entry, "
                f"nor a known Claude agent — dead or mistyped routing target")
    return violations


DOTTED_REF_RE = re.compile(r"`([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+)`")

# A backtick span shaped like a dotted path but ending in one of these is a
# filename (config.yaml, SKILL.md, quality.md, run-external-agent.mjs, …),
# never a config.yaml key path — excluded generically instead of by an
# ever-growing per-filename list.
FILE_EXTENSIONS = {"md", "py", "sh", "yaml", "yml", "json", "toml", "mjs", "js", "ts", "txt"}


def _looks_like_file_reference(ref: str) -> bool:
    return ref.rsplit(".", 1)[-1].lower() in FILE_EXTENSIONS


def _resolves(ref: str, config_data: dict) -> bool:
    node = config_data
    for segment in ref.split("."):
        if not isinstance(node, dict) or segment not in node:
            return False
        node = node[segment]
    return True


def prose_violations(skill_dir: Path, config_data: dict, exceptions: set) -> list:
    """Resolve every `block.key`-shaped backtick reference in SKILL.md and
    references/*.md against config_data; unresolved (and not excepted)
    references are violations."""
    violations = []
    prose_files = []
    skill_md = skill_dir / "SKILL.md"
    if skill_md.exists():
        prose_files.append(skill_md)
    references_dir = skill_dir / "references"
    if references_dir.is_dir():
        prose_files.extend(sorted(references_dir.glob("*.md")))
    # Sibling entrance skills (ADR-0004): a skill/*/SKILL.md with NO config.yaml
    # of its own shares this config.yaml as its single source of values, so its
    # prose is held to the same reference discipline as the main playbook's.
    # (A sibling that carries its own config.yaml is a separate skill — or a
    # test fixture — and validates against its own config, not this one.)
    for sibling in sorted(skill_dir.parent.glob("*/SKILL.md")):
        if (sibling.resolve() != skill_md.resolve()
                and not (sibling.parent / "config.yaml").exists()):
            prose_files.append(sibling)

    for path in prose_files:
        try:
            rel = path.relative_to(skill_dir)
        except ValueError:  # sibling skill — name it relative to skill/
            rel = path.relative_to(skill_dir.parent)
        for lineno, line in enumerate(path.read_text().splitlines(), start=1):
            for match in DOTTED_REF_RE.finditer(line):
                ref = match.group(1)
                if _looks_like_file_reference(ref):
                    continue
                if ref in exceptions:
                    continue
                if not _resolves(ref, config_data):
                    violations.append(
                        f"{rel}:{lineno}: unresolved config reference "
                        f"`{ref}` (not in config.yaml, not in exceptions)")
    return violations


def load_exceptions(exceptions_path: Path) -> set:
    if not exceptions_path.exists():
        return set()
    entries = set()
    for line in exceptions_path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        entry = stripped.split("#", 1)[0].strip()
        if entry:
            entries.add(entry)
    return entries


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skill-dir", type=Path, default=DEFAULT_SKILL_DIR,
                         help="directory containing config.yaml, SKILL.md, references/")
    parser.add_argument("--exceptions", type=Path, default=None,
                         help="path to the prose-reference exceptions list "
                              "(default: tools/config-ref-exceptions.txt next to this script)")
    args = parser.parse_args(argv)

    skill_dir = args.skill_dir
    exceptions_path = args.exceptions or DEFAULT_EXCEPTIONS

    config_data, violations = load_config(skill_dir / "config.yaml")
    if config_data is not None:
        violations += schema_violations(config_data, skill_dir / "config.yaml")
        violations += map_completeness_violations(
            config_data, skill_dir / "config.yaml")
        exceptions = load_exceptions(exceptions_path)
        violations += prose_violations(skill_dir, config_data, exceptions)

    if violations:
        for v in violations:
            print(v, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
