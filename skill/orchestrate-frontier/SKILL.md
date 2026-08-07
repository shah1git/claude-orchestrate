---
name: orchestrate-frontier
description: Execute an already-published, provenance-backed Pocock ticket frontier as lead orchestrator — sealed delegation, wave-level two-lens quality gates, and synthesis. Use only when the tracker already contains the approved spine and dependency-linked tickets. This head always enters Pocock after status; its work is never eligible for the direct path. Do NOT use for raw or decision-bearing work (/orchestrate) or a closed, unpublished run-local ledger (/orchestrate-sweep).
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

## Обязательный протокол исполнения

До первой мутации runtime прочитайте
[единый протокол исполнения](../orchestrate/references/execution-protocol.md) и
следуйте ему без исключений с этой самой мутации: «Payload переходов» задаёт форму
payload уже для `admit_frontier`, «Исполнительный цикл» — после допуска. Эта Голова
не имеет прямого пути.
Читайте его точным путём — `skill://orchestrate/references/execution-protocol.md`
либо `skill/orchestrate/references/execution-protocol.md` в репозитории. Шаблоны
вида `skill://orchestrate/**` инструменты поиска отклоняют: внутренние URL
принимают только точный путь.

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
   `incomplete_tracker_reference`. Do not repair missing provenance or inputs by
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

## Исполнение после допуска

После разрешённого Карточкой `pocock_prepare` с опубликованным набором Тикетов
следуйте только
[единому протоколу исполнения](../orchestrate/references/execution-protocol.md).
Это единственный нормативный источник, включая host-witness UI-доказательство;
не воспроизводите и не расширяйте его policy в этой Голове. Для
`continue_wave` эта Голова передаёт разрешённые Карточкой tracker-наблюдаемые
`remaining`/`ready`/`blocked` наборы и фактическое доказательство;
`begin_synthesis` и `complete` запрашиваются только когда их разрешает
Карточка.
