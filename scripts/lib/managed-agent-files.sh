#!/usr/bin/env bash
# managed-by: rikanv-doctrine

# Safe ownership operations for Markdown files maintained alongside user text.
# Functions return non-zero and set managed_agent_files_error; callers decide
# their public error envelope and must call validation before writing.

managed_agent_files_error=""

managed_agent_files_fail() {
  managed_agent_files_error="$1"
  return 1
}

managed_agent_files_regular_or_absent() {
  local file="$1" label="$2"
  if [ -e "$file" ] || [ -L "$file" ]; then
    [ -f "$file" ] && [ ! -L "$file" ] || managed_agent_files_fail "$label must be a regular file"
  fi
}

# Validates named, exact-line-delimited blocks. Arguments after file are
# triples: name, start marker, end marker. A marker merely containing the
# managed text is an altered marker, not user text. All blocks are validated
# together so a crossing pair cannot be mistaken for two valid blocks.
managed_agent_files_validate_blocks() {
  local file="$1"
  shift
  local triple_count=$#
  [ $((triple_count % 3)) -eq 0 ] || managed_agent_files_fail 'block definitions must be triples'
  [ -f "$file" ] && [ ! -L "$file" ] || managed_agent_files_fail 'managed Markdown file must be a regular file'

  local -a names=() starts=() ends=()
  while [ "$#" -gt 0 ]; do
    names+=("$1")
    starts+=("$2")
    ends+=("$3")
    shift 3
  done

  local -a environment=("MAF_COUNT=${#names[@]}")
  local index
  for index in "${!names[@]}"; do
    environment+=(
      "MAF_NAME_$index=${names[$index]}"
      "MAF_START_$index=${starts[$index]}"
      "MAF_END_$index=${ends[$index]}"
    )
  done

  local result
  if ! result="$(env "${environment[@]}" awk '
      BEGIN {
        count = ENVIRON["MAF_COUNT"]
        for (block = 0; block < count; block++) {
          names[block] = ENVIRON["MAF_NAME_" block]
          starts[block] = ENVIRON["MAF_START_" block]
          ends[block] = ENVIRON["MAF_END_" block]
        }
      }
      {
        for (block = 0; block < count; block++) {
          if (index($0, starts[block])) start_any[block]++
          if (index($0, ends[block])) end_any[block]++
          if ($0 == starts[block]) { start_exact[block]++; start_line[block] = NR }
          if ($0 == ends[block]) { end_exact[block]++; end_line[block] = NR }
        }
      }
      END {
        for (block = 0; block < count; block++) {
          if (start_any[block] != start_exact[block] || end_any[block] != end_exact[block]) {
            printf "the managed %s block has altered markers\n", names[block]; exit 1
          }
          if (start_exact[block] > 1 || end_exact[block] > 1) {
            printf "the managed %s block is duplicated\n", names[block]; exit 1
          }
          if (!((start_exact[block] == 0 && end_exact[block] == 0) || (start_exact[block] == 1 && end_exact[block] == 1))) {
            printf "the managed %s block is malformed\n", names[block]; exit 1
          }
          if (start_exact[block] == 1 && start_line[block] >= end_line[block]) {
            printf "%s block end marker precedes its start marker\n", names[block]; exit 1
          }
        }
        for (left = 0; left < count; left++) {
          if (start_exact[left] != 1) continue
          for (right = left + 1; right < count; right++) {
            if (start_exact[right] != 1) continue
            if (!(end_line[left] < start_line[right] || end_line[right] < start_line[left])) {
              printf "managed blocks %s and %s overlap\n", names[left], names[right]; exit 1
            }
          }
        }
      }
    ' "$file")"; then
    managed_agent_files_fail "${result:-managed block validation failed}"
  fi
}

# Removes validated whole blocks while retaining every byte of every remaining
# line, including an unterminated final line. Callers normally use this to
# recover their user-owned portion before composing canonical managed blocks.
managed_agent_files_strip_blocks() {
  local input="$1" output="$2"
  shift 2
  managed_agent_files_validate_blocks "$input" "$@" || return

  local -a starts=() ends=()
  while [ "$#" -gt 0 ]; do
    shift
    starts+=("$1")
    ends+=("$2")
    shift 2
  done

  local line block skipping=0 has_newline managed_marker
  : >"$output"
  while :; do
    if IFS= read -r line; then
      has_newline=1
    else
      [ -n "$line" ] || break
      has_newline=0
    fi
    managed_marker=0
    for block in "${!starts[@]}"; do
      if [ "$line" = "${starts[$block]}" ]; then
        skipping=1
        managed_marker=1
        break
      fi
      if [ "$line" = "${ends[$block]}" ]; then
        skipping=0
        managed_marker=1
        break
      fi
    done
    [ "$managed_marker" -eq 0 ] && [ "$skipping" -eq 0 ] || continue
    printf '%s' "$line" >>"$output"
    [ "$has_newline" -eq 0 ] || printf '\n' >>"$output"
  done <"$input"
}

