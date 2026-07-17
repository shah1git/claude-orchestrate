#!/usr/bin/env bash
# =============================================================================
# bootstrap-mac.sh — полный контур /orchestrate на новой машине (macOS; работает
# и на Linux). Предпосылка: четыре CLI — claude, codex, agy, grok — уже
# установлены и авторизованы (как на исходном ВПС). Всё остальное собирает
# этот скрипт:
#
#   1. Преф-лайт: git/node/rsync/claude обязательны; codex/agy/grok — желательны
#      (их отсутствие — штатная деградация лейнов, не ошибка установки).
#   2. /opt/claude-orchestrate  — этот репозиторий (скилл + 4 агента), clone/pull.
#   3. /opt/tools/agent-bridge  — мост к внешним агентам (codex-лейны, линза
#      Standards), clone/pull. Путь фиксирован: его ждёт config.yaml.
#   4. Набор скиллов Мэтта Покока — канон в ~/.agents/skills (реальные каталоги,
#      rsync из свежего клона апстрима), ~/.claude/skills/* — симлинки на канон,
#      ~/.codex/skills — девять симлинков хребта (тикет #11). Раскладка
#      байт-в-байт повторяет ВПС.
#   5. ./install.sh — штатная установка скилла оркестрации и агентов.
#   6. Самопроверка: bridge --detect, семь канонических файлов хребта,
#      предостережение об ultra-эффорте в ~/.codex/config.toml.
#
# Идемпотентен: повторный запуск = обновление всех трёх репозиториев и
# перекладка симлинков. Это и есть механизм «подтянуть свежие скиллы Покока».
#
# Первый запуск: клонируйте репозиторий куда удобно (симлинки установки будут
# указывать именно туда, поэтому место должно быть постоянным) и запустите
# скрипт из чекаута:
#   git clone https://github.com/shah1git/claude-orchestrate.git ~/projects/claude-orchestrate
#   bash ~/projects/claude-orchestrate/bootstrap-mac.sh
# sudo потребуется только для /opt/tools (путь моста фиксирован — его ждут
# вызовы лейнов в references/cross-provider.md).
# =============================================================================
set -euo pipefail

# Каталог оркестрации = репозиторий, в котором лежит сам скрипт (тот же приём,
# что в install.sh) — уважаем место, куда владелец положил чекаут. Жёсткий
# /opt/claude-orchestrate был бы вторым экземпляром, копии бы разъехались.
ORCH_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
if [ ! -d "${ORCH_DIR}/skill/orchestrate" ]; then
  echo "✗ рядом со скриптом нет skill/orchestrate — запускайте bootstrap-mac.sh из чекаута claude-orchestrate" >&2
  exit 1
fi
BRIDGE_DIR="/opt/tools/agent-bridge"
POCOCK_CACHE="/opt/tools/mattpocock-skills"   # клон апстрима; канон живёт не здесь,
                                              # а в ~/.agents/skills (копии, как на ВПС)
ORCH_REPO="https://github.com/shah1git/claude-orchestrate.git"
BRIDGE_REPO="https://github.com/shah1git/agent-bridge.git"
POCOCK_REPO="https://github.com/mattpocock/skills.git"

AGENTS_STORE="${HOME}/.agents/skills"         # канон скиллов (читают Orca-порождённые харнессы)
CLAUDE_SKILLS="${HOME}/.claude/skills"        # симлинки на канон (читает Claude Code)
CODEX_SKILLS="${HOME}/.codex/skills"          # девять симлинков хребта (читает Codex)

# Весь набор Покока, установленный на ВПС (engineering + productivity, 22 шт.).
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

# --- 1. Преф-лайт -----------------------------------------------------------
echo "== Преф-лайт =="
command -v git   >/dev/null || die "git не найден"
command -v rsync >/dev/null || die "rsync не найден"
command -v node  >/dev/null || die "node не найден (мосту нужен Node >= 18)"
node_major="$(node -p 'process.versions.node.split(".")[0]')"
[ "${node_major}" -ge 18 ] || die "Node ${node_major} < 18 — обновите Node"
ok "git, rsync, node ${node_major}"

command -v claude >/dev/null || die "claude CLI не найден — без него контур не работает"
ok "claude: $(command -v claude)"

# Внешние исполнители: отсутствие любого — предупреждение, не ошибка (config
# cross_provider.enabled: auto-detect штатно деградирует лейны в Claude-дефолты).
if command -v codex >/dev/null; then
  if codex login status >/dev/null 2>&1; then ok "codex: залогинен"; else warn "codex есть, но не залогинен — выполните: codex login"; fi
