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

The tier you land on also sets how hard to clarify the request before decomposing
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
  [references/clarify.md](references/clarify.md)).

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

**Approval checkpoint — hold before the first delegation wave.** For high-stakes or
expensive runs, present the plan and get explicit user approval before the first `Agent`
call. Lead-only, main loop.

Fire when the run is both *expensive* and *consequential* — any of:
- tier is **Complex multi-part** with a real fan-out (≥ 3 workers / the worktree-isolation
  threshold, Step 3); OR
- the deliverable meets a **dual-lens highest-stakes trigger** — production code headed for
  main, published outside the team, or acted on without reading (Step 4.3); OR
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

| Class | Signals (any one suffices) | Route |
|---|---|---|
| **Judgment** | open design space; ambiguity left to resolve; security implications; unknown root cause; conflicting constraints to weigh; quality assessment of *meaning* (audits/reviews of prose, design, policy, architecture); an error would silently mislead you | `architect` (Fable) or `critic` (Opus) — never lower |
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
`model: fable` — the lead-tier model as a worker. It has exactly three sanctioned uses:
`architect` (bound to it by design), the *correctness* lens of a highest-stakes dual-lens
(point 3, Step 4), and the final escalation rung (Step 4). Beyond those three it is never
a routine route — it spends the most expensive quota, and a `builder`/`scout` ticket never
takes it at *routing* time (the Step 4 escalation ladder's fable rung stays open to any
ticket that has already failed its way up the tiers).

**Provider dimension — optional overlay (Claude is the default).** The class→tier matrix
above always fixes a *Claude* worker; provider is an orthogonal, opt-in choice made only
after the class is set. Route to a non-Claude worker (OpenAI Codex, Google Gemini via
Antigravity) ONLY for one of three named reasons — independent-lens verification; recon
whose context exceeds a Claude window; or an explicit user preference for the Codex coding
hand (opt-in — Claude `builder` stays the default coder) — AND only when its connector is
detected present; otherwise the Claude default stands. Cross-provider is never a silent
default, and a non-Claude worker is an MCP call by the lead, not a sub-agent. Full
mechanics, the connector registry, and the detect-or-degrade rule:
[references/cross-provider.md](references/cross-provider.md).

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

- **Three or more concurrent writers → worktree isolation is mandatory**: spawn every
  builder in the wave with `isolation: "worktree"` and integrate the diffs yourself,
  regardless of how disjoint the path sets look. Structural isolation removes the
  cross-contamination error class instead of policing it; the shared-checkout rules
  below then apply only to waves of one or two writers.
- Parallel writes on a shared checkout only on **disjoint path sets**. If two subtasks
  must touch the same file, chain them sequentially and pass the first diff as INPUT to
  the second. For genuinely conflicting parallel work, spawn builders with
  `isolation: "worktree"` and integrate the diffs yourself.
- A **Codex coding-hand** (references/cross-provider.md) is a writer like any builder: give
  it `sandbox: workspace-write` and `cwd` set to a dedicated worktree, never
  `danger-full-access`. Capture its diff from disk (`git diff`), not its self-report, and
  gate it exactly like a Claude builder's diff — deterministic pre-checks then a *non-Codex*
  critic (producer ≠ grader).
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
   For these highest-stakes deliverables the *correctness* lens is spawned as `critic`
   with `model: fable` — frontier judgment on the lens where a miss ships a defect; the
   *completeness* lens stays on Opus. Two lenses, two models: their failure modes
   decorrelate at the most expensive gate. (Phrase the Fable lens's ticket Fable-style —
   goal, not steps; see delegation.md.)
   Grade every deliverable against these trigger conditions while planning (Step 1);
   the user is never expected to request the second lens. Outside the trigger
   conditions a single critic remains the default — the second lens doubles the most
   expensive gate.

   **Optional cross-model third lens (requires a connector — references/cross-provider.md).**
   For a dual-lens-trigger deliverable, when architectural independence is worth the latency
   — security-relevant code (its flagship documented use: a different-provider reviewer
   whose biases don't overlap), or on user request — add one cross-model critic (Codex or
   Gemini) as a *third* lens on the same deliverable + criteria, read-only. It is additive:
   accept only when **both Claude lenses PASS and the cross-model lens raises no surviving
   critical/major finding**. A cross-model lens can only tighten the gate, never replace a
   Claude lens; its cost is the other subscription's quota plus one round-trip, not Claude
   quota. If no connector is present, the standard two-Claude-lens gate applies unchanged.
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
7. **Whole-diff final review after a multi-writer wave.** Per-ticket critics verify each
   deliverable in isolation; integration-seam defects — two individually-correct diffs that
   conflict, a shared contract one ticket broke for another, a duplicated or double-applied
   change — slip through per-ticket gates. After a multi-builder wave settles and every
   per-ticket gate has passed, run one `critic` over the ENTIRE integrated diff with a
   cross-cutting lens ("do these changes cohere; does any pair conflict; did a shared
   assumption break") before the Step 5 synthesis. This is *additive* to the per-ticket
   gates, not a replacement — and if the integrated result is itself highest-stakes (point 3's
   triggers), it takes the **dual-lens** treatment, not a single critic. For a very large
   integrated diff, focus the review on the **seams** — the interfaces between tickets — or
   partition by subsystem, rather than pushing the whole blob through one critic's context.
   Skip for single-writer changes. For highest-stakes waves an independent cross-model lens
   (references/cross-provider.md) adds the most.

Verdict schema, rubric templates (code / research / design), and the session scorecard:
[references/quality.md](references/quality.md).

## Step 5 — Synthesize and report

Integration is your job — never ask a worker to merge other workers' outputs.
Final message to the user, in the user's language, leading with the outcome:

1. **Result** — what exists now / what was found, first sentence.
2. **Scorecard** — subtasks total / passed first try / retried / escalated; verification
   coverage; test evidence present or not.
3. **Notable findings and open risks** — only what changes what the user does next.
   Fold in here each worker's **Notable (beyond the ticket)** items — out-of-scope
   discoveries, latent bugs, or better approaches they surfaced — keeping only what
   changes what the user does next.

Before shipping, run the connective-tissue self-check on your own synthesis (quality.md §4):
every quantifier and causal/temporal connective in the report ("always", "therefore",
"after", "most") must be supported by a worker deliverable or a source — summaries
hallucinate in the binding words, not the nouns.

**Surface a completion card as each agent returns — not only at the end.** When a delegated
agent finishes, show the user a compact card: agent · model (effort) · tokens / tool-uses ·
one-line outcome · problems, if any · its Notable (beyond the ticket) item **if it produced
one** (`scout` is exempt — it reports `GAPS`, not Notable). Tokens/tool-uses come from the
Agent result's `usage` block for Claude sub-agents; a **cross-provider MCP worker
(Codex/Gemini) returns no usage** — mark those `n/a`, never invent a number. The rest is
free — the worker's report already carries it — and surfacing it per-return keeps the user
abreast of cost and findings as they happen, instead of only in the final scorecard. The
end-of-run scorecard (quality.md §5) still aggregates; the cards are the running commentary.

Then append one JSONL record per delegated ticket to the routing telemetry log —
`telemetry/routing-log.jsonl` next to this file (schema and recalibration thresholds:
[references/quality.md](references/quality.md) §7). The log is what turns the Step 2
rubric from a priori doctrine into a calibrated one; skipping it on "small" sessions
is how the data never accumulates. Include the token / tool-use figures from each Agent
result's `usage` block in the record (quality.md §7).

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
