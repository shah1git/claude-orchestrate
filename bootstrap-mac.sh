#!/usr/bin/env bash
# =============================================================================
# bootstrap-mac.sh — полный контур /orchestrate на новой машине (macOS; работает
# и на Linux). Предпосылка: пять CLI — claude, codex, agy, grok, kimi — уже
# установлены и авторизованы (как на исходном ВПС). Всё остальное собирает
# этот скрипт:
#
#   1. Преф-лайт: git/rsync/claude/python3 обязательны (плюс PyYAML для
#      инструментов — warn); node/codex/agy/grok/kimi/gh — желательны (их
#      отсутствие — штатная деградация лейнов/удобств, не ошибка установки).
#   2. /opt/claude-orchestrate  — этот репозиторий (три головы orchestrate/
#      orchestrate-frontier/orca_orchestrate + 4 агента + движок run-lane),
#      clone/pull.
#   3. Набор скиллов Мэтта Покока — канон в ~/.agents/skills (реальные каталоги,
#      rsync из свежего клона апстрима), ~/.claude/skills/* — симлинки на канон,
#      ~/.codex/skills — девять симлинков хребта (тикет #11). Раскладка
#      байт-в-байт повторяет ВПС.
#   4. ./install.sh — штатная установка трёх голов оркестрации и четырёх агентов.
#   5. Самопроверка: семь канонических файлов хребта, предостережение об
#      ultra-эффорте в ~/.codex/config.toml.
#
# Мост /opt/tools/agent-bridge из этого контура УБРАН (2026-07-22): транспорт
# унифицирован на официальные консольные утилиты вендоров через движок run-lane,
# отдельный мост и его `--detect` больше не нужны (ADR-0005/0006).
#
# Идемпотентен: повторный запуск = обновление обоих репозиториев (наш + апстрим
# Покока) и перекладка симлинков. Это и есть механизм «подтянуть свежие скиллы».
# Если pull обновил сам bootstrap-mac.sh — скрипт перезапускает себя свежей
# версией (bash нельзя доверять дочитывание файла, изменившегося под ногами).
#
# Первый запуск: клонируйте репозиторий куда удобно (симлинки установки будут
# указывать именно туда, поэтому место должно быть постоянным) и запустите
# скрипт из чекаута:
#   git clone https://github.com/shah1git/claude-orchestrate.git ~/projects/claude-orchestrate
#   bash ~/projects/claude-orchestrate/bootstrap-mac.sh
# sudo может потребоваться только для создания /opt/tools — там кэш апстрима
# скиллов Покока (раскладка байт-в-байт повторяет ВПС).
# =============================================================================
set -euo pipefail

# Каталог оркестрации = репозиторий, в котором лежит сам скрипт (тот же приём,
# что в install.sh) — уважаем место, куда владелец положил чекаут. Жёсткий
# /opt/claude-orchestrate был бы вторым экземпляром, копии бы разъехались.
if ! readlink -f / >/dev/null 2>&1; then
  echo "✗ требуется readlink -f: macOS ≥ 12.3, либо GNU coreutils c gnubin в PATH — brew install coreutils && export PATH=\"\$(brew --prefix coreutils)/libexec/gnubin:\$PATH\"" >&2
  exit 1
fi
ORCH_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
if [ ! -d "${ORCH_DIR}/skill/orchestrate" ]; then
  echo "✗ рядом со скриптом нет skill/orchestrate — запускайте bootstrap-mac.sh из чекаута claude-orchestrate" >&2
  exit 1
fi
POCOCK_CACHE="/opt/tools/mattpocock-skills"   # клон апстрима; канон живёт не здесь,
                                              # а в ~/.agents/skills (копии, как на ВПС)
ORCH_REPO="https://github.com/shah1git/claude-orchestrate.git"
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
ok "git, rsync"
if command -v gh >/dev/null; then ok "gh: $(command -v gh)"; else warn "gh нет — автоклон приватной телеметрии и работа с трекером недоступны (brew install gh)"; fi
# node этому скрипту больше не обязателен: его единственный потребитель — мост —
# ретирован. Оставляем мягкую проверку, поскольку часть вендорских CLI (напр.
# agy-proxy) поставляется как Node-пакеты; отсутствие node их и деградирует.
if command -v node >/dev/null; then
  node_major="$(node -p 'process.versions.node.split(".")[0]' 2>/dev/null || echo 0)"
  if [ "${node_major}" -ge 18 ]; then ok "node ${node_major}"; else warn "Node ${node_major} < 18 — обновите, если вендорский CLI на Node этого требует"; fi
else
  warn "node не найден — не критично (мост ретирован); нужен лишь части вендорских CLI"
fi

command -v claude >/dev/null || die "claude CLI не найден — без него контур не работает"
ok "claude: $(command -v claude)"
command -v python3 >/dev/null || die "python3 не найден — без него run-lane, валидатор конфига и телеметрия не работают"
ok "python3: $(command -v python3)"
if python3 -c 'import yaml' 2>/dev/null; then ok "PyYAML"; else warn "PyYAML не найден — выполните: pip3 install pyyaml (без него run-lane/validate_config/telemetry_append не стартуют)"; fi

