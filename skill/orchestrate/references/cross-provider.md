# Cross-provider: optional non-Claude workers (Codex, Gemini/Antigravity)

Source of truth for the OPTIONAL cross-provider layer. The orchestrate skill is
**self-contained and Claude-only by default**: with no cross-provider surface present, every
routing decision resolves to a Claude worker and the run completes normally. This layer lets
the lead *additionally* delegate to non-Claude models — OpenAI Codex and Google Gemini (via
Antigravity `agy`) — when a surface is detected and a named reason applies. Never a silent
default.

Design rationale — including *why external agents are workers/cross-reviewers and never a
second orchestrator* — lives in an external ADR, not here: **`/opt/tools/agent-bridge/ADR-0001-external-agents-as-workers.md`**.
On any conflict, that ADR (the canon for this machine's cross-provider setup) wins; we do not
duplicate its reasoning.

> **We reference, we do not vendor.** The mechanism below (the `agent-bridge`) is a *separate
> project* at `/opt/tools/agent-bridge`, with its own README, ADR, and lifecycle. orchestrate
> is pure Markdown and owns no executable code — it *calls* the bridge if present, never
> copies it in. Like the MCP connectors, the bridge is **capability-detected by presence**:
> if `/opt/tools/agent-bridge/run-external-agent.mjs` exists in this environment, use it; else
> fall back to an MCP surface (interactive only) or to the Claude-only default. Paths here are
> environment-specific (this machine's layout), not a portable assumption.

## Two surfaces — pick by what you need back

A cross-provider "worker" is **the lead invoking an external CLI from the main loop** (only
the lead has that access) with our ticket in the prompt, then grading the return with our
normal gates. The external model's context is fresh by construction (it sees only the prompt),
so producer≠grader and fresh-context independence hold automatically. There are **two
surfaces**, and choosing the wrong one is the mistake to avoid:

| Surface | Use it for | How | Gives back |
|---|---|---|---|
| **MCP tools** (`codex mcp-server`, `gemini-mcp-tool`) | quick, **interactive** "ask a second opinion" | `mcp__codex__codex`, `mcp__gemini-cli__ask-gemini` | prose in the lead's context; **no token/duration metadata; no strict schema** |
| **agent-bridge** (`run-external-agent.mjs`) | **structured, machine-checkable verdicts** in a pipeline; the Codex coding hand | `node /opt/tools/agent-bridge/run-external-agent.mjs --tool codex\|gemini …` | clean JSON `{ok, output, durationMs, model, command, stderrTail}` — schema-validated |

**Why the bridge for structured work (not MCP):** Codex's `--output-schema` is **silently
ignored while MCP is active** (codex bug #15451), so a strict `APPROVED/WARNING/BLOCKED`
verdict is only reliable through `codex exec --output-schema` — which is what the bridge runs.
Verified live 2026-07-06: bridge Codex returned a schema-valid verdict and even reviewed a repo
file itself; bridge Gemini ran `agy --print --model "Gemini 3.1 Pro (High)"` and honoured the
model. The MCP `ask-gemini` path, by contrast, is model-locked to Flash in print mode — fine
for recon, wrong for a Pro-tier critic.

> **File access differs by tool.** Bridge `--tool codex` (`codex exec`) is an *agent* that
> reads files in its `cwd` sandbox itself — point it at a repo and it opens what it needs.
> Bridge `--tool gemini` (`agy --print`) is a *single-shot* completion with **no file access** —
> paste any file content into the prompt (or `@file` where supported); telling it to "read X"
> just hangs (observed: a file-review prompt with no inlined content timed out). So: Codex for
> repo-grounded review, Gemini for prompts whose material is already inline.

## Connector registry

Name capabilities, not frozen tool strings (survives the gemini-cli → `agy` migration). For
each capability: the **bridge** call (structured) and the **MCP** fallback (interactive), plus
the Claude default it degrades to when no surface is present.

| Capability | Use | Bridge (structured) | MCP (interactive) | Degrades to |
|---|---|---|---|---|
| `codex-critic` | cross-model lens | `--tool codex --schema <verdict> --sandbox read-only --cwd <repo>` | `mcp__codex__codex` (read-only) | Claude dual-lens |
| `codex-code` | coding hand | `--tool codex --sandbox workspace-write --cwd <worktree>` | `mcp__codex__codex` (workspace-write) | `builder` (Sonnet) |
| `gemini-critic` | cross-model lens (alt) | `--tool gemini --model "Gemini 3.1 Pro (High)"` | `mcp__gemini-cli__ask-gemini` (Flash-only in print) | Claude dual-lens |
| `gemini-recon` | big-context recon | `--tool gemini --model "Gemini 3.5 Flash (High)"` | `mcp__gemini-cli__ask-gemini` | `scout` (Haiku) |
| `gemini-recon-cheap` | high-volume mechanical recon | `--tool gemini --model "Gemini 3.5 Flash (Low)"` | `mcp__gemini-cli__ask-gemini` | `scout` (Haiku) |

## Model & effort pins — verify against the connector, not the web

**Pin only models the connector actually exposes.** Check `agy models` (Gemini/Antigravity)
and the Codex CLI's own default — never pin from a press announcement. (Lesson, 2026-07-06:
`Gemini 3.5 Pro` and `gemini-3.1-flash-lite` are announced/plausible but **not in `agy models`**
here, so they are not pinnable; `Gemini 3.5 Pro` is not yet public at all.)

> **Verified for — checked 2026-07-06 against `codex login status` + `agy models` (agy 1.0.16):**
> - **Codex**: logged in via ChatGPT; CLI default model floats to the current top (`gpt-5.5`
>   family). Reasoning effort `low | medium | high | xhigh` via `--config model_reasoning_effort=…`
>   (bridge) or `config:{…}` (MCP). Bridge auto-converts a JSON Schema to Codex-strict
>   (all properties → `required`, `additionalProperties:false`).
> - **Gemini via `agy`**: reasoning effort is baked into the **model name suffix** —
>   `(High) | (Medium) | (Low)`. Models present in `agy models`: `Gemini 3.5 Flash (H/M/L)`,
>   `Gemini 3.1 Pro (L/H)` (agy also proxies `Claude Sonnet/Opus 4.6 (Thinking)`, `GPT-OSS 120B`).
>   **`Gemini 3.5 Pro` is not public / not listed** — use `Gemini 3.1 Pro (High)` for the
>   reasoning lens until 3.5 Pro actually ships, then re-verify and update this banner.

| Capability | Model (verified available) | Effort | Why |
|---|---|---|---|
| `codex-critic` | Codex CLI default (`gpt-5.5` family, floats) | **xhigh** | adversarial verification — mirrors our Opus-xhigh critic |
| `codex-code` | Codex CLI default (coding) | **high** (xhigh only for a hard ticket) | implementation to spec — mirrors builder/Sonnet-high; escalate effort before model |
| `gemini-critic` | **`Gemini 3.1 Pro (High)`** | High (in the name) | deep-reasoning cross-model lens (3.5 Pro not public yet) |
| `gemini-recon` | **`Gemini 3.5 Flash (High)`** | High | big-context recon workhorse (1M ctx, cheap, fast) |
| `gemini-recon-cheap` | **`Gemini 3.5 Flash (Low)`** | Low | high-volume mechanical sweeps — the Haiku-lane cross-provider scout |

## Detection & graceful degradation

- **Detection** (once per capability at Step 2): a bridge capability is *available* iff
  `/opt/tools/agent-bridge/run-external-agent.mjs` is present and the underlying CLI is logged
  in (`codex login status` / `agy models` succeed); an MCP capability iff its tool is in the
  lead's tool set. Record availability in the routing plan.
- **Degradation**: if a route selects a cross-provider capability that detection finds absent,
  fall back to its named Claude default (registry, last column), note it in one line of the
  final report, and proceed. Absence is never a run-blocking error.
- **Runtime failure** (present but the call errors / times out / `ok:false`): treat as a worker
  FAIL feeding the escalation ladder (Step 4) — ① one retry → ② escalate to the Claude default
  → ③ report the blocker. A cross-provider failure never escapes the ladder.

Gradeable invariant: *with no cross-provider surface present, every routing decision resolves
to a Claude worker and the run reaches a normal terminal report.*

## When cross-provider is licensed — the three named reasons

Chosen ONLY for one of these, never as a silent default:

1. **Independent-lens verification** — an uncorrelated critic on a highest-stakes deliverable
   (flagship use: a different-provider reviewer whose blind spots don't overlap Claude's;
   avoids self-preference bias — ADR-0001). → Use 1.
2. **Context exceeds Claude** — the recon/research INPUT is too large for a Claude window. → Use 2.
3. **Explicit user preference for the Codex coding hand** — opt-in; Claude `builder` (Sonnet)
   remains the default coder. → Use 3.

## Use 1 — cross-model critic lens (read-only, additive THIRD lens)

For a dual-lens-trigger deliverable (SKILL.md Step 4.3), when architectural independence is
worth the latency (security-relevant code, or on user request), add ONE cross-model critic as
a **third** lens on the same deliverable + acceptance criteria.

- **Call (structured, preferred)**: `node /opt/tools/agent-bridge/run-external-agent.mjs
  --tool codex --schema <verdict.json> --sandbox read-only --cwd <repo>`, prompt on **stdin** =
  the deliverable + the ticket's ACCEPTANCE verbatim + "try to refute; a finding needs a
  concrete failure scenario." Verdict schema = **the bridge's machine-checkable shape**
  (`status ∈ APPROVED|WARNING|BLOCKED`, `summary`, `issues[]`; see the agent-bridge README) —
  this is *distinct* from our critic's prose PASS/FAIL verdict (quality.md §3); the lead maps
  `BLOCKED`/`WARNING`-with-a-real-scenario onto a FAIL. For a Gemini lens use `--tool gemini
  --model "Gemini 3.1 Pro (High)"` (schema is best-effort in-prompt; check `ok`). Read the
  parsed `output`.
- **Call (quick, interactive)**: `mcp__codex__codex` (read-only, effort xhigh) — prose only,
  no strict verdict; use when you just want a fast second opinion, not a gated verdict.
- **Composition**: **additive** — accept only when **both Claude lenses PASS AND the cross-model
  lens raises no surviving critical/major finding** (`BLOCKED`/`WARNING` with a real scenario).
  It can only *tighten* the gate; it never replaces a Claude lens, so it can never cause a
  false-accept. Absent a surface, the standard two-Claude-lens gate applies unchanged.

## Use 2 — Gemini big-context recon (read-only alternative scout)

For reason 2 only. **Call**: `--tool gemini --model "Gemini 3.5 Flash (High)"` (or the MCP
`ask-gemini`, which is Flash anyway), prompt = a **scout-contract** ticket (OBJECTIVE / INPUTS
via `@file` where supported / exact bounded OUTPUT / read-only BOUNDARIES / ACCEPTANCE + the
scout escape hatch `NEEDS_CLARIFICATION`, "0 matches is a valid answer"). For very high-volume
*mechanical* sweeps drop to `Gemini 3.5 Flash (Low)`.

- **Bound OUTPUT tightly** (N rows, exact table, distilled answer) — the huge context stays on
  Gemini's side; only the distilled result returns to the lead.
- **N1–N6 compatibility**: a Gemini recon ticket inherits the scout **verb whitelist** (recon/
  read/classify-by-a-given-rule only — no decide/recommend/design). A judgment-bearing sweep is
  never a Gemini scout; it routes to `architect`.

## Use 3 — Codex coding hand (opt-in alternative builder; writes only in a worktree)

For reason 3 only — opt-in; Claude `builder` remains the default coder. **Call (preferred)**:
`--tool codex --sandbox workspace-write --cwd <worktree>` via the bridge (structured result +
`durationMs` + token count in `stderrTail`); MCP `mcp__codex__codex` is the interactive
fallback. Params: prompt = a **builder-contract** ticket (full spec, explicit scope, "run
scoped checks yourself"); `cwd` = a dedicated **worktree** path (structural write-scoping);
**never `danger-full-access`**; effort **high** (xhigh only for a genuinely hard ticket).

- After return: read the diff **from disk** (`git diff`), never Codex's self-report; run the
  deterministic pre-gate; then a **Claude critic** (or a *different* cross-provider critic) —
  **never a Codex critic on Codex's own work** (producer≠grader). Codex-builder + Claude-critic
  is automatically both producer≠grader AND cross-provider verification.
- **N1–N6 compatibility**: Codex-authored hunks flow into the same integrated-diff snapshot the
  whole-diff final review consumes — that review is author-agnostic.

## Cost, latency, telemetry

Cross-provider calls spend the OTHER subscription's quota (a feature — offloads Claude quota
when it's tight) at the cost of added latency and operation outside Claude Code's sandbox.
Sandbox defaults are least-privilege: **read-only** for uses 1 and 2; **workspace-write +
worktree cwd** for use 3; **never `danger-full-access`**.

Telemetry (quality.md §7): record optional `provider` ∈ `anthropic | openai | google` (default
`anthropic`) and, when `provider != anthropic`, `xprovider_reason` ∈ `independent-lens |
context-size | user-codex-pref`. **Cost fields, by surface:** the **bridge** returns
`durationMs` always and, for Codex, a token count in `stderrTail` (~9k in the live test) —
record those. The **MCP** path returns neither → leave `tokens` `n/a`; never invent a number.
The completion-card token/effort figure follows the same rule.

## Reserve, not baseline — pal/clink

A turnkey multi-agent orchestrator (`pal`/`clink`, `/opt/tools/pal-mcp-server`) was
**evaluated and deliberately rejected** as the base mechanism (ADR-0001): its roles/`consensus`
duplicate what our own loop does, it adds a layer and 18 tools of context, and making it work
with `agy` needs a source patch rebased on every update. Kept as a reserve for a future "council
of agents" need only — do not reach for it as the default cross-provider path.
