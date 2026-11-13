# shellcheck shell=bash disable=SC2034,SC2154

report_live_slot() {
  local dir="$1" name="$2" sid="$3"
  local lease; lease=$(lease_file_of "$sid")
  local holder; holder=$(lease_field "$lease" holder)
  local ssh_port; ssh_port=$(lease_field "$lease" ssh_port)
  local key; key=$(lease_field "$lease" key)

  echo "slot '$name' is live:"
  echo "  holder: $holder"
  echo "  ssh -i $key -p $ssh_port node@localhost"
  cat <<JSON
  {
    "id": "$sid",
    "name": "$SLOT_PROJECT_PREFIX slot: $name",
    "sshHost": "node@localhost",
    "sshPort": $ssh_port,
    "sshIdentityFile": "$key",
    "startDirectory": "/workspace"
  }
JSON
}

find_archive_dir() {
  local project="$1" target="$2"
  rescue_legacy_guard
  local rescue_dir="$STATE/rescue/$(rescue_name)"
  local candidate="$rescue_dir/$target"
  if [[ -d "$candidate" && ! "$target" == .* ]]; then
    echo "$candidate"
    return 0
  fi

  if [[ "$target" == "${project}-"* ]]; then
    local stripped="${target#"${project}-"}"
    candidate="$rescue_dir/$stripped"
    if [[ -d "$candidate" && ! "$stripped" == .* ]]; then
      echo "$candidate"
      return 0
    fi
  fi

  local f adir recorded branches match
  for f in "$rescue_dir"/*/meta.json; do
    [[ -f "$f" ]] || continue
    adir=$(dirname "$f")
    [[ "$(basename "$adir")" == .* ]] && continue

    recorded=$(jq -r '.lease // empty' "$f" 2>/dev/null) || recorded=""
    if [[ "$recorded" == "$target" || "$recorded" == "${target#"${project}-"}" ]]; then
      echo "$adir"
      return 0
    fi

    match=$(jq -r --arg t "$target" 'if .branches and (.branches | index($t)) then "yes" else empty end' "$f" 2>/dev/null) || match=""
    if [[ "$match" == "yes" ]]; then
      echo "$adir"
      return 0
    fi
  done
  return 1
}

cmd_resume() {
  [[ $# -ge 1 ]] || die "Usage: $CALLER resume <branch or lease>"
  local target="$1"
  shift

  activity_refresh

  local check_name="$target"
  [[ "$check_name" == "${SLOT_PROJECT_PREFIX}-"* ]] && check_name="${check_name#"${SLOT_PROJECT_PREFIX}-"}"

  local check_sid="${SLOT_PROJECT_PREFIX}-${check_name}"
  local check_dir="$slots_dir/$check_name"
  [[ -d "$check_dir" ]] || check_dir="${DOCKER_STUB_STATE:+$DOCKER_STUB_STATE/volumes/${check_sid}-workspace}"
  if [[ -n "$check_dir" && -d "$check_dir" ]]; then
    local check_lease; check_lease=$(lease_file_of "$check_sid")
    if [[ -f "$check_lease" ]] && lease_is_live "$check_lease"; then
      report_live_slot "$check_dir" "$check_name" "$check_sid"
      return 0
    fi
  fi

  local sid
  for sid in $(our_slot_ids); do
    local lease; lease=$(lease_file_of "$sid")
    [[ -f "$lease" ]] && lease_is_live "$lease" || continue
    local sname; sname=$(slot_name_of "$sid")
    local d="$slots_dir/$sname"
    [[ -d "$d" ]] || d="${DOCKER_STUB_STATE:+$DOCKER_STUB_STATE/volumes/${sid}-workspace}"
    [[ -n "$d" && -d "$d" ]] || continue
    local cur_branch; cur_branch=$(git -C "$d" rev-parse --abbrev-ref HEAD 2>/dev/null || true)
    if [[ "$cur_branch" == "$target" ]]; then
      report_live_slot "$d" "$sname" "$sid"
      return 0
    fi
  done

  local archive_dir
  if archive_dir=$(find_archive_dir "$SLOT_PROJECT_PREFIX" "$target"); then
    local meta="$archive_dir/meta.json"
    local lease_name=""
    [[ -f "$meta" ]] && lease_name=$(jq -r '.lease // empty' "$meta" 2>/dev/null) || lease_name=""
    [[ -n "$lease_name" ]] || lease_name=$(basename "$archive_dir")

    local restoring_dir="$STATE/rescue/$(rescue_name)/.restoring-$lease_name"
    rm -rf "$restoring_dir" 2>/dev/null || true
    mv "$archive_dir" "$restoring_dir" || die "failed to mark archive for restoration"

    meta="$restoring_dir/meta.json"
    local branch_to_checkout=""
    if [[ -f "$meta" ]]; then
      local in_branches
      in_branches=$(jq -r --arg t "$target" 'if .branches and (.branches | index($t)) then "yes" else empty end' "$meta" 2>/dev/null) || in_branches=""
      if [[ "$in_branches" == "yes" ]]; then
        branch_to_checkout="$target"
      else
        branch_to_checkout=$(jq -r '.branches[0] // empty' "$meta" 2>/dev/null) || branch_to_checkout=""
      fi
    fi

    echo "? allocating slot to resume $target"
    cmd_acquire --for "resume" || {
      mv "$restoring_dir" "$archive_dir" 2>/dev/null || true
      die "could not allocate slot to resume $target"
    }

    local new_dir="${ACQUIRED_DIR:-$SLOT_DIR}"
    [[ -n "$new_dir" && -d "$new_dir" ]] || new_dir="${DOCKER_STUB_STATE:+$DOCKER_STUB_STATE/volumes/${ACQUIRED_ID}-workspace}"
    if [[ -f "$restoring_dir/commits.bundle" ]]; then
      git -C "$new_dir" fetch "$restoring_dir/commits.bundle" 'refs/heads/*:refs/heads/*' >/dev/null 2>&1 || true
    fi

    if [[ -n "$branch_to_checkout" ]]; then
      git -C "$new_dir" checkout --quiet "$branch_to_checkout" 2>/dev/null || true
    fi

    if [[ -f "$restoring_dir/changes.patch" ]]; then
      git -C "$new_dir" apply --whitespace=nowarn "$restoring_dir/changes.patch" 2>/dev/null || true
    fi

    if [[ -f "$restoring_dir/new-files.tar" ]]; then
      tar -xf "$restoring_dir/new-files.tar" -C "$new_dir" 2>/dev/null || true
    fi

    rm -rf "$restoring_dir" 2>/dev/null || true
    return 0
  fi

  local remote_exists=0
  if git -C "$repo_root" rev-parse --verify --quiet "refs/remotes/origin/$target" >/dev/null 2>&1      || git -C "$repo_root" show-ref --verify --quiet "refs/heads/$target" >/dev/null 2>&1; then
    remote_exists=1
  fi

  if (( remote_exists )); then
    echo "? allocating slot for remote branch $target"
    cmd_acquire --for "resume" || die "could not allocate slot for branch $target"
    local new_dir="$ACQUIRED_DIR"
    [[ -n "$new_dir" && -d "$new_dir" ]] || new_dir="${DOCKER_STUB_STATE:+$DOCKER_STUB_STATE/volumes/${ACQUIRED_ID}-workspace}"
    git -C "$new_dir" fetch --quiet origin "refs/heads/$target:refs/remotes/origin/$target" 2>/dev/null || true
    git -C "$new_dir" checkout --quiet -B "$target" "refs/remotes/origin/$target" 2>/dev/null \
      || git -C "$new_dir" checkout --quiet "$target" 2>/dev/null || true
    return 0
  fi

  die "cannot resume '$target': no live slot, no local archive, and branch '$target' does not exist on remote."
}
