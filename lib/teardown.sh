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

  rm -f "$(slot_env_file_of "$proj")" "$STATE/ports/$proj.ports" "$(slot_overlay_of "$proj")"
  rm -f "$(allow_hosts_file_of "$proj")" "$(allow_hosts_base_file_of "$proj")"
  spec_reset "$proj"
  facts_reset "$proj"
  activity_clear "$proj"
}
