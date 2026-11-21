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

  local name="${slot#"${SLOT_PROJECT_PREFIX}-"}"
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

  activity_stamp "$sid" run
  local perl_src rc=0
  perl_src=$(<"$lib_dir/slot-fs.pl")
  printf '%s' "$clean_json" \
    | slot_helper_run "$sid" perl -e "$perl_src" -- "$op" "$COMPOSE_DIR" "$SLOT_COPY_FILES" \
    || rc=$?
  activity_stamp "$sid" run
  return "$rc"
}
