"""Tests for validate_config.py at its one seam: CLI invocation.

argv (--skill-dir, optionally --exceptions) -> process exit code + stderr report.
Every fixture lives in a pytest tmp_path; the real skill/orchestrate/config.yaml
and prose are never touched.
"""
import subprocess
import sys
from pathlib import Path
from textwrap import dedent

VALIDATOR = Path(__file__).parent / "validate_config.py"

# A minimal but structurally complete config.yaml: every block the validator
# requires (SKILL.md header note + the ticket's stable-block list), correctly
# typed. Reused verbatim by fixtures that only need to perturb one thing.
VALID_CONFIG = dedent("""\
    version: 11
    updated: 2026-07-15

    shape:
      values:
        сборка:
          criterion: work splits into vertical slices (spec, then tickets with blocking edges)
          sequence: grill -> spec -> tickets -> frontier
        разбирательство:
          criterion: one thread, nothing to cut into slices
          sequence: straight to work; no spec, no tickets

    routing:
      tie_break: up
      classes:
        judgment: {route: [architect, critic], signals: [open design space]}
      hard_floors:
        - architecture and technology decisions
      escalation:
        ladder: [retry-same-agent, escalate-tier, report-blocker]
        failure_diagnosis: effort-vs-capability
        max_attempts_per_subtask: 2
        tiers: [haiku, sonnet, opus, fable]
        one_way: true
      overrides:
        up_routing: always-allowed

    gates:
      dual_lens_triggers:
        - production code headed for the main branch
      dual_lens_correctness_model: fable
      cross_model_third_lens: opt-in
      ui_live_check: required
      probe_responder_identity: required
      whole_diff_review_from_writers: 2
      review_loop:
        decision_context_pack: required-for-code
        pack_max_lines: 40
        finding_scopes: [introduced, pre-existing, decision-challenge]
        blocking_scope: introduced
        full_rounds_max: 2
        reverify: delta
        proportionality_stop: 2.0

    cross_provider:
      enabled: auto-detect
      staging_inputs_only: required
      auth: subscription-oauth-first
      execution:
        idle_default_s: 300
        max_default_s: null
      lanes:
        codex-critic: {model: gpt-5.6-sol, effort: xhigh}
      defaults:
        web_recon: gemini-recon
      promoted:
        codex-recon: {evidence: "x", as: "y"}
      pilots:
        exit_after_records: 10
        not_on_pilot: {}
      never_shift:
        - judgment-class tickets
      ultra_policy:
        critic_lanes: never
        builder_condition: genuinely hard AND latency-sensitive
        log_reason: required

    availability:
      fallbacks:
        architect: [fable, opus, report-blocker]
      reasons: [quota-window, subscription, connector-absent, connector-error]
      provider_map:
        anthropic: [fable, opus]
      surface_map:
        anthropic-subscription: [fable, opus]

    thresholds:
      first_try_pass_min: 0.80
      recalibrate_after_records_per_tier: 20
      critic_fail_rate_review_below: 0.05
      incident_review_signal: 2

    telemetry:
      log: telemetry/routing-log.jsonl
      stamp_records_with_config: true
      snapshot_dir: telemetry/config-snapshots
    """)


def make_skill_dir(tmp_path, config_text=VALID_CONFIG, skill_md="# Orchestrator Playbook\n",
                    references=None, exceptions_text=None):
    """Build a temp skill-dir fixture: config.yaml + SKILL.md + references/*.md."""
    (tmp_path / "config.yaml").write_text(config_text)
    (tmp_path / "SKILL.md").write_text(skill_md)
    refs_dir = tmp_path / "references"
    refs_dir.mkdir(exist_ok=True)
    for name, text in (references or {}).items():
        (refs_dir / name).write_text(text)
    tools_dir = tmp_path / "tools"
    tools_dir.mkdir(exist_ok=True)
    if exceptions_text is not None:
        (tools_dir / "config-ref-exceptions.txt").write_text(exceptions_text)
    return tmp_path


def run_validator(skill_dir, extra_args=None):
    args = [sys.executable, str(VALIDATOR), "--skill-dir", str(skill_dir)]
    if extra_args:
        args += extra_args
    return subprocess.run(args, capture_output=True, text=True)


def test_valid_config_and_prose_exits_zero_silently(tmp_path):
    skill_dir = make_skill_dir(tmp_path)
    result = run_validator(skill_dir)
    assert result.returncode == 0
    assert result.stderr == ""


