# Delegation: ticket templates and per-model phrasing

Source of truth for how the orchestrator writes task tickets. Grounded in Anthropic's
published guidance: "How we built our multi-agent research system" (delegation anatomy,
effort scaling), "Prompting best practices" (explicitness, context/motivation, examples,
format control), and the per-model prompting pages for Opus 4.8 / Sonnet 5. Haiku 4.5
has no dedicated prompting page — its section derives from the general best practices
plus its official positioning ("near-frontier performance", "sub-agent tasks") in
choosing-a-model.

## The ticket, field by field

| Field | What good looks like | Typical failure without it |
|---|---|---|
| OBJECTIVE | One sentence, outcome-shaped: "A passing test suite for `src/pricing/` covering the discount edge cases listed below." | Worker optimizes for activity, not outcome |
| CONTEXT | Why the task matters, who consumes the result, how it fits the plan. Claude generalizes correctly from the *reason*: "the result feeds a security review, so completeness beats speed." | Worker makes locally reasonable, globally wrong tradeoffs |
| INPUTS | Exact paths, symbols, line ranges, URLs, and relevant findings from other workers, restated (workers never see your conversation). | Worker re-derives or, worse, guesses your context |
| OUTPUT | Exact deliverable format: structure, section names, table columns, language. If machine-consumed, show a literal example. | Unparseable or inconsistent deliverables |
| TOOLS | Which tools to use and the *trigger conditions*: "if the answer depends on current information, search the web before answering rather than answering from memory." | Under-use of tools (esp. Opus 4.8) or endless searching |
| BOUNDARIES | Out-of-scope list, files that must not change, stop conditions ("if the fix requires touching the schema, stop and report instead"). | Scope creep, duplicate work across workers, unrequested refactors |
| ACCEPTANCE | Gradeable criteria (see quality.md). These are handed verbatim to `critic`. | Nothing to verify against; "done" becomes a vibe |

Two workers must never share an OBJECTIVE or overlap on INPUTS-they-modify. Vague or
overlapping delegation is the #1 documented multi-agent failure (duplicated work, gaps).

## Per-model phrasing cheat-sheet

### `scout` — Haiku 4.5

Haiku is fast and cheap but does not fill gaps in your instructions. Write the ticket as
if for a capable executor with zero tolerance for ambiguity:

- **Enumerate the steps** (1, 2, 3…) when order or completeness matters.
- **Give literal search patterns and paths**: "Grep for `LegacyHttpClient` under `src/`,
  include only `*.ts`", not "find CRM references".
- **Show one literal example of the expected output row/entry** — examples are the most
  reliable way to steer format.
- **Install the escape hatch, verbatim**: "If the instructions do not match what you find,
  or the task requires a judgment call, stop and return `NEEDS_CLARIFICATION:` followed
  by what you actually saw. Do not guess. An honest empty result ('found 0 matches') is a
  success, not a failure."
- Keep tickets small: one question or one sweep per ticket. Ten small scout tickets in
  parallel beat one broad one.

### `builder` — Sonnet 5

Sonnet 5 follows instructions **literally** and will not silently generalize an
instruction from one item to another, nor infer requests you didn't make:

- **State scope explicitly**: "apply this to *every* endpoint in `routes/`, not just the
  first one you edit."
- **Full spec in the first message.** Progressive drip-feeding of requirements measurably
  degrades token efficiency and results; the ticket must be complete.
- It is *more* agentic than earlier Sonnets — it will run self-verification loops and
  reach for tools readily. Channel that: "run the test suite yourself and include the
  output" is cheap to ask and high-value.
- Include the anti-overengineering and anti-reward-hacking lines from its agent
  definition only if you observe drift — they are already in its system prompt.

### `architect` / `critic` — Opus 4.8

Opus 4.8 follows instructions **literally** — exactly like Sonnet 5, it does not
generalize an instruction from one item to another and does not infer requests you
didn't make. Give it a complete, explicitly-scoped spec, then autonomy over the *how*:

- **Full task spec up front, then autonomy.** State goal, constraints, explicit scope
  ("assess all six modules listed below", not "the risky modules"), and what "done"
  looks like; let it plan its own procedure.
  (The "state the goal, not the steps" de-prescription advice applies to Fable 5 — the
  orchestrator itself — not to Opus 4.8 tickets.)
