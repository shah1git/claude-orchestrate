# Delegation: ticket templates and per-model phrasing

Source of truth for how the orchestrator writes task tickets. Grounded in Anthropic's
published guidance: "How we built our multi-agent research system" (delegation anatomy,
effort scaling), "Prompting best practices" (explicitness, context/motivation, examples,
format control), and the per-model prompting pages for the generations named in the
"verified for" banner of the cheat-sheet below.

## The ticket, field by field

| Field | What good looks like | Typical failure without it |
|---|---|---|
| OBJECTIVE | One sentence, outcome-shaped: "A passing test suite for `src/pricing/` covering the discount edge cases listed below." | Worker optimizes for activity, not outcome |
| CONTEXT | Why the task matters, who consumes the result, how it fits the plan. Claude generalizes correctly from the *reason*: "the result feeds a security review, so completeness beats speed." | Worker makes locally reasonable, globally wrong tradeoffs |
| INPUTS | Exact paths, symbols, line ranges, URLs, and relevant findings from other workers, restated (workers never see your conversation). | Worker re-derives or, worse, guesses your context |
| OUTPUT | Exact deliverable format: structure, section names, table columns, language. If machine-consumed, show a literal example. | Unparseable or inconsistent deliverables |
| TOOLS | Which tools to use and the *trigger conditions*: "if the answer depends on current information, search the web before answering rather than answering from memory." | Under-use of tools (esp. Opus) or endless searching |
| BOUNDARIES | Out-of-scope list, files that must not change, stop conditions ("if the fix requires touching the schema, stop and report instead"). | Scope creep, duplicate work across workers, unrequested refactors |
| ACCEPTANCE | Gradeable criteria (see quality.md). These are handed verbatim to `critic`. | Nothing to verify against; "done" becomes a vibe |

Two workers must never share an OBJECTIVE or overlap on INPUTS-they-modify. Vague or
overlapping delegation is the #1 documented multi-agent failure (duplicated work, gaps).

**Seeding tickets from the clarification ledger.** When Step 0.5 ran a grill, its ledger is
the source for these fields: sharpened TERMS become the exact vocabulary in
OBJECTIVE/CONTEXT (so isolated workers share one meaning of "account"/"cancellation");
resolved FORKS become BOUNDARIES; the confirmed SUCCESS condition decomposes into per-ticket
ACCEPTANCE; a CODE contradiction found in the grill becomes an INPUT (the file:line). Copy a
canonicalized term verbatim — never paraphrase a term the grill already pinned.

**Pass references between dependent tickets, not transcripts.** When ticket B builds on
ticket A's output, B's INPUTS carry *references* — file paths, the diff on disk, a report
file — plus at most a three-line statement of what A established; never a paste of A's
full report. Every re-narration is a lossy re-compression by a different narrator (the
game-of-telephone failure; the omp case study's DAG solves it the same way — downstream
stages receive links, not transcripts). Disk artifacts also stay provenance-checkable
(quality.md research rubric) and remain authoritative when a narration drifts. Existing
rules are instances of this one: "pass the first diff as INPUT to the second" (SKILL.md
Step 3), per-ticket diff snapshots, Codex diff-from-disk.

**State the source-outranks-ticket rule in every ticket that names a source.** The four
Claude agents carry it in their standing definitions (`agents/*.md`): a document named in
INPUTS — a spec, an ADR, a documented contract — outranks the ticket, and a worker who
finds the two contradicting stops, builds neither side, and reports both statements with
their locations rather than picking a winner. **Cross-provider workers never see those
definitions** — a Codex, Kimi, Grok, or Gemini lane gets exactly the text you write — so
for those the rule has to travel *in the ticket*, one line in BOUNDARIES:

