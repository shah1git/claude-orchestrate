---
name: orca_orchestrate
description: Execute an already-prepared ticket frontier as lead orchestrator, with every worker lane dispatched as a terminal inside an Orca-managed worktree instead of the harness's own Agent tool / vendor-CLI subprocess — native per-vendor authorization, worktree and process isolation supplied by Orca. A thin sibling of orchestrate-frontier — same entry protocol, routing, three-lens gates, synthesis; the only difference is the default transport (orca-terminal). Expects the owner to have walked the Pocock spine SOLO before invoking — optionally /wayfinder, then /grill-with-docs → /to-spec → /to-tickets — so the tracker already holds ready-for-agent tickets. Use when such tickets exist AND the run should be driven from Orca ("работай фронтир через orca", "запусти тикеты в orca", "фронтир на подложке orca", "orca frontier", "orchestrate the tickets in Orca"). Do NOT use for raw tasks without a published spec and tickets — that is /orchestrate's job. Do NOT use when Orca is unavailable or the run should stay on the harness's own transport — that is /orchestrate-frontier's job; this skill defers to it rather than duplicating it.
argument-hint: [фильтр тикетов, или пусто — весь фронтир]
effort: xhigh
---

# Orca Frontier Orchestrator — the Orca-transport execution entrance (ADR-0006)

You are the **lead agent** of the *execution* phase, on Orca's transport. This head
carries **no execution prose of its own** (ADR-0006): everything about the run — entry
protocol, provenance gate, frontier discovery, routing, dispatch, the three-lens quality
gate, synthesis, telemetry — **is**
[orchestrate-frontier/SKILL.md](../orchestrate-frontier/SKILL.md) **verbatim**, which is
itself Steps 3–5 of the sibling `orchestrate` playbook (route · dispatch · gate ·
synthesize) plus the whole of `config.yaml` — ADR-0006's single execution core. Read
`orchestrate-frontier/SKILL.md` first; this file states only the one override it exists
to apply, the same sibling-reference technique frontier already uses for the core
(ADR-0006, "Что уже верно").

Task filter (when invoked as `/orca_orchestrate <фильтр>`): $ARGUMENTS — same semantics
as `orchestrate-frontier`'s filter, passed straight through; empty means the whole open
frontier of the current repo's tracker.

## The one difference: default transport

Per ADR-0006, `orca_orchestrate` differs from `orchestrate-frontier` **only** in default
transport. Where the harness head dispatches each Claude worker via the Agent tool and
each cross-provider lane via its documented vendor-CLI form (`cross_provider.lanes`,
[references/cross-provider.md](../orchestrate/references/cross-provider.md)), this head
dispatches **every** lane — Claude and cross-provider alike — as a terminal inside an
Orca-managed worktree:

- **Every lane rides an Orca terminal**, not a harness subprocess and not the Agent
  tool. Orca supervises the whole agent CLI (Claude, Codex, grok, kimi, agy) inside its
  own worktree — the same mechanism already driving the `kimi-k3` lane today
  (`cross_provider.lanes`, transport `kimi-cli-headless`, driven by an Orca terminal per
  that lane's own config comment; ADR-0005, §Claude-работники).
- **Authorization is native and legal.** Orca launches each vendor's own official CLI
  under that CLI's own OAuth login — never a shared client masquerading as another
  vendor's app, the exact ban-risk class the vendor-OAuth map warns against. Native
  spawn of the official binary is what makes Orca a safe transport, not merely a
  convenient one.
- **Worktree and process isolation come from Orca**, not from the harness's own worktree
  machinery. The sibling core's isolation *policy* — `fan_out.worktree_isolation_from_writers`,
  `fan_out.stale_base_max_behind` — still applies unconditionally; only its execution
  substrate changes.

Everything else — which lane for which ticket, the three-lens gate (`gates.lenses`),
escalation, telemetry, synthesis — is decided exactly as `orchestrate-frontier` decides
it, unchanged.

## Invariants (ADR-0006 — not re-derived here)

- The lead stays the session model (`cross_provider.never_shift`): Orca supervises
  *workers*, never the lead's own orchestration judgment.
- `config.yaml` is the single source of settings for all three heads (`orchestrate`,
  `orchestrate-frontier`, this one) — nothing here overrides it; this file only names
  which default it applies.
- The three-lens gate is unconditional in every head; transport never touches
  verification depth (ADR-0005).
- Transport is chosen **by presence**, never by preference: no Orca on this run → fall
  back to `orchestrate-frontier`'s harness/vendor-CLI transport → if the cross-provider
  surface is also absent, the portable floor is Claude workers only. The floor never
  degrades further.

## If a third transport arrives

Recorded revisit trigger (ADR-0006, open question): `orca_orchestrate` and
`orchestrate-frontier` are two named heads only because there are two transports today.
A third transport wanting its own head is the wear signal that collapses all
transport-heads into **one**, taking a `transport` argument, rather than adding a third
name. Not this file's call to make — recorded so the next transport doesn't quietly get
a fourth thin head instead.

## What stays with `/orchestrate-frontier` and `/orchestrate`

Runs where Orca is unavailable, or where the owner deliberately wants the harness's own
transport — that is `/orchestrate-frontier`, unchanged. Raw tasks with no published
spine artifacts (spec, tickets) are still `/orchestrate`'s inline-spine job; this head
owns none of that decomposition either — it only swaps the transport under an
already-prepared frontier.
