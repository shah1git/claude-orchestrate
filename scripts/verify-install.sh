#!/usr/bin/env bash
# =============================================================================
# verify-install.sh — самопроверка установленного контура /orchestrate.
#
# Отвечает ровно на один вопрос: «то, что сейчас лежит в реестрах харнесса, —
# рабочая установка ЭТОГО чекаута?» Проверяется именно установленное
# (~/.claude/skills, ~/.claude/agents, зеркало ~/.agents), а не исходный
# репозиторий: установка ломается не там, где лежит код, а на стыке — висячая
# ссылка, устаревшая копия, отставший чекаут, отсутствующий PyYAML.
#
# Состав контура нигде не перечислен списком, а ВЫВОДИТСЯ из чекаута: голова —
# любой каталог skill/<name>/ с SKILL.md, агент — любой файл agents/<name>.md.
# Список разъезжается с репозиторием (именно так висячая ссылка на выведенную
# ADR-0008 третью голову `orca_orchestrate` пережила её удаление); выводимый
# состав такой ошибки не допускает по построению.
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

CLAUDE_SKILLS="${HOME}/.claude/skills"
CLAUDE_AGENTS="${HOME}/.claude/agents"
SHARED_SKILLS="${HOME}/.agents/skills"
SHARED_AGENTS="${HOME}/.agents/agents"
CODEX_SKILLS="${HOME}/.codex/skills"

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

# --- B. Головы оркестрации ---------------------------------------------------
# Для каждой головы из чекаута: установлена ли, читается ли SKILL.md сквозь
# ссылку, и совпадает ли установленное с чекаутом (симлинк — целью, копия —
# содержимым). Копия, отставшая от чекаута, — самый тихий из отказов: файлы на
# месте, поведение старое.
echo "== Головы оркестрации (${CLAUDE_SKILLS}) =="
heads_seen=0
for src in "${ORCH_DIR}"/skill/*/; do
  [ -f "${src}SKILL.md" ] || continue
  heads_seen=$((heads_seen + 1))
  name="$(basename "${src%/}")"
  dest="${CLAUDE_SKILLS}/${name}"

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
    # Режим --copy: сверяем содержимое. telemetry — симлинк в приватное репо,
    # __pycache__ — производный мусор; и то и другое к составу скилла не относится.
    # Ключ -x (а не длинный --exclude) — он есть и в GNU, и в BSD diff (macOS).
    diff_out="$(diff -rq -x __pycache__ -x telemetry "${src%/}" "${dest}" 2>&1 || true)"
    if [ -z "${diff_out}" ]; then
      ok "${name}: копия совпадает с чекаутом"
    else
      bad "${name}: копия отстала от чекаута — перезапустите install.sh --copy (расхождений: $(printf '%s\n' "${diff_out}" | wc -l | tr -d ' '))"
      printf '%s\n' "${diff_out}" | head -3 | sed 's/^/      /'
    fi
  fi

  fm="$(frontmatter_name "${dest}/SKILL.md" || true)"
  [ "${fm}" = "${name}" ] || warn "${name}: в шапке SKILL.md name=\"${fm}\" — харнесс найдёт скилл под этим именем, не по каталогу"
done
[ "${heads_seen}" -gt 0 ] || bad "в чекауте нет ни одной головы (skill/*/SKILL.md) — неполный клон?"

# --- C. Агенты ---------------------------------------------------------------
echo "== Агенты (${CLAUDE_AGENTS}) =="
for src in "${ORCH_DIR}"/agents/*.md; do
  [ -f "${src}" ] || continue
  file="$(basename "${src}")"
  name="${file%.md}"
  dest="${CLAUDE_AGENTS}/${file}"

  if [ ! -e "${dest}" ]; then
    bad "${name}: не установлен — запустите install.sh"
    continue
  fi
  fm="$(frontmatter_name "${dest}" || true)"
  if [ "${fm}" != "${name}" ]; then
    bad "${name}: в шапке name=\"${fm}\" — тип агента не совпадёт с именем файла"
    continue
  fi
  # Тир модели: агенты контура пинуют модель явно; без пина воркер уедет на
  # модель сессии, и маршрутизация по тирам перестанет быть маршрутизацией.
  if awk 'NR<=12 && /^model:[[:space:]]*[^[:space:]]/ { found=1 } END { exit !found }' "${dest}"; then
    ok "${name}: установлен, модель пинована ($(awk 'NR<=12 && /^model:/ { sub(/^model:[[:space:]]*/, ""); print; exit }' "${dest}"))"
  else
    warn "${name}: установлен, но без поля model — воркер унаследует модель сессии"
  fi
