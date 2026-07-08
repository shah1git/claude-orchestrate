# Agent teams: experimental third delegation channel

Source of truth for the OPTIONAL team channel. The orchestrate skill is complete without
it: with the surface absent — or the pilot below failed — every delegation resolves to the
one-shot or Workflow channel and the run completes normally. This channel exists for a
narrow niche where worker *persistence* and *worker↔worker exchange* demonstrably pay;
everywhere else it is over-process (core doctrine: the simplest system that works).

> **Verified for: Claude Code 2.1.204–2.1.205 · agent-teams docs as of v2.1.178+ —
> checked 2026-07-08; phase-0 first run on 2.1.205 (results in the Pilot section).** The surface is experimental upstream and disabled by default; at design
> time the flag was OFF in this environment and the binary verifiably carried the surface
> (env-var string + SendMessage/TaskCreate/TaskUpdate/TaskList tool names). Facts marked
> (docs) come from code.claude.com/docs/en/agent-teams and the CLI changelog and were not
> yet exercised here. Re-verify this banner in the pilot's phase 0 and whenever the CLI
> minor version changes — this skill was twice burned by pinning announced-but-not-real
> surfaces; pin only what the surface actually exposes (cross-provider.md, v2.2 lesson).

## The surface in one paragraph (docs)

With `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1` set (settings.json `env` block or shell
environment), every session has one implicit team; the lead spawns persistent teammates
directly via the Agent tool's `name` parameter — no setup step. Each teammate is a full
Claude Code session with its own context window: it loads CLAUDE.md, skills, and MCP
servers, receives the spawn prompt, and sees none of the lead's conversation. Coordination
runs over a shared task list (`TaskCreate`/`TaskUpdate`/`TaskList`/`TaskGet`; states
pending → in progress → completed; dependencies auto-unblock; claims file-locked) and a
mailbox (`SendMessage` — one named recipient per message, no broadcast; delivery is
automatic; an idle or API-failed teammate notifies the lead). A teammate may be spawned
from one of our agent definitions ("spawn a teammate using the builder agent type"): its
`tools` allowlist and `model` bind, and its body is appended to the teammate's system
prompt (`skills`/`mcpServers` frontmatter is ignored). Teammates start with the lead's
permission mode; their permission prompts bubble up to the lead session for the user.
Works in the default in-process display mode — no tmux required.

## Availability — detect or degrade (never a run-blocking error)

- **Detect** (once, at Step 1 when a trigger below matches): treat the channel as
  available when `printenv CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS` returns a non-empty
  value (one Bash call; settings.json `env` entries land in the process environment).
  This is the *primary documented* signal, not an iff: the 2.1.204 binary's gating
  function also honors a `--agent-teams` launch flag and an upstream remote gate, so
  detection can err in both directions — and both land on safe paths (a false-absent
  merely degrades; a false-present fails at spawn into the runtime ladder below).
  Do NOT infer availability from `SendMessage` being in the tool set —
  it exists for one-shot sub-agent continuations with the flag off (observed locally,
  routing-log 2026-07-04). Record the result in the routing plan.
