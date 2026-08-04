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
CONFIGURE_POCOCK_ROLES=0
for arg in "$@"; do
  case "${arg}" in
    --link) MODE="link" ;;
    --configure-omp) CONFIGURE_OMP=1 ;;
    --configure-pocock-roles) CONFIGURE_POCOCK_ROLES=1 ;;
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
  task.maxRuntimeMs: 1800000
  retry.enabled: true
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
  omp config set task.maxRuntimeMs 1800000 >/dev/null
  omp config set retry.enabled true >/dev/null
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
  mkdir -p "${backup_dir}" || return 1
  while [ -e "${backup}" ] || [ -L "${backup}" ]; do
    backup="${backup_dir}/$(basename "${dest}").bak.${suffix}"
    suffix=$((suffix + 1))
  done
  mv "${dest}" "${backup}" || return 1
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

# staging_root_for DEST
# Keep incomplete artifacts outside OMP's live discovery registries while
# retaining the same filesystem for the final rename.
staging_root_for() {
  local dest="$1"
  printf '%s/.claude-orchestrate-staging\n' "$(dirname "$(dirname "${dest}")")"
}

filesystem_id() {
  local path="$1"
  if stat -c '%d' "${path}" >/dev/null 2>&1; then
    stat -c '%d' "${path}" || return 1
  else
    stat -f '%d' "${path}" || return 1
  fi
}

# recover_interrupted_copy DEST
# POSIX cannot atomically replace a non-empty directory. A ready marker is
# created only after staging is complete, so the next invocation can publish a
# copy interrupted between moving the prior artifact to backup and the rename.
recover_interrupted_copy() {
  local dest="$1" stage_root="" staging_prefix="" marker="" staging=""
  stage_root="$(staging_root_for "${dest}")"
  [ -d "${stage_root}" ] || return 0
  staging_prefix="${stage_root}/$(basename "${dest}").staging"

  if [[ -e "${dest}" || -L "${dest}" ]]; then
    for marker in "${staging_prefix}".*.ready; do
      [ -f "${marker}" ] || continue
      staging="${marker%.ready}"
      rm -rf "${staging}" || return 1
      rm -f "${marker}" || return 1
    done
  else
    for marker in "${staging_prefix}".*.ready; do
      [ -f "${marker}" ] || continue
      staging="${marker%.ready}"
      if [[ -e "${staging}" || -L "${staging}" ]]; then
        if ! mv "${staging}" "${dest}"; then
          return 1
        fi
        rm -f "${marker}" || return 1
        echo "recovered interrupted copy ${staging} -> ${dest}"
        return 0
      fi
      rm -f "${marker}" || return 1
    done
  fi

  # A process can die while copying, before its ready marker exists. These
  # incomplete artifacts are never publishable and are safe to remove.
  for staging in "${staging_prefix}".*; do
    [[ -e "${staging}" || -L "${staging}" ]] || continue
    [[ "${staging}" == *.ready ]] && continue
    [ -f "${staging}.ready" ] && continue
    rm -rf "${staging}" || return 1
  done
  return 0
}

# stage_runtime_state SOURCE STAGING
# A copy-mode upgrade replaces code but retains the existing live routing
# history before publication. This keeps failed telemetry copies from leaving a
# newly published artifact without its prior runtime state.
stage_runtime_state() {
  local source="$1" staging="$2"
  rm -rf "${staging}/telemetry" || return 1
  cp -Rp "${source}/telemetry" "${staging}/telemetry" || return 1
}

