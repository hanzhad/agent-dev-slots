# shellcheck shell=bash disable=SC2034,SC2154

# Pure Core: parse "<port>" -> "127.0.0.1 <port>" or "<svc>:<port>" -> "<svc> <port>".
expose_parse_target() {
  local raw="${1:-}" host="127.0.0.1" port=""
  [[ -n "$raw" ]] || { echo "expose: target cannot be empty" >&2; return 1; }
  if [[ "$raw" == *:* ]]; then
    host=${raw%%:*}
    port=${raw#*:}
    [[ "$port" != *:* ]] || { echo "expose: invalid target '$raw'" >&2; return 1; }
    [[ "$host" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]] || {
      echo "expose: invalid service name '$host' in '$raw'" >&2
      return 1
    }
  else
    port="$raw"
  fi
  if [[ ! "$port" =~ ^[1-9][0-9]*$ ]] || (( port < 1 || port > 65535 )); then
    echo "expose: invalid port '$port' in '$raw' (expected 1..65535)" >&2
    return 1
  fi
  printf '%s %s\n' "$host" "$port"
}

# Pure Core: list candidate host ports from unused offsets in slot-<idx>'s block.
expose_candidate_ports() {
  local idx="$1"
  local port_base="${2:-${SLOT_PORT_BASE:-42000}}"
  local port_block="${3:-${SLOT_PORT_BLOCK:-16}}"
  local used_spec="${4:-${SLOT_PORT_NAMES:-SSH_PORT NOVNC_PORT VITE_DEV_PORT}}"
  local forbidden="${5:-${SLOT_FORBIDDEN_PORTS:-}}"
  [[ "$idx" =~ ^[1-9][0-9]*$ ]] || return 1

  local used_count=0
  if [[ "$used_spec" =~ ^[0-9]+$ ]]; then
    used_count=$used_spec
  else
    local -a names=()
    read -ra names <<<"$used_spec"
    used_count=${#names[@]}
  fi

  local -a forb_list=()
  read -ra forb_list <<<"$forbidden"
  local k p f is_forb found=0
  for (( k = used_count; k < port_block; k++ )); do
    p=$(( port_base + idx * port_block + k ))
    is_forb=0
    for f in ${forb_list[@]+"${forb_list[@]}"}; do
      if [[ "$p" == "$f" ]]; then
        is_forb=1
        break
      fi
    done
    (( is_forb )) && continue
    printf '%s\n' "$p"
    found=1
  done
  (( found ))
}

cmd_expose_plan() {
  local name="" target="" for_holder="" have_for=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --for)
        shift
        [[ $# -gt 0 && -n "${1:-}" ]] || die "expose-plan: --for needs a value"
        for_holder="$1"
        have_for=1
        ;;
      -*)
        die "expose-plan: unknown flag '$1'"
        ;;
      *)
        if [[ -z "$name" ]]; then
          name="$1"
        elif [[ -z "$target" ]]; then
          target="$1"
        else
          die "expose-plan: unexpected argument '$1'"
        fi
        ;;
    esac
    shift
  done

  [[ -n "$name" && -n "$target" ]] || die "expose-plan requires: $CALLER expose-plan <slot> <[svc:]port> [--for <holder>]"

  local parsed_target target_host target_port
  parsed_target=$(expose_parse_target "$target") || die "expose-plan: invalid target '$target' — expected <port> or <service>:<port>"
  read -r target_host target_port <<<"$parsed_target"

  valid_name "$name"
  name=$(slot_name_of "$name")

  local sid
  sid=$(slot_id_of "$name")

  idle_limit_load
  slot_exists "$sid" || die "no slot '$name' — 'expose-plan' only acts on a slot that already exists."
  slot_is_ours "$sid" || die "'$name': not this project's own slot."
  sid_is_this_projects "$sid" || die "'$name': not this project's own slot."

  local lease
  lease=$(lease_file_of "$sid")
  if [[ ! -f "$lease" ]]; then
    if [[ -f "$(parked_file_of "$sid")" ]]; then
      die "'$sid' is parked, not leased — 'expose-plan' must not touch a slot nobody holds."
    fi
    die "'$sid' is not leased — 'expose-plan' must not touch a slot nobody holds."
  fi
  [[ -z "$(lease_field "$lease" setup)" ]] || die "'$sid' is still being created."
  lease_is_live "$lease" || die "lease on '$sid' is not live."

  if (( have_for )); then
    local holder
    holder=$(lease_field "$lease" holder)
    [[ "$for_holder" == "$holder" ]] || die "refusing expose-plan on '$sid' — it is leased to '$holder', not '$for_holder'."
  fi

  local ssh_port key idx
  ssh_port=$(lease_field "$lease" ssh_port)
  [[ "$ssh_port" =~ ^[1-9][0-9]*$ ]] || ssh_port=$(slot_port "$sid" "$PRIMARY_PORT")
  [[ "$ssh_port" =~ ^[1-9][0-9]*$ ]] || die "'$sid' has no SSH port recorded."
  key=$(lease_field "$lease" key)
  [[ -n "$key" ]] || die "'$sid' has no SSH key recorded."
  idx=$(slot_index_of "$name") || die "'$name' is not a numbered slot (slot-<n>)."

  local extra_forbidden="$SLOT_FORBIDDEN_PORTS" ports_file pf_k pf_v
  ports_file=$(slot_ports_file "$sid")
  if [[ -f "$ports_file" ]]; then
    while IFS='=' read -r pf_k pf_v || [[ -n "$pf_k" ]]; do
      [[ "$pf_v" =~ ^[1-9][0-9]*$ ]] && extra_forbidden="${extra_forbidden:+$extra_forbidden }$pf_v"
    done <"$ports_file"
  fi

  local candidates
  candidates=$(expose_candidate_ports "$idx" "$SLOT_PORT_BASE" "$SLOT_PORT_BLOCK" "$SLOT_PORT_NAMES" "$extra_forbidden")     || die "no free expose ports remain in slot '$name' block (all offsets used or forbidden)."

  activity_stamp "$sid" run

  printf 'ssh_port=%s\n' "$ssh_port"
  printf 'key=%s\n' "$key"
  printf 'target_host=%s\n' "$target_host"
  printf 'target_port=%s\n' "$target_port"
  local hp
  while IFS= read -r hp; do
    [[ -n "$hp" ]] && printf 'host_port=%s\n' "$hp"
  done <<<"$candidates"
}
