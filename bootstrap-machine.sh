#!/usr/bin/env bash
# One-command bootstrap for a new OMP workstation. It restores reusable OMP
# behavior from this repository, never credentials or machine runtime state.
set -euo pipefail

ORCH_REPO_URL="${ORCH_REPO_URL:-https://github.com/shah1git/claude-orchestrate.git}"
CLAUDE_ORCHESTRATE_DIR="${CLAUDE_ORCHESTRATE_DIR:-${XDG_DATA_HOME:-${HOME}/.local/share}/claude-orchestrate}"
BACKUP_DIR="${XDG_STATE_HOME:-${HOME}/.local/state}/claude-orchestrate/machine-bootstrap-backups"

ok()   { printf '  \033[32m✓\033[0m %s\n' "$1"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$1"; }
die()  { printf '  \033[31m✗\033[0m %s\n' "$1" >&2; exit 1; }

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "$1 не найден"
}

install_omp_if_missing() {
  command -v omp >/dev/null 2>&1 && return
  require_command curl
  echo "== Установка OMP =="
  curl -fsSL https://omp.sh/install | sh
  export PATH="${HOME}/.local/bin:${HOME}/.bun/bin:${PATH}"
  hash -r
  command -v omp >/dev/null 2>&1 || die "официальный установщик завершился, но omp не найден в PATH"
  ok "OMP установлен"
}

ensure_checkout() {
  local parent entries
  if [ -d "${CLAUDE_ORCHESTRATE_DIR}/.git" ]; then
    git -C "${CLAUDE_ORCHESTRATE_DIR}" pull --ff-only
    ok "обновлён ${CLAUDE_ORCHESTRATE_DIR}"
    return
  fi

  if [ -e "${CLAUDE_ORCHESTRATE_DIR}" ]; then
    [ -d "${CLAUDE_ORCHESTRATE_DIR}" ] \
      || die "${CLAUDE_ORCHESTRATE_DIR} существует и не является каталогом"
    shopt -s nullglob dotglob
    entries=("${CLAUDE_ORCHESTRATE_DIR}"/*)
    shopt -u nullglob dotglob
    [ "${#entries[@]}" -eq 0 ] \
      || die "${CLAUDE_ORCHESTRATE_DIR} существует, но не является git checkout и не пуст"
  fi

  parent="$(dirname "${CLAUDE_ORCHESTRATE_DIR}")"
  mkdir -p "${parent}"
  git clone "${ORCH_REPO_URL}" "${CLAUDE_ORCHESTRATE_DIR}"
  ok "клонирован ${CLAUDE_ORCHESTRATE_DIR}"
}

backup_path() {
  local source="$1" label="$2" stamp backup
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  backup="${BACKUP_DIR}/${label}.${stamp}.$$"
  mkdir -p "${BACKUP_DIR}"
  if [ -L "${source}" ]; then
    cp -P "${source}" "${backup}"
  else
    cp -p "${source}" "${backup}"
    chmod 600 "${backup}"
  fi
  printf '%s\n' "${backup}"
}

install_portable_file() {
  local source="$1" destination="$2" label="$3" backup
  if [ -e "${destination}" ] || [ -L "${destination}" ]; then
    if [ ! -L "${destination}" ] && [ -f "${destination}" ] \
       && cmp -s "${source}" "${destination}"; then
      chmod 600 "${destination}"
      ok "${label}: уже совпадает с переносимым снимком"
      return
    fi
    [ -f "${destination}" ] || [ -L "${destination}" ] \
      || die "${destination} существует и не является обычным файлом или симлинком"
    backup="$(backup_path "${destination}" "$(basename "${destination}")")"
    rm -f "${destination}"
    warn "${label}: прежний файл сохранён в ${backup}"
  fi
  install -m 600 "${source}" "${destination}"
  ok "${label}: установлен"
}

require_command git
require_command python3
python3 -c 'import yaml' 2>/dev/null \
  || die "PyYAML не найден — установите пакет python3-yaml или PyYAML"
install_omp_if_missing

echo "== Репозиторий =="
ensure_checkout

PROFILE="${CLAUDE_ORCHESTRATE_DIR}/scripts/omp-portable-profile.yml"
WATCHDOG="${CLAUDE_ORCHESTRATE_DIR}/scripts/omp-portable-WATCHDOG.md"
VALIDATOR="${CLAUDE_ORCHESTRATE_DIR}/scripts/validate_portable_omp_profile.py"
BOOTSTRAP="${CLAUDE_ORCHESTRATE_DIR}/bootstrap-mac.sh"

[ -f "${PROFILE}" ] || die "в checkout отсутствует ${PROFILE}"
[ -f "${WATCHDOG}" ] || die "в checkout отсутствует ${WATCHDOG}"
[ -f "${VALIDATOR}" ] || die "в checkout отсутствует ${VALIDATOR}"
[ -f "${BOOTSTRAP}" ] || die "в checkout отсутствует ${BOOTSTRAP}"

python3 "${VALIDATOR}" "${PROFILE}"

OMP_BASE="$(omp config path)"
[ -n "${OMP_BASE}" ] || die "omp config path вернул пустой путь"
mkdir -p "${OMP_BASE}"

echo "== Переносимый профиль OMP =="
install_portable_file "${PROFILE}" "${OMP_BASE}/config.yml" "config.yml"
install_portable_file "${WATCHDOG}" "${OMP_BASE}/WATCHDOG.md" "WATCHDOG.md"

echo "== Скиллы и нативный контур =="
bash "${BOOTSTRAP}"

cat <<'EOF'

Готово. Перезапустите OMP, затем самостоятельно авторизуйте нужных вендоров:
  /login anthropic
  /login openai-codex
  /login google-antigravity
  /login xai-oauth
  /login kimi-code
EOF
