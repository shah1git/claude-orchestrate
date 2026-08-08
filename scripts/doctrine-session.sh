#!/usr/bin/env bash
# managed-by: rikanv-doctrine
#
# Двухфазная сессия доктрины. `start` вправе подготовить только доверенный
# ff-only checkout; `.doctrine-version` продвигается исключительно точной
# командой `acknowledge`. Pending и ограниченный TTL offline-cache лежат в
# metadata именно текущего Git worktree, поэтому worktree не делят состояние между собой.
set -euo pipefail
export GIT_NO_REPLACE_OBJECTS=1

script_dir="$(cd "$(dirname "$0")" && pwd -P)"
sync_script="$script_dir/doctrine-sync.sh"
[ -x "$sync_script" ] || [ -f "$sync_script" ] || { printf '%s\n' 'doctrine-session.sh: missing managed doctrine-sync.sh' >&2; exit 2; }

SESSION_ID="${DOCTRINE_SESSION_ID:-default}"
SESSION_TTL_SECONDS="${DOCTRINE_SESSION_TTL_SECONDS:-21600}"
LOCK_TIMEOUT_SECONDS="${DOCTRINE_SESSION_LOCK_TIMEOUT_SECONDS:-10}"
SEEN_FILE="${DOCTRINE_SEEN_FILE:-.doctrine-version}"

case "$SESSION_ID" in ''|*[!A-Za-z0-9._-]*) printf '%s\n' 'DOCTRINE_SESSION_ID must be a safe non-empty identifier' >&2; exit 2 ;; esac
case "$SESSION_TTL_SECONDS" in ''|*[!0-9]*) printf '%s\n' 'DOCTRINE_SESSION_TTL_SECONDS must be a non-negative integer' >&2; exit 2 ;; esac
consumer_root="$(pwd -P)"

