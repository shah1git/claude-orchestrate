#!/usr/bin/env bash
# =============================================================================
# bootstrap-mac.sh — нативный OMP-контур Pocock на новой машине (macOS; работает
# и на Linux). Скрипт собирает всё необходимое:
#
#   1. Префлайт: git, rsync, gh, OMP CLI, Python 3 и PyYAML обязательны.
#   2. Этот репозиторий и свежий апстримный набор скиллов Покока обновляются
#      через clone/pull.
#   3. Набор скиллов Мэтта Покока — канон в ~/.agents/skills (реальные каталоги,
#      rsync из свежего клона апстрима). ~/.claude/skills/* и ~/.codex/skills
#      остаются совместимыми зеркалами этого канона для самостоятельных harnesses.
#   4. ./install.sh --configure-omp --configure-pocock-roles устанавливает
#      нативных capability-агентов из .omp/agents и расширение OMP
#      pocock-control. Общая управляющая плоскость голов Pocock —
#      skill/orchestrate/tools/omp_runtime.py; рабочий транспорт — нативный
#      пакетный OMP task.
#   5. Телеметрия подключается к приватному репозиторию, если он доступен.

#
# Идемпотентен: повторный запуск = обновление обоих репозиториев (наш + апстрим
# Покока) и перекладка симлинков. Это и есть механизм «подтянуть свежие скиллы».
# Если pull обновил сам bootstrap-mac.sh — скрипт перезапускает себя свежей
# версией (bash нельзя доверять дочитывание файла, изменившегося под ногами).
#
# Первый запуск вручную: клонируйте репозиторий куда удобно и запустите скрипт
# из чекаута. Однокомандный перенос профиля и этот bootstrap выполняет
# bootstrap-machine.sh.
#   git clone https://github.com/shah1git/claude-orchestrate.git ~/projects/claude-orchestrate
#   bash ~/projects/claude-orchestrate/bootstrap-mac.sh
# Апстримный кэш по умолчанию хранится в пользовательском XDG cache; путь можно
# переопределить через POCOCK_CACHE_DIR.
# =============================================================================
set -euo pipefail

# Каталог нативного OMP-контура = репозиторий, в котором лежит сам скрипт (тот
# же приём, что в install.sh): уважаем место, куда владелец положил чекаут.
# Жёсткий /opt/claude-orchestrate был бы вторым экземпляром, копии бы разъехались.
if ! readlink -f / >/dev/null 2>&1; then
  echo "✗ требуется readlink -f: macOS ≥ 12.3, либо GNU coreutils c gnubin в PATH — brew install coreutils && export PATH=\"\$(brew --prefix coreutils)/libexec/gnubin:\$PATH\"" >&2
  exit 1
fi
ORCH_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
if [ ! -d "${ORCH_DIR}/skill/orchestrate" ] \
   || [ ! -d "${ORCH_DIR}/.omp/agents" ] \
   || [ ! -d "${ORCH_DIR}/.omp/extensions/pocock-control" ] \
   || [ ! -f "${ORCH_DIR}/skill/orchestrate/tools/omp_runtime.py" ]; then
  echo "✗ рядом со скриптом нет полного нативного OMP-контура (.omp/agents, pocock-control или omp_runtime.py) — запускайте bootstrap-mac.sh из чекаута этого репозитория" >&2
  exit 1
fi
POCOCK_CACHE="${POCOCK_CACHE_DIR:-${XDG_CACHE_HOME:-${HOME}/.cache}/claude-orchestrate/mattpocock-skills}"
                                              # Апстрим-кэш вне системного /opt;
                                              # канон по-прежнему живёт в ~/.agents/skills.
ORCH_REPO="https://github.com/shah1git/claude-orchestrate.git"
POCOCK_REPO="https://github.com/mattpocock/skills.git"

AGENTS_STORE="${HOME}/.agents/skills"         # канон скиллов для OMP и совместимых harnesses
CLAUDE_SKILLS="${HOME}/.claude/skills"        # совместимое зеркало для Claude Code, не часть OMP-контура
CODEX_SKILLS="${HOME}/.codex/skills"          # совместимое зеркало для Codex, не часть OMP-контура

# Канонический набор Мэтта Покока (engineering + productivity, 22 шт.).
POCOCK_SKILLS=(
  ask-matt code-review codebase-design diagnosing-bugs domain-modeling
  grill-me grill-with-docs grilling handoff implement
  improve-codebase-architecture prototype research resolving-merge-conflicts
  setup-matt-pocock-skills tdd teach to-spec to-tickets triage wayfinder
  writing-great-skills
)
# Девять симлинков реестра Codex (тикет #11 — состав хребта + wayfinder/research/handoff).
CODEX_LINKS=(code-review grill-with-docs handoff implement research tdd to-spec to-tickets wayfinder)