```
BOUNDARIES: ... The documents named in INPUTS outrank this ticket. If an instruction here
contradicts one of them — two statements that cannot both be true, not silence, not a
strained reading, not this ticket merely narrowing its source — stop work on the part it
affects, implement neither side, and report both statements with their locations. Deliver
whatever the conflict does not touch. (On a review ticket: report the conflict as a
finding rather than grading around it.)
```

**Which copy is canonical.** The *meaning* is maintained in `agents/*.md`; the BOUNDARIES
line is a payload carried to environments where those definitions do not exist. Edit the
agent definitions first and bring this line along in the same change — two copies exist
only because there is no shared surface, and they are worth keeping only in lockstep.

This closes the seam the lead's own hand introduces: a ticket criterion that contradicts
the spec it was cut from is a defect no acceptance criterion catches, because the criterion
*is* the defect, and the worker holding both texts is the cheapest reader who can see it
(2026-07-19: a ticket declared a telemetry field optional while the spec required it on
every record; the worker complied faithfully and the gate paid a review round for it).

**No hedge words in a ticket.** "probably / likely / apparently / should be / I think / may"
in a ticket marks unresolved recon, not a spec. Resolve each before dispatch — send a
`scout`, read the file, or make the decision yourself — or promote it to an explicit line
("Assumption: X holds; if it does not, stop and report"). A load-bearing hedge is a ticket
defect: the isolated worker cannot tell your uncertainty from a requirement.

## Per-model phrasing cheat-sheet

> **Verified for: Fable 5 (lead · architect · final escalation rung) ·
> Opus 4.8 · Sonnet 5 · Haiku 4.5** — checked 2026-07-08. The agents' frontmatter binds
> each worker via a floating alias (`fable`, `opus`, `sonnet`, `haiku`), which always
> resolves to the newest generation of its family — a
> new release is picked up with no edit anywhere. The *behavioral* notes below, however,
> were verified against the specific generations named above and do **not** float: when
> an alias starts resolving to a newer generation, re-verify each claim against
> Anthropic's model-specific prompting pages (platform.claude.com/docs) and update this
> banner. Haiku 4.5 has no dedicated prompting page — its section derives from the
> general best practices plus its official positioning ("near-frontier performance",
> "sub-agent tasks") in choosing-a-model.

### `scout` — Haiku

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
- **Keep it to eyes, not head.** Scout verbs are *find / list / measure / quote / count /
  cross-check / extract / classify-by-a-rule-you-give-it* — never *choose / decide /
  recommend / rank / conclude* where the pick needs judgment. Applying an explicit
  classification rule the ticket provides is mechanical (do it); a judgment choice among N
  goes back as "all N with their attributes" for the lead to decide. Encode the boundary in
  the ticket so it is explicit, not implied.
- **A completeness claim needs two angles.** For any inventory / usage-map / "find all
  X" ticket, the ticket itself must specify at least two *independent* sweep angles —
  e.g. grep the API caller AND grep the UI control; the import path AND the symbol
  name — plus a reconciliation step: items found by one angle but not the other are
  listed explicitly, never silently merged. Scout's report format already requires
  `SEARCHES RUN`; grade against it. A `GAPS: none` from a single-angle sweep is an
  unverified coverage claim, not evidence of completeness — the lead treats it as
  unverified before building a plan on it (2026-07-11 picker incident: 4 of 9 sites
  missed by a one-angle sweep the plan then trusted; §8, incident 4).
- **Write inventories as search-then-collect, not walk-and-count** (v23, config
  `routing.classes.mechanical.inventory_method`). Cheap tiers do not hold a running count
  in their head while walking files — Haiku scored 0.868 and Luna 0.921 on a 38-item sweep
  that required it. The same Haiku scored a perfect 1.0/1.0 on a **450-item** key when the
  items came out of one search and the work was transcribing its results. Size was never
  the variable; method was. So: name the patterns and paths in the ticket (scout's rule 1
  already binds it to them), and let the items fall out of the sweep. Routing up a tier is
  for inventories that genuinely cannot be written this way — **rewrite the ticket before
  you spend the tier**.
