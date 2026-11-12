# shellcheck shell=bash disable=SC2034,SC2154

RELEASE_SKIP_HOLDER_CHECK=0

no_term_refusal() {
  echo "$1 is gone: a lease has no term, so there is nothing to renew or extend. Activity keeps a slot live: a '$CALLER run <slot> -- <cmd>', or container CPU, within the idle limit (one value for the machine, default 2h). Keep using the slot; next: $CALLER run <slot> -- <cmd>" >&2
  exit 1
}

cmd_acquire() {
  local holder="" wait=0 owner="-" timeout_seconds="" deadline=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --for) shift; [[ $# -gt 0 ]] || die "--for needs a value"; holder="$1"; reject_duration_as_holder "$holder" ;;
      --wait) wait=1; LOCK_WAIT=1 ;;
      --ttl|--ttl=*) no_term_refusal "--ttl" ;;
      --as) shift; [[ $# -gt 0 ]] || die "--as needs a value"; owner="$1" ;;
      --timeout) shift; [[ $# -gt 0 ]] || die "--timeout needs a value"; WAIT_TIMEOUT="$1"; timeout_seconds=$(parse_duration "$1") ;;
      *) die "unknown flag to acquire: $1" ;;
    esac
    shift
  done
  [[ -n "$holder" ]] || die "acquire needs --for <holder>"
  if [[ "${AUTO_ADOPTED:-0}" == "1" ]]; then
    if ! ( cmd_doctor >/dev/null 2>&1 ); then
      cmd_doctor >&2 || true
      echo "acquire: doctor check failed — prerequisite not met" >&2
      echo "Next: fix the issues above and run: $CALLER doctor" >&2
      exit 1
    fi
  fi
  require_agent_auth
  owner=$(resolve_session_owner "$owner")
  if [[ -n "$timeout_seconds" ]]; then
    (( wait )) || die "--timeout only applies to --wait"
    deadline=$(( $(now_epoch) + timeout_seconds ))
  fi

  local last_leased=""
  while :; do
    WAIT_DEADLINE=$deadline
    activity_refresh
    lock_acquire
    local ok=0
    if ! check_session_slot_limit "$owner"; then
      lock_release
      exit 4
    fi
    try_acquire_once && ok=1
    if (( ok )); then
      write_reservation "$(lease_file_of "$ACQUIRED_ID")" "$ACQUIRE_SETUP" "$holder" "$owner"
      WAIT_DEADLINE=0
    fi
    lock_release
    if (( ok )); then
      local slot_name="${ACQUIRED_NAME:-$(basename "$ACQUIRED_DIR")}"
      create_slot "$slot_name" || die "creating '$slot_name' failed — the reason is above this line."
      finish_acquire "$ACQUIRED_DIR" "$ACQUIRED_ID" "$holder" "$owner"
      resync_node_modules "$slot_name" "$ACQUIRED_DIR" "$ACQUIRED_ID" || true
      return 0
    fi
    if (( ! wait )); then
      local cap_report
      if [[ "$ACQUIRE_BLOCKED" == memory ]]; then
        echo "acquire refuses: $MEMORY_REASON." >&2
      elif cap_report=$(machine_cap_report); then
        echo "$cap_report" >&2
      else
        echo "no slot available — every path acquire tries is exhausted." >&2
        cmd_list >&2
      fi
      echo "run 'acquire --wait --for $holder' (e.g. in the background) to wait for one" >&2
      exit 2
    fi
    local leased wait_key wait_limit=30
    if [[ "$ACQUIRE_BLOCKED" == memory ]]; then
      wait_key="memory/$MEMORY_FLOOR"
      wait_limit=3
      WAIT_NOTE=" — still waiting on memory: $MEMORY_REASON"
      if [[ "$wait_key" != "$last_leased" ]]; then
        echo "▸ waiting for memory: $MEMORY_REASON" >&2
        last_leased="$wait_key"
      fi
    else
      WAIT_NOTE=""
      leased=$(leased_count)
      machine_cap_load
      wait_key="$leased/$MACHINE_CAP"
      if [[ "$wait_key" != "$last_leased" ]]; then
        echo "▸ waiting for a free slot ($leased/$MACHINE_CAP live; the cap is machine_max in $STATE/limits.json)"
        last_leased="$wait_key"
      fi
    fi
    local seen slept=0
    seen=$(lease_state_signature)
    while (( slept < wait_limit )); do
      sleep 3
      slept=$((slept + 3))
      wait_deadline_check
      [[ "$(lease_state_signature)" == "$seen" ]] || break
    done
  done
}

cmd_release() {
  local name="" force=0 for_holder=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --slot) shift; [[ $# -gt 0 ]] || die "--slot needs a value"; name="$1" ;;
      --for) shift; [[ $# -gt 0 ]] || die "--for needs a value"; for_holder="$1" ;;
      -*) die "unknown flag to release: $1" ;;
      *)
        [[ -z "$name" ]] || die "unknown flag to release: $1"
        name="$1"
        ;;
    esac
    shift
  done
  [[ -n "$name" ]] || usage
  valid_name "$name"
  (( RELEASE_SKIP_HOLDER_CHECK == 1 )) && force=1
  name="${name#"${SLOT_PROJECT_PREFIX}-"}"

  local sid; sid=$(slot_id_of "$name")
  local dir="$slots_dir/$name"
  if ! slot_exists "$sid"; then
    if foreign_slot_exists "$name"; then
      die "'$name': not this project's own slot."
    fi
    die "no slot directory at $dir"
  fi
  [[ -d "$dir" ]] || dir=""
  local lease; lease=$(lease_file_of "$sid")
  local holder="" owner=""
  [[ -f "$lease" ]] && { holder=$(lease_field "$lease" holder); owner=$(lease_owner_field "$lease"); }

  if [[ "$RELEASE_SKIP_HOLDER_CHECK" != 1 ]]; then
    [[ -f "$lease" ]] || die "'$sid' has no lease — there is nothing to release."
    if [[ -z "$for_holder" ]]; then
      { echo "refusing to release '$sid' — release now needs --for <holder> naming whose lease this is; '$sid' is leased to '$holder' (owner '$owner')."
        echo "  if this is your slot: $CALLER release $name --for $holder"
        echo "  if you mean to take it from them: $CALLER evict $name"
      } >&2
      exit 1
    elif [[ "$for_holder" != "$holder" ]]; then
      { echo "refusing to release '$sid' — it is leased to '$holder' (owner '$owner'), not '$for_holder'."
        echo "  if this is your slot: $CALLER release $name --for $holder"
        echo "  if you mean to take it from them: $CALLER evict $name"
      } >&2
      exit 1
    fi
  fi

  # A forced release goes ahead whatever it finds, so it only warns, and by
  # git cherry alone; otherwise the delivery check below decides and refuses.
  local check_dir="${dir:-${DOCKER_STUB_STATE:+$DOCKER_STUB_STATE/volumes/${sid}-workspace}}"
  (( ! force )) || warn_unproven_branches "${check_dir:-$dir}" "⚠ '$sid' still holds branch(es) not proved to be in origin/$DEFAULT_BRANCH — check before assuming they're redundant:"

  if (( ! force )) && [[ -n "$check_dir" && -d "$check_dir/.git" ]] && ! slot_is_clean_for_teardown "$check_dir"; then
    {
      echo "refusing to release '$sid' — it holds work that exists nowhere else:"
      slot_dirty_report "$check_dir"
      echo "push what's worth keeping, or '$CALLER evict $name' to take the slot anyway."
    } >&2
    exit 1
  fi

  sweep_leftover_gateway "$dir" "$sid"
  clear_lease_key "$dir" "$sid"

  if (( ! force )); then
    teardown_dir "$dir" "$sid" || true
    if [[ -n "$dir" && -e "$dir" ]]; then
      echo "slot '$sid' could not be removed: $dir is still there. The lease is kept; remove the directory by hand and release again." >&2
      exit 1
    elif [[ -z "$dir" ]] && slot_exists "$sid"; then
      echo "slot '$sid' could not be removed: $(spec_of "$sid") is still there. The lease is kept; remove it by hand and release again." >&2
      exit 1
    fi
    local wiped_key
    wiped_key=$(lease_field "$lease" key 2>/dev/null || true)
    [[ -n "$wiped_key" ]] && rm -f "$wiped_key" "$wiped_key.pub"
    rm -f "$lease"
    echo "slot '$sid' released and removed — nobody holds it, and its directory and containers are gone."
    return
  fi

  sync_slot_ssh_key "${sid:-$dir}" ""

  if compose_running "$dir" "$sid"; then
    echo "▸ stopping '$sid' containers"
    slot_compose "$dir" "$sid" stop || true
  fi

  local key
  key=$(lease_field "$lease" key 2>/dev/null || true)
  [[ -n "$key" ]] && rm -f "$key" "$key.pub"
  rm -f "$lease"
  clear_holder_file "$dir"

  if ! compose_running "$dir" "$sid"; then
    echo "slot '$sid' released — nobody holds it. Its containers are stopped; the next acquire of it starts them again."
    return
  fi
  echo "⚠ '$sid' containers could not be stopped — they are still running." >&2

  if [[ -n "$(slot_port "$dir" "$PRIMARY_PORT")" ]]; then
    echo "slot '$sid' released — nobody holds it."
    echo "  its containers are still up and still publishing: $(ports_summary "$dir")."
    echo "  'down $name' stops them; 'restart $name' re-reads the recorded ports."
    return
  fi
  echo "slot '$sid' released — nobody holds it. Its containers are still up."
}

cmd_park() {
  local name="$1"; shift || true
  [[ -n "$name" ]] || usage
  valid_name "$name"

  local clear=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --clear) clear=1 ;;
      *) die "unknown flag to park: $1" ;;
    esac
    shift
  done

  local sid; sid=$(slot_id_of "$name")
  local dir="$slots_dir/$name"
  if ! slot_exists "$sid"; then
    if foreign_slot_exists "$name"; then
      die "'$name': not this project's own slot."
    fi
    die "no slot directory at $dir"
  fi
  [[ -d "$dir" ]] || dir=""
  local parked_file; parked_file=$(parked_file_of "$sid")

  if (( clear )); then
    [[ -f "$parked_file" ]] || die "'$sid' is not parked — nothing to clear."
    rm -f "$parked_file"
    echo "'$sid' un-parked — it is offered to 'acquire' again. Nothing about the slot itself changed; whatever made it not reuse-clean before is still there."
    return
  fi

  slot_is_ours "$sid" || die "'$name': not this project's own slot."

  local lease; lease=$(lease_file_of "$sid")
  [[ -f "$lease" ]] || die "'$sid' has no lease — there is nothing held to park. Acquire it first, or if it is already unleased and dirty, 'evict' (or a person at the slot) is the tool for that, not park."

  local holder; holder=$(lease_field "$lease" holder)
  local check_dir="${dir:-${DOCKER_STUB_STATE:+$DOCKER_STUB_STATE/volumes/${sid}-workspace}}"

  if [[ -n "$check_dir" && -d "$check_dir" ]] && slot_is_clean_for_reuse "$check_dir"; then
    clear_lease_key "$dir" "$sid"
    sync_slot_ssh_key "${sid:-$dir}" ""
    local key; key=$(lease_field "$lease" key 2>/dev/null || true)
    [[ -n "$key" ]] && rm -f "$key" "$key.pub"
    rm -f "$lease"
    clear_holder_file "$dir"
    echo "'$sid' was reuse-clean — released, not parked. A plain 'release $name --for $holder' would have done the same thing."
    return
  fi

  local reason="-"
  [[ -n "$check_dir" && -d "$check_dir" ]] && reason=$(slot_dirty_column "$check_dir")

  clear_lease_key "$dir" "$sid"
  sync_slot_ssh_key "${sid:-$dir}" ""
  local key; key=$(lease_field "$lease" key 2>/dev/null || true)
  [[ -n "$key" ]] && rm -f "$key" "$key.pub"
  rm -f "$lease"
  clear_holder_file "$dir"

  write_parked "$parked_file" "$sid" "$holder" "$(now_epoch)" "$reason"
  echo "slot '$sid' parked — the lease is gone; 'acquire' will not hand it out until a person clears it:"
  echo "  $reason"
  echo "  $CALLER park $name --clear once it's dealt with (that only removes the mark — it does not clean the slot)."
}

