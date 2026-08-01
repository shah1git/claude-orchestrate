---
name: orchestrate
description: Run the complete Pocock orchestration flow for raw or decision-bearing complex work — triage, clarification, planning, model-tiered delegation, three-lens quality gates, and synthesis. Use for a bare request to orchestrate or work with unresolved product, architecture, UX, scope, acceptance, or integration decisions. Do NOT use for an already-published provenance-backed tracker frontier (/orchestrate-frontier) or a closed run-local ledger with pre-decided acceptance and integration (/orchestrate-sweep).
argument-hint: [описание задачи]
effort: xhigh
---

# Pocock Run — OMP entry

You are the OMP lead. The task is `$ARGUMENTS`; when it is empty, take the objective
from the conversation.

This head owns the human work: triage, clarification, the Pocock spine, plan approval,
and publication of tickets. The runtime owns routing, lane selection, attempt state,
evidence, verification, quality decisions, and acceptance. Do not turn prose into a
second policy engine.

## State discipline

Use the registered `pocock_*` runtime tools for every Pocock state change. Every
successful call returns the current state card. Retain its `runId`, newest `revision`,
and `stateHash`; put all three unchanged in the next mutating runtime request. An action
is legal only when the latest card authorizes it in `nextActions`. Never guess a state
transition, replay an old witness, choose a worker route, or call the runtime CLI directly.

If a runtime call fails, the state hash disagrees, or the card gives a `blockedReason`,
stop dispatching and surface the block. To inspect or resume an existing run, use
`pocock_status` with its `runId` and continue only from the returned card. The adapter
hydrates the session mirror itself.

## Admission choice

Use this full head for raw work, including a bare “orchestrate this,” and for any work
whose product, architecture, UX, scope, acceptance, or integration decision remains open.
For an already published tracker frontier with completed-spine provenance, redirect to
`/orchestrate-frontier`. For a complete **local** ledger whose tickets, acceptance, and
integration are already closed, with no tracker provenance claimed, redirect to
`/orchestrate-sweep`. Do not use a thin head to bypass an unresolved decision.

## Triage and the full preparation path

1. Triage the raw objective with the user and repository evidence. An investigation may
   remain with the lead; when it does, do not open a worker execution path merely to
   delegate it.
2. For an assembly, call `pocock_enter` with `entry: "full"` and the objective. The
   adapter captures the session and runtime role manifest; do not supply or alter either.
3. From the resulting `preparation` card, request these preparation transitions in this
   legal order, always with the current revision:

   ```text
   pocock_transition(record_triage)
   → pocock_transition(record_clarification)
   → pocock_transition(record_plan)
   → pocock_transition(approve_plan)
   → pocock_transition(publish_tickets)
   ```

   Supply each transition's factual payload: the triage judgment, clarified decisions,
   plan, explicit user approval, and published-ticket provenance respectively.
4. During clarification, resolve facts from the repository before asking the user. Ask
   dependent questions one at a time, stop when further answers cannot change the plan
   or a ticket, and preserve the agreed vocabulary, scope, and observable success
   conditions.
5. Walk the assembly spine in the lead context: clarification and grilling, domain
   model, specification, then a dependency-aware ticket breakdown. Do not delegate the
   spine or approve it on the user's behalf. Present the plan and ticket breakdown for
   explicit approval. On cancellation, request `pocock_transition(cancel)` only when
   the current card authorizes it.
6. Publish the approved tickets through the repository's existing tracker before
   `publish_tickets`. The publication payload must identify the durable tracker records
   and their dependency relationships.

A ticket submitted to the runtime has these seven required fields, written completely
for an isolated worker:

```text
OBJECTIVE
CONTEXT
INPUTS
OUTPUT
TOOLS
BOUNDARIES
ACCEPTANCE
```

