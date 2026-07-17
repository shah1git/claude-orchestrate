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

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
TELEMETRY_LINK="${SCRIPT_DIR}/skill/orchestrate/telemetry"

if [ ! -L "${TELEMETRY_LINK}" ]; then
  echo "✗ ${TELEMETRY_LINK} — не симлинк: эта машина ещё не подключена к телеметрийному репо (см. шапку скрипта)" >&2
  exit 1
fi

# Клон = родитель каталога machines/, в который ведёт симлинк.
MACHINE_DIR="$(readlink -f "${TELEMETRY_LINK}")"
CLONE_DIR="$(dirname "$(dirname "${MACHINE_DIR}")")"
if [ ! -d "${CLONE_DIR}/.git" ]; then
  echo "✗ ${CLONE_DIR} — не git-клон; симлинк ведёт не туда" >&2
  exit 1
fi

cd "${CLONE_DIR}"
git add -A
if git diff --cached --quiet; then
  echo "Локальных новых записей нет."
else
  git commit -q -m "telemetry: $(basename "${MACHINE_DIR}") $(date -u +%Y-%m-%dT%H:%MZ)"
  echo "Закоммичено: $(git log -1 --format=%s)"
fi
git pull --rebase -q
git push -q
echo "Синхронизировано: $(git log -1 --format='%h %s')"
