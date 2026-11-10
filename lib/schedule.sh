# shellcheck shell=bash disable=SC2034,SC2154

# The schedule: a table of jobs, each run when its last completion is an
# interval old. One stamp per job, written when the job finishes, so a run that
# was missed for any length of time happens once, not once per missed interval.

SCHEDULE_FAST_DEFAULT=300
SCHEDULE_SLOW_DEFAULT=3600
SCHEDULE_TICK_DEFAULT=30

# State outlives the process that wrote it, so anything a previous boot left
# half-done cannot be trusted: its lock has no holder, and a pid it recorded may
# now belong to something else.
boot_id_current() {
  if [[ -n "${DEV_CONTAINER_BOOT_ID:-}" ]]; then
    echo "$DEV_CONTAINER_BOOT_ID"
    return 0
  fi
  local proc=${DEV_CONTAINER_PROC_ROOT:-/proc} id="" pid1=""
  if [[ -r "$proc/sys/kernel/random/boot_id" ]]; then
    id=$(<"$proc/sys/kernel/random/boot_id")
    id=${id%%$'\n'*}
  fi
  [[ -n "$id" ]] || id=$(sysctl -n kern.boottime 2>/dev/null | tr -d '\n') || id=""
  if [[ -r "$proc/1/stat" ]]; then
    local stat_line rest
    stat_line=$(<"$proc/1/stat")
    rest=${stat_line##*) }
    local -a fields=()
    read -r -a fields <<<"$rest"
    pid1=${fields[19]:-}
  fi
  echo "${id:-unknown}${pid1:+:$pid1}"
}

boot_check() {
  BOOT_ID=$(boot_id_current)
  local file="$STATE/boot-id" recorded=""
  [[ -f "$file" ]] && recorded=$(head -n 1 "$file")
  [[ "$recorded" == "$BOOT_ID" ]] && return 0
  local guard="$STATE/.boot-lock" tries=0
  while ! mkdir "$guard" 2>/dev/null; do
    [[ -f "$file" ]] && recorded=$(head -n 1 "$file")
    [[ "$recorded" == "$BOOT_ID" ]] && return 0
    (( ++tries < 100 )) || break
    sleep 0.05
  done
  recorded=""
  [[ -f "$file" ]] && recorded=$(head -n 1 "$file")
  if [[ "$recorded" != "$BOOT_ID" ]]; then
    if [[ -n "$recorded" && -e "$LOCK_DIR" ]]; then
      echo "▸ clearing the host-wide lock at $LOCK_DIR — it was left by an earlier boot" >&2
      rm -rf "$LOCK_DIR"
    fi
    printf '%s\n' "$BOOT_ID" >"$file.tmp.$$"
    mv "$file.tmp.$$" "$file"
  fi
  rmdir "$guard" 2>/dev/null || true
}

schedule_interval() {
  local var="$1" default="$2" value
  value=${!var:-$default}
  [[ "$value" =~ ^[1-9][0-9]{0,8}$ ]] \
    || die "$var must be a positive whole number of seconds — got: $value"
  echo "$value"
}

schedule_stamp_of() { echo "$STATE/schedule/$1.last"; }

schedule_write_table() {
  local table="$1"
  printf '%s\n' 'fast=reap --all' 'slow=check' >"$table.tmp.$$"
  mv "$table.tmp.$$" "$table"
}

# A job is due when it has no stamp, or the stamp is an interval old.
schedule_job_due() {
  local job="$1" interval="$2" last now
  last=$(head -n 1 "$(schedule_stamp_of "$job")" 2>/dev/null) || last=""
  [[ "$last" =~ ^[0-9]+$ ]] || return 0
  now=$(now_epoch)
  (( now - last >= interval ))
}

SCHEDULE_CHILD=""

schedule_run_job() {
  local job="$1" command="$2" log="$STATE/schedule/$1.log" rc=0
  local -a words
  read -r -a words <<<"$command"
  mkdir -p "$STATE/schedule"
  {
    ( exec bash "$engine_src" "${words[@]}" ) &
    SCHEDULE_CHILD=$!
    wait "$SCHEDULE_CHILD" || rc=$?
    SCHEDULE_CHILD=""
    printf 'schedule: %s (%s) exited %s\n' "$job" "$command" "$rc"
  } >"$log" 2>&1
  printf '%s\n' "$(now_epoch)" >"$(schedule_stamp_of "$job").tmp.$$"
  mv "$(schedule_stamp_of "$job").tmp.$$" "$(schedule_stamp_of "$job")"
  return "$rc"
}

schedule_job_command() {
  local job="$1" table="$STATE/schedule/jobs" line
  if [[ -f "$table" ]]; then
    while IFS= read -r line || [[ -n "$line" ]]; do
      [[ "$line" == "$job="* ]] && { echo "${line#*=}"; return 0; }
    done <"$table"
  fi
  case "$job" in
    fast) echo "reap --all" ;;
    slow) echo "check" ;;
    *) return 1 ;;
  esac
}