def test_missing_required_block_is_reported_and_fails(tmp_path):
    broken_config = VALID_CONFIG.replace(
        dedent("""\
            gates:
              dual_lens_triggers:
                - production code headed for the main branch
              dual_lens_correctness_model: fable
              cross_model_third_lens: opt-in
              ui_live_check: required
              probe_responder_identity: required
              whole_diff_review_from_writers: 2
              review_loop:
                decision_context_pack: required-for-code
                pack_max_lines: 40
                finding_scopes: [introduced, pre-existing, decision-challenge]
                blocking_scope: introduced
                full_rounds_max: 2
                reverify: delta
                proportionality_stop: 2.0
            """),
        "",
    )
    assert "gates:" not in broken_config  # sanity: the fixture actually dropped the block
    skill_dir = make_skill_dir(tmp_path, config_text=broken_config)
    result = run_validator(skill_dir)
    assert result.returncode != 0
    assert "config.yaml" in result.stderr
    assert "gates" in result.stderr


def test_wrong_type_for_a_required_field_is_reported_and_fails(tmp_path):
    broken_config = VALID_CONFIG.replace("version: 11", 'version: "ten"')
    skill_dir = make_skill_dir(tmp_path, config_text=broken_config)
    result = run_validator(skill_dir)
    assert result.returncode != 0
    assert "config.yaml" in result.stderr
    assert "version" in result.stderr


def test_prose_reference_to_missing_config_key_is_reported_and_fails(tmp_path):
    skill_md = dedent("""\
        # Orchestrator Playbook
        Escalation caps live in config.yaml `routing.escalation.max_attempts_per_subtask`.
        There is no such knob as `routing.nonexistent_key` — a typo.
        File references like `config.yaml` and `SKILL.md` are not config keys
        and must never be flagged.
        """)
    reference_md = dedent("""\
        # Quality
        See `gates.review_loop.pack_max_lines` for the pack budget.
        """)
    skill_dir = make_skill_dir(
        tmp_path, skill_md=skill_md,
        references={"quality.md": reference_md},
    )
    result = run_validator(skill_dir)
    assert result.returncode != 0
    assert "SKILL.md" in result.stderr
    assert "routing.nonexistent_key" in result.stderr
    # the resolvable references and the two bare file mentions (config.yaml
    # itself is valid, so no schema violation either) must not be flagged
    assert "max_attempts_per_subtask" not in result.stderr
    assert "pack_max_lines" not in result.stderr
    assert "reference `config.yaml`" not in result.stderr
    assert "reference `SKILL.md`" not in result.stderr
    assert "quality.md" not in result.stderr


def test_exceptions_file_suppresses_a_known_non_config_reference(tmp_path):
    skill_md = dedent("""\
        # Orchestrator Playbook
        Detection returns `codex.loggedIn` in its JSON payload — a bridge
        response field, not a config.yaml path.
        """)
    exceptions_text = dedent("""\
        # non-config dotted references legitimately in prose
        codex.loggedIn   # agent-bridge --detect JSON field (references/cross-provider.md)
        """)
    skill_dir = make_skill_dir(tmp_path, skill_md=skill_md, exceptions_text=exceptions_text)
    result = run_validator(
        skill_dir,
        extra_args=["--exceptions", str(skill_dir / "tools" / "config-ref-exceptions.txt")],
    )
    assert result.returncode == 0
    assert result.stderr == ""


def test_sibling_skill_without_own_config_is_held_to_this_config(tmp_path):
    # ADR-0004: skill/<sibling>/SKILL.md with no config.yaml of its own shares
    # the main skill's config — its unresolved references must fail red.
    skill_dir = (tmp_path / "orchestrate")
    skill_dir.mkdir()
    make_skill_dir(skill_dir)
    sibling = tmp_path / "orchestrate-frontier"
    sibling.mkdir()
    (sibling / "SKILL.md").write_text(
        "# Frontier\nUses `routing.no_such_key` at entry.\n")
    result = run_validator(skill_dir)
    assert result.returncode == 1
    assert "orchestrate-frontier/SKILL.md" in result.stderr
    assert "routing.no_such_key" in result.stderr


def test_sibling_with_its_own_config_is_not_scanned(tmp_path):
    # A sibling carrying its own config.yaml is a separate skill (or a test
    # fixture): it validates against its own config, never against this one.
    skill_dir = (tmp_path / "orchestrate")
    skill_dir.mkdir()
    make_skill_dir(skill_dir)
    sibling = tmp_path / "standalone-skill"
    sibling.mkdir()
    (sibling / "config.yaml").write_text("version: 1\n")
    (sibling / "SKILL.md").write_text(
        "# Standalone\nRefers to `its.own_key` from its own config.\n")
    result = run_validator(skill_dir)
    assert result.returncode == 0
    assert result.stderr == ""


# A config whose fallback chain routes to a lane that is absent from
# provider_map — the exact drift (sol-xhigh / grok-4.5 outside the map) that let
# a critic grade its own vendor's work undetected.
_CONFIG_LANE_OUTSIDE_MAP = VALID_CONFIG.replace(
    "architect: [fable, opus, report-blocker]",
    "architect: [fable, opus, grok-4.5, report-blocker]")


