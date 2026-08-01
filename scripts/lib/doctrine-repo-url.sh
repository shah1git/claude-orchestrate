#!/usr/bin/env bash
# managed-by: rikanv-doctrine

# Canonical comparison key for doctrine Git origins. The caller receives the
# value in normalized_repo_url to keep compatibility with the sync/subscribe
# trust interface.
normalize_repo_url() {
  local value="$1"
  case "$value" in
    git@*:* ) value="${value#git@}"; value="${value/:/\/}" ;;
    ssh://git@* ) value="${value#ssh://git@}" ;;
    https://* ) value="${value#https://}" ;;
    http://* ) value="http/${value#http://}" ;;
    file://* ) value="${value#file://}" ;;
  esac
  if [[ "$value" == /* ]] && [ -d "$value" ]; then value="$(cd "$value" && pwd -P)"; fi
  value="${value%/}"
  value="${value%.git}"
  normalized_repo_url="$value"
}