- Keep tickets small: one question or one sweep per ticket. Ten small scout tickets in
  parallel beat one broad one — **down to a floor**: every delegation carries fixed
  overhead (spawn, context injection, report), so below some brief size splitting costs
  more than it saves. Granularity has an optimum, not a monotone "smaller is better";
  the telemetry's per-class cost fields (quality.md §7) are how you find it.

### `builder` — Sonnet

Sonnet follows instructions **literally** and will not silently generalize an
instruction from one item to another, nor infer requests you didn't make:

- **State scope explicitly**: "apply this to *every* endpoint in `routes/`, not just the
  first one you edit."
- **Full spec in the first message.** Progressive drip-feeding of requirements measurably
  degrades token efficiency and results; the ticket must be complete.
- **When the ticket implements business logic against a spec, name the pre-agreed seams
  and prescribe `/tdd` on them.** Carry the seams straight from the spec — never let the
  builder pick its own. Say it explicitly: "Implement via the `tdd` skill (Skill tool) on
  these seams: <list>. Red before green, one seam at a time; typecheck regularly, run the
  full suite once at the end." Mechanical tickets (docs, config, pure refactors) don't
  need this — it's for new or changed behavior only.
- It is *more* agentic than earlier Sonnets — it will run self-verification loops and
  reach for tools readily. Channel that: "run the test suite yourself and include the
  output" is cheap to ask and high-value.
- Include the anti-overengineering and anti-reward-hacking lines from its agent
  definition only if you observe drift — they are already in its system prompt.

### `architect` — Fable

Fable is the lead-tier model: it performs best when given the **goal, not the steps** —
an over-scripted procedure actively hurts it. Write the architect ticket as a contract,
not a script:

