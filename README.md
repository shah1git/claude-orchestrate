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

Every delegated ticket carries mandatory acceptance criteria, and every production
deliverable is verified — deterministically where possible, otherwise by `critic` running
in a fresh context that never sees the producer's own reasoning. Failures escalate on a
capped ladder (retry once → raise the model tier → report the blocker) rather than
looping indefinitely.

### Optional cross-provider layer

By default the skill is fully self-contained and Claude-only. When the user has them wired,
the lead can *optionally* delegate to non-Claude models via MCP — OpenAI Codex and Google
Gemini (Antigravity) — for three named reasons only: an independent cross-model verification
lens, big-context recon beyond a Claude window, or (opt-in) a Codex coding hand. It is
capability-detected: absent a connector, every route falls back to the Claude default, so
the Claude-only path always works. See
[skill/orchestrate/references/cross-provider.md](skill/orchestrate/references/cross-provider.md).

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
