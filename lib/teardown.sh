# shellcheck shell=bash disable=SC2034,SC2154

remove_slot_dir() {
  slot_dir_established "$1" || return 1
  rm -rf -- "$SLOT_REAL_DIR"
  local sid; sid=$(slot_id_of "$1")
  spec_reset "$sid"
}

validate_slot_volume() {
  local proj="$1" vol="${2:-}"
  local expected; expected=$(slot_volume_of "$proj")
  [[ -n "$vol" ]] || vol="$expected"
  if [[ "$vol" != "$expected" ]]; then
    echo "refusing to remove volume '$vol': name does not match expected '$expected'." >&2
    return 1
  fi
  if ! docker volume inspect "$vol" >/dev/null 2>&1; then
    return 0
  fi
  local vol_sid vol_proj vol_origin
  vol_sid=$(docker volume inspect "$vol" --format '{{index .Labels "dev-container.sid"}}' 2>/dev/null || true)
  vol_proj=$(docker volume inspect "$vol" --format '{{index .Labels "dev-container.project"}}' 2>/dev/null || true)
  vol_origin=$(docker volume inspect "$vol" --format '{{index .Labels "dev-container.origin"}}' 2>/dev/null || true)
  if [[ "$vol_sid" != "$proj" || "$vol_proj" != "$SLOT_PROJECT_PREFIX" ]]; then
    echo "refusing to remove volume '$vol': labels do not match project '$SLOT_PROJECT_PREFIX' and sid '$proj'." >&2
    return 1
  fi
  if [[ -n "$origin_url" && -n "$vol_origin" && "$vol_origin" != "$origin_url" ]]; then
    echo "refusing to remove volume '$vol': origin '$vol_origin' does not match '$origin_url'." >&2
    return 1
  fi
  return 0
}

remove_slot_volume() {
  local proj="$1" vol="${2:-}"
  validate_slot_volume "$proj" "$vol" || return 1
  local expected; expected=$(slot_volume_of "$proj")
  [[ -n "$vol" ]] || vol="$expected"
  if ! docker volume inspect "$vol" >/dev/null 2>&1; then
    spec_reset "$proj"
    return 0
  fi
  echo "▸ removing volume $vol"
  docker volume rm "$vol" >/dev/null \
    || echo "  ! could not remove volume $vol — a container still holds it" >&2
  spec_reset "$proj"
}

teardown_dir() {
  local dir="$1" proj="${2:-}"
  if [[ $# -eq 1 ]]; then
    proj="$1"
    dir=""
  fi
  local spec; spec=$(spec_of "$proj")
  if [[ -n "$dir" && ( -e "$dir" || -L "$dir" ) ]]; then
    slot_dir_established "$dir" || return 1
    dir=$SLOT_REAL_DIR
  fi
  case "$spec" in
    vol:*)
      validate_slot_volume "$proj" "${spec#vol:}" || return 1
      ;;
    *)
      if [[ -z "$spec" && -z "$dir" ]]; then
        local vol; vol=$(slot_volume_of "$proj")
        if docker volume inspect "$vol" >/dev/null 2>&1; then
          validate_slot_volume "$proj" "$vol" || return 1
        fi
      fi
      ;;
  esac
  if [[ -f "$(compose_file_of)" ]]; then
    echo "▸ tearing down compose project '$proj'"
    slot_compose "$proj" down -v --rmi local || true
  fi
  remove_slot_images "$proj" || true

  local spec; spec=$(spec_of "$proj")
  case "$spec" in
    vol:*)
      remove_slot_volume "$proj" "${spec#vol:}" || return 1
      ;;
    dir:*)
      if [[ -e "${spec#dir:}" ]]; then
        echo "▸ removing ${spec#dir:}"
        remove_slot_dir "${spec#dir:}" || return 1
      fi
      ;;
    *)
      if [[ -n "$dir" && -e "$dir" ]]; then
        echo "▸ removing $dir"
        remove_slot_dir "$dir" || return 1
      else
        local vol; vol=$(slot_volume_of "$proj")
        if docker volume inspect "$vol" >/dev/null 2>&1; then
          remove_slot_volume "$proj" "$vol" || return 1
        fi
      fi
      ;;
  esac

  rm -f "$(slot_env_file_of "$proj")" "$STATE/ports/$proj.ports" "$(slot_overlay_of "$proj")" "$(lease_token_file_of "$proj")"
  rm -f "$(allow_hosts_file_of "$proj")" "$(allow_hosts_base_file_of "$proj")" "$STATE/repos/$proj.json"
  toolbox_clear "$proj"
  spec_reset "$proj"
  facts_reset "$proj"
  activity_clear "$proj"
}

