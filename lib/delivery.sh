# shellcheck shell=bash disable=SC2034,SC2154

shallow_compare_status() {
  local tip="$1" compare_branch="$2" repo_slug="$3" status
  status=$(gh_here api "repos/$repo_slug/compare/$tip...$compare_branch" --jq .status 2>/dev/null) \
    || return 2
  case "$status" in
    ahead|identical) return 0 ;;
    *) return 1 ;;
  esac
}

shallow_delivered_via_compare() {
  local dir="$1" tip="$2" branch="$3" repo_slug="$4" compare_branch
  [[ "$(git -C "$dir" rev-parse --is-shallow-repository 2>/dev/null)" == true ]] || return 1
  compare_branch="$branch"
  git -C "$dir" show-ref --verify --quiet "refs/remotes/origin/$branch" \
    || compare_branch=$(default_branch "$dir")
  shallow_compare_status "$tip" "$compare_branch" "$repo_slug"
}

merge_tree_delivered_against_refs() {
  local dir="$1" tip="$2" refs="$3" ref merged_tree
  while IFS= read -r ref; do
    [[ -n "$ref" && "$ref" != null ]] || continue
    git -C "$dir" cat-file -e "${ref}^{commit}" 2>/dev/null || continue
    merged_tree=$(git -C "$dir" merge-tree --write-tree "$ref" "$tip" 2>/dev/null) \
      && [[ "$merged_tree" == "$(git -C "$dir" rev-parse "${ref}^{tree}" 2>/dev/null)" ]] \
      && return 0
  done <<<"$refs"
  return 1
}

merged_pr_merge_commit_oids() {
  echo "$1" | jq -r '.[] | select(.state=="MERGED") | .mergeCommit.oid // empty' 2>/dev/null
}

merged_pr_head_oids() {
  echo "$1" | jq -r '.[] | select(.state=="MERGED") | .headRefOid // empty' 2>/dev/null
}

tip_behind_merged_pr_head() {
  local dir="$1" tip="$2" oids="$3" oid
  while IFS= read -r oid; do
    [[ -n "$oid" && "$oid" != null ]] || continue
    git -C "$dir" cat-file -e "${oid}^{commit}" 2>/dev/null || continue
    git -C "$dir" merge-base --is-ancestor "$tip" "$oid" 2>/dev/null && return 0
  done <<<"$oids"
  return 1
}

branch_delivered_via_merge_tree() {
  local dir="$1" tip="$2" default_ref="$3" json="${4:-}" refs
  refs=$(
    printf '%s\n' "$default_ref"
    [[ -n "$json" ]] && merged_pr_merge_commit_oids "$json"
  )
  merge_tree_delivered_against_refs "$dir" "$tip" "$refs"
}

branch_tip_in_remote() {
  local dir="$1" tip="$2" facts_reach="${3:-}"
  if target_is_facts "$dir"; then
    [[ "$facts_reach" == 1 ]]
  else
    [[ -n "$(git -C "$dir" branch -r --contains "$tip" 2>/dev/null)" ]]
  fi
}

