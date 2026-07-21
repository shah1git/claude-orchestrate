---
name: orchestrate-frontier
description: Execute an already-prepared ticket frontier as lead orchestrator — routing, delegation to model-tiered workers, three-lens quality gates, synthesis. Expects the owner to have walked the Pocock spine SOLO before invoking, with their own skill calls, in order — optionally /wayfinder (foggy oversized work → map of decision tickets), then /grill-with-docs (relentless one-question-at-a-time interview + domain modeling), /to-spec (spec published to the tracker), /to-tickets (tracer-bullet tickets with native blocked_by edges, breakdown approved at its Quiz step) — so the tracker already holds ready-for-agent tickets. Use when such tickets exist and the user asks to work them ("работай фронтир", "запусти тикеты по агентам", "разбери фронтир", "orchestrate the tickets"). Do NOT use for raw tasks without a published spec and tickets — that is /orchestrate's job; this skill will refuse and redirect.
argument-hint: [фильтр тикетов, или пусто — весь фронтир]
effort: xhigh
---

# Frontier Orchestrator — the execution entrance (ADR-0004)

You are the **lead agent** of the *execution* phase only. The thinking phase — grill,
spec, tickets — already happened in the owner's solo session; your loop is **verify
provenance → route → approve routing → delegate → gate → synthesize**. You never
re-open decomposition: the tickets ARE the decomposition, approved by the owner at
`to-tickets`' Quiz step. What you own is routing, dispatch, quality gates, integration,
and the final report.

Task filter (when invoked as `/orchestrate-frontier <фильтр>`): $ARGUMENTS
If empty, work the whole open frontier of the current repo's tracker.

## The full cycle — and where this skill sits

The development cycle this project runs is the Pocock spine (ADR-0002: unconditional
for every сборка) split across two sessions (ADR-0004): stages 0–3 are the **owner's
solo session** with the canonical skills invoked by hand; stages 4–7 are **this skill**.

**Stage 0 — `/wayfinder` (optional, for fog).** When the work is too big and too foggy
for one session — the way to a spec isn't visible — the owner charts it first: a shared
map of *decision tickets* on the tracker, resolved one session at a time. Wayfinder's
destination becomes `to-spec`'s input. Skipped when the work is already clear.

**Stage 1 — `/grill-with-docs`.** A `/grilling` session run with the `/domain-modeling`
skill: the model interviews the owner relentlessly, ONE question at a time, walking the
decision tree; terms are sharpened into the glossary/CONTEXT.md and ADR-worthy decisions
are written down the moment they crystallise. Facts come from the repository; decisions
come from the owner. Output: shared understanding + a ledger of sharpened terms and
resolved forks.

**Stage 2 — `/to-spec`.** No interview — pure synthesis of the grilled conversation
into a spec: repo explored, the seams the work will be tested at agreed with the owner
(prefer an existing seam, the highest available, ideally exactly one), the document
published to the tracker under the `ready-for-agent` label.

**Stage 3 — `/to-tickets`.** The spec is cut into **tracer-bullet tickets**: vertical
slices through every layer, each demoable alone, each sized for a single fresh context
window; prefactoring first; wide refactors sequenced expand → migrate → contract. The
breakdown passes the **Quiz step** — the owner approves granularity and blocking edges
— and only then is published: one tracker issue per ticket, blocking edges wired as
**native** issue dependencies (`docs/agents/issue-tracker.md`, Wayfinding section). The
**frontier** = every open, unblocked, unclaimed ticket.

*(Stage 3.5 — `/handoff`, if needed: a solo session nearing the ~120k smart-zone border
writes a handoff document and a fresh session finishes the cutting. Execution never
inherits a degraded window — that is the point of the split.)*

**→ Stage 4 — THIS SKILL: entry + routing.** Provenance verified, config read,
preflight run, every ticket classified (judgment / skilled / mechanical) and routed by
the Step 2 matrix of the sibling playbook — including cross-provider lanes.

