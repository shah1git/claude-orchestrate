#!/usr/bin/env bash
# Installs the native OMP Pocock skill heads, capability agents, and extension.
#
# Default mode: detached copies, so a running orchestration pins one installed
# snapshot. `--link` is an explicit development mode with live checkout edits.
set -euo pipefail

# Resolve this script's own directory, following symlinks, so it works
# regardless of the caller's current working directory.
REPO_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"

MODE="copy"
CONFIGURE_OMP=0
for arg in "$@"; do
  case "${arg}" in
    --link) MODE="link" ;;
    --configure-omp) CONFIGURE_OMP=1 ;;
    *) echo "unknown install option: ${arg}" >&2; exit 2 ;;
  esac
done

SHARED_SKILLS_DIR="${HOME}/.agents/skills"

# OMP discovers native task agents and extensions below its active agent base.
# PI_CODING_AGENT_DIR selects that base. Global task invariants change only when
# the owner passes --configure-omp.
OMP_BASE_DIR="${PI_CODING_AGENT_DIR:-${HOME}/.omp/agent}"
OMP_AGENTS_DIR="${OMP_BASE_DIR}/agents"
OMP_EXTENSION_DIR="${OMP_BASE_DIR}/extensions"
OMP_AGENTS_SOURCE="${REPO_DIR}/.omp/agents"
OMP_EXTENSION_SOURCE="${REPO_DIR}/.omp/extensions/pocock-control"
POCOCK_PROFILE_SOURCE="${REPO_DIR}/scripts/omp-portable-profile.yml"
INSTALL_BACKUP_DIR="${XDG_STATE_HOME:-${HOME}/.local/state}/claude-orchestrate/install-backups"

report_omp_isolation() {
  cat <<'EOF'
Repository OMP defaults and task invariants:
  async.enabled: false
  task.batch: true
  task.enableEffort: true
  task.isolation.mode: auto (installation default; explicit isolated backends are valid)
  task.isolation.apply: false
  task.isolation.merge: patch
  task.maxRecursionDepth: 1
  task.maxConcurrency: 6
  retry.modelFallback: true
EOF
}

