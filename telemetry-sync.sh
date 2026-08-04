#!/usr/bin/env bash
# =============================================================================
# telemetry-sync.sh — синхронизация телеметрии /orchestrate с приватным репо.
#
# Устройство: skill/orchestrate/telemetry на каждой машине — СИМЛИНК на
# machines/<hostname> внутри локального клона приватного телеметрийного репо.
# Каждая машина пишет только в свой каталог, поэтому слияние бесконфликтно по
# построению. Этот скрипт коммитит локальные записи, забирает чужие машины
# (pull --rebase) и пушит.
#
# Скрипт сознательно НЕ знает адреса приватного репо: он работает с origin
# того клона, куда указывает симлинк. Главный репозиторий остаётся пригодным
# к публикации — ни одной приватной строки.
#
# Подключение новой машины (один раз):
#   git clone <адрес приватного телеметрийного репо> ~/orchestrate-telemetry
#   mkdir -p ~/orchestrate-telemetry/machines/"$(hostname)"
#   mv  <чекаут>/skill/orchestrate/telemetry/* ~/orchestrate-telemetry/machines/"$(hostname)"/ 2>/dev/null || true
#   rmdir <чекаут>/skill/orchestrate/telemetry
#   ln -s ~/orchestrate-telemetry/machines/"$(hostname)" <чекаут>/skill/orchestrate/telemetry
# =============================================================================
set -euo pipefail

run_with_timeout() {
  if command -v timeout >/dev/null 2>&1; then
    timeout 120 "$@"
    return
  fi
  if command -v gtimeout >/dev/null 2>&1; then
    gtimeout 120 "$@"
    return
  fi
  if ! command -v python3 >/dev/null 2>&1; then
    echo "✗ для 120-секундного лимита git-вызовов нужен timeout, gtimeout или python3" >&2
    return 1
  fi
  python3 - "$@" <<'PY'
import subprocess
import sys

try:
    result = subprocess.run(sys.argv[1:], check=False, timeout=120)
except subprocess.TimeoutExpired:
    print("git-вызов превысил лимит 120 секунд", file=sys.stderr)
    raise SystemExit(124)
raise SystemExit(result.returncode)
PY
}

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
TELEMETRY_LINK="${SCRIPT_DIR}/skill/orchestrate/telemetry"

if [ ! -L "${TELEMETRY_LINK}" ]; then
  echo "✗ ${TELEMETRY_LINK} — не симлинк: эта машина ещё не подключена к телеметрийному репо (см. шапку скрипта)" >&2
  exit 1
fi

# Клон = родитель каталога machines/, в который ведёт симлинк.
MACHINE_DIR="$(readlink -f "${TELEMETRY_LINK}")"
MACHINE_NAME="$(hostname)"
case "${MACHINE_DIR}" in
  */"machines/${MACHINE_NAME}") ;;
  *)
    echo "✗ ${TELEMETRY_LINK} должен вести в machines/${MACHINE_NAME}, получено ${MACHINE_DIR}" >&2
    exit 1
    ;;
esac
CLONE_DIR="$(dirname "$(dirname "${MACHINE_DIR}")")"
if [ ! -d "${CLONE_DIR}/.git" ]; then
  echo "✗ ${CLONE_DIR} — не git-клон; симлинк ведёт не туда" >&2
  exit 1
fi

cd "${CLONE_DIR}"
MACHINE_PATH="machines/${MACHINE_NAME}"
if ! git diff --cached --quiet -- . ":(exclude)${MACHINE_PATH}"; then
  echo "✗ индекс содержит подготовленные изменения вне ${MACHINE_PATH}; синхронизация их не изменяет" >&2
  exit 1
fi
git add -A -- "${MACHINE_PATH}"
if git diff --cached --quiet -- "${MACHINE_PATH}"; then
  echo "Локальных новых записей нет."
else
  git commit -q -m "telemetry: ${MACHINE_NAME} $(date -u +%Y-%m-%dT%H:%MZ)" --only -- "${MACHINE_PATH}"
  echo "Закоммичено: $(git log -1 --format=%s)"
fi
run_with_timeout git pull --rebase -q
run_with_timeout git push -q
echo "Синхронизировано: $(git log -1 --format='%h %s')"
