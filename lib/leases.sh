# shellcheck shell=bash disable=SC2034,SC2154

LOCK_HELD=0
LOCK_WAIT=0
WAIT_DEADLINE=0
WAIT_TIMEOUT=""
WAIT_NOTE=""
wait_deadline_check() {
  (( WAIT_DEADLINE == 0 || $(now_epoch) < WAIT_DEADLINE )) && return 0
  echo "no slot became free within $WAIT_TIMEOUT$WAIT_NOTE" >&2
  exit 3
}
REAP_LOCK_WAIT=${DEV_CONTAINER_REAP_LOCK_WAIT:-300}
REAP_LOCK_DEADLINE=0
wait_interval_us() {
  local raw="$1" whole frac
  if [[ "$raw" =~ ^([0-9]+)([.]([0-9]+))?$ ]]; then
    whole="${BASH_REMATCH[1]}"
    frac="${BASH_REMATCH[3]:-}"
  elif [[ "$raw" =~ ^[.]([0-9]+)$ ]]; then
    whole="0"
    frac="${BASH_REMATCH[1]}"
  else
    return 1
  fi
  [[ "$whole$frac" == *[1-9]* ]] || return 1
  whole="${whole#"${whole%%[!0]*}"}"
  [[ -n "$whole" ]] || whole="0"
  (( ${#whole} <= 9 )) || return 1
  frac="${frac}000000"
  frac="${frac:0:6}"
  local us=$(( 10#$whole * 1000000 + 10#$frac ))
  (( us > 0 )) || us=1
  printf '%s\n' "$us"
}
wait_interval_spec() {
  local raw="${DEV_CONTAINER_WAIT_INTERVAL:-1}" us=""
  if us=$(wait_interval_us "$raw"); then
    printf '%s %s\n' "$raw" "$us"
  else
    printf '1 1000000\n'
  fi
}
lock_take() { ( set -C; : >"$1" ) 2>/dev/null; }
lock_acquire() {
  if lock_take "$LOCK_DIR"; then
    LOCK_HELD=1
    return 0
  fi
  local interval interval_us
  read -r interval interval_us <<<"$(wait_interval_spec)"
  local start_sec=$SECONDS slept_us=0 wall_us=0 elapsed_us=0 next_report_us=30000000
  while :; do
    wall_us=$(( (SECONDS - start_sec) * 1000000 ))
    elapsed_us=$(( slept_us > wall_us ? slept_us : wall_us ))
    (( LOCK_WAIT )) && wait_deadline_check
    if (( REAP_LOCK_DEADLINE )); then
      (( $(now_epoch) < REAP_LOCK_DEADLINE )) \
        || die "✖ reap could not get the host-wide lock at $LOCK_DIR within ${REAP_LOCK_WAIT}s and stopped here — every slot it had not reached was not looked at. 'ps' shows which dev-container holds it."
    elif (( elapsed_us >= 30000000 )); then
      (( LOCK_WAIT )) || die "the host-wide lock at $LOCK_DIR was busy for 30s — another dev-container is holding it. 'acquire --wait' waits it out; if nothing else is running, a stuck previous run left it: remove it by hand (rm -rf $LOCK_DIR)."
      if (( elapsed_us >= next_report_us )); then
        echo "▸ the host-wide lock at $LOCK_DIR has been busy for $(( elapsed_us / 1000000 ))s — still waiting (--wait)" >&2
        while (( next_report_us <= elapsed_us )); do
          next_report_us=$(( next_report_us + 60000000 ))
        done
      fi
    fi
    sleep "$interval"
    slept_us=$(( slept_us + interval_us ))
    lock_take "$LOCK_DIR" && break
  done
  LOCK_HELD=1
}
lock_release() {
  rm -f "$LOCK_DIR"
  # shellcheck disable=SC2034
  LOCK_HELD=0
}
RESERVED_LEASE=""

lease_file_of() { echo "$STATE/leases/$1.json"; }
lease_token_file_of() { echo "$STATE/lease-tokens/$1.token"; }

lease_field() {
  local file="$1" field="$2"
  [[ -f "$file" ]] || return 1
  jq -r --arg f "$field" '.[$f] // empty' "$file" 2>/dev/null
}

lease_is_ephemeral() {
  local file="$1"
  [[ -f "$file" && "$(lease_field "$file" ephemeral 2>/dev/null || true)" == "true" ]]
}

remove_lease_key_files() {
  local lease="$1" key
  key=$(lease_field "$lease" key 2>/dev/null || true)
  case "$key" in
    ""|/dev/*) return 0 ;;
  esac
  if [[ ! -e "$key" || -f "$key" || -L "$key" ]]; then rm -f "$key"; fi
  if [[ ! -e "$key.pub" || -f "$key.pub" || -L "$key.pub" ]]; then rm -f "$key.pub"; fi
}

write_lease() {
  local file="$1" holder="$2" project="$3" since="$4" key="$5" ssh_port="$6" owner="$7" root="$8" token_sha256="${9:-}" ephemeral="${10:-0}" idle_limit="${11:-}" repos_json="${12:-[]}"
  [[ -n "$repos_json" ]] || repos_json="[]"
  jq -n --arg holder "$holder" --arg project "$project" \
        --argjson since "$since" \
        --arg key "$key" --argjson ssh_port "$ssh_port" --arg owner "$owner" --arg repo_root "$root" \
        --arg lease_token_sha256 "$token_sha256" \
        --arg ephemeral "$ephemeral" \
        --arg idle_limit "$idle_limit" \
        --argjson repos "$repos_json" \
    '{holder:$holder, project:$project, since:$since, key:$key, ssh_port:$ssh_port, owner:$owner, repo_root:$repo_root, repos:$repos} + (if $lease_token_sha256 == "" then {} else {lease_token_sha256:$lease_token_sha256} end) + (if ($ephemeral == "1" or $ephemeral == "true") then {ephemeral:true} else {} end) + (if ($idle_limit | test("^[1-9][0-9]*$")) then {idle_limit:($idle_limit | tonumber)} else {} end)' \
    >"$file.tmp.$$"
  mv "$file.tmp.$$" "$file"
}

holder_file_of() { echo "$1/$COMPOSE_DIR/.slot-holder"; }

iso_of_epoch() {
  local epoch="$1"
  date -u -r "$epoch" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null \
    || date -u -d "@$epoch" +%Y-%m-%dT%H:%M:%SZ
}

ensure_excluded() {
  local dir="$1" pattern="$2"
  local exclude; exclude=$(git -C "$dir" rev-parse --path-format=absolute --git-path info/exclude 2>/dev/null || git -C "$dir" rev-parse --git-path info/exclude 2>/dev/null) || return 0
  case "$exclude" in
    /*) ;;
    *) exclude="$dir/$exclude" ;;
  esac
  mkdir -p "$(dirname "$exclude")"
  touch "$exclude"
  grep -qxF "$pattern" "$exclude" 2>/dev/null || echo "$pattern" >>"$exclude"
}

ensure_holder_file_excluded() { ensure_excluded "$1" '.slot-holder'; }

# docs/repo and docs/fix are nested clones: without an entry here the slot reads
# as dirty for a reason that is not work, whatever the project's .gitignore says.
DOCS_CLONE_EXCLUDES="docs/repo docs/fix"
exclude_docs_clones() {
  local path
  for path in $DOCS_CLONE_EXCLUDES; do
    ensure_excluded "$1" "$path"
  done
}

write_holder_file() {
  local target="$1" holder="$2" owner="$3" slot_id="$4" since="$5"
  if [[ "$target" == /* && -d "$target" ]]; then
    local file; file=$(holder_file_of "$target")
    ensure_holder_file_excluded "$target"
    mkdir -p "$(dirname "$file")"
    {
      printf 'holder=%s\n' "$holder"
      printf 'owner=%s\n' "$owner"
      printf 'slot_id=%s\n' "$slot_id"
      printf 'since_epoch=%s\n' "$since"
      printf 'since_iso=%s\n' "$(iso_of_epoch "$since")"
    } >"$file.tmp.$$"
    mv "$file.tmp.$$" "$file"
  else
    local sid="$slot_id"
    [[ -n "$sid" ]] || sid=$(slot_id_of "$target")
    local hfile="$COMPOSE_DIR/.slot-holder"
    if [[ -n "${DOCKER_STUB_STATE:-}" ]]; then
      local vdir="$DOCKER_STUB_STATE/volumes/$(slot_volume_of "$sid")"
      if [[ -d "$vdir" ]]; then
        ensure_holder_file_excluded "$vdir"
        mkdir -p "$vdir/$COMPOSE_DIR"
        {
          printf 'holder=%s\n' "$holder"
          printf 'owner=%s\n' "$owner"
          printf 'slot_id=%s\n' "$sid"
          printf 'since_epoch=%s\n' "$since"
          printf 'since_iso=%s\n' "$(iso_of_epoch "$since")"
        } >"$vdir/$hfile.tmp.$$"
        mv "$vdir/$hfile.tmp.$$" "$vdir/$hfile"
      fi
    else
      {
        printf 'holder=%s\n' "$holder"
        printf 'owner=%s\n' "$owner"
        printf 'slot_id=%s\n' "$sid"
        printf 'since_epoch=%s\n' "$since"
        printf 'since_iso=%s\n' "$(iso_of_epoch "$since")"
      } | slot_sh "$sid" sh -c \
        'f=$1; x=$(git rev-parse --path-format=absolute --git-path info/exclude 2>/dev/null || git rev-parse --git-path info/exclude 2>/dev/null) && { mkdir -p "$(dirname "$x")" && touch "$x"; grep -qxF "$(basename "$f")" "$x" 2>/dev/null || echo "$(basename "$f")" >>"$x"; }; mkdir -p "$(dirname "$f")" && cat >"$f.tmp" && mv "$f.tmp" "$f"' sh "$hfile" 2>/dev/null || true
    fi
  fi
}

clear_holder_file() {
  local target="$1"
  if [[ "$target" == /* && -d "$target" ]]; then
    rm -f "$(holder_file_of "$target")"
  else
    local sid; sid=$(slot_id_of "$target")
    slot_exists "$sid" || return 0
    local hfile="$COMPOSE_DIR/.slot-holder"
    if [[ -n "${DOCKER_STUB_STATE:-}" ]]; then
      rm -f "$DOCKER_STUB_STATE/volumes/$(slot_volume_of "$sid")/$hfile"
    else
      slot_sh "$sid" sh -c 'rm -f "$1"' sh "$hfile" 2>/dev/null || true
    fi
  fi
}

lease_owner_field() {
  local file="$1" v
  v=$(lease_field "$file" owner)
  [[ -n "$v" ]] && echo "$v" || echo "-"
}

resolve_session_owner() {
  local owner="$1" name
  if [[ -z "${CLAUDE_CODE_HOST_SESSION_ID:-}" ]]; then
    echo "$owner"
    return 0
  fi
  name="$owner"
  if [[ "$owner" =~ ^(.*)\ \[[^][]*\]$ ]]; then name="${BASH_REMATCH[1]}"; fi
  if [[ "$name" == "-" || -z "$name" ]]; then name="session"; fi
  echo "$name [$CLAUDE_CODE_HOST_SESSION_ID]"
}

session_owner_ref() {
  local owner="$1"
  if [[ "$owner" =~ \[([^][]+)\]$ ]]; then
    printf '%s\n' "${BASH_REMATCH[1]}"
    return 0
  fi
  return 1
}

session_cap_load() {
  local ref="$1" limits="$STATE/limits.json" raw=""
  SESSION_CAP=""
  [[ -f "$limits" && -n "$ref" ]] || return 0
  # A harness may spell the same session with or without a leading local_, so
  # the lookup compares ids with that prefix stripped on both sides. A key that
  # equals the caller's id exactly wins; otherwise the key in the other spelling.
  raw=$(jq -r --arg ref "$ref" --arg bare "${ref#local_}" '
    if (type != "object") then ""
    else
      (if (.sessions | type) == "object" then .sessions else {} end) as $s
      | ([$s | to_entries[] | select((.key | sub("^local_"; "")) == $bare)] | first) as $alt
      | if ($ref != "" and ($s | has($ref))) then
          ($s[$ref] | if type == "number" then tostring else "not-a-number" end)
        elif ($bare != "" and ($alt != null)) then
          ($alt.value | if type == "number" then tostring else "not-a-number" end)
        elif has("default") then
          (.default | if type == "number" then tostring else "not-a-number" end)
        else
          ""
        end
    end' "$limits" 2>/dev/null) || raw=""
  [[ -n "$raw" ]] || return 0
  if [[ "$raw" =~ ^-?[0-9]+$ ]]; then
    if (( raw < 0 )); then
      SESSION_CAP=0
    elif (( raw > SLOT_INDEX_CEILING )); then
      SESSION_CAP=$SLOT_INDEX_CEILING
    else
      SESSION_CAP=$raw
    fi
  fi
  return 0
}

session_in_use() {
  local ref="$1" f lease_ref count=0
  for f in "$STATE"/leases/*.json; do
    [[ -f "$f" ]] || continue
    lease_is_live "$f" || continue
    lease_ref=""
    [[ "$(lease_owner_field "$f")" =~ \[([^][]+)\]$ ]] && lease_ref="${BASH_REMATCH[1]}"
    [[ -n "$lease_ref" && "${lease_ref#local_}" == "${ref#local_}" ]] || continue
    count=$((count + 1))
  done
  echo "$count"
}

session_cap_report() {
  local ref="$1"
  session_cap_load "$ref"
  [[ -n "$SESSION_CAP" ]] || return 1
  local f lease_ref held=0 held_ids=""
  for f in "$STATE"/leases/*.json; do
    [[ -f "$f" ]] || continue
    lease_is_live "$f" || continue
    lease_ref=""
    [[ "$(lease_owner_field "$f")" =~ \[([^][]+)\]$ ]] && lease_ref="${BASH_REMATCH[1]}"
    [[ -n "$lease_ref" && "${lease_ref#local_}" == "${ref#local_}" ]] || continue
    held=$((held + 1))
    held_ids+="${held_ids:+, }$(basename "$f" .json)"
  done
  (( held >= SESSION_CAP )) || return 1
  if (( held == 0 )); then
    echo "refused: session [$ref] (CLAUDE_CODE_HOST_SESSION_ID when set, else the --as ref) is paused (limit is $SESSION_CAP in $STATE/limits.json); holds 0 slot(s)."
  else
    echo "refused: session [$ref] (CLAUDE_CODE_HOST_SESSION_ID when set, else the --as ref) holds $held slot(s) ($held_ids) and its limit is $SESSION_CAP (from $STATE/limits.json). Release one, or ask the owner to raise the limit in limits.json; never acquire under another name."
  fi
  return 0
}

write_reservation() {
  local file="$1" setup="$2" holder="$3" owner="$4" root="${5:-${repo_root:-}}" ephemeral="${6:-0}" idle_limit="${7:-}"
  jq -n --arg holder "$holder" --arg project "$(basename "$file" .json)" \
        --argjson since "$(now_epoch)" \
        --arg owner "$owner" --arg setup "$setup" --argjson pid "$$" --arg boot_id "${BOOT_ID:-}" \
        --arg repo_root "$root" \
        --arg ephemeral "$ephemeral" \
        --arg idle_limit "$idle_limit" \
    '{holder:$holder, project:$project, since:$since, key:"", ssh_port:0, owner:$owner, setup:$setup, pid:$pid, boot_id:$boot_id, repo_root:$repo_root} + (if ($ephemeral == "1" or $ephemeral == "true") then {ephemeral:true} else {} end) + (if ($idle_limit | test("^[1-9][0-9]*$")) then {idle_limit:($idle_limit | tonumber)} else {} end)' \
    >"$file.tmp.$$"
  mv "$file.tmp.$$" "$file"
  RESERVED_LEASE="$file"
}

drop_reservation() {
  [[ -n "$RESERVED_LEASE" ]] && rm -f "$RESERVED_LEASE"
  RESERVED_LEASE=""
}

sweep_dead_reservations() {
  local f pid boot
  for f in "$STATE"/leases/*.json; do
    [[ -f "$f" ]] || continue
    pid=$(lease_field "$f" pid)
    [[ -n "$pid" ]] || continue
    boot=$(lease_field "$f" boot_id)
    if [[ -n "$boot" && -n "${BOOT_ID:-}" && "$boot" != "$BOOT_ID" ]]; then
      echo "▸ dropping '$(basename "$f" .json)' reservation — it was made in an earlier boot, so its pid ($pid) means nothing now" >&2
      rm -f "$f"
      continue
    fi
    kill -0 "$pid" 2>/dev/null && continue
    [[ "$(lease_field "$f" pid)" == "$pid" ]] || continue
    echo "▸ dropping '$(basename "$f" .json)' reservation — the acquire that made it (pid $pid) is gone" >&2
    rm -f "$f"
  done
}

lease_state_stream_file() {
  local f="$1" sz
  sz=$({ wc -c <"$f"; } 2>/dev/null) || sz=""
  printf 'file:%s\n' "${sz//[[:space:]]/}"
  cat "$f" 2>/dev/null || true
  printf '\n'
}

lease_state_signature() {
  {
    local dir f
    for dir in "$STATE/leases" "$STATE/parked"; do
      printf 'dir:%s\n' "$dir"
      if [[ -d "$dir" ]]; then
        for f in "$dir"/* "$dir"/.[!.]* "$dir"/..?*; do
          [[ -e "$f" || -L "$f" ]] || continue
          printf 'entry:%s\n' "${f##*/}"
          if [[ -f "$f" ]]; then
            lease_state_stream_file "$f"
          fi
        done
      fi
    done
    printf 'slots:%s\n' "${slots_dir:-}"
    if [[ -n "${slots_dir:-}" && -d "$slots_dir" ]]; then
      ls -1A "$slots_dir" 2>/dev/null || true
    fi
    printf 'limits:%s\n' "$STATE/limits.json"
    if [[ -f "$STATE/limits.json" ]]; then
      printf 'present:1\n'
      lease_state_stream_file "$STATE/limits.json"
    else
      printf 'present:0\n'
    fi
  } | adopt_sha256
}

live_lease_ids() {
  local f
  for f in "$STATE"/leases/*.json; do
    [[ -f "$f" ]] || continue
    if lease_is_live "$f"; then basename "$f" .json; fi
  done
  return 0
}

leased_count() {
  live_lease_ids | wc -l | tr -d ' '
}

machine_cap_load() {
  local limits="$STATE/limits.json" raw="" bad=""
  MACHINE_CAP=$SLOT_INDEX_CEILING
  [[ -f "$limits" ]] || return 0
  raw=$(jq -r 'if (type == "object") and has("machine_max") then (.machine_max | if type == "number" then tostring else "not-a-number" end) else "" end' "$limits" 2>/dev/null) || raw=""
  [[ -n "$raw" ]] || return 0
  if [[ "$raw" =~ ^-?[0-9]+$ ]]; then
    if (( raw < 0 )); then
      MACHINE_CAP=0; bad="$raw is below 0, using 0"
    elif (( raw > SLOT_INDEX_CEILING )); then
      MACHINE_CAP=$SLOT_INDEX_CEILING; bad="$raw is above $SLOT_INDEX_CEILING, using $SLOT_INDEX_CEILING"
    else
      MACHINE_CAP=$raw
    fi
  else
    bad="not a whole number, using $SLOT_INDEX_CEILING"
  fi
  if [[ -n "$bad" ]] && (( ! MACHINE_CAP_WARNED )); then
    echo "warning: machine_max in $limits is $bad" >&2
    MACHINE_CAP_WARNED=1
  fi
  return 0
}

machine_in_use() { leased_count; }

machine_cap_report() {
  machine_cap_load
  local used; used=$(machine_in_use)
  (( used >= MACHINE_CAP )) || return 1
  echo "acquire refuses: $used/$MACHINE_CAP live slots on this machine; the cap is machine_max in $STATE/limits.json (default $SLOT_INDEX_CEILING). A slot stops counting after $(fmt_duration "$IDLE_LIMIT") with no run and no container CPU —"
  local f
  for f in "$STATE"/leases/*.json; do
    [[ -f "$f" ]] || continue
    lease_is_live "$f" || continue
    echo "  $(basename "$f" .json): $(lease_field "$f" holder)"
  done
  return 0
}

parked_file_of() { echo "$STATE/parked/$1.json"; }

write_parked() {
  local file="$1" project="$2" holder="$3" since="$4" reason="$5"
  jq -n --arg project "$project" --arg holder "$holder" \
        --argjson since "$since" --arg reason "$reason" \
    '{project:$project, holder:$holder, since:$since, reason:$reason}' \
    >"$file.tmp.$$"
  mv "$file.tmp.$$" "$file"
}

fmt_duration() {
  local secs="$1"
  if (( secs >= 3600 )); then
    printf '%dh%02dm' $(( secs / 3600 )) $(( (secs % 3600) / 60 ))
  else
    printf '%dm' $(( secs / 60 ))
  fi
}
