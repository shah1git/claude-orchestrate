---
name: orchestrate
description: Run the complete Pocock orchestration flow for raw or decision-bearing complex work. Before `pocock_enter`, triage fully resolved ordinary work for the strictly limited direct path. Use the full path for a bare request to orchestrate or any unresolved product, architecture, UX, scope, acceptance, or integration decision. Do NOT use the direct path for an already-published provenance-backed tracker frontier (/orchestrate-frontier) or a closed run-local ledger with pre-decided acceptance and integration (/orchestrate-sweep).
argument-hint: [описание задачи]
effort: xhigh
---

# Orchestrate — OMP entry

You are the OMP lead. The task is `$ARGUMENTS`; when it is empty, take the objective
from the conversation.

This head owns triage, clarification, the Pocock spine, plan approval, and publication
of tickets. It may finish one narrowly eligible ordinary task on the direct path before
there is a Pocock run. The runtime owns routing, lane selection, attempt state, evidence,
verification, quality decisions, and acceptance for every Pocock run. Do not turn prose
into a second policy engine.

## State discipline for a Pocock run

Use the registered `pocock_*` runtime tools for every Pocock state change. On a new OMP
session, first call `pocock_status` **without** `runId`: the core finds the one active
durable run for this workspace and the adapter hydrates its state card. If the returned
card contains `runtimeMismatch`, call `pocock_enter` for the current full objective; the
core transactionally journals and stages the replacement before retiring the incompatible
run and activating its replacement. If there is no active run, triage the new objective;
otherwise resume only from the returned card.

Every successful call returns the current state card. Retain its `runId`, newest
`revision`, and `stateHash`; put all three unchanged in the next mutating runtime request.
An action is legal only when the latest card authorizes it in `nextActions`. Never guess
a state transition, replay an old witness, choose a worker route, or call the runtime CLI
directly.

The core permits exactly one nonterminal durable run per workspace, across OMP sessions.
Its budget reservation and attempt counters belong to that run and persist through
hydration; opening a session cannot reset them. If a runtime call fails, the state hash
disagrees, or the card gives a `blockedReason`, stop dispatching and surface the block.
Never automatically cancel and re-enter after a recoverable failure. Explicit owner
abandonment is the only ordinary cancellation path. A new `pocock_enter` may replace an
active run only when the core itself proves `runtime_mismatch`; the core first journals
and stages the replacement, then retires the incompatible run and activates the staged
replacement. Never infer this condition from an adapter error or retry a start rejected
as `active_run_exists`.

## Triage before `pocock_enter`

1. Resolve the objective against the conversation and repository evidence. First exclude
   a published tracker frontier and a closed local sweep: each always uses its own head,
   even if one selected ticket appears simple. Direct execution never consumes either
   form of prepared work.
2. Use the direct path only when **every** fact below is true:
   - the work is ordinary and fully resolved, with no open product, architecture, UX,
     scope, acceptance, or integration decision;
   - it is executable as one blocking native OMP `task` batch containing exactly one
     worker item that is forbidden to spawn or delegate;
   - its request is self-contained as `Target`, `Change`, and `Acceptance`; and
   - it is neither a tracker frontier nor a sweep.
3. For an eligible direct task, submit one normal blocking native `task` batch containing
   exactly one worker item, explicitly forbid delegation, and carry the complete contract.
   The host independently verifies that the observed result and patch satisfy the complete
   `Target`, `Change`, and `Acceptance` contract before responding. Do not
   call `pocock_enter`, create a run or state card, invoke a Pocock lens, or invent a
   retry around this path.
4. If any fact is false or uncertain, use the full path below. Doubt is a reason to enter
   full Pocock, never a reason to widen the direct exception.

## Full preparation path after triage

1. For full orchestration, call `pocock_enter` with `entry: "full"` and the objective.
   The adapter captures the session and runtime role manifest; do not supply or alter
   either.
2. From the resulting `preparation` card, request these preparation transitions in this
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
3. During clarification, resolve facts from the repository before asking the user. Ask
   dependent questions one at a time, stop when further answers cannot change the plan
   or a ticket, and preserve the agreed vocabulary, scope, and observable success
   conditions.
4. Walk the assembly spine in the lead context: clarification and grilling, domain
   model, specification, then a dependency-aware ticket breakdown. Do not delegate the
   spine or approve it on the user's behalf. Present the plan and ticket breakdown for
   explicit approval. Request cancellation only for owner abandonment when the current
   card authorizes it. A core-proven `runtimeMismatch` is handled only by
   `pocock_enter`, which lets the core transactionally replace the incompatible run.
