# Cross-provider: non-Claude workers (Codex, Gemini/Antigravity, Grok, Kimi)

Source of truth for the cross-provider layer. Portability floor: with **no cross-provider
surface present**, every routing decision resolves to a Claude worker and the run completes
normally — the skill degrades to self-contained Claude-only. When surfaces ARE present,
non-Claude workers are **regular members of the routing pool**, routed by the lead's judgment
under the standing mandate below — not an exception layer. Judgment-driven, never silent:
every non-Claude route carries a named reason into telemetry.

> **ONE INVOCATION MODEL (v26, owner 2026-07-22): every external lane is invoked by its own
> VENDOR CONSOLE UTILITY (CLI).** There is no bridge and no MCP server in the lane path any
> more — "only codex went a unique route" is gone; codex now runs through its CLI like
> everyone else. Each lane declares its `transport` in config: `codex-cli`, `grok-cli`,
> `agy-print` (Gemini + `agy-opus`), `kimi-cli-headless`. Claude workers are **NOT** in this
> model (owner decision): they run natively through the harness Agent tool — worktree,
> telemetry, context inheritance — and `claude -p` is only their portability fallback on a
> non-Claude host (ADR-0005, §Claude-workers).
>
> **The bridge `/opt/tools/agent-bridge` is retired from this project.** It is no longer
> referenced or depended on: its gemini path was broken in code and it never handled
> grok/kimi/claude. Codex's structured-verdict capability (`codex exec --output-schema`) is
> reached by calling the codex CLI directly — the same thing the bridge wrapped. The bridge's
> own repo lives elsewhere in `/opt/tools`; its fate is outside this project.
>
> **Interim (ADR-0005, slices 3–5):** the drift-proof executor `tools/run-lane` (one argv,
> artifact-from-disk, model-verification) is not written yet. Until it lands, an external lane
> is invoked by the lead per its documented CLI form below — exactly as grok/kimi already are
> today. Not a regression: codex is brought to the same state, all get the executor later.
>
> Historical notes further down that mention "the bridge" or "MCP as a Gemini path" are
> **pre-v26 context, not current instruction** — the current path is always the vendor CLI.

Design rationale — including *why external agents are workers/cross-reviewers and never a
second orchestrator* — lives in **`docs/adr/0001-external-agents-as-workers.md`** (preserved
into this repo 2026-07-22 when the bridge project was deleted; the reasoning is canon even
though its bridge/MCP *mechanism* is retired, superseded by run-lane + vendor-CLI).
Our own decision record for the invocation model is **`docs/adr/0005-lane-invocation-unification.md`**.

> **We reference, we do not vendor.** orchestrate is pure Markdown for its METHOD, but its own
> thin tools (`validate_config.py`, `telemetry_append.py`, and the coming `run-lane`) live in
> `tools/` and travel with the repo — that is what makes the skill portable (ADR-0005, F1). We
> do not copy in foreign code: vendor CLIs are **capability-detected by presence** — if the CLI
> exists and reports logged-in, its lane is available; else that lane falls back down its chain
> to a Claude worker. Paths are environment-specific, not a portable assumption.

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

## One path — the vendor's console utility

A cross-provider "worker" is **the lead invoking an external CLI from the main loop** (only
the lead has that access) with our ticket in the prompt, then grading the return with our
normal gates. The external model's context is fresh by construction (it sees only the prompt),
so producer≠grader and fresh-context independence hold automatically.

**There is one surface per lane: its vendor console utility** (v26). Not two, not a
bridge-or-MCP choice — the drift that framing invited is exactly what cost a day. Each lane's
`transport` in config names the CLI:

| Lane | Console utility | Structured return |
|---|---|---|
| `codex-*` (critic/code/recon) | `codex exec` — `--output-schema` gives a strict `APPROVED/WARNING/BLOCKED` verdict, `--json` gives usage + session id | machine-checkable verdict + usage |
| `grok-build` / `composer-build` | `grok -p` — `--json-schema`, `--output-format` | schema-checkable |
| `gemini-*` / `agy-opus` | `agy` — see the mandatory-form box below; no strict schema (emulated by prompt, reported as `prompt_asked`) | prose/artifact + journal witness |
| `kimi-k3` | `kimi -p` — `--output-format stream-json` | text/stream |

**MCP is not a lane transport.** `mcp__codex__codex` survives only as an ad-hoc "ask a second
opinion" outside the gate and outside these lanes; `gemini-mcp-tool` is removed entirely (it
hardcodes `supportsModelSelection: false` and silently drops the requested model — verified
2026-07-21 as serving `Gemini 3.1 Pro (High)` while config claimed Flash). Strict verdicts and
model guarantees come from the CLI directly, never from an MCP wrapper.

*(Historical: a `codex exec --output-schema` verdict was verified live 2026-07-06; the same
run's bridge-Gemini form `agy --print --model …` is broken — kept below only as the worked
example of the flag-order trap.)* That broken form — `--print`/`-p` is
not a boolean, it consumes the next token as the prompt, so this invocation sent the prompt
`--model` and never selected a model at all. The correct form is
`agy --model "<name>" [--effort <level>] --add-dir <abs-path> --print-timeout 5m -p "<prompt>" < /dev/null`
— flags before `-p`, prompt last, stdin explicitly closed or the CLI hangs waiting on input, and
**`--add-dir` is mandatory whenever the agent must read files**: `cd <dir> && agy …` does *not*
place the agent there (v24, 2026-07-21). The log shows `workspaceDirs` set correctly while the
agent's tools sit in agy's own scratch — so it cannot find the material and starts searching the
filesystem for it. That, not misbehaviour, is what produced incident #6 (`find /` into a sibling
workspace): the agent was hunting for materials it had not been given. Reproduced on three
different models in a row (Gemini Flash, Claude Opus 4.6, gpt-oss) — a harness property, not a
model one. Verified
live 2026-07-21: an unknown model name is now rejected with the available list, and
`"Gemini 3.6 Flash (Low)"` produced 357 words in 5.5s. The MCP `ask-gemini` path, by contrast,
reports itself model-locked to Flash in print mode — plausibly because that wrapper builds the
same malformed command; unverified either way, so treat it as Flash-only until re-tested. Fine
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
> - **Cross-model lens (the Standards axis since v11/ADR-0002) → prefer `codex-critic` (Sol)**
>   over `gemini-critic` when the only Gemini path is the bridge; Sol returned a clean read-only
>   findings list (8/8) under `sandbox: read-only` with no violations.
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
> **v27/v28 update:** the Standards axis is now an **equal pool**, not a single preferred lane —
> `availability.fallbacks.standards-lens: equal:[gemini-flash, codex-critic, grok-4.5, kimi-k3]`,
> rotated by least-dispatched-this-run, all non-Claude by construction, terminal `sonnet-inline-note`.
> There is no single-default standards lane anymore — the former cross-provider defaults
> standards key is dissolved into the chain;
> `codex-critic`'s 8/8-0FP record is provenance for its pool membership, not a preference over peers.

