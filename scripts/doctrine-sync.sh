#!/usr/bin/env bash
# managed-by: rikanv-doctrine
#
# doctrine-sync.sh — доверенная подготовка checkout доктрины.
#
# Канонический низкоуровневый модуль. Обычный вызов совместим с прежней
# командой, но передаётся в `doctrine-session.sh start`: sync не подтверждает
# прочтение и никогда не записывает `.doctrine-version`. После статуса
# `PREPARED`/`READING_REQUIRED` прочитай объявленные документы и выполни
# `doctrine-session.sh acknowledge <revisions.prepared>`.
#
# `--check` сохранён для CI: он получает доверенную вершину, но не двигает
# checkout и не меняет marker. Внутренний вызов session использует этот модуль
# для единственного доверенного fast-forward.
#
# Карта кодов `--check` (на неё завязан legacy CI):
#   0 — актуально; 2 — ошибка использования; 10 — hard-дельта;
#   11 — отсутствует marker; 12 — checkout изменился во время ff-only;
#   13 — недоверенный checkout; 14 — только soft/derived-дельта;
#   15 — marker невалиден или вне доверенной истории; 124 — timeout Git.
#
# Переменные окружения:
#   DOCTRINE_REPO_URL, DOCTRINE_BRANCH, DOCTRINE_SEEN_FILE,
#   DOCTRINE_NETWORK_TIMEOUT_SECONDS — см. `doctrine-session.sh`.
#
# Использование:
#   bash doctrine-sync.sh [DOCTRINE_DIR] [--check] [--status-json]
#
set -euo pipefail
export GIT_NO_REPLACE_OBJECTS=1
script_dir="$(cd "$(dirname "$0")" && pwd -P)"
[ -f "$script_dir/lib/doctrine-repo-url.sh" ] || { printf '%s\n' 'doctrine-sync.sh: missing managed scripts/lib/doctrine-repo-url.sh' >&2; exit 2; }
# shellcheck source=lib/doctrine-repo-url.sh
source "$script_dir/lib/doctrine-repo-url.sh"

STATUS_JSON=0
SYNC_ARGS=()
for arg in "$@"; do
  case "$arg" in
    --status-json) STATUS_JSON=1 ;;
    *) SYNC_ARGS+=("$arg") ;;
  esac
done

if [ "$STATUS_JSON" -eq 1 ]; then
  command -v jq >/dev/null 2>&1 || { printf '%s\n' 'doctrine-sync.sh --status-json требует jq' >&2; exit 2; }
  script_path="$(cd "$(dirname "$0")" && pwd -P)/$(basename "$0")"
  status_tmp="$(mktemp)"
  detail_tmp="$(mktemp)"
  trap 'rm -f "$status_tmp" "$detail_tmp"' EXIT
  set +e
  DOCTRINE_STATUS_FD=3 bash "$script_path" "${SYNC_ARGS[@]}" --check 3>"$detail_tmp" >"$status_tmp" 2>&1
  status_rc=$?
  set -e
  cat "$status_tmp" >&2

  status_dir="doctrine"
  for arg in "${SYNC_ARGS[@]}"; do
    case "$arg" in -*) ;; *) status_dir="$arg" ;; esac
  done
  status_seen_file="${DOCTRINE_SEEN_FILE:-.doctrine-version}"
  seen_revision=""
  local_revision=""
  trusted_revision=""
  seen_version=""
  trusted_version=""
  network_effect=false
  git_metadata_effect=false
  if [ -f "$status_seen_file" ] && [ ! -L "$status_seen_file" ]; then
    seen_revision="$(tr -d '[:space:]' <"$status_seen_file")"
  fi
  if [ "$status_rc" -ne 13 ] && git -C "$status_dir" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    local_revision="$(git -C "$status_dir" rev-parse HEAD 2>/dev/null || true)"
    trusted_revision="$(git -C "$status_dir" rev-parse refs/rikanv-doctrine/trusted-tip 2>/dev/null || true)"
    [ -n "$trusted_revision" ] || trusted_revision="$(git -C "$status_dir" rev-parse "origin/${DOCTRINE_BRANCH:-main}" 2>/dev/null || true)"
    if [ -n "$seen_revision" ]; then
      seen_version="$(git -C "$status_dir" show "$seen_revision:VERSION" 2>/dev/null | tr -d '[:space:]' || true)"
    fi
    if [ -n "$trusted_revision" ]; then
      trusted_version="$(git -C "$status_dir" show "$trusted_revision:VERSION" 2>/dev/null | tr -d '[:space:]' || true)"
    fi
  fi
  if [ -s "$detail_tmp" ] && jq -e '.effects.network | type == "boolean"' "$detail_tmp" >/dev/null 2>&1; then
    network_effect="$(jq -r '.effects.network' "$detail_tmp")"
    git_metadata_effect="$(jq -r '.effects.git_metadata' "$detail_tmp")"
    trusted_revision="$(jq -r '.trusted_revision // empty' "$detail_tmp")"
    trusted_version="$(jq -r '.trusted_version // empty' "$detail_tmp")"
    seen_version="$(jq -r '.seen_version // empty' "$detail_tmp")"
  fi
  case "$status_rc" in
    0) status="current"; code="CURRENT" ;;
    10) status="hard_delta"; code="HARD_DELTA" ;;
    11) status="missing_marker"; code="MISSING_MARKER" ;;
    12) status="checkout_changed"; code="CHECKOUT_CHANGED" ;;
    13) status="foreign_checkout"; code="FOREIGN_CHECKOUT" ;;
    14) status="soft_delta"; code="SOFT_DELTA" ;;
    15) status="invalid_revision"; code="INVALID_REVISION" ;;
    124) status="infra_timeout"; code="INFRA_TIMEOUT" ;;
    *) status="error"; code="UNCLASSIFIED_ERROR" ;;
  esac
  jq -n --arg status "$status" --arg code "$code" --argjson exit_code "$status_rc" \
    --arg seen "$seen_revision" --arg local "$local_revision" --arg trusted "$trusted_revision" \
    --arg seen_version "$seen_version" --arg trusted_version "$trusted_version" \
    --argjson network_effect "$network_effect" --argjson git_metadata_effect "$git_metadata_effect" \
    '{schema_version:1,module:"sync",operation:"check",status:$status,code:$code,exit_code:$exit_code,effects:{project_files:false,checkout:false,git_metadata:$git_metadata_effect,network:$network_effect},revisions:{seen:($seen|if .=="" then null else . end),local:($local|if .=="" then null else . end),trusted:($trusted|if .=="" then null else . end)},versions:{seen:($seen_version|if .=="" then null else . end),trusted:($trusted_version|if .=="" then null else . end)}}'
  exit "$status_rc"
