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
there is a Pocock run. The runtime owns routing, slot selection, attempt state, evidence,
verification, quality decisions, and acceptance for every Pocock run. Do not turn prose
into a second policy engine.

## Обязательный протокол исполнения

До первой мутации runtime прочитайте
[единый протокол исполнения](references/execution-protocol.md). После допуска
Pocock-прогона следуйте ему без исключений; прямой путь в этот протокол не входит.

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
name a route, slot, worker, or quality outcome in a ticket; the core derives those from
the ticket and its policy. The exact field meanings are in the
[ticket-writing reference](references/delegation.md#the-ticket-field-by-field).

## Исполнение после допуска

После `publish_tickets` следуйте
[единому протоколу исполнения](references/execution-protocol.md). Он определяет
дисциплину состояния, запечатанную раздачу, UI-доказательства, pre-gate, Линзы,
приёмку, `continue_wave`, синтез и терминальные переходы.