cmd_renew() { no_term_refusal "renew"; }

cmd_list() {
  local brief=0 fast=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --brief|--no-git) brief=1; shift ;;
      --fast) fast=1; shift ;;
      *) die "list: unknown argument '$1'" ;;
    esac
  done

  HOST_GIT_READ_ONLY=1
  activity_refresh

  local dirty_header=DIRTY acquirable_header=ACQUIRABLE
  if (( fast )); then
    dirty_header="DIRTY (skipped: --fast)"
    acquirable_header="ACQUIRABLE (fast — no delivery check)"
  elif (( brief )); then
    dirty_header="DIRTY (skipped: --brief)"
    acquirable_header="ACQUIRABLE (skipped: --brief)"
  fi
  printf '%-16s %-24s %-8s %-14s %-20s %-8s %-12s %-6s %-20s %-16s %-16s %s\n' \
    SLOT PROJECT STATE HOLDER OWNER AGE "LAST ACTIVE" SSH PROCS GIT "$dirty_header" "$acquirable_header"

  local printed_any=0
  local -a offpin_notes=()
  local -a secrets_notes=()

  if [[ -d "$slots_dir" ]]; then
    local d
    for d in "$slots_dir"/*/; do
      [[ -d "$d" ]] || continue
      d=${d%/}
      local name; name=$(basename "$d")
      if ! slot_is_ours "$d"; then
        printed_any=1
        printf '%-16s %-24s %-8s %-14s %-20s %-8s %-12s %-6s %-20s %-16s %-16s %s\n' \
          "$name" "(other project)" "-" "-" "-" "-" "-" "-" "-" "-" "-" "-"
        continue
      fi
    done
  fi

  local all_sids; all_sids=$(our_slot_ids)
  local sid
  for sid in $all_sids; do
    printed_any=1
    local name; name=$(slot_name_of "$sid")
    local d="$slots_dir/$name"
    [[ -d "$d" ]] || d=""
    local lease; lease=$(lease_file_of "$sid")
    local state holder="-" owner="-" age="-" lastactive="-" sshc="-" procs="-"
    local check_d="${d:-${DOCKER_STUB_STATE:+$DOCKER_STUB_STATE/volumes/${sid}-workspace}}"
    local dirty="-"
    if (( ! brief && ! fast )); then
      if [[ -n "$check_d" && -d "$check_d" ]]; then
        dirty=$(slot_dirty_column "$check_d")
      else
        dirty="-"
      fi
    fi
    local git_user; git_user="unset"
    if [[ -n "$d" && -d "$d" ]]; then
      git_user=$(git -C "$d" config --local user.name 2>/dev/null || true)
      [[ -n "$git_user" ]] || git_user="unset"
    else
      local env_file; env_file=$(slot_env_file_of "$sid")
      [[ -f "$env_file" ]] && git_user=$(env_value "$env_file" GIT_USER_NAME)
      [[ -n "$git_user" ]] || git_user="unset"
    fi

      local running=0
      compose_running "$d" "$sid" && running=1
      if (( running )) && slot_runs_with_secrets "$sid"; then
        secrets_notes+=("$name")
      fi
      if (( running )); then
        local sessions; sessions=$(ssh_session_count "$d" "$sid")
        sshc="ssh:$sessions"
        local nonbase; nonbase=$(non_base_procs "$d" "$sid")
        if [[ "$nonbase" == unknown ]]; then
          procs="procs:unknown (ps failed)"
        else
          procs="procs:$nonbase"
        fi
        if (( sessions > 0 )); then
          date +%s >"$STATE/seen/$sid"
        fi
      fi

      local unbuilt=0
      slot_unbuilt_reason "$check_d" "$sid" >/dev/null && unbuilt=1
      local clash=0
      PORT_CLASH_REASON=""
      slot_port_clash "$check_d" && clash=1
      local docsdrift=0
      DOCS_ORIGIN_DRIFT_REASON=""
      slot_docs_origin_drifted "$check_d" && docsdrift=1
      local offpin=0
      DOCS_PIN_REASON=""
      docs_repo_off_pin "$check_d" && { offpin=1; offpin_notes+=("$name: $DOCS_PIN_REASON"); }

      local parked_file; parked_file=$(parked_file_of "$sid")
      local parked=0 parked_reason="" parked_holder="" parked_since=""
      if [[ -f "$parked_file" ]]; then
        parked=1
        parked_reason=$(lease_field "$parked_file" reason)
        parked_holder=$(lease_field "$parked_file" holder)
        parked_since=$(lease_field "$parked_file" since)
      fi

      local acquirable="-"
      if (( fast || ! brief )); then
        if slot_acquire_verdict "$check_d" "$sid" "$fast"; then
          acquirable="acquirable"
        else
          acquirable="$ACQUIRE_REASON"
        fi
        if (( clash )); then
          case "$acquirable" in
            acquirable|unreadable:*) acquirable="portclash:$PORT_CLASH_REASON" ;;
          esac
        fi
      fi

      if [[ -f "$lease" ]]; then
        holder=$(lease_field "$lease" holder)
        owner=$(lease_owner_field "$lease")
        local since; since=$(lease_field "$lease" since)
        age=$(( ($(now_epoch) - since) / 60 ))"m"
        lastactive=$(fmt_duration "$(( $(now_epoch) - $(lease_last_activity "$lease") ))")
        if (( unbuilt )); then
          state="unbuilt"
        elif (( docsdrift )); then
          state="docs-drift"
        elif ! lease_is_live "$lease"; then
          state="idle"
        elif (( offpin )); then
          state="off-pin"
        else
          state="leased"
        fi
      elif (( unbuilt )); then
        state="unbuilt"
      elif (( docsdrift )); then
        state="docs-drift"
        holder="$DOCS_ORIGIN_DRIFT_REASON"
      elif (( parked )); then
        state="parked"
        holder="$parked_holder"
        age=$(( ($(now_epoch) - parked_since) / 60 ))"m"
        (( brief || fast )) || dirty="$parked_reason"
      elif (( clash )); then
        state="portclash"
        holder="$PORT_CLASH_REASON"
      elif (( offpin )); then
        state="off-pin"
      else
        state="free"
      fi
      printf '%-16s %-24s %-8s %-14s %-20s %-8s %-12s %-6s %-20s %-16s %-16s %s\n' \
        "$name" "$sid" "$state" "$holder" "$owner" "$age" "$lastactive" "$sshc" "$procs" "$git_user" "$dirty" "$acquirable"
    done
  local note
  for note in ${offpin_notes[@]+"${offpin_notes[@]}"}; do
    echo "  off-pin $note"
  done
  for note in ${secrets_notes[@]+"${secrets_notes[@]}"}; do
    echo "  secrets $note — running with docker-compose.secrets.yml; 'up $note' goes back to the default stack"
  done

  local proj
  for proj in $(slot_compose_projects); do
    case "$proj" in "${SLOT_PROJECT_PREFIX}-"*) ;; *) continue ;; esac
    slot_exists "$proj" && continue
    local odir; odir=$(workspace_of_project "$proj")
    if [[ -z "$odir" || ! -d "$odir" ]]; then
      printed_any=1
      printf '%-16s %-24s %-8s %-14s %-20s %-8s %-12s %-6s %-20s %-16s %-16s %s\n' \
        "-" "$proj" "orphan" "-" "-" "-" "-" "-" "-" "-" "no directory (${odir:--})" "-"
    fi
  done

  (( printed_any )) || echo "(no slot directories under $slots_dir, and no orphan compose projects matching '$SLOT_PROJECT_GLOB' — nothing to list, not nothing to say)"
}

cmd_reap() {
  [[ $# -eq 0 ]] || die "unknown flag to reap: $1"
  pull_main_checkout
  HOST_GIT_READ_ONLY=1
  REAP_LOCK_DEADLINE=$(( $(now_epoch) + REAP_LOCK_WAIT ))
  activity_refresh
  local f
  for f in "$STATE"/leases/*.json; do
    [[ -f "$f" ]] || continue
    local sid; sid=$(basename "$f" .json)
    case "$sid" in "${SLOT_PROJECT_PREFIX}-"*) ;; *) continue ;; esac
    local name=${sid#"${SLOT_PROJECT_PREFIX}"-}
    local dir="$slots_dir/$name"
    slot_exists "$sid" || continue
    [[ -d "$dir" ]] || dir=""
    lease_is_live "$f" && continue

    if slot_is_abandoned "$dir" "$sid"; then
      archive_and_clean_slot "$dir" "$sid" "$name" || true
    fi
  done

  local d
  for d in "$slots_dir"/*/; do
    [[ -d "$d" ]] || continue
    d=${d%/}
    slot_is_ours "$d" || continue
    local sid; sid=$(slot_id_of "$d")
    [[ -f "$(lease_file_of "$sid")" ]] && continue
    [[ -f "$(parked_file_of "$sid")" ]] || prune_delivered_branches "$d" "$sid"
  done

  local proj
  for proj in $(slot_compose_projects); do
    case "$proj" in "${SLOT_PROJECT_PREFIX}-"*) ;; *) continue ;; esac
    [[ -f "$(lease_file_of "$proj")" ]] && continue
    local odir; odir=$(workspace_of_project "$proj")
    local owned=0
    [[ -n "$odir" && -d "$odir" ]] && slot_is_ours "$odir" && owned=1
    if [[ -z "$odir" || ! -d "$odir" ]] || (( ! owned )); then
      if [[ -n "$odir" && -d "$odir" ]] && slot_is_clean_for_teardown "$odir" \
         && (( $(ssh_session_count "$odir" "$proj") == 0 )); then
        echo "▸ reaping orphan compose project '$proj' — clean, no session"
        teardown_dir "$odir" "$proj" || echo "  ! '$proj' left as it is" >&2
      else
        echo "'$proj' orphan — no lease, $( [[ -n "$odir" ]] && echo "at $odir" || echo "no directory" ):"
        [[ -n "$odir" && -d "$odir" ]] && slot_dirty_report "$odir"
      fi
    fi
  done

  local sid
  for sid in $(our_slot_ids); do
    local sname; sname=$(slot_name_of "$sid")
    local sdir="$slots_dir/$sname"
    [[ -d "$sdir" ]] && continue
    [[ -f "$(parked_file_of "$sid")" ]] && continue
    local slease; slease=$(lease_file_of "$sid")
    if [[ -f "$slease" ]] && lease_is_live "$slease"; then
      continue
    fi
    if compose_running "" "$sid"; then
      continue
    fi
    echo "▸ reaping abandoned volume slot '$sid'"
    archive_and_clean_slot "" "$sid" "$sname" || teardown_dir "" "$sid" || true
  done

  exit 0
}