fi

set -- "${SYNC_ARGS[@]}"

# The JSON wrapper runs this invocation with fd 3 open.  Emit effects from the
# branch that actually ran instead of guessing from its exit code: code 13, for
# example, can be discovered either before or after a remote operation.
status_network_effect=false
status_git_metadata_effect=false
status_trusted_revision=""
status_trusted_version=""
status_seen_version=""
emit_status_detail() {
  [ "${DOCTRINE_STATUS_FD:-}" = "3" ] || return 0
  jq -cn --argjson network "$status_network_effect" --argjson git_metadata "$status_git_metadata_effect" \
    --arg trusted_revision "$status_trusted_revision" --arg trusted_version "$status_trusted_version" \
    --arg seen_version "$status_seen_version" \
    '{effects:{network:$network,git_metadata:$git_metadata},trusted_revision:($trusted_revision|if .=="" then null else . end),trusted_version:($trusted_version|if .=="" then null else . end),seen_version:($seen_version|if .=="" then null else . end)}' >&3
}
trap 'status_exit_code=$?; trap - EXIT; emit_status_detail; exit "$status_exit_code"' EXIT

DOCTRINE_DIR="doctrine"
MODE="sync"
for arg in "$@"; do
  case "$arg" in
    --check) MODE="check" ;;
    -*)      printf 'Неизвестный флаг: %s\n' "$arg" >&2; exit 2 ;;
    *)       DOCTRINE_DIR="$arg" ;;
  esac
done

# Старый прямой вызов остаётся точкой входа, но больше не может подтвердить
# ревизию сам. Session владеет pending-состоянием и единственным atomic
# продвижением marker в acknowledge. Внутренний вызов ниже нужен session для
# подготовительного ff-only и не должен рекурсивно делегироваться обратно.
if [ "$MODE" = "sync" ] && [ "${DOCTRINE_SESSION_INTERNAL:-0}" != "1" ]; then
  exec bash "$script_dir/doctrine-session.sh" start "$DOCTRINE_DIR" --status-json
fi

REPO_URL="${DOCTRINE_REPO_URL:-https://github.com/shah1git/rikanv-doctrine.git}"
BRANCH="${DOCTRINE_BRANCH:-main}"
SEEN_FILE="${DOCTRINE_SEEN_FILE:-.doctrine-version}"
NETWORK_TIMEOUT_SECONDS="${DOCTRINE_NETWORK_TIMEOUT_SECONDS:-120}"
TRUSTED_REF="refs/rikanv-doctrine/trusted-tip"

case "$DOCTRINE_DIR" in
  ""|.|./|..|../|/|*/.|*/..|*/./*|*/../*)
    printf 'DOCTRINE_DIR не может быть текущим каталогом или корнем файловой системы: %s\n' "$DOCTRINE_DIR" >&2
    exit 2
    ;;
esac
if ! git check-ref-format --branch "$BRANCH" >/dev/null 2>&1; then
  printf 'DOCTRINE_BRANCH не является допустимым именем ветки Git: %s\n' "$BRANCH" >&2
  exit 2
fi
case "$REPO_URL" in
  ""|-*) printf 'DOCTRINE_REPO_URL не может быть пустым или начинаться с дефиса.\n' >&2; exit 2 ;;
esac
case "$REPO_URL" in
  https://*|http://*|ssh://*|git@*:*|file://*|/*) ;;
  *) printf 'DOCTRINE_REPO_URL должен быть абсолютным URL/путём.\n' >&2; exit 2 ;;
esac
seen_base="${SEEN_FILE##*/}"
seen_parent="${SEEN_FILE%/*}"
[ "$seen_parent" != "$SEEN_FILE" ] || seen_parent="."
case "$seen_base" in
  ""|.|..|*[!A-Za-z0-9._-]*)
    printf 'DOCTRINE_SEEN_FILE должен оканчиваться безопасным именем файла.\n' >&2
    exit 2
    ;;
