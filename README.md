# orchestrate

A Claude Code skill for delegating complex work across model-tiered subagents.

## What it is

`/orchestrate` turns the main Claude Code session into a **lead orchestrator**: it
triages a task, decomposes it, and delegates subtasks to four specialized subagents,
each pinned to the model tier its job actually needs:

| Agent | Model | Role |
|---|---|---|
| `architect` | Fable | System design, architecture decisions, root-cause debugging, risk assessment |
| `builder` | Sonnet | Implementation to a clear spec: code, tests, refactors, docs |
| `scout` | Haiku, read-only | Mechanical recon: find files/usages, grep sweeps, inventories, classification |
| `critic` | Opus | Adversarial verification of any deliverable, in a fresh context |

Every delegated ticket carries mandatory acceptance criteria, and every gated deliverable is
verified through the same fixed three-lens gate, unconditionally: Standards (does it meet
this repo's conventions), Spec (is it the right thing, nothing extra), and `critic` (can it
be trusted — the only lens with a PASS/FAIL verdict), each in a fresh context that never sees
the producer's own reasoning. Production code and security-relevant work are examples of why
the gate can't be skipped, not a trigger list — deterministic checks (tests, typecheck,
build) run first wherever they apply. Failures escalate on a capped ladder (retry once →
raise the model tier → report the blocker) rather than looping indefinitely.

### Optional cross-provider layer

By default the skill is fully self-contained and Claude-only. When the user has them wired,
the lead can also route to non-Claude models — OpenAI Codex and Google Gemini (Antigravity)
— via MCP or the `agent-bridge`, including as the Standards lens above (a permanent,
non-Claude member of the fixed gate, closing self-preference bias, not an opt-in extra) and
as a coding hand for well-specified builder tickets. See
[skill/orchestrate/references/cross-provider.md](skill/orchestrate/references/cross-provider.md)
(Use 1–4) for the full list of named uses. It is capability-detected: absent a connector,
every route resolves to a Claude default, and the Standards lens specifically is never
silently skipped — its fallback chain always terminates in a Claude lens run inline, with a
note in the final report (exact executors and fallback order live in config: `gates.lenses`,
`availability.fallbacks`) — so the Claude-only path always works.

## Install

```bash
git clone <this-repo-url> /opt/claude-orchestrate
cd /opt/claude-orchestrate
./install.sh          # symlink mode: edits here are live in Claude Code immediately
./install.sh --copy   # copy mode: independent copies under ~/.claude
```

## Repo layout

```
skill/orchestrate/          the skill itself (SKILL.md + references/)
agents/                     the four agent definitions
install.sh                  wires ~/.claude/skills and ~/.claude/agents to this repo
```

## Usage

Invoke explicitly with `/orchestrate <task>`, or let Claude Code auto-invoke it on
large, multi-part tasks that decompose into independent subtasks (multi-file features,
codebase audits, migrations, parallel research sweeps). The four agents are also
directly invocable on their own, without going through the skill.

## Development

Edit the files under `skill/` and `agents/` directly in this repo — with the default
symlink install, changes are picked up by Claude Code immediately, no reinstall needed.
If you installed with `--copy`, re-run `./install.sh --copy` after every edit to refresh
the copies.

See [skill/orchestrate/README.md](skill/orchestrate/README.md) for the detailed docs
(in Russian) and the changelog.
