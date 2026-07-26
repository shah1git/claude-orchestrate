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

# (каталоги назначения создаёт install_contour — по одному месту на реестр)

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

# prune_retired DIR
# Уборка следов выведенного из проекта. Удаляется ТОЛЬКО симлинк, который
# одновременно (а) указывает внутрь этого чекаута и (б) чья цель больше не
# существует, — то есть след прошлой установки головы или агента, удалённых из
# репозитория. Потерять при этом нечего по построению: сам симлинк содержимого
# не хранит, а его цели уже нет. Чужие ссылки (например, на канон Покока в
# ~/.agents/skills) под условие не подпадают и не трогаются.
prune_retired() {
  local dir="$1" entry target
  [ -d "${dir}" ] || return 0
  for entry in "${dir}"/*; do
    [ -L "${entry}" ] || continue      # не симлинк — не наше дело
    [ -e "${entry}" ] && continue      # цель жива — это действующая установка
    target="$(readlink "${entry}")"
    case "${target}" in
      "${REPO_DIR}"/skill/*|"${REPO_DIR}"/agents/*)
        rm -f "${entry}"
        echo "убрано  ${entry} (висячая ссылка на выведенное из репозитория)"
        ;;
    esac
  done
}

# install_contour SKILLS_DEST AGENTS_DEST
# Состав контура ВЫВОДИТСЯ из репозитория, а не перечисляется списком: голова —
# любой каталог skill/<name>/ с файлом SKILL.md, агент — любой файл
# agents/<name>.md. Перечисление именами разъезжается с репозиторием: третья
# голова `orca_orchestrate` была выведена из проекта (ADR-0008), а установщик
# продолжал её ставить, потому что помнил имя списком — в режиме симлинков это
# давало висячую ссылку, а в режиме --copy `cp -r` падал на несуществующем
# источнике и обрывал установку до агентов. Выводимый состав такой ошибки не
# допускает по построению; prune_retired убирает следы прежних установок.
install_contour() {
  local skills_dest="$1" agents_dest="$2" src
  mkdir -p "${skills_dest}" "${agents_dest}"

  for src in "${REPO_DIR}"/skill/*/; do
    [ -f "${src}SKILL.md" ] || continue
    install_one "${src%/}" "${skills_dest}/$(basename "${src%/}")"
  done

  for src in "${REPO_DIR}"/agents/*.md; do
    [ -f "${src}" ] || continue
    install_one "${src}" "${agents_dest}/$(basename "${src}")"
  done

  prune_retired "${skills_dest}"
  prune_retired "${agents_dest}"
}

install_contour "${SKILLS_DIR}" "${AGENTS_DIR}"

# Also wire the shared `~/.agents` skills store, if present. Harnesses spawned
# by Orca (and other agent CLIs) read skills from `~/.agents/skills` rather than
# `~/.claude/skills`, so an install that only touched `~/.claude` left
# `/orchestrate` reported as an "unknown skill" in those terminals. We mirror the
# same derived contour there too. NOTE: finding the skill file is
# necessary but not sufficient — `/orchestrate` spawns tiered Claude subagents via
# the Agent tool, so it only *functions* in a Claude-Code-family harness that
# supports subagents and model tiers; in others the skill is found but cannot run.
SHARED_AGENTS_DIR="${HOME}/.agents"
if [ -d "${SHARED_AGENTS_DIR}/skills" ]; then
  install_contour "${SHARED_AGENTS_DIR}/skills" "${SHARED_AGENTS_DIR}/agents"
fi

echo "Done (${MODE} mode)."