> **File access — per CLI, directly (v26; the bridge's staging-copy scheme is retired).**
> `codex exec` is an *agent* that reads files in its `--cwd` sandbox itself — point it at a
> worktree and it opens what it needs. `agy` gets file access through `--add-dir <abs>`
> (mandatory — `cd` alone does not place the agent there, see the flag box above); it opens
> real content in that directory. For lanes constrained to inlined material
> (`requires_pregate_output_inline`, e.g. `gemini-critic`), the lead pastes the deliverable
> and pre-gate output into the prompt and the lane does **not** walk the tree. Isolation for
> writing lanes is the dedicated worktree plus each CLI's own `--sandbox`/hardening profile
> (grok/kimi: `grok_hardening`/`kimi_hardening`, fail-closed), not a copy. Take the artifact
> from the written file (`--out`/the file the agent wrote), never from truncated print.

## Connector registry

Name capabilities, not frozen tool strings. For each lane: the **console-utility (CLI)** call
and the Claude default it degrades to when no surface is present. One column, one path — the
vendor CLI (v26). No bridge column, no MCP column.

| Lane | Use | Console utility (CLI) call — shape | Degrades to |
|---|---|---|---|
| `codex-critic` | cross-model lens — a Standards-axis route (fixed gate member, not opt-in) | `codex exec --model gpt-5.6-sol -c model_reasoning_effort=xhigh --output-schema <verdict> --sandbox read-only --cwd <repo>` | `standards-lens` chain → `sonnet-inline-note` |
| `codex-code` | coding hand | `codex exec --model gpt-5.6-terra -c model_reasoning_effort=high --sandbox workspace-write --cwd <worktree>` | `builder` (Sonnet) |
| `codex-recon` | cheap repo-grounded recon with shell | `codex exec --model gpt-5.6-luna -c model_reasoning_effort=medium --sandbox read-only --cwd <repo>` | `scout` (file-only) / `builder` (needs shell) |
| `gemini-critic` | cross-model lens (alt) — Standards-axis fallback; judges by inlined material only (`requires_pregate_output_inline`) | `agy --model "Gemini 3.6 Flash (High)" --add-dir <abs> --print-timeout 5m -p "<prompt>" < /dev/null` | `standards-lens` chain → `sonnet-inline-note` |
| `gemini-recon` / `gemini-flash` | big-context recon | `agy --model "Gemini 3.6 Flash (High)" --add-dir <abs> -p "<prompt>" < /dev/null` | `scout` (Haiku) |
| `agy-opus` | frontier-Claude on Google quota | `agy --model claude-opus-4-6-thinking --add-dir <abs> -p "<prompt>" < /dev/null` | `opus` (Anthropic quota) |
| `grok-build` | fast builder/recon/judge | `grok -p --model grok-4.5 --json-schema <verdict> --sandbox <orchestrate> --cwd <worktree>` | `builder` / `codex-code` |
| `kimi-k3` | 1M-context builder/judge | `kimi -p -m k3 --add-dir <abs> --output-format stream-json` | `sonnet` / `codex-code` |

The calls above show the *shape*; the model/effort values substituted at routing time come
from config.yaml `cross_provider.lanes` (the skill root), where lane pins — and each lane's
`transport` (the CLI name) — are edited. `--add-dir <abs>` is mandatory for any lane that must
read files (see the flag box above); the artifact is taken from `--out`/the written file, never
from truncated print.

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
>   differ) — treat circulating web figures as unconfirmed. Effort is pinned per call via
>   `codex exec … -c model_reasoning_effort=<level>` on the codex CLI (there is **no** `--effort`
>   flag on `codex exec` — verified on the live CLI 2026-07-22; the drift this doc exists to prevent).
>   **Config-inheritance hazard:** `~/.codex/config.toml` pins the owner's interactive
>   default to `terra` + effort **`ultra`** — a routed call that omits `--effort` silently
>   inherits ultra and its multi-agent cost, so **every routed Codex call must pin both
>   `--model` and `--effort` explicitly.** Bridge behavior otherwise unchanged: JSON Schema
>   auto-strictified, structured `usage` + `sessionId` returned.
> - **Gemini via `agy`**: reasoning effort is baked into the **model name suffix** —
>   `(High) | (Medium) | (Low)` — or, equivalently, the base slug plus a separate `--effort`
>   flag (both verified 2026-07-21). Models present in `agy models` as of 2026-07-21:
>   `Gemini 3.6 Flash (H/M/L)`, `Gemini 3.5 Flash (H/M/L)`, `Gemini 3.1 Pro (L/H)` (agy also
>   proxies `Claude Sonnet/Opus 4.6 (Thinking)`, `GPT-OSS 120B` — do not route Claude through
>   a third-party harness, see the vendor policy map above).
>   **Standing owner rule (2026-07-21): always the latest Flash, and no Gemini Pro anywhere.**
>   Every Gemini lane therefore pins `Gemini 3.6 Flash (High)`; when a newer Flash ships,
>   update all four lanes at once. Cost accepted knowingly: Pro's 2M window is gone, so
>   oversized reads and diffs get sliced.

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
| `gemini-critic` | **`Gemini 3.6 Flash (High)`** | High (in the name, or `--effort high`) | cross-model lens. v23 (owner, 2026-07-21): **Pro is not used anywhere** — latest Flash replaces it. The lane's original point was Pro's 2M window for large diffs; that is gone, so oversized reviews get sliced. |
| `gemini-recon` | **`Gemini 3.6 Flash (High)`** | High | big-context recon workhorse (cheap, fast) |
| `gemini-recon-cheap` | **`Gemini 3.6 Flash (High)`** | High (raised from Low, owner 2026-07-21 — Low economised a quota that is not scarce) | high-volume mechanical sweeps — **the scout default since v23**, ahead of Haiku |

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