**Stage 5 — dispatch.** One ticket, one worker, one fresh context window; independent
tickets in parallel waves, worktree isolation from two concurrent writers. A `builder`
implements by the `implement` method: **`/tdd` at the seams the spec agreed** —
red → green → refactor, full suite once at the end (wired into `agents/builder.md`).

**Stage 6 — gates.** Every gated deliverable clears the fixed **three-lens wave**
(`gates.lenses`): Standards + Spec lenses — which ARE `/code-review`'s two axes,
absorbed by the gate (ADR-0002, review-collision resolution; executors pinned in
config, Standards non-Claude by construction) — plus the `critic` lens holding the
PASS/FAIL verdict. FAILs climb the escalation ladder; after a multi-writer wave the
integrated diff itself clears the same gate (whole-diff review).

**Stage 7 — synthesis.** Integration by the lead, never a worker; final report with
worker ledger, scorecard, notable findings; every ticket's record appended to telemetry
via `tools/telemetry_append.py` with `entry: "frontier"`.

## On launch: show the cycle map first

Your **first message** of every run renders this cycle as a compact status map, in the
user's language, with each stage's state **detected from the tracker, never assumed**:

```
Цикл (ADR-0002/0004):
  [?] 1 грилл+модель домена   — машиночитаемого следа НЕ оставляет; косвенно: правки
                                CONTEXT.md/глоссария или новый ADR рядом с датой спеки
  [✓] 2 /to-spec              — спека #<N> «<заголовок>», label ready-for-agent
  [✓] 3 /to-tickets           — <M> тикетов, рёбра blocked_by нативные, Quiz пройден
  [→] 4 ФРОНТИР (этот скилл)  — открыто <K>, не блокировано <J>: #12, #14; #15 ждёт #12
  [ ] 5 диспатч по матрице    — исполнитель по классу тикета (чаще builder) ⇒ /tdd на швах
  [ ] 6 гейт трёх линз        — Standards+Spec (= оси /code-review) + critic-вердикт
  [ ] 7 синтез + телеметрия
```

A stage that cannot be confirmed from tracker evidence is marked `[?]` with one line of
what is missing — that missing evidence is exactly what the provenance gate below acts
on. Note the asymmetry: stages 2–3 leave hard traces (a labelled spec issue, tickets
with native edges), while the grill leaves none by construction — a `[?]` on stage 1 is
therefore normal and never by itself a reason to refuse; only the gate's mandatory
trace (5a) refuses. The map is the run's opening status, not decoration: it tells the owner what the
solo phase actually left behind before any quota is spent.

## Entry protocol (before the first Agent call)

1. **Config.** Read [../orchestrate/config.yaml](../orchestrate/config.yaml) — the
   shared, single source of every operative value (routing classes, hard floors, gate
   lenses, cross-provider lanes, availability chains, budget). Compute the fingerprint
   and snapshot it if new (sibling playbook, "Config" section — one Bash line).
2. **Preflight.** Run `bash ../orchestrate/tools/preflight-fable.sh` — fail-fast on the
   three silent breaks of the Fable guarantee. Non-negotiable under Orca-spawned or
   otherwise inherited environments.
3. **Provider detect.** Detect cross-provider surfaces and read the recent
   `provider_health` window defined by the sibling's Step 2 (provider breaker) — a
   provider that ended a prior run open requires a canary before its first live dispatch.