branch_check_pr_json() {
  local dir="$1" branch="$2" tip="$3" repo_slug="$4" default_ref="$5" cache="$6" closed_cache="$7"
  local facts_reach="${8:-}" facts_sid="${9:-}" facts_sub="${10:-.}" notremote="${11:-}"

  if ! command -v gh >/dev/null 2>&1; then
    if [[ -f "$closed_cache" ]]; then
      BRANCH_REPORT=$(cat "$closed_cache")
      return 1
    fi
    BRANCH_REPORT="$branch: unknown (no gh on PATH)"
    return 2
  fi
  local json
  if ! json=$(gh_here pr list --repo "$repo_slug" --head "$branch" --state all \
                --json headRefOid,state,number,mergeCommit --limit 5 2>/dev/null); then
    if [[ -f "$closed_cache" ]]; then
      BRANCH_REPORT=$(cat "$closed_cache")
      return 1
    fi
    BRANCH_REPORT="$branch: unknown (gh refused)"
    return 2
  fi

  if [[ "$(echo "$json" | jq --arg tip "$tip" \
        '[.[] | select(.headRefOid==$tip)] | length')" -gt 0 ]]; then
    touch "$cache"
    return 0
  fi

  local count; count=$(echo "$json" | jq 'length')
  if [[ "$count" -gt 0 ]]; then
    if branch_tip_in_remote "$dir" "$tip" "$facts_reach"; then
      return 0
    fi
    if target_is_facts "$dir"; then
      local merged_oids
      merged_oids=$({ merged_pr_merge_commit_oids "$json"; merged_pr_head_oids "$json"; })
      if [[ -n "$merged_oids" ]] && merged_oids_cover_tip "$facts_sid" "$facts_sub" "$tip" "$merged_oids"; then
        touch "$cache"
        return 0
      fi
    else
      if branch_delivered_via_merge_tree "$dir" "$tip" "$default_ref" "$json"; then
        touch "$cache"
        return 0
      fi
      if tip_behind_merged_pr_head "$dir" "$tip" "$(merged_pr_head_oids "$json")"; then
        touch "$cache"
        return 0
      fi
    fi
    local pr_tip pr_num beyond=""
    pr_tip=$(echo "$json" | jq -r '.[0].headRefOid')
    pr_num=$(echo "$json" | jq -r '.[0].number')
    if target_is_facts "$dir"; then
      if beyond=$(slot_sh "$facts_sid" sh -c 'cd "$1" && git cat-file -e "$2" 2>/dev/null && (git rev-list --count "$2..$3" 2>/dev/null || echo "?")' sh "$facts_sub" "$pr_tip" "$branch" 2>/dev/null) && [[ -n "$beyond" ]]; then
        BRANCH_REPORT="$branch: $beyond commits beyond PR #$pr_num"
      else
        BRANCH_REPORT="$branch: tip differs from PR #$pr_num (sha not fetched)"
      fi
    else
      if git -C "$dir" cat-file -e "$pr_tip" 2>/dev/null; then
        beyond=$(git -C "$dir" rev-list --count "$pr_tip..$branch" 2>/dev/null || echo '?')
        BRANCH_REPORT="$branch: $beyond commits beyond PR #$pr_num"
      else
        BRANCH_REPORT="$branch: tip differs from PR #$pr_num (sha not fetched)"
      fi
    fi
    if [[ "$(echo "$json" | jq '[.[] | select(.state=="OPEN")] | length')" -eq 0 ]]; then
      printf '%s\n' "$BRANCH_REPORT" >"$closed_cache"
      { merged_pr_merge_commit_oids "$json"; merged_pr_head_oids "$json"; } >"${closed_cache}.merge-commits"
    fi
    return 1
  fi

  if branch_tip_in_remote "$dir" "$tip" "$facts_reach"; then
    return 0
  fi
  local n="$notremote"
  if [[ -z "$n" ]] && ! target_is_facts "$dir"; then
    n=$(git -C "$dir" rev-list --count "$branch" --not --remotes 2>/dev/null || echo '?')
  fi
  BRANCH_REPORT="$branch: never pushed, ${n:-?} commits"
  return 1
}