ok()   { printf '  \033[32m✓\033[0m %s\n' "$1"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$1"; }
die()  { printf '  \033[31m✗\033[0m %s\n' "$1" >&2; exit 1; }

# --- 1. Префлайт -----------------------------------------------------------
echo "== Префлайт =="
command -v git   >/dev/null || die "git не найден"
command -v rsync >/dev/null || die "rsync не найден"
command -v gh    >/dev/null || die "gh не найден — он требуется для телеметрии и работы с трекером"
command -v omp   >/dev/null || die "OMP CLI не найден — нативный контур Pocock не может быть установлен"
command -v python3 >/dev/null || die "python3 не найден — без него не запускается omp_runtime.py"
python3 -c 'import yaml' 2>/dev/null || die "PyYAML не найден — выполните: pip3 install pyyaml; без него не запускается omp_runtime.py"
ok "git, rsync, gh"
ok "OMP: $(command -v omp)"
ok "Python 3 + PyYAML"

# --- 2-3. Репозитории -------------------------------------------------------
# clone_or_pull DIR REPO: свежий клон либо ff-only pull; чужие правки не трёт.
clone_or_pull() {
  local dir="$1" repo="$2" parent
  if [ -d "${dir}/.git" ]; then
    git -C "${dir}" pull --ff-only && ok "обновлён ${dir}"
  else
    # На свежей машине родительского каталога кэша ещё нет, а `-w` на
    # несуществующем пути ложен. Прежняя проверка читала это как отсутствие
    # прав и предлагала sudo, то есть создавала root-овые каталоги в домашней
    # папке владельца — вред вместо помощи. Создаём путь сами, как это уже
    # делает ensure_checkout в bootstrap-machine.sh.
    parent="$(dirname "${dir}")"
    mkdir -p "${parent}" || die "не удалось создать ${parent} — если каталог принадлежит другому пользователю (например, остался от запуска под sudo), верните его себе: chown -R \"\$(id -un)\" ${parent}"
    git clone "${repo}" "${dir}" && ok "клонирован ${dir}"
  fi
}

echo "== Репозитории =="
orch_head_before="$(git -C "${ORCH_DIR}" rev-parse HEAD 2>/dev/null || true)"
clone_or_pull "${ORCH_DIR}"   "${ORCH_REPO}"
# Самообновление: если pull сменил ревизию, мог измениться и этот файл — дальше
# исполнялась бы старая копия из буфера bash, и свежие шаги молча не выполнились
# бы. Перезапускаем себя свежей версией; env-предохранитель исключает цикл.
if [ -z "${ORCH_BOOTSTRAP_REEXEC:-}" ] && [ -n "${orch_head_before}" ] \
   && [ "${orch_head_before}" != "$(git -C "${ORCH_DIR}" rev-parse HEAD)" ]; then
  echo "  ↻ репозиторий обновился — перезапускаю свежий bootstrap"
  ORCH_BOOTSTRAP_REEXEC=1 exec bash "${ORCH_DIR}/bootstrap-mac.sh"
fi
clone_or_pull "${POCOCK_CACHE}" "${POCOCK_REPO}"

# --- 4. Скиллы Покока: канон + симлинки -------------------------------------
# Канон — реальные каталоги в ~/.agents/skills: одна копия — много точек входа.
# rsync --delete делает повторный запуск честным обновлением: локальный канон всегда
# равен свежему апстриму.
# ВНИМАНИЕ: локальные правки этих 22 каталогов не переживут обновление —
# своё держите в отдельных скиллах, канон Покока неприкосновенен.
echo "== Скиллы Покока (${#POCOCK_SKILLS[@]} шт.) =="
mkdir -p "${AGENTS_STORE}" "${CLAUDE_SKILLS}"
for name in "${POCOCK_SKILLS[@]}"; do
  src=""
  for category in engineering productivity; do
    [ -d "${POCOCK_CACHE}/skills/${category}/${name}" ] && src="${POCOCK_CACHE}/skills/${category}/${name}"
  done
  [ -n "${src}" ] || { warn "${name}: не найден в апстриме (переименован?) — пропущен"; continue; }
  rsync -a --delete "${src}/" "${AGENTS_STORE}/${name}/"

  # Симлинк ~/.claude/skills/<name> -> ../../.agents/skills/<name> (относительный).
  # Существующий каталог-не-симлинк бережно уводится в .bak.
  link="${CLAUDE_SKILLS}/${name}"
  target="../../.agents/skills/${name}"
  if [ -L "${link}" ] && [ "$(readlink "${link}")" = "${target}" ]; then :; else
    if [ -e "${link}" ] || [ -L "${link}" ]; then mv "${link}" "${link}.bak"; warn "${name}: прежнее содержимое ~/.claude/skills отложено в .bak"; fi
    ln -s "${target}" "${link}"
  fi
done
ok "канон в ${AGENTS_STORE}, симлинки в ${CLAUDE_SKILLS}"

# Совместимый реестр Codex: девять симлинков на канон через ~/.claude/skills.
# Это потребительское зеркало скиллов для отдельного harness Codex; нативный
# контур Pocock устанавливается независимо в OMP.
mkdir -p "${CODEX_SKILLS}"
for name in "${CODEX_LINKS[@]}"; do
  link="${CODEX_SKILLS}/${name}"
  target="${HOME}/.claude/skills/${name}"
  if [ -L "${link}" ] && [ "$(readlink "${link}")" = "${target}" ]; then :; else
    if [ -e "${link}" ] || [ -L "${link}" ]; then mv "${link}" "${link}.bak"; fi
    ln -s "${target}" "${link}"
  fi
done
ok "реестр Codex: ${#CODEX_LINKS[@]} симлинков в ${CODEX_SKILLS}"

# --- 5. Нативный контур OMP ------------------------------------------------
echo "== Нативный контур OMP =="
bash "${ORCH_DIR}/install.sh" --configure-omp --configure-pocock-roles

# --- 5.5 Телеметрия: подключение к приватному репо (если клон уже есть) ------
# Адрес телеметрийного репо здесь сознательно не хранится (главный репозиторий
# остаётся пригодным к публикации): шаг срабатывает, только если владелец сам
# склонировал приватное репо в один из двух конвенционных путей. Каждая машина
# пишет в machines/<hostname> — слияние бесконфликтно по построению; дальше
# синхронизация — telemetry-sync.sh.
echo "== Телеметрия =="
TELEM_LINK="${ORCH_DIR}/skill/orchestrate/telemetry"

# Автоклон приватного репо. Адрес в публичном скрипте по-прежнему не хранится —
# он ВЫВОДИТСЯ: владелец берётся из origin этого чекаута, имя конвенционное
# (orchestrate-telemetry, оно и так закреплено путями ниже). У форка выведется
# его собственный владелец — репозиторий остаётся пригодным к публикации.
# Переопределение при необходимости: ORCH_TELEMETRY_REPO=owner/name.
if [ ! -L "${TELEM_LINK}" ] && [ ! -d "${HOME}/orchestrate-telemetry/.git" ] && [ ! -d "/opt/orchestrate-telemetry/.git" ]; then
  telem_slug="${ORCH_TELEMETRY_REPO:-}"
  if [ -z "${telem_slug}" ]; then
    telem_owner="$(git -C "${ORCH_DIR}" remote get-url origin 2>/dev/null \
      | sed -nE 's#^(git@github\.com:|https://github\.com/)([^/]+)/.*#\2#p')"
    [ -n "${telem_owner}" ] && telem_slug="${telem_owner}/orchestrate-telemetry"
  fi
  if [ -n "${telem_slug}" ]; then
    # Репо приватный: клонируем через gh (он умеет авторизацию); фолбэк — git
    # с запретом интерактивного запроса пароля, чтобы падать сразу, а не висеть.
    if command -v gh >/dev/null && gh auth status >/dev/null 2>&1; then
      gh repo clone "${telem_slug}" "${HOME}/orchestrate-telemetry" \
        && ok "клонирован ${telem_slug} -> ~/orchestrate-telemetry" \
        || warn "клон ${telem_slug} не удался (нет репо или доступа) — телеметрия останется локальной"
    else
      GIT_TERMINAL_PROMPT=0 git clone "https://github.com/${telem_slug}.git" "${HOME}/orchestrate-telemetry" \
        && ok "клонирован ${telem_slug} -> ~/orchestrate-telemetry" \
        || warn "клон ${telem_slug} не удался (приватный репо без gh?) — выполни: gh repo clone ${telem_slug} ~/orchestrate-telemetry и перезапусти"
    fi
  fi
fi

for cand in "${HOME}/orchestrate-telemetry" "/opt/orchestrate-telemetry"; do
  if [ -d "${cand}/.git" ] && [ ! -L "${TELEM_LINK}" ]; then
    mdir="${cand}/machines/$(hostname)"
    mkdir -p "${mdir}"
    if [ -d "${TELEM_LINK}" ]; then
      # Переносим накопленное на этой машине в её каталог (перенос, не удаление).
      find "${TELEM_LINK}" -mindepth 1 -maxdepth 1 -exec mv {} "${mdir}/" \;
      rmdir "${TELEM_LINK}"
    fi
    ln -s "${mdir}" "${TELEM_LINK}"
    ok "телеметрия подключена: ${TELEM_LINK} -> ${mdir}"
    break
  fi
done
if [ -L "${TELEM_LINK}" ]; then
  ok "телеметрия пишется в приватное репо ($(readlink "${TELEM_LINK}"))"
else
  warn "телеметрия локальная: клона телеметрийного репо нет — склонируйте его в ~/orchestrate-telemetry и перезапустите (см. telemetry-sync.sh)"
fi

echo "== Самопроверка установленного контура =="
bash "${ORCH_DIR}/scripts/verify-install.sh"


echo
echo "Готово. Нативный OMP-контур установлен из .omp/agents; расширение pocock-control использует управляющую плоскость omp_runtime.py."
echo "  1. В каждом новом проекте один раз выполните настройку по файлу"
echo "     setup-matt-pocock-skills (трекер, метки триажа, путь документов)."
echo "  2. Обновление всего контура в будущем — просто повторный запуск:"
echo "     bash ${ORCH_DIR}/bootstrap-mac.sh"
echo "  3. После установки или обновления перезапустите OMP — он перечитает нативных агентов и расширение pocock-control."
