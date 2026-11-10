# shellcheck shell=bash disable=SC2034,SC2154

rescue_bundle_path() {
  local sid="$1"
  printf '%s/rescue/%s-%s-%s.bundle' "$STATE" "$sid" "$(date +%Y%m%d-%H%M%S)" "$$"
}

prune_delivered_branches() {
  local dir="$1" sid="$2" docs="$1/docs/repo"
  local -a slot_prunable=() docs_prunable=()
  collect_branch_statuses "$dir"
  (( ${#PRUNABLE_BRANCHES[@]} == 0 )) || slot_prunable=("${PRUNABLE_BRANCHES[@]}")
  docs_repo_is_clean "$dir" || true
  (( ${#DOCS_PRUNABLE_BRANCHES[@]} == 0 )) || docs_prunable=("${DOCS_PRUNABLE_BRANCHES[@]}")
  (( ${#slot_prunable[@]} + ${#docs_prunable[@]} > 0 )) || return 0

  lock_acquire
  if [[ -f "$(lease_file_of "$sid")" || -f "$(parked_file_of "$sid")" ]]; then
    lock_release
    return 0
  fi
  local entry
  if (( ${#slot_prunable[@]} > 0 )); then
    for entry in "${slot_prunable[@]}"; do
      # shellcheck disable=SC2086
      delete_delivered_branch "$dir" "$sid" checkout $entry || { lock_release; return 0; }
    done
  fi
  if (( ${#docs_prunable[@]} > 0 )); then
    for entry in "${docs_prunable[@]}"; do
      # shellcheck disable=SC2086
      delete_delivered_branch "$docs" "$sid" docs/repo $entry || { lock_release; return 0; }
    done
  fi
  lock_release
  return 0
}

delete_delivered_branch() {
  local dir="$1" sid="$2" where="$3" branch="$4" tip="$5" now
  now=$(git -C "$dir" rev-parse --verify --quiet "refs/heads/$branch" 2>/dev/null) || return 0
  [[ "$now" == "$tip" ]] || return 0
  branch_is_prunable_candidate "$dir" "$branch" || return 0
  local err
  if ! err=$(git -C "$dir" branch -D "$branch" 2>&1 >/dev/null); then
    echo "⚠ '$sid': could not delete delivered branch '$branch' at $tip in $where — stopping this slot's deletions: $err" >&2
    return 1
  fi
  echo "▸ '$sid': deleted delivered branch '$branch' at $tip in $where"
}

rescue_and_reset() {
  local dir="$1" sid="$2" date_stamp bundle bundled=1
  date_stamp=$(date +%Y%m%d-%H%M%S)

  echo "▸ committing whatever '$sid' holds as a WIP rescue commit"
  git -C "$dir" add -A
  local wip_branch="" wip_unsigned=0
  if ! git -C "$dir" diff --cached --quiet; then
    wip_branch=$(git -C "$dir" rev-parse --abbrev-ref HEAD)
    local cf; cf=$(compose_file_of)
    local workdir=""
    compose_running "$dir" "$sid" && workdir=$(workdir_of "$cf")
    if [[ -n "$workdir" ]]; then
      local svc
      svc=$(container_service_of "$dir" "$sid") || die "refusing to reset '$sid': the one service that mounts the checkout at /workspace is not known, so its uncommitted work cannot be committed inside the container."
      ( cd "$dir" && slot_compose "$dir" "$sid" exec -T -u node -w "$workdir" "$svc" \
          git commit --quiet --no-verify -m "chore(slot): rescue $sid $(date +%Y-%m-%d)" ) \
        || die "refusing to reset '$sid': its uncommitted work could not be committed inside the container, and the reset would have deleted it."
    else
      git -C "$dir" -c commit.gpgsign=false commit --quiet --no-verify \
        -m "chore(slot): rescue $sid $(date +%Y-%m-%d)" \
        || die "refusing to reset '$sid': its uncommitted work could not be committed, and the reset would have deleted it."
      wip_unsigned=1
    fi
  fi

  if ! docs_repo_is_clean "$dir"; then
    echo "⚠ '$sid' also has $DOCS_DIRTY_REASON — this rescue's bundle covers only $dir itself, not docs/repo; nothing below removes it, but check it by hand before the slot goes back out." >&2
  fi

  mkdir -p "$STATE/rescue"
  bundle="$(rescue_bundle_path "$sid")"
  git -C "$dir" bundle create "$bundle" --all >/dev/null 2>&1 || bundled=0
  if (( bundled )); then
    echo "▸ bundled every ref, the stash included, to $bundle"
  else
    echo "⚠ could not bundle '$sid' to $bundle — the rescue pushes below are the only copy there will be" >&2
  fi

  local b rc rescue_ref pushed=0 signatures_required=0 unsigned push_out reason
  local refused=()
  local push_log="$STATE/rescue/${sid}-${date_stamp}-push.log"
  while IFS= read -r b; do
    [[ -n "$b" ]] || continue
    if [[ -n "$wip_branch" && "$wip_unsigned" == 1 && "$b" == "$wip_branch" ]]; then
      echo "▸ '$b' — bundle only (container not running, so the commit is unsigned)"
      refused+=("$b (bundle only, container not running)")
      continue
    fi
    if branch_delivery_status "$dir" "$b"; then
      rc=0
    else
      rc=$?
    fi
    (( rc == 0 )) && continue
    if (( signatures_required )); then
      unsigned=$(git -C "$dir" log --format='%G?' "refs/heads/$b" --not --remotes=origin 2>/dev/null \
        | grep -c '^N' || true)
      if (( unsigned > 0 )); then
        echo "▸ skipping '$b' — $unsigned unsigned commit(s), and this remote has already refused one branch for that; it is in the bundle"
        refused+=("$b (unsigned, not offered)")
        continue
      fi
    fi
    rescue_ref="refs/heads/rescue/${sid}/${date_stamp}/${b}"
    echo "▸ pushing $rescue_ref from the host"
    if push_out=$(forge_git -C "$dir" push --quiet origin "refs/heads/$b:$rescue_ref" 2>&1); then
      echo "rescue branch: $rescue_ref"
      pushed=$((pushed + 1))
    else
      printf '=== %s\n%s\n' "$b" "$push_out" >> "$push_log"
      forge_hint_for "$dir"
      case "$push_out" in
        *"must have verified signatures"*) signatures_required=1 ;;
      esac
      reason=$(printf '%s\n' "$push_out" | sed -n 's/^remote: *- *//p' | head -1)
      echo "⚠ refused: $b${reason:+ — $reason}" >&2
      refused+=("$b")
    fi
  done < <(git -C "$dir" for-each-ref --format='%(refname:short)' refs/heads/)
  (( pushed == 0 )) && echo "▸ nothing held — every local branch was already delivered"

  if (( ${#refused[@]} > 0 )); then
    echo "▸ the remote refused ${#refused[@]} branch(es):" >&2
    printf '    %s\n' "${refused[@]}" >&2
    (( bundled )) || die "refusing to reset '$sid': the branches above could not be pushed and no bundle was written — resetting now would be the only copy of them gone."
    echo "    they are in $bundle — 'git fetch \"$bundle\" <branch>' brings one back." >&2
    if [[ -s "$push_log" ]]; then
      echo "    what the remote said each time, in full: $push_log" >&2
    fi
  fi

  reset_slot "$dir"
  lock_acquire
  RELEASE_SKIP_HOLDER_CHECK=1 cmd_release "$(basename "$dir")" || true
  lock_release
}