5. Publish the approved tickets through the repository's existing tracker before
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
forbidden otherwise. `INPUTS` must be a complete inline contract, a resolvable repository
path, a full URL, a fully qualified `issue://owner/repo/N`, or an accepted upstream
artifact—not `#123`, `Issue #123`, or an instruction to read tracker or IRC prose. The
core reports such an incomplete source as `incomplete_tracker_reference`. Do not
name a route, lane, worker, or quality outcome in a ticket; the core derives those from
the ticket and its policy. The exact field meanings are in the
[ticket-writing reference](references/delegation.md#the-ticket-field-by-field).

## Shared execution loop

This loop applies only after a Pocock run has been admitted; the direct path never enters
it. After publication, and for another entry after its admission, use this exact loop
whenever the current state card authorizes more work. Published `full` and `frontier`
preparation supplies the current seven-field ticket set; `sweep` preparation supplies no
tickets because the core reads its sealed ledger and selects the work itself. If the
current card exposes a legal branch not shown in this compact diagram, the card wins: do
not infer or substitute a transition.

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
  → lens_dispatch_pending: one wave-level fixed three-lens task
    → if one lens alone fails execution or schema validation, retry only that lens
    → adjudication_pending: pocock_adjudicate
      → repair_pending for affected producer attempts: pocock_transition(retry)
        → pocock_prepare
      or accepted producer tickets: pocock_accept
→ pocock_transition(continue_wave) only in the form the current card authorizes:
  published tickets carry tracker-observed remaining/ready/blocked sets plus evidence;
  sweep carries no payload because the runtime-owned sealed DAG computes those sets
→ repeat while work remains; already accepted tickets remain accepted; only an explicit
  empty remaining set authorizes pocock_transition(begin_synthesis) → lead synthesis
  → pocock_transition(complete)
```

For each Pocock native dispatch, make exactly one syntactically valid but semantically
empty `task` call. Its entire raw input is only the placeholder below; the extension
replaces it with the core-sealed task input before transport:

```text
task({
  context: "Pocock sealed dispatch",
  tasks: [{ task: "Pocock sealed dispatch placeholder" }]
})
```

Do not place ticket text, routing, identities, output schemas, or results in that call.
The sealed OMP profile keeps global `task.isolation.apply=false` and
`task.isolation.merge=patch`; the core validates and centrally applies the returned
patch. Once the blocking native task is settled, it is one-shot: never wait for or revive
it through Hub. Continue only through the next card-authorized runtime command; a retry
creates a fresh sealed attempt.

For a `ui_live` attempt, use the issued `challengeToken` as the browser tab
`name`: first `open` the exact issued target, then `run` on that same named tab.
The `run` code must exercise the stated criterion, include that exact criterion
in the assertion context, and call the host `assert` helper against a
non-constant observed result. Only those challenge-bound successful host calls
count as evidence; prose, screenshots without the challenge, and evidence from
another attempt do not satisfy it. Then request `pocock_pregate`.

`pocock_pregate` executes the sealed direct-argv checks and determines which producer
attempts enter review. `pocock_prepare_lenses` creates exactly three distinct
wave-level reviewers—Standards, Spec, and Critic—over precisely that pre-gate-passed
subset. The core seals one shared producer vendor/family for the wave and chooses every
reviewer independently from it. Each lens returns
`{lens, summary, reports:[{attemptId, summary, findings, verdict}]}` with a report for
every passed producer attempt. Standards and Spec emit `NO_VERDICT`; Critic alone emits
`PASS` or `FAIL`. If a lens alone fails execution or schema validation, only that lens
receives a new sealed attempt. `pocock_adjudicate` is the only place reports are
adjudicated: it preserves already accepted producer tickets and accepts another only with
a Critic `PASS` and zero surviving blocking findings introduced by Standards or Spec. The
lead cannot waive either condition. On repair, request `retry` from the current card and
return to preparation; on acceptance, request `pocock_accept`. Attempt eligibility,
retry limits, gate conditions, and the decision to accept remain code-owned.

Synthesize only after the runtime authorizes `begin_synthesis`, then request `complete`
from the resulting card. A nonterminal run is never completed merely because a session
ends.

For every terminal run, present the final ledger by ticket in the user's language.
For each ticket, state the delivered outcome, its final acceptance state, and the factual
acceptance evidence: the applicable sealed verification result, accepted UI evidence where
required, and any unresolved blocker or failure. This ledger is an account of deliverables
and acceptance, not an execution-history export: do not require or list individual attempts,
roles, agents, declared or observed models, fallback witnesses, tokens, durations, or requests.
`observedModel` and `modelFallback` remain operational telemetry on the live card and settlement, not a
final-answer requirement. Never manufacture evidence.
