# shellcheck shell=bash disable=SC2034,SC2154

slot_port_clash() {
  local dir="$1" mine other port name other_name other_port
  local ports; ports=$(slot_ports_file "$dir")
  [[ -f "$ports" ]] || return 1
  for name in $SLOT_PORT_NAMES; do
    port=$(env_value "$ports" "$name")
    [[ -n "$port" ]] || continue
    for other in "$STATE"/ports/*.ports; do
      [[ -f "$other" ]] || continue
      [[ "$other" == "$ports" ]] && continue
      while IFS== read -r other_name other_port || [[ -n "$other_name" ]]; do
        if [[ "$other_port" == "$port" ]]; then
          mine=$(basename "$other" .ports)
          PORT_CLASH_REASON="$name $port is also ${mine#"${SLOT_PROJECT_PREFIX}-"}'s"
          [[ "$other_name" == "$name" ]] || PORT_CLASH_REASON+=" $other_name"
          return 0
        fi
      done <"$other"
    done
  done
  return 1
}

slot_ports_file() { echo "$STATE/ports/$(slot_id_of "$1").ports"; }

slot_port() { env_value "$(slot_ports_file "$1")" "$2"; }

ports_summary() {
  local dir="$1" name out=""
  for name in $SLOT_PORT_NAMES; do
    out+="${out:+, }$name $(slot_port "$dir" "$name")"
  done
  echo "$out"
}

SLOT_FORBIDDEN_PORTS=""
[[ -z "${PE_SLOT_FORBIDDEN_PORTS+x}" ]] || SLOT_FORBIDDEN_PORTS=$PE_SLOT_FORBIDDEN_PORTS

port_is_forbidden() {
  local p="$1" f
  for f in $SLOT_FORBIDDEN_PORTS; do [[ "$p" == "$f" ]] && return 0; done
  return 1
}

allocated_port() {
  local dir="$1" want="$2" number index=0 name
  number=$(slot_index_of "$dir") \
    || die "$(basename "$dir") is not a numbered slot (slot-<n>) — a slot's ports come from its number."
  for name in $SLOT_PORT_NAMES; do
    if [[ "$name" == "$want" ]]; then
      echo $(( SLOT_PORT_BASE + number * SLOT_PORT_BLOCK + index ))
      return
    fi
    index=$((index + 1))
  done
  die "SLOT_PORTS does not name $want"
}

assert_port_value() {
  local where="$1" name="$2" value="$3"
  [[ -n "$value" ]] || die "$where: the $name port came out empty — refusing to run compose or record it. Compose would fall back to the owner's dev container ports."
  if port_is_forbidden "$value"; then
    die "$where: $name port $value is the owner's own dev container's. No slot allocation produces it, so something upstream lost the value."
  fi
}

assert_slot_ports() {
  local where="$1" dir="$2" name
  for name in $SLOT_PORT_NAMES; do
    assert_port_value "$where" "$name" "$(slot_port "$dir" "$name")"
  done
}

record_slot_ports() {
  local dir="$1" record="$2" line
  while IFS= read -r line; do
    [[ -n "$line" ]] || continue
    assert_port_value "recording this slot's ports" "${line%%=*}" "${line#*=}"
  done <<<"$record"
  printf '%s' "$record" >"$(slot_ports_file "$dir")"
}

resolve_slot_ports() {
  local dir="$1" check_busy="${2:-}" file name value record="" fresh=0 changed=0
  file=$(slot_ports_file "$dir")
  [[ -f "$file" ]] || fresh=1
  for name in $SLOT_PORT_NAMES; do
    value=$(env_value "$file" "$name")
    if [[ -z "$value" ]] || port_is_forbidden "$value"; then
      value=$(allocated_port "$dir" "$name") || return 1
      if [[ -n "$check_busy" ]] && port_in_use "$value"; then
        die "$name port $value, the one $(basename "$dir")'s number gives it, is already in use on this host — stop what holds it and run this again."
      fi
      (( fresh )) || echo "▸ this slot's recorded $name was missing or the owner's own — assigned $value"
      changed=1
    fi
    record+="$name=$value"$'\n'
  done
  if (( changed )); then
    record_slot_ports "$dir" "$record"
  fi
  if (( fresh )); then
    echo "▸ allocated this slot's ports: $(ports_summary "$dir")"
  elif (( ! changed )); then
    echo "▸ reusing the ports recorded for this slot: $(ports_summary "$dir")"
  fi
}

slot_block_has_forbidden_port() {
  local number="$1" index=0 name
  for name in $SLOT_PORT_NAMES; do
    ! port_is_forbidden $(( SLOT_PORT_BASE + number * SLOT_PORT_BLOCK + index )) || return 0
    index=$((index + 1))
  done
  return 1
}

port_in_use() {
  local port="$1"
  docker ps --format '{{.Ports}}' 2>/dev/null | grep -q ":${port}->" && return 0
  return 1
}