- **Detection** (once per lane at Step 2, v26 — per-CLI, no bridge): probe each vendor console
  utility directly by presence + logged-in. A lane is *available* iff its `transport` CLI is on
  PATH and reports authenticated: `codex login status` (codex-*), `agy models` (gemini-* /
  `agy-opus` — a non-empty catalog also confirms the Google surface), `grok --version` +
  its login check (grok-*), `kimi --version` + its login check (kimi-k3). Each probe is cheap,
  local, and does not spend model quota; absence is data, not an error. Record availability in
  the routing plan. (Future `tools/run-lane --detect` will fold these into one call, ADR-0005 —
  until then probe per lane.)
- **Degradation**: if a route selects a cross-provider capability that detection finds absent,
  walk that lane's ordered fallback chain in config.yaml `availability.fallbacks` (the
  registry's "Degrades to" column shows the same terminal Claude default; the operative,
  user-editable chain is the config's), note the reroute in one line of the final report,
  stamp `fallback_from`/`fallback_reason` into the ticket's telemetry record, and proceed.
  Absence is never a run-blocking error, and an availability reroute consumes no escalation
  attempts (SKILL.md Step 2, "Availability fallbacks").
- **Runtime failure** — v20 splits this by TYPE (config `availability.midflight`):
  - **Transport death** (nonzero transport exit, `ok:false` of transport class, empty/truncated
    stream, timeout with no output, sudden login prompt) is an AVAILABILITY event, NOT a quality
    FAIL: it does not burn `max_attempts_per_subtask`; it re-dispatches the same ticket on a
    fresh worktree per the role's fallback chain and feeds the provider breaker (`availability.breaker`).
    Ambiguity (a report exists but carries transport artefacts) resolves to quality.
  - **Content failure** (transport intact, a usable-but-wrong report) is a worker FAIL feeding the
    escalation ladder (Step 4) — ① one retry → ② escalate to the default → ③ report the blocker.
  A cross-provider content failure never escapes the ladder. Two named Codex failure causes to
  check before burning the retry: (a) **stale client** — Codex CLI below
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
   view, and the ADR's evidence supports it). Flagship shape: the cross-model lens is now
   the **Standards axis of the fixed three-lens gate** — permanent, not opt-in (ADR-0002 /
   config v11–v12; formerly an opt-in third lens bolted onto a dual-lens gate, v3–v10). →
   Use 1.
2. **Quota-spread** — orchestration load belongs on all the user's subscriptions (Claude,
   ChatGPT/Codex, Google), not on Claude alone. Baseline allocation: the default-lane
   table. → Use 4.