- **Degrade** (surface absent): trigger A → a sequential chain of one-shot `builder`
  tickets, each restating full context and consuming the prior wave's diff as INPUT
  (Step 3 discipline — today's status quo); trigger B → 2–3 parallel one-shot `architect`
  investigations with the lead adjudicating between the returned analyses. Note the
  degradation in one line of the final report, tell the user the enabling one-liner
  (`"env": {"CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1"}` in settings.json), and proceed.
  The skill NEVER edits settings itself.
- **Runtime failure** (flag set but a spawn/assign errors): treat as a worker FAIL feeding
  the Step 4 escalation ladder — ① one retry → ② fall back to the degradation path above →
  ③ report. A team failure never escapes the ladder. Mirrors cross-provider.md.

## When the channel fires — exactly two triggers

Everything not matching a trigger stays one-shot / Workflow. Both triggers additionally
require: the surface detected present, AND the Step 1 approval checkpoint (choosing the
team channel always fires it — a team run is expensive and consequential by construction).

| Trigger | Gradeable condition | Team shape |
|---|---|---|
| **A. Persistent build** | ≥ 3 *dependent* implementation waves on ONE subsystem, where consecutive wave tickets would name substantially the same INPUTS file set — i.e. context re-acquisition, not implementation, dominates the one-shot chain's cost | 1–2 `builder`-definition teammates (Sonnet) + lead |
| **B. Competing hypotheses** | unknown root cause with 2–3 plausible, *distinguishable* hypotheses, each needing active investigation, where cross-refutation matters (single-agent anchoring risk) | 2–3 `architect`-definition teammates (Fable), one hypothesis each + lead adjudicates |

Anti-triggers (explicitly NOT this channel): uniform sweeps ≥ 5 items (→ Workflow);
independent parallel subtasks that merely report back (→ one-shot fan-out); anything
`scout`-class — **scout never joins a team**: mechanical work gains nothing from
persistence, and a persistent cheap tier only accumulates drift.

## Hard rules — non-negotiable (carried from the v1.6 plan)

1. **`critic` is always OUTSIDE the team.** Every verification is a one-shot,
   fresh-context sub-agent spawned by the lead via the Agent tool (docs: this works
   normally while a team is active — confirm in phase 0). A teammate never verifies
   team output — persistence is exactly what destroys grader independence.
2. **Task assignment is lead-only — no self-claiming.** The surface cannot disable
   self-claiming by config; it is enforced structurally: self-claim can only grab an
   *unassigned, unblocked* task, so **an unassigned, unblocked task must never exist in
   the shared list**. Create every task already assigned to its named owner; only if
   `TaskCreate` proves unable to assign at creation (verify in phase 0) fall back to
   `TaskUpdate` in the same turn — the create→update window is an ASSUMED-safe gap until
   phase 0 probes it. Additionally, every spawn prompt carries the prohibition (addendum
   below). An observed self-claim is a §8 incident; work from a self-claimed task is
   discarded and re-issued. The single sanctioned exception is phase 0's decoy task
   (pilot below): a deliberate test artifact proving this rule load-bearing, deleted
   before any real task exists — not a §8 incident.
3. **Worker↔worker messages only for the two named patterns** (topology section below).
   Everything else flows through the lead.
4. **Teammates never delegate.** No sub-agents, no teammates-of-teammates (docs: the
   surface forbids nesting; in-process teammates cannot run background sub-agents). All
   routing stays with the lead — if a teammate needs recon outside its ticket, it asks
   the lead, who routes it (usually a one-shot `scout`).

## Spawning teammates

- Spawn via the Agent tool with a `name` (docs/changelog; exact schema is visible in the
  tool definition at runtime — trust the live schema over this text). Reference our agent
  definitions by type: trigger A → `builder`; trigger B → `architect`. Short ASCII names
  that encode the role: `auth-builder`, `hypo-cache`, `hypo-race`.
- The spawn prompt = a full Step 3 seven-field ticket (the teammate sees none of your
  conversation — same isolation rule as one-shots) **plus the teammate addendum**:

  ```
  TEAM RULES (non-negotiable):
  - Work ONLY on tasks the lead assigned to you by name. Never claim, start, or
    modify any other task, even if it sits unblocked in the shared list.
  - Do not spawn sub-agents or teammates; if you need recon or a decision outside
    your ticket, message the lead and wait.
  - Message other teammates ONLY when your ticket names a sanctioned pattern and a
    recipient; cc the lead (separate SendMessage) on every such exchange.
  - When your task is done: run your scoped checks, update the task status, send the
    lead your report (same format as your agent definition), then go idle. Do not
    pick up new work.
  - Your memory of earlier waves is NOT a source of truth: the current task text and
    the actual repository state override anything you remember.
  ```
- Keep spawn prompts lean (everything in them is permanent context for that teammate —
  docs) and shut teammates down as soon as their arc ends (idle teammates keep costing).
- For trigger A's first wave, spawn the builder teammate with **plan approval required**
  (docs feature): it plans read-only, the lead checks the plan against the wave-1 ticket,
  then approves. Later waves skip plan mode — it is the expensive mode (~7× tokens, docs)
  and the lead's wave tickets are already full specs.

## Communication topology — two sanctioned patterns, all else through the lead

1. **Structured hypothesis debate (trigger B).** Rounds are lead-initiated:
   *investigate → exchange → adjudicate.* Each holder investigates its own hypothesis,
   reports evidence to the lead; the lead then instructs each holder to send each rival
   ONE challenge message (strongest refutation of the rival's evidence, with file:line /
   repro), each rival replies ONCE to each challenge, cc the lead on everything. No
   free-running argument threads: after one challenge–response round the lead adjudicates
   (main loop) — kill, merge, or continue a hypothesis; at most one more round before
   Step 4's two-attempt discipline applies to the investigation as a whole.
2. **One-round interface handshake (trigger A with two builders).** When two builder
   teammates share a contract (schema, API signature, event shape): builder-1 sends the
   proposed contract to builder-2, builder-2 replies AGREED or with a counter, cc the
   lead both ways. One round; if not AGREED after one counter, the lead decides the
   contract and writes it into both tickets. The lead re-states the agreed contract in
   the next wave's tickets regardless — a handshake is never left implicit in teammate
   memory.

There is no broadcast: "cc the lead" is a second SendMessage to the lead. Any other
teammate-to-teammate message is out of contract → §8 incident.

## Wave protocol (trigger A)

Per wave: **ticket → build → deterministic pre-gate → outside critic → reconcile → next
ticket.** The quality gates are Step 4 unchanged, mapped onto persistence:

1. **Ticket.** One task per wave per teammate, created assigned (hard rule 2), full
   seven fields in the task description; wave N's ACCEPTANCE written before dispatch.
   Every wave ticket **restates all constraints in force** — vocabulary, boundaries,
   contracts — even if wave N−1 stated them. Never rely on teammate memory (the Step 3
   isolation rule, applied to a worker that merely *feels* less isolated).
2. **Build.** Teammate implements, runs scoped checks itself, reports with evidence
   (its agent-definition format), updates task status, idles. Two concurrent writers max,
   on disjoint path sets each ticket names; a third concurrent writer is not a team run —
   re-shape or use the one-shot worktree-isolation rule (Step 3). Trust diffs, not task
   states: statuses can lag (docs) — the wave is done when the lead has the diff.
3. **Deterministic pre-gate.** The lead captures the wave diff from disk (`git diff`,
   never the self-report) and re-runs the code-gradeable criteria. FAIL → straight back
   to the teammate with output attached (consumes a retry; zero Opus tokens). Unchanged
   from Step 4.2.
4. **Outside critic.** One-shot `critic` (fresh context) on the wave diff + criteria
   verbatim. FAIL → Step 4 escalation ladder, with one channel-specific rung: after ① one
   teammate retry, ② is **retire the teammate and continue the chain as one-shot
   builder tickets** (fresh context IS the escalation), then ③ report. Dual-lens and
   cross-model rules (Step 4.3) apply unchanged when a wave's deliverable meets their
   triggers.
5. **Reconcile before the next wave.** Compare the teammate's *stated assumptions* (from
   its report) against the actual accumulated diff; a divergence is corrected in the next
   ticket's INPUTS explicitly ("wave 2 assumed X; the diff does Y; build on Y") — the
   Step 3 spec-drift rule applied to teammate memory.