- **Put trigger conditions on capabilities**: it tends to favor reasoning over tool
  calls, so say *when* to search, *when* to read files ("never speculate about code you
  have not opened — read the file first").
- **Grant micro-autonomy** to prevent it pausing to ask: "for minor choices, pick a
  reasonable option and note it; ask only for scope changes."
- For review-type work, demand **coverage first**: "report every issue you find,
  including uncertain and low-severity ones, with a confidence level and severity each —
  a separate step will filter." Conservative-reporting instructions silently suppress
  its (excellent) recall.

## Worked examples

### Scout ticket

```
OBJECTIVE: Inventory every place the legacy LegacyHttpClient client is referenced in /opt/acme-shop.
CONTEXT: We are planning its replacement with the new typed API client; the architect needs an
  exact usage map to size the migration. Completeness matters more than speed.
INPUTS: Repository root /opt/acme-shop. Search terms: "LegacyHttpClient", "legacyHttpClient", "legacy_http".
TOOLS: Grep for the sweeps, Read for classification context. (Scout has no shell access.)
STEPS: 1) Grep each term case-insensitively across src/. 2) For each hit record file,
  line, and enclosing function/component name (Read the surrounding lines). 3) Classify
  each hit: api-call | type-import | comment | config.
OUTPUT: A markdown table: | file | line | symbol | class |. One row per hit, sorted by file.
  Example row: | src/lib/crm.ts | 42 | syncOrder() | api-call |
BOUNDARIES: Read-only. Do not propose fixes. Do not search outside /opt/acme-shop.
ACCEPTANCE: Every term searched (show the search patterns run); zero-hit terms reported
  as zero; table parses; classifications only from the four allowed values.
If the repo layout does not match these instructions, return NEEDS_CLARIFICATION with what
you actually saw. Do not guess. "0 matches" is a valid, successful answer.
```

### Builder ticket

```
OBJECTIVE: Implement rate limiting on the two public POST endpoints in src/api/routes.ts
  per the approved plan below.
CONTEXT: Part of the hardening pass the user requested; architect's plan is authoritative.
  This will be adversarially verified against the acceptance criteria, so verify before
  reporting.
INPUTS: Plan: <paste architect's relevant section verbatim>. Files: src/api/routes.ts,
  src/middleware/. Existing middleware pattern to imitate: src/middleware/auth.ts.
TOOLS: Read the files named above before editing. Run `npm test` and `npm run typecheck`
  yourself before finishing and include the output.
OUTPUT: Edited code + a summary listing each file changed and why, plus the verbatim tail
  of the test run.
BOUNDARIES: Apply to BOTH endpoints (this instruction covers every endpoint listed, not
  just the first). Do not touch the schema, do not add config options beyond the plan, no
  refactoring outside the named files. If the plan conflicts with the code you find, stop
  and report the conflict instead of improvising.
ACCEPTANCE: `npm test` and `npm run typecheck` pass (output shown); both endpoints
  covered by a new test each; no files outside src/api/ and src/middleware/ modified;
  matches the plan's chosen algorithm.
```

### Critic ticket

```
OBJECTIVE: Verify the deliverable below against its acceptance criteria. Try to refute it.
INPUTS: <the deliverable — diff, report, or plan> + ACCEPTANCE: <criteria verbatim from
  the producing ticket>.
TOOLS: Re-run the deterministic checks yourself (tests, typecheck); read the touched
  files; do not trust the producer's claimed outputs.
OUTPUT: The verdict schema from quality.md — per-criterion PASS/FAIL with evidence,
  findings ranked by severity with confidence, overall verdict.
BOUNDARIES: Judge only against the stated criteria and correctness. Report gaps, not
  style preferences. Flag only issues that affect correctness or the stated requirements;
  everything else is a note, not a finding.
```

## Parallelization rules

- Independent tickets → **one message, multiple Agent calls** (they run concurrently).
- 3–5 concurrent workers is the healthy default; more only for uniform sweeps (then
  prefer a Workflow pipeline — see SKILL.md).
- Sequential only when output B genuinely consumes output A (design → build → verify).
- Do not have workers message each other or share files as a coordination channel; all
  coordination flows through you.
