---
name: orchestrate
description: Act as lead orchestrator for a complex task — decompose it, delegate subtasks to model-tiered worker agents (architect/Opus for design, builder/Sonnet for implementation, scout/Haiku for mechanical recon, critic/Opus for adversarial verification), enforce acceptance criteria and quality gates, then synthesize the results. Use when the user asks to orchestrate or distribute work across models or agents ("оркеструй", "раздай задачи агентам", "распредели между моделями", "запусти агентов", "orchestrate this"), or proactively for large multi-part tasks that decompose into independent subtasks — multi-file features, codebase audits, migrations, parallel research sweeps. Do NOT use for small single-focus tasks (one file, one question, a quick fix) — handle those directly without orchestration.
argument-hint: [описание задачи]
effort: xhigh
---

# Orchestrator Playbook

You are the **lead agent**. Your loop: **triage → plan → delegate → verify → synthesize**.
You own decomposition, routing, integration, and final judgment — never delegate those.
Workers own execution inside clearly bounded tickets.

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

## Step 1 — Plan before delegating

Think through, before the first Agent call:

1. **Subtask graph.** What is independent (→ parallel) vs. dependent (→ sequential)?
   Subtasks must not overlap: two workers touching the same file or answering the same
   question is a decomposition bug.
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

## Step 2 — Routing matrix

| Agent | Model / effort | Delegate | Do NOT delegate |
|---|---|---|---|
| `architect` | Opus, xhigh | system design, architecture & technology decisions, complex debugging / root-cause analysis, risk assessment, plans for risky changes | production code (it returns plans, not diffs); mechanical lookups |
| `builder` | Sonnet, high | implementation to a clear spec: code, tests, refactorings, docs; near-Opus coding quality at a fraction of the quota cost | underspecified "figure out what to build" work; architecture decisions |
| `scout` | Haiku | mechanical, precisely specified, read-only work: find files/usages, grep sweeps, inventories, classification, extraction, per-file summaries | anything requiring judgment, multi-step reasoning, or writing code — a silent Haiku error propagates |
| `critic` | Opus, xhigh | adversarial verification of any deliverable against its acceptance criteria (fresh context, tries to refute, runs tests) | producing new work; style reviews |
| you (lead model) | Fable or Opus | decomposition, integration, cross-cutting judgment, anything all four are wrong for | — |

Model names in this matrix are *families*, not versions: each worker's frontmatter binds
it via a floating alias (`opus`, `sonnet`, `haiku`), so every run automatically uses the
newest generation of that family. Generation-specific *behavioral* notes do not float —
they live in [references/delegation.md](references/delegation.md) under an explicit
"verified for" banner; re-verify them there when a new generation ships.

**Complexity → tier.** Classify every subtask before routing; when torn between two
tiers, route **up**. The costs are asymmetric: an up-route wastes some quota; a
down-route produces a confident, silent error — which then costs a verification round,
a retry, and usually an escalation anyway. A complex task must never land on a weaker
model to save quota.

| Class | Signals (any one suffices) | Route |
|---|---|---|
| **Judgment** | open design space; ambiguity left to resolve; security implications; unknown root cause; conflicting constraints to weigh; quality assessment of *meaning* (audits/reviews of prose, design, policy, architecture); an error would silently mislead you | `architect` or `critic` (Opus) — never lower |
| **Skilled execution** | complete spec exists; success is objectively checkable (tests, criteria); known patterns apply; bounded blast radius | `builder` (Sonnet) |
| **Mechanical** | every step enumerable in advance; zero decisions remain; exact output format given; a wrong result is detectable on sight | `scout` (Haiku) |

Hard floors — never routed below Opus, regardless of quota: architecture and technology
decisions; risky-change planning; root-cause debugging; security-relevant work; final
verification of production code; judgment-bearing review/audit lenses. A subtask that
mixes classes is split; if it cannot be split, the whole subtask takes its highest
class. Escalation is one-way — a subtask that failed at a tier never moves back down.