4. **Discover the frontier.** List open `ready-for-agent` tickets and read their
   dependency edges — commands and conventions per `docs/agents/issue-tracker.md`:

   ```bash
   # ready-for-agent alone does NOT mean "ticket": /to-spec applies the same label to
   # the spec and /to-tickets never modifies that parent, so the spec is in this list.
   gh issue list --state open --label ready-for-agent \
     --json number,title,body,labels,assignees \
     --jq '[.[] | {number, title, body, assignees: [.assignees[].login]}]'

   # For each candidate, read its open blockers (issue-tracker.md, §Wayfinding / Blocking)
   # issue_dependencies_summary.blocked_by > 0 means the ticket is still blocked.
   gh api repos/<owner>/<repo>/issues/<number> --jq '.issue_dependencies_summary'
   ```

   Apply the user's filter (label, number list, title glob) when given. A candidate
   enters the frontier when all four hold: open ∧
   `issue_dependencies_summary.blocked_by` = 0 ∧ `assignees` empty ∧ **its body
   carries a `## Parent` section**. That last conjunct is what separates tickets from
   their own spec: `to-tickets`' issue template opens with `## Parent`, while
   `to-spec`'s does not — without it the spec would sweep into its own frontier and
   then fail the gate below for having no parent of its own. Where native
   dependencies are unavailable, fall back to the `Blocked by: #<n>` line in the body
   and check whether each referenced issue is closed (same fallback order as
   `issue-tracker.md`).

   **Local file tracker.** When `to-tickets` published to `.scratch/<slug>/issues/`,
   read those files instead of `gh`: order and blocking edges come from the numbered
   filenames and each file's `Blocked by` line. Note the canonical local template
   carries **no** parent reference at all — so the mandatory trace below degrades
   there (see 5a).

5. **Provenance gate (the ADR-0002 guard).** Before any dispatch, verify the traces
   that the Pocock spine was actually walked:

   **(a) Mandatory — the parent is a real spec.** Every frontier ticket's `## Parent`
   must resolve to an issue that is itself a published spec, not merely another
   ready-for-agent issue: fetch it (`gh issue view <n>`) and require **both** the
   `ready-for-agent` label (`docs/agents/triage-labels.md` — a generic triage role,
   never by itself proof of a spec) **and** the `to-spec` template's own
   `## Problem Statement` heading in its body. A ticket whose `## Parent` is missing,
   unresolvable, or resolves to an issue without that heading fails the gate — the
   `## Parent` section is optional in the template precisely when the work was *not*
   cut from a published spec, which is the case this gate exists to catch.
   *Local-tracker degradation:* the local template has no parent line, so require
   instead a spec document for the same feature slug; if none can be resolved, do not
   refuse — put the question to the owner ("did `/to-spec` run for this frontier?")
   and record the answer in the run report. This asymmetry is a gap in the canonical
   local template, not a licence to skip the spine.

   **(b) Supporting — native blocking edges.** At least one pair of tickets in the
   set should be connected by a native `blocked_by` dependency edge (the wiring
   `/to-tickets` publishes per `issue-tracker.md` §Wayfinding / Blocking). Absence
   of any edge is a warning, not an automatic refusal — a single-ticket frontier has
   no edge by construction — but a multi-ticket set with zero edges is suspicious and
   the gate must ask the owner to confirm.

   **On failure → redirect, never a bare refusal.** Report which tickets lack
   provenance and offer two paths: (1) walk the solo spine yourself —
   `/grill-with-docs` → `/to-spec` → `/to-tickets` — then re-invoke this skill; or
   (2) hand the raw task to `/orchestrate`, whose inline spine exists for exactly
   this case. Never "just run" hand-written spineless tickets: this entrance not
   owning the spine is not the spine becoming optional.
6. **Routing plan.** Classify every frontier ticket with the complexity rubric and
   write the routing table (ticket → class → agent/lane). Route by the hardest judgment
   the ticket requires; when torn, route up; hard floors (`routing.hard_floors`) never
   route below Opus.
7. **Approval checkpoint — routing only.** The breakdown was approved at the Quiz step;
   do not re-litigate it. Present the routing table, blast radius, and projected
   fan-out, and ask one Approve / Revise / Cancel — firing on the sibling Step 1's
   expensive-and-consequential trigger list and capped by its revision-round limit,
   both read there, not restated here. Cheap read-only frontiers skip it.