# Внешние исполнители: отсутствие любого — предупреждение, не ошибка (config
# cross_provider.enabled: auto-detect штатно деградирует лейны в Claude-дефолты).
if command -v codex >/dev/null; then
  if codex login status >/dev/null 2>&1; then ok "codex: залогинен"; else warn "codex есть, но не залогинен — выполните: codex login"; fi
else warn "codex CLI нет — codex-лейны и линза Standards уйдут в Claude-фолбэк"; fi
if command -v agy >/dev/null; then ok "agy: $(command -v agy)"; else warn "agy нет — весь Google-пул недоступен: gemini-flash (разведка, судейские роли, builder-пилот v30) и agy-opus (фронтир-Claude на квоте Google)"; fi
if command -v grok >/dev/null; then ok "grok: $(command -v grok)"; else warn "grok нет — лейн grok-build (полный: builder/разведка/судейские роли) недоступен"; fi
if command -v kimi >/dev/null || [ -x "${HOME}/.kimi-code/bin/kimi" ]; then ok "kimi: $(command -v kimi || printf '%s' "${HOME}/.kimi-code/bin/kimi")"; else warn "kimi CLI нет — лейн kimi-k3 (builder/разведка/судейские роли) недоступен"; fi

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

# --- 5.7 Ужесточение Grok (config v17; references/grok/) ---------------------
# Только если grok CLI установлен. Три уровня: config.toml (ставим, если нет —
# не затираем твои ключи), кастомный fail-closed профиль песочницы sandbox.toml
# (генерируем с АБСОЛЮТНЫМИ deny-путями этой машины — тильда в deny не
# раскрывается), bubblewrap (обязателен для fail-closed). env-выключатели
# спавнер лейна ставит сам (cross_provider.grok_hardening.env).
if command -v grok >/dev/null; then
  echo "== Ужесточение Grok =="
  GROK_HOME="${HOME}/.grok"
  mkdir -p "${GROK_HOME}"

  # config.toml — ставим, только если файла ещё нет (иначе можем затереть ключи).
  if [ -f "${GROK_HOME}/config.toml" ]; then
    # Не заменяем файл (в нём ключи владельца), НО чиним одну конкретную строку:
    # прежние версии bootstrap вписывали profile = "orchestrate" в этот ГЛОБАЛЬНЫЙ
    # файл, из-за чего интерактивный grok владельца тоже садился в песочницу и терял
    # доступ к ~/projects/git/.env/push (баг, исправлен 2026-07-24). Меняем ТОЛЬКО
    # эту строку на "off" — ключи и прочие настройки не трогаем. Лейн защищает себя
    # сам флагом --sandbox (references/grok/config.toml объясняет почему).
    if grep -Eq '^[[:space:]]*profile[[:space:]]*=[[:space:]]*"orchestrate"' "${GROK_HOME}/config.toml"; then
      awk '/^[[:space:]]*profile[[:space:]]*=[[:space:]]*"orchestrate"/{print "profile = \"off\"  # правка bootstrap 2026-07-24: глобально песочницу не форсим (ломала интерактив); лейн включает её сам флагом"; next} {print}' \
        "${GROK_HOME}/config.toml" > "${GROK_HOME}/config.toml.tmp" && mv "${GROK_HOME}/config.toml.tmp" "${GROK_HOME}/config.toml"
      ok "~/.grok/config.toml: profile \"orchestrate\" -> \"off\" (интерактивный grok разблокирован; защита лейна не тронута)"
      warn "перезапусти grok НОВОЙ сессией — песочница фиксируется на сессию, resume её не снимет"
    else
      warn "~/.grok/config.toml уже есть — не трогаю (нет форс-строки profile=orchestrate); при желании сверь с ${ORCH_DIR}/skill/orchestrate/references/grok/config.toml"
    fi
  else
    cp "${ORCH_DIR}/skill/orchestrate/references/grok/config.toml" "${GROK_HOME}/config.toml"
    ok "~/.grok/config.toml установлен (выключатели телеметрии/выгрузки; песочницу лейн включает сам флагом, глобально НЕ форсится)"
  fi

  # sandbox.toml — генерируем с реальными абсолютными deny-путями (только
  # существующие каталоги; тильда в deny grok'ом НЕ раскрывается).
  deny_entries=""
  for p in "${HOME}/.ssh" "${HOME}/.claude" "${HOME}/.codex" "${HOME}/.agents" /opt/rikanv-doctrine; do
    [ -e "${p}" ] && deny_entries="${deny_entries}\"${p}\", "
  done
  {
    echo "# Кастомный fail-closed профиль песочницы Grok для /orchestrate."
    echo "# Сгенерирован bootstrap-mac.sh с абсолютными deny-путями этой машины"
    echo "# (тильда в deny НЕ раскрывается). Наличие deny => обязателен bubblewrap"
    echo "# => fail-closed вместо fail-open встроенных профилей. Обоснование —"
    echo "# skill/orchestrate/references/grok/README.md."
    echo "[profiles.orchestrate]"
    echo "extends = \"strict\""
    echo "deny = [${deny_entries%, }]"
  } > "${GROK_HOME}/sandbox.toml"
  ok "~/.grok/sandbox.toml сгенерирован (deny: $(echo "${deny_entries%, }" | tr -d '"'))"

  # bubblewrap — обязателен для fail-closed кастомного профиля, но только на
  # Linux: на macOS ту же работу делает родной Seatbelt, ставить нечего.
  if [ "$(uname -s)" = "Darwin" ]; then
    ok "macOS: песочница — родной Seatbelt, bwrap не нужен; профиль orchestrate применится нативно"
  elif command -v bwrap >/dev/null; then
    ok "bubblewrap: $(command -v bwrap)"
  elif command -v apt-get >/dev/null; then
    warn "bubblewrap не найден — ставлю (нужен sudo)"; sudo apt-get install -y bubblewrap >/dev/null 2>&1 && ok "bubblewrap установлен" || warn "не смог поставить bubblewrap — установи вручную (apt), иначе профиль orchestrate не стартует"
  else
    warn "bubblewrap не найден, apt-get недоступен — поставь пакет bubblewrap средствами своего дистрибутива, иначе профиль orchestrate не стартует"
  fi

  # Пред-открытый бинарь: предупреждаем про самосборку.
  gv="$(grok --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)"
  if [ "${gv}" = "0.2.101" ] || [ -z "${gv}" ]; then
    warn "grok ${gv:-?} собран до открытия исходников — цель: самосборка 0.2.102 из xai-org/grok-build (references/grok/README.md); до неё лейны на официальном бинаре, ужатом конфигом"
  fi
