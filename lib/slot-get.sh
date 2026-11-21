# shellcheck shell=bash disable=SC2034,SC2154

GET_MAX_BYTES_DEFAULT=52428800

cmd_get() {
  local slot="" rel_src="" stdout_mode=0 max_bytes="$GET_MAX_BYTES_DEFAULT"
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --stdout)
        stdout_mode=1
        ;;
      --max-bytes)
        shift
        [[ $# -gt 0 ]] || die "get: --max-bytes needs a value"
        max_bytes="$1"
        ;;
      --max-bytes=*)
        max_bytes="${1#--max-bytes=}"
        ;;
      -*)
        die "get: unknown flag '$1'"
        ;;
      *)
        if [[ -z "$slot" ]]; then
          slot="$1"
        elif [[ -z "$rel_src" ]]; then
          rel_src="$1"
        else
          die "get: unexpected argument '$1'"
        fi
        ;;
    esac
    shift
  done

  (( stdout_mode )) || die "get requires --stdout in the engine: $CALLER get <slot> --stdout <rel-src> [--max-bytes <N>]"
  [[ -n "$slot" && -n "$rel_src" ]] || die "get requires: $CALLER get <slot> --stdout <rel-src> [--max-bytes <N>]"
  [[ "$max_bytes" =~ ^[1-9][0-9]*$ ]] || die "get: --max-bytes must be a positive integer, got '$max_bytes'"

  local name="${slot#"${SLOT_PROJECT_PREFIX}-"}"
  valid_name "$name"

  local sid
  sid=$(slot_id_of "$name")

  idle_limit_load
  slot_exists "$sid" || die "no slot '$name' — 'get' only acts on a slot that already exists."
  slot_is_ours "$sid" || die "'$name': not this project's own slot."
  sid_is_this_projects "$sid" || die "'$name': not this project's own slot."

  local lease
  lease=$(lease_file_of "$sid")
  if [[ ! -f "$lease" ]]; then
    if [[ -f "$(parked_file_of "$sid")" ]]; then
      die "'$sid' is parked, not leased — 'get' must not touch a slot nobody holds."
    fi
    die "'$sid' is not leased — 'get' must not touch a slot nobody holds."
  fi
  [[ -z "$(lease_field "$lease" setup)" ]] || die "'$sid' is still being created."
  lease_is_live "$lease" || die "lease on '$sid' is not live."

  local req_json
  req_json=$(jq -nc --arg path "$rel_src" --argjson max_bytes "$max_bytes" '{path: $path, max_bytes: $max_bytes}')

  local perl_src rc=0 spec
  perl_src=$(<"$lib_dir/slot-fs.pl")
  spec=$(spec_of "$sid")

  activity_stamp "$sid" run
  if [[ "$spec" == dir:* ]]; then
    local dir="${spec#dir:}"
    [[ -n "$dir" && -d "$dir" ]] || dir="$slots_dir/$name"
    [[ -d "$dir" ]] || die "slot directory missing for '$sid': $dir"
    ( cd "$dir" && printf '%s' "$req_json" | perl -e "$perl_src" -- get_raw "$COMPOSE_DIR" "$SLOT_COPY_FILES" ) || rc=$?
  else
    printf '%s' "$req_json" | slot_helper_run "$sid" perl -e "$perl_src" -- get_raw "$COMPOSE_DIR" "$SLOT_COPY_FILES" || rc=$?
  fi
  activity_stamp "$sid" run
  return "$rc"
}