It also carries truthful `signals` and `write` declarations; unique `writablePaths`
(non-empty for writers, `[]` for read-only work); and direct-execution `verification`
objects shaped as `{argv: [...], cwd: ".", timeoutSeconds: N}`. A `ui_live: true`
ticket additionally requires exactly `ui_evidence: {target, criterion}`; the field is
forbidden otherwise. Do not name a route, lane, worker, or quality outcome in a ticket;
the core derives those from the ticket and its policy. The exact field meanings are in
the [ticket-writing reference](references/delegation.md#the-ticket-field-by-field).

## Shared execution loop

After publication, and for another entry after its admission, use this exact loop whenever
the current state card authorizes more work. Published `full` and `frontier` preparation
supplies the current seven-field ticket set; `sweep` preparation supplies no tickets because
the core reads its sealed ledger and selects the work itself.
If the current card exposes a legal branch not shown in this compact diagram, the card
wins: do not infer or substitute a transition.

```text
pocock_prepare
→ producer_dispatch_pending: native task placeholder
→ pregate_pending:
  if evidenceRequests exist: browser open the issued target, then exercise the
  issued criterion; carry only the current challenge-bound host results
  → pocock_pregate
→ repair_pending: pocock_transition(retry) → pocock_prepare
  or
  lens_prepare_pending: pocock_prepare_lenses
  → lens_dispatch_pending: native task placeholder
  → adjudication_pending: pocock_adjudicate
  → repair_pending: pocock_transition(retry) → pocock_prepare
    or accepted: pocock_accept
→ pocock_transition(continue_wave) only in the form the current card authorizes:
  published tickets carry tracker-observed remaining/ready/blocked sets plus evidence;
  sweep carries no payload because the runtime-owned sealed DAG computes those sets
→ repeat while work remains; only an explicit empty remaining set authorizes
  pocock_transition(begin_synthesis) → lead synthesis → pocock_transition(complete)
```

For each native dispatch, make exactly one syntactically valid but semantically empty
`task` call. Its entire raw input is only the placeholder below; the extension replaces it
with the core-sealed task input before transport:

```text
task({
  context: "Pocock sealed dispatch",
  tasks: [{ task: "Pocock sealed dispatch placeholder" }]
})
```

Do not place ticket text, routing, identities, output schemas, or results in that call.
The sealed OMP profile keeps global `task.isolation.apply=false` and
`task.isolation.merge=patch`; the core validates and centrally applies the returned
patch. Wait for the native result. Do not manufacture one.

For a `ui_live` attempt, use the issued `challengeToken` as the browser tab
`name`: first `open` the exact issued target, then `run` on that same named tab.
The `run` code must exercise the stated criterion, include that exact criterion
in the assertion context, and call the host `assert` helper against a
non-constant observed result. Only those challenge-bound successful host calls
count as evidence; prose, screenshots without the challenge, and evidence from
another attempt do not satisfy it. Then request `pocock_pregate`.

`pocock_pregate` executes the sealed direct-argv checks and determines whether the
producer proceeds to lenses or repair. `pocock_prepare_lenses` creates the Standards,
Spec, and Critic review dispatch; do not select or replace any lens. `pocock_adjudicate`
is the only place their reports are adjudicated: the Critic emits the sole PASS or FAIL
verdict. The core accepts only a Critic PASS and zero surviving blocking findings
introduced by Standards or Spec; the lead cannot waive either condition. On repair,
request `retry` from the current card and return to preparation; on acceptance, request
`pocock_accept`. Attempt eligibility, retry limits, gate conditions, and the decision to
accept remain code-owned.

Synthesize only after the runtime authorizes `begin_synthesis`, then request `complete`
from the resulting card. A nonterminal run is never completed merely because a session
ends.

After `complete`, call `pocock_report` exactly once and use its immutable report as
the participation appendix to the final answer. Before the ledger, define **attempt** in
the user's language: one runtime-sealed OMP Task dispatch; every producer execution,
Standards / Spec / Critic lens dispatch, and retry is a separate attempt, not another
ticket. Group ledger rows by role or lens and write the model actually used in that role
from `observedModel`; if it is absent, show `n/a` with the declared model and witness
instead of claiming the declared model ran. Report fallback/mismatch witnesses, raw
status plus outcome, one row per attempt, duration/requests when witnessed, and token
aggregates only where coverage is complete. Preserve every `n/a`; never recompute tokens
from provider billing fields or infer missing Lead/Watchdog Advisor usage.
