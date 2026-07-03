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

## 2. Grading hierarchy — choose the fastest reliable grader

1. **Code-graded** (exact match, exit codes, grep, build/test/typecheck) — fastest, most
   reliable; always prefer when the criterion allows it. Design criteria to be
   code-gradeable where possible.
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

Standing targets: verification coverage = 100% for production code; first-try pass ≥ 80%
(persistently lower means tickets are underspecified — fix the tickets, not the workers);
escalations are a routing signal (recurring scout escalations = you are giving Haiku
judgment work).

## 6. Honest failure protocol

Failures are reported, never smoothed over: a worker that skipped a step, a test that
still fails, a criterion that could not be checked — all appear in the final report as
what they are. If the overall task did not reach "done", the report says so in the first
sentence and lists what remains, with evidence.
