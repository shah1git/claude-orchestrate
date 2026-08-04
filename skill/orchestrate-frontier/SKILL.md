---
name: orchestrate-frontier
description: Execute an already-published, provenance-backed Pocock ticket frontier as lead orchestrator — sealed delegation, wave-level three-lens quality gates, and synthesis. Use only when the tracker already contains the approved spine and dependency-linked tickets. This head always enters Pocock after status; its work is never eligible for the direct path. Do NOT use for raw or decision-bearing work (/orchestrate) or a closed, unpublished run-local ledger (/orchestrate-sweep).
argument-hint: [фильтр тикетов, или пусто — весь фронтир]
effort: xhigh
---

# Pocock Frontier — OMP execution entry

You are the OMP lead for an already published ticket frontier. The filter is `$ARGUMENTS`;
when it is empty, use the repository's current frontier.

This head validates provenance and enters the shared execution loop. It does not reopen
clarification, change the decomposition, recreate a specification, approve a new plan, or
use `/orchestrate`'s direct path. The runtime owns routing, state, evidence, gate policy,
retry eligibility, and acceptance.

## Choose this head only for a published frontier

This head is exclusive to tracker-published tickets with completed-spine and approval
provenance. Redirect raw work, a bare request to orchestrate, or any unresolved product,
architecture, UX, scope, acceptance, or integration decision to `/orchestrate`. Redirect a
complete but **unpublished** local ledger whose acceptance and integration are already
decided to `/orchestrate-sweep`; do not manufacture tracker provenance for it. Neither
case authorizes direct execution: a frontier is never direct, even when its current wave
contains only one ordinary-looking ticket.

## State discipline

On a new OMP session, call `pocock_status` **without** `runId` before entering anything.
The core finds the single active durable run for this workspace and the adapter hydrates
its card. If that card contains `runtimeMismatch`, call `pocock_enter` with
`entry: "frontier"` and the current objective; the core transactionally journals and
stages the replacement before retiring the incompatible run and activating the staged
replacement. Otherwise resume only from that card. If no active run exists, this head may
call `pocock_enter` for frontier admission. Every successful
runtime call returns a state card;
carry its `runId`, newest `revision`, and `stateHash` unchanged into the next mutating
request, and invoke only an action in that card's `nextActions`.

The core—not a session—owns the one nonterminal durable run and its budget and attempt
counters. A failed call, witness disagreement, or `blockedReason` stops dispatching; it
does not justify automatic cancellation and re-entry. Explicit owner abandonment is the
only ordinary cancellation path. Only a core-proven `runtimeMismatch` card authorizes
the replacement entry above; never infer it from an adapter error or retry a start
rejected as `active_run_exists`. Never call the runtime CLI directly.

## Frontier admission

1. Only after `pocock_status` establishes that no active durable run exists or returns a
   core-proven `runtimeMismatch`, call `pocock_enter` with `entry: "frontier"` and the
   filtered objective. The adapter captures
   the session and runtime role manifest; do not add routing or role choices. This call is
   unconditional for a frontier admission: there is no direct alternative.
2. While the returned card is in `frontier_admission`, inspect the repository's existing
   tracker records. Admission requires evidence that the selected ticket set is published
   with approval provenance, that every selected ticket is dependency-linked, that the
   records resolve to the completed Pocock spine and its published plan or specification,
   and that their declared dependencies identify the current executable frontier. A claim
   that the spine happened is not a substitute for durable provenance.
