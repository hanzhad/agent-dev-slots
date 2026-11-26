# shellcheck shell=bash disable=SC2034,SC2154

fs_refuse() {
  jq -nc --arg err "$1" '{ok: false, error: $err}'
  exit 1
}

cmd_fs() {
  local op="${1:-}" slot="${2:-}" flag="${3:-}"
  [[ $# -eq 3 && "$flag" == "--stdin-json" ]] \
    || fs_refuse "fs requires: $CALLER fs <read|grep|glob|diff|write|edit> <slot> --stdin-json"
  case "$op" in
    read|grep|glob|diff|write|edit) ;;
    *) fs_refuse "unsupported fs operation: $op" ;;
  esac

  local name; name=$(slot_name_of "$slot")
  [[ -n "$name" && "$name" != -* && "$name" =~ ^[a-zA-Z0-9_-]+$ ]] \
    || fs_refuse "invalid slot name: $slot"

  local sid
  sid=$(slot_id_of "$name")

  idle_limit_load
  slot_exists "$sid" || fs_refuse "slot '$name' does not exist"
  slot_is_ours "$sid" || fs_refuse "'$name' is not this project's own slot"
  sid_is_this_projects "$sid" || fs_refuse "'$name' is not this project's own slot"

  local spec
  spec=$(spec_of "$sid")
  [[ "$spec" == vol:* ]] || fs_refuse "fs requires a volume-backed slot: $sid"

  local lease
  lease=$(lease_file_of "$sid")
  [[ -f "$lease" ]] || fs_refuse "'$sid' is not leased"
  [[ -z "$(lease_field "$lease" setup)" ]] || fs_refuse "'$sid' is still being created"
  lease_is_live "$lease" || fs_refuse "lease on '$sid' is not live"

  local raw_json
  raw_json=$(head -c 1048577)
  (( ${#raw_json} <= 1048576 )) || fs_refuse "request payload exceeds maximum size"
  printf '%s' "$raw_json" | jq -e 'type == "object"' >/dev/null 2>&1 \
    || fs_refuse "stdin must be a valid JSON object"

  if [[ "$op" == write || "$op" == edit ]]; then
    local tok token_file got_sha want_sha
    tok=$(printf '%s' "$raw_json" | jq -r 'if (.lease_token | type) == "string" then .lease_token else "" end' 2>/dev/null) || tok=""
    [[ "$tok" =~ ^[0-9a-f]{64}$ ]] || fs_refuse "write/edit requires a valid 64-hex lease_token"
    token_file=$(lease_token_file_of "$sid")
    [[ -f "$token_file" ]] || fs_refuse "lease token file missing for '$sid'"
    want_sha=$(lease_field "$lease" lease_token_sha256 2>/dev/null) || want_sha=""
    [[ "$want_sha" =~ ^[0-9a-f]{64}$ ]] || fs_refuse "lease for '$sid' has no lease_token_sha256"
    got_sha=$(printf '%s' "$tok" | adopt_sha256)
    [[ "$got_sha" == "$want_sha" ]] || fs_refuse "invalid lease_token for '$sid'"
    tok=""
  fi

  local clean_json
  clean_json=$(printf '%s' "$raw_json" | jq -c 'del(.lease_token)' 2>/dev/null) \
    || fs_refuse "failed to sanitize request JSON"
  raw_json=""

  if [[ "$op" == diff ]] && printf '%s' "$clean_json" | jq -e 'has("subdir") and .subdir != null' >/dev/null 2>&1; then
    local sub_val
    sub_val=$(printf '%s' "$clean_json" | jq -r '
      if (.subdir | type) != "string" or (.subdir | length) == 0 then
        error("subdir must be a non-empty relative string")
      elif (.subdir | explode | any(. == 0)) then
        error("NUL byte refused in subdir")
      elif (.subdir | contains("\\")) then
        error("backslash refused in subdir")
      elif (.subdir | startswith("/") or endswith("/")) then
        error("subdir must be a relative path without leading or trailing /")
      elif (.subdir | split("/") | any(. == "" or . == "." or . == ".." or . == ".git")) then
        error("invalid path segment in subdir")
      else
        .subdir
      end
    ' 2>/dev/null) || fs_refuse "invalid subdir parameter"

    local state_repos_file state_json="[]" cat_json="[]" allowed_repos_json resolved_subdir=""
    state_repos_file=$(slot_repos_state_file_of "$sid" 2>/dev/null || true)
    [[ -n "$state_repos_file" && -f "$state_repos_file" ]] \
      && state_json=$(jq -c 'if type == "array" then . else [] end' "$state_repos_file" 2>/dev/null || echo "[]")
    if [[ -n "${SLOT_REPOS_CATALOG:-}" ]]; then
      cat_json=$(printf '%s' "$SLOT_REPOS_CATALOG" | jq -c '[.repos[] | select(.attach == "default" or .legacy_docs == true)]' 2>/dev/null || echo "[]")
    elif declare -F docs_repo_configured >/dev/null 2>&1 && docs_repo_configured; then
      cat_json='[{"alias":"docs","path":"docs/repo"},{"alias":"docs-fix","path":"docs/fix"}]'
    fi
    allowed_repos_json=$(jq -nc --argjson a "$state_json" --argjson b "$cat_json" '$a + $b')
    resolved_subdir=$(printf '%s' "$allowed_repos_json" | jq -r --arg s "$sub_val" '[.[] | select(.alias == $s or .path == $s) | .path] | first // empty' 2>/dev/null || true)
    [[ -n "$resolved_subdir" ]] || fs_refuse "unknown or unattached subdir: $sub_val"
    clean_json=$(printf '%s' "$clean_json" | jq -c --arg s "$resolved_subdir" '.subdir = $s' 2>/dev/null) \
      || fs_refuse "failed to normalize subdir"
  fi

  local ro_prefixes=""
  ro_prefixes=$({
    if [[ -n "${SLOT_REPOS_CATALOG:-}" ]]; then
      printf '%s' "$SLOT_REPOS_CATALOG" | jq -r '.repos[] | select(.mode == "ro") | .path' 2>/dev/null || true
    fi
    local sr_file; sr_file=$(slot_repos_state_file_of "$sid" 2>/dev/null || true)
    if [[ -n "$sr_file" && -f "$sr_file" ]]; then
      jq -r '.[]? | select(.mode == "ro") | .path' "$sr_file" 2>/dev/null || true
    fi
    if declare -F docs_repo_configured >/dev/null 2>&1 && docs_repo_configured; then
      printf 'docs/repo\n'
    fi
  } | sort -u | tr '\n' ' ')

  activity_stamp "$sid" run
  local perl_src rc=0
  perl_src=$(<"$lib_dir/slot-fs.pl")
  printf '%s' "$clean_json" \
    | slot_helper_run "$sid" perl -e "$perl_src" -- "$op" "$COMPOSE_DIR" "$SLOT_COPY_FILES" "$ro_prefixes" "${SLOT_REPOS_FILE:-}" \
    || rc=$?
  activity_stamp "$sid" run
  return "$rc"
}
