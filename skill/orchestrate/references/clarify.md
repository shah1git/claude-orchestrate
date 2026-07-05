# Clarify: the request-level grill before decomposition

Source of truth for the Step 0.5 clarification gate. **Adapted from** the `grill-with-docs`
skill by Matt Pocock (aihero.dev), MIT-licensed — itself built on `grill-me`: the
relentless, one-question-at-a-time, doc-grounded interrogation method. This file adapts
that mechanism to an orchestrator — same Socratic core, but its output is tuned to feed
decomposition and tickets, and its file-writing is gated because an orchestration run is
often read-only. Self-contained on purpose: orchestrate ships everything it needs and must
not hard-depend on a separately-installed skill (we internalize the *method* as adapted
prose with attribution; we do not invoke the external skill at runtime, nor vendor its
files verbatim — its solo-session framing does not fit an orchestrator).

> **Verified against grill-with-docs upstream 2026-07-05** (the local copy at
> `~/.claude/skills/grill-with-docs`, copied verbatim from mattpocock/skills). The method
> below is a condensation, not a copy; re-check it against upstream when that skill updates.

## Who runs it, and when

Only the **lead**, only in the **main loop**. Workers get a fresh isolated context and
cannot talk to the user (SKILL.md Step 3, Step 4.6). The one thing you may delegate
mid-grill is code exploration: a fork the codebase can settle is a `scout` ticket or your
own Read — "explore the codebase instead of asking" (grill-with-docs) maps onto the
orchestrator as "resolve from code before spending a user turn." Which tier grills, and
how hard, is the Step 0.5 trigger table.

## The method (adapted grill)

1. **One open question at a time, relentless.** Walk down each branch of the decision tree,
   resolving dependencies one-by-one; wait for the answer before the next question. Do not
   flatten a dependency-ordered march into a questionnaire — the value is that each answer
   reshapes the next question.
2. **Always give your recommended answer.** Every question carries the lead's default
   ("I'd assume X because Y — correct?") so the user can confirm cheaply.
3. **Sharpen fuzzy language to a canonical term.** When a term is vague or overloaded,
   propose the precise word and the ones to avoid: "You said 'account' — the Customer or
   the User? Those are different." Resolved terms become shared ticket vocabulary.
4. **Cross-reference every claim with the code.** When the user states how something works,
   check the code agrees; surface contradictions with the file:line. A contradiction is
   both a clarification and a future ticket INPUT.
5. **Stress-test with concrete scenarios.** Invent edge-case scenarios that force precision
   about the boundaries between concepts.
6. **Challenge against an existing glossary.** If a CONTEXT.md already exists and the user
   uses a term against its definition, call it out immediately.
7. **Stop when clarification stops changing the plan.** The moment the remaining questions
   no longer alter the decomposition or any ticket's INPUTS / BOUNDARIES / ACCEPTANCE,
   stop. Clarify to plan, not to interrogate — the "build the simplest thing" doctrine
   (SKILL.md Step 0) governs the grill too.

**Depth is your judgment — no fixed question count.** The number of questions is set by the
task, not by a quota: keep asking, one at a time, until the stop condition — that may be
one question or a dozen. Never truncate the grill to hit a small number, never pad it to
reach a large one. (Mahler's "5–9 questions" is a fixed quota we deliberately reject in
favor of the semantic stop condition.)

## Interaction mechanism — one-at-a-time vs. AskUserQuestion

- **Full grill → free-form, one open question per turn.** `AskUserQuestion`'s batched,
  pre-committed multiple choice cannot express dependency-ordered follow-ups; using it for
  the full grill defeats the "walk the tree" method.
- **Light grill / independent forks → one `AskUserQuestion` (≤4).** When the open questions
  are independent and each has a small closed answer set ("which service is 'the API'?",
  "read-only, or may builders write?"), the batched form is faster and lower-friction; a
  multi-turn march there is over-process.
- **Gradeable rule:** if resolving one question changes what the next should be →
  one-at-a-time. If the forks are independent and enumerable → one batch. More than four
  independent forks is a full grill, not a batch. The `≤4` is the tool's per-call ceiling,
  not a cap on how many questions the grill may ask in total.

## The clarification ledger

Capture resolutions as you go, in your working notes (not necessarily a file — see the doc
policy). The ledger is the bridge from grill to tickets:

```
TERMS    canonical term — one-line definition — avoid: <synonyms>
FORKS    question → decision → why (one clause)
SUCCESS  the confirmed "done", stated gradeably
CODE     claim ↔ file:line, agreed or contradicted
ADRs     decisions passing the three-condition test below, if any
```

The ledger → plan/ticket mapping is the table in SKILL.md Step 0.5; delegation.md repeats
it as the ticket-seeding rule.

## Doc side-effects — on / offered / off

grill-with-docs writes CONTEXT.md / docs/adr **inline, unprompted** (its SKILL.md: "update
CONTEXT.md right there. Don't batch these up"). The orchestrator deliberately does **not**
inherit that: a run is often read-only, many target repos have no CONTEXT.md, and creating
or committing files is a repo mutation the user's standing rule says to confirm first.

- **Off (default).** Read-only runs (comparison, research, report-only audits) and runs
  with no writable target repo: capture everything in the ledger, write nothing. The
  sharpened terms and decisions flow into tickets, not files.
- **Offered (end of grill).** When the repo already documents itself (a CONTEXT.md /
  CONTEXT-MAP.md / docs/adr exists — it opted into the discipline) OR the run will produce
  code changes: at the end, offer to persist — "I resolved N terms and M decisions — write
  CONTEXT.md / docs/adr/NNNN? (y/n)." Write only on a yes.
- **On (explicit request).** The user asked to update the docs/glossary: write inline and
  lazily, as upstream does.

Never create a repo's **first** CONTEXT.md as a silent side effect — introducing a glossary
is itself an architectural act; offer it, don't assume it.

## CONTEXT.md format (condensed from grill-with-docs CONTEXT-FORMAT.md)

A glossary and nothing else — no implementation details, no spec, no scratch pad. Per term:
the canonical word, a one-or-two-sentence definition of what it *is*, and an `_Avoid_:`
list of rejected synonyms. Be opinionated — pick one word, list the rest as avoid. Only
terms specific to this project's domain; general programming concepts don't belong. Single
repo → one root `CONTEXT.md`; multiple bounded contexts → a `CONTEXT-MAP.md` at the root
pointing to per-context files. Create lazily, only when a term is actually resolved and the
doc policy says write.

```md
# {Context name}
{one or two sentences: what this context is and why it exists}

## Language
**Order**: A customer's request to purchase goods. _Avoid_: Purchase, transaction
**Customer**: A person or org that places orders. _Avoid_: Client, buyer, account
```

## ADR format + the "should we even?" test (condensed from ADR-FORMAT.md)

Offer an ADR only when **all three** hold — otherwise skip it:

1. **Hard to reverse** — changing your mind later costs real work.
2. **Surprising without context** — a future reader will wonder "why this way?"
3. **A real trade-off** — genuine alternatives existed and you picked one for reasons.

Format is tiny: `docs/adr/NNNN-slug.md`, sequential numbering, 1–3 sentences (context,
decision, why). Optional Status / Considered Options / Consequences only when they add
value. Persist only per the doc policy above.

```md
# {Short title of the decision}
{1–3 sentences: the context, what we decided, and why.}
```
