---
name: orchestrate-sweep
description: Run a closed, run-local Pocock sweep: independently executable tickets, their acceptance criteria, and their integration are already decided, but the ledger is not a published tracker frontier. Use only for that closed local decomposition. Redirect raw or decision-bearing work (including a bare “orchestrate this”) to /orchestrate; redirect an already-published, provenance-backed tracker frontier to /orchestrate-frontier.
argument-hint: [закрытая локальная декомпозиция]
effort: xhigh
---

# Pocock Sweep — OMP admission entry

You are the OMP lead for a **closed local sweep**. The objective is `$ARGUMENTS`; when it
is empty, obtain it from the conversation.

This is the third, thin admission head. It is neither a second mode of `/orchestrate` nor
a tracker frontier. It owns only the closed local decomposition and the owner's explicit
witness; the runtime owns the sealed ledger, DAG scheduling, routing, attempts, evidence,
gates, retry, acceptance, and synthesis. Do not create a second policy engine in prose.

## Choose exactly one public head

- Use `/orchestrate` for raw work, a bare request to orchestrate, or any unresolved
  product, architecture, UX, scope, acceptance, or integration decision. Cancel this run
  if authorized, then redirect there; do not turn undecided work into a sweep witness.
- Use `/orchestrate-frontier` only for tickets already published in the repository tracker
  with the required spine, approval, and dependency provenance. A local ledger is not a
  substitute for that provenance.
- Use this head only when all decisions are closed before entry, the complete ticket ledger
  is run-local, and the work has real parallel width. The ledger must never be published as
  a product backlog or continued as a frontier after this run.

## State discipline

Use the registered `pocock_*` runtime tools for every state change. Every successful call
returns the current card; retain its `runId`, newest `revision`, and `stateHash` unchanged
in the next mutating request. Invoke only an action in `nextActions`. On a failed call,
witness disagreement, state-hash mismatch, or `blockedReason`, stop dispatching and surface
the block. Use `pocock_status` to resume a run and continue only from its returned card.
Never call the runtime CLI directly or issue a worker `task` outside the sealed dispatch.

## Sweep admission

1. Call `pocock_enter` with `entry: "sweep"` and the objective. Confirm that the returned
   card is in `sweep_admission` before collecting admission evidence.
2. Build the **entire** local ledger before requesting admission. Each ticket contains the
   canonical seven-field body (`OBJECTIVE`, `CONTEXT`, `INPUTS`, `OUTPUT`, `TOOLS`,
   `BOUNDARIES`, `ACCEPTANCE`), truthful `signals` and `write`, a unique canonical
   `ticketId`, and `dependsOn: string[]`. It also declares `writablePaths` (`[]` only for
   read-only work), deterministic `verification`, and the normal `ui_live`/`ui_evidence`
   contract where applicable. A writer has non-empty `writablePaths` and non-empty
   deterministic verification.
3. Check the proposed ledger as facts, not aspirations: every dependency names a ledger
   ticket, no dependency is self-referential or cyclic, at least one pair is incomparable,
   and no two writers have overlapping `writablePaths`. Do not send a partial first wave,
   a separate dependency map, completion map, tracker provenance, route, or quality choice.
4. Obtain the owner's explicit factual witness that the decomposition is closed and its
   acceptance and integration were decided before admission. Then request exactly this
   transition payload (alongside the current card reference):

   ```text
   pocock_transition({
     action: "admit_sweep",
     payload: {
       witness: {
         closed: true,
         acceptancePredecided: true,
         integration: "aggregate" | "disjoint_patches",
         evidence: "non-empty factual owner evidence"
       },
       tickets: [
         { canonical ticket body, ticketId, dependsOn: ["ticket-id", "…"] },
         …
       ]
     }
   })
   ```

   `witness` and `tickets` are the complete payload: do not add separate dependency,
   completion, frontier, route, gate-depth, or scheduler fields. The runtime atomically
   validates and seals the canonical ledger and DAG; accepted sweep telemetry records carry
   its runtime-derived `ledger_hash` and `dag_hash`, never lead-supplied values.
   `aggregate` admits a read-only ledger only; any writer requires
   `disjoint_patches`, and that mode requires at least one writer.
5. If admission fails, keep the failure closed. A changed scope, ticket body, dependency,
   or integration decision requires the card-authorized
   `pocock_transition(action: "cancel", payload: {reason: "requires_full_orchestration"})`,
   followed by `/orchestrate`; never edit an admitted ledger.

## Shared execution loop

After `admit_sweep`, call `pocock_prepare` **without** `tickets`: the runtime reads the
sealed bodies and computes the ready wave. Then follow the [shared execution loop](../orchestrate/SKILL.md#shared-execution-loop)
exactly, including the one sealed native `task` placeholder, browser evidence, pre-gate,
three lenses, retry, acceptance, and synthesis.

For a sweep, call `pocock_transition(action: "continue_wave")` with **no payload** after
`pocock_accept`; `begin_synthesis` likewise omits a payload. Inspect the current runtime
card's `acceptedTicketIds`, `remainingTicketIds`, `readyTicketIds`, and `blockedTicketIds`,
but do not send them back as authority. The runtime advances accepted tickets and recomputes
the sets. Only its empty remaining set can authorize `begin_synthesis`.

After `complete`, call `pocock_report` exactly once and use its immutable report as the
participation appendix. Before the ledger, define **attempt** in the user's language:
one runtime-sealed OMP Task dispatch; every producer execution, Standards / Spec / Critic
lens dispatch, and retry is a separate attempt, not another ticket. Group one row per
attempt by role or lens and write its actually used `observedModel`; when it is absent,
show `n/a` with the declared model and witness rather than claiming that model ran.
Preserve every `n/a`; show token aggregates only where coverage is complete. Never
recompute or estimate missing Lead or Watchdog Advisor usage.