branch_delivery_status() {
  local dir="$1" branch="$2" repo_slug="${3:-$gh_repo}" tip
  local facts_sid="${4:-}" facts_sub="${5:-.}"
  BRANCH_REPORT=""

  local cache_key
  local f_has=0 f_ahead=0 f_reach=0 f_cherry=na f_notremote="" f_mt=0 is_shallow=false def_name=""
  if target_is_facts "$dir"; then
    [[ -n "$facts_sid" ]] || facts_sid="$dir"
    if facts_unreadable "$dir"; then
      BRANCH_REPORT="$branch: could not read repository facts"
      return 2
    fi
    local line _fb _f_unsigned
    line=$(branch_fact_line "$dir" "$branch")
    IFS=$'\t' read -r _fb tip f_has f_ahead f_reach f_cherry f_notremote _f_unsigned f_mt <<<"$line"
    [[ -n "$tip" ]] || {
      BRANCH_REPORT="$branch: could not resolve its own tip"
      return 2
    }
    def_name=$(fact "$dir" default)
    def_name=${def_name:-$DEFAULT_BRANCH}
    is_shallow=$(fact "$dir" shallow)
  else
    ensure_remote_refs_fresh "$dir"
    tip=$(git -C "$dir" rev-parse "refs/heads/$branch" 2>/dev/null) || {
      BRANCH_REPORT="$branch: could not resolve its own tip"
      return 2
    }
  fi

  if [[ "$repo_slug" == "$gh_repo" ]]; then
    # Named by the key, as rescue and the token cache are. An entry still named by
    # the label is not read: it is a cache miss, and the verdict is derived again.
    cache_key="${PROJECT_KEY:-$SLOT_PROJECT_PREFIX}-${branch//\//_}-${tip}"
  else
    cache_key="${repo_slug//\//_}-${branch//\//_}-${tip}"
  fi
  local cache="$STATE/delivered/$cache_key"
  local closed_cache="$STATE/closed/$cache_key"

  if target_is_facts "$dir"; then
    if [[ "$f_has" == 1 ]]; then
      [[ "$f_ahead" == 0 ]] && return 0
      [[ "$f_reach" == 1 ]] && return 0
      if [[ "$is_shallow" == true && -n "$repo_slug" ]]; then
        local shallow_rc=0
        shallow_compare_status "$tip" "$branch" "$repo_slug" || shallow_rc=$?
        case "$shallow_rc" in
          0) touch "$cache"; return 0 ;;
          2) BRANCH_REPORT="$branch: unknown (compare api failed)"; return 2 ;;
        esac
      fi
      BRANCH_REPORT="$branch: $f_ahead commits not on origin/$branch"
      return 1
    fi

    [[ "$f_reach" == 1 ]] && return 0
    [[ -f "$cache" ]] && return 0

    local default_ref="origin/$def_name"
    if [[ "$f_cherry" == clean ]]; then
      touch "$cache"
      return 0
    fi

    if [[ "$is_shallow" == true && -n "$repo_slug" ]]; then
      local shallow_rc=0
      shallow_compare_status "$tip" "$def_name" "$repo_slug" || shallow_rc=$?
      case "$shallow_rc" in
        0) touch "$cache"; return 0 ;;
        2) BRANCH_REPORT="$branch: unknown (compare api failed)"; return 2 ;;
      esac
    fi

    if [[ "$f_mt" == 1 ]]; then
      touch "$cache"
      return 0
    fi

    if [[ -f "$closed_cache" && -f "${closed_cache}.merge-commits" ]]; then
      if merged_oids_cover_tip "$facts_sid" "$facts_sub" "$tip" "$(cat "${closed_cache}.merge-commits")"; then
        touch "$cache"
        return 0
      fi
      BRANCH_REPORT=$(cat "$closed_cache")
      return 1
    fi

    if [[ -z "$repo_slug" ]]; then
      BRANCH_REPORT="$branch: commits not in $default_ref (no remote configured)"
      return 1
    fi

    branch_check_pr_json "$dir" "$branch" "$tip" "$repo_slug" "$default_ref" "$cache" "$closed_cache" "$f_reach" "$facts_sid" "$facts_sub" "$f_notremote"
    return $?
  fi

  if git -C "$dir" show-ref --verify --quiet "refs/remotes/origin/$branch"; then
    local ahead
    ahead=$(git -C "$dir" rev-list --count "origin/$branch..$branch" 2>/dev/null || echo 0)
    if [[ "$ahead" -eq 0 ]]; then
      return 0
    fi
    if [[ -n "$(git -C "$dir" branch -r --contains "$tip" 2>/dev/null)" ]]; then
      return 0
    fi
    local shallow_rc=0
    if [[ -n "$repo_slug" ]]; then
      shallow_delivered_via_compare "$dir" "$tip" "$branch" "$repo_slug" || shallow_rc=$?
      case "$shallow_rc" in
        0) touch "$cache"; return 0 ;;
        2) BRANCH_REPORT="$branch: unknown (compare api failed)"; return 2 ;;
      esac
    fi
    BRANCH_REPORT="$branch: $ahead commits not on origin/$branch"
    return 1
  fi

  if [[ -n "$(git -C "$dir" branch -r --contains "$tip" 2>/dev/null)" ]]; then
    return 0
  fi

  if [[ -f "$cache" ]]; then
    return 0
  fi

  local default_ref cherry_out
  default_ref="origin/$(default_branch "$dir")"
  if git -C "$dir" show-ref --verify --quiet "refs/remotes/$default_ref" \
      && cherry_out=$(git -C "$dir" cherry "$default_ref" "$branch" 2>/dev/null) \
      && ! grep -q '^+' <<<"$cherry_out"; then
    touch "$cache"
    return 0
  fi

  local shallow_rc=0
  if [[ -n "$repo_slug" ]]; then
    shallow_delivered_via_compare "$dir" "$tip" "$branch" "$repo_slug" || shallow_rc=$?
    case "$shallow_rc" in
      0) touch "$cache"; return 0 ;;
      2) BRANCH_REPORT="$branch: unknown (compare api failed)"; return 2 ;;
    esac
  fi

  if git -C "$dir" show-ref --verify --quiet "refs/remotes/$default_ref" \
      && branch_delivered_via_merge_tree "$dir" "$tip" "$default_ref"; then
    touch "$cache"
    return 0
  fi

  if [[ -f "$closed_cache" ]]; then
    if [[ -f "${closed_cache}.merge-commits" ]]; then
      if tip_behind_merged_pr_head "$dir" "$tip" "$(cat "${closed_cache}.merge-commits")" \
          || merge_tree_delivered_against_refs "$dir" "$tip" "$(cat "${closed_cache}.merge-commits")"; then
        touch "$cache"
        return 0
      fi
      BRANCH_REPORT=$(cat "$closed_cache")
      return 1
    fi
  fi

  if [[ -z "$repo_slug" ]]; then
    BRANCH_REPORT="$branch: commits not in $default_ref (no remote configured)"
    return 1
  fi

  branch_check_pr_json "$dir" "$branch" "$tip" "$repo_slug" "$default_ref" "$cache" "$closed_cache"
}