require_omp_sources() {
  local missing=0 agent_seen=0 src

  [ -f "${POCOCK_PROFILE_SOURCE}" ] || {
    echo "missing portable OMP profile: ${POCOCK_PROFILE_SOURCE}" >&2
    missing=1
  }
  [ -d "${OMP_EXTENSION_SOURCE}" ] || {
    echo "missing required OMP extension source: ${OMP_EXTENSION_SOURCE}" >&2
    missing=1
  }
  [ -d "${OMP_AGENTS_SOURCE}" ] || {
    echo "missing required OMP agents source: ${OMP_AGENTS_SOURCE}" >&2
    missing=1
  }
  for src in "${OMP_AGENTS_SOURCE}"/*.md; do
    [ -f "${src}" ] || continue
    agent_seen=1
  done
  [ "${agent_seen}" -eq 1 ] || {
    echo "no OMP agent definitions found in ${OMP_AGENTS_SOURCE}" >&2
    missing=1
  }

  report_omp_isolation
  [ "${missing}" -eq 0 ] || exit 1
}

# configure_omp_runtime
# This opt-in action is used only with --configure-omp. The public heads can be
# invoked from any repository, so their control-plane invariants live in the
# main OMP config. OMP owns its configuration writer; using it preserves
# unrelated user settings while making batch transport and isolation settings
# effective everywhere and enabling fallback model resolution.
configure_omp_runtime() {
  command -v omp >/dev/null 2>&1 || {
    echo "OMP CLI is required for the native Pocock contour" >&2
    exit 1
  }

  omp config set async.enabled false >/dev/null
  omp config set display.showTokenUsage true >/dev/null
  omp config set task.batch true >/dev/null
  omp config set task.enableEffort true >/dev/null
  omp config set task.showResolvedModelBadge true >/dev/null
  omp config set task.isolation.mode auto >/dev/null
  omp config set task.isolation.apply false >/dev/null
  omp config set task.isolation.merge patch >/dev/null
  omp config set task.maxRecursionDepth 1 >/dev/null
  omp config set task.maxConcurrency 6 >/dev/null
  omp config set retry.modelFallback true >/dev/null
  echo "configured OMP native task invariants in ${OMP_BASE_DIR}/config.yml"
}

LAST_BACKUP=""


# Destination directories are created by the native installers below.

# backup_existing DEST
# Preserve an existing user artifact outside every live discovery registry.
# Numeric suffixes prevent the installer from overwriting a backup it did not make.
backup_existing() {
  local dest="$1" backup_dir="${INSTALL_BACKUP_DIR}$(dirname "${1}")"
  local backup="${backup_dir}/$(basename "${dest}").bak" suffix=1
  mkdir -p "${backup_dir}"
  while [ -e "${backup}" ] || [ -L "${backup}" ]; do
    backup="${backup_dir}/$(basename "${dest}").bak.${suffix}"
    suffix=$((suffix + 1))
  done
  mv "${dest}" "${backup}"
  LAST_BACKUP="${backup}"
  echo "backed up   ${dest} -> ${backup}"
}

# relocate_legacy_backups DEST
# Older installers left complete same-name skills beside DEST, where OMP could
# discover them. Move only that canonical artifact's .bak traces out of registry.
relocate_legacy_backups() {
  local dest="$1" entry
  for entry in "${dest}.bak" "${dest}.bak."*; do
    [[ -e "${entry}" || -L "${entry}" ]] || continue
    backup_existing "${entry}"
  done
}

# copy_is_current SRC DEST
# Runtime telemetry, Python bytecode, and pytest caches are mutable state, not
# skill content; matching the verifier, ignore them for copy-mode idempotence.
copy_is_current() {
  local src="$1" dest="$2"
  if [ -d "${src}" ]; then
    diff -qr -x __pycache__ -x .pytest_cache -x telemetry "${src}" "${dest}" >/dev/null 2>&1
  else
    cmp -s "${src}" "${dest}"
  fi
}

# restore_runtime_state BACKUP DEST
# A copy-mode upgrade replaces code but must not replace live routing history.
# The just-copied telemetry entry is safe to remove: it came from SRC moments
# earlier; the existing user's state remains intact in BACKUP until restored.
restore_runtime_state() {
  local backup="$1" dest="$2"
  if [[ -e "${backup}/telemetry" || -L "${backup}/telemetry" ]]; then
    rm -rf "${dest}/telemetry"
    cp -Rp "${backup}/telemetry" "${dest}/telemetry"
    echo "restored ${dest}/telemetry from ${backup}"
  fi
}


# install_one SRC DEST
# Copies by default; links only in explicit `--link` development mode.
# Existing content is moved aside rather than deleted; an already-correct link
# is left unchanged.
install_one() {
  local src="$1" dest="$2"
  local backup=""

  if [[ "${MODE}" == "copy" ]]; then
    if [[ -e "${dest}" && ! -L "${dest}" ]] && copy_is_current "${src}" "${dest}"; then
      echo "up to date  ${dest} (copy)"
      return
    fi
    # A symlink at DEST points into a checkout, so removing it loses no
    # content.  Real content is always retained under a non-conflicting backup.
    if [[ -e "${dest}" && ! -L "${dest}" ]]; then
      LAST_BACKUP=""
      backup_existing "${dest}"
      backup="${LAST_BACKUP}"
    elif [[ -L "${dest}" ]]; then
      rm -f "${dest}"
    fi
    cp -r "${src}" "${dest}"
    if [[ -d "${src}" && -n "${backup}" ]]; then
      restore_runtime_state "${backup}" "${dest}"
    fi
    echo "copied  ${src} -> ${dest}"
    return
  fi

  # Symlink mode: idempotent — do nothing if already correctly linked.
  if [[ -L "${dest}" && "$(readlink "${dest}")" == "${src}" ]]; then
    echo "up to date  ${dest} -> ${src}"
    return
  fi

  if [[ -e "${dest}" || -L "${dest}" ]]; then
    backup_existing "${dest}"
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
      "${REPO_DIR}"/skill/*|"${REPO_DIR}"/agents/*|"${REPO_DIR}"/.omp/agents/*|"${REPO_DIR}"/.omp/extensions/*)
        rm -f "${entry}"
        echo "убрано  ${entry} (висячая ссылка на выведенное из репозитория)"
        ;;
    esac
  done
}

# prune_retired_pocock_agents
# The OMP agent registry owns the `pocock-` namespace. Unlike generic stale
# checkout links, prior copy-mode installations leave ordinary manifests there;
# preserve those files in the regular install backup before removing them from
# OMP discovery. A stale link has no content of its own and can be removed.
prune_retired_pocock_agents() {
  local entry
  [ -d "${OMP_AGENTS_DIR}" ] || return 0
  for entry in "${OMP_AGENTS_DIR}"/pocock-*.md; do
    [[ -f "${entry}" || -L "${entry}" ]] || continue
    [ -f "${OMP_AGENTS_SOURCE}/$(basename "${entry}")" ] && continue
    if [ -L "${entry}" ]; then
      rm -f "${entry}"
      echo "убрано  ${entry} (устаревшая ссылка на Pocock agent)"
    else
      LAST_BACKUP=""
      backup_existing "${entry}"
      echo "убрано  ${entry} (устаревший Pocock agent; резервная копия: ${LAST_BACKUP})"
    fi
  done
}



# install_heads
# OMP discovers public skills in the shared ~/.agents/skills registry. Derive
# the head set from the checkout so renames cannot leave a hand-maintained list.
install_heads() {
  local src renamed_run_backup=""
  mkdir -p "${SHARED_SKILLS_DIR}"

  # Pocock-prefixed aliases have no active public meaning. Archive every trace
  # outside discovery before installing the owner-selected orchestration-first
  # interface. The full head's live telemetry follows the rename in copy mode.
  relocate_legacy_backups "${SHARED_SKILLS_DIR}/pocock-run"
  if [[ -e "${SHARED_SKILLS_DIR}/pocock-run" || -L "${SHARED_SKILLS_DIR}/pocock-run" ]]; then
    LAST_BACKUP=""
    backup_existing "${SHARED_SKILLS_DIR}/pocock-run"
    renamed_run_backup="${LAST_BACKUP}"
  fi
  relocate_legacy_backups "${SHARED_SKILLS_DIR}/pocock-frontier"
  if [[ -e "${SHARED_SKILLS_DIR}/pocock-frontier" || -L "${SHARED_SKILLS_DIR}/pocock-frontier" ]]; then
    backup_existing "${SHARED_SKILLS_DIR}/pocock-frontier"
  fi
  relocate_legacy_backups "${SHARED_SKILLS_DIR}/pocock-sweep"
  if [[ -e "${SHARED_SKILLS_DIR}/pocock-sweep" || -L "${SHARED_SKILLS_DIR}/pocock-sweep" ]]; then
    LAST_BACKUP=""
    backup_existing "${SHARED_SKILLS_DIR}/pocock-sweep"
  fi


  for src in "${REPO_DIR}"/skill/*/; do
    [ -f "${src}SKILL.md" ] || continue
    relocate_legacy_backups "${SHARED_SKILLS_DIR}/$(basename "${src%/}")"
    install_one "${src%/}" "${SHARED_SKILLS_DIR}/$(basename "${src%/}")"
  done
  if [[ "${MODE}" == "copy" && -n "${renamed_run_backup}" ]]; then
    restore_runtime_state "${renamed_run_backup}" "${SHARED_SKILLS_DIR}/orchestrate"
  fi
  prune_retired "${SHARED_SKILLS_DIR}"
}

