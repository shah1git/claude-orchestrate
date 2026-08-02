#!/usr/bin/env bash
# =============================================================================
# verify-install.sh — самопроверка установленного контура /orchestrate.
#
# Отвечает ровно на один вопрос: «нативный OMP-контур этого чекаута установлен
# полностью и согласован?» Проверяются публичные головы в ~/.agents/skills,
# capability-agents и extension в активной базе OMP, эффективные глобальные
# task-инварианты, Python-runtime, конфиг и регресс-стражи.
#
# Состав голов и OMP-агентов выводится из чекаута, а не дублируется списком.
# Поэтому удалённый или переименованный артефакт не может незаметно остаться
# обязательной частью проверки.
#
# Запуск:
#   bash scripts/verify-install.sh              # полная проверка
#   bash scripts/verify-install.sh --no-tests   # без прогона тестов (быстро)
#   bash scripts/verify-install.sh --offline    # без обращения к origin
#
# Код возврата: 0 — установка исправна (предупреждения допустимы); 1 — есть
# провалы; 2 — ошибка запуска (нет readlink -f, скрипт вне чекаута).
# Вызывается в конце bootstrap-mac.sh и пригоден к самостоятельному запуску —
# одно определение «правильной установки» на оба случая, без дублирования.
#
# Совместимость: bash 3.2 (штатный /bin/bash в macOS) — без ассоциативных
# массивов, mapfile и прочего из bash ≥ 4.
# =============================================================================
set -euo pipefail

RUN_TESTS=1
OFFLINE=0
for arg in "$@"; do
  case "${arg}" in
    --no-tests) RUN_TESTS=0 ;;
    --offline)  OFFLINE=1 ;;
    -h|--help)  sed -n '2,26p' "$0"; exit 0 ;;
    *) printf 'verify-install.sh: неизвестный аргумент %s\n' "${arg}" >&2; exit 2 ;;
  esac
done

# Чекаут = каталог на уровень выше scripts/ (тот же приём разрешения пути, что
# в install.sh и bootstrap-mac.sh: уважаем место, куда владелец положил клон).
if ! readlink -f / >/dev/null 2>&1; then
  echo "✗ требуется readlink -f: macOS ≥ 12.3, либо GNU coreutils c gnubin в PATH" >&2
  exit 2
fi
SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
ORCH_DIR="$(cd "$(dirname "${SCRIPT_PATH}")/.." && pwd)"
[ -d "${ORCH_DIR}/skill" ] || { echo "✗ ${ORCH_DIR} не похож на чекаут claude-orchestrate" >&2; exit 2; }

SHARED_SKILLS="${HOME}/.agents/skills"
OMP_BASE_DIR="${PI_CODING_AGENT_DIR:-${HOME}/.omp/agent}"
OMP_AGENTS_DIR="${OMP_BASE_DIR}/agents"
OMP_EXTENSION_DIR="${OMP_BASE_DIR}/extensions"
OMP_AGENTS_SOURCE="${ORCH_DIR}/.omp/agents"
OMP_EXTENSION_SOURCE="${ORCH_DIR}/.omp/extensions/pocock-control"
OMP_CONFIG_SOURCE="${ORCH_DIR}/.omp/config.yml"


