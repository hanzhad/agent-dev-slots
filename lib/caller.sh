# shellcheck shell=bash disable=SC2034,SC2154

CALLER_LIFELINE_ACTIVE=0
CALLER_LIFELINE_MAIN_PID=""
CALLER_LIFELINE_SYNC_FD=""
CALLER_WATCHER_PID=""
CALLER_CHILD_PID=""
CALLER_DEAD_FILE=""
CALLER_INTERRUPTED=0
CALLER_ABORTING=0
CALLER_STALE_WARNED=0
SLOT_DIR="${SLOT_DIR:-}"
SLOT_ID="${SLOT_ID:-}"
RESUME_RESTORING_DIR="${RESUME_RESTORING_DIR:-}"
RESUME_ARCHIVE_DIR="${RESUME_ARCHIVE_DIR:-}"

# Pure predicate: true iff $1 requests the stdin caller lifeline.
caller_lifeline_requested() {
  [[ "${1:-}" == "stdin" ]]
}

# Pure check: prints a one-line warning on stdout when a legacy per-project
# bin/dev-container launcher invoked the containerized engine without a caller
# lifeline. Never matches global installs (/usr/local/bin, /opt, ~/.local/bin).
caller_stale_launcher_warning() {
  local caller_path="${1:-}" lifeline_env="${2:-}" in_container="${3:-}" root="${4:-}"
  [[ "$in_container" == "1" && -z "$lifeline_env" ]] || return 0
  case "$caller_path" in
    bin/dev-container | ./bin/dev-container) ;;
    *)
      [[ -n "$root" && "$caller_path" == "${root%/}/bin/dev-container" ]] || return 0
      ;;
  esac
  printf 'warning: %s is an outdated per-project launcher without caller lifeline; install the global CLI (scripts/install-cli.sh) or use scripts/dev-container-thin.\n' "$caller_path"
}