# install_one SRC DEST [RUNTIME_STATE_SOURCE]
# Copies by default through a sibling staging directory, so a failed copy never
# replaces a working artifact. Links are used only in explicit development mode.
# Existing content is moved aside rather than deleted; an already-correct link
# is left unchanged.
install_one() {
  local src="$1" dest="$2" requested_runtime_state="${3:-}"
  local backup="" stage_root="" staging="" ready_marker="" previous_link_target=""
  local registry_dir="" registry_device="" staging_device="" runtime_state_source=""
  local suffix=1 had_link=0 restored=0 needs_requested_runtime_state=0

  if [[ "${MODE}" == "copy" ]]; then
    if ! recover_interrupted_copy "${dest}"; then
      return 1
    fi
    if [[ -d "${src}" && -n "${requested_runtime_state}" \
        && ( -e "${requested_runtime_state}/telemetry" || -L "${requested_runtime_state}/telemetry" ) ]]; then
      needs_requested_runtime_state=1
    fi
    if [[ -e "${dest}" && ! -L "${dest}" ]] \
        && copy_is_current "${src}" "${dest}" \
        && [ "${needs_requested_runtime_state}" -eq 0 ]; then
      echo "up to date  ${dest} (copy)"
      return
    fi

    registry_dir="$(dirname "${dest}")"
    stage_root="$(staging_root_for "${dest}")"
    if ! mkdir -p "${stage_root}"; then
      return 1
    fi
    if ! registry_device="$(filesystem_id "${registry_dir}")" \
        || ! staging_device="$(filesystem_id "${stage_root}")"; then
      echo "cannot determine filesystem for staged install of ${dest}" >&2
      return 1
    fi
    if [ "${registry_device}" != "${staging_device}" ]; then
      echo "staging area for ${dest} is not on the destination filesystem" >&2
      return 1
    fi

    staging="${stage_root}/$(basename "${dest}").staging.$$"
    while [[ -e "${staging}" || -L "${staging}" ]]; do
      staging="${stage_root}/$(basename "${dest}").staging.$$.${suffix}"
      suffix=$((suffix + 1))
    done
    if ! cp -R "${src}" "${staging}"; then
      rm -rf "${staging}"
      return 1
    fi

    if [[ -d "${src}" ]]; then
      if [[ -n "${requested_runtime_state}" \
          && ( -e "${requested_runtime_state}/telemetry" || -L "${requested_runtime_state}/telemetry" ) ]]; then
        runtime_state_source="${requested_runtime_state}"
      elif [[ -e "${dest}" && ! -L "${dest}" \
          && ( -e "${dest}/telemetry" || -L "${dest}/telemetry" ) ]]; then
        runtime_state_source="${dest}"
      fi
      if [[ -n "${runtime_state_source}" ]] \
          && ! stage_runtime_state "${runtime_state_source}" "${staging}"; then
        rm -rf "${staging}"
        return 1
      fi
    fi

    ready_marker="${staging}.ready"
    if ! touch "${ready_marker}"; then
      rm -rf "${staging}"
      return 1
    fi

    # A symlink at DEST points into a checkout, so it has no content to back
    # up. Preserve its target until the replacement succeeds, however, so even
    # a failed rename leaves the old destination intact.
    if [[ -e "${dest}" && ! -L "${dest}" ]]; then
      LAST_BACKUP=""
      if ! backup_existing "${dest}"; then
        rm -f "${ready_marker}"
        rm -rf "${staging}"
        return 1
      fi
      backup="${LAST_BACKUP}"
    elif [[ -L "${dest}" ]]; then
      had_link=1
      if ! previous_link_target="$(readlink "${dest}")"; then
        rm -f "${ready_marker}"
        rm -rf "${staging}"
        return 1
      fi
      if ! rm -f "${dest}"; then
        rm -f "${ready_marker}"
        rm -rf "${staging}"
        return 1
      fi
    fi

    if ! mv "${staging}" "${dest}"; then
      if [[ -n "${backup}" ]]; then
        if mv "${backup}" "${dest}"; then
          restored=1
        else
          echo "failed to restore ${dest} from ${backup}" >&2
        fi
      elif [ "${had_link}" -eq 1 ]; then
        if ln -s "${previous_link_target}" "${dest}"; then
          restored=1
        else
          echo "failed to restore ${dest} symlink" >&2
        fi
      else
        restored=1
      fi
      if [ "${restored}" -eq 1 ]; then
        rm -f "${ready_marker}"
        rm -rf "${staging}"
      fi
      return 1
    fi

    rm -f "${ready_marker}"
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
  local src renamed_run_backup="" runtime_state_source=""
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
    runtime_state_source=""
    if [[ "${MODE}" == "copy" && -n "${renamed_run_backup}" \
        && "$(basename "${src%/}")" == "orchestrate" ]]; then
      runtime_state_source="${renamed_run_backup}"
    fi
    install_one "${src%/}" "${SHARED_SKILLS_DIR}/$(basename "${src%/}")" "${runtime_state_source}"
  done
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
# This opt-in action runs only with --configure-pocock-roles. The `pocock-*`
# model roles are part of the contour, not of one checkout: the public heads
# run in any repository, so the assignments live in the main OMP config. The
# portable profile supplies installation defaults only. The `pocock-*`
# namespace is reconciled with the portable profile so retired roles cannot
# survive an upgrade. Other parsed settings retain their values, while PyYAML
# intentionally normalizes comments, anchors, and presentation.
install_pocock_model_roles() {
  python3 - "${POCOCK_PROFILE_SOURCE}" "${OMP_BASE_DIR}/config.yml" <<'PY'
import os
import sys
import tempfile
from pathlib import Path

try:
    import fcntl
except ImportError:
    fcntl = None

import yaml

source_path, target_path = (Path(argument) for argument in sys.argv[1:3])
source = yaml.safe_load(source_path.read_text(encoding="utf-8")) or {}
target_file = Path(target_path)
write_target = target_file.resolve(strict=False) if target_file.is_symlink() else target_file

roles = {key: value for key, value in (source.get("modelRoles") or {}).items() if key.startswith("pocock-")}
chains = {
    key: value
    for key, value in ((source.get("retry") or {}).get("fallbackChains") or {}).items()
    if key.startswith("pocock-")
}


def read_target() -> tuple[bytes, dict]:
    snapshot = write_target.read_bytes() if write_target.is_file() else b""
    target = yaml.safe_load(snapshot.decode("utf-8")) if snapshot else {}
    if target is None:
        target = {}
    if not isinstance(target, dict):
        raise SystemExit(f"OMP config must be a mapping: {target_file}")
    return snapshot, target


def reconcile(target: dict) -> str:
    target_roles = target.setdefault("modelRoles", {})
    if not isinstance(target_roles, dict):
        raise SystemExit(f"OMP config modelRoles must be a mapping: {target_file}")
    for key in list(target_roles):
        if key.startswith("pocock-") and key not in roles:
            del target_roles[key]
    target_roles.update(roles)

    retry = target.setdefault("retry", {})
    if not isinstance(retry, dict):
        raise SystemExit(f"OMP config retry must be a mapping: {target_file}")
    target_chains = retry.setdefault("fallbackChains", {})
    if not isinstance(target_chains, dict):
        raise SystemExit(f"OMP config retry.fallbackChains must be a mapping: {target_file}")
    for key in list(target_chains):
        if key.startswith("pocock-") and key not in chains:
            del target_chains[key]
    target_chains.update(chains)
    return yaml.safe_dump(target, allow_unicode=True, sort_keys=False)


write_target.parent.mkdir(parents=True, exist_ok=True)
lock_path = write_target.with_name(f".{write_target.name}.pocock-roles.lock")
with lock_path.open("a+", encoding="utf-8") as lock_file:
    if fcntl is not None:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
    for _ in range(3):
        snapshot, target = read_target()
        serialized = reconcile(target)
        temporary_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=write_target.parent,
                prefix=f".{write_target.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary_file:
                temporary_path = Path(temporary_file.name)
                temporary_file.write(serialized)
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            current = write_target.read_bytes() if write_target.is_file() else b""
            if current != snapshot:
                continue
            os.replace(temporary_path, write_target)
            break
        finally:
            if temporary_path is not None and temporary_path.exists():
                temporary_path.unlink()
    else:
        raise SystemExit(
            f"OMP config changed repeatedly while reconciling Pocock routes: {target_file}; retry"
        )

print(f"reconciled {len(roles)} Pocock model roles and {len(chains)} fallback chains in {target_file}")
PY
}

require_omp_sources
if [ "${CONFIGURE_OMP}" -eq 1 ]; then
  configure_omp_runtime
else
  echo "OMP profile unchanged; use --configure-omp to install the required global task invariants"
fi

# The existing adapter accepts the additive runtime protocol. Publish heads and
# their runtime first, then install the strict adapter only after its compatible
# runtime is live.
install_heads
retire_legacy_harness_links

if [ "${CONFIGURE_POCOCK_ROLES}" -eq 1 ]; then
  install_pocock_model_roles
else
  echo "Pocock model roles unchanged; use --configure-pocock-roles to install portable Pocock routes"
fi

install_omp_native

echo "Done (${MODE} mode)."