esac
consumer_root="$(pwd -P)"
seen_parent_abs="$(cd "$seen_parent" 2>/dev/null && pwd -P || true)"
case "$seen_parent_abs" in
  "$consumer_root"|"$consumer_root"/*) ;;
  *) printf 'DOCTRINE_SEEN_FILE должен находиться внутри текущего проекта и иметь существующий parent-каталог.\n' >&2; exit 2 ;;
esac
if { [ -e "$SEEN_FILE" ] || [ -L "$SEEN_FILE" ]; } && { [ ! -f "$SEEN_FILE" ] || [ -L "$SEEN_FILE" ]; }; then
  printf 'DOCTRINE_SEEN_FILE должен быть обычным файлом, не symlink.\n' >&2
  exit 2
fi
case "$NETWORK_TIMEOUT_SECONDS" in
  ""|0|*[!0-9]*)
    printf 'DOCTRINE_NETWORK_TIMEOUT_SECONDS должен быть целым числом больше нуля.\n' >&2
    exit 2
    ;;
esac

build_ssh_command() {
  local custom="${GIT_SSH_COMMAND:-ssh}" program suffix
  case "$custom" in
    ssh|ssh\ *|/usr/bin/ssh|/usr/bin/ssh\ *) ;;
    *)
      printf 'GIT_SSH_COMMAND должен запускать OpenSSH (ssh или /usr/bin/ssh), чтобы doctrine могла гарантировать timeout.\n' >&2
      return 2
      ;;
  esac
  case "$custom" in
    *ConnectTimeout*|*ServerAliveInterval*|*ServerAliveCountMax*)
      printf 'Не задавай timeout-опции в GIT_SSH_COMMAND: doctrine добавляет обязательные значения сама.\n' >&2
      return 2
      ;;
  esac
  program="${custom%% *}"
  [ "$program" != "$custom" ] && suffix="${custom#* }" || suffix=""
  SSH_COMMAND_WITH_TIMEOUT="$program -o ConnectTimeout=$NETWORK_TIMEOUT_SECONDS -o ServerAliveInterval=$NETWORK_TIMEOUT_SECONDS -o ServerAliveCountMax=1"
  [ -z "$suffix" ] || SSH_COMMAND_WITH_TIMEOUT="$SSH_COMMAND_WITH_TIMEOUT $suffix"
}
build_ssh_command || exit $?

# Доктрина — приватный репо. Локально доступ даёт git credential helper
# (`gh auth` / SSH-ключ). В CI интерактивного логина нет — туда пробрасывается
# fine-grained read-only DOCTRINE_TOKEN. Токен не добавляется в URL: иначе
# постоянный clone сохранял бы секрет в remote.origin.url.
ASKPASS=""
if [ -n "${DOCTRINE_TOKEN:-}" ]; then
  ASKPASS="$(mktemp)"
  printf '%s\n' '#!/bin/sh' \
    'case "$1" in' \
    '  *Username*) printf "%s\n" "x-access-token" ;;' \
    '  *Password*) printf "%s\n" "$DOCTRINE_TOKEN" ;;' \
    'esac' > "$ASKPASS"
  chmod 700 "$ASKPASS"
  export DOCTRINE_TOKEN
fi

run_with_timeout() {
  local timeout_seconds="$1" command_pid watchdog_pid command_rc timeout_flag timeout_state
  shift
  timeout_flag="$(mktemp)"
  printf 'waiting\n' > "$timeout_flag"
  "$@" &
  command_pid=$!
  (
    sleep "$timeout_seconds"
    printf 'timeout\n' > "$timeout_flag"
    kill -TERM "$command_pid" 2>/dev/null || exit 0
    sleep 1
    kill -KILL "$command_pid" 2>/dev/null || true
  ) &
  watchdog_pid=$!
  if wait "$command_pid"; then command_rc=0; else command_rc=$?; fi
  kill "$watchdog_pid" 2>/dev/null || true
  wait "$watchdog_pid" 2>/dev/null || true
  timeout_state="$(<"$timeout_flag")"
  rm -f -- "$timeout_flag"
  if [ "$timeout_state" = "timeout" ]; then
    printf 'Git-операция превысила timeout %s с.\n' "$timeout_seconds" >&2
    return 124
  fi
  return "$command_rc"
}

load_credential_config() {
  local scope entry key value
  GIT_CREDENTIAL_ARGS=()
  for scope in system global; do
    while IFS= read -r -d '' entry; do
      key="${entry%%$'\n'*}"
      value="${entry#*$'\n'}"
      GIT_CREDENTIAL_ARGS+=(-c "$key=$value")
    done < <(
      if [ "$scope" = "system" ]; then
        env -u GIT_CONFIG -u GIT_CONFIG_PARAMETERS -u GIT_CONFIG_SYSTEM -u GIT_CONFIG_NOSYSTEM \
          GIT_CONFIG_COUNT=0 GIT_CONFIG_GLOBAL=/dev/null \
          git config --system --includes --null --get-regexp '^credential(\..*)?\.(helper|username|usehttppath)$' 2>/dev/null || true
      else
        env -u GIT_CONFIG -u GIT_CONFIG_PARAMETERS -u GIT_CONFIG_GLOBAL \
          GIT_CONFIG_COUNT=0 GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_SYSTEM=/dev/null \
          git config --global --includes --null --get-regexp '^credential(\..*)?\.(helper|username|usehttppath)$' 2>/dev/null || true
      fi
    )
  done
}
load_credential_config

remote_git() {
  status_git_metadata_effect=true
  case "$REPO_URL" in
    https://*|http://*|ssh://*|git@*:*) status_network_effect=true ;;
  esac
  if [ -n "$ASKPASS" ]; then
    run_with_timeout "$NETWORK_TIMEOUT_SECONDS" env \
      -u GIT_CONFIG -u GIT_CONFIG_PARAMETERS \
      GIT_CONFIG_COUNT=0 GIT_CONFIG_NOSYSTEM=1 \
      GIT_CONFIG_SYSTEM=/dev/null GIT_CONFIG_GLOBAL=/dev/null \
      GIT_ASKPASS="$ASKPASS" GIT_TERMINAL_PROMPT=0 GIT_SSH_COMMAND="$SSH_COMMAND_WITH_TIMEOUT" git \
      "${GIT_CREDENTIAL_ARGS[@]}" \
      -c http.lowSpeedLimit=1 -c "http.lowSpeedTime=$NETWORK_TIMEOUT_SECONDS" "$@"
  else
    run_with_timeout "$NETWORK_TIMEOUT_SECONDS" env \
      -u GIT_CONFIG -u GIT_CONFIG_PARAMETERS \
      GIT_CONFIG_COUNT=0 GIT_CONFIG_NOSYSTEM=1 \
      GIT_CONFIG_SYSTEM=/dev/null GIT_CONFIG_GLOBAL=/dev/null \
      GIT_SSH_COMMAND="$SSH_COMMAND_WITH_TIMEOUT" git \
      "${GIT_CREDENTIAL_ARGS[@]}" \
      -c http.lowSpeedLimit=1 -c "http.lowSpeedTime=$NETWORK_TIMEOUT_SECONDS" "$@"
  fi
}

trusted_doctrine_checkout() {
  local dir="$1" require_seen_match="${2:-1}" top dir_abs top_abs common_dir actual_origin checkout_status current_branch
  local seen_rev head_rev seen_lines seen_line tracking_ref fetch_rc trust_root trust_tmp trust_template trust_bundle trusted_tip
  trust_error=""
  trust_infra_failure=0
  trust_infra_rc=0
  [ -d "$dir" ] || { trust_error="каталог отсутствует"; return 1; }
  top="$(git -C "$dir" rev-parse --show-toplevel 2>/dev/null || true)"
  [ -n "$top" ] || { trust_error="Git не видит самостоятельный репозиторий"; return 1; }
  dir_abs="$(cd "$dir" && pwd -P)"
  top_abs="$(cd "$top" 2>/dev/null && pwd -P || true)"
  [ "$top_abs" = "$dir_abs" ] || { trust_error="Git поднимается к родительскому репозиторию $top"; return 1; }
  common_dir="$(git -C "$dir" rev-parse --path-format=absolute --git-common-dir 2>/dev/null || true)"
  [ -n "$common_dir" ] || { trust_error="не удалось определить Git common dir"; return 1; }
  if [ -e "$common_dir/info/grafts" ] || [ -L "$common_dir/info/grafts" ]; then
    trust_error="legacy info/grafts меняет историю checkout"
    return 1
  fi
  grep -qxF 'rikanv-doctrine/v1' "$dir/.rikanv-doctrine-id" 2>/dev/null || {
    trust_error="нет identity-маркера .rikanv-doctrine-id"; return 1;
  }
  if git -C "$dir" config --local --includes --name-only --get-regexp \
    '^(url\..*\.insteadof|include(if\..*)?\.path)$' >/dev/null 2>&1; then
    trust_error="локальная Git-конфигурация перенаправляет или подключает источник"
    return 1
  fi
  actual_origin="$(git -C "$dir" config --local --get remote.origin.url 2>/dev/null || true)"
  [ -n "$actual_origin" ] || { trust_error="не настроен remote origin"; return 1; }
  normalize_repo_url "$actual_origin"; actual_origin="$normalized_repo_url"
  normalize_repo_url "$REPO_URL"
  [ "$actual_origin" = "$normalized_repo_url" ] || {
    trust_error="origin '$actual_origin' не совпадает с доверенным '$normalized_repo_url'"; return 1;
  }
  current_branch="$(git -C "$dir" symbolic-ref --quiet --short HEAD 2>/dev/null || true)"
  if [ -n "$current_branch" ] && [ "$current_branch" != "$BRANCH" ]; then
    trust_error="checkout находится на ветке '$current_branch', а настроена '$BRANCH'"
    return 1
  fi
  if ! checkout_status="$(git -C "$dir" status --porcelain=v1 --untracked-files=all 2>/dev/null)"; then
    trust_error="не удалось проверить чистоту checkout"
    return 1
  fi
  [ -z "$checkout_status" ] || { trust_error="checkout содержит незакоммиченные изменения"; return 1; }
  if [ "$require_seen_match" -eq 1 ] && [ -f "$SEEN_FILE" ]; then
    seen_rev=""
    seen_lines=0
    while IFS= read -r seen_line || [ -n "$seen_line" ]; do
      seen_lines=$((seen_lines + 1))
      [ "$seen_lines" -ne 1 ] || seen_rev="${seen_line%$'\r'}"
    done < "$SEEN_FILE"
    head_rev="$(git -C "$dir" rev-parse HEAD 2>/dev/null || true)"
    if [ "$seen_lines" -ne 1 ] || [ -z "$head_rev" ] || [ "$seen_rev" != "$head_rev" ]; then
      trust_error="$SEEN_FILE не соответствует HEAD доверенного checkout"
      return 1
    fi
  fi
  # Fetch выполняется в новом bare repo, не читающем конфигурацию проверяемого
  # checkout. Объекты возвращаются bundle-командой без URL/remote resolution.
  tracking_ref="$TRUSTED_REF"
  trust_root="$(mktemp -d)"
  trust_tmp="$trust_root/repo.git"
  trust_template="$trust_root/empty-template"
  trust_bundle="$trust_root/trusted.bundle"
  mkdir "$trust_template"
  if ! env -u GIT_CONFIG -u GIT_CONFIG_PARAMETERS -u GIT_TEMPLATE_DIR \
    GIT_CONFIG_COUNT=0 GIT_CONFIG_NOSYSTEM=1 \
    GIT_CONFIG_SYSTEM=/dev/null GIT_CONFIG_GLOBAL=/dev/null \
    git init --quiet --bare --template="$trust_template" "$trust_tmp"; then
    rm -rf -- "$trust_root"
    trust_error="не удалось создать временное хранилище trust-fetch"
    trust_infra_failure=1
    trust_infra_rc=1
    return 1
  fi
  fetch_rc=0
  remote_git -C "$trust_tmp" fetch --quiet --no-tags -- "$REPO_URL" \
    "+refs/heads/$BRANCH:$tracking_ref" || fetch_rc=$?
  if [ "$fetch_rc" -ne 0 ]; then
    # Session передаёт private metadata-файл. Только неудача фактического
    # trust-fetch разрешает ей ограниченный offline fallback; trust/integrity
    # ошибки никогда не помечаются как сеть.
    if [ -n "${DOCTRINE_SESSION_FAILURE_FILE:-}" ]; then
      printf '%s\n' network >"$DOCTRINE_SESSION_FAILURE_FILE"
    fi
    rm -rf -- "$trust_root"
    trust_error="не удалось получить доверенную ветку $BRANCH из origin"
    trust_infra_failure=1
    trust_infra_rc="$fetch_rc"
    return 1
  fi
  trusted_tip="$(git -C "$trust_tmp" rev-parse "$tracking_ref")"
  if ! git -C "$trust_tmp" bundle create "$trust_bundle" "$tracking_ref" >/dev/null ||
     ! git -C "$dir" bundle unbundle "$trust_bundle" >/dev/null 2>&1 ||
     ! git -C "$dir" update-ref "$tracking_ref" "$trusted_tip"; then
    rm -rf -- "$trust_root"
    trust_error="не удалось импортировать доверенную ревизию origin/$BRANCH"
    trust_infra_failure=1
    trust_infra_rc=1
    return 1
  fi
  if ! git -C "$dir" merge-base --is-ancestor HEAD "$tracking_ref" 2>/dev/null; then
    trust_error="HEAD не происходит из актуальной доверенной ветки origin/$BRANCH"
    return 1
  fi
  return 0
}

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m%s\033[0m\n' "$*"; }

# is_hard PATH — успех (0), если во frontmatter файла есть `dependency: hard`.
# Сканируем именно frontmatter-блок (между первыми «---» … «---»), а не
# фиксированное число строк: длина описания/use-when у файлов разная.
# Код возврата отдаём ТОЛЬКО через END (через флаг found): ранний `exit 0`
# всё равно запускает END, и `exit` там перезаписал бы код — отсюда флаг.
is_hard() {
  awk '
    NR==1 && $0!="---"   { exit }      # нет frontmatter — не hard
    seen && $0=="---"    { exit }      # конец frontmatter — дальше не ищем
    { seen=1 }
    /^dependency:[[:space:]]+hard[[:space:]]*$/ { found=1; exit }
    END { exit !found }
  ' "$1"
}

read_seen_marker() {
  seen_file_present=0
  seen_file_valid=0
  old_rev=""
  if [ -f "$SEEN_FILE" ]; then
    seen_file_present=1
    seen_lines=0
    while IFS= read -r seen_line || [ -n "$seen_line" ]; do
      seen_lines=$((seen_lines + 1))
      [ "$seen_lines" -ne 1 ] || old_rev="${seen_line%$'\r'}"
    done < "$SEEN_FILE"
    old_rev_len="${#old_rev}"
    case "$old_rev" in *[!0-9a-f]*) old_rev_hex=0 ;; *) old_rev_hex=1 ;; esac
    if [ "$seen_lines" -eq 1 ] && { [ "$old_rev_len" -eq 40 ] || [ "$old_rev_len" -eq 64 ]; } &&
       [ "$old_rev_hex" -eq 1 ]; then
      seen_file_valid=1
    fi
  fi
}

# Повреждённый существующий marker — отдельное состояние, не «первый запуск».
# Проверяем до clone/trust, чтобы код 15 не зависел от наличия ignored checkout.
read_seen_marker
if [ "$seen_file_present" -eq 1 ] && [ "$seen_file_valid" -ne 1 ]; then
  warn "Маркер $SEEN_FILE существует, но не содержит ровно один полный commit SHA."
  warn "Нужна ручная сверка; marker не изменён."
  exit 15
fi

# Временный каталог для сверки без постоянного клона (CI-раннер, см. ниже).
# Объявляем и вешаем trap заранее, до первого возможного exit: если скрипт
# упадёт (в том числе на set -e) уже после того, как временный клон создан,
# каталог всё равно должен быть удалён, а не остаться мусором в /tmp раннера.
TMP_CLONE=""
# Форма if, а не `[ -n "$TMP_CLONE" ] && rm ...`: если TMP_CLONE пуст (обычный
# клон, временный каталог не создавался), тест `[ -n ... ]` возвращает 1, и
# это стало бы ПОСЛЕДНЕЙ командой EXIT-обработчика — bash в этом случае
# подменяет код возврата всего скрипта на код последней команды трапа,
# затирая настоящий exit N (например, 11 превратился бы в 1). `if` этого не
# делает: код возврата функции не зависит от того, была ветка пустой.
cleanup_tmp_clone() {
  if [ -n "$TMP_CLONE" ]; then rm -rf -- "$TMP_CLONE"; fi
  if [ -n "$ASKPASS" ]; then rm -f -- "$ASKPASS"; fi
}
trap 'status_exit_code=$?; trap - EXIT; cleanup_tmp_clone; emit_status_detail; exit "$status_exit_code"' EXIT

# --- Гарантировать наличие клона (или временную замену для --check) --------
doctrine_repo_present=0
trust_seen_match=1
trusted_doctrine_checkout "$DOCTRINE_DIR" "$trust_seen_match" && doctrine_repo_present=1
if [ "$trust_infra_failure" -ne 0 ]; then exit "$trust_infra_rc"; fi
if [ "$doctrine_repo_present" -ne 1 ]; then
  if [ -e "$DOCTRINE_DIR" ]; then
    if [[ "$trust_error" == HEAD\ не\ происходит\ из\ актуальной\ доверенной\ ветки* ]] && [ -f "$SEEN_FILE" ]; then
      warn "Ревизия маркера больше не принадлежит актуальной истории origin/$BRANCH; полная ручная сверка обязательна."
      exit 15
    fi
    warn "Каталог $DOCTRINE_DIR не является доверенным checkout доктрины: $trust_error."
    warn "Не перезаписываю его; выбери другой путь или убери конфликт вручную."
    exit 13
  fi
  if [ "$MODE" = "check" ]; then
    # На CI-раннере постоянного клона нет НИКОГДА — доктрина в .gitignore
    # потребителя (см. doctrine-subscribe.sh, шаг 3), actions/checkout её не
    # восстанавливает. Раньше здесь сразу отдавался exit 10 («потребитель
    # отстал») — это была ложь: мы ещё не смотрели на реальное состояние
    # доктрины, отсутствие клона ничего не говорит об отставании. Если у
    # потребителя есть закоммиченный маркер .doctrine-version — сверяться
    # есть с чем: клонируем доктрину во временный каталог (свежий клон уже
    # стоит на нужной ветке, fetch/merge ему не нужны) и продолжаем обычным
    # потоком сравнения ниже. Если маркера тоже нет — сверяться реально не с
    # чем, это отдельный код 11, а не «отстал».
    if [ ! -f "$SEEN_FILE" ]; then
      warn "Доктрина не подключена и маркер $SEEN_FILE отсутствует — сверяться не с чем."
      warn "Запусти doctrine-session.sh start, прочитай подготовленные документы и выполни acknowledge."
      exit 11
    fi
    bold "Постоянного клона нет — временно клонирую доктрину для сверки"
    TMP_CLONE="$(mktemp -d)"
    remote_git clone --quiet --branch "$BRANCH" -- "$REPO_URL" "$TMP_CLONE"
    DOCTRINE_DIR="$TMP_CLONE"
    trust_seen_match=0
  else
    bold "Доктрина не найдена — клонирую в $DOCTRINE_DIR"
    remote_git clone --quiet --branch "$BRANCH" -- "$REPO_URL" "$DOCTRINE_DIR"
    trust_seen_match=0
    if ! trusted_doctrine_checkout "$DOCTRINE_DIR" "$trust_seen_match"; then
      [ "$trust_infra_failure" -eq 0 ] || exit "$trust_infra_rc"
      warn "Клон $DOCTRINE_DIR не прошёл проверку доверия: $trust_error."
      warn "Маркер $SEEN_FILE не записан; каталог автоматически не удаляю."
      exit 13
    fi
    # Даже для обычного sync продолжаем общий путь сравнения ниже. Клон мог
    # исчезнуть как ignored-артефакт, пока закоммиченный marker сохранился;
    # его нельзя молча выдать за «первое подключение» и перезаписать.
  fi
fi

# --- Страховка маркера: git-контекст — именно клон доктрины ------------------
# `git -C DIR` не требует, чтобы DIR был корнем репозитория: если валидного
# .git в DIR нет, git поднимается по дереву каталогов и берёт ПЕРВЫЙ найденный
# репозиторий — внутри проекта-потребителя это сам потребитель. Ровно так
# портился маркер: пустой/битый doctrine/.git (типовой случай — скопированный
# без содержимого каталог в throwaway-worktree потребителя) проходит проверку
# `-d .git` выше, после чего fetch/merge/rev-parse молча работают с
# репозиторием ПОТРЕБИТЕЛЯ, и в .doctrine-version записывается его SHA вместо
# ревизии доктрины. Поэтому ДО первого `git -C` сверяем: корень репозитория,
# который git видит из $DOCTRINE_DIR, — это сам $DOCTRINE_DIR. Пути сравниваем
# физические (pwd -P): git отдаёт toplevel с раскрытыми симлинками, и
# лексическое сравнение дало бы ложный отказ на симлинк-пути.
if ! trusted_doctrine_checkout "$DOCTRINE_DIR" "$trust_seen_match"; then
  [ "$trust_infra_failure" -eq 0 ] || exit "$trust_infra_rc"
  if [[ "$trust_error" == HEAD\ не\ происходит\ из\ актуальной\ доверенной\ ветки* ]] && [ -f "$SEEN_FILE" ]; then
    warn "Ревизия маркера больше не принадлежит актуальной истории origin/$BRANCH; полная ручная сверка обязательна."
    exit 15
  fi
  doctrine_top="$(git -C "$DOCTRINE_DIR" rev-parse --show-toplevel 2>/dev/null || true)"
  warn "Каталог $DOCTRINE_DIR не является доверенным самостоятельным клоном доктрины: $trust_error."
  if [ -n "$doctrine_top" ]; then
    warn "git-контекст из него поднимается к «$doctrine_top» — это РОДИТЕЛЬСКИЙ репозиторий, и его SHA попал бы в маркер."
  else
    warn "git не находит в нём валидного репозитория."
  fi
  warn "Не делаю ничего: маркер $SEEN_FILE не тронут. Рецепт: удали каталог $DOCTRINE_DIR и запусти sync заново (rm -rf сам не выполняю — удаление данных решает человек)."
  exit 13
fi

# --- Использовать вершину, уже полученную trust-fetch -----------------------
# `trusted_doctrine_checkout` непосредственно перед этим блоком принудительно
# обновил TRUSTED_REF из явного REPO_URL. Не делаем второй fetch через имя
# remote: его refspec — локальная конфигурация checkout и не является границей
# доверия. В --check рабочее дерево не двигаем; обычный sync делает ff-only.
if [ "$MODE" = "check" ]; then
  new_rev="$(git -C "$DOCTRINE_DIR" rev-parse "$TRUSTED_REF")"
else
  if ! git -C "$DOCTRINE_DIR" merge --quiet --ff-only "$TRUSTED_REF"; then
    warn "Клон $DOCTRINE_DIR разошёлся с актуальной доверенной веткой origin/$BRANCH — fast-forward невозможен."
    warn "Рецепт: rm -rf $DOCTRINE_DIR и запусти sync заново (сам rm -rf НЕ выполняю — это удаление данных, решать человеку)."
    exit 12
  fi
  new_rev="$(git -C "$DOCTRINE_DIR" rev-parse HEAD)"
fi

# Версию читаем через `git show`, а не `cat VERSION` из рабочего дерева: в
# режиме --check с уже существующим клоном merge не делается (см. выше), и
# рабочее дерево остаётся на старой ревизии — `cat` подсунул бы старую версию
# под видом новой. `git show rev:file` не зависит от checkout вообще.
new_ver="$(git -C "$DOCTRINE_DIR" show "$new_rev:VERSION" 2>/dev/null)"
new_ver="${new_ver%$'\r'}"
new_ver="${new_ver:-?}"

status_trusted_revision="$new_rev"
[ "$new_ver" = "?" ] || status_trusted_version="$new_ver"
if [ -n "$old_rev" ]; then
  status_seen_version="$(git -C "$DOCTRINE_DIR" show "$old_rev:VERSION" 2>/dev/null | tr -d '[:space:]' || true)"
fi

# Ревизия, на которой потребитель сверился в прошлый раз.
if [ "$MODE" = "check" ] && [ -z "$old_rev" ]; then
  warn "Маркер $SEEN_FILE отсутствует — определить непросмотренную дельту нельзя."
  warn "Запусти doctrine-session.sh start, затем подтвердить подготовленную ревизию через acknowledge."
  exit 11
fi

# --- Нечего показывать ------------------------------------------------------
if [ -n "$old_rev" ] && [ "$old_rev" = "$new_rev" ]; then
  bold "Доктрина актуальна (версия $new_ver). Непросмотренных изменений нет."
  exit 0
fi

# --- Есть дельта ------------------------------------------------------------
comparison_known=0
if [ -n "$old_rev" ] &&
   git -C "$DOCTRINE_DIR" cat-file -e "$old_rev^{commit}" 2>/dev/null &&
   git -C "$DOCTRINE_DIR" merge-base --is-ancestor "$old_rev" "$new_rev" 2>/dev/null; then
  comparison_known=1
  old_ver="$(git -C "$DOCTRINE_DIR" show "$old_rev:VERSION" 2>/dev/null || echo '?')"
  old_ver="${old_ver%$'\r'}"
  changed="$(git -C "$DOCTRINE_DIR" diff --name-only "$old_rev" "$new_rev")"
else
  old_ver="?"
  changed=""   # первый запуск или маркер указывает на неизвестную ревизию
fi

if [ -n "$old_rev" ] && [ "$comparison_known" -ne 1 ]; then
  warn "Ревизия маркера $old_rev отсутствует в актуальной истории доктрины; hard/soft-классификация невозможна."
  warn "Нужна полная ручная сверка; marker не изменён."
  exit 15
fi

bold "Доктрина обновлена: ${old_ver} → ${new_ver}"
echo

if [ -n "$changed" ]; then
  # hard-файлы среди изменённых — по frontmatter `dependency: hard`. Берём
  # содержимое файла из git-объекта на ревизии new_rev (`git show`), а не с
  # диска: в режиме --check с уже существующим клоном merge не делается (см.
  # выше), и рабочее дерево может стоять на старой ревизии — чтение с диска
  # подсунуло бы старое содержимое файла под видом нового.
  hard=""
  while IFS= read -r f; do
    [ -z "$f" ] && continue
    case "$f" in
      *.md)
        old_hard=0
        new_hard=0
        if git -C "$DOCTRINE_DIR" cat-file -e "$old_rev:$f" 2>/dev/null &&
           is_hard <(git -C "$DOCTRINE_DIR" show "$old_rev:$f" 2>/dev/null); then
          old_hard=1
        fi
        if git -C "$DOCTRINE_DIR" cat-file -e "$new_rev:$f" 2>/dev/null &&
           is_hard <(git -C "$DOCTRINE_DIR" show "$new_rev:$f" 2>/dev/null); then
          new_hard=1
        fi
        if [ "$old_hard" -eq 1 ] || [ "$new_hard" -eq 1 ]; then
          hard="${hard}${f}"$'\n'
        fi
        ;;
    esac
  done <<EOF
$changed
EOF

  echo "Изменённые файлы:"
  printf '%s\n' "$changed" | sed 's/^/  /'
  echo

  if [ -n "$hard" ]; then
    warn "HARD-файлы изменились — перечитай перед задачей, где они важны:"
    printf '%s' "$hard" | sed 's/^/  - /'
    echo
  fi
else
  echo "Первая сверка с этой доктриной (предыдущая ревизия неизвестна)."
  echo "Полный список норм — README.md; что нового — CHANGELOG.md."
  echo
fi

# Дельта человекочитаемого журнала: всё выше старой версии. Журнал читается
# через git show из new_rev, а не с диска: в режиме --check рабочее дерево
# существующего клона не обновляется (merge не выполняется), и дисковый
# CHANGELOG.md остаётся на старой ревизии — awk встретил бы запись old_ver
# первой же и напечатал бы пустую дельту. Та же ловушка «чтения с устаревшего
# дерева», что закрыта выше для VERSION и hard-файлов.
#
# awk дочитывает поток ДО КОНЦА, печать глушится флагом stopped — ранний
# `exit` здесь запрещён: awk — читатель конвейера, и его досрочный выход
# оставляет git show писать в закрытый пайп. Пока непрочитанный остаток
# журнала меньше буфера конвейера (~64 КиБ), git show успевает дописать и
# гонка молчит; стоило журналу перерасти буфер — git show получил SIGPIPE,
# `set -euo pipefail` превратил статус конвейера (141) в аборт скрипта до
# записи маркера, а в --check — до штатного exit 10 (CI увидел «сбой
# инфраструктуры» вместо «потребитель отстал»). Дочитать журнал стоит
# миллисекунды; SIGPIPE исключается структурно, а не подавляется.
if [ "$old_ver" != '?' ] && git -C "$DOCTRINE_DIR" cat-file -e "$new_rev:CHANGELOG.md" 2>/dev/null; then
  echo "Из CHANGELOG.md:"
  git -C "$DOCTRINE_DIR" show "$new_rev:CHANGELOG.md" | awk -v stop="## [$old_ver]" '
    /^## \[/ && index($0, stop) { stopped = 1 }
    /^## \[/ && !stopped        { started = 1 }
    started && !stopped         { print "  " $0 }
  '
  echo
fi

if [ "$MODE" = "check" ]; then
  if [ -n "${hard:-}" ]; then
    warn "Потребитель отстал: в дельте есть hard-файлы. Запусти doctrine-session.sh start и сверь затронутое."
    exit 10
  fi
  warn "Потребитель отстал, но дельта содержит только soft/derived-файлы."
  exit 14
fi

# Подтверждение marker намеренно принадлежит только session acknowledge.
# Успешная подготовка оставляет `.doctrine-version` без изменений.
bold "Ревизия подготовлена без изменения marker; продолжи через doctrine-session.sh acknowledge."