# Pure process-tree walk: prints transitive descendant PIDs of $1 (excluding $1).
# Reads /proc/[0-9]*/stat directly on Linux so comm names with spaces or ')'
# parse safely without procps; falls back to BSD/BusyBox-compatible ps -ax on
# non-/proc hosts.
descendant_pids_of() {
  local root="${1:-}"
  [[ "$root" =~ ^[0-9]+$ ]] || return 0
  local -a children_of=() seen=() queue=("$root") out=()
  local pid ppid cur child head=0
  if [[ -d /proc ]]; then
    local f stat_line rest state
    for f in /proc/[0-9]*/stat; do
      [[ -r "$f" ]] || continue
      stat_line=""
      { IFS= read -r -d '' stat_line <"$f" || [[ -n "$stat_line" ]]; } 2>/dev/null || continue
      pid=${stat_line%% *}
      [[ "$pid" =~ ^[0-9]+$ && "$stat_line" == *") "* ]] || continue
      rest=${stat_line##*) }
      IFS=' ' read -r state ppid _ <<<"$rest"
      [[ "$ppid" =~ ^[0-9]+$ ]] || continue
      children_of[ppid]="${children_of[ppid]:-}${children_of[ppid]:+ }$pid"
    done
  else
    local ps_out
    ps_out=$(ps -ax -o pid=,ppid= 2>/dev/null || true)
    while read -r pid ppid _; do
      [[ "$pid" =~ ^[0-9]+$ && "$ppid" =~ ^[0-9]+$ ]] || continue
      children_of[ppid]="${children_of[ppid]:-}${children_of[ppid]:+ }$pid"
    done <<<"$ps_out"
  fi
  seen[root]=1
  while (( head < ${#queue[@]} )); do
    cur=${queue[head]}
    head=$((head + 1))
    for child in ${children_of[cur]:-}; do
      [[ -z "${seen[child]:-}" ]] || continue
      seen[child]=1
      queue+=("$child")
      out+=("$child")
    done
  done
  (( ${#out[@]} == 0 )) || printf '%s\n' "${out[@]}"
}

caller_warn_if_stale_launcher() {
  (( CALLER_STALE_WARNED )) && return 0
  local in_container=0 msg
  [[ -f "${DEV_CONTAINER_DOCKERENV_PATH:-/.dockerenv}" ]] && in_container=1
  msg=$(caller_stale_launcher_warning "${CALLER:-}" "${DEV_CONTAINER_CALLER_LIFELINE:-}" "$in_container" "${repo_root:-}")
  if [[ -n "$msg" ]]; then
    CALLER_STALE_WARNED=1
    printf '%s\n' "$msg" >&2
  fi
}

caller_lifeline_start() {
  (( CALLER_LIFELINE_ACTIVE )) && return 0
  caller_lifeline_requested "${DEV_CONTAINER_CALLER_LIFELINE:-}" || return 0

  CALLER_LIFELINE_MAIN_PID=$BASHPID
  CALLER_DEAD_FILE="$STATE/scratch/lifeline-dead.$CALLER_LIFELINE_MAIN_PID"
  mkdir -p "$STATE/scratch"
  rm -f "$CALLER_DEAD_FILE"

  local wfd=""
  exec {CALLER_LIFELINE_SYNC_FD}<&0 {wfd}<&0 0</dev/null

  CALLER_INTERRUPTED=0
  CALLER_LIFELINE_ACTIVE=1
  trap 'CALLER_INTERRUPTED=1' USR1 TERM INT HUP

  (
    trap 'exit 0' TERM INT HUP
    trap - USR1 EXIT
    exec {CALLER_LIFELINE_SYNC_FD}<&-
    while IFS= read -r -u "$wfd" _; do :; done
    : >"$CALLER_DEAD_FILE"
    kill -USR1 "$CALLER_LIFELINE_MAIN_PID" 2>/dev/null || true
  ) </dev/null >/dev/null 2>&1 &
  CALLER_WATCHER_PID=$!
  exec {wfd}<&-
}

caller_lifeline_stop() {
  [[ -z "$CALLER_LIFELINE_MAIN_PID" || "${BASHPID:-$$}" == "$CALLER_LIFELINE_MAIN_PID" ]] || return 0
  CALLER_LIFELINE_ACTIVE=0
  if [[ -n "$CALLER_WATCHER_PID" ]]; then
    kill "$CALLER_WATCHER_PID" 2>/dev/null || true
    wait "$CALLER_WATCHER_PID" 2>/dev/null || true
    CALLER_WATCHER_PID=""
  fi
  if [[ -n "$CALLER_LIFELINE_SYNC_FD" ]]; then
    exec {CALLER_LIFELINE_SYNC_FD}<&- || true
    CALLER_LIFELINE_SYNC_FD=""
  fi
  if [[ -n "$CALLER_DEAD_FILE" ]]; then
    rm -f "$CALLER_DEAD_FILE"
    CALLER_DEAD_FILE=""
  fi
  if (( ! CALLER_ABORTING )); then
    trap - USR1 TERM INT HUP
  fi
}

caller_is_dead() {
  (( CALLER_LIFELINE_ACTIVE )) || return 1
  (( CALLER_INTERRUPTED )) && return 0
  [[ -n "$CALLER_DEAD_FILE" && -e "$CALLER_DEAD_FILE" ]] && return 0
  if [[ -n "$CALLER_LIFELINE_SYNC_FD" ]]; then
    if perl -e 'my $fd=shift; open(my $fh,"<&=",$fd) or exit 0; my $r=""; vec($r,fileno($fh),1)=1; exit(select($r,undef,undef,0)>0 ? 0 : 1)' "$CALLER_LIFELINE_SYNC_FD" 2>/dev/null; then
      return 0
    fi
  fi
  (( CALLER_INTERRUPTED )) && return 0
  [[ -n "$CALLER_DEAD_FILE" && -e "$CALLER_DEAD_FILE" ]] && return 0
  return 1
}

caller_resume_restore() {
  if [[ -n "${RESUME_RESTORING_DIR:-}" && -d "$RESUME_RESTORING_DIR" && -n "${RESUME_ARCHIVE_DIR:-}" && ! -e "$RESUME_ARCHIVE_DIR" ]]; then
    mv "$RESUME_RESTORING_DIR" "$RESUME_ARCHIVE_DIR" 2>/dev/null || true
  fi
  RESUME_RESTORING_DIR=""
  RESUME_ARCHIVE_DIR=""
}

caller_abort() {
  (( CALLER_ABORTING )) && return 0
  CALLER_ABORTING=1
  trap '' USR1 TERM INT HUP
  caller_lifeline_stop
  (( LOCK_HELD )) && lock_release
  if (( AGENT_TOKEN_LOCK_HELD )); then
    rm -f "$AGENT_TOKEN_LOCK"
    AGENT_TOKEN_LOCK_HELD=0
  fi
  if [[ -n "${RESERVED_LEASE:-}" ]]; then
    local abort_dir="${1:-${ACQUIRED_DIR:-}}" abort_sid="${2:-${ACQUIRED_ID:-}}"
    [[ -n "$abort_sid" ]] || abort_sid=$(basename "$RESERVED_LEASE" .json)
    abort_acquire_slot "$abort_dir" "$abort_sid" || true
  fi
  caller_resume_restore
  exit 143
}

caller_abort_if_dead() {
  (( CALLER_LIFELINE_ACTIVE && ! CALLER_ABORTING )) || return 0
  if caller_is_dead; then
    caller_abort "${1:-}" "${2:-}"
  fi
}

caller_sleep() {
  local dur="$1"
  if (( ! CALLER_LIFELINE_ACTIVE || CALLER_ABORTING )); then
    sleep "$dur"
    return 0
  fi
  if (( ! LOCK_HELD )) && { (( CALLER_INTERRUPTED )) || [[ -n "$CALLER_DEAD_FILE" && -e "$CALLER_DEAD_FILE" ]]; }; then
    caller_abort
  fi
  local spid=""
  sleep "$dur" &
  spid=$!
  if (( ! CALLER_INTERRUPTED )) && [[ -z "$CALLER_DEAD_FILE" || ! -e "$CALLER_DEAD_FILE" ]]; then
    wait "$spid" 2>/dev/null || true
  fi
  if kill -0 "$spid" 2>/dev/null; then
    kill "$spid" 2>/dev/null || true
    wait "$spid" 2>/dev/null || true
  fi
  (( LOCK_HELD )) || caller_abort_if_dead
}

caller_kill_tree() {
  local root="$1" p d round
  [[ -n "$root" ]] || return 0
  local -a targets=() seen=()
  seen[root]=1
  while IFS= read -r d; do
    [[ "$d" =~ ^[0-9]+$ && -z "${seen[d]:-}" ]] || continue
    seen[d]=1
    targets+=("$d")
  done < <(descendant_pids_of "$root")
  targets+=("$root")

  kill -TERM "${targets[@]}" 2>/dev/null || true

  for (( round = 0; round < 15; round++ )); do
    local any_alive=0
    for p in "${targets[@]}"; do
      if [[ -d "/proc/$p" ]]; then
        local st=""
        { IFS= read -r -d '' st <"/proc/$p/stat" || [[ -n "$st" ]]; } 2>/dev/null || continue
        if [[ "$st" == *") "* ]]; then
          local r=${st##*) }
          [[ "$r" == Z* ]] && continue
        fi
        any_alive=1
        break
      elif kill -0 "$p" 2>/dev/null; then
        any_alive=1
        break
      fi
    done
    (( any_alive )) || break
    sleep 0.02
  done

  local -a cur_targets=("${targets[@]}")
  for p in "${cur_targets[@]}"; do
    while IFS= read -r d; do
      [[ "$d" =~ ^[0-9]+$ && -z "${seen[d]:-}" ]] || continue
      seen[d]=1
      targets+=("$d")
    done < <(descendant_pids_of "$p")
  done
  kill -KILL "${targets[@]}" 2>/dev/null || true
}

caller_run_interruptible() {
  if (( ! CALLER_LIFELINE_ACTIVE )); then
    "$@"
    return $?
  fi
  caller_abort_if_dead

  (
    CALLER_LIFELINE_ACTIVE=0
    CALLER_WATCHER_PID=""
    CALLER_DEAD_FILE=""
    RESERVED_LEASE=""
    LIVE_LOCK_HELD=0
    RESUME_RESTORING_DIR=""
    RESUME_ARCHIVE_DIR=""
    if [[ -n "$CALLER_LIFELINE_SYNC_FD" ]]; then
      exec {CALLER_LIFELINE_SYNC_FD}<&- || true
      CALLER_LIFELINE_SYNC_FD=""
    fi
    trap '(( LOCK_HELD )) && lock_release; (( AGENT_TOKEN_LOCK_HELD )) && rm -f "$AGENT_TOKEN_LOCK"' EXIT
    trap 'exit 143' TERM INT HUP
    trap - USR1
    "$@"
  ) &
  CALLER_CHILD_PID=$!

  local rc=0
  if (( ! CALLER_INTERRUPTED )) && [[ -z "$CALLER_DEAD_FILE" || ! -e "$CALLER_DEAD_FILE" ]]; then
    while kill -0 "$CALLER_CHILD_PID" 2>/dev/null; do
      rc=0
      wait "$CALLER_CHILD_PID" 2>/dev/null || rc=$?
      if (( CALLER_INTERRUPTED )) || [[ -n "$CALLER_DEAD_FILE" && -e "$CALLER_DEAD_FILE" ]]; then
        break
      fi
      kill -0 "$CALLER_CHILD_PID" 2>/dev/null || break
    done
  fi

  if (( CALLER_INTERRUPTED )) || caller_is_dead; then
    caller_kill_tree "$CALLER_CHILD_PID"
    wait "$CALLER_CHILD_PID" 2>/dev/null || true
    CALLER_CHILD_PID=""
    caller_abort "${ACQUIRED_DIR:-}" "${ACQUIRED_ID:-}"
  fi

  CALLER_CHILD_PID=""
  return "$rc"
}

caller_commit_or_abort() {
  caller_abort_if_dead "${1:-}" "${2:-}"
  caller_lifeline_stop
}