cmd_evict() {
  if [[ "${1:-}" == "--stale" ]]; then
    shift
    local days=${1:-7}
    shift || true
    [[ $# -eq 0 ]] || die "unknown flag to evict --stale: $1"
    local cutoff=$(( $(now_epoch) - days * 86400 ))
    for sid in $(our_slot_ids); do
      local sname; sname=$(slot_name_of "$sid")
      local sdir="$slots_dir/$sname"
      local seen="$STATE/seen/$sid"
      local last=0
      [[ -f "$seen" ]] && last=$(cat "$seen")
      if (( last < cutoff )); then
        echo "▸ evicting '$sid' — no ssh session in $days+ days"
        if [[ -d "$sdir" ]]; then
          rescue_and_reset "$sdir" "$sid"
        else
          archive_and_clean_slot "" "$sid" "$sname"
        fi
      fi
    done
    return
  fi

  local name="$1"; shift || true
  [[ -n "$name" ]] || usage
  valid_name "$name"
  local yes=0 force=0 arg
  for arg in "$@"; do
    case "$arg" in
      --yes) yes=1 ;;
      --force) force=1 ;;
      *) die "evict: unknown argument '$arg' (expected --yes and/or --force)." ;;
    esac
  done
  local sid; sid=$(slot_id_of "$name")
  local dir="$slots_dir/$name"
  if ! slot_exists "$sid"; then
    if foreign_slot_exists "$name"; then
      die "'$name': not this project's own slot."
    fi
    die "no slot directory at $dir"
  fi
  [[ -d "$dir" ]] || dir=""
  if ! slot_is_ours "$sid"; then
    local foreign_origin="(no origin)"
    if [[ -n "$dir" && -d "$dir" ]]; then
      foreign_origin=$(git -C "$dir" remote get-url origin 2>/dev/null) || foreign_origin="(no origin)"
    elif docker volume inspect "$(slot_volume_of "$sid")" >/dev/null 2>&1; then
      foreign_origin=$(docker volume inspect "$(slot_volume_of "$sid")" --format '{{index .Labels "dev-container.origin"}}' 2>/dev/null || true)
      [[ -n "$foreign_origin" ]] || foreign_origin="(no origin)"
    fi
    die "'${dir:-$name}' is not this project's slot — its origin is '$foreign_origin', not '$origin_url'. Refusing to evict it: that would push every local branch there to '$foreign_origin' and then reset and clean the checkout."
  fi

  if (( ! force )); then
    local lease; lease=$(lease_file_of "$sid")
    local blockers=""
    if [[ -f "$lease" ]]; then
      activity_refresh
      ! lease_is_live "$lease" \
        || blockers+="  leased to '$(lease_field "$lease" holder)' and live — last active $(fmt_idle_for "$lease")"$'\n'
    fi
    local sessions; sessions=$(ssh_session_count "$dir" "$sid")
    (( sessions > 0 )) \
      && blockers+="  $sessions live ssh session(s) — somebody is connected right now"$'\n'
    local procs; procs=$(non_base_procs "$dir" "$sid")
    [[ -z "$procs" ]] \
      || blockers+="  processes beyond the base set: $procs"$'\n'
    if [[ -n "$blockers" ]]; then
      { echo "refusing to evict '$sid' — it is in use:"
        printf '%s' "$blockers"
        echo "  an eviction resets the checkout ('git reset --hard', 'git clean -fd'), so whatever is uncommitted in there now would be committed as a rescue and everything else discarded."
        echo "  if that is what you mean, say so: $CALLER evict $name --yes --force"
      } >&2
      exit 1
    fi
  fi

  if (( ! yes )); then
    read -r -p "evict '$sid' now? [y/N] " ans
    [[ "$ans" == [yY]* ]] || { echo "not evicted."; exit 0; }
  fi
  if [[ -z "$dir" || ! -d "$dir" || "$(spec_of "$sid")" == vol:* ]]; then
    archive_and_clean_slot "" "$sid" "$name"
  else
    rescue_and_reset "$dir" "$sid"
  fi
}