seen_base="${SEEN_FILE##*/}"
seen_parent="${SEEN_FILE%/*}"
[ "$seen_parent" != "$SEEN_FILE" ] || seen_parent="."
case "$seen_base" in ""|.|..|*[!A-Za-z0-9._-]*) printf '%s\n' 'DOCTRINE_SEEN_FILE must end in a safe file name' >&2; exit 2 ;; esac
seen_parent_abs="$(cd "$seen_parent" 2>/dev/null && pwd -P || true)"
case "$seen_parent_abs" in "$consumer_root"|"$consumer_root"/*) ;; *) printf '%s\n' 'DOCTRINE_SEEN_FILE must be inside the consumer worktree' >&2; exit 2 ;; esac
if { [ -e "$SEEN_FILE" ] || [ -L "$SEEN_FILE" ]; } && { [ ! -f "$SEEN_FILE" ] || [ -L "$SEEN_FILE" ]; }; then
  printf '%s\n' 'DOCTRINE_SEEN_FILE must be a regular non-symlink file' >&2
  exit 2
fi
case "$LOCK_TIMEOUT_SECONDS" in ''|0|*[!0-9]*) printf '%s\n' 'DOCTRINE_SESSION_LOCK_TIMEOUT_SECONDS must be a positive integer' >&2; exit 2 ;; esac

state_path="$(git rev-parse --git-path rikanv-doctrine-session 2>/dev/null || true)"
[ -n "$state_path" ] || { printf '%s\n' 'doctrine-session.sh must run inside the consumer Git worktree' >&2; exit 2; }
case "$state_path" in /*) state_dir="$state_path" ;; *) state_dir="$consumer_root/$state_path" ;; esac
mkdir -p "$state_dir"
lock_dir="$state_dir/lock"
lock_held=0
release_lock() { if [ "$lock_held" -eq 1 ]; then rmdir "$lock_dir" 2>/dev/null || true; fi; }
trap release_lock EXIT

acquire_lock() {
  local deadline now
  deadline=$(( $(date +%s) + LOCK_TIMEOUT_SECONDS ))
  while ! mkdir "$lock_dir" 2>/dev/null; do
    now=$(date +%s)
    [ "$now" -lt "$deadline" ] || return 1
    sleep 0.1
  done
  lock_held=1
}

json_array_from_file() {
  local file="$1"
  if [ -f "$file" ]; then jq -Rsc 'split("\n") | map(select(length > 0))' <"$file"; else printf '[]'; fi
}

emit_start() {
  local code="$1" confirmed="$2" prepared="$3" local_revision="$4" remote_prepared="$5" hard_json="$6" changelog="$7"
  jq -cn --arg code "$code" --arg confirmed "$confirmed" --arg prepared "$prepared" --arg local "$local_revision" \
    --argjson remote_prepared "$remote_prepared" --argjson hard "$hard_json" --argjson changelog "$changelog" \
    '{schema_version:1,module:"session",operation:"start",code:$code,status:$code,revisions:{confirmed:($confirmed|if .=="" then null else . end),prepared:($prepared|if .=="" then null else . end),local:($local|if .=="" then null else . end)},documents:{hard:$hard,changelog_required:$changelog},prepared_from_remote:$remote_prepared}'
}

emit_ack() {
  local code="$1" confirmed="$2" prepared="$3"
  jq -cn --arg code "$code" --arg confirmed "$confirmed" --arg prepared "$prepared" \
    '{schema_version:1,module:"session",operation:"acknowledge",code:$code,status:$code,revisions:{confirmed:($confirmed|if .=="" then null else . end),prepared:($prepared|if .=="" then null else . end)}}'
}

read_marker() {
  marker_revision=""
  marker_valid=1
  if [ -e "$SEEN_FILE" ] || [ -L "$SEEN_FILE" ]; then
    if [ ! -f "$SEEN_FILE" ] || [ -L "$SEEN_FILE" ]; then marker_valid=0; return; fi
    local -a lines=()
    mapfile -t lines <"$SEEN_FILE"
    if [ "${#lines[@]}" -ne 1 ] || ! [[ "${lines[0]}" =~ ^[0-9a-f]{40}([0-9a-f]{24})?$ ]]; then
      marker_valid=0
    else
      marker_revision="${lines[0]}"
    fi
  fi
}

pending_read() {
  pending_present=0
  pending_revision=""; pending_confirmed=""; pending_remote=false; pending_changelog=false; pending_dir=""
  [ -f "$state_dir/pending" ] || return 0
  local -a fields=()
  mapfile -t fields <"$state_dir/pending"
  [ "${#fields[@]}" -eq 5 ] || return 0
  [[ "${fields[0]}" =~ ^[0-9a-f]{40}([0-9a-f]{24})?$ ]] || return 0
  case "${fields[1]}" in ''|[0-9a-f][0-9a-f]*) ;; *) return 0 ;; esac
  [ "${fields[2]}" = true ] || [ "${fields[2]}" = false ] || return 0
  [ "${fields[3]}" = true ] || [ "${fields[3]}" = false ] || return 0
  [ -n "${fields[4]}" ] || return 0
  pending_present=1; pending_revision="${fields[0]}"; pending_confirmed="${fields[1]}"
  pending_remote="${fields[2]}"; pending_changelog="${fields[3]}"; pending_dir="${fields[4]}"
}

write_pending() {
  local revision="$1" confirmed="$2" remote="$3" changelog="$4" directory="$5"
  printf '%s\n%s\n%s\n%s\n%s\n' "$revision" "$confirmed" "$remote" "$changelog" "$directory" >"$state_dir/pending"
}


is_hard() {
  awk '
    NR==1 && $0!="---" { exit }
    seen && $0=="---" { exit }
    { seen=1 }
    /^dependency:[[:space:]]+hard[[:space:]]*$/ { found=1; exit }
    END { exit !found }
  '
}

# Не соединять git show с рано завершающимся awk под pipefail: process
# substitution изолирует producer, а `is_hard` получает только документ.
is_hard_at_revision() {
  local revision="$1" path="$2"
  git -C "$doctrine_dir" cat-file -e "$revision:$path" 2>/dev/null || return 1
  is_hard < <(git -C "$doctrine_dir" show "$revision:$path" 2>/dev/null)
}

collect_hard_documents() {
  local old="$1" new="$2" path old_hard new_hard
  : >"$state_dir/hard"
  if [ -z "$old" ]; then
    while IFS= read -r path; do
      case "$path" in *.md) is_hard_at_revision "$new" "$path" && printf '%s\n' "$path" >>"$state_dir/hard" ;; esac
    done < <(git -C "$doctrine_dir" ls-tree -r --name-only "$new")
    return
  fi
  while IFS= read -r path; do
    [ -n "$path" ] || continue
    case "$path" in
      *.md)
        old_hard=0; new_hard=0
        is_hard_at_revision "$old" "$path" && old_hard=1
        is_hard_at_revision "$new" "$path" && new_hard=1
        if [ "$old_hard" -eq 1 ] || [ "$new_hard" -eq 1 ]; then printf '%s\n' "$path" >>"$state_dir/hard"; fi
        ;;
    esac
  done < <(git -C "$doctrine_dir" diff --name-only "$old" "$new")
}

checkout_is_dirty() {
  [ -d "$doctrine_dir" ] || return 1
  local top dir_abs status
  top="$(git -C "$doctrine_dir" rev-parse --show-toplevel 2>/dev/null || true)"
  [ -n "$top" ] || return 1
  dir_abs="$(cd "$doctrine_dir" && pwd -P)"
  [ "$(cd "$top" 2>/dev/null && pwd -P || true)" = "$dir_abs" ] || return 1
  status="$(git -C "$doctrine_dir" status --porcelain=v1 --untracked-files=all 2>/dev/null || true)"
  [ -n "$status" ]
}

network_cache_valid() {
  [ -f "$state_dir/network-cache" ] || return 1
  local -a cache=()
  mapfile -t cache <"$state_dir/network-cache"
  [ "${#cache[@]}" -eq 3 ] && [ "${cache[0]}" = "$SESSION_ID" ] && [[ "${cache[1]}" =~ ^[0-9]+$ ]] && [ "$(date +%s)" -le "${cache[1]}" ] && [ "${cache[2]}" = "$marker_revision" ]
}

start() {
  local doctrine_arg="doctrine" arg
  while [ "$#" -gt 0 ]; do
    arg="$1"; shift
    case "$arg" in --status-json) ;; -*) emit_start SYNC_FAILED "" "" "" false '[]' false; return ;; *) doctrine_arg="$arg" ;; esac
  done
  doctrine_dir="$doctrine_arg"
  acquire_lock || { emit_start SYNC_FAILED "" "" "" false '[]' false; return; }
  read_marker
  if [ "$marker_valid" -ne 1 ]; then emit_start UNTRUSTED_CHECKOUT "" "" "" false '[]' false; return; fi
  if checkout_is_dirty; then emit_start DIRTY_CHECKOUT "$marker_revision" "" "" false '[]' false; return; fi
  pending_read
  if [ "$pending_present" -eq 1 ]; then
    doctrine_dir="$pending_dir"
    if [ "$(git -C "$doctrine_dir" rev-parse HEAD 2>/dev/null || true)" = "$pending_revision" ]; then
      emit_start READING_REQUIRED "$pending_confirmed" "$pending_revision" "$pending_revision" "$pending_remote" "$(json_array_from_file "$state_dir/hard")" "$pending_changelog"
    else
      emit_start UNTRUSTED_CHECKOUT "$marker_revision" "" "" false '[]' false
    fi
    return
  fi
  if [ -f "$state_dir/current-cache" ]; then
    local -a current_cache=()
    mapfile -t current_cache <"$state_dir/current-cache"
    if [ "${#current_cache[@]}" -eq 4 ] && [ "${current_cache[0]}" = "$SESSION_ID" ] &&
       [[ "${current_cache[1]}" =~ ^[0-9]+$ ]] && [ "$(date +%s)" -le "${current_cache[1]}" ] &&
       [ "${current_cache[2]}" = "$marker_revision" ] && [ "${current_cache[3]}" = "$marker_revision" ]; then
      emit_start CURRENT "$marker_revision" "" "$marker_revision" false '[]' false
      return
    fi
  fi
  if network_cache_valid; then
    emit_start NETWORK_UNAVAILABLE "$marker_revision" "" "$(git -C "$doctrine_dir" rev-parse HEAD 2>/dev/null || true)" false '[]' false
    return
  fi
  : >"$state_dir/failure"
  local sync_rc=0 old="$marker_revision" new remote=false
  DOCTRINE_SESSION_INTERNAL=1 DOCTRINE_SESSION_FAILURE_FILE="$state_dir/failure" \
    bash "$sync_script" "$doctrine_dir" >/dev/null 2>&1 || sync_rc=$?
  if [ "$sync_rc" -ne 0 ]; then
    if [ "$sync_rc" -eq 13 ] || [ "$sync_rc" -eq 15 ]; then
      emit_start UNTRUSTED_CHECKOUT "$old" "" "$(git -C "$doctrine_dir" rev-parse HEAD 2>/dev/null || true)" false '[]' false
    elif [ "$(<"$state_dir/failure")" = network ] && [ -n "$old" ] && [ "$(git -C "$doctrine_dir" rev-parse HEAD 2>/dev/null || true)" = "$old" ]; then
      printf '%s\n%s\n%s\n' "$SESSION_ID" "$(( $(date +%s) + SESSION_TTL_SECONDS ))" "$old" >"$state_dir/network-cache"
      emit_start NETWORK_UNAVAILABLE "$old" "" "$old" false '[]' false
    else
      emit_start SYNC_FAILED "$old" "" "$(git -C "$doctrine_dir" rev-parse HEAD 2>/dev/null || true)" false '[]' false
    fi
    return
  fi
  new="$(git -C "$doctrine_dir" rev-parse HEAD 2>/dev/null || true)"
  if [ -z "$new" ]; then emit_start SYNC_FAILED "$old" "" "" false '[]' false; return; fi
  if [ -n "$old" ] && [ "$old" = "$new" ]; then
    printf '%s\n%s\n%s\n%s\n' "$SESSION_ID" "$(( $(date +%s) + SESSION_TTL_SECONDS ))" "$old" "$new" >"$state_dir/current-cache"
    emit_start CURRENT "$old" "" "$new" false '[]' false
    return
  fi
  [ -n "$old" ] && remote=true
  collect_hard_documents "$old" "$new"
  # CHANGELOG обязателен после remote update; при первом чтении известной
  # истории его нет, поэтому status не требует несуществующего сравнения.
  write_pending "$new" "$old" "$remote" "$remote" "$doctrine_dir"
  emit_start PREPARED "$old" "$new" "$new" "$remote" "$(json_array_from_file "$state_dir/hard")" "$remote"
}

acknowledge() {
  [ "$#" -eq 1 ] || { emit_ack ACKNOWLEDGEMENT_REJECTED "" ""; return; }
  local requested="$1" current
  [[ "$requested" =~ ^[0-9a-f]{40}([0-9a-f]{24})?$ ]] || { emit_ack ACKNOWLEDGEMENT_REJECTED "" ""; return; }
  acquire_lock || { emit_ack ACKNOWLEDGEMENT_REJECTED "" ""; return; }
  read_marker; pending_read
  if [ "$marker_valid" -ne 1 ] || [ "$pending_present" -ne 1 ] || [ "$requested" != "$pending_revision" ] || [ "$marker_revision" != "$pending_confirmed" ]; then
    emit_ack ACKNOWLEDGEMENT_REJECTED "$marker_revision" "$pending_revision"; return
  fi
  local doctrine_dir="$pending_dir"
  current="$(git -C "$doctrine_dir" rev-parse HEAD 2>/dev/null || true)"
  if [ "$current" != "$requested" ] || checkout_is_dirty; then
    emit_ack ACKNOWLEDGEMENT_REJECTED "$marker_revision" "$pending_revision"; return
  fi
  local marker_parent marker_tmp
  marker_parent="$(dirname "$SEEN_FILE")"
  marker_tmp="$marker_parent/.doctrine-version.session.$$"
  (umask 077; printf '%s\n' "$requested" >"$marker_tmp")
  mv -f "$marker_tmp" "$SEEN_FILE"
  : >"$state_dir/pending"; : >"$state_dir/hard"; : >"$state_dir/network-cache"
  emit_ack ACKNOWLEDGED "$requested" "$requested"
}

[ "$#" -gt 0 ] || { printf '%s\n' 'usage: doctrine-session.sh start [DOCTRINE_DIR] --status-json | acknowledge <revision>' >&2; exit 2; }
operation="$1"; shift
case "$operation" in
  start) start "$@" ;;
  acknowledge) acknowledge "$@" ;;
  *) printf '%s\n' 'usage: doctrine-session.sh start [DOCTRINE_DIR] --status-json | acknowledge <revision>' >&2; exit 2 ;;
esac
