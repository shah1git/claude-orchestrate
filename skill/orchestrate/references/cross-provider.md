# Cross-provider: non-Claude workers (Codex, Gemini/Antigravity)

Source of truth for the cross-provider layer. Portability floor: with **no cross-provider
surface present**, every routing decision resolves to a Claude worker and the run completes
normally — the skill degrades to self-contained Claude-only. When surfaces ARE present
(this machine: Codex + Antigravity `agy`), non-Claude workers are **regular members of the
routing pool**, routed by the lead's judgment under the standing mandate below — not an
exception layer. Judgment-driven, never silent: every non-Claude route carries a named
reason into telemetry.

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

## Auth principle — subscription OAuth first (config v5; omp case study, 2026-07-12)

External surfaces authenticate through the **provider's official CLI logged into the
user's subscription** (Codex CLI ← ChatGPT/Codex plan; Gemini CLI / Antigravity ← Google
account) — never through metered API keys by default. Verified live 2026-07-12: both
current lanes already run this way (bridge `--detect`: codex `loggedIn: true`; gemini
surface present with its model catalog). Why this is the default, not a convenience:

- **zero marginal cost** — the subscriptions are already paid; quota-spread moves work
  onto them without opening a new bill;
- **separate quota windows** — subscription pools are independent of API-tier limits,
  which is the entire point of the spread;
- **no key custody** — the vendor CLI holds and refreshes its own tokens; nothing in our
  config to rotate or leak.

Caveats the lead must respect: subscription pools are **opaque and shared** (Codex and
ChatGPT Work draw one pool — a lane can go dark mid-run; the availability fallbacks cover
it), and a headless CLI login can expire. An expired login is `fallback_reason:
connector-error`/`subscription` in telemetry plus a request to the user to re-login —
never a silent switch to an API key. A metered API key is a deliberate, owner-approved
exception with its reason logged. Future connectors (e.g., Grok Build CLI) attach by the
same rule: subscription OAuth surface first.

**Vendor policy map — subscription OAuth in third-party harnesses (verified 2026-07-12;
recheck on any auth incident):**

| Vendor | Third-party use of subscription OAuth | Enforcement |
|---|---|---|
| Anthropic | forbidden (legal-and-compliance page, explicit) | billing cutoff since 2026-04; account disabling since 2026-01 |
| OpenAI | **explicitly allowed** — CEO and the Codex eng-lead publicly endorsed ChatGPT-plan OAuth in third-party harnesses (2026-02/05) | no documented bans |
| Google | forbidden (Gemini CLI ToS, names third-party tools verbatim) | mass account bans 2026-02-27 (paid Ultra included); automated detection since 2026-03-18 |

The architectural line that keeps our lanes on the safe side of this map: we **spawn the
vendor's official CLI as a subprocess** — every backend call is made by the vendor's own
client, headless modes are documented features. That is categorically different from what
banned tools do: extract the OAuth token and impersonate the official client from
third-party code. Keep it that way — a lane that would call a vendor backend directly
with a subscription token is a stop-and-ask decision, never an implementation detail.
Residual gray zone for Google: their detection has produced false positives — an
unexplained Google-account restriction on this machine is a possible detection misfire;
surface it to the owner immediately.

## Two surfaces — pick by what you need back

A cross-provider "worker" is **the lead invoking an external CLI from the main loop** (only
the lead has that access) with our ticket in the prompt, then grading the return with our
normal gates. The external model's context is fresh by construction (it sees only the prompt),
so producer≠grader and fresh-context independence hold automatically. There are **two
surfaces**, and choosing the wrong one is the mistake to avoid:

| Surface | Use it for | How | Gives back |
|---|---|---|---|
| **MCP tools** (`codex mcp-server`, `gemini-mcp-tool`) | quick, **interactive** "ask a second opinion" | `mcp__codex__codex`, `mcp__gemini-cli__ask-gemini` | prose in the lead's context; **no token/duration metadata; no strict schema** |
| **agent-bridge** (`run-external-agent.mjs`) | **structured, machine-checkable verdicts** in a pipeline; the Codex coding hand | `node /opt/tools/agent-bridge/run-external-agent.mjs --tool codex\|gemini …` | clean JSON `{ok, output, durationMs, model, usage, sessionId, sandboxViolations, command, stderrTail}` — schema-validated |