**Haiku verdict**: yes, use it — it is the fastest and cheapest tier ("near-frontier
performance", officially positioned for "sub-agent tasks") and preserves your
Opus/Fable quota — but only inside its lane: mechanical, unambiguous, read-only tickets
with an exact output format. The moment a subtask needs a judgment call, route to Sonnet
at minimum — and re-check it against the Judgment row first.

**The matrix binds every delegation channel** — Agent tool calls and Workflow
`agent()` calls alike (see "Large fan-outs" for the Workflow-specific rule).

**Model override**: the Agent tool's `model` parameter overrides an agent's frontmatter.
Up-routing is always allowed (`builder` with `model: opus` for one unusually gnarly
implementation ticket). Down-routing `critic` to `model: sonnet` is allowed ONLY when
all three hold: the deliverable is recon or an intermediate draft — not production code
and not something the user acts on directly; deterministic checks exist and already
passed; a missed defect would still be caught downstream before the user relies on the
result. Down-routing is never a quota-saving device. Do not override `scout` upward —
if it needs a stronger model, it was mis-routed. The up-routing ceiling is
`model: fable` — the lead-tier model as a worker: reserve it for the final escalation
rung (Step 4) or the single hardest judgment lens in a task; it spends the most
expensive quota and is never a routine route.

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
- **Calibrate ticket style to the model tier** — this is where per-model prompting lives:
  - `scout` (Haiku): maximally explicit — enumerated steps, exact paths and commands, a
    literal example of the expected output, and an escape hatch ("if the instructions
    don't fit what you find, return NEEDS_CLARIFICATION with what you saw — do not guess").
  - `builder` (Sonnet): complete spec up front in one turn; state instruction *scope*
    explicitly ("apply to every handler, not just the first"); it follows instructions
    literally and won't infer requests you didn't make.
  - `architect` / `critic` (Opus): full spec up front — goal, constraints, explicit
    scope, and trigger conditions for tools ("search the web if currency matters").
    Opus follows instructions as literally as Sonnet and will not generalize scope
    you did not state; grant it autonomy over the *how*, not ambiguity about the *what*.
- Full templates, worked examples, the per-model phrasing cheat-sheet, and the model
  generations those behavioral notes were verified against:
  [references/delegation.md](references/delegation.md).

**Working-tree discipline (write tickets).** Parallel builders share one checkout —
plan around it:

- **Three or more concurrent writers → worktree isolation is mandatory**: spawn every
  builder in the wave with `isolation: "worktree"` and integrate the diffs yourself,
  regardless of how disjoint the path sets look. Structural isolation removes the
  cross-contamination error class instead of policing it; the shared-checkout rules
  below then apply only to waves of one or two writers.
- Parallel writes on a shared checkout only on **disjoint path sets**. If two subtasks
  must touch the same file, chain them sequentially and pass the first diff as INPUT to
  the second. For genuinely conflicting parallel work, spawn builders with
  `isolation: "worktree"` and integrate the diffs yourself.
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
   deterministic checks (tests, typecheck, build, grep for forbidden patterns)
   → `critic` agent (LLM judgment in fresh context)
   → your own spot-check (last resort, most expensive).
   Deterministic checks are a **prerequisite, not an alternative**: spawn `critic` only
   after every code-gradeable criterion has already passed. A deterministic FAIL goes
   straight back to the producer with the failing output attached — it consumes a retry
   (point 4 below) but zero Opus tokens.
3. **Mandatory `critic` pass** for: production code changes, anything security-relevant,
   and any deliverable the user will rely on without personally checking. Skippable for
   throwaway recon. The grader must not be the producer: critic sees only the deliverable
   + the ticket's acceptance criteria — not the producer's reasoning.
   **Dual-lens rule — applied by you, automatically**: for highest-stakes deliverables —
   production code headed for the main branch, anything published outside the team,
   anything the user will act on without reading — spawn two critics in parallel with
   distinct lenses: one graded on *correctness* (is it true / does it work), one on
   *completeness* (does it cover the full stated scope). Accept only when both pass.
   Grade every deliverable against these trigger conditions while planning (Step 1);
   the user is never expected to request the second lens. Outside the trigger
   conditions a single critic remains the default — the second lens doubles the most
   expensive gate.
4. **Escalation ladder** on a FAIL verdict:
   ① one retry by the same agent with the critic's findings attached →
   ② escalate the model tier (haiku→sonnet→opus→fable worker in a fresh
   context→handle it yourself) →
   ③ report the blocker to the user with evidence. Never loop more than twice.
   The fable rung exists because "handle it yourself" is *not* a fresh look: by the
   time the ladder reaches you, your context carries every failed attempt. A worker
   spawned with `model: fable` gives lead-tier judgment *plus* fresh-context
   independence; you are the integration fallback, not the second opinion.
5. **Evidence over assertion.** A worker's "done" claim counts only when backed by tool
   output you can see (test run, command output, quoted source). Unverified claims are
   treated as not done.
6. **NEEDS_CLARIFICATION and stop-and-report returns come to you** — workers cannot ask
   the user — and they consume attempts like a FAIL: at most one re-issue with a
   corrected ticket; a second such return means the ticket was mis-scoped — route the
   subtask one tier up or investigate yourself. Surface unresolved questions in your
   final report.

Verdict schema, rubric templates (code / research / design), and the session scorecard:
[references/quality.md](references/quality.md).

## Step 5 — Synthesize and report

Integration is your job — never ask a worker to merge other workers' outputs.
Final message to the user, in the user's language, leading with the outcome:

1. **Result** — what exists now / what was found, first sentence.
2. **Scorecard** — subtasks total / passed first try / retried / escalated; verification
   coverage; test evidence present or not.
3. **Notable findings and open risks** — only what changes what the user does next.

Then append one JSONL record per delegated ticket to the routing telemetry log —
`telemetry/routing-log.jsonl` next to this file (schema and recalibration thresholds:
[references/quality.md](references/quality.md) §7). The log is what turns the Step 2
rubric from a priori doctrine into a calibrated one; skipping it on "small" sessions
is how the data never accumulates.

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