confirm_slot_absent() {
  local dir="${1:-}" sid="$2"
  local vol sname
  local -a remaining=()
  spec_reset "$sid"
  vol=$(slot_volume_of "$sid")
  sname=$(slot_name_of "$sid")

  if [[ -n "$dir" && ( -e "$dir" || -L "$dir" ) ]]; then
    remaining+=("directory $dir")
  elif [[ -n "${slots_dir:-}" && -n "$sname" && ( -e "$slots_dir/$sname" || -L "$slots_dir/$sname" ) ]]; then
    remaining+=("directory $slots_dir/$sname")
  fi
  if docker volume inspect "$vol" >/dev/null 2>&1; then
    remaining+=("volume $vol")
  fi
  if compose_running "$dir" "$sid"; then
    remaining+=("running compose project $sid")
  fi
  if (( ${#remaining[@]} > 0 )); then
    local IFS=", "
    printf '%s\n' "${remaining[*]}"
    return 1
  fi
  return 0
}

abort_acquire_slot() {
  local dir="${1:-}" sid="${2:-}"
  [[ -n "$sid" ]] || sid=$(basename "${RESERVED_LEASE:-}" .json)
  if [[ -z "$sid" ]]; then
    drop_reservation
    return 0
  fi
  if [[ -z "${RESERVED_LEASE:-}" && -f "$(lease_file_of "$sid")" && -n "$(lease_field "$(lease_file_of "$sid")" setup)" ]]; then
    RESERVED_LEASE="$(lease_file_of "$sid")"
  fi

  spec_reset "$sid"

  local vol sname cids cid cleanup_failed=0
  vol=$(slot_volume_of "$sid")
  sname=$(slot_name_of "$sid")

  if [[ -n "$dir" && ( -e "$dir" || -L "$dir" ) ]]; then
    if slot_dir_established "$dir"; then
      dir=$SLOT_REAL_DIR
    else
      cleanup_failed=1
    fi
  elif [[ -n "${slots_dir:-}" && -n "$sname" && ( -e "$slots_dir/$sname" || -L "$slots_dir/$sname" ) ]]; then
    if slot_dir_established "$slots_dir/$sname"; then
      dir=$SLOT_REAL_DIR
    else
      cleanup_failed=1
    fi
  fi

  if (( ! cleanup_failed )) && docker volume inspect "$vol" >/dev/null 2>&1; then
    validate_slot_volume "$sid" "$vol" || cleanup_failed=1
  fi

  if (( ! cleanup_failed )); then
    local -a rm_ids=()
    if cids=$(docker ps -a -q --filter "label=com.docker.compose.project=$sid" 2>/dev/null); then
      for cid in $cids; do
        [[ -n "$cid" ]] && rm_ids+=("$cid")
      done
    else
      cleanup_failed=1
    fi
    if cids=$(docker ps -a -q --filter "volume=$vol" 2>/dev/null); then
      for cid in $cids; do
        [[ -n "$cid" ]] && rm_ids+=("$cid")
      done
    else
      cleanup_failed=1
    fi
    if (( ${#rm_ids[@]} > 0 )); then
      docker rm -f "${rm_ids[@]}" >/dev/null 2>&1 || cleanup_failed=1
    fi

    if [[ -f "$(compose_file_of)" ]]; then
      slot_compose "$dir" "$sid" down -v --remove-orphans --rmi local || true
    fi

    teardown_dir "$dir" "$sid" || cleanup_failed=1
  fi

  clear_slot_allow_hosts "$sid"
  rm -f "$(lease_token_file_of "$sid")" "$(allow_hosts_file_of "$sid")" "$(allow_hosts_base_file_of "$sid")"
  rm -f "$STATE/keys/${sid}-"* 2>/dev/null || true
  toolbox_clear "$sid"
  spec_reset "$sid"
  facts_reset "$sid"
  activity_clear "$sid"

  local remaining=""
  if ! remaining=$(confirm_slot_absent "$dir" "$sid"); then
    cleanup_failed=1
  fi

  if (( cleanup_failed )); then
    local res_path="${RESERVED_LEASE:-$(lease_file_of "$sid")}"
    preserve_failed_reservation "$sid"
    echo "  ! could not fully roll back '$sid' (${remaining:-container or volume teardown failed}) — keeping reservation $res_path" >&2
    return 1
  fi

  rm -f "$(slot_env_file_of "$sid")" "$STATE/ports/$sid.ports" "$(slot_overlay_of "$sid")" "$STATE/repos/$sid.json" "$STATE/seen/$sid"
  drop_reservation
}