cmd_restart() {
  local name="$1"; shift || true
  [[ -n "$name" ]] || usage
  valid_name "$name"
  [[ $# -eq 0 ]] || die "unknown flag to restart: $1"
  require_agent_auth

  local dir="$slots_dir/$name"
  local proj; proj=$(slot_id_of "$name")

  if ! slot_exists "$proj"; then
    die "no slot directory at $dir — 'restart' only revives a slot that still exists. To create one: $CALLER acquire --for <holder>"
  fi
  [[ -d "$dir" ]] || dir=""
  if [[ ! -f "$(compose_file_of)" ]]; then
    die "no $(compose_file_of) — every slot is composed from the main checkout's $COMPOSE_DIR/, and this checkout has none."
  fi

  local fresh_env=0
  [[ -f "$(slot_env_file_of "$proj")" ]] || fresh_env=1
  ensure_slot_env "$dir" "$proj"
  if (( fresh_env )); then
    local lease_key; lease_key=$(lease_field "$(lease_file_of "$proj")" key 2>/dev/null || true)
    if [[ -n "$lease_key" && -f "$lease_key.pub" ]]; then
      sync_slot_ssh_key "${proj:-$dir}" "$(cat "$lease_key.pub")"
    fi
  fi

  apply_checkout_git_identity "${dir:-$proj}"
  local target="${dir:-$proj}"
  [[ -z "$dir" ]] || create_mount_dirs "$dir"

  resolve_slot_ports "$target"
  assert_slot_ports "restarting '$proj'" "$target"
  sync_slot_env_identity "$target" "$proj"

  ensure_shared_volumes

  if slot_image_is_stale "$proj" "$(slot_build_hash)"; then
    build_slot_image "$dir" "$proj"
    run_slot_install "$name" "$dir" "$proj"
  fi

  echo "▸ starting compose project '$proj' on $(ports_summary "$target")"
  slot_compose "$dir" "$proj" up -d

  provision_slot "$dir" "$proj" \
    || die "'$name' is up but was not provisioned — the reason and the fix are above; '$CALLER restart $name' runs it again."

  echo
  echo "slot '$name' back up: $target"
  echo "  the host key is new — sshd's key volume is per compose project, and a"
  echo "  container recreated from scratch generates a fresh one. Clear the old one"
  echo "  before connecting, or ssh refuses with \"REMOTE HOST IDENTIFICATION HAS CHANGED\":"
  echo "    ssh-keygen -R '[localhost]:$(slot_port "$target" "$PRIMARY_PORT")'"
}

workdir_of() {
  local cf="$1" line
  line=$(grep -m1 -E '^[[:space:]]*-[[:space:]]*\.\.:/[^:[:space:]]+' "$cf" 2>/dev/null) || return 0
  echo "$line" | sed -E 's#^[[:space:]]*-[[:space:]]*\.\.:([^:[:space:]]+).*#\1#'
}

LIVE_LOCK_HELD=0
LIVE_LOCK_FILE=""
LIVE_GATEWAY_ACTIVE=0
LIVE_GATEWAY_DIR=""
LIVE_GATEWAY_SID=""
LIVE_RUN_PID=""

live_lock_file_of() { echo "$STATE/live/$SLOT_PROJECT_PREFIX.lock"; }

live_lock_held_by_live_pid() {
  local file="$1" pid
  [[ -f "$file" ]] || return 1
  IFS= read -r pid <"$file" 2>/dev/null || pid=""
  [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null
}

live_lock_acquire() {
  local file pid waited=0
  mkdir -p "$STATE/live"
  file=$(live_lock_file_of)
  until ( set -C; printf '%s\n' "$$" >"$file" ) 2>/dev/null; do
    if [[ -f "$file" ]]; then
      IFS= read -r pid <"$file" 2>/dev/null || pid=""
      if [[ -n "$pid" ]] && ! kill -0 "$pid" 2>/dev/null; then
        rm -f "$file"
        continue
      fi
    fi
    waited=$((waited + 1))
    (( waited <= 600 )) || die "another live run of '$SLOT_PROJECT_PREFIX' is still holding $file after 60s."
    sleep 0.1
  done
  LIVE_LOCK_FILE=$file
  LIVE_LOCK_HELD=1
}

live_gateway_rm() {
  local dir="$1" sid="$2"
  [[ -n "$LIVE_GATEWAY" ]] || return 0
  (
    unset GH_TOKEN
    export COMPOSE_PROFILES="$LIVE_GATEWAY"
    slot_compose "$dir" "$sid" rm -f -s "$LIVE_GATEWAY" >/dev/null 2>&1
  ) || true
}

live_gateway_cleanup() {
  if [[ -n "$LIVE_RUN_PID" ]]; then
    kill -9 "$LIVE_RUN_PID" 2>/dev/null || true
    wait "$LIVE_RUN_PID" 2>/dev/null || true
    LIVE_RUN_PID=""
  fi
  if (( LIVE_GATEWAY_ACTIVE )); then
    LIVE_GATEWAY_ACTIVE=0
    live_gateway_rm "$LIVE_GATEWAY_DIR" "$LIVE_GATEWAY_SID"
  fi
  if (( LIVE_LOCK_HELD )); then
    rm -f "$LIVE_LOCK_FILE"
    LIVE_LOCK_HELD=0
  fi
}

sweep_leftover_gateway() {
  local dir="$1" sid="$2" lock
  [[ -n "$LIVE_GATEWAY" && -f "$(compose_file_of)" ]] || return 0
  lock=$(live_lock_file_of)
  if live_lock_held_by_live_pid "$lock"; then
    return 0
  fi
  rm -f "$lock"
  live_gateway_rm "$dir" "$sid"
}

# run --live: the command goes to the project's one-shot live service with the
# named keys and nothing else. Each value is read as data from LIVE_ENV_FILE in
# the owner's checkout (last line wins; absent or blank refuses the whole run,
# by name) and reaches docker by name, never in an argument. No forge token.
run_live() {
  local dir="$1" sid="$2"; shift 2
  local -a names=() pairs=() flags=(--rm -u node)
  while [[ "$1" != "--" ]]; do names+=("$1"); shift; done
  shift
  local file="$repo_root/$LIVE_ENV_FILE" name line value found pair rc=0
  for name in "${names[@]}"; do
    value="" found=""
    if [[ -f "$file" ]]; then
      while IFS= read -r line || [[ -n "$line" ]]; do
        if [[ "$line" == "$name="* ]]; then
          value=${line#*=}
          found=1
        fi
      done <"$file"
    fi
    [[ -n "$found" && -n "$value" ]] || die "--live $name: no value for it in $file."
    pairs+=("$name=$value")
  done
  [[ -t 0 ]] || flags+=(-T)
  for name in "${names[@]}"; do flags+=(-e "$name"); done
  write_slot_overlay "$sid"
  live_lock_acquire
  LIVE_GATEWAY_DIR=$dir
  LIVE_GATEWAY_SID=$sid
  trap 'live_gateway_cleanup; exit 130' INT
  trap 'live_gateway_cleanup; exit 143' TERM
  trap 'live_gateway_cleanup; exit 129' HUP
  if [[ -n "$LIVE_GATEWAY" ]]; then
    live_gateway_rm "$dir" "$sid"
    LIVE_GATEWAY_ACTIVE=1
    if ! (
      unset GH_TOKEN
      export COMPOSE_PROFILES="$LIVE_GATEWAY"
      slot_compose "$dir" "$sid" up -d --wait "$LIVE_GATEWAY"
    ); then
      live_gateway_cleanup
      die "starting the live gateway '$LIVE_GATEWAY' for '$sid' failed — the command was not run."
    fi
  fi
  activity_stamp "$sid" run
  (
    unset GH_TOKEN
    for pair in "${pairs[@]}"; do export "$pair"; done
    export COMPOSE_PROFILES="$LIVE_SERVICE"
    slot_compose "$dir" "$sid" run "${flags[@]}" "$LIVE_SERVICE" "$@"
  ) &
  LIVE_RUN_PID=$!
  wait "$LIVE_RUN_PID" || rc=$?
  LIVE_RUN_PID=""
  activity_stamp "$sid" run
  live_gateway_cleanup
  trap - INT TERM HUP
  return "$rc"
}

cmd_run() {
  local name="$1"; shift || true
  [[ -n "$name" ]] || usage
  valid_name "$name"
  local -a live_names=()
  local live_seen=" " live_n
  while [[ "${1:-}" == "--live" ]]; do
    live_n="${2:-}"
    [[ "$live_n" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || die "--live needs a variable name (letters, digits, underscore), got '$live_n'."
    shift 2
    [[ "$live_seen" == *" $live_n "* ]] && continue
    live_seen+="$live_n "
    live_names+=("$live_n")
  done
  [[ "${1:-}" == "--" ]] || die "run needs '--' before the command: $CALLER run $name [--live NAME]... -- <cmd> [args...]"
  shift
  [[ $# -ge 1 ]] || die "run needs a command after '--': $CALLER run $name -- <cmd> [args...]"
  if (( ${#live_names[@]} )); then
    [[ -n "$LIVE_SERVICE" ]] || die "--live needs LIVE_SERVICE in project.env — the compose service that runs the command."
    [[ -n "$LIVE_ENV_FILE" ]] || die "--live needs LIVE_ENV_FILE in project.env — the file, relative to the checkout, that holds the keys."
  fi

  local sid; sid=$(slot_id_of "$name")
  local dir="$slots_dir/$name"
  if ! slot_exists "$sid"; then
    if foreign_slot_exists "$name"; then
      die "'$name': not this project's own slot."
    fi
    if archive_exists_for_slot "$SLOT_PROJECT_PREFIX" "$name"; then
      die "slot '$name' was collapsed — restore it with: $CALLER resume $name"
    fi
    die "no slot directory at $dir — 'run' only acts on a slot that already exists."
  fi
  [[ -d "$dir" ]] || dir=""
  slot_is_ours "$sid" || die "'$name': not this project's own slot."

  local lease; lease=$(lease_file_of "$sid")
  if [[ ! -f "$lease" ]]; then
    if [[ -f "$(parked_file_of "$sid")" ]]; then
      die "'$sid' is parked, not leased — 'run' must not touch a slot nobody holds. '$CALLER park $name --clear' once whatever parked it is dealt with, then acquire it."
    fi
    if archive_exists_for_slot "$SLOT_PROJECT_PREFIX" "$name"; then
      die "slot '$name' was collapsed — restore it with: $CALLER resume $name"
    fi
    die "'$sid' is not leased — 'run' must not touch a slot nobody holds. '$CALLER acquire --for <holder>' first."
  fi
  if ! lease_is_live "$lease"; then
    die "slot '$name' is abandoned — restore it with: $CALLER resume $name"
  fi

  local cf; cf=$(compose_file_of)
  [[ -f "$cf" ]] || die "no $cf — every slot is composed from the main checkout's $COMPOSE_DIR/, and this checkout has none."
  compose_running "$dir" "$sid" || die "'$sid' has no running containers — '$CALLER restart $name' brings them back."

  local -a exec_flags=()
  [[ -t 0 ]] || exec_flags+=(-T)
  exec_flags+=(-u node)
  local rc=0
  if (( ${#live_names[@]} )); then
    run_live "$dir" "$sid" "${live_names[@]}" -- "$@" || rc=$?
    return "$rc"
  fi
  sweep_leftover_gateway "$dir" "$sid"
  local workdir; workdir=$(workdir_of "$cf")
  [[ -n "$workdir" ]] && exec_flags+=(-w "$workdir")
  local svc
  svc=$(container_service_of "$dir" "$sid") || die "cannot run in '$sid' without the one service that mounts the checkout at /workspace."

  case "$FORGE_AUTH" in
    app)
      agent_installation_token
      activity_stamp "$sid" run
      GH_TOKEN="$AGENT_TOKEN" slot_compose "$dir" "$sid" exec -e GH_TOKEN "${exec_flags[@]}" "$svc" "$@" || rc=$?
      AGENT_TOKEN="" ;;
    token|"")
      activity_stamp "$sid" run
      slot_compose "$dir" "$sid" exec "${exec_flags[@]}" "$svc" "$@" || rc=$? ;;
    *) die "internal: FORGE_AUTH $FORGE_AUTH has no rule for run" ;;
  esac
  activity_stamp "$sid" run
  return "$rc"
}

cmd_put() {
  local name="${1:-}" src="${2:-}" dst="${3:-}"
  [[ -n "$name" && -n "$src" && -n "$dst" && $# -eq 3 ]] || die "put takes exactly: $CALLER put <slot> <src> <dst> or $CALLER put <slot> --stdin <dst>"
  valid_name "$name"

  case "$dst" in
    /*) die "put: <dst> is relative to the slot's workspace (/workspace), not absolute: $dst" ;;
    ..|../*|*/..|*/../*|*/..) die "put: <dst> must stay inside the slot's workspace: $dst" ;;
  esac

  local sid
  sid=$(slot_id_of "$name")
  slot_exists "$sid" || die "no slot '$name' — 'put' only acts on a slot that already exists."
  slot_is_ours "$sid" || die "'$name': not this project's own slot."
  [[ -f "$(lease_file_of "$sid")" ]] || die "'$sid' is not leased — 'put' must not touch a slot nobody holds."

  local is_stdin=0 is_tar=0
  if [[ "$src" == "--stdin" ]]; then
    is_stdin=1
  elif [[ "$src" == "--tar" || "$src" == "--stdin-tar" ]]; then
    is_stdin=1
    is_tar=1
  else
    [[ -e "$src" ]] || die "put: no such file or directory on this host: $src"
  fi

  local spec
  spec=$(spec_of "$sid")

  if [[ "$spec" == dir:* ]]; then
    local dir="${spec#dir:}"
    [[ -n "$dir" && -d "$dir" ]] || dir="$slots_dir/$name"
    if compose_running "$sid"; then
      if (( is_tar )); then
        slot_sh "$sid" sh -c 'mkdir -p "$1" && tar -xf - -C "$1"' sh "$dst" \
          || die "put: could not unpack into '$sid' — the reason is above this line."
      elif (( is_stdin )); then
        slot_sh "$sid" sh -c 'mkdir -p "$(dirname "$1")" && cat >"$1"' sh "$dst" \
          || die "put: could not write into '$sid' — the reason is above this line."
      elif [[ -d "$src" ]]; then
        echo "▸ putting directory $src into '$sid' at $dst/"
        tar -C "$src" -cf - . | slot_sh "$sid" sh -c 'mkdir -p "$1" && tar -xf - -C "$1"' sh "$dst" \
          || die "put: could not unpack into '$sid' — the reason is above this line."
      else
        echo "▸ putting $src into '$sid' at $dst"
        slot_sh "$sid" sh -c 'mkdir -p "$(dirname "$1")" && cat >"$1"' sh "$dst" <"$src" \
          || die "put: could not write into '$sid' — the reason is above this line."
      fi
    else
      if (( is_tar )); then
        mkdir -p "$dir/$dst"
        tar -C "$dir/$dst" -xf - || die "put: could not unpack into '$sid' — the reason is above this line."
      elif (( is_stdin )); then
        mkdir -p "$(dirname "$dir/$dst")"
        cat >"$dir/$dst" || die "put: could not write into '$sid' — the reason is above this line."
      elif [[ -d "$src" ]]; then
        echo "▸ putting directory $src into '$sid' at $dst/"
        mkdir -p "$dir/$dst"
        tar -C "$src" -cf - . | tar -C "$dir/$dst" -xf - \
          || die "put: could not unpack into '$sid' — the reason is above this line."
      else
        echo "▸ putting $src into '$sid' at $dst"
        mkdir -p "$(dirname "$dir/$dst")"
        cp -f "$src" "$dir/$dst" || die "put: could not write into '$sid' — the reason is above this line."
      fi
    fi
    return 0
  fi

  # Volume slot
  if (( is_tar )); then
    slot_exec_stream "$sid" sh -c 'mkdir -p "$1" && tar -xf - -C "$1"' sh "$dst" \
      || die "put: could not unpack into '$sid' — the reason is above this line."
  elif (( is_stdin )); then
    slot_exec_stream "$sid" sh -c 'mkdir -p "$(dirname "$1")" && cat >"$1"' sh "$dst" \
      || die "put: could not write into '$sid' — the reason is above this line."
  elif [[ -d "$src" ]]; then
    echo "▸ putting directory $src into '$sid' at $dst/"
    tar -C "$src" -cf - . | slot_exec_stream "$sid" sh -c 'mkdir -p "$1" && tar -xf - -C "$1"' sh "$dst" \
      || die "put: could not unpack into '$sid' — the reason is above this line."
  else
    echo "▸ putting $src into '$sid' at $dst"
    slot_exec_stream "$sid" sh -c 'mkdir -p "$(dirname "$1")" && cat >"$1"' sh "$dst" <"$src" \
      || die "put: could not write into '$sid' — the reason is above this line."
  fi
}

cmd_db_target() {
  local name="$1"; shift || true
  [[ -n "$name" ]] || usage
  valid_name "$name"

  local sid; sid=$(slot_id_of "$name")
  local dir="$slots_dir/$name"
  if ! slot_exists "$sid"; then
    if foreign_slot_exists "$name"; then
      die "'$name': not this project's own slot."
    fi
    die "no slot directory at $dir — 'db-target' only acts on a slot that already exists."
  fi
  [[ -d "$dir" ]] || dir=""
  slot_is_ours "$sid" || die "'$name': not this project's own slot."

  local lease; lease=$(lease_file_of "$sid")
  if [[ ! -f "$lease" ]]; then
    if [[ -f "$(parked_file_of "$sid")" ]]; then
      die "'$sid' is parked, not leased — 'db-target' must not touch a slot nobody holds. '$CALLER park $name --clear' once whatever parked it is dealt with, then acquire it."
    fi
    die "'$sid' is not leased — 'db-target' must not touch a slot nobody holds. '$CALLER acquire --for <holder>' first."
  fi

  local cf; cf=$(compose_file_of)
  [[ -f "$cf" ]] || die "no $cf — every slot is composed from the main checkout's $COMPOSE_DIR/, and this checkout has none."
  compose_running "$dir" "$sid" || die "'$sid' has no running containers — '$CALLER restart $name' brings them back."

  compose_args_of "$sid"
  printf 'DB_RESTORE_PROJECT=%q\n' "$sid"
  printf 'DB_RESTORE_COMPOSE_ARGS=('
  printf ' %q' "${COMPOSE_ARGS[@]}"
  printf ' )\n'
}

cmd_up_compat() {
  [[ $# -eq 0 ]] || die "unknown flag to up: $1"
  echo "▸ 'up' is 'acquire --for manual' now — a slot is a lease, not a bare container."
  cmd_acquire --for manual
}

cmd_up() {
  [[ $# -gt 0 ]] || { cmd_up_compat; return; }
  local name="$1"; shift
  local secrets=0 for_holder="" have_for=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --secrets) secrets=1 ;;
      --for)
        [[ $# -ge 2 ]] || die "up: --for needs a value"
        for_holder="$2"; have_for=1; shift ;;
      *) die "unknown flag to up: $1" ;;
    esac
    shift
  done
  valid_name "$name"
  (( secrets )) || (( ! have_for )) || die "up: --for only goes with --secrets"

  if (( secrets )); then
    local sid; sid=$(slot_id_of "$name")
    local dir="$slots_dir/$name"
    slot_exists "$sid" || die "no slot directory at $dir"
    [[ -d "$dir" ]] || dir=""
    local lease; lease=$(lease_file_of "$sid")
    [[ -f "$lease" ]] || die "'$sid' has no lease — 'up $name --secrets' needs a slot you hold."
    local holder; holder=$(lease_field "$lease" holder)
    if (( ! have_for )); then
      die "refusing 'up $name --secrets' — it needs --for <holder> naming whose lease this is; '$sid' is leased to '$holder'."
    elif [[ "$for_holder" != "$holder" ]]; then
      die "refusing 'up $name --secrets' — '$sid' is leased to '$holder', not '$for_holder'."
    fi
    local hcontent=""
    if [[ -n "$dir" && -d "$dir" ]]; then
      local hfile; hfile=$(holder_file_of "$dir")
      [[ -f "$hfile" ]] || die "refusing 'up $name --secrets' — $hfile (.slot-holder) is missing; the lease is not confirmed from the slot's side."
      hcontent=$(cat "$hfile")
    elif [[ -n "${DOCKER_STUB_STATE:-}" ]]; then
      local stub_holder="$DOCKER_STUB_STATE/volumes/$(slot_volume_of "$sid")/$COMPOSE_DIR/.slot-holder"
      [[ -f "$stub_holder" ]] || die "refusing 'up $name --secrets' — $COMPOSE_DIR/.slot-holder (.slot-holder) is missing; the lease is not confirmed from the slot's side."
      hcontent=$(cat "$stub_holder")
    else
      hcontent=$(slot_sh "$sid" sh -c 'cat "$1"' sh "$COMPOSE_DIR/.slot-holder" 2>/dev/null || true)
      [[ -n "$hcontent" ]] || die "refusing 'up $name --secrets' — $COMPOSE_DIR/.slot-holder (.slot-holder) is missing; the lease is not confirmed from the slot's side."
    fi
    [[ "$(sed -n 's/^holder=//p' <<<"$hcontent")" == "$holder" ]] \
      || die "refusing 'up $name --secrets' — the slot's .slot-holder does not name '$holder'."
    [[ -f "$(secrets_overlay_of)" ]] || die "$(secrets_overlay_of) does not exist — the overlay comes from the main checkout's $COMPOSE_DIR/."
    local spec; spec=$(spec_of "$sid")
    if [[ -f "$repo_root/.env.test.local" ]]; then
      if [[ "$spec" == dir:* ]]; then
        local d="${spec#dir:}"
        [[ -n "$d" && -d "$d" ]] || d="$slots_dir/$name"
        mkdir -p "$(dirname "$d/.env.test.local")"
        cp -f "$repo_root/.env.test.local" "$d/.env.test.local"
      else
        slot_helper_run "$sid" sh -c 'mkdir -p "$(dirname "$1")" && cat >"$1"' sh ".env.test.local" <"$repo_root/.env.test.local" \
          || die "could not put .env.test.local into '$sid' — the reason is above this line."
      fi
    fi
    if [[ -f "$repo_root/.local/node-chrome-storage.json" ]]; then
      if [[ "$spec" == dir:* ]]; then
        local d="${spec#dir:}"
        [[ -n "$d" && -d "$d" ]] || d="$slots_dir/$name"
        mkdir -p "$(dirname "$d/.local/node-chrome-storage.json")"
        cp -f "$repo_root/.local/node-chrome-storage.json" "$d/.local/node-chrome-storage.json"
      else
        slot_helper_run "$sid" sh -c 'mkdir -p "$(dirname "$1")" && cat >"$1"' sh ".local/node-chrome-storage.json" <"$repo_root/.local/node-chrome-storage.json" \
          || die "could not put .local/node-chrome-storage.json into '$sid' — the reason is above this line."
      fi
    fi
    COMPOSE_WITH_SECRETS=1
  fi
  cmd_restart "$name"
}

cmd_down() {
  local name="$1"; shift || true
  [[ -n "$name" ]] || usage
  valid_name "$name"
  local force=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --force) force=1 ;;
      *) die "unknown flag to down: $1" ;;
    esac
    shift
  done

  local dir="$slots_dir/$name"
  local sid; sid=$(slot_id_of "$name")
  [[ -d "$dir" ]] || dir=""
  local lease; lease=$(lease_file_of "$sid")
  if [[ -f "$lease" ]] && (( ! force )); then
    die "'$sid' is leased to $(lease_field "$lease" holder) — release it first, or 'down $name --force'."
  fi
  local check_dir="${dir:-${DOCKER_STUB_STATE:+$DOCKER_STUB_STATE/volumes/${sid}-workspace}}"
  if [[ -n "$check_dir" && -d "$check_dir/.git" ]] && (( ! force )) && ! slot_is_clean_for_teardown "$check_dir"; then
    {
      echo "refusing to remove '$sid' — it holds work that exists nowhere else:"
      slot_dirty_report "$check_dir"
      echo "push what's worth keeping, or 'down $name --force' to remove it anyway."
    } >&2
    exit 1
  fi
  teardown_dir "$dir" "$sid" || exit 1
  [[ -f "$lease" ]] && rm -f "$lease"
  echo "slot '$name' down"
}

cmd_install() {
  local name="${1:-}"
  [[ -n "$name" ]] || usage
  valid_name "$name"
  [[ $# -eq 1 ]] || die "install takes a slot name and nothing else — the install's flags are fixed."

  local sid; sid=$(slot_id_of "$name")
  local dir="$slots_dir/$name"
  slot_exists "$sid" || die "no slot directory at $dir — 'install' only acts on a slot that already exists."
  [[ -d "$dir" ]] || dir=""
  slot_is_ours "$sid" || die "'$name': not this project's own slot."
  [[ -f "$(lease_file_of "$sid")" ]] || die "'$sid' is not leased — 'install' must not touch a slot nobody holds."

  refuse_unclean_checkout

  ensure_shared_volumes
  install_slot "$dir" "$sid"
  build_slot_image "$dir" "$sid"
}


cmd_clean() {
  local all=0 slot_name=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --all) all=1 ;;
      slot-*) slot_name="$1" ;;
      *) die "clean: unknown option '$1'" ;;
    esac
    shift
  done

  activity_refresh

  if (( all )); then
    cmd_clean_all
    return 0
  fi

  if [[ -n "$slot_name" ]]; then
    local sid; sid=$(slot_id_of "$slot_name")
    local dir="$slots_dir/$slot_name"
    slot_exists "$sid" || die "no slot directory at $dir"
    [[ -d "$dir" ]] || dir=""
    archive_and_clean_slot "$dir" "$sid" "$slot_name"
    return 0
  fi

  clean_project_slots "$slots_dir" "$SLOT_PROJECT_PREFIX"
  return 0
}