6. **After the final wave: whole-diff review** (Step 4.7) over the entire multi-wave
   diff — **including single-writer team runs**: wave seams are writer seams; Step 4.7's
   "skip for single-writer changes" does not apply to the team channel.

**Retire-and-respawn.** After ~3 waves, or earlier on any drift sign — the teammate
references state the diff contradicts, repeats an already-corrected error, or its reports
degrade — request a handoff summary (what exists, key decisions, open gotchas), verify it
against the diff, shut the teammate down (docs: graceful shutdown by name), and spawn a
replacement seeded with the verified summary. A persistent context is a cache, and caches
get invalidated on schedule, not on catastrophe.

## Debate protocol (trigger B)

1. Lead writes 2–3 hypothesis tickets: same OBJECTIVE shape ("establish or refute: …"),
   disjoint hypotheses, shared INPUTS, ACCEPTANCE = "verdict with reproducible evidence;
   every claim carries file:line or command output". One task per holder, assigned.
2. Holders investigate independently (read-only unless the ticket grants a repro
   sandbox), report to the lead.
3. One challenge–response round (topology pattern 1), cc the lead.
4. Lead adjudicates in the main loop: surviving hypothesis, with the debate evidence.
   Adjudication is lead work — never delegated (Step 2 matrix: cross-cutting judgment).
5. If the conclusion is consequential (drives a fix, a rollback, anything the user acts
   on), spawn a one-shot **outside `critic`** on the adjudicated conclusion + evidence
   bundle: fresh context, tries to refute the winner — the debate can converge on shared
   groupthink; the outside gate cannot.
6. Shut all holders down; the fix itself is a normal one-shot/Workflow delegation (or a
   trigger-A run if it independently qualifies).

## Telemetry (schema: quality.md §7)

One record per wave-ticket / hypothesis-ticket (not per teammate lifetime), with
`channel: "team"`, `teammate` (name), and `wave` (1-based). Teammates return no `usage`
block to the lead — omit `tokens` (never invent; the completion-card rule applies) and
record lead-measured `duration_ms` wall-clock where useful. Log self-claim events,
out-of-contract messages, and drift-respawns in `note` and as §8 incidents (class:
`team-coordination`).

