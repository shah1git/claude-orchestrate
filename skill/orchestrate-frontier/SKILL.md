---
name: orchestrate-frontier
description: Execute an already-published, provenance-backed Pocock ticket frontier as lead orchestrator — sealed delegation, three-lens quality gates, and synthesis. Use only when the tracker already contains the approved spine and dependency-linked tickets. Do NOT use for raw or decision-bearing work (/orchestrate) or a closed, unpublished run-local ledger (/orchestrate-sweep).
argument-hint: [фильтр тикетов, или пусто — весь фронтир]
effort: xhigh
---

# Pocock Frontier — OMP execution entry

You are the OMP lead for an already published ticket frontier. The filter is `$ARGUMENTS`;
when it is empty, use the repository's current frontier.

This head validates provenance and enters the shared execution loop. It does not reopen
clarification, change the decomposition, recreate a specification, or approve a new plan.
The runtime owns routing, state, evidence, gate policy, retry eligibility, and acceptance.

## Choose this head only for a published frontier

This head is exclusive to tracker-published tickets with completed-spine and approval
provenance. Redirect raw work, a bare request to orchestrate, or any unresolved product,
architecture, UX, scope, acceptance, or integration decision to `/orchestrate`. Redirect a
complete but **unpublished** local ledger whose acceptance and integration are already
decided to `/orchestrate-sweep`; do not manufacture tracker provenance for it.

## State discipline

Use the registered `pocock_*` runtime tools for every Pocock state change. Each successful
call returns a state card; carry its `runId`, newest `revision`, and `stateHash` unchanged
into the next mutating runtime request. Invoke only an action named in that card's
`nextActions`. A failed call, witness disagreement, or `blockedReason` stops dispatching.
Use `pocock_status` to resume an existing run and continue only from the card it returns.
If `pocock_status` returns `runtimeMismatch` or the core reports `runtime_changed`,
use that run only for `status`. Do not claim that a fresh session can resume
the same run. Open a new OMP session so it can pin the installed runtime, then enter a
new frontier run from the same durable approved provenance.

## Frontier admission

1. Call `pocock_enter` with `entry: "frontier"` and the filtered objective. The adapter
   captures the session and runtime role manifest; do not add routing or role choices.
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
   Do not repair missing provenance by inventing a ticket or choosing a route. The exact
   field meanings are in the
   [ticket-writing reference](../orchestrate/references/delegation.md#the-ticket-field-by-field).
4. If the selected set is not a published frontier, a proof, field, approval, or dependency
   link is missing, do not request `admit_frontier` and do not dispatch. If the card
   authorizes cancellation, request `pocock_transition(cancel)`, then redirect by the
   actual admission fact: unresolved or raw work goes to `/orchestrate`; only a complete,
   unpublished, closed local ledger with pre-decided acceptance and integration goes to
   `/orchestrate-sweep`. Never invent provenance or relabel ordinary missing proof as a
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
restating or overriding its policy:
If the current card exposes a legal branch not shown in this compact diagram, the card
wins: do not infer or substitute a transition.

```text
pocock_prepare
→ producer_dispatch_pending: native task placeholder
→ pregate_pending:
  satisfy any issued browser open + exercise challenge, then pocock_pregate
→ repair_pending: pocock_transition(retry) → pocock_prepare
  or lens_prepare_pending: pocock_prepare_lenses
  → lens_dispatch_pending: native task placeholder
  → adjudication_pending: pocock_adjudicate
  → repair_pending: pocock_transition(retry) → pocock_prepare
    or accepted: pocock_accept
→ pocock_transition(continue_wave) with exact remaining/ready/blocked tracker
  sets and evidence; repeat while work remains
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
patch, records settled results, and accepts only issued host browser evidence.
When a producer card exposes `evidenceRequests`, use its `challengeToken` as the
browser tab `name`: `open` the exact target, then `run` on that same named tab.
The `run` code must exercise the criterion, include that exact criterion in the
assertion context, and call the host `assert` helper against a non-constant
observed result before `pocock_pregate`.
The core executes sealed direct-argv checks, constructs the Standards, Spec, and Critic
lens dispatch, and adjudicates their reports. The Critic emits the sole PASS or FAIL
verdict; acceptance also requires zero surviving introduced blocking Standards or Spec
findings.

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