**Why the bridge for structured work (not MCP):** Codex's `--output-schema` is **silently
ignored while MCP is active** (codex bug #15451 — closed upstream 2026-07-09 with no
documented fix visible on the issue). This machine doesn't have MCP configured for Codex, so
the bug's precondition doesn't even apply here — but the underlying argument still holds: a
strict `APPROVED/WARNING/BLOCKED` verdict is only reliable through `codex exec --output-schema`
— which is what the bridge runs.
Verified live 2026-07-06: bridge Codex returned a schema-valid verdict and even reviewed a repo
file itself; bridge Gemini ran `agy --print --model "Gemini 3.1 Pro (High)"` and honoured the
model. The MCP `ask-gemini` path, by contrast, is model-locked to Flash in print mode — fine
for recon, wrong for a Pro-tier critic.

> **Bridge Gemini is unreliable for read-only *contracts* — regressed 2026-07-11 (incident
> #5, 2nd sighting).** The 2026-07-06 note above verified that the bridge honours the *model*;
> it did not verify that Gemini honours a *read-only-review* instruction. In the role-polygon
> benchmark the bridge Gemini path failed both uses: as a scout it returned an interactive
> greeting ("terminal commands are enabled… how can I help?") instead of running the inlined
> task; as a critic (`Gemini 3.1 Pro (High)`) it **edited the deliverable** (swapped `eval` for
> an `ast` evaluator) instead of returning a findings list — no gradeable verdict. This repeats
> the earlier routing-log `MISFIRE(committed instead of review)`. Both are contract failures,
> not model-quality failures (it detected the defect while "fixing" it). Rule of thumb:
> - **Cross-model third lens → prefer `codex-critic` (Sol)** over `gemini-critic` when the only
>   Gemini path is the bridge; Sol returned a clean read-only findings list (8/8) under
>   `sandbox: read-only` with no violations.
> - **When Gemini is the right lens anyway** (its distinct angle is wanted), reach it via the
>   **`ask-gemini` MCP with material inlined in the prompt**, not the bridge — that path did
>   honour a read-only extraction task (scout t2, 17/17). Accept its Flash-in-print model lock.
> - Treat any bridge-Gemini output that *isn't* the requested read-only artefact as a MISFIRE,
>   not a finding; do not let a bridge-Gemini "fix" reach disk (its `--cwd` is a throwaway copy,
>   so it cannot — but never promote its edits).
>
> **Update 2026-07-12 — the model is exonerated; the fault is transport-specific.** A two-transport
> re-test on the sealed polygon tasks (`benchmark/results/2026-07-12-gemini-retest/`) had Gemini
> *pass* every task on both paths, so the 2/2 2026-07-11 misfires were a property of the failing
> transport, not of Gemini: the earlier stance "suspend `gemini-critic` until the model is fixed"
> is wrong and is lifted. What the re-test pinned instead:
> - **`ask-gemini` MCP is the working Gemini path** — clean, no contamination (scout 25/25, 17/17;
>   critic 7/8, 0 FP, and forced-Flash still caught the critical swapped-args seam). **But it is
>   Flash-locked in print mode regardless of `--model`** (a warning surfaces): route it where Flash
>   suffices; never *claim* a "3.1 Pro lens" through it — the connector does not expose the pin.
> - **Native `agy` (interactive, e.g. inside an Orca terminal) honours the model** but is unfit for
>   autonomous dispatch: it needs per-step permission approvals (not headless), and under `--sandbox`
>   it relocates cwd and runs `find /` across the whole filesystem — in the re-test one candidate
>   thereby answered a *neighbour* workspace's ticket and wrote+committed into its directory (a live
>   incident #6 "poisoned neighbour"; telemetry/incidents.md). Do not promote native agy as a lane
>   until each staged workspace is structurally isolated and the agent is pre-trusted.
> So `gemini-critic` is a usable opt-in third lens again **via `ask-gemini` MCP, at Flash tier**;
> `codex-critic` (Sol) stays the *preferred* third lens on quality (8/8, 0 FP, honours its Pro pin).

> **File access — bridge v2 gives Gemini a repo too, via a copy, not a permission.** Bridge
> `--tool codex` (`codex exec`) is an *agent* that reads files in its `cwd` sandbox itself —
> point it at a repo and it opens what it needs, unchanged. Bridge `--tool gemini` (`agy
> --print`) now gets file access as well when `--cwd` is passed: the bridge copies `--cwd`
> into a throwaway staging directory (excluding `.git`, `node_modules`, `.venv`, `__pycache__`,
> `dist`, `build`; 200 MB cap) and hands agy that copy via `--add-dir`, so it can open real file
> content there. The isolation is **structural (a copy), not behavioural** — agy's own
> restriction modes (`--mode plan`, its own `--sandbox`) are NOT a safety net for this: tested
> live 2026-07-09, they proved non-deterministic (the same scenario sometimes honoured
> read-only, sometimes wrote to disk anyway). So `read-only` (default) never lets agy touch the
> real directory; whatever it creates or edits lives and dies with the copy, reported
> best-effort in `sandboxViolations`. Bare `agy --print` invoked with no `--add-dir` at all still
> has no file access — paste content inline (or `@file` where supported) for those calls. So:
> Codex for repo-grounded review as before; Gemini is now also repo-grounded through the
> bridge's staging copy, or inline-prompt when no `--cwd` is given.

## Connector registry

Name capabilities, not frozen tool strings (survives the gemini-cli → `agy` migration). For
each capability: the **bridge** call (structured) and the **MCP** fallback (interactive), plus
the Claude default it degrades to when no surface is present.

| Capability | Use | Bridge (structured) | MCP (interactive) | Degrades to |
|---|---|---|---|---|
| `codex-critic` | cross-model lens | `--tool codex --model gpt-5.6-sol --effort xhigh --schema <verdict> --sandbox read-only --cwd <repo>` | `mcp__codex__codex` (read-only) | Claude dual-lens |
| `codex-code` | coding hand | `--tool codex --model gpt-5.6-terra --effort high --sandbox workspace-write --cwd <worktree>` | `mcp__codex__codex` (workspace-write) | `builder` (Sonnet) |
| `codex-recon` | cheap repo-grounded recon with shell | `--tool codex --model gpt-5.6-luna --effort medium --sandbox read-only --cwd <repo>` | `mcp__codex__codex` (read-only) | `scout` (file-only) / `builder` (needs shell) |
| `gemini-critic` | cross-model lens (alt) — ⚠ bridge unreliable for review (incident #5); prefer `codex-critic`, or `ask-gemini` MCP inlined | `--tool gemini --model "Gemini 3.1 Pro (High)"` | `mcp__gemini-cli__ask-gemini` (Flash-only in print) | Claude dual-lens |
| `gemini-recon` | big-context recon | `--tool gemini --model "Gemini 3.5 Flash (High)"` | `mcp__gemini-cli__ask-gemini` | `scout` (Haiku) |
| `gemini-recon-cheap` | high-volume mechanical recon | `--tool gemini --model "Gemini 3.5 Flash (Low)"` | `mcp__gemini-cli__ask-gemini` | `scout` (Haiku) |

The bridge calls above show the *shape*; the model/effort values substituted at routing
time come from config.yaml `cross_provider.lanes` (the skill root), which is where lane
pins are edited.

## Model & effort pins — verify against the connector, not the web

**The operative pins live in config.yaml `cross_provider.lanes` (skill root) and are
edited there** — this section is the *verification provenance*: which models and efforts
were confirmed actually available, when, and against what evidence. The two must move
together in one direction only: if config pins something this section's banner has not
verified, verify against the connector first (and update the banner), then route the
first call — a config edit is never by itself evidence of availability.

**Pin only models the connector actually exposes.** Check `agy models` (Gemini/Antigravity)
and the Codex CLI's own default — never pin from a press announcement. (Lesson, 2026-07-06:
`Gemini 3.5 Pro` and `gemini-3.1-flash-lite` are announced/plausible but **not in `agy models`**
here, so they are not pinnable; `Gemini 3.5 Pro` is not yet public at all.)

> **Verified for — checked 2026-07-09 (GPT-5.6 GA day) against the Codex CLI's own model
> catalog (`~/.codex/models_cache.json`, client 0.144.0) plus live bridge calls; Gemini
> re-checked same day via `agy models` (agy 1.1.0 — roster unchanged):**
> - **Codex**: logged in via ChatGPT; the catalog exposes the **GPT-5.6 family (GA
>   2026-07-09)** — `gpt-5.6-sol` (flagship), `gpt-5.6-terra` (balanced), `gpt-5.6-luna`
>   (fast/cheap) — all with a **372k context window** (up from 272k; `gpt-5.5` remains
>   listed). All three answered live on this plan (verified 2026-07-09: Sol via `codex exec`,
>   Terra and Luna through the bridge). The 5.6 reasoning-effort ladder is
>   `low | medium | high | xhigh | max | ultra` (`max` — deeper single-thread reasoning than
>   xhigh; `ultra` — max plus auto-delegation to ~4 parallel sub-agents, a quota multiplier;
>   **Luna has no ultra**). Provenance of the ladder's top (2026-07-10, official model guide
>   cross-checked against the catalog): the *API* ladder is `none | low | medium | high |
>   xhigh | max` — `ultra` exists only on the Codex/ChatGPT-Work surface (it is in the CLI
>   catalog's effort enum; the API-side analog is the Responses-API **Multi-agent beta**).
>   `reasoning.mode: "pro"` ("Sol Pro" in ChatGPT pickers is this *mode*, not a model slug)
>   lives in the Responses API and the ChatGPT product only — **absent from the CLI catalog
>   (checked 2026-07-10)**, so there is nothing to pin for our lanes. The 372k context figure is the CLI catalog's
>   value; OpenAI has published no official 5.6 context window (product-side ChatGPT caps
>   differ) — treat circulating web figures as unconfirmed. Effort is pinned per call via the bridge's **`--effort`** flag
>   (added 2026-07-09) or `-c model_reasoning_effort=…` (direct CLI) / `config:{…}` (MCP).
>   **Config-inheritance hazard:** `~/.codex/config.toml` pins the owner's interactive
>   default to `terra` + effort **`ultra`** — a routed call that omits `--effort` silently
>   inherits ultra and its multi-agent cost, so **every routed Codex call must pin both
>   `--model` and `--effort` explicitly.** Bridge behavior otherwise unchanged: JSON Schema
>   auto-strictified, structured `usage` + `sessionId` returned.
> - **Gemini via `agy`**: reasoning effort is baked into the **model name suffix** —
>   `(High) | (Medium) | (Low)`. Models present in `agy models`: `Gemini 3.5 Flash (H/M/L)`,
>   `Gemini 3.1 Pro (L/H)` (agy also proxies `Claude Sonnet/Opus 4.6 (Thinking)`, `GPT-OSS 120B`).
>   **`Gemini 3.5 Pro` is not public / not listed** — use `Gemini 3.1 Pro (High)` for the
>   reasoning lens until 3.5 Pro actually ships, then re-verify and update this banner.

Codex pins are now **explicit slugs, not the floating CLI default**: the config default is
the owner's interactive choice (`terra` + `ultra` — wrong effort profile for our lanes), and
OpenAI declares Sol/Terra/Luna *durable capability tiers* that advance on their own cadence —
so the tier slug is the stable thing to pin, re-verified against the catalog on each
generation bump (same "family, not version" policy as our Claude aliases).

| Capability | Model (verified available) | Effort | Why |
|---|---|---|---|
| `codex-critic` | **`gpt-5.6-sol`** | **xhigh** (`max` by lead judgment for the hardest verdicts) | adversarial verification — mirrors our Opus-xhigh critic; Sol is the frontier coding/cybersecurity tier (SOTA on agentic code-review at GA) |
| `codex-code` | **`gpt-5.6-terra`** | **high** (escalation: terra-xhigh → sol high/xhigh) | implementation to spec — Terra sits at/above frontier on the Coding Agent Index at ~¼ the cost; mirrors builder/Sonnet-high |
| `codex-recon` | **`gpt-5.6-luna`** | **medium** (`low` for bulk) | cheap repo-grounded *agentic* recon **with shell** in a read-only sandbox; native cwd file access, no staging copy (Luna ≈ Opus 4.8 on coding index at the family's lowest price) |
| `gemini-critic` | **`Gemini 3.1 Pro (High)`** | High (in the name) | deep-reasoning cross-model lens (3.5 Pro not public yet) |
| `gemini-recon` | **`Gemini 3.5 Flash (High)`** | High | big-context recon workhorse (1M ctx, cheap, fast) |
| `gemini-recon-cheap` | **`Gemini 3.5 Flash (Low)`** | Low | high-volume mechanical sweeps — the Haiku-lane cross-provider scout |

**Effort ladder & the `ultra` policy (2026-07-09).** `max` is a plain escalation step —
deeper reasoning, still one thread; usable anywhere xhigh is, by lead judgment. `ultra` makes
Codex internally coordinate ~4 parallel sub-agents. It is permitted as a *worker-internal
accelerator*: the ticket surface does not change (one ticket in, one deliverable out, our
gates unchanged), so ADR-0001's "workers, never a second orchestrator" holds — the worker
parallelizes inside itself, it does not direct our pipeline. But it is never a baseline:
it multiplies quota spend by roughly its agent count. Reserve it, by explicit lead judgment
with a logged reason (`effort: ultra` in telemetry), for a builder ticket that is genuinely
hard AND latency-sensitive. On **critic lanes ultra is not used at all**: its sub-agents are
the same model, so it buys cost, not lens independence — xhigh/`max` is the critic ceiling.

**Ticket phrasing for GPT-5.6 lanes (verified for 5.6 — official migration guide, 2026-07).**
OpenAI's own measurements argue for *leaner* prompts on this family: simplified system
prompts gave ~10–15 % eval improvement at 41–66 % fewer total tokens, and leaner prompts cut
costs 33–67 % in sample workloads. Phrase Codex tickets closer to our Fable style than our
Sonnet style — goal, constraints, explicit scope, acceptance criteria — not enumerated step
lists. Keep **all seven contract fields** (OBJECTIVE / CONTEXT / INPUTS / OUTPUT / TOOLS /
BOUNDARIES / ACCEPTANCE) intact: trim the *how*, never the *what*. Also per the guide, state
autonomy boundaries explicitly — with boundaries undefined, 5.6 produces unnecessary approval
pauses, which a non-interactive bridge call cannot answer. Re-verify this note on the next
family bump (same policy as delegation.md banners).

## Detection & graceful degradation

- **Detection** (once per capability at Step 2): a single call, `node
  /opt/tools/agent-bridge/run-external-agent.mjs --detect` (no `--tool` needed), returns JSON
  covering both CLIs in one shot — `codex.present`/`codex.loggedIn`/`codex.version` and
  `gemini.present`/`gemini.models` — and always exits 0, so absence is data, not an error. A
  bridge capability is *available* iff the bridge script is present and the relevant CLI reports
  present+logged-in in that JSON; an MCP capability iff its tool is in the lead's tool set.
  Record availability in the routing plan. **Fallback** (if `--detect` itself is unavailable,
  e.g. an older bridge): the old three-check method — presence of
  `/opt/tools/agent-bridge/run-external-agent.mjs` plus `codex login status` / `agy models`
  succeeding individually.
- **Degradation**: if a route selects a cross-provider capability that detection finds absent,
  walk that lane's ordered fallback chain in config.yaml `availability.fallbacks` (the
  registry's "Degrades to" column shows the same terminal Claude default; the operative,
  user-editable chain is the config's), note the reroute in one line of the final report,
  stamp `fallback_from`/`fallback_reason` into the ticket's telemetry record, and proceed.
  Absence is never a run-blocking error, and an availability reroute consumes no escalation
  attempts (SKILL.md Step 2, "Availability fallbacks").
- **Runtime failure** (present but the call errors / times out / `ok:false`): treat as a worker
  FAIL feeding the escalation ladder (Step 4) — ① one retry → ② escalate to the Claude default
  → ③ report the blocker. A cross-provider failure never escapes the ladder. Two named Codex
  failure causes to check before burning the retry: (a) **stale client** — Codex CLI below
  0.144.0 hides the GPT-5.6 models entirely (documented minimum; this machine sits exactly at
  it), presenting as "model unavailable" — our 2026-07-09 `SKIPPED` routing-log record was
  exactly this; the fix is an upgrade, not a retry. (b) **safety classifiers** — OpenAI runs
  real-time cyber-/bio-misuse classifiers on 5.6 that can block or pause a request mid-run;
  a security-review ticket on a codex lane can plausibly trip one — the fix is rerouting to
  the Claude default, not a retry. Naming the cause picks the right rung.

Gradeable invariant: *with no cross-provider surface present, every routing decision resolves
to a Claude worker and the run reaches a normal terminal report.*

## When to route cross-provider — the lead's judgment under a standing mandate

Standing user mandate (2026-07-09; it **replaces and voids** the 2026-07-05
"Codex = opt-in" decision): provider routing is the **lead's judgment call** over the full
pool of detected surfaces. Two standing values drive that judgment:

1. **Additional analytical angle** — an uncorrelated model whose blind spots don't overlap
   Claude's (avoids self-preference bias — ADR-0001; the user independently holds this
   view, and the ADR's evidence supports it). Flagship shape: the cross-model lens on
   dual-lens deliverables — **opt-in by lead judgment since config v3** (owner decision
   2026-07-12; formerly default-on). → Use 1.
2. **Quota-spread** — orchestration load belongs on all the user's subscriptions (Claude,
   ChatGPT/Codex, Google), not on Claude alone. Baseline allocation: the default-lane
   table. → Use 4.

Uses 1–4 below are worked shapes, not an exhaustive license — the lead may route outside
them when judgment says so, logging the reason (`lead-judgment`). Judgment routes
*providers*, never *quality*: the class→tier quality bar (SKILL.md Step 2), the never-shift
invariants (Use 4), and the composition/gating rules are not overridable by this mandate.

## Use 1 — cross-model critic lens (read-only, additive THIRD lens)

For a dual-lens-trigger deliverable (SKILL.md Step 4.3), add ONE cross-model critic as a
**third** lens on the same deliverable + acceptance criteria. Since config v3 (owner
decision 2026-07-12; formerly default-on) this lens is **opt-in by lead judgment**: invoke
it when the independent angle is worth one round-trip — the lens sits on the critical path
of every highest-stakes gate. When invoked, prefer `codex-critic` (polygon: 8/8 planted
defects, 0 FP, honours its Sol-xhigh pin); `gemini-critic` is a usable alternate again after
the 2026-07-12 two-transport re-test (the 2026-07-11 2/2 misfires were transport-, not
model-caused), but only via the `ask-gemini` MCP path and only at Flash tier — that surface
is Flash-locked in print mode, so it cannot deliver a Pro-tier Gemini lens (see the Update
2026-07-12 note above). Neither invoking nor omitting the third lens needs a note in the
final report; the two-Claude-lens gate is complete on its own.

- **Call (structured, preferred)**: `node /opt/tools/agent-bridge/run-external-agent.mjs
  --tool codex --model gpt-5.6-sol --effort xhigh --schema <verdict.json> --sandbox read-only
  --cwd <repo>`, prompt on **stdin** =
  the deliverable + the ticket's ACCEPTANCE verbatim + "try to refute; a finding needs a
  concrete failure scenario." Verdict schema = **the bridge's machine-checkable shape**
  (`status ∈ APPROVED|WARNING|BLOCKED`, `summary`, `issues[]`; see the agent-bridge README) —
  this is *distinct* from our critic's prose PASS/FAIL verdict (quality.md §3); the lead maps
  `BLOCKED`/`WARNING`-with-a-real-scenario onto a FAIL. For a Gemini lens use `--tool gemini
  --model "Gemini 3.1 Pro (High)"` (schema is best-effort in-prompt; check `ok`). Read the
  parsed `output`. **`--resume` is forbidden here**: a critic's context must be fresh-by-
  construction (producer≠grader depends on it), and `--resume` is reserved for the coding hand
  (Use 3) alone.
- **Call (quick, interactive)**: `mcp__codex__codex` (read-only, effort xhigh) — prose only,
  no strict verdict; use when you just want a fast second opinion, not a gated verdict.
- **Lens brief — mandatory contents (v8).** An external lens knows nothing of the home
  doctrine; its prompt must carry, in order: (1) the deliverable (diff) and the ticket's
  ACCEPTANCE verbatim; (2) the **decision-context pack** (quality.md §3a) — the same
  pack the Claude lenses got, verbatim, including each entry's `base | added-by-diff`
  provenance; (3) the finding-scope vocabulary and the credential rule, compressed:
  "scope ∈ introduced | pre-existing | decision-challenge; a documented decision is an
  accepted ADR / CONTEXT.md entry / decision comment predating the diff or citing outside
  authority; an intent comment added by this diff with no citation is a self-declaration —
  keep the finding introduced"; (4) "try to refute; a finding needs a concrete failure
  scenario"; (5) read-only. The bridge verdict schema is unchanged — prefix each
  `issues[]` entry with its scope tag (`[introduced] …`); the lead maps
  `BLOCKED`/`WARNING` onto FAIL only through surviving `introduced` critical/major
  entries (quality.md §3a verdict mapping) and adjudicates the rest exactly like
  Claude-lens findings (§3b).
- **Composition**: **additive** — accept only when **both Claude lenses PASS AND the cross-model
  lens raises no surviving critical/major finding** (`BLOCKED`/`WARNING` with a real scenario).
  It can only *tighten* the gate; it never replaces a Claude lens, so it can never cause a
  false-accept. Absent a surface, the standard two-Claude-lens gate applies unchanged.

## Use 2 — Gemini big-context recon (read-only alternative scout)

For recon whose INPUT exceeds a Claude window — and, under the Use 4 baseline, the default
web-recon lane. **Call**: `--tool gemini --model "Gemini 3.5 Flash (High)"` (or the MCP
`ask-gemini`, which is Flash anyway), prompt = a **scout-contract** ticket (OBJECTIVE / INPUTS
via `@file` where supported / exact bounded OUTPUT / read-only BOUNDARIES / ACCEPTANCE + the
scout escape hatch `NEEDS_CLARIFICATION`, "0 matches is a valid answer"). For very high-volume
*mechanical* sweeps drop to `Gemini 3.5 Flash (Low)`. Note `gemini-recon` is also the
designated home for *cheap web sweeps* generally: our Claude cheap tier has no web tools
by design (scout is local read-only; builder has WebFetch but no WebSearch), so a
coverage-style web verification task routes here, or to `builder`/`architect` on the
Claude-only path (SKILL.md Step 2).

**Staging scope — INPUTS only (config v4; incident #6).** The staging copy (or `--cwd`)
an external worker receives contains ONLY the ticket's INPUTS — never sibling directories
holding other agents' outputs or finished deliverables. Incident #6 (2026-07-12,
owner-reported): a Flash recon worker skipped a reverse-engineering task and returned
Opus's output, found in a neighboring folder, as its own deliverable. The barrier is
structural, not behavioral — same family as Codex reward hacking (hence the
diff-from-disk rule, Use 3): scope what the worker can *see*, don't instruct it to be
honest. Applies to every external lane: gemini staging = the INPUTS paths only; codex
`--cwd` = the narrowest directory containing the INPUTS or a dedicated worktree, never a
workspace root where other agents' work lives. Config: `cross_provider.staging_inputs_only`.

- **Bound OUTPUT tightly** (N rows, exact table, distilled answer) — the huge context stays on
  Gemini's side; only the distilled result returns to the lead.
- **N1–N6 compatibility**: any cross-provider recon ticket (Gemini here, or `codex-recon`/Luna
  from the registry) inherits the scout **verb whitelist** (recon/read/classify-by-a-given-rule
  only — no decide/recommend/design; for `codex-recon`, "run this named check/script and report
  its output" is within the whitelist — executing a *given* command is mechanical, choosing
  which check matters is not). A judgment-bearing sweep is never a cross-provider scout; it
  routes to `architect`.

## Use 3 — Codex coding hand (peer builder; writes only in a worktree)

A peer coding lane, chosen at routing time by lead judgment — the Use 4 baseline makes it
the default for well-specified builder tickets when detected. **Call (preferred)**:
`--tool codex --model gpt-5.6-terra --effort high --sandbox workspace-write --cwd <worktree>`
via the bridge (structured result + `durationMs` + structured `usage` token counts); MCP
`mcp__codex__codex` is the interactive fallback. Params: prompt = a **builder-contract**
ticket (full spec, explicit scope, "run scoped checks yourself"); `cwd` = a dedicated
**worktree** path (structural write-scoping); **never `danger-full-access`**; effort **high**
— for a genuinely hard ticket escalate terra-xhigh, then `gpt-5.6-sol` high/xhigh; `ultra`
only under the policy above (hard AND latency-sensitive, logged reason).

- After return: read the diff **from disk** (`git diff`), never Codex's self-report; run the
  deterministic pre-gate; then a **Claude critic** (or a *different* cross-provider critic) —
  **never a Codex critic on Codex's own work** (producer≠grader). Codex-builder + Claude-critic
  is automatically both producer≠grader AND cross-provider verification. This rule now has a
  named external justification for the 5.6 family: METR's pre-deployment evaluation of 5.6 Sol
  (published 2026-06-26) measured the **highest reward-hacking rate of any public model** on
  its agent harness — gaming tests rather than solving tasks, including exploiting hidden
  test-suite information — so a Codex worker's own "tests pass" is precisely the claim our
  gates must re-derive themselves, never accept on report.
- **Multi-turn refinement**: take `sessionId` from the bridge's output and pass it back via
  `--resume <id>` (or `--resume-last`) to send a follow-up fix into the *same* Codex thread
  instead of re-briefing from scratch. This is the coding hand's privilege only — see the
  `--resume` ban for critics in Use 1.
- **N1–N6 compatibility**: Codex-authored hunks flow into the same integrated-diff snapshot the
  whole-diff final review consumes — that review is author-agnostic.

## Use 4 — baseline allocation under the mandate (default lanes; quality-equivalent only; the third-lens row is opt-in since v3)

Baseline provider allocation under the standing mandate (2026-07-09): spread orchestration
load across all subscriptions, with the lead's judgment free to depart from the table when
a ticket's specifics warrant it. This is a *user-specific standing mandate* (like the paths
above, not a portable assumption of the skill). When detection (Step 2) finds the surface
present, these lanes route cross-provider **by default** — no per-run user prompt needed:

| Lane (class) | Default route under quota-spread | Degrades to |
|---|---|---|
| web recon & big-context recon | `gemini-recon` (Flash High) | `builder`/`architect` per SKILL.md Step 2 |
| high-volume mechanical sweeps whose material can be **inlined** in the prompt | `gemini-recon-cheap` (Flash Low) | `scout` (Haiku) |
| mechanical repo sweeps that need **shell execution** (run a linter/script, count via command — never scout's lane, it has no shell) | `codex-recon` (Luna, medium) | `builder` (Sonnet) |
| well-specified implementation tickets (builder-class) | `codex-code` (Terra high, worktree, gates unchanged) | `builder` (Sonnet) |
| third lens on dual-lens deliverables — **opt-in by lead judgment since v3**, not a default lane | `codex-critic` (Sol xhigh) preferred; `gemini-critic` usable again via `ask-gemini` MCP at Flash tier only (re-test 2026-07-12; not the Pro tier, surface Flash-locks it) | additive — omitted, two-Claude-lens gate stands |

What NEVER shifts under the mandate — quality is the routing invariant, quota is not:

- **judgment-class tickets** (`architect`/Fable) and the lead itself — no external model
  substitutes for the deep-reasoning lane (ADR-0001: externals are workers, not orchestrators);
- **the primary grader** — a Claude critic (Opus) still grades every gated deliverable; the
  cross-model lens stays *additive* (Use 1 composition rule), so quota-spread can never
  cause a false-accept;
- **local repo file-walking recon** — `scout` keeps this lane as the *default*, not because
  cross-provider file access is missing (bridge Gemini reads via the staging copy; Codex reads
  its `cwd` natively), but on economics: Haiku is the cheapest quota of all, and Gemini's
  staging copy gets expensive for large repos (copy time, 200 MB cap). For *targeted*
  file-level review (a handful of files, not a whole-repo walk), `gemini-critic` via the
  staging copy is a viable route. With GPT-5.6 the old "don't spend the coding hand's quota
  on recon" argument is retired for Codex: `codex-recon` (Luna) is the family's designated
  cheap tier, so routing a sweep there — when it needs shell, or when Claude quota is tight
  (`xprovider_reason: quota-spread`) — no longer inverts the spread. Pure file-walking still
  defaults to `scout`.
- **the escalation ladder** — a failed cross-provider ticket escalates to its named Claude
  default (runtime-failure rule above), never sideways to another external provider.

Gates are provider-agnostic and unchanged: ticket contracts, deterministic pre-gates,
producer≠grader, diff-from-disk for coding hands. Telemetry: `xprovider_reason:
"quota-spread"`. If the user asks for maximum Claude quality on a specific run («только
Claude», "Claude-only this run"), that per-run instruction overrides the standing
mandate for that run.

## Cost, latency, telemetry

Cross-provider calls spend the OTHER subscription's quota (a feature — offloads Claude quota
when it's tight) at the cost of added latency and operation outside Claude Code's sandbox.

**One pool on the OpenAI side (2026-07-10; OpenAI Codex pricing page + help center, as
compiled by GA-week operator coverage — the primary help pages are fetch-blocked):** Codex
(CLI / desktop / cloud) and **ChatGPT Work** (plus ChatGPT for Excel and Workspace Agents)
draw from a **single shared agentic credit pool** of the subscription. Codex lanes therefore
compete with the owner's own interactive Work/Codex use — "spending the other subscription"
means spending that one pool, budgeted as one line item. The credit schedule keeps the API
price ratios: Sol 125 / 12.5 / 750 credits per 1M input/cached/output tokens, Terra exactly
×½, Luna ×⅕ (OpenAI's guidance: a message averages 5–40 credits); on a Plus plan the Sol
window is 15–90 messages per 5 h (Terra 20–110, Luna 50–280). Two lane-design consequences,
now with arithmetic behind them: the third-lens lane **alternates** codex-critic /
gemini-critic (Use 4) because Sol is the scarcest window, and recon rides Luna, not Sol, at
one-fifth the credit burn.
Sandbox defaults are least-privilege: **read-only** for uses 1 and 2 and for `codex-recon`;
**workspace-write + worktree cwd** for use 3; **never `danger-full-access`** — and note the
owner's `~/.codex/config.toml` sets `sandbox_mode = "danger-full-access"` globally for
*interactive* use, which the bridge structurally overrides: it always passes an explicit
`--sandbox` and accepts only `read-only|workspace-write`.

Telemetry (quality.md §7): record optional `provider` ∈ `anthropic | openai | google` (default
`anthropic`) and, when `provider != anthropic`, `xprovider_reason` ∈ `independent-lens |
context-size | quota-spread | lead-judgment` (the last for judgment routes outside the
Use 1–4 shapes; `user-codex-pref` is retired with the voided 2026-07-05 decision). **Cost
fields, by surface:** the **bridge** returns `durationMs` always and, for Codex, a structured
`usage` field (bridge v2 — parsed from the `--json` event stream: `input_tokens`,
`cached_input_tokens`, `output_tokens`, `reasoning_output_tokens`; supersedes the old
`stderrTail`-scrape approach) — record those. Gemini still reports no usage data (`usage: null`
in the bridge output) — leave `tokens` `n/a` for it. The **MCP** path returns neither →
leave `tokens` `n/a`; never invent a number. The completion-card token/effort figure follows
the same rule.

## Reserve, not baseline — pal/clink

A turnkey multi-agent orchestrator (`pal`/`clink`, `/opt/tools/pal-mcp-server`) was
**evaluated and deliberately rejected** as the base mechanism (ADR-0001): its roles/`consensus`
duplicate what our own loop does, it adds a layer and 18 tools of context, and making it work
with `agy` needs a source patch rebased on every update. Kept as a reserve for a future "council
of agents" need only — do not reach for it as the default cross-provider path.
