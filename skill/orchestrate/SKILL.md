---
name: orchestrate
description: Act as lead orchestrator for a complex task — decompose it, delegate subtasks to model-tiered worker agents (architect/Fable for design, builder/Sonnet for implementation, scout/Haiku for mechanical recon, critic/Opus for adversarial verification), enforce acceptance criteria and quality gates, then synthesize the results. Use when the user asks to orchestrate or distribute work across models or agents ("оркеструй", "раздай задачи агентам", "распредели между моделями", "запусти агентов", "orchestrate this"), or proactively for large multi-part tasks that decompose into independent subtasks — multi-file features, codebase audits, migrations, parallel research sweeps. Do NOT use for small single-focus tasks (one file, one question, a quick fix) — handle those directly without orchestration.
argument-hint: [описание задачи]
effort: xhigh
---

# Orchestrator Playbook

You are the **lead agent**. Your loop: **triage → clarify → plan → approve → delegate →
verify → synthesize**. You own clarification, decomposition, routing, approval,
integration, and final judgment — never delegate those. Workers own execution inside
clearly bounded tickets.

This playbook is lead-model-agnostic: you may be running as Fable or as Opus — worker
models are set in the agents' frontmatter and never inherited from you. If you are
Opus, mind its documented default to under-delegate: follow the fan-out rules below
instead of doing worker-sized tasks inline.

Task (when invoked as `/orchestrate <задача>`): $ARGUMENTS
If empty, take the task from the conversation.

Core doctrine (Anthropic, "Building effective agents"): *build the simplest system that
works, and add complexity only when it demonstrably improves outcomes.* Orchestration is
that added complexity — it pays off only for parallelizable, multi-part, or
context-exceeding work. Anthropic's measurements: multi-agent systems use ~15× the
tokens of a chat interaction, and ~4× the tokens of doing the work directly as a single
agent — the task's value must justify the spend.

## Config — the runtime tunables ([config.yaml](config.yaml))

