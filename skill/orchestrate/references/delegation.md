# Delegation: sealed OMP ticket contract

This is the operational reference for tickets submitted through `pocock_prepare`.
It specifies ticket content and the observable execution contract. Routing, agent
identity, model resolution, retry eligibility, isolation mechanics, output schema,
and acceptance are owned by the native OMP control plane; a ticket must not try to
select or override them.

## The ticket, field by field

| Field | Required content | Failure prevented |
|---|---|---|
| OBJECTIVE | One observable outcome, stated in one sentence. | Activity is mistaken for completion. |
| CONTEXT | Why the result matters and who consumes it. | A locally plausible result violates the larger plan. |
| INPUTS | Exact paths, symbols, records, URLs, and upstream artifacts. | The isolated worker guesses or repeats discovery. |
| OUTPUT | The durable deliverable and its required shape. | A correct result arrives in an unusable form. |
| TOOLS | Capabilities genuinely needed by this ticket. | The worker searches indefinitely or cannot verify its work. |
| BOUNDARIES | Explicit non-goals, writable paths, and stop conditions. | Scope creep or overlapping writes. |
| ACCEPTANCE | Objective checks that can fail on a plausible defect. | “Done” becomes an unsupported assertion. |

## Required execution metadata

Alongside the seven textual fields, submit the following metadata. Use the tracker
identifier as `ticketId` (or `id`) where available so that attempts remain
traceable.

| Field | Required form |
|---|---|
| `signals` | Non-empty array of truthful classification strings. |
| `write` | Boolean. |
| `writablePaths` | Unique normalized POSIX repository-relative file paths, or directory prefixes ending in `/`. A writer (`write: true`) declares at least one; a read-only ticket declares `[]`. |
| `verification` | An array of command objects: `[{argv: ["…"], cwd: ".", timeoutSeconds: N}]`. `argv` is a non-empty string array and is run directly, not through a shell; `cwd` is `.` or an existing normalized repository-relative directory; `timeoutSeconds` is a positive integer. |
| `ui_live` | `true` only when the acceptance requires live host browser or xdev proof. |
| `ui_evidence` | Required exactly when `ui_live: true`, and then exactly `{target: "…", criterion: "…"}`; forbidden otherwise. |

`writablePaths` is a permission boundary, not a hint: it cannot use absolute paths,
`.`/`..`, backslashes, duplicate entries, or repository metadata. A file entry
permits that file only; a trailing-slash entry permits that directory subtree.

Use the exact executable invocation as verification—for example,
`{"argv":["python3","-m","pytest","tests/test_rate_limit.py"],"cwd":".","timeoutSeconds":60}`—
not a shell pipeline, a prose instruction, or a command that depends on an
interactive session. The runtime enforces the configured timeout cap. Empty
`verification` is valid only when no deterministic command can exercise the result.

The ticket may declare `max_diff_lines_override` only when the approved ticket
justifies a larger limit. The ordinary per-ticket (per-attempt) ceiling and all
other pre-gate limits are configured in [`gates.pre_gate`](../config.yaml), rather
than copied into a ticket.

Do not add a route, lane, agent, model, reviewer, verdict, retry instruction, or
output schema. Those values are sealed after the control plane classifies the
ticket.

## Completeness rules

**One ticket is one vertical slice.** It may touch several layers when that is
required for an independently demonstrable result. Two tickets must not share an
OBJECTIVE or overlap on files they modify in the same wave.

**Resolve uncertainty before dispatch.** Words such as “probably”, “likely”,
“should”, or “maybe” mark an unresolved decision. Replace them with a verified
fact, an explicit approved assumption with a stop condition, or a clarification.

**Named sources outrank the ticket.** Put this rule in BOUNDARIES whenever INPUTS
names a specification, ADR, or documented contract:

```text
The documents named in INPUTS outrank this ticket. If two statements cannot both
be true, implement neither side of the conflict; return NEEDS_CLARIFICATION with
both statements and their locations. Complete only the unaffected work.
```

Silence, a narrower scope, or an inconvenient instruction is not a contradiction.

**Pass references, not transcripts.** A dependent ticket names the upstream
artifact, changed files, and at most a short statement of the established fact.
The artifact on disk remains authoritative.

**Acceptance must be adversarially gradeable.** Prefer invariants and executable
checks:

```text
OBJECTIVE: Add rate limiting to the two public POST handlers.
CONTEXT: The public API must reject bursts before downstream writes occur.
INPUTS: src/api/routes.ts; docs/rate-limit-policy.md.
OUTPUT: Production implementation and focused regression tests.
TOOLS: read, grep, edit, bash.
BOUNDARIES: Do not change persistence schema or unrelated endpoints. The
documents named in INPUTS outrank this ticket; report any contradiction.
ACCEPTANCE: Both handlers return 429 after the documented limit; requests below
the limit retain their previous responses; the named focused test command exits 0.
```

A criterion that would remain green after breaking the behavior is not evidence
and must be rewritten before publication.

## Native delivery, application, and rejection

A sealed producer runs in native OMP task isolation. The sealed OMP profile keeps
global `task.isolation.apply=false` and `task.isolation.merge=patch`: the producer
returns a patch and only the strict structured result requested by the sealed task.
A successful writer result names every changed repository file in `changedFiles`;
a read-only result names none. Producer patches are never auto-applied.

Before any repository mutation, the runtime validates every producer patch
individually: it must parse, remain within `writablePaths`, and have a normalized
`changedFiles` set exactly equal to the files observed in that patch. Deletes,
renames, copies, and symbolic-link operations are forbidden. The runtime then
checks the combined wave patch and applies it centrally and atomically; it never
accepts a whole-wave diff as attribution for an individual producer.

`pocock_pregate` runs each declared `verification` command by direct `argv`
execution, applies the configured per-ticket (per-attempt) diff ceiling, and
checks the combined diff. A `ui_live` ticket also receives an attempt-bound challenge token:
the host must record successful `open` and then `exercise` evidence against that
same token, target, and criterion through `browser` or `xdev`. Worker assertions,
screenshots without the challenge, and evidence from another attempt do not
satisfy it.

Any invalid patch, scope or `changedFiles` mismatch, failed verification, diff
ceiling breach, missing UI proof, or rejected quality gate rejects the affected
attempt or wave. The runtime rolls back centrally applied patches before repair or
blocking; do not manually merge, preserve, or replay producer changes. Retries are
authorized only by the current state card and use a fresh attempt.

After a passing pre-gate, the runtime dispatches exactly the fixed Standards, Spec,
and Critic lenses. Their reports are adjudicated centrally; the Critic is the sole
PASS/FAIL verdict, and acceptance additionally requires no surviving blocking
Standards or Spec finding.
