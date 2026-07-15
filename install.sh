#!/usr/bin/env bash
# Installs the orchestrate skill and its agents into ~/.claude.
#
# Default mode: symlinks (edits in this repo are live immediately in Claude Code).
# --copy mode:  copies instead (use on machines where you don't want a live link
#               to this checkout — you must re-run this script after any edit).
set -euo pipefail

# Resolve this script's own directory, following symlinks, so it works
# regardless of the caller's current working directory.
REPO_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"

MODE="link"
if [[ "${1:-}" == "--copy" ]]; then
  MODE="copy"
fi

CLAUDE_DIR="${HOME}/.claude"
SKILLS_DIR="${CLAUDE_DIR}/skills"
AGENTS_DIR="${CLAUDE_DIR}/agents"

mkdir -p "${SKILLS_DIR}" "${AGENTS_DIR}"

# install_one SRC DEST
# Links (default) or copies (--copy) SRC (a file or directory inside the repo)
# to DEST (a path inside ~/.claude). Existing content at DEST that is not
# already the correct symlink is moved aside to DEST.bak first.
install_one() {
  local src="$1" dest="$2"

  if [[ "${MODE}" == "copy" ]]; then
    # Back up real (non-symlink) content before replacing it — same guarantee
    # as link mode. A symlink at DEST points into a repo checkout, so removing
    # it loses nothing.
    if [[ -e "${dest}" && ! -L "${dest}" ]]; then
      rm -rf "${dest}.bak"
      mv "${dest}" "${dest}.bak"
      echo "backed up   ${dest} -> ${dest}.bak"
    elif [[ -L "${dest}" ]]; then
      rm -f "${dest}"
    fi
    cp -r "${src}" "${dest}"
    echo "copied  ${src} -> ${dest}"
    return
  fi

  # Symlink mode: idempotent — do nothing if already correctly linked.
  if [[ -L "${dest}" && "$(readlink "${dest}")" == "${src}" ]]; then
    echo "up to date  ${dest} -> ${src}"
    return
  fi

  if [[ -e "${dest}" || -L "${dest}" ]]; then
    mv "${dest}" "${dest}.bak"
    echo "backed up   ${dest} -> ${dest}.bak"
  fi

  ln -s "${src}" "${dest}"
  echo "linked  ${dest} -> ${src}"
}

install_one "${REPO_DIR}/skill/orchestrate" "${SKILLS_DIR}/orchestrate"

for agent in architect builder scout critic; do
  install_one "${REPO_DIR}/agents/${agent}.md" "${AGENTS_DIR}/${agent}.md"
done

# Also wire the shared `~/.agents` skills store, if present. Harnesses spawned
# by Orca (and other agent CLIs) read skills from `~/.agents/skills` rather than
# `~/.claude/skills`, so an install that only touched `~/.claude` left
# `/orchestrate` reported as an "unknown skill" in those terminals. We mirror the
# skill and the four agent definitions there too. NOTE: finding the skill file is
# necessary but not sufficient — `/orchestrate` spawns tiered Claude subagents via
# the Agent tool, so it only *functions* in a Claude-Code-family harness that
# supports subagents and model tiers; in others the skill is found but cannot run.
SHARED_AGENTS_DIR="${HOME}/.agents"
if [ -d "${SHARED_AGENTS_DIR}/skills" ]; then
  mkdir -p "${SHARED_AGENTS_DIR}/agents"
  install_one "${REPO_DIR}/skill/orchestrate" "${SHARED_AGENTS_DIR}/skills/orchestrate"
  for agent in architect builder scout critic; do
    install_one "${REPO_DIR}/agents/${agent}.md" "${SHARED_AGENTS_DIR}/agents/${agent}.md"
  done
fi

echo "Done (${MODE} mode)."