branch_is_prunable_candidate() {
  local dir="$1" branch="$2" wts
  [[ "$branch" != "$DEFAULT_BRANCH" ]] || return 1
  if target_is_facts "$dir"; then
    ! facts_lines "$dir" wtbranch | grep -qxF "$branch"
    return
  fi
  wts=$(git -C "$dir" worktree list --porcelain 2>/dev/null) || return 1
  ! grep -qxF "branch refs/heads/$branch" <<<"$wts"
}

collect_branch_statuses() {
  local dir="$1" facts_sid="${2:-}" facts_sub="${3:-.}" b rc
  HELD_COUNT=0 UNKNOWN_COUNT=0
  BRANCH_REPORTS=()
  PRUNABLE_BRANCHES=()
  if target_is_facts "$dir"; then
    [[ -n "$facts_sid" ]] || facts_sid="$dir"
    if facts_unreadable "$dir"; then
      UNKNOWN_COUNT=1
      BRANCH_REPORTS+=("could not read repository facts from '$facts_sid'")
      return 0
    fi
    local btip _rest
    while IFS=$'\t' read -r b btip _rest; do
      [[ -n "$b" ]] || continue
      if branch_delivery_status "$dir" "$b" "$gh_repo" "$facts_sid" "$facts_sub"; then
        rc=0
      else
        rc=$?
      fi
      case "$rc" in
        0) if branch_is_prunable_candidate "$dir" "$b"; then
             PRUNABLE_BRANCHES+=("$b $btip")
           fi ;;
        1) HELD_COUNT=$((HELD_COUNT + 1)); BRANCH_REPORTS+=("$BRANCH_REPORT") ;;
        2) UNKNOWN_COUNT=$((UNKNOWN_COUNT + 1)); BRANCH_REPORTS+=("$BRANCH_REPORT") ;;
      esac
    done < <(facts_lines "$dir" branch)
    return 0
  fi
  while IFS= read -r b; do
    [[ -n "$b" ]] || continue
    if branch_delivery_status "$dir" "$b"; then
      rc=0
    else
      rc=$?
    fi
    case "$rc" in
      0) if branch_is_prunable_candidate "$dir" "$b"; then
           PRUNABLE_BRANCHES+=("$b $(git -C "$dir" rev-parse "refs/heads/$b")")
         fi ;;
      1) HELD_COUNT=$((HELD_COUNT + 1)); BRANCH_REPORTS+=("$BRANCH_REPORT") ;;
      2) UNKNOWN_COUNT=$((UNKNOWN_COUNT + 1)); BRANCH_REPORTS+=("$BRANCH_REPORT") ;;
    esac
  done < <(git -C "$dir" for-each-ref --format='%(refname:short)' refs/heads/)
}

