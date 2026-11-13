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
lock_take() { ( set -C; : >"$1" ) 2>/dev/null; }
lock_acquire() {
  local waited=0
  until lock_take "$LOCK_DIR"; do
    waited=$((waited + 1))
    (( LOCK_WAIT )) && wait_deadline_check
    if (( REAP_LOCK_DEADLINE )); then
      (( $(now_epoch) < REAP_LOCK_DEADLINE )) \
        || die "✖ reap could not get the host-wide lock at $LOCK_DIR within ${REAP_LOCK_WAIT}s and stopped here — every slot it had not reached was not looked at. 'ps' shows which dev-container holds it."
    elif (( waited > 30 )); then
      (( LOCK_WAIT )) || die "the host-wide lock at $LOCK_DIR was busy for 30s — another dev-container is holding it. 'acquire --wait' waits it out; if nothing else is running, a stuck previous run left it: remove it by hand (rm -rf $LOCK_DIR)."
      (( waited % 60 == 31 )) && echo "▸ the host-wide lock at $LOCK_DIR has been busy for ${waited}s — still waiting (--wait)" >&2
    fi
    sleep 1
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

lease_field() {
  local file="$1" field="$2"
  [[ -f "$file" ]] || return 1
  jq -r --arg f "$field" '.[$f] // empty' "$file" 2>/dev/null
}

write_lease() {
  local file="$1" holder="$2" project="$3" since="$4" key="$5" ssh_port="$6" owner="$7" root="$8"
  jq -n --arg holder "$holder" --arg project "$project" \
        --argjson since "$since" \
        --arg key "$key" --argjson ssh_port "$ssh_port" --arg owner "$owner" --arg repo_root "$root" \
    '{holder:$holder, project:$project, since:$since, key:$key, ssh_port:$ssh_port, owner:$owner, repo_root:$repo_root}' \
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

check_session_slot_limit() {
  local owner="$1" limits="$STATE/limits.json"
  [[ -f "$limits" ]] || return 0
  local ref=""
  [[ "$owner" =~ \[([^][]+)\]$ ]] && ref="${BASH_REMATCH[1]}"
  if [[ -z "$ref" ]]; then
    echo "refused: $limits is set, so acquire needs --as \"<name> [<ref>]\" — copy your own address from ListAgents (a subagent passes its dispatcher's). Got: '$owner'." >&2
    return 1
  fi
  local limit
  limit=$(jq -r --arg ref "$ref" '(.sessions[$ref] // .default) // empty' "$limits")
  [[ -n "$limit" ]] || return 0
  local f held=0 held_ids=""
  for f in "$STATE"/leases/*.json; do
    [[ -f "$f" ]] || continue
    [[ "$(lease_owner_field "$f")" == *"[$ref]" ]] || continue
    held=$((held + 1))
    held_ids+="${held_ids:+, }$(basename "$f" .json)"
  done
  (( held >= limit )) || return 0
  echo "refused: session [$ref] (CLAUDE_CODE_HOST_SESSION_ID when set, else the --as ref) holds $held slot(s) ($held_ids) and its limit is $limit (from $limits). Release one, or ask the owner to raise the limit in limits.json; never acquire under another name." >&2
  return 1
}

write_reservation() {
  local file="$1" setup="$2" holder="$3" owner="$4"
  jq -n --arg holder "$holder" --arg project "$(basename "$file" .json)" \
        --argjson since "$(now_epoch)" \
        --arg owner "$owner" --arg setup "$setup" --argjson pid "$$" --arg boot_id "${BOOT_ID:-}" \
    '{holder:$holder, project:$project, since:$since, key:"", ssh_port:0, owner:$owner, setup:$setup, pid:$pid, boot_id:$boot_id}' \
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

lease_state_signature() {
  ls -a "$STATE/leases" "$STATE/parked" "$slots_dir" 2>/dev/null || true
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
    if (( raw < 1 )); then
      MACHINE_CAP=1; bad="$raw is below 1, using 1"
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