done

# --- C.2 Висячие ссылки в реестрах -------------------------------------------
# Обобщение того самого дефекта: ссылка есть, цели нет. Харнесс показывает такой
# скилл в списке и падает при вызове. Проверяем все реестры разом.
echo "== Висячие ссылки в реестрах =="
dangling=0
for reg in "${CLAUDE_SKILLS}" "${CLAUDE_AGENTS}" "${SHARED_SKILLS}" "${SHARED_AGENTS}" "${CODEX_SKILLS}"; do
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

# --- D. Зеркало ~/.agents ----------------------------------------------------
# Харнессы, порождённые не-Claude CLI, читают скиллы отсюда, а не из ~/.claude.
echo "== Зеркало ~/.agents =="
if [ -d "${SHARED_SKILLS}" ]; then
  mirror_bad=0
  for src in "${ORCH_DIR}"/skill/*/; do
    [ -f "${src}SKILL.md" ] || continue
    name="$(basename "${src%/}")"
    [ -f "${SHARED_SKILLS}/${name}/SKILL.md" ] || { bad "зеркало: ${name} не установлен в ${SHARED_SKILLS}"; mirror_bad=1; }
  done
  for src in "${ORCH_DIR}"/agents/*.md; do
    [ -f "${src}" ] || continue
    [ -f "${SHARED_AGENTS}/$(basename "${src}")" ] || { bad "зеркало: $(basename "${src}") не установлен в ${SHARED_AGENTS}"; mirror_bad=1; }
  done
  [ "${mirror_bad}" -eq 0 ] && ok "головы и агенты продублированы в ~/.agents"
else
  warn "${SHARED_SKILLS} нет — зеркало не нужно (нет сторонних харнессов на этой машине)"
fi

# --- E. Хребет Покока --------------------------------------------------------
# Семь канонических скиллов подготовительной фазы: /orchestrate ссылается на них
# из прозы, и без них фронтир готовить нечем.
echo "== Хребет Покока =="
spine_missing=0
for name in grilling domain-modeling to-spec to-tickets implement tdd code-review; do
  [ -f "${CLAUDE_SKILLS}/${name}/SKILL.md" ] || { bad "хребет: нет ${name}/SKILL.md"; spine_missing=1; }
done
[ "${spine_missing}" -eq 0 ] && ok "семь канонических файлов хребта на месте"

# --- F. Инструменты установленного скилла ------------------------------------
# Проверяем инструменты ИМЕННО установленного экземпляра и его же валидатором:
# так проверка касается артефакта, который будет исполняться, а не исходника.
echo "== Инструменты =="
INSTALLED="${CLAUDE_SKILLS}/orchestrate"
if [ ! -f "${INSTALLED}/SKILL.md" ]; then
  bad "инструменты не проверены: голова orchestrate не установлена"
else
  if command -v python3 >/dev/null; then
    ok "python3: $(python3 --version 2>&1)"
    if python3 -c 'import yaml' 2>/dev/null; then
      ok "PyYAML"
    else
      bad "PyYAML не установлен — run-lane, валидатор конфига и телеметрия не стартуют: pip3 install pyyaml"
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

    # Движок раздачи: пакет должен импортироваться установленным путём — битый
    # или неполный перенос виден именно здесь, а не при первом наряде.
    if guard 60 python3 -c "import sys; sys.path.insert(0, '${INSTALLED}/tools'); import run_lane.__main__" 2>/dev/null; then
      ok "run_lane импортируется"
    else
      bad "run_lane не импортируется из ${INSTALLED}/tools — раздача нарядов работать не будет"
    fi
  else
    bad "python3 не найден — без него не работают ни run-lane, ни валидатор, ни телеметрия"
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

# --- G. Лейны ----------------------------------------------------------------
# Инвентарь исполнителей. Отсутствие внешнего CLI — не отказ установки: конфиг
# штатно деградирует в Claude-дефолты. Отказ ровно один — нет самого claude.
echo "== Лейны =="
if command -v claude >/dev/null; then
  ok "claude: $(command -v claude)"
else
  bad "claude CLI не найден — контур не работает ни в каком виде"
fi
if [ -x "${INSTALLED}/tools/run-lane" ] && python3 -c 'import yaml' 2>/dev/null; then
  det_out="$(guard 180 "${INSTALLED}/tools/run-lane" detect --config "${INSTALLED}/config.yaml" 2>/dev/null || true)"
  if [ -n "${det_out}" ]; then
    # Разбор JSON — питоном: свой парсер в bash был бы ровно тем «умным трюком»,
    # который потом врёт на первом же изменении формата.
    printf '%s' "${det_out}" | python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)
# 2026-07-30 критик-гейт находка 5: "no adapter registered" и "empty
# probe_command" — ПОСТОЯННЫЕ ошибки конфигурации (правка конфига их не
# переживёт, повторный запуск ничего не изменит); всё прочее, что попадает в
# login_probe == "failed" (таймаут, самообновление бинаря, нераспознанный
# вывод), — ВРЕМЕННЫЙ сбой самой пробы. Смешивать их в один совет "повторите"
# вводит в заблуждение для первой категории.
PERMANENT_LOGIN_PROBE_REASONS = {"no adapter registered", "empty probe_command"}
for key in sorted(data):
    t = data[key]
    lanes = ", ".join(t.get("lanes") or []) or "—"
    # 2026-07-30: упавшая проба (login_probe == "failed", CLI мог обновляться
    # ровно в момент проверки) — это НЕ то же самое, что достоверный отрицательный
    # ответ (login_probe == "ok", logged_in false). Формулировка "CLI есть, но не
    # залогинен" подразумевает второе и не должна звучать при первом — иначе
    # рабочий лейн, чья проба просто не успела ответить, выглядит мёртвым.
    if t.get("present") and t.get("logged_in"):
        print("  \033[32m✓\033[0m %s (%s): лейны %s" % (key, t.get("cli"), lanes))
    elif t.get("login_probe") == "failed" and t.get("login_probe_reason") in PERMANENT_LOGIN_PROBE_REASONS:
        reason = t.get("login_probe_reason") or "причина не названа"
        print("  \033[33m!\033[0m %s (%s): логин не проверить — %s (ошибка конфигурации, не временный сбой) — лейны %s недоступны" % (key, t.get("cli"), reason, lanes))
    elif t.get("login_probe") == "failed":
        reason = t.get("login_probe_reason") or "причина не названа"
        print("  \033[33m!\033[0m %s (%s): проверить логин не удалось (%s) — возможно, CLI обновлялся; повторите" % (key, t.get("cli"), reason))
    elif t.get("present"):
        print("  \033[33m!\033[0m %s (%s): CLI есть, но не залогинен — лейны %s уйдут в Claude-фолбэк" % (key, t.get("cli"), lanes))
    else:
        print("  \033[33m!\033[0m %s (%s): CLI нет — лейны %s недоступны" % (key, t.get("cli"), lanes))
'
    # Предупреждения посчитаны выше только визуально; для итога поднимаем счётчик
    # по числу недоступных транспортов — без влияния на код возврата.
    unavailable="$(printf '%s' "${det_out}" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    print(0); sys.exit(0)
print(sum(1 for v in d.values() if not (v.get("present") and v.get("logged_in"))))
')"
    WARNS=$((WARNS + unavailable))
  else
    warn "run-lane detect не дал ответа — инвентарь лейнов не построен"
  fi
else
  warn "инвентарь лейнов пропущен (нет run-lane или PyYAML)"
fi

# --- Итог --------------------------------------------------------------------
echo
if [ "${FAILS}" -eq 0 ]; then
  printf '\033[32m✓ Установка исправна\033[0m — провалов нет, предупреждений: %s\n' "${WARNS}"
  echo "  Напоминание: Claude Code перечитывает реестр скиллов только при старте сессии."
  exit 0
else
  printf '\033[31m✗ Провалено проверок: %s\033[0m (предупреждений: %s)\n' "${FAILS}" "${WARNS}" >&2
  echo "  Обычное лечение: bash ${ORCH_DIR}/bootstrap-mac.sh — он подтянет свежее и переустановит." >&2
  exit 1
fi