- **State the goal, constraints, and explicit scope** ("assess all six modules listed
  below", not "the risky modules") and what "done" looks like — then stop: let it plan
  its own procedure. De-prescription is the point of routing design work to Fable.
- **Put trigger conditions on capabilities** where grounding or currency matters:
  "search the web if the answer depends on current information"; "never speculate about
  code you have not opened — read the file first."
- **Grant micro-autonomy** to prevent it pausing to ask: "for minor choices, pick a
  reasonable option and note it; ask only for scope changes."

### `critic` — Opus

Opus follows instructions **literally** — exactly like Sonnet, it does not generalize an
instruction from one item to another and does not infer requests you didn't make. Give it
a complete, explicitly-scoped spec, then autonomy over the *how*:

- **Full task spec up front.** State the deliverable, the acceptance criteria verbatim,
  and explicit scope; let it plan its own verification procedure.
- **Put trigger conditions on capabilities**: it tends to favor reasoning over tool
  calls, so say *when* to re-run tests, *when* to read files ("never speculate about code
  you have not opened — read the file first").
- For review-type work, demand **coverage first**: "report every issue you find,
  including uncertain and low-severity ones, with a confidence level and severity each —
  a separate step will filter." Conservative-reporting instructions silently suppress
  its (excellent) recall.
- **Scope classifies; adjudication filters.** The finding-scope axis (quality.md §3a)
  never licenses the critic to report less — keep the coverage demand above verbatim;
  the lead's adjudication (§3b) is the filter. Include the decision-context pack in the
  ticket's INPUTS for any target-repo code review.
- Fable's only appearance in the critic role is the **final escalation rung**: if `critic`
  still fails at Opus, the last ladder step (`routing.escalation.tiers`,
  `routing.overrides.fable_ceiling_uses`) respawns it with `model: fable` — not a standing
  dual-lens gate (that mechanism no longer exists, ADR-0002; the gate's non-Claude angle is
  the Standards lens, cross-provider.md Use 1). Phrase that one ticket Fable-style: goal, not
  steps, same as the architect section above.

## Worked examples

### Scout ticket

```
OBJECTIVE: Inventory every place the legacy LegacyHttpClient client is referenced in /opt/acme-shop.
CONTEXT: We are planning its replacement with the new typed API client; the architect needs an
  exact usage map to size the migration. Completeness matters more than speed.
INPUTS: Repository root /opt/acme-shop. Search terms: "LegacyHttpClient", "legacyHttpClient", "legacy_http".
TOOLS: Grep for the sweeps, Read for classification context. (Scout has no shell access.)
STEPS: 1) Grep each term case-insensitively across src/. 2) Second angle: Grep the
  import path "lib/legacy-http" across src/ — a file importing it that matched no term
  in step 1 is a hit too. 3) For each hit record file, line, and enclosing
  function/component name (Read the surrounding lines). 4) Classify each hit:
  api-call | type-import | comment | config. 5) Reconcile the angles: list every file
  found by only one of them.
OUTPUT: A markdown table: | file | line | symbol | class |. One row per hit, sorted by file.
  Example row: | src/lib/crm.ts | 42 | syncOrder() | api-call |
  After the table, one line per single-angle-only file (or "reconciliation: both angles agree").
BOUNDARIES: Read-only. Do not propose fixes. Do not search outside /opt/acme-shop.
ACCEPTANCE: Every term searched (show the search patterns run); both angles run and
  reconciled, single-angle-only files listed; zero-hit terms reported as zero; table
  parses; classifications only from the four allowed values.
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
  Pre-agreed seams (from the spec): the `POST /orders` handler and the
  `POST /orders/:id/cancel` handler — test each through its HTTP boundary, not the
  internal rate-limit counter.
TOOLS: Read the files named above before editing. Implement via the `tdd` skill (Skill
  tool) on the seams listed above — red before green, one seam at a time. Typecheck
  regularly as you go, after each seam (`npm run typecheck`); run the full suite once, at
  the end (`npm test`), and include the verbatim output.
OUTPUT: Edited code + a summary listing each file changed and why, plus the verbatim tail
  of the test run.
BOUNDARIES: Apply to BOTH endpoints (this instruction covers every endpoint listed, not
  just the first). Do not touch the schema, do not add config options beyond the plan, no
  refactoring outside the named files. If the plan conflicts with the code you find, stop
  and report the conflict instead of improvising. The documents named in INPUTS outrank
  this ticket: where an instruction here contradicts the plan or the spec's seams — two
  statements that cannot both be true, not silence and not a strained reading — stop on
  the affected part, implement neither side, report both statements with their locations,
  and deliver whatever the conflict does not touch.
ACCEPTANCE: `npm test` and `npm run typecheck` pass (output shown); both endpoints
  covered by a new test each; no files outside src/api/ and src/middleware/ modified;
  matches the plan's chosen algorithm.
```

### Critic ticket

```
OBJECTIVE: Verify the deliverable below against its acceptance criteria. Try to refute it.
INPUTS: <the deliverable — diff, report, or plan> + ACCEPTANCE: <criteria verbatim from
  the producing ticket> + decision-context pack (target-repo code only; quality.md §3a —
  assembled by scout, never by the producer).
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
- Mind the delegation floor cost: each worker pays fixed overhead before doing any work.
  Merging two tiny tickets into one bounded brief is often cheaper than spawning twice —
  split for independence and clarity, not for smallness itself.
- 3–5 concurrent workers is the healthy default; more only for uniform sweeps (then
  prefer a Workflow pipeline — see SKILL.md).
- Sequential only when output B genuinely consumes output A (design → build → verify).
- Do not have workers message each other or share files as a coordination channel; all
  coordination flows through you. (This binds the one-shot and Workflow channels. The
  experimental team channel sanctions exactly two worker↔worker patterns — structured
  hypothesis debate and a one-round interface handshake, cc the lead — defined in
  references/teams.md; everything else there still flows through the lead.)