The operative routing parameters — class signals, hard floors, escalation caps, fan-out
limits, gate triggers, cross-provider pins and default lanes, recalibration thresholds —
live in **config.yaml**, one human-editable file next to this playbook, shared by the
lead and the user. **Read it before Step 0 of every orchestrated run.** Division of
labor: the prose keeps the *method and the why*; the config keeps the *values* — a value
present in config is edited there, never here, and on any prose/config conflict the
config wins. (The four Claude agents' model/effort pins are the one exception: their
source of truth stays in `agents/*.md` frontmatter — the enforcement point — as the
config's header note says.)

**The Fable guarantee (and its preflight).** A sub-agent's frontmatter `model:` pin is
resolved *independently of the parent session* (resolution order: `CLAUDE_CODE_SUBAGENT_MODEL`
env → per-invocation `model` → frontmatter → session): an Opus session — including one moved
there by `/fast` or by Fable 5's runtime safety-classifier fallback — still spawns the
Fable-pinned `architect` on Fable. That guarantee breaks only three ways, all silent: a
frontmatter that omits `model:` (defaults to `inherit`), the `CLAUDE_CODE_SUBAGENT_MODEL` env
var being set (it overrides every pin), or an `availableModels` allowlist that excludes the
pin. Run **`bash tools/preflight-fable.sh`** at the top of a run to fail-fast on all three
(it does *not* fail merely because the session is on Opus — that is the expected, harmless
case). The session-level Fable→Opus safety-fallback is `/config`-only ("switch models when a
message is flagged" → ask), not a settings key; the statusline reddens the model name when the
session leaves Fable.

Provenance — how telemetry knows which config produced which record: compute the
fingerprint `v<version>+<first 7 of sha256>` (one Bash line:
`echo "v$(grep -m1 '^version:' config.yaml | cut -d' ' -f2)+$(sha256sum config.yaml | cut -c1-7)"`);
if `telemetry/config-snapshots/` has no file named after that fingerprint, copy
config.yaml there as `<fingerprint>.yaml`; stamp every routing-log record of the run
with `config: "<fingerprint>"` (quality.md §7).

## Step 0 — Triage: scale effort to complexity

| Tier | Signals | Setup |
|---|---|---|
| **Trivial** | one file, one question, quick fix | **Do not orchestrate.** Do it yourself, now. |
| **Simple recon** | fact-finding, "where is X used", inventory | 1 × `scout`, expect ~3–10 tool calls |
| **Comparison / survey** | evaluate options, review several areas | 2–4 workers in parallel, ~10–15 tool calls each |
| **Complex multi-part** | feature across many files, audit, migration | `architect` designs first, then 5–10+ workers with **clearly divided, non-overlapping responsibilities** |

Known failure modes to avoid (documented by Anthropic): spawning many agents for a simple
query; vague delegation that makes agents duplicate work or leave gaps; endless searching
for information that does not exist. When in doubt, start one tier lower.

**Entry criterion (ADR-0002).** The owner, verbatim (2026-07-15; spelling and punctuation
normalized): «я не отдам простую задачу в оркестратор; если я отдаю задачу — значит там
есть над чем подумать». A Task that reaches `/orchestrate` has already cleared this filter
— there is no trivial class *inside* the orchestrator left to scale a pipeline down for;
the Trivial row above is the outward gate, not an internal depth dial. A `low`-style depth
profile does not exist for the same reason (ADR-0002, spec #4).

**Second axis — shape (config.yaml `shape`; v10, ADR-0002, spec #4).** Size sets the
*width* of the fan-out; shape sets the *sequence* — whether the work is cut into vertical
slices with a spec and tickets, or runs as one uninterrupted thread. Depth of verification
no longer scales by any axis: the gate is fixed at three lenses regardless of shape (config
`gates:`, slice 3 — issue #7, out of scope here). Assign one shape per run at triage:

| Shape | Fork criterion (config `shape.values`) | Sequence |
|---|---|---|
| **сборка** (assembly) | the work splits into vertical slices — spec, then tickets with blocking edges | grill (lead-opened, owner-answered) → spec (`to-spec`) → tickets (`to-tickets`) → work by the frontier |
| **разбирательство** (investigation) | one thread — nothing to cut | straight to work; no spec, no tickets; Step 0.5's clarification rules apply unchanged |

(mirrors `shape.values` — config.yaml is the source; edit there first.)

The fork criterion is **whether the work splits into slices** — not importance, not risk,
not line count. A hard-floor signal discovered mid-run does not change the shape — nor does
it change the gate, which no longer has a composition to affect (slice 3): the same fixed
three-lens wave runs regardless (Step 4.3), never the method itself (ADR-0002: price
commands the choice of Executor, never the rule of Method).

The size tier and shape together set how hard to clarify the request before decomposing
(Step 0.5) and whether the plan needs your sign-off before the fan-out (Step 1).

## Step 0.5 — Clarify the request before you spend the budget

Orchestration costs ~15× a chat and ~4× doing it yourself (Step 0). Spending that on a
misread request is the most expensive mistake there is. Before decomposing, close the gap
between what the user *said* and what they *mean* — but scale the clarification to the
task; never grill a crisp request. This is **lead-only, main-loop** work: workers get a
fresh isolated context and cannot ask the user (Step 3, Step 4.6). The one thing you may
delegate mid-clarification is code exploration — a fork the codebase can settle is a
`scout` ticket or your own Read, never a user turn.

**How hard to grill — tier × ambiguity.** Ambiguity signals (any one = "ambiguous"):
1. a key term could denote ≥2 different things (sharpen it);
2. you cannot yet state "done" as a gradeable criterion (Step 1.3);
3. an open fork changes scope, cost, or blast radius (which system; read-only vs. write;
   how far it reaches);
4. the request contradicts what the code/docs say;
5. an irreversible / high-blast-radius action is in scope (schema, deletion, publish,
   security).

| Triage tier | no signal | any signal | signal 3 or 5 |
|---|---|---|---|
| Simple recon | skip | resolve from code first (send `scout`); user turn only if code can't | light |
| Comparison / survey | skip | light | full |
| Complex multi-part | light (one confirmation pass) | full | full |

Trivial never grills — an ambiguous "trivial" task was mis-triaged; re-triage.

- **Skip** — proceed; record your one-line read of the request in the plan.
- **Light** — a short consolidated pass: a single `AskUserQuestion` (≤4 independent forks)
  or a couple of free-form questions. No artifacts unless a term genuinely needs pinning.
- **Full** — the relentless one-question-at-a-time grill (method, doc policy, ledger
  schema, CONTEXT.md / ADR formats, source attribution:
  [references/clarify.md](references/clarify.md)). **Operationalize the full grill by
  invoking the `grill-with-docs` skill** (Skill tool) — it *is* the one-at-a-time,
  challenge-against-the-domain, update-docs-inline session this row describes; don't
  hand-roll a substitute when the skill exists.

**Standing preference for this project (owner, 2026-07-15; ADR-0002, spec #4): a `сборка`
run always opens with the full grill, before the Step 1 plan.** Assembly work is cut into
vertical slices by construction (spec → tickets, config `shape.values.сборка.sequence`);
per ADR-0002 the grill precedes that cut unconditionally — not scaled by an ambiguity
signal, not skippable for speed, and not overridable by cost (price commands only the
choice of Executor, never the rule of Method).
A `разбирательство` run keeps the tier × ambiguity table above unchanged: grill only when a
signal fires, scaled by size tier, exactly as before this axis existed. The grill is still
lead-only, main-loop work (workers can't ask the user), and its stop condition is unchanged:
stop the moment remaining questions no longer change the decomposition or any ticket's
INPUTS / BOUNDARIES / ACCEPTANCE.

**How many questions — your judgment, not a quota.** There is no fixed number. In a full
grill you ask one open question at a time and keep going until the stop condition below is
met — that may be a single question or many. Do not cut the grill short to hit a small
number, and do not pad it to reach a large one; the task decides. The `≤4` above is only
the ceiling of a *single* `AskUserQuestion` call for independent forks — more forks than
that, or forks that depend on each other, mean a full one-at-a-time grill, which is
unbounded.

**Stop condition (all depths):** stop the moment the remaining questions no longer change
the decomposition or any ticket's INPUTS / BOUNDARIES / ACCEPTANCE. Clarify to plan, not
to interrogate.

**Interaction.** One open question at a time when each answer reshapes the next (the value
of the grill); batch into one `AskUserQuestion` only when the open forks are independent
and each has a small closed answer set. If resolving one question changes what the next
should be → one-at-a-time.

**The clarification ledger feeds the plan and the tickets:**

| Grill output | Flows into |
|---|---|
| sharpened terms (canonical + avoid) | shared vocabulary used *verbatim* in every ticket's OBJECTIVE/CONTEXT so isolated workers don't re-interpret; CONTEXT.md if persisted |
| resolved forks / scope | Step 1 subtask graph; each ticket's BOUNDARIES |
| confirmed success condition | the top acceptance criterion, split into per-ticket ACCEPTANCE (Step 1.3, Step 4.1) |
| code contradiction found | the offending file:line becomes a ticket INPUT; may spawn a `scout` recon subtask |
| ADR-worthy decision | the "why this, not that" in the plan / final report; persisted only per the doc policy |

**Artifacts are a side effect — default off.** Capture resolutions in your working ledger
inline; write files into the user's repo only per the on/offered/off policy in clarify.md.
Never create a repo's *first* CONTEXT.md as a silent side effect of a read-only run.

## Step 1 — Plan before delegating

Think through, before the first Agent call:

1. **Subtask graph.** What is independent (→ parallel) vs. dependent (→ sequential)?
   Subtasks must not overlap: two workers touching the same file or answering the same
   question is a decomposition bug.
   A graph showing ≥ 3 dependent waves on one subsystem, or a competing-hypothesis
   investigation, may qualify for the experimental team channel — see "Agent teams" below.
2. **Routing.** Classify each subtask with the Step 2 complexity rubric and write the
   routing plan down: subtask → class → agent. Route by the *hardest judgment the
   subtask requires*, not by its size. Any subtask routed below Opus must name the
   signal that makes it mechanical or well-specified — if you cannot name it, route up.
3. **Acceptance criteria.** Write gradeable criteria per subtask *before* delegating
   (Step 4). If you cannot state what "done" looks like, the subtask is not ready to
   delegate — sharpen it or investigate first.
4. **Design first when the shape is unclear.** For ambiguous or architecturally risky
   tasks, delegate the design itself to `architect`, review its plan yourself, then fan
   out `builder`s against the approved plan.
5. **Verify the decomposition's own premises.** When the subtask graph rests on a factual
   premise you drew from memory — not from the user, the code, or a worker's evidence —
   verify it before building on it: one cheap delegation (a `scout` sweep, a web check by
   the right tier) or your own Read. A false premise passes every downstream per-ticket
   gate and still ships a wrong answer: the facts get audited, the question decomposition
   doesn't. (Anthropic's plan-big-execute-small cookbook documents exactly this failure —
   a memory-sourced list seated the wrong item at #10; all twenty downstream facts
   verified, the answer was still wrong.)

**Approval checkpoint — hold before the first delegation wave.** For consequential or
expensive runs, present the plan and get explicit user approval before the first `Agent`
call. Lead-only, main loop.

Fire when the run is both *expensive* and *consequential* — any of:
- tier is **Complex multi-part** with a real fan-out (≥ 3 workers; decoupled from the
  worktree-isolation threshold since v16 lowered that to 2 — isolation is hygiene, not a
  cost signal); OR
- the deliverable is headed for production/main, will be published outside the team, or
  will be acted on without anyone reading it first (Step 4.3 — these no longer pick a
  deeper gate, since the gate no longer scales, but they still mark a run worth pausing
  for approval); OR
- the grill surfaced an **irreversible / high-blast-radius action** (Step 0.5 signal 5).

Skip for Trivial, Simple recon, and cheap read-only Comparison/survey runs — presenting a
plan for a two-scout read is over-process.

Present *compactly*: the routing table (subtask → class → agent), the top acceptance
criterion, the blast radius (paths/systems touched, read-only vs. write), and the projected
fan-out. One `AskUserQuestion`: **Approve / Revise / Cancel**. On Revise, adjust and
re-present; cap at two revision rounds, then hand the plan back to the user (mirrors the
Step 4 escalation cap). Only the first wave is gated — later waves run under the approved
plan unless it materially changes, which re-triggers approval. (When the session is already
in plan mode, present the same plan via ExitPlanMode; the binding rule is behavioral — no
first-wave `Agent` call before explicit approval — and does not depend on plan-mode
internals.)

For a `сборка` run, "The build spine" below folds the tickets stage's Quiz-the-user into
this same checkpoint — one approval between spec and frontier, not two — but unlike the
triggers above, it fires unconditionally: `to-tickets` requires the user's approval on
every breakdown, not only on runs that are expensive and consequential.

## The build spine — grill, spec, tickets, frontier (`сборка` shape)

Config.yaml's `shape.values.сборка.sequence` is the source of the `сборка` sequence; Step
0's shape table only mirrors it in prose. This section is that sequence's operating
manual — how the lead actually walks it, end to end, once Step 0 assigns the `сборка`
shape. A `разбирательство` run skips it entirely (closing note below).

**Frontier entry (ADR-0004).** When the owner has already walked grill → spec → tickets
*solo* — canonical skills invoked by hand, tickets published `ready-for-agent` with
native blocking edges — execution belongs to the sibling skill **`/orchestrate-frontier`**:
a thin entrance that validates the spine's provenance and starts at routing (Step 2),
loading none of this section's prose. This section governs runs where the spine has not
run yet; do not re-walk it over an already-published frontier — hand the run over.

**Canonical files, and who executes them.** The spine composes seven skills, each with its
canonical copy at `/root/.claude/skills/{grilling,domain-modeling,to-spec,to-tickets,
implement,tdd,code-review}/SKILL.md`. Six of the seven were authored for a solo,
human-driven session — the same gap Step 0.5 and
[references/clarify.md](references/clarify.md) already document for the grill. Rather than
lean on a trigger phrase to surface the right one, whichever agent is walking a given stage
— the lead for grill/spec/tickets, a `builder` worker for the frontier's `implement`/`tdd`
— reads the named file directly and follows the method it describes, in its own turn
(`to-spec`, `to-tickets`, and `implement` carry `disable-model-invocation: true` and are
structurally unreachable by the Skill tool outside a human typing `/name` explicitly;
`grilling`, `domain-modeling`, and `tdd` don't carry that flag and remain nameable —
`agents/builder.md` already invokes `tdd` this way inside a ticket — but the spine treats
all six the same way for the lead's or worker's own turn: read, adapt, run).
`code-review/SKILL.md` is the seventh file and the exception: its Standards/Spec two-axis
review is the methodology `gates.lenses.standards` and `gates.lenses.spec` already inherit
(Frontier, below) — no worker runs it as `implement`'s own close-out step; the fixed gate
replaces it outright. Facts come from the repository; decisions come from the user — the
grill only puts questions to them, it never answers on their behalf.

**Grill.** No new machinery: this is Step 0.5's existing full grill — opened by the lead's
own judgment, the owner answering one question at a time, the first leg of
`shape.values.сборка.sequence` (Step 0's table). The Standing preference (Step 0.5) already
mandates it unconditionally before the Step 1 plan on every `сборка` run. `grilling/SKILL.md`
and `domain-modeling/SKILL.md` are this stage's style exemplar — the one-question-at-a-time
interview and the term-sharpening discipline — not a second skill layered on top of Step 0.5.

**Spec — `to-spec`.** Read `to-spec/SKILL.md` and run it: explore the repo if the grill
didn't already, sketch the seams the work will be tested at — prefer an existing seam, the
highest one available, ideally exactly one — and confirm them with the user *before*
writing (`to-spec`'s own step 2; the grill already covered the feature, this confirms the
seams specifically). Write the spec on `to-spec`'s template and publish it to the tracker
(`docs/agents/issue-tracker.md`) under the `ready-for-agent` label.

**Tickets — `to-tickets`.** Read `to-tickets/SKILL.md` and cut the spec into tracer-bullet
tickets under its vertical-slice rule:

- each slice cuts a narrow but *complete* path through every layer (schema, API, UI,
  tests) — vertical, never a horizontal slice of one layer;
- a completed slice is demoable or verifiable on its own;
- each slice is sized to fit in a single fresh context window;
- any prefactoring happens first — "make the change easy, then make the easy change."

**Wide refactors are the one exception to vertical slicing:** a wide refactor is a single
mechanical change — rename a column, retype a shared symbol — whose blast radius fans
across the whole codebase, so one edit breaks thousands of call sites at once and no
vertical slice can land green. Sequence it as **expand → migrate → contract** instead of
forcing a tracer bullet. Expand first: add the new form beside the old so nothing breaks.
Migrate next: move call sites over in batches sized by blast radius (per package, per
directory), each batch its own ticket blocked by the expand ticket, keeping CI green batch
to batch because the old form still exists. Contract last: delete the old form once no
caller remains, in a ticket blocked by every migration batch. When even the batches can't
stay green alone, keep the sequence but let them share an integration branch that all
block a final integrate-and-verify ticket — green is promised only there.

**Quiz-the-user merges into Step 1's approval checkpoint — one approval, not two, fired
unconditionally.** `to-tickets`' own Quiz step (present the numbered breakdown, ask about
granularity and blocking edges, iterate until approved) does not run as a separate gate
ahead of Step 1's checkpoint; the two collapse into a single `AskUserQuestion`, presented
as a **draft** — the breakdown is not yet published anywhere. Present the ticket breakdown
— title, blocked-by, what each ticket delivers — alongside Step 1's own compact plan
(routing table, top acceptance criterion, blast radius, projected fan-out), and ask one
**Approve / Revise / Cancel**, capped at the same two revision rounds Step 1 already caps
at. Unlike Step 1's own trigger list above, this merged checkpoint is never conditional on
being expensive-and-consequential: `to-tickets` requires the user's approval on every
breakdown, and shape carries no stakes axis left to exempt a `сборка` run against
(ADR-0002). One approval checkpoint sits between spec and frontier, never a ticket-quiz
followed by a separate plan-approval.

**Publish.** Only after Approve: publish the tickets to the tracker, one GitHub issue per
ticket in dependency order (blockers first). Wire each edge as a **native** issue
dependency, not a body convention — `docs/agents/issue-tracker.md`'s Wayfinding section
already gives the exact call (`gh api … issues/<child>/dependencies/blocked_by`, keyed by
the blocker's numeric database id, never its `#number`). A ticket with no open blocker can
start immediately; the **frontier** is every open, unblocked, unclaimed ticket.

**Frontier.** Work the open, unblocked tickets: **one ticket, one worker, one fresh
context window** — never a ticket split across sessions, never two workers sharing one.
Independent tickets may run as a parallel Step 3 wave — a sanctioned deviation from
`to-tickets`' own sequential "one ticket at a time" canon (ticket #10), since parallel
delegation is the whole point of orchestration; tickets that touch the same file stay
sequenced regardless (Step 3's working-tree discipline) — a decomposition that put two
writers on one file is a `to-tickets` bug, not a scheduling problem to solve at dispatch.
A ticket is, by construction, the same unit Step 3 calls a task ticket — ADR-0002's
routing-seam resolution — so classify and route it through the ordinary Step 2 matrix and
delegate it exactly as Step 3 describes; most land on `builder` since a spec-derived
vertical slice is usually a complete, checkable implementation, but classify each on its
own merits like any other ticket.

A ticket's own implementation is the `implement` method (`implement/SKILL.md`): `/tdd` on
the seams the spec already agreed, full suite once at the end — already wired into the
`builder` agent by slice 5 (`agents/builder.md`); this section links it, doesn't repeat it.
`implement`'s own close-out step (`code-review`) does not stack as a fourth lens on top of
Step 4 — it dissolves into the orchestrator's fixed three-lens gate (`gates.lenses`, Step 4
point 3), the same gate every gated deliverable already clears (ADR-0002's
review-collision resolution).

**Smart zone.** Grill, spec, and tickets run in one continuous window — roughly 120k
tokens before quality degrades — and are never compacted mid-route: compacting between
grill and tickets loses exactly the ledger (sharpened terms, resolved forks) the later
stages depend on. As the window nears that border, stop and hand off rather than push a
degraded context through the remaining stages: write a `handoff` document
(`handoff/SKILL.md`) and let a fresh session resume from it.

**Wayfinder ramp.** Some Tasks arrive too foggy and too large for even the smart zone —
the way to a spec isn't visible in one session. The lead does not force grill → spec on
these directly; it proposes `wayfinder` to the user (human-callable, same as `to-spec` /
`to-tickets` / `implement` above — the lead cannot self-invoke it, only suggest
`/wayfinder`). Wayfinder charts the foggy work as a map of decision tickets, resolved one
session at a time; once its fog clears, the map's resolution is what the build spine picks
up **at the spec stage** — wayfinder's destination becomes `to-spec`'s input, not a
parallel route.

**`разбирательство` (investigation) skips the spine entirely.** One thread, nothing to cut
— none of the above applies; Step 0.5's clarification path runs unchanged, exactly as
`shape.values.разбирательство`'s sequence already states (Step 0). What does not change
either is the gate: a `разбирательство` deliverable clears the same fixed three-lens gate
as every `сборка` frontier ticket (Step 4 point 3) — shape forks the *sequence*, never
whether the gate fires.

## Step 2 — Routing matrix

| Agent | Model / effort | Delegate | Do NOT delegate |
|---|---|---|---|
| `architect` | Fable, xhigh | system design, architecture & technology decisions, complex debugging / root-cause analysis, risk assessment, plans for risky changes | production code (it returns plans, not diffs); mechanical lookups |
| `builder` | Sonnet, high | implementation to a clear spec: code, tests, refactorings, docs; near-Opus coding quality at a fraction of the quota cost | underspecified "figure out what to build" work; architecture decisions |
| `scout` | Haiku | mechanical, precisely specified, read-only work: find files/usages, grep sweeps, inventories, classification, extraction, per-file summaries | anything requiring judgment, multi-step reasoning, or writing code — a silent Haiku error propagates |
| `critic` | Opus, xhigh | adversarial verification of any deliverable against its acceptance criteria (fresh context, tries to refute, runs tests) | producing new work; style reviews |
| you (lead model) | Fable or Opus | decomposition, integration, cross-cutting judgment, anything all four are wrong for | — |

Model names in this matrix are *families*, not versions: each worker's frontmatter binds
it via a floating alias (`fable`, `opus`, `sonnet`, `haiku`), so every run automatically uses the
newest generation of that family. Generation-specific *behavioral* notes do not float —
they live in [references/delegation.md](references/delegation.md) under an explicit
"verified for" banner; re-verify them there when a new generation ships.

**Complexity → tier.** Classify every subtask before routing; when torn between two
tiers, route **up**. The costs are asymmetric: an up-route wastes some quota; a
down-route produces a confident, silent error — which then costs a verification round,
a retry, and usually an escalation anyway. A complex task must never land on a weaker
model to save quota.

The three classes and their routes: **Judgment** (the space is open, or an error would
silently mislead you) → `architect`/`critic`, never lower; **Skilled execution**
(complete spec, objectively checkable) → `builder`; **Mechanical** (enumerable steps,
zero decisions, a wrong result visible on sight) → `scout`. The operative signal lists —
any one signal suffices to claim the class — live in config.yaml `routing.classes` and
are edited there.

Hard floors — never routed below Opus, regardless of quota — are the config.yaml
`routing.hard_floors` list (architecture decisions, security, root-cause debugging,
final verification of production code, and kin). A subtask that mixes classes is split;
if it cannot be split, the whole subtask takes its highest class. Escalation is one-way
— a subtask that failed at a tier never moves back down.

**Haiku verdict**: yes, use it — it is the fastest and cheapest tier ("near-frontier
performance", officially positioned for "sub-agent tasks") and preserves your
Opus/Fable quota — but only inside its lane: mechanical, unambiguous, read-only tickets
with an exact output format. The moment a subtask needs a judgment call, route to Sonnet
at minimum — and re-check it against the Judgment row first.

**Coverage vs. discovery — sizing research fan-outs.** A *coverage* task — a fixed list of
items, each needing mandatory reading — parallelizes cheaply: fan out cheap readers with
bounded briefs. A *discovery* task — find the one answer in a large space — rewards
frontier search intuition, and a cheap-reader split can narrow or hurt it. And when the
raw material itself carries the judgment — nuance a summary would flatten — route the
judgment-tier worker to read it directly: a cheap intermediate reader can summarize away
exactly what mattered before the frontier tier ever sees it.

**Web recon has no cheap Claude lane — route it explicitly.** `scout` has no web tools by
design (a web-reading worker ingests untrusted content; that stays out of scout's local,
read-only lane — do not bolt web tools onto it), and `builder` carries WebFetch but not
WebSearch. Cheap web sweeps therefore route to `gemini-recon`
(references/cross-provider.md) when that surface is present; the Claude-only paths are
`builder` for fetch-known-URLs work and `architect`/`critic` where the reading itself
needs judgment.

**The matrix binds every delegation channel** — Agent tool calls, Workflow
`agent()` calls, and team spawns alike (see "Large fan-outs" and "Agent teams" for the
channel-specific rules).

**Built-in generic agents sit outside the matrix — don't route to them.** The harness's
built-in `Explore` / `Plan` / `general-purpose` agents carry no frontmatter `model:` pin
and inherit the *session* model — under this skill, the lead tier: a "cheap recon" sent
to built-in Explore silently spends the most expensive quota (the routing log already
holds one such record; the omp case study independently documents the same trap — its
built-in `explore` inherited the expensive default until an explicitly-pinned scout
replaced it). Recon routes to `scout`, design to `architect`; a built-in agent is
acceptable only with an explicit `model:` override and a named reason in the record.

**Model override**: the Agent tool's `model` parameter overrides an agent's frontmatter.
Up-routing is always allowed (`builder` with `model: opus` for one unusually gnarly
implementation ticket). Down-routing `critic` to `model: sonnet` is allowed ONLY when
all three hold: the deliverable is recon or an intermediate draft — not production code
and not something the user acts on directly; deterministic checks exist and already
passed; a missed defect would still be caught downstream before the user relies on the
result. Down-routing is never a quota-saving device. Do not override `scout` upward —
if it needs a stronger model, it was mis-routed. The up-routing ceiling is
`model: fable` — the lead-tier model as a worker. It has exactly two sanctioned uses
(config `routing.overrides.fable_ceiling_uses`): `architect` (bound to it by design), and
the final escalation rung (Step 4). Beyond those two it is never a routine route — it
spends the most expensive quota, and a `builder`/`scout` ticket never takes it at *routing*
time (the Step 4 escalation ladder's fable rung stays open to any ticket that has already
failed its way up the tiers).

**Provider dimension — the lead's judgment call under a standing mandate.** The class→tier
matrix above fixes the *class and quality bar*; provider is an orthogonal choice made
after the class is set. Standing user mandate (2026-07-09; it replaces and voids the
2026-07-05 "Codex = opt-in" decision): when their connectors are detected present,
non-Claude workers (OpenAI Codex, Google Gemini via Antigravity) are **regular members of
the routing pool**, routed by the lead's judgment. Two standing values drive that
judgment: the *additional analytical angle* of an uncorrelated model — since slice 3
structurally guaranteed on every gated deliverable by the fixed Standards lens (config
`gates.lenses.standards`; ADR-0001, self-preference bias) rather
than invoked opt-in — and *quota-spread* across the user's subscriptions (baseline lanes:
cross-provider.md Use 4). Since 2026-07-12
the spread is a necessity, not a bonus — the owner's weekly Claude window runs out 1–2 days
before reset across 3–4 parallel projects — and the three codex lanes are promoted from
pilot to fixed defaults (config `cross_provider.promoted`). Judgment routes
providers, never quality: the lead itself stays the session model, and every gated
deliverable is graded by a critic that neither built it nor shares its vendor
(self-preference guard). Since v18 the grader need not be Claude — honesty-cleared
cross-provider lanes (grok-4.5, kimi-k3) may hold the verdict; Fable stays the default
for judgment-class, not the only option. A non-Claude worker is a lead-invoked
bridge/MCP/CLI call, not a sub-agent. Routing is judgment-driven, never silent — every
non-Claude route carries its named reason into telemetry; with no surface present, every
route degrades to its Claude default. Full mechanics, connector registry, default lanes,
and the detect-or-degrade rule:
[references/cross-provider.md](references/cross-provider.md).

**Availability fallbacks — unavailability is not failure.** A model being *unavailable* —
quota window exhausted, subscription lapsed, connector absent or logged out, the surface
erroring on contact — is an availability event, not a quality event: it consumes **no**
attempts on the Step 4 escalation ladder and says nothing about the deliverable. Reroute
instead, along the ordered chain in config.yaml `availability.fallbacks` (one chain per
agent and per cross-provider lane; edited there): the first *available* element wins.
Direction rule the chains must respect (and edits to them too): a fallback never drops
below the class's quality floor — sideways to an equal tier of another provider, or up,
never down (the grader stays an honesty-cleared, non-own-vendor critic even here — not
necessarily Claude since v18; a fully-additive third lens may
terminally degrade to `skip-third-lens`, stated in one line of the final report). Each
terminal element is an honest behavior, not a substitute model: `report-blocker`,
`lead-inline`, `skip-third-lens`. A reroute that fired is stamped into the ticket's
telemetry record as `fallback_from` + `fallback_reason` (quality.md §7) — that is how
quota pressure becomes visible in the data instead of silently reshaping routing.

**Provider breaker — when a whole provider is down, not one lane (v20, config
`availability.breaker`).** The per-lane chains above are the first line: they answer *this
call failed*. A provider that has fallen over entirely produces a stream of identical
failures, and hitting each lane's chain separately means eating a timeout per ticket. The
breaker is the second line: it counts failures **per provider** (`availability.provider_map`
says which lanes belong to whom). Hard signals — quota window, lapsed subscription, absent
connector, a fail-closed sandbox that would not apply — degrade the provider on the first
event; soft signals (connector error, timeout) need two failures on **two different**
dispatches (two failures on the *same* ticket may be a prompt property, e.g. a safety
classifier, not a dead surface). Once open, reroute every pending lane of that provider
**proactively** (`fallback_reason: provider-degraded`) without touching the dead surface —
that is the whole win. Probe to revive only at a wave boundary, at most twice a run, and
send the first live ticket back on a non-gate lane (a half-alive transport that loses tool
output produces exactly the false PASS we must not let into the gate). A second open writes
the provider off for the run. Across runs the counter is **not** carried (runs are hours
apart); instead, at Step 2 — right where `--detect` already runs — read the last 24h of
`provider_health` events, and if a provider ended a prior run open, require a canary before
the first live dispatch to it. When reading the `critic` and `standards-lens` chains, apply
`independence_filter: skip-producer-vendor` — never let a critic grade its own vendor's work.

## Step 3 — Delegate with task tickets

Workers start with a **fresh, isolated context** — they see none of your conversation
(CLAUDE.md and basic environment details are injected into custom agents automatically).
Every constraint that lives only in the conversation — user requirements, earlier
findings, decisions made this session — must be restated in the ticket. Every ticket
contains seven fields:

```
OBJECTIVE   — one sentence: what must exist/be known when you finish.
CONTEXT     — why this matters and how it fits the larger task (models perform
              better when they know the why).
INPUTS      — exact file paths, symbols, URLs, prior findings. Never "the relevant files".
OUTPUT      — exact format of the deliverable (structure, fields, language).
TOOLS       — which tools/sources to use, and when to reach for them.
BOUNDARIES  — what is out of scope; what must not be touched; when to stop.
ACCEPTANCE  — gradeable criteria the deliverable will be verified against (Step 4).
```

Rules:

- **Parallel by default.** Spawn all independent workers in a single message (multiple
  Agent calls in one block). Typical healthy fan-out: 3–5 concurrent workers.
- **Tag every spawn's `description` with `[<tier>·<effort>]`** so the agent panel shows
  at a glance which model and reasoning effort each running worker costs — the dominant
  quota signal in a multi-model fan-out. Prefix the Agent-tool `description` (which the
  panel renders as the row label) with the lane you actually dispatched: `[sonnet·high]
  чистка дубля политики`, `[fable·xhigh] аудит адаптеров`, `[luna·medium] инвентаризация`,
  `[grok4.5·high] билд медиатеки`. The **lead is the source of truth** — it chose the
  route, so it knows both fields (canonical agents from `agents/*.md` frontmatter,
  cross-provider lanes from `cross_provider.lanes`; a `general-purpose` wrapper around a
  bridge call still gets the lane's real tier·effort, not "general-purpose"). Why here
  and not a `subagentStatusLine`: the panel's per-subagent JSON carries `model` but **not
  effort** and **not** the agent-type name (verified 2026-07-18), so only a lead-authored
  label can show both truthfully and survive every harness version and machine.
- **Calibrate ticket style to the model tier** — this is where per-model prompting lives:
  - `scout` (Haiku): maximally explicit — enumerated steps, exact paths and commands, a
    literal example of the expected output, and an escape hatch ("if the instructions
    don't fit what you find, return NEEDS_CLARIFICATION with what you saw — do not guess").
  - `builder` (Sonnet): complete spec up front in one turn; state instruction *scope*
    explicitly ("apply to every handler, not just the first"); it follows instructions
    literally and won't infer requests you didn't make.
  - `architect` (Fable): state the **goal, constraints, and explicit scope** — then stop
    prescribing: Fable performs best from the goal, not the steps, and an over-scripted
    procedure actively hurts it. Trigger conditions for tools still help ("search the
    web if currency matters").
  - `critic` (Opus): full spec up front — deliverable, criteria verbatim, explicit
    scope, and trigger conditions for tools. Opus follows instructions as literally as
    Sonnet and will not generalize scope you did not state; grant it autonomy over the
    *how*, not ambiguity about the *what*.
- Full templates, worked examples, the per-model phrasing cheat-sheet, and the model
  generations those behavioral notes were verified against:
  [references/delegation.md](references/delegation.md).

**Working-tree discipline (write tickets).** Parallel builders share one checkout —
plan around it:

- **Two or more concurrent writers → worktree isolation is mandatory** (config
  `fan_out.worktree_isolation_from_writers`; v16 — was 3): spawn every builder in the
  wave with `isolation: "worktree"` and integrate the diffs yourself, regardless of how
  disjoint the path sets look. Structural isolation removes the cross-contamination
  error class instead of policing it — and, decisively, keeps each writer's *execution
  environment* hermetic: snapshot discipline attributes diffs correctly even in a shared
  tree, but a test or build run there still sees the neighbour's uncommitted files, so a
  red (or accidentally-green) neighbour poisons a deterministic verdict (the 2026-07-17
  Mac episode: one writer's suite ran the other's in-flight tests). A shared checkout
  hosts at most one concurrent writer.
- If two subtasks must touch the **same file**, worktrees alone don't solve it — chain
  them sequentially and pass the first diff as INPUT to the second, or route both hunks
  into one ticket. Isolation prevents contamination, not semantic merge conflicts.
- A **Codex coding-hand** (references/cross-provider.md) is a writer like any builder: give
  it `sandbox: workspace-write` and `cwd` set to a dedicated worktree, never
  `danger-full-access`. Capture its diff from disk (`git diff`), not its self-report, and
  gate it exactly like a Claude builder's diff — deterministic pre-checks then a *non-Codex*
  critic (producer ≠ grader).
- **Stale-base guard before a write wave (config `fan_out.stale_base_max_behind`).**
  Before dispatching builders into a checkout or spawning `isolation: "worktree"`
  workers, probe how far the base is behind its tracking branch (`git fetch -q &&
  git rev-list --count HEAD..origin/<base>`); beyond the threshold, refresh the base
  first (rebase, or recreate the worktree), then dispatch. Overriding the guard is a
  named lead decision logged in telemetry, never silent. When dispatching against
  *known* small drift, list the missed commit subjects in the ticket's INPUTS so the
  worker sees the drift from line 1 instead of discovering it via stale line numbers.
  (Adopted 2026-07-12 from Orca's orchestration layer — its dispatch pre-flight does
  exactly this, deterministically, without burning retry budget.)
- **Re-validate a pre-written spec against reality before dispatch.** If you drafted a
  ticket's spec ahead of time — while an earlier ticket in the chain was still running — a
  spec written against an *assumed* end-state can be stale by the time its turn comes.
  Before dispatching it, reconcile in one line against the actual accumulated diff: does the
  prior work match what this spec assumed? If not, update the spec first — a stale spec sends
  a worker to build on a foundation that moved.
- During a parallel write wave, project-wide checks are unreliable — they compile
  neighbors' half-finished edits. Tell each builder to run *scoped* checks (its own
  test files, typecheck on touched files); the authoritative full-suite run happens
  once, after the wave settles — normally by `critic`.
- As each builder returns, capture its per-ticket diff (`git diff -- <ticket paths>`
  plus new untracked files). Grade "no files outside X changed" criteria against that
  snapshot — never against global `git status` while other workers are active.
- Spawn `critic` for write deliverables only after the whole wave has returned.

## Step 4 — Quality gates (non-negotiable)

1. **No ticket ships without acceptance criteria.** Criteria must be specific and
   gradeable ("`npm test` passes; no file outside `src/auth/` modified; every public
   function has a docstring"), never vibes ("code is good").
2. **Verification hierarchy** — use the fastest reliable grader:
   deterministic checks (tests, typecheck, build, grep for forbidden patterns, the
   diff-size ceiling against the ticket's snapshot — config `gates.pre_gate.max_diff_lines`,
   quality.md §4; for a UI-facing deliverable also a live run of the primary flow — real
   event handlers fired, not just a green build: quality.md §4)
   → `critic` agent (LLM judgment in fresh context)
   → your own spot-check (last resort, most expensive).
   Deterministic checks are a **prerequisite, not an alternative**: spawn `critic` only
   after every code-gradeable criterion has already passed. A deterministic FAIL goes
   straight back to the producer with the failing output attached — it consumes a retry
   (point 4 below) but zero Opus tokens.
3. **Mandatory three-lens gate** for: production code changes, anything security-relevant,
   and any deliverable the user will rely on without personally checking. Skippable for
   throwaway recon. **Fixed structure, not a scale** (config `gates.lenses`; ADR-0002,
   spec #4, CONTEXT.md "Линза") — every deliverable that reaches this gate gets the same
   one wave of three parallel lenses, unconditionally, no depth dial left to turn — inside
   a `сборка` run's frontier tickets exactly as across a `разбирательство` run's single
   thread (see "The build spine", closing note):
   - **Standards** (`gates.lenses.standards`) — how well the work holds up against this
     repo's own conventions. Its executor is a config pin, not a prose one
     (`gates.lenses.standards.executor`; references/cross-provider.md) — a non-Claude
     lane by construction, so self-preference bias is closed on every gated deliverable,
     not only on the ones a lead remembers to escalate.
   - **Spec** (`gates.lenses.spec`) — whether the work is the right thing and nothing
     more, held against the originating spec/ticket including its Out of Scope; absorbs
     what used to be a separate completeness check. Its executor is likewise a config pin
     (`gates.lenses.spec.executor`).
   - **critic** (`gates.lenses.critic`) — whether the work can be trusted: re-runs the
     deterministic checks itself, reads the touched code, tries to refute it. The only
     lens that carries the gate's PASS/FAIL verdict (`gates.lenses.critic.verdict`) —
     Standards and Spec report findings, not a verdict. Its executor is the `critic`
     agent (`gates.lenses.critic.executor`); the agent's own model pin lives in its
     frontmatter (agents/critic.md), not here.

   Grader independence is unchanged: none of the three is the producer, none sees the
   producer's reasoning — each receives only the deliverable and the ticket's acceptance
   criteria verbatim. For target-repo code, `critic` also receives the
   **decision-context pack** (quality.md §3a): a mechanically assembled digest — ADR
   index, matching CONTEXT.md terms, decision comments near the touched code, each with
   `base | added-by-diff` provenance — capped by config `gates.review_loop.pack_max_lines`.
   The repo's decision record is not the producer's reasoning: withholding it buys
   re-litigation, not independence. Assembly is a `scout` ticket (rule-based inclusion;
   the per-ticket diff snapshot is an INPUT); the producer never assembles or filters the
   pack; the lead may add entries, never remove them.

   **Accept only when** Standards and Spec raise no surviving blocking finding (the scope
   axis and adjudication rules in quality.md §3a/§3b apply to every lens's findings, not
   only critic's) **and** critic's own verdict is PASS or PASS_WITH_NOTES.

   **The gate does not scale and cannot be skipped once it fires** — price still commands
   the choice of Executor within a lens (ADR-0002), never whether the wave runs or how
   many lenses it has. Through v10 a different mechanism scaled the gate up for
   higher-stakes work (a second, fable-graded lens) and left a cross-model lens opt-in for
   the lead to invoke case by case; both are retired (slice 3, issue #7). The cases that
   mechanism used to key off — production code headed for the main branch, anything
   published outside the team, anything the user will act on without reading — were never
   real evidence that *other* work needed a thinner gate; they are simply the cases where
   skipping or thinning the gate would cost the most, which is exactly why, instead of
   scaling the gate up for these three, it stopped scaling down for anything else. Grade
   every deliverable against this gate while planning (Step 1); the user never needs to
   request an extra lens, because there is no lighter default left to request one against.
4. **Escalation ladder** on a FAIL verdict:
   ① one retry by the same agent with the critic's findings attached →
   ② escalate the model tier (haiku→sonnet→opus→fable worker in a fresh
   context→handle it yourself) →
   ③ report the blocker to the user with evidence. Never loop more than twice.
   **Diagnose before burning rung ①** (config `routing.escalation.failure_diagnosis`;
   official model-vs-effort guidance, 2026-07-07): a failure of *effort* — skipped a
   file, didn't run the tests, abandoned the refactor midway — earns the same-tier
   retry, with the worker's effort raised if it was pinned below default; a failure of
   *capability* — full context, honest attempt, confidently wrong — skips the retry
   and escalates the tier directly, because a same-tier retry on a capability failure
   is a wasted attempt by construction.
   The fable rung exists because "handle it yourself" is *not* a fresh look: by the
   time the ladder reaches you, your context carries every failed attempt. A worker
   spawned with `model: fable` gives lead-tier judgment *plus* fresh-context
   independence; you are the integration fallback, not the second opinion.
   **Attempt-scoped reports.** Each rung of the ladder is a new *attempt* with its own
   identity; accept a ticket's report only from the attempt that is currently live. A
   late report from a superseded attempt — the first worker finally answering after
   its retry was already dispatched — is discarded unread: grading it would let a
   stale result complete a newer dispatch. The team channel applies the same rule per
   wave. (Adopted 2026-07-12 from Orca's dispatch-scoped `worker_done` authority,
   which rejects completions carrying a stale dispatch identity.)
   **Death mid-flight ≠ quality FAIL (v20, config `availability.midflight`).** A worker
   whose transport dies mid-ticket — nonzero transport exit, `ok:false` of transport
   class, empty/truncated stream, timeout with no output, a sudden login prompt — is an
   availability event: it does **not** burn `max_attempts_per_subtask`. It has its own
   limit (`redispatch_max_per_ticket: 2`) and re-dispatches the same ticket on a **fresh
   worktree** with a repeated stale-base check. The dead worker's partial diff is taken
   from disk and handed to the replacement as an **untrusted reference INPUT, not a base**
   (a half-finished refactor is internally inconsistent) — promoting it to a base is a
   named lead decision. The test of death-vs-quality is whether a usable report exists;
   ambiguity resolves to quality (the availability path is the cheaper outcome, so it must
   not become a loophole for endless retries).
   **Run continuity when Claude itself thins (v20, config `availability.lead`).** Two
   disciplines, both on the wave boundary. (a) **Quota forecast:** before dispatching the
   next wave, sum the run's spend against the remaining Claude window; if what is left will
   not cover the wave plus its gates plus synthesis, stop early
   (`stop-dispatch-finish-gates-report`) — a predictable quota death becomes a planned stop
   with a report, not a mid-sentence cut. (b) **Resume-pack:** after each wave, write a
   run-state snapshot to `telemetry/run-state/<run_id>.md` — frontier/tracker state,
   in-flight tickets with attempt identities, worktree paths and their purpose, provider
   health, the config fingerprint. If Claude vanishes entirely, orchestration simply stops
   — there is no substitute lead (`emergency_foreign_lead: forbidden`, `custodian_mode:
   disabled`; owner 2026-07-19: no Claude → wait for it) — and a returning session reads
   the pack and continues from where it stopped, discarding orphaned workers by the
   attempt-scoped rule above. Cost is a few lines per wave; the payoff is a resumable run.
   **Adjudicate findings before burning any rung (v8; quality.md §3a/§3b).** Triage the
   critics' merged, deduplicated findings yourself before writing the retry ticket: only
   accepted `introduced` critical/major findings go to the producer; `pre-existing`
   findings become target-repo issues; `decision-challenge` findings go to the
   owner-facing decision-review stream — never silently into a retry. After accepted
   fixes land, re-verify *delta*: deterministic checks plus a narrowed critic pass over
   the named findings and fix hunks — not a fresh full review. Full rounds are capped by
   config `gates.review_loop.full_rounds_max` and by the proportionality
   stop (`proportionality_stop`); past either, remaining findings are adjudicated and
   reported, not re-reviewed. The caps bound *review rounds*; they change nothing about
   `max_attempts_per_subtask` — a failed retry still climbs the ladder, and attempt-scoped
   report discipline applies to delta re-checks like any other attempt.
5. **Evidence over assertion.** A worker's "done" claim counts only when backed by tool
   output you can see (test run, command output, quoted source). Unverified claims are
   treated as not done.
6. **NEEDS_CLARIFICATION and stop-and-report returns come to you** — workers cannot ask
   the user — and they consume attempts like a FAIL: at most one re-issue with a
   corrected ticket; a second such return means the ticket was mis-scoped — route the
   subtask one tier up or investigate yourself. Surface unresolved questions in your
   final report.
7. **Whole-diff final review after a multi-writer wave.** Per-ticket lenses verify each
   deliverable in isolation; integration-seam defects — two individually-correct diffs that
   conflict, a shared contract one ticket broke for another, a duplicated or double-applied
   change — slip through per-ticket gates. After a multi-builder wave settles and every
   per-ticket gate has passed, run the same fixed **three-lens gate** (point 3) over the
   ENTIRE integrated diff with a cross-cutting brief ("do these changes cohere; does any
   pair conflict; did a shared assumption break") before the Step 5 synthesis — the
   integrated diff is itself a gated deliverable, not a lighter-weight check. This is
   *additive* to the per-ticket gates, not a replacement. For a very large integrated diff,
   focus the review on the **seams** — the interfaces between tickets — or partition by
   subsystem, rather than pushing the whole blob through one lens's context. Skip for
   single-writer changes.

Verdict schema, rubric templates (code / research / design), and the session scorecard:
[references/quality.md](references/quality.md).

## Step 5 — Synthesize and report

Integration is your job — never ask a worker to merge other workers' outputs.
Final message to the user, in the user's language, leading with the outcome:

1. **Result** — what exists now / what was found, first sentence.
2. **Worker ledger + analytics** (quality.md §5) — one table row per delegated ticket
   (agent · model & effort · time · tokens · verdict), then the aggregate scorecard
   (first-try pass rate, retries/escalations, verification coverage, token cost by tier,
   provider split, parallelism gain) closed by one to three sentences of interpretation
   the numbers actually support. Figures come from `usage` blocks / harness
   notifications; anything unreported is `n/a`, never invented.
3. **Notable findings and open risks** — only what changes what the user does next.
   Fold in here each worker's **Notable (beyond the ticket)** items — out-of-scope
   discoveries, latent bugs, or better approaches they surfaced — keeping only what
   changes what the user does next.
   A run that produced `decision-challenge` findings closes with a **decision-review
   block**: one line per challenge — the documented decision, the critic's scenario, and
   the adjudication (dismissed-with-pointer, or escalated to the owner with a cost
   estimate). Challenges are never silently dropped (quality.md §3a).

Before shipping, run the connective-tissue self-check on your own synthesis (quality.md §4):
every quantifier and causal/temporal connective in the report ("always", "therefore",
"after", "most") must be supported by a worker deliverable or a source — summaries
hallucinate in the binding words, not the nouns.

**Surface a completion card as each agent returns — not only at the end.** When a delegated
agent finishes, show the user a compact card: agent · model (effort) · tokens / tool-uses ·
one-line outcome · problems, if any · its Notable (beyond the ticket) item **if it produced
one** (`scout` is exempt — it reports `GAPS`, not Notable). Tokens/tool-uses come from the
Agent result's `usage` block for Claude sub-agents; a **cross-provider MCP worker
(Codex/Gemini) returns no usage**, and a **team-channel teammate likewise returns no
`usage` block to the lead** — mark both `n/a`, never invent a number. The rest is
free — the worker's report already carries it — and surfacing it per-return keeps the user
abreast of cost and findings as they happen, instead of only in the final scorecard. The
end-of-run scorecard (quality.md §5) still aggregates; the cards are the running commentary.

By this point every delegated ticket should already have its row in the routing
telemetry log — `telemetry/routing-log.jsonl` next to this file — because each row is
appended **when that ticket's final verdict lands**, via `tools/telemetry_append.py`
(v21; schema and recalibration thresholds: [references/quality.md](references/quality.md)
§7; records carry the delegation `channel` — `oneshot | workflow | team`). The tool is
the old v8 "self-check before appending" made executable — it rejects drifted field
names and non-canonical verdicts outright — and it is the session-budget fuse: each
append prints the run's cumulative token spend against `session_budget.tokens_max`, and
a breach exits non-zero, which means STOP DISPATCHING new tickets (quality.md §3;
overrides are named, never silent). Never append by hand: the vocabulary drifted three
times precisely through hand-written lines, and every drifted record silently drops out
of the gate-yield / escape-rate / proportionality-stop arithmetic that §3b and
recalibration depend on. Step 5's job is the completeness pass: confirm every delegated
ticket has its row (a missed one is appended now, late but honest), include the token /
tool-use figures from each Agent result's `usage` block, then append the run-summary
record. The log is what turns the Step 2 rubric from a priori doctrine into a
calibrated one; skipping it on "small" sessions is how the data never accumulates.

## Large fan-outs (≥ 5 similar items)

When the Workflow tool is available and the work is a uniform sweep (N files to migrate,
N modules to audit), prefer a Workflow pipeline over hand-spawning: pass these agents via
`agentType` (`'scout'`, `'builder'`, `'critic'`) and keep the find → verify shape:

```js
const results = await pipeline(items,
  (item) => agent(ticketFor(item), { agentType: 'builder', label: item }),
  (out, item) => agent(verifyTicket(out, item), { agentType: 'critic', label: `verify:${item}` }))
```

**Routing is mandatory inside Workflow scripts — on every single `agent()` call.**
An `agent()` call with neither `agentType` nor `model` inherits the session model —
you, the orchestrator: the most expensive tier, running without any worker's tuned
system prompt. The Workflow tool's own "default to omitting model" advice does NOT
apply under this skill — it silently defeats the routing matrix. Rule: every `agent()`
call carries `agentType: 'architect' | 'builder' | 'scout' | 'critic'` (preferred —
model and tuned prompt come together), or at minimum an explicit `model`. Before
launching a workflow, re-read the script and reject it if any `agent()` call lacks
both. Orchestrator-tier workers are never spawned; work that Step 2 routes to "you"
happens in the main loop.

Pick `agentType` with the Step 2 complexity rubric, not by habit: judgment-bearing
lenses (does this text / design / policy hold up?) are `critic` — Opus — even when
there are ten of them in the sweep; `builder` covers spec-implementations; `scout`
covers mechanical sweeps only.

The same quality gates apply: every pipeline stage's ticket carries acceptance criteria,
and `critic` verdicts gate what reaches the final report.

## Agent teams — experimental third channel (narrow niche)

When the agent-teams surface is enabled (detect with one Bash call:
`printenv CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS` non-empty; absent → the degradation
below, never a run-blocking error), a third delegation channel exists alongside one-shot
sub-agents and Workflow pipelines: persistent teammates with a shared task list and
named inter-agent messaging. It is experimental upstream and under a falsifiable pilot
here — full doctrine, protocols, spawn addendum, and the pre-committed keep/drop
criterion: [references/teams.md](references/teams.md).

**Exactly two triggers** (everything else stays one-shot / Workflow):
1. **Persistent build** — ≥ 3 dependent implementation waves on one subsystem where
   consecutive tickets would re-read substantially the same file set: context
   re-acquisition, not implementation, dominates the one-shot chain's cost. Team:
   1–2 `builder`-definition teammates.
2. **Competing hypotheses** — an unknown root cause with 2–3 plausible, distinguishable
   hypotheses rewarding adversarial cross-examination. Team: 2–3 `architect`-definition
   hypothesis-holders; the lead adjudicates.

**Hard rules (non-negotiable):** `critic` is always OUTSIDE the team — every verification
is a one-shot fresh-context sub-agent; task assignment is lead-only — an unassigned,
unblocked task must never exist in the shared list (that is all self-claiming can grab)
and every spawn prompt forbids claiming; worker↔worker messages only for the two named
patterns in teams.md (structured hypothesis debate; one-round interface handshake, cc the
lead) — everything else flows through you; `scout` never joins a team, and teammates never
spawn sub-agents or teammates.

Quality gates are unchanged and mapped per wave (acceptance criteria before dispatch,
deterministic pre-gate on the disk diff, outside critic, escalation ladder with
"retire the teammate, continue one-shot" as rung ②) — and the Step 4.7 whole-diff review
applies to every multi-wave team run, including single-writer ones: wave seams are writer
seams. Context hygiene: every wave ticket restates all constraints (teammate memory is
never a source of truth); reconcile the teammate's stated assumptions against the actual
diff before each wave; retire-and-respawn with a verified handoff summary after ~3 waves
or on drift signs. Choosing this channel always fires the Step 1 approval checkpoint.
Telemetry records carry `channel: "team"` plus `teammate` and `wave` (quality.md §7).
Degradation when absent: trigger 1 → a sequential one-shot `builder` chain with full
context restated per wave; trigger 2 → parallel one-shot `architect` investigations, lead
adjudicates.