head_is_delivered() {
  local dir="$1" head_branch
  if target_is_facts "$dir"; then
    facts_unreadable "$dir" && return 2
    head_branch=$(head_branch_of "$dir")
    if [[ -n "$head_branch" ]]; then
      branch_delivery_status "$dir" "$head_branch"
      return $?
    fi
    local tip reach
    tip=$(fact "$dir" head | cut -f2)
    [[ -n "$tip" ]] || return 2
    reach=$(fact "$dir" headreach)
    [[ "$reach" == 1 ]]
    return
  fi
  head_branch=$(head_branch_of "$dir")
  if [[ -n "$head_branch" ]]; then
    branch_delivery_status "$dir" "$head_branch"
    return $?
  fi
  ensure_remote_refs_fresh "$dir"
  local tip
  tip=$(git -C "$dir" rev-parse HEAD 2>/dev/null) || return 2
  [[ -n "$(git -C "$dir" branch -r --contains "$tip" 2>/dev/null)" ]]
}

DOCS_DIRTY_REASON=""
DOCS_DIRTY_COLUMN=""
DOCS_PRUNABLE_BRANCHES=()

branch_unproven_against_master() {
  local dir="$1" branch="$2"
  BRANCH_UNPROVEN_REPORT=""
  [[ "$branch" != "$DEFAULT_BRANCH" ]] || return 0
  git -C "$dir" show-ref --verify --quiet "refs/remotes/origin/$DEFAULT_BRANCH" || return 0
  git -C "$dir" cherry "origin/$DEFAULT_BRANCH" "$branch" 2>/dev/null | grep -q '^+' || return 0
  BRANCH_UNPROVEN_REPORT="$branch: not proved to be in origin/$DEFAULT_BRANCH — git cherry finds at least one commit whose patch has no match on origin/$DEFAULT_BRANCH; check before assuming it's redundant"
  return 1
}

collect_unproven_branches() {
  local dir="$1" label="${2:-}" b
  UNPROVEN_REPORTS=()
  if target_is_facts "$dir"; then
    facts_unreadable "$dir" && return 0
    local def; def=$(fact "$dir" default); def=${def:-$DEFAULT_BRANCH}
    local _tip _has _ahead _reach cherry _rest
    while IFS=$'\t' read -r b _tip _has _ahead _reach cherry _rest; do
      [[ -n "$b" && "$b" != "$def" ]] || continue
      if [[ "$cherry" == plus ]]; then
        UNPROVEN_REPORTS+=("${label}$b: not proved to be in origin/$def — git cherry finds at least one commit whose patch has no match on origin/$def; check before assuming it's redundant")
      fi
    done < <(facts_lines "$dir" branch)
    return 0
  fi
  [[ -f "$dir/.git/HEAD" ]] || return 0
  ensure_remote_refs_fresh "$dir"
  while IFS= read -r b; do
    [[ -n "$b" ]] || continue
    if ! branch_unproven_against_master "$dir" "$b"; then
      UNPROVEN_REPORTS+=("${label}${BRANCH_UNPROVEN_REPORT}")
    fi
  done < <(git -C "$dir" for-each-ref --format='%(refname:short)' refs/heads/)
}

slot_branches_unproven_against_master() {
  local dir="$1" all=()
  collect_unproven_branches "$dir" ""
  all+=("${UNPROVEN_REPORTS[@]}")
  if target_is_facts "$dir"; then
    if facts_lines "$dir" clone | grep -qxF "docs/repo"; then
      facts_of "$dir" docs/repo >/dev/null
      collect_unproven_branches "$(facts_file "$dir" docs/repo)" "docs/repo: "
      all+=("${UNPROVEN_REPORTS[@]}")
    fi
  else
    collect_unproven_branches "$dir/docs/repo" "docs/repo: "
    all+=("${UNPROVEN_REPORTS[@]}")
  fi
  UNPROVEN_REPORTS=("${all[@]}")
}

warn_unproven_branches() {
  local dir="$1" header="$2"
  slot_branches_unproven_against_master "$dir"
  (( ${#UNPROVEN_REPORTS[@]} > 0 )) || return 0
  echo "$header" >&2
  local r
  for r in "${UNPROVEN_REPORTS[@]}"; do echo "  $r" >&2; done
}