def test_lane_absent_from_provider_map_is_flagged(tmp_path):
    skill_dir = make_skill_dir(tmp_path, config_text=_CONFIG_LANE_OUTSIDE_MAP)
    result = run_validator(skill_dir)
    assert result.returncode == 1
    assert "grok-4.5" in result.stderr
    assert "provider_map" in result.stderr


def test_chain_terminals_are_not_required_in_maps(tmp_path):
    # report-blocker is a terminal behaviour, not a routable lane: it must NOT be
    # demanded in provider_map/surface_map even though it appears in every chain.
    skill_dir = make_skill_dir(tmp_path)
    result = run_validator(skill_dir)
    assert result.returncode == 0
    assert "report-blocker" not in result.stderr


# Slice 1's third condition (ADR-0005 §4): a routable name must resolve to
# something real — a cross_provider.lanes entry, an availability.aliases entry,
# or a known Claude agent — independently of whether the vendor/surface maps
# happen to still list it. Here 'ghost-lane' is *present* in both maps (so the
# first two conditions are silent) but is neither a lane nor an alias: a dead
# or mistyped routing target that the old two-condition check would have missed.
_CONFIG_GHOST_ROUTE = (
    VALID_CONFIG
    .replace(
        "architect: [fable, opus, report-blocker]",
        "architect: [fable, opus, ghost-lane, report-blocker]")
    .replace(
        "    anthropic: [fable, opus]",
        "    anthropic: [fable, opus, ghost-lane]")
    .replace(
        "    anthropic-subscription: [fable, opus]",
        "    anthropic-subscription: [fable, opus, ghost-lane]")
)


def test_routable_name_not_a_lane_or_alias_is_flagged(tmp_path):
    skill_dir = make_skill_dir(tmp_path, config_text=_CONFIG_GHOST_ROUTE)
    result = run_validator(skill_dir)
    assert result.returncode == 1
    assert "ghost-lane" in result.stderr
    assert "resolves to neither a" in result.stderr
    # the maps DID list it — this failure must come from the new third
    # condition, not from the pre-existing provider_map/surface_map checks
    assert "is absent from" not in result.stderr


# Positive counterpart: an alias (sol-xhigh -> codex-critic) must resolve
# through availability.aliases without needing a second, duplicate entry of
# its own in provider_map/surface_map — the exact de-duplication slice 1 does
# to config.yaml's real provider_map/surface_map (sol-xhigh, grok-4.5 removed
# as separate entries once the alias dictionary exists).
_CONFIG_ALIAS_RESOLVES_WITHOUT_DUPLICATE_MAP_ENTRY = (
    VALID_CONFIG
    .replace(
        "architect: [fable, opus, report-blocker]",
        "architect: [fable, opus, sol-xhigh, report-blocker]")
    .replace(
        "  provider_map:\n    anthropic: [fable, opus]",
        "  aliases:\n    sol-xhigh: codex-critic\n"
        "  provider_map:\n    anthropic: [fable, opus]\n"
        "    openai: [codex-critic]")
    .replace(
        "  surface_map:\n    anthropic-subscription: [fable, opus]",
        "  surface_map:\n    anthropic-subscription: [fable, opus]\n"
        "    openai-codex: [codex-critic]")
)


def test_alias_resolves_without_duplicate_map_entry(tmp_path):
    skill_dir = make_skill_dir(
        tmp_path, config_text=_CONFIG_ALIAS_RESOLVES_WITHOUT_DUPLICATE_MAP_ENTRY)
    result = run_validator(skill_dir)
    assert result.returncode == 0
    assert result.stderr == ""


# --- ADR-0007 latency-envelope regress guard -------------------------------


def test_burst_only_lane_without_latency_envelope_is_a_violation(tmp_path):
    """ADR-0007 §2.10: a burst-only transport (silent while working) MUST declare
    a latency_envelope, else the idle floor would kill correct-but-slow work."""
    broken = VALID_CONFIG.replace(
        "    codex-critic: {model: gpt-5.6-sol, effort: xhigh}",
        "    codex-critic: {model: gpt-5.6-sol, effort: xhigh}\n"
        "    kimi-lane: {model: k, transport: kimi-cli-headless}")
    assert "kimi-cli-headless" in broken  # sanity: the burst-only lane was added
    skill_dir = make_skill_dir(tmp_path, config_text=broken)
    result = run_validator(skill_dir)
    assert result.returncode == 1
    assert "latency_envelope" in result.stderr


def test_missing_execution_floor_is_a_violation(tmp_path):
    """The executor must always have a liveness floor (idle_default_s)."""
    broken = VALID_CONFIG.replace(
        "  execution:\n    idle_default_s: 300\n    max_default_s: null\n", "")
    assert "execution:" not in broken  # sanity: the block was dropped
    skill_dir = make_skill_dir(tmp_path, config_text=broken)
    result = run_validator(skill_dir)
    assert result.returncode == 1
    assert "idle_default_s" in result.stderr