3. Each ticket must contain all seven required fields:

   ```text
   OBJECTIVE
   CONTEXT
   INPUTS
   OUTPUT
   TOOLS
   BOUNDARIES
   ACCEPTANCE
   ```

   It must also carry truthful `signals` and `write`; unique `writablePaths`
   (non-empty for writers and `[]` for read-only work); and direct-execution
   `verification` objects shaped as `{argv: [...], cwd: ".", timeoutSeconds: N}`.
   A `ui_live: true` ticket requires exactly `ui_evidence: {target, criterion}`.
   `INPUTS` must name a complete inline contract, resolvable repository path, full URL,
   fully qualified `issue://owner/repo/N`, or accepted upstream artifact. It must not use
   `#123`, `Issue #123`, or instructions to read tracker or IRC prose; the core reports
   `incomplete_tracker_reference_in_inputs`. Do not repair missing provenance or inputs by
   inventing a ticket or choosing a route. The exact field meanings are in the
   [ticket-writing reference](../orchestrate/references/delegation.md#the-ticket-field-by-field).
4. If the selected set is not a published frontier, a proof, field, approval, or dependency
   link is missing, do not request `admit_frontier` and do not dispatch. Keep the admission
   closed and surface the factual classification. Do not automatically cancel, re-enter,
   or redirect the work through a direct batch. A later change of head requires explicit
   owner abandonment; a core-proven `runtimeMismatch` instead permits the replacement
   entry described above. Never invent provenance or relabel ordinary missing proof as a
   sweep.
5. If provenance cannot be inspected because the tracker is unavailable or its local
   representation lacks the needed spine linkage, ask the owner once for an explicit
   attestation that the selected frontier completed the approved spine. Only that explicit
   attestation permits admission; include it, and the reason the durable proof is
   unavailable, in the `admit_frontier` provenance payload. Never silently override the
   missing proof.
6. With durable provenance, or with the recorded explicit attestation in the exceptional
   case above, request `pocock_transition(admit_frontier)` with the provenance payload.
   Only after the returned card authorizes it, call `pocock_prepare` with the published
   seven-field ticket set.

## Shared execution loop

Use the same loop as [orchestrate](../orchestrate/SKILL.md#shared-execution-loop), without
restating or overriding its policy. The direct exception exists only before
`pocock_enter` in `/orchestrate`; it cannot appear in this head. If the current card
exposes a legal branch not shown in this compact diagram, the card wins: do not infer or
substitute a transition.

```text
pocock_prepare
→ producer_dispatch_pending: native task placeholder
→ pregate_pending:
  satisfy any issued browser open + exercise challenge, then pocock_pregate
→ repair_pending: pocock_transition(retry) → pocock_prepare
  or lens_prepare_pending: pocock_prepare_lenses
  → lens_dispatch_pending: one wave-level fixed three-lens task
    → an isolated failed lens retries alone
    → adjudication_pending: pocock_adjudicate
      → repair_pending only for affected producer attempts: core routes each by its recorded rejection cause (no separate retry)
        → pocock_prepare
→ pocock_transition(continue_wave) with exact remaining/ready/blocked tracker
  sets and evidence; accepted tickets remain accepted; repeat while work remains
→ only an explicit empty remaining set authorizes begin_synthesis → complete
```

Each native dispatch is one syntactically valid but semantically empty `task` call. Its
raw input contains only this placeholder, which the extension replaces with the
core-sealed task input:

```text
task({
  context: "Pocock sealed dispatch",
  tasks: [{ task: "Pocock sealed dispatch placeholder" }]
})
```

Do not put tickets, routing, identities, output schemas, or results into that call. The
sealed OMP profile keeps global `task.isolation.apply=false` and
`task.isolation.merge=patch`; the core validates and centrally applies each returned
patch, records settled results, and accepts only issued host browser evidence. A settled
native task is one-shot: never wait for or revive it through Hub; any retry is a fresh
card-authorized sealed attempt.

The core executes sealed direct-argv checks, then creates exactly three distinct
wave-level reviewers over the pre-gate-passed producer subset. A wave may mix
`mechanical`, `skilled`, and `judgment` producer attempts on their respective slots.
Configuration makes producer and lens slot sets disjoint, the three lens slots pairwise
distinct. Before dispatching lenses,
the core fails closed with `independent_reviewer_unavailable` if a lens's opaque
`resolvedModel` string exactly matches that of any producer in the wave; it does not
classify vendors or families. Each lens returns
`{lens, summary, reports:[{attemptId, summary, findings, verdict}]}` covering every passed
producer attempt. Standards and Spec emit `NO_VERDICT`; Critic alone emits `PASS` or
`FAIL`. Only an isolated failed lens is retried on the same slot. Retry routing is
core-owned: a missing diagnosis uses
`lastFailureKind`; `capability` deepens the class (writers stop at `skilled`, exhausted
depth blocks as `escalation_exhausted`) and `availability` preserves the slot while OMP
owns model replacement.
During partial acceptance each rejected ticket routes directly by its recorded rejection
cause, without a separate `retry`; adjudication preserves already accepted tickets and
rejects any ticket with a Critic `FAIL` or surviving introduced blocking Standards or Spec
finding.

After `pocock_accept`, query the durable tracker and call `continue_wave` with exact
`remainingTicketIds`, `nextTicketIds`, `blockedTicketIds`, and factual `evidence`.
Continue until the runtime observes an explicit empty remaining set and authorizes
`begin_synthesis`. A nonterminal run is never completed merely because the session ends.

For every terminal run, present the final ledger by ticket in the user's language.
For each ticket, state the delivered outcome, its final acceptance state, and the factual
acceptance evidence: the applicable sealed verification result, accepted UI evidence where
required, and any unresolved blocker or failure. This ledger is an account of deliverables
and acceptance, not an execution-history export: do not require or list individual attempts,
roles, agents, declared or observed models, fallback witnesses, tokens, durations, or requests.
`observedModel` and `modelFallback` remain operational telemetry on the live card and settlement, not a
final-answer requirement. Never manufacture evidence.
