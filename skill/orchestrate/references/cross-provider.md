# Cross-provider: optional non-Claude workers (Codex, Gemini/Antigravity)

Source of truth for the OPTIONAL cross-provider layer (v2.0). The orchestrate skill is
**self-contained and Claude-only by default**: with no cross-provider connector present,
every routing decision resolves to a Claude worker and the run completes normally. This
layer lets the lead *additionally* delegate to non-Claude models — OpenAI Codex and Google
Gemini (via Antigravity) — when a connector is detected and a named reason applies. It is
never a silent default.

> Non-Claude models are reachable only as **MCP tool calls**, not as Claude Code
> sub-agents (the Agent tool spawns Claude models only). So a cross-provider "worker" is
> **the lead calling an MCP tool from the main loop** with our ticket in the prompt, then
> grading the return with our normal gates. The external model's context is fresh by
> construction (it sees only the prompt, never our conversation), so producer≠grader and
> fresh-context independence hold automatically. The one cost: the result returns into the
> lead's context with no intermediate agent to summarize it — fine for critics (the lead
> reads the verdict anyway) and coding (the lead reads the diff from disk), but for big
> recon the ticket MUST bound OUTPUT tightly so the huge context stays on the provider's side.

> **Not shipped with the skill.** A dev repo may allow-list these MCP tools in
> `.claude/settings.local.json`, but `install.sh` wires only `skill/orchestrate/` and the
> agents — the allow-list does not travel. On a fresh install the layer is dormant until
> the user wires the connectors at their own scope. That is the intended self-contained
> default, not a bug.

## Who invokes it

Only the **lead**, in the **main loop** (only the lead is guaranteed MCP access). Cross-
provider is chosen at Step 2 routing, AFTER the complexity class has already fixed the
Claude tier — the provider is an orthogonal choice, never a substitute for classification.

## Connector registry

Name capabilities, not tool strings (this survives the gemini-cli → Antigravity `agy`
migration — gemini-cli consumer access ceased 2026-06-18, and there is no single canonical
`agy` MCP tool name). Use the first present connector; else degrade.

| Capability | Use | Connector (preference order) | Sandbox default | Degrades to |
|---|---|---|---|---|
| `codex-code` | coding hand | `mcp__codex__codex` (+ `codex-reply` for multi-turn) | `workspace-write`, `cwd`=worktree, approval `on-failure` | `builder` (Sonnet) |
| `codex-critic` | cross-model lens | `mcp__codex__codex` | `read-only`, approval `never` | Claude dual-lens |
| `gemini-recon` | big-context recon | *(agy-backed MCP once wired)* → `mcp__gemini-cli__ask-gemini` | `read-only` | `scout` (Haiku) |
| `gemini-recon-lite` | high-volume mechanical recon | *(agy-backed MCP)* → `mcp__gemini-cli__ask-gemini` | `read-only` | `scout` (Haiku) |
| `gemini-critic` | cross-model lens (alt) | *(agy-backed MCP)* → `mcp__gemini-cli__ask-gemini` | `read-only` | Claude dual-lens |

## Model & effort pins

Pin the reasoning **effort** explicitly per use — it is the quality lever, and an unset
effort silently takes the connector's default, which is often lower than the use needs. Let
the **model float** to the connector's current top model instead of freezing a version
string — the exact analog of our Claude family-not-version discipline (SKILL.md): the Codex
CLI already defaults to the current top model for ChatGPT-authenticated sessions, so name
the *intent* and pass an explicit `model` only to override.