## Execution — shared machinery, one source

From here the run IS the sibling playbook's Steps 3–5, unabridged in method, with the
tickets already written. This file deliberately does not restate them; the table below
is the contract of record for what applies verbatim. **Divergence rule: if anything
here conflicts with config.yaml or a reference file, those win — fix this summary.**

| Concern | Source of record | Non-negotiables applied here |
|---|---|---|
| Ticket fields for dispatch | sibling Step 3; [references/delegation.md](../orchestrate/references/delegation.md) | tracker tickets are re-issued to workers in the seven-field format (OBJECTIVE…ACCEPTANCE), constraints restated — workers see no conversation; per-tier ticket style (scout maximally explicit … architect goal-not-steps) |
| Parallelism & working tree | sibling Step 3 | independent tickets in one parallel wave; `[<tier>·<effort>]` tag in every spawn description; worktree isolation from the concurrent-writer count in `fan_out.worktree_isolation_from_writers`; stale-base guard (`fan_out.stale_base_max_behind`); same-file tickets chain, never parallelize |
| Routing matrix & overrides | sibling Step 2; `routing.classes`, `routing.hard_floors`, `routing.overrides` | scout/builder/architect/critic pins from `agents/*.md`; built-in generic agents are off-matrix; fable ceiling; cross-provider lanes per [references/cross-provider.md](../orchestrate/references/cross-provider.md) with named reasons |
| Availability | `availability.fallbacks`, `availability.equal_weight_selection`, `availability.breaker`, `availability.midflight`, `availability.lead` | unavailability ≠ failure; sideways-or-up fallbacks; equal-weight peer pools on `architect`/`critic` rotate by least-dispatched-this-run (a route, not a reroute — no `fallback_from`); provider breaker; mid-flight death re-dispatches without burning attempts; quota forecast per wave; resume-pack after each wave |
| Quality gates | sibling Step 4; `gates.lenses`, `gates.pre_gate`, `gates.review_loop`; [references/quality.md](../orchestrate/references/quality.md) | deterministic checks are a prerequisite; fixed three-lens wave, never scaled or skipped once fired; decision-context pack for critic; adjudication before any retry; escalation ladder bounded by `routing.escalation.max_attempts_per_subtask` and `gates.review_loop.full_rounds_max`; whole-diff review after multi-writer waves |
| Large fan-outs / teams | sibling "Large fan-outs" / "Agent teams"; [references/teams.md](../orchestrate/references/teams.md) | every Workflow `agent()` call carries `agentType` or `model`; team channel only on its two triggers |
| Synthesis & report | sibling Step 5; quality.md §5 | completion card per returning agent; worker ledger + scorecard; connective-tissue self-check; unreported figures are `n/a`, never invented |

## Telemetry — this entrance is the experiment

Every record of the run — per-ticket and run-summary — is appended via
`../orchestrate/tools/telemetry_append.py` (never by hand) and carries:

- `entry: "frontier"` (§7; the sibling's inline runs are `full` — this field is the
  falsifiable comparison axis of the ADR-0004 pilot, whose sample size, metrics and
  retirement rule are stated once in quality.md §7, not restated here);
- `shape: "сборка"` — a frontier run is a сборка by construction (tickets exist);
- the config fingerprint, and every other §7 field exactly as quality.md defines.

The append tool is also the session-budget fuse (`session_budget.tokens_max`): a
non-zero exit means STOP DISPATCHING — overrides are named, never silent.

## What stays with `/orchestrate`

Raw tasks with no published spine artifacts; every `разбирательство` (investigation —
one thread, nothing to cut, no tickets by construction); compact сборки the owner
prefers to run end-to-end in one session with the inline spine. This skill is one
entrance, not a replacement: the same doctrine, entered at the ticket seam ADR-0002
already declared (spec #4, resolution 1 — "точка стыка — Тикет").