Uses 1–4 below are worked shapes, not an exhaustive license — the lead may route outside
them when judgment says so, logging the reason (`lead-judgment`). Judgment routes
*providers*, never *quality*: the class→tier quality bar (SKILL.md Step 2), the never-shift
invariants (Use 4), and the composition/gating rules are not overridable by this mandate.

## Use 1 — Standards lens: the fixed gate's permanent cross-model angle

Every gated deliverable runs the fixed three-lens gate (`gates.lenses`, ADR-0002): Standards,
Spec, критик (SKILL.md Step 4). The Standards lens's whole point is a **non-Claude executor**
closing the self-preference-bias gap (CONTEXT.md, term "Self-preference bias") — since
config v11–v12 this is a **permanent member of the gate**, not an opt-in extra bolted onto a
dual-lens gate (that mechanism, v3–v10, no longer exists). the axis is an **equal pool** `availability.fallbacks.standards-lens:
equal:[gemini-flash, codex-critic, grok-4.5, kimi-k3]` (rotated least-dispatched, terminal
`sonnet-inline-note`) — no single default lane
(config.yaml). Prefer `codex-critic` (polygon: 8/8 planted defects, 0 FP, honours its
Sol-xhigh pin) as the primary; `gemini-critic` is a usable stand-in after the 2026-07-12
two-transport re-test (the 2026-07-11 2/2 misfires were transport-, not model-caused), but
only via the `ask-gemini` MCP path and only at Flash tier — that surface is Flash-locked in
print mode, so it cannot deliver a Pro-tier Gemini lens (see the Update 2026-07-12 note
above). The chain's terminal is **not a skip**: `skip-third-lens` (the generic `codex-critic`/
`gemini-critic` lane chains above) doesn't apply to this axis, because a gate-fixed lens
cannot be dropped from the gate. `sonnet-inline-note` — when both external surfaces are
unavailable, the lead executes the Standards question itself with a Claude model (`sonnet`)
inline in the main loop, and notes the reroute in one line of the final report. Every call of
this axis stamps `xprovider_reason: independent-lens` in telemetry (quality.md §7) — the
standing label for mandate value 1 above, distinct from `context-size`/`quota-spread`/
`lead-judgment`.

### Smell baseline — delivered in the prompt, every call

The lane has no filesystem access to the `code-review` skill (or any skill file) — so the
Standards lens's prompt must carry the full smell baseline below on **every** invocation, not
a reference to it. Source: the `code-review` skill (Matt Pocock skills,
`engineering/code-review`), which in turn cites Martin Fowler, *Refactoring*, ch. 3.

Each smell reads *what it is* → *how to fix*; match it against the diff:

- **Mysterious Name** — a function, variable, or type whose name doesn't reveal what it does
  or holds. → rename it; if no honest name comes, the design's murky.
- **Duplicated Code** — the same logic shape appears in more than one hunk or file in the
  change. → extract the shared shape, call it from both.
- **Feature Envy** — a method that reaches into another object's data more than its own. →
  move the method onto the data it envies.
- **Data Clumps** — the same few fields or params keep travelling together (a type wanting to
  be born). → bundle them into one type, pass that.
- **Primitive Obsession** — a primitive or string standing in for a domain concept that
  deserves its own type. → give the concept its own small type.
- **Repeated Switches** — the same `switch`/`if`-cascade on the same type recurs across the
  change. → replace with polymorphism, or one map both sites share.
- **Shotgun Surgery** — one logical change forces scattered edits across many files in the
  diff. → gather what changes together into one module.
- **Divergent Change** — one file or module is edited for several unrelated reasons. → split
  so each module changes for one reason.
- **Speculative Generality** — abstraction, parameters, or hooks added for needs the spec
  doesn't have. → delete it; inline back until a real need shows.
- **Message Chains** — long `a.b().c().d()` navigation the caller shouldn't depend on. → hide
  the walk behind one method on the first object.
- **Middle Man** — a class or function that mostly just delegates onward. → cut it, call the
  real target direct.
