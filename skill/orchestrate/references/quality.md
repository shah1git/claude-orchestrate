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

## 3. Critic verdict schema

`critic` returns exactly this structure (it is in its system prompt; tickets may extend
but not weaken it):

```markdown
## VERDICT: PASS | FAIL | PASS_WITH_NOTES

### Criteria
| # | Criterion (verbatim) | PASS/FAIL | Evidence (command output / file:line / quote) |

### Findings (only on FAIL / PASS_WITH_NOTES; ranked by severity)
1. [severity: critical|major|minor] [confidence: high|medium|low] — one-sentence defect;
   concrete failure scenario; file:line.

### Not checked
- criteria that could not be verified and why (missing tooling, ambiguity) — these count
  as NOT verified, never as passed.
```

Interpretation rules for the orchestrator:

- **PASS** — accept; record in scorecard.
- **PASS_WITH_NOTES** — accept; carry notes into the final report if user-relevant.
- **FAIL** — escalation ladder: ① one retry by the producer with the findings attached →
  ② escalate model tier → ③ do it yourself or report the blocker. **Two attempts max**
  per subtask; a third failure is a blocker report, not another retry.
- A criterion in "Not checked" blocks PASS for production-code deliverables.

## 4. Rubric templates by deliverable type

**Code change** (default criteria to put in ACCEPTANCE):
- deterministic: tests pass (output shown), typecheck/build pass, diff confined to the
  ticket's named paths (graded against the per-ticket diff snapshot — see SKILL.md
  "Working-tree discipline" — never against global git status during a parallel wave);
- judgment: implements the approved plan (not a workaround), no hard-coded values that
  only satisfy the tests, no unrequested refactoring, edge cases from the ticket covered.

**Research / recon** (Anthropic's research-system rubric, adapted):
- factual accuracy — claims match sources;
- citation accuracy — every claim carries file:line or URL, and the cited source
  actually says it;
- completeness — all requested aspects covered; empty results stated explicitly;
- source quality — primary sources (code, official docs) over hearsay;
- tool efficiency — sensible tool use, no fabricated-source chasing.
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

## 5. Session scorecard

The orchestrator's final report includes:

| Metric | Value |
|---|---|
| Subtasks delegated (by agent) | e.g. 2 scout / 3 builder / 1 architect |
| Passed verification first try | n/m |
| Retried after FAIL | n (with one-line reasons) |
| Escalated (tier raised or taken over) | n |
| Verification coverage | verified deliverables / deliverables shipped into the result |
| Deterministic evidence present | yes/no per code deliverable |
| Token cost (by agent) | e.g. architect 82k / builder 62k / critic 54k — from each Agent result's `usage` block |

Standing targets: verification coverage = 100% for production code; first-try pass ≥ 80%
(persistently lower means tickets are underspecified — fix the tickets, not the workers);
escalations are a routing signal (recurring scout escalations = you are giving Haiku
judgment work).

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
{"date": "2026-07-04", "task": "de-version model labels", "ticket": "verify:v1.5",
 "class": "judgment", "agent": "critic", "model": "opus",
 "first_try": true, "retries": 0, "escalated_to": null, "verdict": "PASS", "note": "", "tokens": 54154, "tool_uses": 8}
```

Field rules: `class` ∈ `judgment | skilled | mechanical` (the Step 2 classes); `agent`
is the agent type actually used (`architect | builder | scout | critic`), with `model`
recording the effective model (differs from the frontmatter alias family only on an
explicit override); `retries` counts ticket re-issues after any failure — deterministic
pre-gate FAILs, critic FAILs, and NEEDS_CLARIFICATION re-issues alike; `first_try` is
simply `retries == 0`; `escalated_to` names the tier that ultimately passed, `null` if
none; `verdict` is the final verdict after all attempts; `note` is one short clause,
filled only when it explains a FAIL, retry, or escalation.

Optional cost fields — `tokens`, `tool_uses`, `duration_ms` — may be appended to any
record. They are free: the harness reports them in each Agent result's `usage` block
(`subagent_tokens` / `tool_uses` / `duration_ms`), so the lead already has them at
synthesis time. Recording them lets the log calibrate *cost per class* — is a class
routinely burning more than its tier is worth? — not just correctness; the two together
are what make the Step 2 rubric empirical.

Provider fields (optional) — `provider` ∈ `anthropic | openai | google` (default
`anthropic` when omitted) records which provider actually ran the ticket; and when
`provider != anthropic`, `xprovider_reason` ∈ `independent-lens | context-size |
user-codex-pref` records why the cross-provider route was licensed (SKILL.md Step 2,
references/cross-provider.md). Every existing record omits these and is read as `anthropic`
— backward-compatible.

Channel field (optional) — `channel` ∈ `oneshot | workflow | team` (default `oneshot`
when omitted; every existing record predates the field and is read as `oneshot` —
backward-compatible) records which delegation channel ran the ticket. For
`channel: "team"` records add `teammate` (the teammate's name) and `wave` (1-based
integer): one record per wave- or hypothesis-ticket, not per teammate lifetime. Teammates
return no `usage` block to the lead — omit `tokens` rather than inventing it; a
lead-measured `duration_ms` (wall-clock) is fine. Team-channel records feed the pilot's
pre-committed keep/drop criterion (references/teams.md).

Recalibration — review whenever any tier accumulates ≥ 20 new records, and at every
version bump at the latest:

- `scout` first-try pass < 80% → its lane is too wide: tighten the Mechanical row's
  signals, or stop routing classification-flavored sweeps to Haiku.
- `builder` first-try pass < 80% → either tickets are underspecified (fix the ticket
  template usage) or Skilled-execution signals admit judgment work — read the failing
  records' notes before deciding which.
- Recurring `escalated_to` out of any tier → the class boundary above it is drawn
  wrong; move the recurring signal one row up in the Step 2 table.
- `critic` FAIL rate on first-try deliverables persistently < 5% → the gate may be
  over-applied to low-risk deliverables; revisit what "mandatory critic pass" covers —
  but never weaken it for production code.

The thresholds above are initial guesses; revise them here as data accumulates.

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