fi

# --- 5.8 Ужесточение Kimi Code CLI (аудит 2026-07-18; references/kimi/) --------
# Только если kimi установлен. Безопасная часть (telemetry=false) — в глобальный
# конфиг; функцио-затрагивающие ручки (auto-update, cron, services) НЕ форсим
# глобально — их лейн передаёт через env при спавне (не трогает интерактивные
# сессии владельца в omp/нативном CLI). Молчаливой выгрузки репо у Kimi нет
# (в отличие от Grok) — см. references/kimi/README.md.
if command -v kimi >/dev/null || [ -x "${HOME}/.kimi-code/bin/kimi" ]; then
  echo "== Ужесточение Kimi =="
  KCFG="${HOME}/.kimi-code/config.toml"
  if [ -f "${KCFG}" ]; then
    # telemetry — ключ КОРНЕВОЙ таблицы TOML, поэтому ищем его только до первого
    # заголовка [секции]: одноимённый ключ внутри чужой секции — другой ключ,
    # а слепое добавление при уже существующем корневом дало бы дубликат —
    # невалидный TOML. Остальной конфиг (ключи логина и т.п.) не трогаем.
    kimi_root_telemetry="$(awk '/^\[/{exit} /^[[:space:]]*telemetry[[:space:]]*=/{print; exit}' "${KCFG}")"
    if [ -z "${kimi_root_telemetry}" ]; then
      # Вставка первой строкой: всё до первой [секции] попадает в корень TOML.
      printf 'telemetry = false  # egress-hardening (references/kimi)\n' | cat - "${KCFG}" > "${KCFG}.tmp" && mv "${KCFG}.tmp" "${KCFG}"
      ok "~/.kimi-code/config.toml: добавлено telemetry = false (privacy, без потери функций)"
    elif printf '%s\n' "${kimi_root_telemetry}" | grep -q 'false'; then
      ok "~/.kimi-code/config.toml: telemetry уже выключена"
    else
      # Явное telemetry = true переключаем: телеметрия Kimi выключена на всех
      # машинах контура (references/kimi/README.md).
      awk '!intable && /^\[/{intable=1}
           !intable && /^[[:space:]]*telemetry[[:space:]]*=/{print "telemetry = false  # egress-hardening (references/kimi)"; next}
           {print}' "${KCFG}" > "${KCFG}.tmp" && mv "${KCFG}.tmp" "${KCFG}"
      ok "~/.kimi-code/config.toml: telemetry переключена в false"
    fi
  else
    warn "~/.kimi-code/config.toml нет — залогинься в kimi, затем перезапусти bootstrap"
  fi
  warn "лейн Kimi спавнить с env: KIMI_DISABLE_TELEMETRY=1 KIMI_CODE_NO_AUTO_UPDATE=1 KIMI_DISABLE_CRON=1 (см. references/kimi/README.md); /feedback не использовать"
fi

# --- 6. Самопроверка ---------------------------------------------------------
echo "== Самопроверка =="
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