- **Refused Bequest** — a subclass or implementer that ignores or overrides most of what it
  inherits. → drop the inheritance, use composition.

Two rules bind the baseline, same as the skill's own copy: **the repo overrides** (a
documented repo standard always wins; where it endorses something the baseline would flag,
suppress the smell), and **always a judgement call** (each smell is a labelled heuristic —
"possible Feature Envy" — never a hard violation; skip anything tooling already enforces).

**Sync obligation.** This is a verbatim copy, not an independent restatement — the canon lives
at `/root/.claude/skills/code-review/SKILL.md`, the sole upstream since the claude/codex skill
registries were unified (`/root/.codex/skills/code-review` is a symlink onto the claude copy,
slice 7, issue #11). A change to the canon's twelve-smell list must land here in the same
edit; this copy does not get to drift on its own schedule. Check it at every self-review of
this doctrine: diff the normalized text of the twelve entries above against the canon — a
mismatch is a defect in this file, never in the canon.

### Calling the axis

- **Call (structured, preferred) — primary lane `codex-critic`**: `codex exec --model
  gpt-5.6-sol --effort xhigh --output-schema <verdict.json> --sandbox read-only --cwd <repo>`,
  prompt on **stdin** = the deliverable + the ticket's ACCEPTANCE verbatim + "try to refute; a
  finding needs a concrete failure scenario." Verdict schema = the CLI's machine-checkable
  shape (`status ∈ APPROVED|WARNING|BLOCKED`, `summary`, `issues[]`) — *distinct* from our
  critic's prose PASS/FAIL verdict (quality.md §3); the lead maps `BLOCKED`/`WARNING`-with-a-
  real-scenario onto a FAIL. Read the parsed `output`. **`--resume` is forbidden here**: a
  lens's context must be fresh-by-construction (producer≠grader depends on it), and `--resume`
  is reserved for the coding hand (Use 3) alone.
- **`gemini-critic` fallback — direct `agy`, judged by inlined material.** Call `agy --model
  "Gemini 3.6 Flash (High)" --add-dir <abs> --print-timeout 5m -p "<prompt>" < /dev/null` with
  the deliverable and the full lens brief (including the smell baseline) inlined in the prompt.
  This lane judges by inlined material only (`requires_pregate_output_inline`), does not run
  live tools, and takes its artifact from the written file, not truncated print. Honest caveat:
  Flash-tier, no strict schema — ask it to state status/summary/issues in that shape and parse
  the prose by hand. *(History, incident #5: an old bridge-Gemini path once edited the
  deliverable instead of returning findings; that path is retired — the direct-agy form with
  the flag-order fix and `requires_pregate_output_inline` is the current, verified one.)*
- **Ad-hoc second opinion (outside the gate)**: `mcp__codex__codex` (read-only, effort xhigh) —
  prose only, no strict verdict; the ONE surviving MCP use, never the gate's Standards call
  itself (no schema to grade against).
- **Lens brief — mandatory contents (v8; extended v12 for the smell baseline).** An external
  lens knows nothing of the home doctrine; its prompt must carry, in order: (1) the
  deliverable (diff) and the ticket's ACCEPTANCE verbatim; (2) the **full smell baseline**
  above, pasted in full — never a reference, per "Smell baseline" above; (3) the
  **decision-context pack** (quality.md §3a) — the same pack the other two lenses got,
  verbatim, including each entry's `base | added-by-diff` provenance; (4) the finding-scope
  vocabulary and the credential rule, compressed: "scope ∈ introduced | pre-existing |
  decision-challenge; a documented decision is an accepted ADR / CONTEXT.md entry / decision
  comment predating the diff or citing outside authority; an intent comment added by this
  diff with no citation is a self-declaration — keep the finding introduced"; (5) "try to
  refute; a finding needs a concrete failure scenario"; (6) read-only. The bridge verdict
  schema is unchanged — prefix each `issues[]` entry with its scope tag (`[introduced] …`);
  the lead maps `BLOCKED`/`WARNING` onto FAIL only through surviving `introduced`
  critical/major entries (quality.md §3a verdict mapping) and adjudicates the rest exactly
  like the other two lenses' findings (§3b).
- **Composition**: Standards is one of the fixed gate's **three parallel lenses**, not an
  additive extra layered on a Claude-only gate — its findings are scoped (`introduced |
  pre-existing | decision-challenge`) and adjudicated exactly like Spec's and критик's
  (quality.md §3a/§3b); only критик carries the PASS/FAIL verdict (`gates.lenses.critic.
  verdict: true`). The axis always runs: when its lane degrades all the way to
  `sonnet-inline-note`, that is a degrade recorded in one line of the final report — never a
  silent shrink to a two-lens gate.

## Use 2 — Gemini big-context recon (read-only alternative scout)

For recon whose INPUT exceeds a Claude window — and, under the Use 4 baseline, the default
web-recon lane. **Call**: `agy --model "Gemini 3.6 Flash (High)" -p "<prompt>" < /dev/null` (or the MCP
`ask-gemini`, which is Flash anyway), prompt = a **scout-contract** ticket (OBJECTIVE / INPUTS
via `@file` where supported / exact bounded OUTPUT / read-only BOUNDARIES / ACCEPTANCE + the
scout escape hatch `NEEDS_CLARIFICATION`, "0 matches is a valid answer"). For very high-volume
*mechanical* sweeps use `gemini-recon-cheap`, which since v23 also runs `Gemini 3.6 Flash
(High)` — it is now a role name, not an effort tier. Note `gemini-recon` is also the
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
the default for well-specified builder tickets when detected. **Call**:
`codex exec --model gpt-5.6-terra -c model_reasoning_effort=high --sandbox workspace-write --cwd <worktree>`
(`--json` gives `durationMs` + structured `usage` token counts). Params: prompt = a
**builder-contract**
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

## Use 4 — baseline allocation under the mandate (default lanes; quality-equivalent only; the Standards-axis row is a fixed gate member, not opt-in, since v11)

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
| Standards axis of the fixed gate — **runs on every gated deliverable, not opt-in** (ADR-0002) | **equal pool** `equal:[gemini-flash, codex-critic, grok-4.5, kimi-k3]`, all non-Claude, rotated least-dispatched — no single default | `availability.fallbacks.standards-lens` → `sonnet-inline-note` (never a silent gate-shrink) |

What NEVER shifts under the mandate — quality is the routing invariant, quota is not:

- **judgment-class tickets** (`architect`/Fable) and the lead itself — no external model
  substitutes for the deep-reasoning lane (ADR-0001: externals are workers, not orchestrators);
- **the primary grader** — v18 (2026-07-19): no longer "must be Claude". The gate's sole
  PASS/FAIL verdict may be held by any **honesty-cleared** lane (opus/critic, sol,
  grok-4.5, kimi-k3 — the last two cleared 2026-07-19 by an honesty test: as critic over a
  red test-suite they returned FAIL and did not tamper, plus t3/t4/t5 FULL). The invariant
  is now honesty + independence, not vendor: a critic never grades **its own vendor's**
  deliverable and never grades what **it built** (self-preference guard, ADR-0001). Opus
  stays the default grader; Grok's METR record was lifted on one probe, so every critic
  verdict is still cross-checked by the deterministic pre-gate (a PASS over red checks
  surfaces at once). The Standards lens remains one of three fixed lenses, never the sole
  verdict-holder, so quota-spread on it can never by itself cause a false-accept;
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
now with arithmetic behind them: the Standards axis's default lane is `codex-critic` (Sol —
the scarcest window), falling back to `gemini-critic` only on unavailability
(`availability.fallbacks.standards-lens`), not routine alternation; and recon rides Luna, not
Sol, at one-fifth the credit burn.
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