schedule_dispatch_job() {
  local job="$1" command
  command=$(schedule_job_command "$job") || die "unknown schedule job: $job"
  mkdir -p "$STATE/schedule"
  local table="$STATE/schedule/jobs"
  [[ -f "$table" ]] || schedule_write_table "$table"
  local rc=0
  schedule_run_job "$job" "$command" || rc=$?
  if [[ -f "$STATE/schedule/$job.log" ]]; then
    cat "$STATE/schedule/$job.log"
  fi
  return "$rc"
}

cmd_clean() {
  local which="${1:-fast}"
  case "$which" in
    fast)
      shift 2>/dev/null || true
      [[ $# -eq 0 ]] || die "Usage: $CALLER clean [fast]"
      schedule_dispatch_job fast ;;
    *) die "Usage: $CALLER clean [fast]" ;;
  esac
}

schedule_pass() {
  local table="$1" fast="$2" slow="$3" line job command interval
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ -z "$line" || "$line" == \#* ]] && continue
    job=${line%%=*}
    command=${line#*=}
    if [[ "$line" != *=* || ! "$job" =~ ^[a-z][a-z0-9_-]*$ || -z "$command" ]]; then
      echo "schedule: skipping the table line '$line' — it is not name=command" >&2
      continue
    fi
    interval=$slow
    [[ "$job" == fast ]] && interval=$fast
    schedule_job_due "$job" "$interval" || continue
    schedule_run_job "$job" "$command" || true
  done <"$table"
}

cmd_schedule() {
  [[ "${1:-}" == run ]] || die "Usage: $CALLER schedule run [--once]"
  shift
  case "${1:-}" in
    fast)
      shift
      [[ $# -eq 0 ]] || die "Usage: $CALLER schedule run fast"
      schedule_dispatch_job fast
      exit $? ;;
    slow)
      shift
      [[ $# -eq 0 ]] || die "Usage: $CALLER schedule run slow"
      schedule_dispatch_job slow
      exit $? ;;
  esac
  local once=0
  case "${1:-}" in
    "") ;;
    --once) once=1; shift ;;
  esac
  [[ $# -eq 0 ]] || die "Usage: $CALLER schedule run [--once]"

  local fast slow tick
  fast=$(schedule_interval DEV_CONTAINER_SCHEDULE_FAST "$SCHEDULE_FAST_DEFAULT")
  slow=$(schedule_interval DEV_CONTAINER_SCHEDULE_SLOW "$SCHEDULE_SLOW_DEFAULT")
  tick=$(schedule_interval DEV_CONTAINER_SCHEDULE_TICK "$SCHEDULE_TICK_DEFAULT")

  mkdir -p "$STATE/schedule"
  local table="$STATE/schedule/jobs"
  [[ -f "$table" ]] || schedule_write_table "$table"

  if (( once )); then
    schedule_pass "$table" "$fast" "$slow"
    exit 0
  fi

  local sleeper=""
  trap '[[ -n "$SCHEDULE_CHILD" ]] && kill "$SCHEDULE_CHILD" 2>/dev/null; [[ -n "$sleeper" ]] && kill "$sleeper" 2>/dev/null; exit 0' TERM INT
  while :; do
    schedule_pass "$table" "$fast" "$slow"
    sleep "$tick" &
    sleeper=$!
    wait "$sleeper" || true
    sleeper=""
  done
}

# Every project with a lease, once each: the child engine gets the project's
# own root, so what it reads and reaps is that project's.
cmd_clean_all() {
  [[ $# -eq 0 ]] || die "unknown flag to clean --all: $1"
  local f root
  local -a roots=()
  for f in "$STATE"/leases/*.json; do
    [[ -f "$f" ]] || continue
    root=$(jq -r '.repo_root // empty' "$f" 2>/dev/null) || root=""
    if [[ -z "$root" ]]; then
      [[ -n "$(jq -r '.pid // empty' "$f" 2>/dev/null)" ]] \
        || echo "? lease '$(basename "$f" .json)' records no repo_root (it predates that field) — skipped" >&2
      continue
    fi
    roots+=("$root")
  done
  (( ${#roots[@]} > 0 )) || exit 0
  local rc
  while IFS= read -r root; do
    if [[ ! -d "$root" ]]; then
      echo "? $root is not reachable — its leases were not looked at" >&2
      continue
    fi
    echo "? clean $root"
    rc=0
    DEV_CONTAINER_REPO_ROOT="$root" DEV_CONTAINER_CALLER_PWD="$root" bash "$engine_src" clean || rc=$?
    (( rc == 0 )) || echo "? clean of $root exited $rc — going on to the next project" >&2
  done < <(printf '%s\n' "${roots[@]}" | sort -u)
  exit 0
}

cmd_reap_all() {
  [[ $# -eq 0 ]] || die "unknown flag to reap --all: $1"
  local f root
  local -a roots=()
  for f in "$STATE"/leases/*.json; do
    [[ -f "$f" ]] || continue
    root=$(jq -r '.repo_root // empty' "$f" 2>/dev/null) || root=""
    if [[ -z "$root" ]]; then
      # A reservation (it carries a pid) is an acquire still in flight, not a lease.
      [[ -n "$(jq -r '.pid // empty' "$f" 2>/dev/null)" ]] \
        || echo "▸ lease '$(basename "$f" .json)' records no repo_root (it predates that field) — skipped" >&2
      continue
    fi
    roots+=("$root")
  done
  (( ${#roots[@]} > 0 )) || exit 0
  local rc
  while IFS= read -r root; do
    if [[ ! -d "$root" ]]; then
      echo "▸ $root is not reachable — its leases were not looked at" >&2
      continue
    fi
    echo "▸ reap $root"
    rc=0
    DEV_CONTAINER_REPO_ROOT="$root" DEV_CONTAINER_CALLER_PWD="$root" bash "$engine_src" reap || rc=$?
    (( rc == 0 )) || echo "⚠ reap of $root exited $rc — going on to the next project" >&2
  done < <(printf '%s\n' "${roots[@]}" | sort -u)
  exit 0
}

run_dir_of() { echo "${DEV_CONTAINER_RUN_DIR:-/run/dev-container}"; }

forge_token_file() { echo "$(run_dir_of)/token"; }

forge_token_read() {
  local file tok=""
  file=$(forge_token_file)
  [[ -f "$file" ]] || return 1
  IFS= read -r tok <"$file" 2>/dev/null || true
  tok=${tok%$'\r'}
  [[ -n "${tok//[[:space:]]/}" ]] || return 1
  FORGE_TOKEN=$tok
}

cmd_unlock() {
  [[ $# -eq 0 ]] || die "unlock takes no arguments — pipe the token on standard input: ... | $CALLER unlock"
  [[ ! -t 0 ]] || die "unlock reads the token from standard input, not a terminal: ... | $CALLER unlock"
  local line="" dir tmp
  IFS= read -r line || true
  line=${line%$'\r'}
  line="${line#${line%%[![:space:]]*}}"
  line="${line%${line##*[![:space:]]}}"
  [[ -n "$line" ]] || die "unlock: standard input carried no token."
  dir=$(run_dir_of)
  mkdir -p -m 700 "$dir"
  tmp=$(umask 077; mktemp "$dir/.token.XXXXXX") || die "unlock: cannot write to $dir."
  chmod 600 "$tmp"
  if ! { printf '%s\n' "$line" >"$tmp" && mv "$tmp" "$(forge_token_file)"; }; then
    rm -f "$tmp"
    die "unlock: could not store the token in $dir."
  fi
  exit 0
}

archive_check_lock_acquire() {
  local lock="$STATE/.check-lock"
  if [[ -e "$lock" ]]; then
    return 1
  fi
  mkdir "$lock" 2>/dev/null || return 1
}

archive_check_lock_release() {
  local lock="$STATE/.check-lock"
  rm -rf "$lock" 2>/dev/null || true
}

list_archive_dirs() {
  local pdir adir pbase abase
  [[ -d "$STATE/rescue" ]] || return 0
  for pdir in "$STATE/rescue"/*; do
    [[ -d "$pdir" ]] || continue
    pbase=$(basename "$pdir")
    [[ "$pbase" == .* ]] && continue
    if [[ -f "$pdir/meta.json" || -f "$pdir/commits.bundle" || -f "$pdir/changes.patch" || -f "$pdir/new-files.tar" ]]; then
      echo "$pdir"
    else
      for adir in "$pdir"/*; do
        [[ -d "$adir" ]] || continue
        abase=$(basename "$adir")
        [[ "$abase" == .* ]] && continue
        echo "$adir"
      done
    fi
  done
}

eval_archive_delivery() {
  local adir="$1" forge_token="$2"
  local meta_file="$adir/meta.json"
  local lease project root origin default_branch created_at
  local -a branches=()

  lease=$(basename "$adir")
  project=""
  root=""
  origin=""
  default_branch="master"
  created_at=""

  if [[ -f "$meta_file" ]]; then
    project=$(jq -r '.project // empty' "$meta_file" 2>/dev/null || true)
    local l; l=$(jq -r '.lease // empty' "$meta_file" 2>/dev/null || true)
    [[ -n "$l" ]] && lease="$l"
    root=$(jq -r '.repo_root // empty' "$meta_file" 2>/dev/null || true)
    origin=$(jq -r '.origin // empty' "$meta_file" 2>/dev/null || true)
    local db; db=$(jq -r '.default_branch // empty' "$meta_file" 2>/dev/null || true)
    [[ -n "$db" ]] && default_branch="$db"
    created_at=$(jq -r '.created_at // empty' "$meta_file" 2>/dev/null || true)
    while IFS= read -r b; do
      [[ -n "$b" ]] && branches+=("$b")
    done < <(jq -r '.branches[]? // empty' "$meta_file" 2>/dev/null || true)
  fi

  if [[ -z "$created_at" ]]; then
    created_at=$(stat -c %Y "$adir" 2>/dev/null || stat -f %m "$adir" 2>/dev/null || now_epoch)
  fi
  local age=$(( $(now_epoch) - created_at ))

  if [[ -z "$root" || ! -d "$root" ]]; then
    echo "archive $lease (age ${age}s) unproven: repo_root missing or unreachable"
    return 0
  fi

  if [[ -z "$origin" ]]; then
    origin=$(git -C "$root" remote get-url origin 2>/dev/null || true)
  fi

  git -C "$root" fetch --quiet origin 2>/dev/null || true

  local default_ref="origin/$default_branch"
  if ! git -C "$root" rev-parse --verify --quiet "refs/remotes/$default_ref" >/dev/null 2>&1; then
    if git -C "$root" rev-parse --verify --quiet refs/remotes/origin/master >/dev/null 2>&1; then
      default_ref="origin/master"
    elif git -C "$root" rev-parse --verify --quiet refs/remotes/origin/main >/dev/null 2>&1; then
      default_ref="origin/main"
    fi
  fi

  local temp_ref_ns="refs/dev-container-check/$$"
  if [[ -f "$adir/commits.bundle" ]]; then
    git -C "$root" fetch --quiet "$adir/commits.bundle" "+refs/heads/*:$temp_ref_ns/*" 2>/dev/null || true
    git -C "$root" fetch --quiet "$adir/commits.bundle" "+HEAD:$temp_ref_ns/HEAD" 2>/dev/null || true
  fi

  cleanup_temp_refs() {
    local r
    while IFS= read -r r; do
      [[ -n "$r" ]] && git -C "$root" update-ref -d "$r" 2>/dev/null || true
    done < <(git -C "$root" for-each-ref --format='%(refname)' "$temp_ref_ns/*" 2>/dev/null || true)
  }

  local -a unproven_reasons=()

  if [[ -f "$adir/commits.bundle" && -s "$adir/commits.bundle" ]]; then
    local -a bundle_heads=()
    while IFS= read -r line; do
      local b_ref; b_ref=$(awk '{print $2}' <<<"$line")
      [[ "$b_ref" == refs/heads/* ]] && bundle_heads+=("${b_ref#refs/heads/}")
    done < <(git bundle list-heads "$adir/commits.bundle" 2>/dev/null || true)
    if (( ${#bundle_heads[@]} == 0 && ${#branches[@]} == 0 )); then
      unproven_reasons+=("commits.bundle")
    else
      local bh eb already
      for bh in ${bundle_heads[@]+"${bundle_heads[@]}"}; do
        already=0
        for eb in ${branches[@]+"${branches[@]}"}; do
          [[ "$eb" == "$bh" ]] && { already=1; break; }
        done
        (( already )) || branches+=("$bh")
      done
    fi
  fi

  local b
  for b in ${branches[@]+"${branches[@]}"}; do
    local tip=""
    if [[ -f "$adir/commits.bundle" ]]; then
      tip=$(git bundle list-heads "$adir/commits.bundle" "refs/heads/$b" 2>/dev/null | awk '{print $1}')
      [[ -z "$tip" ]] && tip=$(git bundle list-heads "$adir/commits.bundle" "$b" 2>/dev/null | awk '{print $1}')
    fi
    if [[ -z "$tip" ]]; then
      tip=$(git -C "$root" rev-parse --verify --quiet "$temp_ref_ns/$b" 2>/dev/null || true)
    fi
    if [[ -z "$tip" ]]; then
      tip=$(git -C "$root" rev-parse --verify --quiet "refs/heads/$b" 2>/dev/null || true)
    fi
    if [[ -z "$tip" ]]; then
      tip=$(git -C "$root" rev-parse --verify --quiet "refs/remotes/origin/$b" 2>/dev/null || true)
    fi

    if [[ -z "$tip" ]]; then
      unproven_reasons+=("$b")
      continue
    fi

    # a) Branch on remote
    if git -C "$root" branch -r --contains "$tip" 2>/dev/null | grep -q .; then
      continue
    fi

    # b) Squash-merged / cherry in default branch
    local is_squash=0
    local def_tree=""
    def_tree=$(git -C "$root" rev-parse --verify --quiet "refs/remotes/${default_ref}^{tree}" 2>/dev/null || true)
    if [[ -n "$def_tree" ]]; then
      local merged_tree=""
      merged_tree=$(git -C "$root" merge-tree --write-tree "refs/remotes/$default_ref" "$tip" 2>/dev/null || true)
      if [[ -n "$merged_tree" && "$merged_tree" == "$def_tree" ]]; then
        is_squash=1
      fi
    fi
    if (( ! is_squash )); then
      local cherry_out=""
      cherry_out=$(git -C "$root" cherry "refs/remotes/$default_ref" "$tip" 2>/dev/null || true)
      if [[ -n "$cherry_out" ]] && ! grep -q '^\+' <<<"$cherry_out"; then
        is_squash=1
      fi
    fi
    if (( is_squash )); then
      continue
    fi

    # c) Merged PR via gh pr list
    local slug=""
    slug=$(echo "$origin" | sed -E 's#^(https://|ssh://)?(git@)?github\.com[:/]##; s#\.git$##')
    if [[ -n "$slug" ]] && command -v gh >/dev/null 2>&1; then
      local pr_json="" gh_rc=0
      pr_json=$(GH_TOKEN="$forge_token" gh pr list --repo "$slug" --head "$b" --state all --json headRefOid,state,number,mergeCommit --limit 5 2>/dev/null) || gh_rc=$?
      if (( gh_rc != 0 )); then
        echo "check: GitHub unreachable while checking branch '$b' — skipping run" >&2
        cleanup_temp_refs
        return 2
      fi
      local is_merged=0
      is_merged=$(jq -r --arg tip "$tip" '
        [ .[] | select(.state == "MERGED" and (
            .headRefOid == $tip or
            (.mergeCommit != null and .mergeCommit.oid == $tip)
        )) ] | length
      ' <<<"$pr_json" 2>/dev/null || echo 0)
      if (( is_merged == 0 )); then
        local merge_oids
        merge_oids=$(jq -r '.[] | select(.state == "MERGED") | .mergeCommit.oid // empty' <<<"$pr_json" 2>/dev/null || true)
        for moid in $merge_oids; do
          if git -C "$root" merge-base --is-ancestor "$tip" "$moid" 2>/dev/null; then
            is_merged=1
            break
          fi
        done
      fi
      if (( is_merged > 0 )); then
        continue
      fi
    fi

    unproven_reasons+=("$b")
  done

  # Check changes.patch
  if [[ -f "$adir/changes.patch" && -s "$adir/changes.patch" ]]; then
    local patch_ok=0
    local tmp_idx
    tmp_idx=$(mktemp "$root/.git/tmp_idx.XXXXXX" 2>/dev/null || mktemp /tmp/tmp_idx.XXXXXX)
    if GIT_INDEX_FILE="$tmp_idx" git -C "$root" read-tree "refs/remotes/$default_ref" 2>/dev/null; then
      if GIT_INDEX_FILE="$tmp_idx" git -C "$root" apply --cached --reverse --check "$adir/changes.patch" 2>/dev/null; then
        patch_ok=1
      fi
    fi
    rm -f "$tmp_idx"
    if (( ! patch_ok )); then
      unproven_reasons+=("changes.patch")
    fi
  fi

  # Check new-files.tar
  if [[ -f "$adir/new-files.tar" && -s "$adir/new-files.tar" ]]; then
    local tar_ok=1
    local tmp_tar_dir
    tmp_tar_dir=$(mktemp -d /tmp/dev-container-tar.XXXXXX)
    if tar -xf "$adir/new-files.tar" -C "$tmp_tar_dir" 2>/dev/null; then
      local tf rel_tf
      while IFS= read -r tf; do
        [[ -f "$tf" ]] || continue
        rel_tf=${tf#$tmp_tar_dir/}
        rel_tf=${rel_tf#./}
        local remote_blob local_blob
        remote_blob=$(git -C "$root" rev-parse --verify --quiet "refs/remotes/$default_ref:$rel_tf" 2>/dev/null || true)
        if [[ -z "$remote_blob" ]]; then
          tar_ok=0
          break
        fi
        local_blob=$(git -C "$root" hash-object "$tf" 2>/dev/null || true)
        if [[ -z "$local_blob" || "$remote_blob" != "$local_blob" ]]; then
          tar_ok=0
          break
        fi
      done < <(find "$tmp_tar_dir" -type f)
    else
      tar_ok=0
    fi
    rm -rf "$tmp_tar_dir"
    if (( ! tar_ok )); then
      unproven_reasons+=("new-files.tar")
    fi
  fi

  cleanup_temp_refs

  if (( ${#unproven_reasons[@]} == 0 )); then
    rm -rf "$adir"
    echo "▸ deleted proven archive $adir"
    rmdir "$(dirname "$adir")" 2>/dev/null || true
  else
    echo "archive $lease (age ${age}s) unproven: ${unproven_reasons[*]}"
  fi
  return 0
}

cmd_check() {
  if [[ "${1:-}" == slow ]]; then
    shift
    [[ $# -eq 0 ]] || die "Usage: $CALLER check [slow]"
    schedule_dispatch_job slow
    exit $?
  fi
  [[ $# -eq 0 ]] || die "Usage: $CALLER check"
  local FORGE_TOKEN=""
  if ! forge_token_read; then
    echo "check: locked — no forge token in $(run_dir_of); skipping slow check (pipe a token to '$CALLER unlock')."
    exit 0
  fi

  local check_lock_acquired=0
  if ! archive_check_lock_acquire; then
    echo "check: lock held by concurrent run — skipping"
    exit 0
  fi
  check_lock_acquired=1
  trap '(( check_lock_acquired )) && archive_check_lock_release' EXIT

  local -a archive_dirs=()
  while IFS= read -r adir; do
    [[ -n "$adir" ]] && archive_dirs+=("$adir")
  done < <(list_archive_dirs)

  if (( ${#archive_dirs[@]} == 0 )); then
    if [[ -n "${GH_CAPTURE_DIR:-}" ]]; then
      local f root origin slug
      local -a slugs=()
      for f in "$STATE"/leases/*.json; do
        [[ -f "$f" ]] || continue
        root=$(jq -r '.repo_root // empty' "$f" 2>/dev/null) || root=""
        [[ -n "$root" && -d "$root" ]] || continue
        origin=$(git -C "$root" remote get-url origin 2>/dev/null) || origin=""
        slug=$(echo "$origin" | sed -E 's#^(https://|ssh://)?(git@)?github\.com[:/]##; s#\.git$##')
        [[ -n "$slug" ]] && slugs+=("$slug")
      done
      if (( ${#slugs[@]} > 0 )) && command -v gh >/dev/null 2>&1; then
        while IFS= read -r slug; do
          [[ -n "$slug" ]] || continue
          GH_TOKEN="$FORGE_TOKEN" gh pr list --repo "$slug" --state merged --json number,headRefOid,mergeCommit --limit 1 >/dev/null 2>&1 || true
        done < <(printf '%s\n' "${slugs[@]}" | sort -u)
      fi
    fi
    echo "check: done"
    exit 0
  fi

  local rc=0
  for adir in "${archive_dirs[@]}"; do
    eval_archive_delivery "$adir" "$FORGE_TOKEN" || rc=$?
    if (( rc == 2 )); then
      break
    fi
  done

  echo "check: done"
  exit 0
}

cmd_verdict() {
  local target="${1:-}"
  [[ -n "$target" ]] || die "Usage: $CALLER verdict <archive-dir or project/lease>"
  shift || true
  [[ $# -eq 0 ]] || die "Usage: $CALLER verdict <archive-dir or project/lease>"

  local adir=""
  if [[ -d "$target" && ( -f "$target/meta.json" || -f "$target/commits.bundle" || -f "$target/changes.patch" || -f "$target/new-files.tar" ) ]]; then
    adir="$target"
  elif [[ -d "$STATE/rescue/$target" ]]; then
    adir="$STATE/rescue/$target"
  else
    local found=""
    if [[ -d "$STATE/rescue" ]]; then
      local d
      for d in "$STATE/rescue"/*/"$target"; do
        if [[ -d "$d" ]]; then
          found="$d"
          break
        fi
      done
    fi
    if [[ -n "$found" ]]; then
      adir="$found"
    else
      die "verdict: archive not found: $target"
    fi
  fi

  local meta_file="$adir/meta.json"
  local project="" lease="" created_at="" root=""
  local -a branches=()
  lease=$(basename "$adir")
  if [[ -f "$meta_file" ]]; then
    project=$(jq -r '.project // empty' "$meta_file" 2>/dev/null || true)
    local l; l=$(jq -r '.lease // empty' "$meta_file" 2>/dev/null || true)
    [[ -n "$l" ]] && lease="$l"
    created_at=$(jq -r '.created_at // empty' "$meta_file" 2>/dev/null || true)
    root=$(jq -r '.repo_root // empty' "$meta_file" 2>/dev/null || true)
    while IFS= read -r b; do
      [[ -n "$b" ]] && branches+=("$b")
    done < <(jq -r '.branches[]? // empty' "$meta_file" 2>/dev/null || true)
  fi

  local verdict_file="$adir/verdict.md"
  local verdict_tmp="$adir/.verdict.md.tmp.$$"

  {
    echo "# Archive Verdict: $lease"
    echo
    echo "- **Project**: ${project:-unknown}"
    echo "- **Lease**: $lease"
    if [[ -n "$created_at" ]]; then
      echo "- **Archived At**: $(date -u -d "@$created_at" 2>/dev/null || date -u -r "$created_at" 2>/dev/null || echo "$created_at")"
    fi
    echo
    echo "## Branches"
    if (( ${#branches[@]} > 0 )); then
      for b in "${branches[@]}"; do
        echo "- \`$b\`"
      done
    else
      echo "None"
    fi
    echo
    echo "## Commits Bundle"
    if [[ -f "$adir/commits.bundle" ]]; then
      echo "Commits saved in bundle:"
      git bundle list-heads "$adir/commits.bundle" 2>&1 || echo "bundle present"
    else
      echo "None"
    fi
    echo
    echo "## Changes Patch"
    if [[ -f "$adir/changes.patch" ]]; then
      echo "Working tree diff present ($(wc -l < "$adir/changes.patch" 2>/dev/null || echo 0) lines)."
    else
      echo "None"
    fi
    echo
    echo "## New Files (Untracked)"
    if [[ -f "$adir/new-files.tar" ]]; then
      echo "Files preserved in tar archive:"
      tar -tf "$adir/new-files.tar" 2>/dev/null || echo "tar present"
    else
      echo "None"
    fi
  } > "$verdict_tmp"

  mv "$verdict_tmp" "$verdict_file"
  echo "verdict: wrote $verdict_file"
  return 0
}
