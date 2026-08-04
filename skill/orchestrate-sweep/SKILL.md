---
name: orchestrate-sweep
description: Run a closed, run-local Pocock sweep: independently executable tickets, their acceptance criteria, and their integration are already decided, but the ledger is not a published tracker frontier. This head always enters Pocock after status and never uses the direct path. Use only for that closed local decomposition. Redirect raw or decision-bearing work (including a bare “orchestrate this”) to /orchestrate; redirect an already-published, provenance-backed tracker frontier to /orchestrate-frontier.
argument-hint: [закрытая локальная декомпозиция]
effort: xhigh
---

# Pocock Sweep — OMP admission entry

You are the OMP lead for a **closed local sweep**. The objective is `$ARGUMENTS`; when it
is empty, obtain it from the conversation.

This is the third, thin admission head. It is neither a second mode of `/orchestrate` nor
a tracker frontier, and it never uses `/orchestrate`'s direct path. It owns only the
closed local decomposition and the owner's explicit witness; the runtime owns the sealed
ledger, DAG scheduling, routing, attempts, evidence, gates, retry, acceptance, and
synthesis. Do not create a second policy engine in prose.

## Choose exactly one public head

- Use `/orchestrate` for raw work, a bare request to orchestrate, or any unresolved
  product, architecture, UX, scope, acceptance, or integration decision. It alone triages
  whether an ordinary fully resolved task may use the direct path; this head never does.
- Use `/orchestrate-frontier` only for tickets already published in the repository tracker
  with the required spine, approval, and dependency provenance. A local ledger is not a
  substitute for that provenance.
- Use this head only when all decisions are closed before entry, the complete ticket ledger
  is run-local, and the work has real parallel width. The ledger must never be published as
  a product backlog or continued as a frontier after this run.

## Обязательный протокол исполнения

До первой мутации runtime прочитайте
[единый протокол исполнения](../orchestrate/references/execution-protocol.md). После
допуска следуйте ему без исключений; эта Голова не имеет прямого пути.

## Sweep admission

1. Only after `pocock_status` establishes that no active durable run exists or returns a
   core-proven `runtimeMismatch`, call `pocock_enter` with `entry: "sweep"` and the
   objective. Confirm that the returned card is in `sweep_admission` before collecting
   admission evidence. This is mandatory for a sweep: there is no direct alternative.
2. Build the **entire** local ledger before requesting admission. Each ticket contains the
   canonical seven-field body (`OBJECTIVE`, `CONTEXT`, `INPUTS`, `OUTPUT`, `TOOLS`,
   `BOUNDARIES`, `ACCEPTANCE`), truthful `signals` and `write`, a unique canonical
   `ticketId`, and `dependsOn: string[]`. It also declares `writablePaths` (`[]` only for
   read-only work), deterministic `verification`, and the normal `ui_live`/`ui_evidence`
   contract where applicable. A writer has non-empty `writablePaths` and non-empty
   deterministic verification. `INPUTS` must name complete sources or accepted upstream
   artifacts, never `#123`, `Issue #123`, tracker, or IRC instructions; an incomplete
   tracker reference is `incomplete_tracker_reference`.
3. Check the proposed ledger as facts, not aspirations: every dependency names a ledger
   ticket **and binds its accepted output**, no dependency is self-referential or cyclic,
   at least one pair is incomparable, and no two writers have overlapping `writablePaths`.
   Do not send a partial first wave, a separate dependency map, completion map, tracker
   provenance, route, or quality choice.
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
   or integration decision never triggers automatic cancellation or re-entry. Surface the
   mismatch and wait for explicit owner abandonment before any card-authorized
   cancellation and later use of `/orchestrate`. A core-proven `runtimeMismatch` instead
   permits the replacement `pocock_enter` described above; never edit an admitted ledger.

## Исполнение после допуска

После `admit_sweep` вызовите разрешённый Карточкой `pocock_prepare` **без**
`tickets`: runtime читает запечатанные тела и вычисляет готовую Волну. Затем
следуйте только
[единому протоколу исполнения](../orchestrate/references/execution-protocol.md).
Это единственный нормативный источник, включая host-witness UI-доказательство;
не воспроизводите и не расширяйте его policy в этой Голове.

После `pocock_accept` запросите разрешённый `pocock_transition` с
`action: "continue_wave"` **без payload**. `begin_synthesis` также не получает
payload. Проверяйте в текущей Карточке `acceptedTicketIds`, `remainingTicketIds`,
`readyTicketIds` и `blockedTicketIds`, но не передавайте их обратно как авторитет:
runtime продвигает принятые Тикеты и пересчитывает эти наборы. Только его пустой
remaining set может разрешить `begin_synthesis`; `complete` запрашивается только
после разрешения Карточки.