# Replaces one exact managed block in place, or appends it to an unmanaged
# document. Existing user bytes retain their order and representation; only a
# missing final record separator is added when it is necessary to append a
# line-delimited Markdown block.
managed_agent_files_replace_block() {
  local input="$1" output="$2" start="$3" end="$4" replacement="$5"
  managed_agent_files_validate_blocks "$input" managed "$start" "$end" || return
  managed_agent_files_validate_blocks "$replacement" managed "$start" "$end" || return

  local present
  present="$(grep -cxF "$start" "$input" || true)"
  if [ "$present" -eq 0 ]; then
    cat "$input" >"$output"
    if [ -s "$input" ] && [ "$(tail -c 1 "$input" | wc -l | tr -d '[:space:]')" -eq 0 ]; then
      printf '\n' >>"$output"
    fi
    cat "$replacement" >>"$output"
    return
  fi

  local line skipping=0 has_newline
  : >"$output"
  while :; do
    if IFS= read -r line; then
      has_newline=1
    else
      [ -n "$line" ] || break
      has_newline=0
    fi
    if [ "$line" = "$start" ]; then
      cat "$replacement" >>"$output"
      skipping=1
      continue
    fi
    if [ "$line" = "$end" ]; then
      skipping=0
      continue
    fi
    [ "$skipping" -eq 0 ] || continue
    printf '%s' "$line" >>"$output"
    [ "$has_newline" -eq 0 ] || printf '\n' >>"$output"
  done <"$input"
}

managed_agent_files_trim_blank_edges() {
  local input="$1" output="$2"
  awk '
    NF { started=1 }
    started { lines[++count]=$0; if (NF) last=count }
    END { for (line_number=1; line_number<=last; line_number++) print lines[line_number] }
  ' "$input" >"$output"
}

managed_agent_files_imports_project_file() {
  local input="$1" target="$2" first_line_only="${3:-0}"
  awk -v target="$target" -v first_line_only="$first_line_only" '
    function normalize(path, count, part, top, result, number) {
      sub(/^[[:space:]]*@/, "", path)
      sub(/[[:space:]]+$/, "", path)
      for (number in normalized_parts) delete normalized_parts[number]
      count=split(path, path_parts, "/")
      top=0
      for (number=1; number<=count; number++) {
        part=path_parts[number]
        if (part == "" || part == ".") continue
        if (part == "..") {
          if (top == 0) return path
          delete normalized_parts[top--]
          continue
        }
        normalized_parts[++top]=part
      }
      result=""
      for (number=1; number<=top; number++) result=result (number == 1 ? "" : "/") normalized_parts[number]
      return result
    }
    first_line_only && FNR > 1 { next }
    /^[[:space:]]*```/ || /^[[:space:]]*~~~/ { fenced=!fenced; next }
    !fenced && /^[[:space:]]*@[^[:space:]]+[[:space:]]*$/ && normalize($0) == target { found=1 }
    END { exit(found ? 0 : 1) }
  ' "$input"
}

# Returns bridge state in managed_agent_files_claude_bridge: absent, managed,
# or external. A managed bridge has one canonical prefix and may retain any
# user-owned suffix. An altered or duplicated marker is a blocking conflict.
managed_agent_files_validate_claude_bridge() {
  local file="$1" marker="$2"
  managed_agent_files_claude_bridge="absent"
  [ -e "$file" ] || [ -L "$file" ] || return 0
  managed_agent_files_regular_or_absent "$file" 'CLAUDE.md' || return

  local marker_any marker_exact
  marker_any="$(grep -cF "$marker" "$file" || true)"
  marker_exact="$(grep -cxF "$marker" "$file" || true)"
  [ "$marker_any" = "$marker_exact" ] || { managed_agent_files_fail 'the Claude bridge marker was altered'; return; }
  case "$marker_exact" in
    0) managed_agent_files_claude_bridge="external" ;;
    1)
      [ "$(sed -n '1p' "$file")" = '@AGENTS.md' ] && [ "$(sed -n '2p' "$file")" = "$marker" ] && [ "$(wc -l <"$file" | tr -d '[:space:]')" -ge 3 ] && [ -z "$(sed -n '3p' "$file")" ] || { managed_agent_files_fail 'the managed Claude bridge is malformed'; return; }
      managed_agent_files_claude_bridge="managed"
      ;;
    *) managed_agent_files_fail 'the managed Claude bridge is duplicated' ;;
  esac
}

# Writes the desired Claude file without touching an external existing bridge.
# The canonical bridge imports AGENTS.md exactly once, so callers can reject an
# AGENTS.md -> CLAUDE.md edge before applying it.
managed_agent_files_render_claude_bridge() {
  local source="$1" output="$2" marker="$3" enabled="$4" state="$5"
  local custom="$6"
  case "$state" in
    managed)
      tail -n +4 "$source" >"$custom"
      ;;
    external)
      cp "$source" "$custom"
      ;;
    absent)
      : >"$custom"
      ;;
    *) managed_agent_files_fail 'unknown Claude bridge state'; return ;;
  esac

  if [ "$enabled" -eq 1 ]; then
    if [ "$state" = external ] && managed_agent_files_imports_project_file "$source" AGENTS.md 1; then
      cp "$source" "$output"
    else
      { printf '@AGENTS.md\n%s\n\n' "$marker"; cat "$custom"; } >"$output"
    fi
  elif [ "$state" = managed ]; then
    cp "$custom" "$output"
  elif [ "$state" = external ]; then
    cp "$source" "$output"
  else
    : >"$output"
  fi
}
