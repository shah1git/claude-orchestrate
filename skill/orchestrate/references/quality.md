# Quality gates: metrics, rubrics, verdicts

Source of truth for how delegated work is measured. Grounded in Anthropic's published
guidance: "Define success criteria and build evaluations" (gradeable criteria, grading
hierarchy, LLM-judge design), the multi-agent research system rubric, and the Claude Code
best-practices verification patterns (fresh-context refutation, evidence over assertion).

## 1. Acceptance criteria — the entry gate

Every ticket carries criteria that are **specific and measurable**. Test: could a
different model with no context grade the deliverable against them? If not, rewrite.

| Bad (vibes) | Good (gradeable) |
|---|---|
| "The code should work well" | "`npm test` exits 0; the two new endpoints each have a test; no file outside `src/api/` modified" |
| "A thorough report" | "Every one of the 6 listed modules has a section; each claim cites file:line; zero-finding modules explicitly say so" |
| "Safe output" | "grep for `dangerouslySetInnerHTML` over the diff returns 0 hits; user input reaches SQL only via the query builder" |

Multidimensional is normal: correctness + scope + evidence, each independently checkable.

**A check must be able to fail.** Design each criterion so a *broken* deliverable would
visibly fail it: mentally break the work and confirm the check catches it. A green check
that stays green on broken work is worse than no check — it manufactures false confidence.
Prefer a criterion with a concrete failing case ("`grep -c X` returns 0", "the test for the
edge case fails when the guard is removed") over one that can pass vacuously.

## 2. Grading hierarchy — choose the fastest reliable grader

1. **Code-graded** (exact match, exit codes, grep, build/test/typecheck) — fastest, most
   reliable; always prefer when the criterion allows it. Design criteria to be
   code-gradeable where possible. Code-graded criteria are a *prerequisite gate*:
   `critic` is spawned only after all of them have passed (SKILL.md Step 4) — a
   deterministic FAIL returns to the producer at zero LLM cost.
2. **LLM-graded** (`critic`) — for judgment criteria (does the plan address the risk?
   is the summary faithful to the source?). Reliable enough at scale *if* the rubric is
   explicit and the output is constrained (PASS/FAIL per criterion, not an essay).
3. **You** (orchestrator spot-check) — most expensive; reserve for integration seams and
   final synthesis.

Grader independence: the producer never grades its own work. `critic` runs in a fresh
context and receives only the deliverable + criteria — not the producer's reasoning —
so it evaluates the result on its own terms. (Anthropic's evals guidance recommends
grading with a *different* model than the producer; fresh context is what buys
independence when producer and grader share a model. Grading Sonnet/Haiku work with a
*stronger* model is this skill's own policy on top of that.)

**The producer's reasoning is excluded; the target repo's *decision record* is not.**
"Only the deliverable + criteria" was never meant to strip the grader of the repository's
documented decisions — ADRs, CONTEXT.md entries, decision comments adjacent to the touched
code. Those are facts about the codebase, authored before this deliverable existed;
withholding them buys re-litigation, not independence (observed 2026-07-12: a cross-model
lens re-opened a documented trade-off and a repo-wide pattern as major blockers on a
210-line feature, making the review rounds cost more than the feature). Every critic
ticket over target-repo code therefore carries a **decision-context pack** (§3a); the
producer's own reasoning, drafts, and self-reports stay excluded exactly as before.

## 3. Critic verdict schema

`critic` returns exactly this structure (it is in its system prompt; tickets may extend
but not weaken it):

```markdown
## VERDICT: PASS | FAIL | PASS_WITH_NOTES

### Criteria
| # | Criterion (verbatim) | PASS/FAIL | Evidence (command output / file:line / quote) |

### Findings (only on FAIL / PASS_WITH_NOTES; ranked by severity)
1. [severity: critical|major|minor] [confidence: high|medium|low]
   [scope: introduced|pre-existing|decision-challenge] — one-sentence defect;
   concrete failure scenario; file:line. (`scope` omitted reads as `introduced`.)

### Not checked
- criteria that could not be verified and why (missing tooling, ambiguity) — these count
  as NOT verified, never as passed.
```

Interpretation rules for the orchestrator:

- **PASS** — accept; record in scorecard.
- **PASS_WITH_NOTES** — accept; carry notes into the final report if user-relevant.
- **FAIL** — escalation ladder: ① one retry by the producer with the **adjudicated**
  findings attached (§3b — the lead triages findings before any retry) →
  ② escalate model tier → ③ do it yourself or report the blocker. **Two attempts max**
  per subtask; a third failure is a blocker report, not another retry.
- A criterion in "Not checked" blocks PASS for production-code deliverables.

### 3a. Finding scope and the decision-context pack

**Scope axis.** Every finding carries an optional `scope` (omitted = `introduced`):

| Scope | Means | Effect on the verdict |
|---|---|---|
| `introduced` | the defect is new in this diff — the deliverable created it or materially widened it | drives the verdict: a surviving critical/major blocks |
| `pre-existing` | the defect (or the pattern instantiated) exists on the base branch; the diff repeats an established repo recipe without widening its blast radius | never blocks the diff; the lead routes it to a target-repo issue |
| `decision-challenge` | the finding contradicts a **credentialed documented decision** (below): the critic believes the decision itself is wrong or has an unconsidered failure scenario | never blocks the diff; routed to the lead/owner as a decision-review item |

**Verdict mapping (tightens §3; the verdict *vocabulary* is unchanged):** FAIL is
warranted only by a failed acceptance criterion or a surviving `introduced`
critical/major finding. Minor-only findings, `pre-existing`, and `decision-challenge`
findings yield at most PASS_WITH_NOTES. This is what makes review rounds converge: a
fresh high-recall critic can always find *something*; only what this diff broke can
send the work back.

**The credential rule — what counts as a documented decision.** A decision credential is:
an accepted ADR; a CONTEXT.md entry; or an inline decision comment that (a) exists on the
base branch — not added by the diff under review — or (b) cites an authority outside the
diff (an ADR id, an issue, a named review finding, an owner decision with a date). A bare
"this is intentional" comment **introduced by the same diff with no outside citation is a
self-declaration, not a decision**: it earns no downgrade — the finding stays
`introduced`, and the critic flags the self-declaration itself as an additional finding
(a producer hiding a defect behind a freshly-invented "decision" is exactly the failure
mode this rule catches). The pack marks each decision reference `base | added-by-diff`,
graded against the per-ticket diff snapshot, so the check is mechanical, not a judgment
call.

**Documented ≠ correct — the anti-entrenchment rule.** The scope axis reclassifies
challenges; it never suppresses them. A critic that sees a concrete failure scenario a
documented decision does not consider MUST report it (as `decision-challenge`, naming the
decision's location and the unconsidered scenario). The lead MUST surface every such
finding: dismissed ones with a one-line pointer to the doc text that already answers the
scenario; substantive ones escalated to the owner as a decision-review item with a cost
estimate (what reversing the decision would take). A decision doc that turns out not to
answer the raised scenario gains a sentence — a follow-up doc ticket in the target repo,
gated like any change there. That, not an orchestrator-side ledger, is where
adjudications accumulate: the repo's own decision record converges to answering every
serious challenge once, and stays visible to human reviewers and other tools. No
review-ledger is kept — a second source of truth diverges from the first.

**The decision-context pack.** Every critic ticket over target-repo code carries a pack
assembled **mechanically** (a `scout` ticket — rule-based inclusion, read-only, no shell
needed; the per-ticket diff snapshot is an INPUT):

1. ADR index — one line per ADR in the repo's ADR directory: id, title, status;
2. CONTEXT.md term entries whose canonical term (or listed avoid-terms) appear in the diff;
3. inline decision markers in the touched files — comments matching decision-marker
   patterns (ADR/issue references, "owner decision" / «решение владельца», review-finding
   ids) within or adjacent to the touched symbols — each as `file:line + first sentence +
   provenance: base | added-by-diff`;
4. total budget: config `gates.review_loop.pack_max_lines` — references and one-sentence
   quotes, never transcripts (the v5 reference-passing principle).

The **producer never assembles or filters the pack** — that would hand it the channel to
soften its own review. The lead may *add* entries (a decision pinned in this run's grill,
a doc it knows is relevant), never remove them. An empty pack ("no ADR dir, no
CONTEXT.md, no markers found") is itself signal: with nothing documented, nothing
qualifies as a `decision-challenge`, and self-declared intent comments carry no weight.

### 3b. Round economics — convergence, not re-discovery

A **full round** is one wave over the deliverable — the fixed three-lens gate (SKILL.md
Step 4.3) running Standards, Spec, and critic in parallel counts as ONE round, not three.
The cap is a flat ceiling in config `gates.review_loop.full_rounds_max`: since v11 it no
longer varies by profile — verification depth does not scale by any axis (ADR-0002); the
v8 per-profile keying it replaced is retired along with the profile axis itself (slice 3,
issue #7). Any round beyond the first stays reserved, by lead judgment, for fixes
extensive enough to move integration seams.

**Adjudicate before any retry.** The lead merges findings across lenses (deduplicating),
assigns each an outcome, and only then re-issues:

- `introduced` critical/major, accepted → into the retry ticket (this consumes the
  escalation ladder's attempt, unchanged);
- `introduced`, disputed → the lead checks the evidence itself; a finding the lead
  refutes is recorded in `note`, not sent to the producer;
- `pre-existing` → a target-repo issue (or a final-report line when the repo has no
  tracker); blocks nothing — unless the diff materially widens the defect's reach,
  which makes it `introduced`;
- `decision-challenge` → the §3a stream: dismiss-with-pointer or escalate-to-owner;
  never silently into a retry ticket — a producer must not "fix" a documented decision
  on a critic's word alone;
- minors → PASS_WITH_NOTES material, carried to the final report; never a round.

**Delta re-verification, not a fresh full round.** After the producer applies accepted
fixes: re-run the deterministic checks, then verify — same critic definition, fresh
spawn, narrowed ticket — that each named finding is resolved and the fix hunks introduce
nothing new. The re-check reviews the fix hunks, not the whole deliverable; anything it
notices outside them is a note, never a verdict driver. It consumes no full round and no
ladder attempt; a delta re-check that finds a named finding NOT resolved is the retry
failing — the ladder proceeds (attempt economics unchanged, `max_attempts_per_subtask`
untouched). For a trivial fix (a few lines, deterministically covered) the lead's own
diff read suffices on a `standard` profile — the §2 hierarchy's spot-check rung, applied
deliberately.

**Proportionality stop.** When cumulative gate + fix spend crosses the configured ratio
to the producer's build spend (config `gates.review_loop.proportionality_stop`; token
figures from this run's §7 records), the loop stops: remaining findings go to
adjudication and the final report, not into another round. Overriding the stop or the
round caps is a named lead decision logged in telemetry, never silent. Grounding:
telemetry 2026-07-04..11 puts the critic's real-defect yield at ~20% of gates,
concentrated in security/auth/transactional work — rounds beyond the caps re-discover,
they do not converge; the 2026-07-12 a real project case (several "final reviews" costing
more than the 210-line feature) is the incident that pinned this.

## 4. Rubric templates by deliverable type

**Code change** (default criteria to put in ACCEPTANCE):
- deterministic: tests pass (output shown), typecheck/build pass, diff confined to the
  ticket's named paths (graded against the per-ticket diff snapshot — see SKILL.md
  "Working-tree discipline" — never against global git status during a parallel wave);
- deterministic, **UI-facing changes**: the primary user flow is exercised *live* — a
  scripted browser/DOM interaction, or a hand click-through when scripting is
  impractical — so the real event handlers fire before acceptance. tsc, build, and code
  review (all three lenses) do not substitute for runtime event-handler behavior: the
  2026-07-09 live-FileList reset passed every static gate and failed on the first real
  click (§8, incident 1);
- deterministic, **probes**: a negative-path probe must assert *who answered* — body
  shape or an identifying header — never the status code alone. A 404-expected check
  "passes" identically when routing is broken (§8, incident 2);
- judgment: implements the approved plan (not a workaround), no hard-coded values that
  only satisfy the tests, no unrequested refactoring, edge cases from the ticket covered.

**Research / recon** (Anthropic's research-system rubric, adapted):
- factual accuracy — claims match sources;
- citation accuracy — every claim carries file:line or URL, and the cited source
  actually says it;
- completeness — all requested aspects covered; empty results stated explicitly;
- source quality — primary sources (code, official docs) over hearsay;
- tool efficiency — sensible tool use, no fabricated-source chasing;
- provenance — the deliverable is demonstrably the worker's *own work on the ticket's
  INPUTS*: analysis claims carry evidence obtainable only by doing the work
  (input-traceable offsets/symbols, intermediate artifacts), and the grader asks "could
  this have been copied from an existing artifact?" A correct-looking result appropriated
  from a neighboring agent's output is a FAIL, not a PASS (§8, incident 6) — it poisons
  lane calibration and silently diverges the moment the input changes.
- connective accuracy — for synthesis/summary deliverables, each claim's *strength* and
  *causal/temporal shape* match the source, not just its nouns: quantifiers
  (all/most/none/every/always/never/N%) and causal-temporal connectives
  (therefore/because/so/when/after/leads to) are each supported by the cited source. An
  overstated quantifier or an invented causal link is a defect even when every cited noun is
  real — this is where summaries hallucinate. Apply it to the lead's own Step 5 report too.

**Design / plan**:
- addresses every requirement and constraint in the ticket;
- decisions carry a "why this, not that" for each non-obvious choice;
- risks named with mitigations; rollback path stated for risky changes;
- concrete enough to hand to `builder` without follow-up questions.

Judge mechanics (from Anthropic guidance): one grading call with the full rubric beats a
panel of per-criterion judges; force constrained output (PASS/FAIL + short evidence);
have the judge reason first, then emit the verdict; a reviewer told to find gaps will
always find some — hence the calibration line in critic's prompt ("only gaps that affect
correctness or the stated requirements; the rest are notes").

## 5. Session scorecard — worker ledger + analytics

The orchestrator's final report closes with two blocks, rendered in the user's language
(standing user directive, 2026-07-09: per-agent results, «красиво и информативно»).
The per-return completion cards (SKILL.md Step 5) are the running commentary; the ledger
is the end-of-run aggregation — both, not either.

**Block 1 — worker ledger.** One table row per delegated ticket, in dispatch order:

| # | Ticket | Agent | Model (effort) | Time | Tokens | Tools | Verdict |
|---|---|---|---|---|---|---|---|
| 1 | inventory | scout | Haiku | 1m 40s | 61 133 | 60 | PASS ✓ |
| 2 | S1-security | architect | Fable (xhigh) | 6m 12s | 99 517 | 37 | PASS ✓ — 1 miss, caught by cross-check |
| 3 | verify:xmodel | codex-critic | GPT-5.5 · OpenAI (xhigh) | 1m 51s | ~9k | n/a | APPROVED ✓ |

Data honesty: tokens / tool-uses come from the Agent result's `usage` block; time from
the harness's task notification (or the bridge's `durationMs` for cross-provider calls).
A surface that returns no figure gets `n/a` — never an invented number (the
completion-card rule). A retried ticket keeps ONE row: both attempts' cost summed, the
retry named in the verdict cell.

**Block 2 — session analytics.** The aggregate metrics:

| Metric | Value |
|---|---|
| Subtasks delegated (by agent) | e.g. 2 scout / 3 builder / 1 architect |
| Passed verification first try | n/m |
| Retried after FAIL | n (with one-line reasons) |
| Escalated (tier raised or taken over) | n |
| Verification coverage | verified deliverables / deliverables shipped into the result |
| Deterministic evidence present | yes/no per code deliverable |
| Token cost by tier | e.g. fable 480k (58%) / opus 210k (25%) / sonnet 95k (12%) / haiku 40k (5%) |
| Provider split | e.g. anthropic 82% / openai 12% / google 6% — the quota-spread mandate made visible |
| Parallelism gain | sum of worker durations vs. wall-clock, when ≥ 2 workers ran concurrently |

Close the block with one to three sentences of *interpretation*, not merely numbers —
only observations the ledger's own data supports (the §4 connective-tissue rule applies
to analytics prose too): which class burned the most quota and whether its tier earned
it; whether retries cluster on one agent type (a ticket-quality signal — §7 thresholds);
how much Claude quota the cross-provider share offloaded.

Standing targets: verification coverage = 100% for production code; first-try pass at or
above the config.yaml `thresholds.first_try_pass_min` floor (persistently lower means
tickets are underspecified — fix the tickets, not the workers); escalations are a routing
signal (recurring scout escalations = you are giving Haiku judgment work).

## 6. Honest failure protocol

Failures are reported, never smoothed over: a worker that skipped a step, a test that
still fails, a criterion that could not be checked — all appear in the final report as
what they are. If the overall task did not reach "done", the report says so in the first
sentence and lists what remains, with evidence.

## 7. Routing telemetry — calibrating the Step 2 rubric

The complexity→tier rubric ships a priori; this log makes it empirical. At the end of
every orchestrated session (SKILL.md Step 5) the lead appends one record per
**delegated** ticket to `telemetry/routing-log.jsonl` in the skill directory. The file
is data, not doctrine — read it for recalibration, never load it into a ticket.

One JSON object per line:

```json
{"date": "2026-07-15", "task": "config v10 shape axis", "ticket": "config-v10",
 "class": "judgment", "agent": "critic", "model": "opus", "shape": "разбирательство",
 "first_try": true, "retries": 0, "escalated_to": null, "verdict": "PASS", "note": "",
 "config": "v10+a1b2c3d", "tokens": 54154, "tool_uses": 8}
```

Field rules: `class` ∈ `judgment | skilled | mechanical` (the Step 2 classes); `agent`
is the agent type actually used (`architect | builder | scout | critic`), with `model`
recording the effective model (differs from the frontmatter alias family only on an
explicit override); `shape` ∈ `сборка | разбирательство` (config.yaml `shape`, ADR-0002,
spec #4) — the run-level fork the lead assigned at Step 0 triage, the same value on every
delegated-ticket record of that run; `retries` counts ticket re-issues after any failure —
deterministic pre-gate FAILs, critic FAILs, and NEEDS_CLARIFICATION re-issues alike;
`first_try` is simply `retries == 0`; `escalated_to` names the tier that ultimately
passed, `null` if none; `verdict` is the final verdict after all attempts; `note` is one
short clause, filled only when it explains a FAIL, retry, or escalation.

**Canonical `verdict` vocabulary** (pinned 2026-07-11, after the free-text field drifted
into 27 spellings in one week and the first analysis pass had to normalize them by
script). Exactly one of seven values:

| Value | Means |
|---|---|
| `PASS` | accepted as delivered |
| `PASS_WITH_NOTES` | accepted; minor notes carried forward |
| `FAIL_FIXED` | a gate failed (deterministic, critic, or cross-model lens); findings fixed and re-verified within the run — the gate *working* |
| `FAIL` | final failure: blocker reported, or deliverable unusable/incomplete |
| `PASS_THEN_FAIL` | passed every gate, defect surfaced after acceptance — an **escaped defect**; when process-caused it pairs with an incident-journal entry (§8) |
| `MISROUTE` | a lead routing error voided the ticket |
| `SKIPPED` | never ran (surface absent, ticket obsoleted) |

Severity counts, round history, and lens details go in `note` — never into new verdict
spellings. The vocabulary exists to make two things countable across sessions: the
gate-yield (`FAIL_FIXED` / all gated) and the escape rate (`PASS_THEN_FAIL` +
process-caused incidents / all accepted). Legacy note: records written before 2026-07-11
used two field generations (`ts`/`subagent`/`outcome`/`notes`/`routed_to`); all were
migrated to the canonical names on 2026-07-11, with each original verdict's payload
preserved as a `[was: …]` suffix in `note`. A second repair pass (2026-07-12) normalized
week-one drift written *after* the pinning — field aliases (`ts`/`subtask`/`role`/`run`/
`tools`/`wall_s`/`reason`), 18 non-canonical verdict spellings, missing `provider` on
cross-provider records — original verdicts again preserved as `[was: …]`; hence the
SKILL.md Step 5 self-check-before-append rule.

Optional cost fields — `tokens`, `tool_uses`, `duration_ms` — may be appended to any
record. They are free: the harness reports them in each Agent result's `usage` block
(`subagent_tokens` / `tool_uses` / `duration_ms`), so the lead already has them at
synthesis time. Recording them lets the log calibrate *cost per class* — is a class
routinely burning more than its tier is worth? — not just correctness; the two together
are what make the Step 2 rubric empirical.

Review-loop fields (optional, v8) — `rounds` (integer: full critic rounds consumed, §3b;
delta re-verifications excluded) may be appended to gated records. Finding-scope counts
and adjudication outcomes stay in `note` as one clause (e.g. `note: "5 findings: 2
introduced fixed, 1 decision-challenge dismissed (doc answers it), 2 minor→notes"`) —
never new verdict spellings; the seven-value vocabulary is unchanged.

Provider fields (optional) — `provider` ∈ `anthropic | openai | google` (default
`anthropic` when omitted) records which provider actually ran the ticket; and when
`provider != anthropic`, `xprovider_reason` ∈ `independent-lens | context-size |
quota-spread | lead-judgment` records why the cross-provider route was chosen (SKILL.md
Step 2, references/cross-provider.md; `user-codex-pref` appears only in pre-mandate
records and is retired). Every existing record omits these and is read as `anthropic`
— backward-compatible.

Fallback fields (optional) — when an availability reroute fired (SKILL.md Step 2,
"Availability fallbacks"), the record carries `fallback_from` (the agent/lane originally
routed) and `fallback_reason` ∈ `quota-window | subscription | connector-absent |
connector-error` (the config.yaml `availability.reasons` vocabulary); `agent`/`model`
record what actually ran. An availability reroute consumes no `retries` and does not
touch `escalated_to` — those measure quality, this measures quota pressure.

Config field — every record carries `config: "v<version>+<hash7>"`, the fingerprint of
the skill's config.yaml at dispatch time: its `version:` value plus the first 7 hex
characters of the file's sha256 (the SKILL.md "Config" section gives the one-liner). On
first sight of a new fingerprint the lead copies the file to
`telemetry/config-snapshots/<fingerprint>.yaml`, so every record resolves to the exact
config bytes that routed it — even outside git history. Records before 2026-07-11
predate the config and legitimately omit the field. This is what lets recalibration
(below) separate "the rubric misroutes" from "the rubric changed mid-sample".

Shape field — every record carries `shape` ∈ `сборка | разбирательство` (config.yaml
`shape`; ADR-0002, spec #4): the fork the lead assigned this run at Step 0 triage, stamped
on every delegated-ticket record the run produces — the same provenance pattern as
`config`, and read alongside it (a `shape` value is only meaningful together with the
`config` fingerprint that defined it). Records before 2026-07-15 predate the axis (v9 and
earlier scaled gate depth by the now-retired profile axis instead) and legitimately omit
the field.

Channel field (optional) — `channel` ∈ `oneshot | workflow | team` (default `oneshot`
when omitted; every existing record predates the field and is read as `oneshot` —
backward-compatible) records which delegation channel ran the ticket. For
`channel: "team"` records add `teammate` (the teammate's name) and `wave` (1-based
integer): one record per wave- or hypothesis-ticket, not per teammate lifetime. Teammates
return no `usage` block to the lead — omit `tokens` rather than inventing it; a
lead-measured `duration_ms` (wall-clock) is fine. Team-channel records feed the pilot's
pre-committed keep/drop criterion (references/teams.md).

Recalibration — the numeric thresholds live in config.yaml `thresholds` (at pin time:
first-try floor 0.80, review after 20 records per tier, critic-FAIL-rate floor 0.05) and
are revised *there* as data accumulates. Review whenever any tier crosses the
record-count threshold, and at every version bump at the latest:

- `scout` first-try pass below the floor → its lane is too wide: tighten the mechanical
  class's signals (config.yaml `routing.classes.mechanical`), or stop routing
  classification-flavored sweeps to Haiku.
- `builder` first-try pass below the floor → either tickets are underspecified (fix the
  ticket template usage) or the skilled-execution signals admit judgment work — read the
  failing records' notes before deciding which.
- Recurring `escalated_to` out of any tier → the class boundary above it is drawn
  wrong; move the recurring signal one class up in config.yaml `routing.classes`.
- `critic` FAIL rate on first-try deliverables persistently below its floor → the gate
  may be over-applied to low-risk deliverables; revisit what "mandatory critic pass"
  covers — but never weaken it for production code.

## 8. Incident journal — qualitative self-improvement

The routing log (§7) is quantitative — it tells you *that* a class is mis-routed. The
incident journal is its qualitative complement — it captures *why* a run went wrong so the
playbook can be fixed, not just the symptom.

Scope: this journal is for orchestration-**process** failures — a playbook rule let a
mistake through: a misread ticket, a lost or empty worker report, a spec/reality divergence,
a silently-wrong result that a gate missed, a worker that died mid-task. A **transient or
external** failure — a flaky API, a network blip, a provider timeout, a connector quirk — is
not a playbook defect and does not belong here; note it and move on. When a process mishap
occurs, record one line immediately: what happened, its class, what it cost. Keep these in
the session report and, for a durable trail, a running `telemetry/incidents.md` (optional,
created lazily on the first incident).

The rule: **2+ incidents of the same class is a *review signal*, not an automatic rewrite.**
It means stop and judge whether a real playbook flaw is behind them — and if so, fix that
rule (issue-style: the rule that let it happen, not the one instance). Let an observation
settle across a couple of sightings before promoting it into the skill text — one-off noise
is not a pattern, and you never rewrite the skill to route around a transient/external issue.
This is how the playbook learns from its own *process* failures rather than repeating them.
