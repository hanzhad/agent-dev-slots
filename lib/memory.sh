# shellcheck shell=bash disable=SC2034,SC2154

# acquire waits while the machine's available memory is below a floor. The
# floor is read on every check, from $HOME/.config/machine-limits/memory-floor-mb
# (an integer in MiB; the directory may be shared with other tools). A missing
# or unreadable file means the engine's own default.

MEMORY_FLOOR_DEFAULT_MB=2048
MEMORY_REASON=""
MEMORY_FLOOR=$MEMORY_FLOOR_DEFAULT_MB
MEMORY_FLOOR_NOTE=""
MEMORY_METER_WARNED=0

memory_floor_file() { echo "$HOME/.config/machine-limits/memory-floor-mb"; }

memory_floor_load() {
  local file v=""
  file=$(memory_floor_file)
  MEMORY_FLOOR_NOTE=""
  if [[ -f "$file" ]]; then
    { IFS= read -r v || true; } 2>/dev/null <"$file" || v=""
    v=${v//[[:space:]]/}
    if [[ ! "$v" =~ ^[0-9]+$ ]]; then
      v=""
      MEMORY_FLOOR_NOTE=" (that file is unreadable or not a whole number of MiB, so the default applies)"
    fi
  fi
  MEMORY_FLOOR=${v:-$MEMORY_FLOOR_DEFAULT_MB}
}

memory_available_mb() {
  local path=${DEV_CONTAINER_MEMINFO_PATH:-/proc/meminfo} kb
  kb=$(sed -n 's/^MemAvailable:[[:space:]]*\([0-9][0-9]*\) kB.*/\1/p' "$path" 2>/dev/null | head -1) || kb=""
  [[ -n "$kb" ]] || return 1
  echo $(( kb / 1024 ))
}

# Returns 0 when acquire must wait, with the reason in MEMORY_REASON.
memory_floor_blocks() {
  local avail
  MEMORY_REASON=""
  memory_floor_load
  if ! avail=$(memory_available_mb); then
    if (( ! MEMORY_METER_WARNED )); then
      echo "warning: cannot read available memory from ${DEV_CONTAINER_MEMINFO_PATH:-/proc/meminfo} — the memory floor is not applied" >&2
      MEMORY_METER_WARNED=1
    fi
    return 1
  fi
  (( avail < MEMORY_FLOOR )) || return 1
  MEMORY_REASON="available memory is $avail MiB, below the floor of $MEMORY_FLOOR MiB (set in $(memory_floor_file), default $MEMORY_FLOOR_DEFAULT_MB)$MEMORY_FLOOR_NOTE"
  return 0
}