else warn "codex CLI нет — codex-лейны и линза Standards уйдут в Claude-фолбэк"; fi
if command -v agy >/dev/null; then ok "agy: $(command -v agy)"; else warn "agy нет — gemini-фолбэк линзы будет недоступен (не критично)"; fi
if command -v grok >/dev/null; then ok "grok: $(command -v grok)"; else warn "grok нет — grok-build пилот будет недоступен"; fi

# --- 2-3. Репозитории -------------------------------------------------------
# clone_or_pull DIR REPO: свежий клон либо ff-only pull; чужие правки не трёт.
clone_or_pull() {
  local dir="$1" repo="$2"
  if [ -d "${dir}/.git" ]; then
    git -C "${dir}" pull --ff-only && ok "обновлён ${dir}"
  else
    [ -w "$(dirname "${dir}")" ] || die "нет прав на $(dirname "${dir}") — выполните: sudo mkdir -p ${dir} && sudo chown -R \"\$(id -un)\" $(dirname "${dir}")"
    git clone "${repo}" "${dir}" && ok "клонирован ${dir}"
  fi
}

echo "== Репозитории =="
clone_or_pull "${ORCH_DIR}"   "${ORCH_REPO}"
clone_or_pull "${BRIDGE_DIR}" "${BRIDGE_REPO}"
clone_or_pull "${POCOCK_CACHE}" "${POCOCK_REPO}"

# --- 4. Скиллы Покока: канон + симлинки -------------------------------------
# Канон — реальные каталоги в ~/.agents/skills (та же раскладка, что на ВПС:
# одна копия — много точек входа). rsync --delete делает повторный запуск
# честным обновлением: локальный канон всегда равен свежему апстриму.
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

  # Симлинк ~/.claude/skills/<name> -> ../../.agents/skills/<name> (относительный,
  # как на ВПС). Существующий каталог-не-симлинк бережно уводится в .bak.
  link="${CLAUDE_SKILLS}/${name}"
  target="../../.agents/skills/${name}"
  if [ -L "${link}" ] && [ "$(readlink "${link}")" = "${target}" ]; then :; else
    if [ -e "${link}" ] || [ -L "${link}" ]; then mv "${link}" "${link}.bak"; warn "${name}: прежнее содержимое ~/.claude/skills отложено в .bak"; fi
    ln -s "${target}" "${link}"
  fi
done
ok "канон в ${AGENTS_STORE}, симлинки в ${CLAUDE_SKILLS}"

# Реестр Codex: девять абсолютных симлинков на канон через ~/.claude/skills —
# ровно как на ВПС (тикет #11). Codex-воркеры читают хребет отсюда.
if command -v codex >/dev/null; then
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
fi

# --- 5. Скилл оркестрации и агенты ------------------------------------------
echo "== /orchestrate =="
bash "${ORCH_DIR}/install.sh"

# --- 5.5 Телеметрия: подключение к приватному репо (если клон уже есть) ------
# Адрес телеметрийного репо здесь сознательно не хранится (главный репозиторий
# остаётся пригодным к публикации): шаг срабатывает, только если владелец сам
# склонировал приватное репо в один из двух конвенционных путей. Каждая машина
# пишет в machines/<hostname> — слияние бесконфликтно по построению; дальше
# синхронизация — telemetry-sync.sh.
echo "== Телеметрия =="
TELEM_LINK="${ORCH_DIR}/skill/orchestrate/telemetry"
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

# --- 6. Самопроверка ---------------------------------------------------------
echo "== Самопроверка =="
node "${BRIDGE_DIR}/run-external-agent.mjs" --detect || warn "bridge --detect завершился с ошибкой — проверьте Node и логины"

spine_missing=0
for name in grilling domain-modeling to-spec to-tickets implement tdd code-review; do
  [ -f "${CLAUDE_SKILLS}/${name}/SKILL.md" ] || { warn "хребет: нет ${name}/SKILL.md"; spine_missing=1; }
done
[ "${spine_missing}" -eq 0 ] && ok "семь канонических файлов хребта на месте"

# Унаследованный ultra-эффорт: для оркестрации безопасно (лейны пинуют эффорт
# явно), но ручной `codex` без флага будет молча жечь квоту на максимуме.
if [ -f "${HOME}/.codex/config.toml" ] && grep -q 'model_reasoning_effort *= *"ultra"' "${HOME}/.codex/config.toml"; then
  warn 'в ~/.codex/config.toml стоит model_reasoning_effort="ultra" — помните об этом при ручных запусках codex'
fi

echo
echo "Готово. Дальше:"
echo "  1. Перезапустите Claude Code — он перечитает реестр скиллов."
echo "  2. В каждом новом проекте один раз выполните настройку по файлу"
echo "     setup-matt-pocock-skills (трекер, метки триажа, путь документов)."
echo "  3. Обновление всего контура в будущем — просто повторный запуск:"
echo "     bash ${ORCH_DIR}/bootstrap-mac.sh"
