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
| INPUTS | Complete inline contracts, resolvable repository paths with qualified symbols or records, full URLs, fully qualified issue URIs, and accepted upstream artifacts. | The isolated worker guesses, repeats discovery, or relies on an unavailable conversation. |
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

Do not add a route, slot, agent, model, reviewer, verdict, retry instruction, or
output schema. Those values are sealed after the control plane classifies the
ticket.

## Sealed INPUTS and dependencies

`INPUTS` must resolve without tracker shorthand or conversation context. It may contain
a complete contract embedded in the ticket, a resolvable repository-relative path, a full
URL, or a fully qualified `issue://owner/repo/N` URI. A bare tracker reference such as
`#123` or `Issue #123` is forbidden: it does not identify a
repository and cannot be sealed for an isolated worker. The core rejects such a reference
with `incomplete_tracker_reference`; a sealed-input binding failure aborts the
command rather than dispatching against an inferred source.

The ban is limited to incomplete tracker references. It does not prohibit a commit hash,
a Markdown anchor such as `docs/guide.md#section-2`, a source-language token such as
`#define` or `C#`, a full URL, a fully qualified issue URI, or the name of a repository
fixture.

Dependencies bind accepted outputs, not instructions to rediscover context. A dependent
ticket names the accepted upstream artifact by its repository path or durable URI and the
established interface or fact it consumes. Never make a dependency say to read a tracker,
an issue discussion, IRC, chat history, or another ticket's prose.

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

**Pass accepted outputs, not transcripts.** A dependent ticket names the accepted
upstream artifact and the established fact it consumes. The artifact on disk or at its
durable URI remains authoritative; a tracker, IRC, or chat instruction is not a dependency.

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

Any dispatch, state, or sealed-INPUT binding failure aborts the command. A failure the
core can safely attribute to one producer attempt rejects that attempt without discarding
its neighbours. A settled native task is one-shot: never wait for or revive it through
Hub. A retry is a new sealed attempt authorized only by the current state card.

After pre-gate, the core dispatches one **wave-level** fixed gate over exactly the
pre-gate-passed producer attempts. A wave may mix `mechanical`, `skilled`, and
`judgment` producer attempts on their respective slots. Configuration makes producer and
lens slot sets disjoint and the three lens slots pairwise distinct. Before dispatching
lenses, the core fails closed with
`independent_reviewer_unavailable` if a lens's opaque `resolvedModel` string exactly
matches that of any producer in the wave; it does not classify vendors or families. Each
lens returns `{lens, summary, reports:[{attemptId, summary, findings, verdict}]}`, with
one report for every passed producer attempt. Standards and Spec emit `NO_VERDICT` per
report; Critic emits `PASS` or `FAIL`. A lens execution or schema failure retries only
that failed lens on the same slot. A missing retry diagnosis uses `lastFailureKind`;
`capability` deepens the ticket class (writers stop at `skilled`, exhausted depth blocks
as `escalation_exhausted`) while `availability` preserves the slot and leaves model
replacement to OMP. Central adjudication preserves accepted
producer tickets. During partial acceptance, each rejected ticket routes directly by its
recorded rejection cause, without a separate `retry`; acceptance additionally requires no
surviving blocking Standards or Spec finding.