FAILS=0
WARNS=0
ok()   { printf '  \033[32m✓\033[0m %s\n' "$1"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$1"; WARNS=$((WARNS + 1)); }
bad()  { printf '  \033[31m✗\033[0m %s\n' "$1" >&2; FAILS=$((FAILS + 1)); }

# Ограничитель времени для внешних вызовов: на macOS GNU `timeout` отсутствует,
# поэтому оборачиваем только когда он есть (или как gtimeout из coreutils).
TIMEOUT_BIN=""
command -v timeout  >/dev/null 2>&1 && TIMEOUT_BIN="timeout"
[ -z "${TIMEOUT_BIN}" ] && command -v gtimeout >/dev/null 2>&1 && TIMEOUT_BIN="gtimeout"
guard() { # guard СЕКУНДЫ КОМАНДА...
  local secs="$1"; shift
  if [ -n "${TIMEOUT_BIN}" ]; then "${TIMEOUT_BIN}" "${secs}" "$@"; else "$@"; fi
}

# frontmatter_name ФАЙЛ — значение поля `name:` из YAML-шапки (первые 12 строк).
# Харнесс сопоставляет скилл и агента именно по этому полю, а не по имени файла;
# расхождение делает `/имя` ненаходимым при внешне корректной раскладке.
frontmatter_name() {
  awk 'NR<=12 && /^name:[[:space:]]*/ { sub(/^name:[[:space:]]*/, ""); print; exit }' "$1"
}

# verify_omp_file SOURCE DEST LABEL
# An OMP agent is a single file. It may be the default copied artifact or an
# explicit development link; either must match this checkout.
verify_omp_file() {
  local src="$1" dest="$2" label="$3"
  if [ ! -e "${dest}" ]; then
    if [ -L "${dest}" ]; then
      bad "${label}: висячая ссылка — запустите install.sh"
    else
      bad "${label}: не установлен — запустите install.sh"
    fi
  elif [ -L "${dest}" ]; then
    if [ "$(readlink -f "${dest}")" = "$(readlink -f "${src}")" ]; then
      ok "${label}: симлинк на этот чекаут"
    else
      bad "${label}: ссылка ведёт не в этот чекаут"
    fi
  elif cmp -s "${src}" "${dest}"; then
    ok "${label}: копия совпадает с чекаутом"
  else
    bad "${label}: копия отстала от чекаута — перезапустите install.sh"
  fi
}

# verify_omp_tree SOURCE DEST LABEL
# Extensions and skill heads are directories; exclude the same mutable runtime
# state as the legacy skill verification above.
verify_omp_tree() {
  local src="$1" dest="$2" label="$3" diff_out
  if [ ! -e "${dest}" ]; then
    if [ -L "${dest}" ]; then
      bad "${label}: висячая ссылка — запустите install.sh"
    else
      bad "${label}: не установлен — запустите install.sh"
    fi
  elif [ -L "${dest}" ]; then
    if [ "$(readlink -f "${dest}")" = "$(readlink -f "${src}")" ]; then
      ok "${label}: симлинк на этот чекаут"
    else
      bad "${label}: ссылка ведёт не в этот чекаут"
    fi
  else
    diff_out="$(diff -rq -x __pycache__ -x .pytest_cache -x telemetry "${src}" "${dest}" 2>&1 || true)"
    if [ -z "${diff_out}" ]; then
      ok "${label}: копия совпадает с чекаутом"
    else
      bad "${label}: копия отстала от чекаута — перезапустите install.sh"
    fi
  fi
}

# yaml_section_value SECTION KEY EXPECTED FILE
# The repository config has a deliberately small, fixed shape; checking these
# scalar settings needs no global PyYAML dependency and stays offline-safe.
yaml_section_value() {
  local section="$1" key="$2" expected="$3" file="$4"
  awk -v section="${section}" -v key="${key}" -v expected="${expected}" '
    $0 == section ":" { active=1; next }
    active && /^[^[:space:]#]/ { exit }
    active && $0 ~ "^[[:space:]]+" key ":[[:space:]]*" expected "([[:space:]]*(#.*)?)?$" {
      found=1; exit
    }
    END { exit !found }
  ' "${file}"
}

# yaml_task_isolation_value KEY EXPECTED FILE
yaml_task_isolation_value() {
  local key="$1" expected="$2" file="$3"
  awk -v key="${key}" -v expected="${expected}" '
    $0 == "task:" { in_task=1; next }
    in_task && /^[^[:space:]#]/ { exit }
    in_task && /^  isolation:[[:space:]]*$/ { in_isolation=1; next }
    in_isolation && /^  [^[:space:]#]/ { exit }
    in_isolation && $0 ~ "^    " key ":[[:space:]]*" expected "([[:space:]]*(#.*)?)?$" {
      found=1; exit
    }
    END { exit !found }
  ' "${file}"
}

yaml_task_isolation_mode_isolated() {
  local file="$1" mode
  for mode in auto apfs btrfs zfs linux-reflink overlayfs windows-blockclone projfs rcopy worktree fuse-overlay fuse-projfs; do
    if yaml_task_isolation_value mode "${mode}" "${file}"; then
      return 0
    fi
  done
  return 1
}

verify_omp_config() {
  local file="$1"
  if [ ! -f "${file}" ]; then
    bad "нет обязательного репозиторного .omp/config.yml"
    return
  fi
  ok "репозиторный .omp/config.yml найден"

  if yaml_section_value async enabled false "${file}"; then ok "async.enabled: false"; else bad "async.enabled должен быть false"; fi
  if yaml_section_value display showTokenUsage true "${file}"; then ok "display.showTokenUsage: true"; else bad "display.showTokenUsage должен быть true"; fi
  if yaml_section_value task batch true "${file}"; then ok "task.batch: true"; else bad "task.batch должен быть true"; fi
  if yaml_section_value task enableEffort true "${file}"; then ok "task.enableEffort: true"; else bad "task.enableEffort должен быть true"; fi
  if yaml_section_value task showResolvedModelBadge true "${file}"; then ok "task.showResolvedModelBadge: true"; else bad "task.showResolvedModelBadge должен быть true"; fi
  if yaml_task_isolation_mode_isolated "${file}"; then ok "task.isolation.mode включает штатный backend"; else bad "task.isolation.mode должен включать известный backend OMP и не может быть none"; fi
  if yaml_task_isolation_value apply false "${file}"; then ok "task.isolation.apply: false"; else bad "task.isolation.apply должен быть false"; fi
  if yaml_task_isolation_value merge patch "${file}"; then ok "task.isolation.merge: patch"; else bad "task.isolation.merge должен быть patch"; fi
  if yaml_section_value task maxRecursionDepth 1 "${file}"; then ok "task.maxRecursionDepth: 1"; else bad "task.maxRecursionDepth должен быть 1"; fi
  if yaml_section_value task maxConcurrency 6 "${file}"; then ok "task.maxConcurrency: 6"; else bad "task.maxConcurrency должен быть 6"; fi
  if yaml_section_value retry modelFallback true "${file}"; then ok "retry.modelFallback: true"; else bad "retry.modelFallback должен быть true"; fi
}

verify_effective_omp_config() {
  local runtime="${ORCH_DIR}/skill/orchestrate/tools/omp_runtime.py"
  local report
  if ! command -v omp >/dev/null 2>&1; then
    bad "OMP CLI не найден"
    return
  fi
  if [ ! -x "${runtime}" ]; then
    bad "исполняемый OMP runtime не найден: ${runtime}"
    return
  fi
  if report="$(cd /tmp && "${runtime}" metadata --request '{}' 2>&1)"; then
    ok "глобальные OMP task-инварианты действуют вне этого чекаута"
  else
    bad "глобальные OMP task-инварианты несовместимы: ${report}"
  fi
}

# --- A. Чекаут ---------------------------------------------------------------
echo "== Чекаут =="
if [ -d "${ORCH_DIR}/.git" ]; then
  head_sha="$(git -C "${ORCH_DIR}" rev-parse --short HEAD)"
  head_sub="$(git -C "${ORCH_DIR}" log -1 --pretty=%s)"
  ok "${ORCH_DIR} @ ${head_sha} — ${head_sub}"

  # Ветка сравнения: upstream текущей ветки, иначе origin/main.
  upstream="$(git -C "${ORCH_DIR}" rev-parse --abbrev-ref --symbolic-full-name '@{upstream}' 2>/dev/null || echo "origin/main")"
  if [ "${OFFLINE}" -eq 1 ]; then
    warn "проверка свежести пропущена (--offline)"
  elif guard 60 git -C "${ORCH_DIR}" fetch --quiet 2>/dev/null; then
    if git -C "${ORCH_DIR}" rev-parse --verify --quiet "${upstream}" >/dev/null; then
      behind="$(git -C "${ORCH_DIR}" rev-list --count "HEAD..${upstream}")"
      ahead="$(git -C "${ORCH_DIR}" rev-list --count "${upstream}..HEAD")"
      if [ "${behind}" -gt 0 ]; then
        bad "чекаут отстаёт от ${upstream} на ${behind} коммит(ов) — установка не свежая: запустите bootstrap-mac.sh"
      else
        ok "свежесть: совпадает с ${upstream}"
      fi
      [ "${ahead}" -gt 0 ] && warn "локально впереди ${upstream} на ${ahead} коммит(ов) — не запушено"
    else
      warn "${upstream} не найден — сравнить не с чем"
    fi
  else
    warn "git fetch не удался (нет сети?) — свежесть не проверена"
  fi

  if [ -n "$(git -C "${ORCH_DIR}" status --porcelain)" ]; then
    warn "рабочее дерево грязное — установленное отражает правки, которых нет в origin"
  fi
else
  warn "${ORCH_DIR} не git-репозиторий — свежесть проверить нечем"
fi

# --- B. Публичные головы OMP -------------------------------------------------
# Для каждой головы из чекаута проверяем установленный shared skill. Копия,
# отставшая от чекаута, считается отказом так же, как неверный симлинк.
echo "== Публичные головы OMP (${SHARED_SKILLS}) =="
heads_seen=0
for src in "${ORCH_DIR}"/skill/*/; do
  [ -f "${src}SKILL.md" ] || continue
  heads_seen=$((heads_seen + 1))
  name="$(basename "${src%/}")"
  dest="${SHARED_SKILLS}/${name}"

  if [ ! -e "${dest}" ]; then
    if [ -L "${dest}" ]; then bad "${name}: висячая ссылка (цель не существует) — запустите install.sh"
    else bad "${name}: не установлен — запустите install.sh"; fi
    continue
  fi
  if [ ! -f "${dest}/SKILL.md" ]; then
    bad "${name}: установлен, но SKILL.md не читается"
    continue
  fi

  if [ -L "${dest}" ]; then
    if [ "$(readlink -f "${dest}")" = "$(readlink -f "${src%/}")" ]; then
      ok "${name}: симлинк на этот чекаут (правки живут сразу)"
    else
      bad "${name}: ссылка ведёт в $(readlink -f "${dest}"), а не в этот чекаут — на машине две копии, они разъедутся"
    fi
  else
    # Режим копирования: telemetry — изменяемое состояние, __pycache__ —
    # производный мусор; и то и другое к составу скилла не относится.
    # Ключ -x (а не длинный --exclude) — он есть и в GNU, и в BSD diff (macOS).
    diff_out="$(diff -rq -x __pycache__ -x .pytest_cache -x telemetry "${src%/}" "${dest}" 2>&1 || true)"
    if [ -z "${diff_out}" ]; then
      ok "${name}: копия совпадает с чекаутом"
    else
      bad "${name}: копия отстала от чекаута — перезапустите install.sh (расхождений: $(printf '%s\n' "${diff_out}" | wc -l | tr -d ' '))"
      printf '%s\n' "${diff_out}" | head -3 | sed 's/^/      /'
    fi
  fi

  fm="$(frontmatter_name "${dest}/SKILL.md" || true)"
  [ "${fm}" = "${name}" ] || warn "${name}: в шапке SKILL.md name=\"${fm}\" — харнесс найдёт скилл под этим именем, не по каталогу"
done
[ "${heads_seen}" -gt 0 ] || bad "в чекауте нет ни одной головы (skill/*/SKILL.md) — неполный клон?"


# --- C. Висячие ссылки в активных OMP-реестрах -------------------------------
echo "== Висячие ссылки в активных OMP-реестрах =="
dangling=0
for reg in "${SHARED_SKILLS}" "${OMP_AGENTS_DIR}" "${OMP_EXTENSION_DIR}"; do
  [ -d "${reg}" ] || continue
  for entry in "${reg}"/*; do
    [ -L "${entry}" ] || continue
    [ -e "${entry}" ] && continue
    case "${entry}" in
      *.bak) warn "висячий след прошлой установки: ${entry} (безвреден, можно удалить руками)" ;;
      *) bad "висячая ссылка: ${entry} -> $(readlink "${entry}")"; dangling=$((dangling + 1)) ;;
    esac
  done
done
[ "${dangling}" -eq 0 ] && ok "висячих ссылок нет"

# --- D. Legacy non-OMP contour ------------------------------------------------
# A successful cutover leaves no callable project head in Claude Code. Generic
# agent names are checked only when they still point at this checkout, so an
# unrelated user-created agent is never mistaken for Pocock residue.
echo "== Legacy non-OMP contour =="
legacy_found=0
for entry in \
  "${HOME}/.claude/skills/orchestrate" \
  "${HOME}/.claude/skills/orchestrate-frontier" \
  "${HOME}/.claude/skills/orchestrate-sweep" \
  "${HOME}/.claude/skills/pocock-run" \
  "${HOME}/.claude/skills/pocock-frontier" \
  "${HOME}/.claude/skills/pocock-sweep"; do
  if [ -e "${entry}" ] || [ -L "${entry}" ]; then
    bad "legacy Claude skill всё ещё активен: ${entry} — запустите install.sh"
    legacy_found=$((legacy_found + 1))
  fi
done
for entry in \
  "${HOME}/.claude/agents/architect.md" \
  "${HOME}/.claude/agents/builder.md" \
  "${HOME}/.claude/agents/critic.md" \
  "${HOME}/.claude/agents/scout.md" \
  "${HOME}/.agents/agents/architect.md" \
  "${HOME}/.agents/agents/builder.md" \
  "${HOME}/.agents/agents/critic.md" \
  "${HOME}/.agents/agents/scout.md"; do
  [ -L "${entry}" ] || continue
  case "$(readlink "${entry}")" in
    "${ORCH_DIR}"/agents/*)
      bad "legacy Pocock agent всё ещё активен: ${entry}"
      legacy_found=$((legacy_found + 1))
      ;;
  esac
done
[ "${legacy_found}" -eq 0 ] && ok "активных ссылок старого Claude-контура нет"


# --- D.1 Нативный контур OMP -------------------------------------------------
# OMP uses the shared skill registry for the three public heads, but loads native
# task agents and global extensions from separate registries.  These checks are
# deliberately local: --offline must never require a network connection.
echo "== Нативный контур OMP =="
verify_omp_config "${OMP_CONFIG_SOURCE}"
verify_effective_omp_config
cat <<'EOF'
  Дефолт и инварианты изоляции задач в .omp/config.yml:
    async.enabled: false
    display.showTokenUsage: true
    task.batch: true; task.enableEffort: true; task.showResolvedModelBadge: true
    task.isolation.mode: auto (дефолт); явный изолирующий backend тоже допустим
    task.isolation.apply: false; task.isolation.merge: patch
    task.maxRecursionDepth: 1; task.maxConcurrency: 6
    retry.modelFallback: true
EOF

for name in orchestrate orchestrate-frontier orchestrate-sweep; do
  src="${ORCH_DIR}/skill/${name}"
  if [ -f "${src}/SKILL.md" ]; then
    verify_omp_tree "${src}" "${SHARED_SKILLS}/${name}" "OMP skill ${name}"
  else
    bad "исходник OMP skill ${name} отсутствует в чекауте"
  fi
done

for name in pocock-run pocock-frontier pocock-sweep; do
  if [ -e "${SHARED_SKILLS}/${name}" ] || [ -L "${SHARED_SKILLS}/${name}" ]; then
    bad "выведенная OMP-голова ${name} всё ещё активна в ${SHARED_SKILLS}"
  fi
done

omp_agents_seen=0
if [ -d "${OMP_AGENTS_SOURCE}" ]; then
  for src in "${OMP_AGENTS_SOURCE}"/*.md; do
    [ -f "${src}" ] || continue
    omp_agents_seen=$((omp_agents_seen + 1))
    verify_omp_file "${src}" "${OMP_AGENTS_DIR}/$(basename "${src}")" "OMP agent $(basename "${src}")"
  done
  [ "${omp_agents_seen}" -gt 0 ] || bad "в ${OMP_AGENTS_SOURCE} нет ожидаемых OMP agents (*.md)"
else
  bad "исходный каталог OMP agents отсутствует: ${OMP_AGENTS_SOURCE}"
fi

if [ -d "${OMP_EXTENSION_SOURCE}" ]; then
  verify_omp_tree "${OMP_EXTENSION_SOURCE}" "${OMP_EXTENSION_DIR}/pocock-control" "OMP extension pocock-control"
else
  bad "исходник OMP extension отсутствует: ${OMP_EXTENSION_SOURCE}"
fi

# Семь канонических скиллов подготовительной фазы должны быть в общем
# OMP-совместимом реестре ~/.agents/skills.
echo "== Хребет Покока =="
spine_missing=0
for name in grilling domain-modeling to-spec to-tickets implement tdd code-review; do
  [ -f "${SHARED_SKILLS}/${name}/SKILL.md" ] || { bad "хребет OMP: нет ${name}/SKILL.md"; spine_missing=1; }
done
[ "${spine_missing}" -eq 0 ] && ok "семь канонических файлов хребта на месте"

# --- F. Инструменты установленного OMP-скилла --------------------------------
# Проверяем именно установленный экземпляр и его же валидатор.
echo "== Инструменты OMP =="
INSTALLED="${SHARED_SKILLS}/orchestrate"
if [ ! -f "${INSTALLED}/SKILL.md" ]; then
  bad "инструменты не проверены: OMP-голова orchestrate не установлена"
else
  if command -v python3 >/dev/null; then
    ok "python3: $(python3 --version 2>&1)"
    if python3 -c 'import yaml' 2>/dev/null; then
      ok "PyYAML"
    else
      bad "PyYAML не установлен — OMP runtime, валидатор и телеметрия не стартуют: pip3 install pyyaml"
    fi

    # Валидатор: конфиг против схемы + проза против конфига (детерминированный шов).
    if [ -f "${INSTALLED}/tools/validate_config.py" ]; then
      if vc_out="$(guard 120 python3 "${INSTALLED}/tools/validate_config.py" 2>&1)"; then
        ok "config.yaml и проза согласованы (validate_config)"
      else
        bad "validate_config провален:"
        printf '%s\n' "${vc_out}" | head -5 | sed 's/^/      /'
      fi
    else
      bad "нет ${INSTALLED}/tools/validate_config.py"
    fi

    if core_out="$(cd /tmp && guard 60 "${INSTALLED}/tools/omp_runtime.py" metadata --request '{}' 2>&1)"; then
      ok "OMP runtime запускается и принимает эффективный профиль"
    else
      bad "OMP runtime не прошёл metadata preflight: ${core_out}"
    fi
  else
    bad "python3 не найден — без него не работают OMP runtime, валидатор и телеметрия"
  fi

  # Тесты инструментов гоняем в чекауте (это dev-артефакт, в установку он
  # попадает лишь как соседние файлы). Красный прогон — отказ: значит подтянутая
  # ревизия сломана, и установка её закрепила.
  if [ "${RUN_TESTS}" -eq 0 ]; then
    warn "тесты пропущены (--no-tests)"
  elif ! python3 -c 'import pytest' 2>/dev/null; then
    warn "pytest не установлен — тесты инструментов пропущены (pip3 install pytest)"
  else
    if t_out="$(cd "${ORCH_DIR}/skill/orchestrate/tools" && guard 600 python3 -m pytest -q 2>&1)"; then
      ok "тесты инструментов: $(printf '%s\n' "${t_out}" | tail -1)"
    else
      bad "тесты инструментов красные:"
      printf '%s\n' "${t_out}" | tail -5 | sed 's/^/      /'
    fi
  fi
fi

# --- G. Нативный OMP runtime -------------------------------------------------
echo "== OMP runtime =="
if command -v omp >/dev/null 2>&1; then
  ok "omp: $(command -v omp)"
else
  bad "OMP CLI не найден — нативный контур не работает"
fi

if command -v bun >/dev/null 2>&1; then
  if adapter_out="$(cd "${ORCH_DIR}" && guard 60 bun test ./.omp/extensions/pocock-control/index.test.ts 2>&1)"; then
    ok "OMP adapter regression: $(printf '%s\n' "${adapter_out}" | tail -1)"
  else
    bad "OMP adapter regression красный:"
    printf '%s\n' "${adapter_out}" | tail -5 | sed 's/^/      /'
  fi
else
  bad "bun не найден — TypeScript-адаптер OMP нельзя проверить"
fi

# --- Итог --------------------------------------------------------------------
echo
if [ "${FAILS}" -eq 0 ]; then
  printf '\033[32m✓ Установка исправна\033[0m — провалов нет, предупреждений: %s\n' "${WARNS}"
  echo "  Напоминание: OMP перечитывает реестры скиллов и extensions при старте сессии."
  exit 0
else
  printf '\033[31m✗ Провалено проверок: %s\033[0m (предупреждений: %s)\n' "${FAILS}" "${WARNS}" >&2
  echo "  Обычное лечение: bash ${ORCH_DIR}/bootstrap-mac.sh — он подтянет свежее и переустановит." >&2
  exit 1
fi
