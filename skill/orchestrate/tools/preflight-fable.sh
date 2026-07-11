#!/usr/bin/env bash
# =============================================================================
# preflight-fable.sh — guard that orchestration sub-agents will actually reach
# their pinned models (Fable for architect, etc.) before a fan-out.
#
# WHY THIS EXISTS (the Fable guarantee, and the three ways it silently breaks)
# ---------------------------------------------------------------------------
# A custom sub-agent's model is resolved in this order (Claude Code v2.1.196+):
#     1. CLAUDE_CODE_SUBAGENT_MODEL   (env var — beats everything below)
#     2. per-invocation `model:` in the Agent/Task call
#     3. the agent's frontmatter `model:`
#     4. the parent session's model     (used only when the above are absent)
# A frontmatter pin (level 3) is INDEPENDENT of the parent session: an Opus
# session still spawns a Fable-pinned `architect` on Fable. `/fast` and Fable
# 5's runtime safety-classifier fallback move only the SESSION, never a pinned
# sub-agent (Claude Code docs: sub-agents.md, model-config.md — confidence high).
#
# So the architecture already delivers Fable through agents regardless of the
# session. Three — and only three — things silently defeat it:
#     (1) a frontmatter that omits `model:` (default = `inherit` -> leaks the
#         session model) or sets it to `inherit`;
#     (2) CLAUDE_CODE_SUBAGENT_MODEL being set (overrides every frontmatter pin);
#     (3) an `availableModels` allowlist that excludes the pinned model (the
#         excluded value is skipped and the sub-agent silently runs on inherit).
# This script fails on any of the three. It deliberately does NOT fail when the
# session itself is on Opus — that is the expected, harmless case (agents stay
# pinned); it only reports the live session model for awareness.
#
# Run it at the top of an orchestrated run:  bash tools/preflight-fable.sh
# Exit 0 = pins are authoritative; non-zero = a gotcha is live, fix before fan-out.
# =============================================================================
set -uo pipefail

AGENTS_DIR="${CLAUDE_AGENTS_DIR:-$HOME/.claude/agents}"   # runtime-authoritative copies
SETTINGS="${CLAUDE_SETTINGS:-$HOME/.claude/settings.json}"
fail=0
note() { printf '  %s\n' "$*"; }

echo "== preflight-fable: sub-agent model-pin integrity =="

# --- Gotcha 2: the override env var must not be set ---------------------------
if [ -n "${CLAUDE_CODE_SUBAGENT_MODEL:-}" ]; then
  echo "FAIL: CLAUDE_CODE_SUBAGENT_MODEL='${CLAUDE_CODE_SUBAGENT_MODEL}' is set"
  note "It overrides EVERY frontmatter pin and per-invocation model. Unset it:"
  note "  unset CLAUDE_CODE_SUBAGENT_MODEL   (and remove it from any settings.env)"
  fail=1
else
  echo "OK:  CLAUDE_CODE_SUBAGENT_MODEL is unset (frontmatter pins stay authoritative)"
fi

# --- Gotcha 1: every agent must pin its model explicitly ---------------------
declare -A WANT=( [architect]=fable [builder]=sonnet [critic]=opus [scout]=haiku )
if [ -d "$AGENTS_DIR" ]; then
  for a in architect builder critic scout; do
    f="$AGENTS_DIR/$a.md"
    if [ ! -f "$f" ]; then echo "FAIL: $a.md missing in $AGENTS_DIR"; fail=1; continue; fi
    line=$(grep -m1 -E '^model:' "$f" | sed 's/#.*//;s/[[:space:]]*$//')
    val=$(printf '%s' "$line" | sed -E 's/^model:[[:space:]]*//')
    if [ -z "$line" ]; then
      echo "FAIL: $a.md has no 'model:' line -> defaults to inherit (leaks session model)"; fail=1
    elif [ "$val" = inherit ]; then
      echo "FAIL: $a.md pins 'inherit' -> leaks session model"; fail=1
    elif printf '%s' "$val" | grep -qi "${WANT[$a]}"; then
      echo "OK:  $a -> $val"
    else
      echo "WARN: $a pinned '$val' (expected to contain '${WANT[$a]}') — intended?"
    fi
  done
else
  echo "FAIL: agents dir not found: $AGENTS_DIR"; fail=1
fi

# --- Gotcha 3: no availableModels allowlist excluding fable -------------------
if [ -f "$SETTINGS" ] && command -v jq >/dev/null 2>&1; then
  allow=$(jq -r 'try (.availableModels // empty) | if type=="array" then join(",") else . end' "$SETTINGS" 2>/dev/null)
  if [ -n "$allow" ]; then
    if printf '%s' "$allow" | grep -qi fable; then
      echo "OK:  availableModels allowlist present and includes fable"
    else
      echo "FAIL: availableModels allowlist set WITHOUT fable -> Fable agents silently downgrade"
      note "  allowlist = $allow"; fail=1
    fi
  else
    echo "OK:  no availableModels allowlist (no model is excluded)"
  fi
  # --- Informational: live session model + fallback awareness ----------------
  sess=$(jq -r 'try (.model // "unset")' "$SETTINGS" 2>/dev/null)
  echo "INFO: session model pin = $sess"
  case "$sess" in
    *fable*) : ;;
    *) note "session is not pinned to Fable — harmless for pinned agents, but the"
       note "lead itself runs off-Fable; Fable 5 safety-fallback -> Opus is /config-only." ;;
  esac
else
  echo "INFO: $SETTINGS or jq unavailable — skipped allowlist/session checks"
fi

echo "== $([ $fail -eq 0 ] && echo 'PASS — pins authoritative, safe to fan out' || echo 'FAIL — fix the above before delegating') =="
exit $fail
