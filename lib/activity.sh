# shellcheck shell=bash disable=SC2034,SC2154

# A lease has a holder and no term. It is live while its slot is active: a
# `run` within the idle limit, or container CPU above the threshold within it.
# The idle limit is one value for the machine (idle_limit, in seconds, in the
# machine limits file); projects and slots cannot set it. What counts as
# activity is kept as two stamps per slot under $STATE/activity, so whatever
# later decides what to do with an idle slot reads the same data.

IDLE_LIMIT_DEFAULT=7200
IDLE_LIMIT=$IDLE_LIMIT_DEFAULT
IDLE_LIMIT_WARNED=0
CPU_ACTIVE_PERCENT=${DEV_CONTAINER_CPU_ACTIVE_PERCENT:-5}

idle_limit_load() {
  local limits="$STATE/limits.json" raw=""
  IDLE_LIMIT=$IDLE_LIMIT_DEFAULT
  [[ -f "$limits" ]] || return 0
  raw=$(jq -r 'if (type == "object") and has("idle_limit") then (.idle_limit | if type == "number" then tostring else "not-a-number" end) else "" end' "$limits" 2>/dev/null) || raw=""
  [[ -n "$raw" ]] || return 0
  if [[ "$raw" =~ ^[0-9]+$ ]] && (( raw >= 1 )); then
    IDLE_LIMIT=$raw
  elif (( ! IDLE_LIMIT_WARNED )); then
    echo "warning: idle_limit in $limits is not a whole number of seconds, at least 1 — using the default $IDLE_LIMIT_DEFAULT" >&2
    IDLE_LIMIT_WARNED=1
  fi
  return 0
}

activity_file_of() { echo "$STATE/activity/$1.$2"; }

activity_stamp() {
  local file; file=$(activity_file_of "$1" "$2")
  mkdir -p "$STATE/activity"
  now_epoch >"$file.tmp.$$"
  mv "$file.tmp.$$" "$file"
}

activity_epoch() {
  local v
  v=$(cat "$(activity_file_of "$1" "$2")" 2>/dev/null) || v=0
  [[ "$v" =~ ^[0-9]+$ ]] || v=0
  echo "$v"
}

activity_clear() { rm -f "$(activity_file_of "$1" run)" "$(activity_file_of "$1" cpu)"; }

stats_show_cpu_above() {
  awk -v prefix="$1-" -v limit="$CPU_ACTIVE_PERCENT" '
    index($1, prefix) == 1 { v = $2; sub(/%/, "", v); if (v + 0 > limit) found = 1 }
    END { exit !found }'
}

# One `docker stats` for the machine, then a cpu stamp for every lease whose
# containers (named <lease>-<service>-<n>) are above the threshold right now.
activity_refresh() {
  idle_limit_load
  local stats f sid
  stats=$(docker stats --no-stream --format '{{.Name}} {{.CPUPerc}}' 2>/dev/null) || return 0
  [[ -n "$stats" ]] || return 0
  for f in "$STATE"/leases/*.json; do
    [[ -f "$f" ]] || continue
    sid=$(basename "$f" .json)
    if printf '%s\n' "$stats" | stats_show_cpu_above "$sid"; then
      activity_stamp "$sid" cpu
    fi
  done
  return 0
}

lease_last_activity() {
  local file="$1" sid since best run cpu
  sid=$(basename "$file" .json)
  since=$(lease_field "$file" since)
  [[ "$since" =~ ^[0-9]+$ ]] || since=0
  best=$since
  run=$(activity_epoch "$sid" run)
  cpu=$(activity_epoch "$sid" cpu)
  (( run > best )) && best=$run
  (( cpu > best )) && best=$cpu
  echo "$best"
}

lease_is_live() {
  local file="$1" pid boot current_boot
  [[ -f "$file" ]] || return 1
  if [[ -n "$(lease_field "$file" setup)" ]]; then
    boot=$(lease_field "$file" boot_id)
    current_boot=${BOOT_ID:-$(boot_id_current)}
    [[ -z "$boot" || -z "$current_boot" || "$boot" == "$current_boot" ]] || return 1
    pid=$(lease_field "$file" pid)
    [[ -z "$pid" ]] || kill -0 "$pid" 2>/dev/null
    return
  fi
  (( $(now_epoch) - $(lease_last_activity "$file") < IDLE_LIMIT ))
}

fmt_idle_for() {
  local idle=$(( $(now_epoch) - $(lease_last_activity "$1") ))
  (( idle < 0 )) && idle=0
  echo "$(fmt_duration "$idle") ago"
}