## Pilot — falsifiable, criterion written BEFORE the first run (quality.md §1 on ourselves)

**Phase 0 — mechanics smoke test** (after the user enables the flag; interactive session,
trivial sandbox repo, ≤ 15 minutes): spawn one builder-definition teammate; verify:
(a) spawn works and the definition's tools/model bind; (b) a task created-assigned is not
claimable by others and an intentionally unassigned decoy task IS self-claimed (proves the
structural rule is load-bearing, then delete the decoy); (c) SendMessage teammate↔lead
both ways; (d) graceful shutdown; (e) note whether builder's `effort: high` frontmatter or
the lead's effort applies, if observable. Any hard failure here → the channel stays
documented-only; re-verify on a later CLI version.

> **Phase 0 result — first run, 2026-07-08, CLI 2.1.205, flag ON, CHILD session
> (`CLAUDE_CODE_CHILD_SESSION=1`).** Mixed; the channel stays **documented-only** pending
> a re-run in a top-level interactive session. Verified: detection fires (env var set;
> `~/.claude/teams/session-…` provisioned); the Agent tool accepts `name` at runtime
> (absent from the rendered schema — trust the live schema); the `builder` definition's
> tools and model bind exactly (teammate self-reported Sonnet + the frontmatter tool set);
> effort is NOT observable from inside the worker (checklist (e): unanswerable by
> self-report). **Failed to manifest: team-tool injection** — the spawned worker received
> NO TaskGet/TaskUpdate/SendMessage, i.e. it ran as a regular one-shot despite `name`;
> checks (b)/(c)/(d) therefore moot in this session shape (decoy untouched but
> unclaimable-by-construction — inconclusive, not passed). Candidate causes, unresolved:
> child-session limitation (design ASSUMED #3), the upstream remote gate, or a
> harness-path difference. Also settled: `TaskCreate` has NO owner field (ASSUMED #1
> refuted) — assignment is `TaskUpdate {owner}` only, so the safe ordering is
> **create → assign → only then spawn**, which removes the claim window structurally.
> Positive transfer: the spawn-prompt TEAM RULES held — the worker refused to fabricate
> unavailable tool calls, reported honestly, went idle without picking up other work.

**Phase 1 — two real runs** (one trigger A with ≥ 3 waves, one trigger B), telemetry per
wave as above, user records `/usage` before/after each run. Phase 1 is gated on a CLEAN
phase-0 re-run in a top-level interactive session (team-tool injection observed).

**Keep/drop criterion (pre-committed):**
- **KEEP** only if ALL hold across the pilot: (1) wave/hypothesis first-try pass rate
  ≥ 80% (the standing §5 target — the one-shot baseline this channel must not undercut);
  (2) zero shipped work from a self-claimed task and zero critic-inside-team events;
  (3) zero defects traced to teammate stale context that passed per-wave gates (the
  Cognition failure mode — zero tolerance, this is the exact risk the channel was warned
  about); (4) cost sanity: the run's wall-clock and user-recorded usage ≤ 2× the lead's
  pre-run written estimate of the degraded (one-shot chain) equivalent.
- **DROP** immediately on (3), or if any other condition fails in both runs: remove the
  SKILL.md section and this file's channel status becomes "dropped — evidence:" with the
  telemetry; the `channel` field stays in §7 (it is generic).
- **One renegotiation allowed:** if exactly one condition fails in exactly one run with
  an identifiable playbook cause, fix the rule (§8 discipline) and re-run that trigger
  once. No second renegotiation.
- **Default clause:** any pilot outcome not matching KEEP or the single renegotiation is
  a **DROP** — the pre-commitment defaults to dropping, never to discretion. (KEEP's
  conditions are evaluated across the whole pilot; DROP on condition (3) is immediate
  and per-run.)

## Known surface limitations that bind this design (docs)

- No `/resume`/`/rewind` of in-process teammates → a team run must fit one session; never
  plan a team across a session boundary; after an accidental resume, respawn, don't
  message ghosts.
- Task status can lag → gates key on diffs and reports, never on task states.
- Shutdown is slow (teammate finishes its current call) → budget it; don't force-kill.
- One team per session; the lead is fixed; teammates can't set per-teammate permission
  modes at spawn (they inherit the lead's — with `--dangerously-skip-permissions` on the
  lead, ALL teammates run unguarded: do not run team pilots in that mode).
- Permission prompts bubble to the lead session for the USER → pre-approve the common
  operations before spawning, or expect interruptions.