# Remove only live symlinks created by older releases of this checkout. Real
# user files and links to any other repository are never touched.
retire_legacy_harness_links() {
  local entry target
  for entry in \
    "${HOME}/.claude/skills/orchestrate" \
    "${HOME}/.claude/skills/orchestrate-frontier" \
    "${HOME}/.claude/skills/pocock-run" \
    "${HOME}/.claude/skills/pocock-frontier" \
    "${HOME}/.claude/skills/orchestrate-sweep" \
    "${HOME}/.claude/skills/pocock-sweep" \
    "${HOME}/.claude/agents/architect.md" \
    "${HOME}/.claude/agents/builder.md" \
    "${HOME}/.claude/agents/critic.md" \
    "${HOME}/.claude/agents/scout.md" \
    "${HOME}/.agents/agents/architect.md" \
    "${HOME}/.agents/agents/builder.md" \
    "${HOME}/.agents/agents/critic.md" \
    "${HOME}/.agents/agents/scout.md"; do
    [ -L "${entry}" ] || continue
    target="$(readlink "${entry}")"
    case "${target}" in
      "${REPO_DIR}"/skill/*|"${REPO_DIR}"/agents/*)
        rm -f "${entry}"
        echo "retired  ${entry} (legacy non-OMP contour link)"
        ;;
    esac
  done
}

# OMP-native artifacts live in their own registries. Generic stale checkout
# symlinks are pruned there, while obsolete `pocock-*.md` manifests are removed
# from the agent registry regardless of whether the prior install used copies
# or links.
install_omp_native() {
  local src
  mkdir -p "${OMP_AGENTS_DIR}" "${OMP_EXTENSION_DIR}"

  for src in "${OMP_AGENTS_SOURCE}"/*.md; do
    [ -f "${src}" ] || continue
    relocate_legacy_backups "${OMP_AGENTS_DIR}/$(basename "${src}")"
    install_one "${src}" "${OMP_AGENTS_DIR}/$(basename "${src}")"
  done
  install_one "${OMP_EXTENSION_SOURCE}" "${OMP_EXTENSION_DIR}/pocock-control"
  relocate_legacy_backups "${OMP_EXTENSION_DIR}/pocock-control"

  prune_retired "${OMP_AGENTS_DIR}"
  prune_retired_pocock_agents
  prune_retired "${OMP_EXTENSION_DIR}"
}

# install_pocock_model_roles
# The `pocock-*` model roles are part of the contour, not of one checkout: the
# public heads run in any repository, so the assignments live in the main OMP
# config. The portable profile supplies installation defaults only. Roles are
# namespaced and therefore do not belong behind --configure-omp, which gates
# global task invariants. The `pocock-*` namespace is reconciled with the
# portable profile so retired roles cannot survive an upgrade; every other
# setting in the user's main config is preserved verbatim.
install_pocock_model_roles() {
  python3 - "${POCOCK_PROFILE_SOURCE}" "${OMP_BASE_DIR}/config.yml" <<'PY'
import sys
from pathlib import Path

import yaml

source_path, target_path = (Path(argument) for argument in sys.argv[1:3])
source = yaml.safe_load(source_path.read_text(encoding="utf-8")) or {}
target_file = Path(target_path)
target = yaml.safe_load(target_file.read_text(encoding="utf-8")) if target_file.is_file() else {}
target = target or {}

roles = {key: value for key, value in (source.get("modelRoles") or {}).items() if key.startswith("pocock-")}
chains = {
    key: value
    for key, value in ((source.get("retry") or {}).get("fallbackChains") or {}).items()
    if key.startswith("pocock-")
}
target_roles = target.setdefault("modelRoles", {})
for key in list(target_roles):
    if key.startswith("pocock-") and key not in roles:
        del target_roles[key]
target_roles.update(roles)

target_chains = target.setdefault("retry", {}).setdefault("fallbackChains", {})
for key in list(target_chains):
    if key.startswith("pocock-") and key not in chains:
        del target_chains[key]
target_chains.update(chains)

target_file.parent.mkdir(parents=True, exist_ok=True)
target_file.write_text(yaml.safe_dump(target, allow_unicode=True, sort_keys=False), encoding="utf-8")
print(f"reconciled {len(roles)} Pocock model roles and {len(chains)} fallback chains in {target_file}")
PY
}

require_omp_sources
if [ "${CONFIGURE_OMP}" -eq 1 ]; then
  configure_omp_runtime
else
  echo "OMP profile unchanged; use --configure-omp to install the required global task invariants"
fi

install_omp_native
install_pocock_model_roles

install_heads
retire_legacy_harness_links

echo "Done (${MODE} mode)."