> **Verified for: GPT-5.5 / GPT-5.5-codex (Codex ChatGPT-auth default); Gemini 3.5 Flash /
> 3.5 Pro / 3.1 Flash-Lite — checked 2026-07-05.** Codex reasoning-effort levels are
> `low | medium | high | xhigh` (config key `model_reasoning_effort`; `xhigh` = extra-high,
> thinks longest). Codex lineup: `gpt-5.5` default, `gpt-5.4`/`gpt-5.4-mini` fallback,
> `gpt-5.2-codex` for API-key auth. **GPT-5.6 (Sol / Terra / Luna) entered limited preview
> 2026-06-26** — becomes the floating Codex default at GA; re-verify then. Gemini lineup:
> **`gemini-3.5-flash`** (GA 2026-05-19 — beats 3.1 Pro on coding/agentic at ~40% less cost,
> 4× faster, 1M ctx), **`gemini-3.5-pro`** (deep-reasoning tier, 2M ctx, June 2026),
> **`gemini-3.1-flash-lite`** (cheap, high-volume). Gemini exposes no reasoning-effort dial —
> pin its `model` only. **Asymmetry to watch: Codex's CLI default floats to the current top,
> but Gemini's connector default LAGGED (returned 3.1 Pro in testing) — so pass the Gemini
> `model` EXPLICITLY.** Re-verify this banner when either lineup moves.

| Capability | Model (floats to the connector's current top) | Reasoning effort | Why |
|---|---|---|---|
| `codex-critic` (use 1) | current top Codex/GPT reasoning model (GPT-5.5 → 5.6 Sol) | **xhigh** | adversarial verification — mirrors our Opus-xhigh critic; max reasoning, latency acceptable |
| `codex-code` (use 3) | current top Codex coding model (GPT-5.5-codex family) | **high** (xhigh only for a genuinely hard ticket) | implementation to spec — mirrors builder/Sonnet-high; escalate effort before model |
| `gemini-recon` (use 2) | **`gemini-3.5-flash`** (pass explicitly — connector default lags) | n/a (read-only recon, no effort dial) | big-context recon workhorse: near-Pro coding/reasoning at Flash cost/speed, 1M ctx |
| `gemini-recon-lite` (use 2, high-volume) | `gemini-3.1-flash-lite` | n/a | cheap, high-volume *mechanical* sweeps — the Haiku-lane cross-provider scout |
| `gemini-critic` (use 1 alt) | `gemini-3.5-pro` (deep-reasoning tier, 2M ctx) | n/a (highest available) | adversarial lens |

**How to set it on the call.** Codex MCP: pass `config: {"model_reasoning_effort": "xhigh"}`
(and an explicit `model` only to override the floating default). Gemini MCP: pass `model`
**explicitly** (e.g. `gemini-3.5-flash` for recon; the connector default lagged to 3.1 Pro
in testing, so don't rely on it). An unset Codex effort means the connector default — pin it.

## Detection & graceful degradation

- **Detection** (once per capability at Step 2, not per call): a capability is *available*
  iff a connector listed for it in the registry is present in the lead's current tool set.
  Its presence in the callable tool set is the signal — no probe call needed. Record
  availability in the routing plan.
- **Degradation**: if a route selects a cross-provider capability that detection finds
  absent, fall back to its named Claude default (registry, last column), note it in one
  line of the final report ("requested Codex coding-hand; connector absent; used Claude
  builder"), and proceed. Absence is never a run-blocking error.
- **Runtime failure** (available but the call times out / errors / refuses): treat exactly
  as a worker FAIL feeding the escalation ladder (Step 4) — ① one retry → ② escalate to the
  Claude default → ③ report the blocker. A cross-provider failure never escapes the ladder.

Gradeable invariant: *with zero connectors present, every routing decision resolves to a
Claude worker and the run reaches a normal terminal report.*

## When cross-provider is licensed — the three named reasons

Cross-provider is chosen ONLY for one of these, and never as a silent default:

1. **Independent-lens verification** — an uncorrelated critic on a highest-stakes
   deliverable (its flagship use: a different-provider reviewer whose blind spots don't
   overlap Claude's). → Use 1.
2. **Context exceeds Claude** — the recon/research INPUT is too large for a Claude worker's
   practical window. → Use 2.
3. **Explicit user preference for the Codex coding hand** — the user asked for it. Opt-in:
   the Claude `builder` (Sonnet) remains the default coder. → Use 3.

## Use 1 — cross-model critic lens (read-only, additive THIRD lens)

For a dual-lens-trigger deliverable (SKILL.md Step 4.3), when architectural independence is
worth the latency (security-relevant code, or on user request), add ONE cross-model critic
as a **third** lens on the same deliverable + acceptance criteria.

- **Call**: lead → `mcp__codex__codex` (or a gemini-critic connector). Params: `prompt` =
  the deliverable + the ticket's ACCEPTANCE verbatim + the verdict schema (quality.md §3),
  instructed to "try to refute; a finding needs a concrete failure scenario"; `sandbox:
  read-only`; `approval-policy: never`; `cwd`: repo root (so it reads the touched files
  itself); effort **xhigh** via `config: {"model_reasoning_effort": "xhigh"}` (§Model &
  effort pins); `model`: the connector's top reasoning model (floats — pass explicitly only
  to override).
- **Composition**: **additive** — accept only when **both Claude lenses PASS AND the
  cross-model lens raises no surviving critical/major finding**. A cross-model minor → note;
  a critical/major → escalation ladder. The lens can only *tighten* the gate; it never
  replaces a Claude lens, so it can never cause a false-accept. Absent a connector, the
  standard two-Claude-lens gate applies unchanged.

## Use 2 — Gemini big-context recon (read-only alternative scout)

For reason 2 only. **Call**: lead → gemini-recon connector. Params: `prompt` = a **scout-
contract** ticket (OBJECTIVE / INPUTS via `@file` where supported / exact bounded OUTPUT /
read-only BOUNDARIES / ACCEPTANCE + the scout escape hatch: `NEEDS_CLARIFICATION`, "0
matches is a valid answer"); `sandbox: read-only`; `model`: **`gemini-3.5-flash`** passed
explicitly (beats 3.1 Pro on coding/agentic at lower cost, 1M ctx; the connector default
lagged to 3.1 Pro). For very high-volume *mechanical* sweeps use `gemini-3.1-flash-lite` — the
cheap Haiku-lane tier — under the same read-only, no-judgment scout discipline.

- **Bound OUTPUT tightly** (N rows, exact table, distilled answer) — the huge context stays
  on Gemini's side; only the distilled result returns to the lead.
- **Compatibility with the planned N1–N6 refinements**: a Gemini recon ticket inherits the
  scout **verb whitelist** (recon/read verbs only — no decide/recommend/design). A
  judgment-bearing sweep is never a Gemini scout; it routes to `architect`.

## Use 3 — Codex coding hand (opt-in alternative builder; writes only in a worktree)

For reason 3 only — opt-in; Claude `builder` remains the default coder. **Call**: lead →
`mcp__codex__codex`. Params: `prompt` = a **builder-contract** ticket (full spec, explicit
scope, "run scoped checks yourself"); `sandbox: workspace-write` (the only writing use);
`cwd`: a dedicated **worktree** path (structural write-scoping); `approval-policy:
on-failure`; **never `danger-full-access`**; effort **high** via
`config: {"model_reasoning_effort": "high"}` (xhigh only for a genuinely hard ticket —
escalate effort before model); `model`: the current top Codex coding model (floats — pass
explicitly only to override).

- After return: read the diff **from disk** (`git diff`), never Codex's self-report; run the
  deterministic pre-gate; then a **Claude critic** (or a *different* cross-provider critic)
  — **never a Codex critic on Codex's own work** (producer≠grader). Codex-builder +
  Claude-critic is automatically both producer≠grader AND cross-provider verification.
- **Compatibility with the planned N1–N6 refinements**: Codex-authored hunks flow into the
  same integrated-diff snapshot the planned whole-diff final review consumes — that review
  is author-agnostic.

## Cost, latency, sandbox

Cross-provider calls spend the OTHER subscription's quota (a feature — offloads Claude
quota when it's tight) at the cost of added latency and operation outside Claude Code's
sandbox/permission model. Sandbox defaults are least-privilege: **read-only** for uses 1
and 2; **workspace-write + worktree cwd + approval on-failure** for use 3; **never
`danger-full-access`**.

## Telemetry

Record the provider actually used so cross-provider routing calibrates too (quality.md §7):
optional `provider` ∈ `anthropic | openai | google` (default `anthropic` when omitted) and,
when `provider != anthropic`, `xprovider_reason` ∈ `independent-lens | context-size |
user-codex-pref`. The existing `model` field carries the effective model.
