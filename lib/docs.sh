# shellcheck shell=bash disable=SC2034,SC2154

docs_origin_matches() {
  local a="$1" b="${2:-$DOCS_REPO_URL}"
  a=${a%/}; a=${a%.git}; a=${a%/}
  b=${b%/}; b=${b%.git}; b=${b%/}
  [[ "$a" == "$b" ]]
}

docs_repo_marker_pin_file() {
  local docs="$1" gitdir
  gitdir=$(git -C "$docs" rev-parse --git-dir 2>/dev/null) || return 1
  [[ "$gitdir" = /* ]] || gitdir="$docs/$gitdir"
  echo "$gitdir/slot-docs-pin"
}

docs_repo_write_marker_pin() {
  local docs="$1" pin="$2" file
  file=$(docs_repo_marker_pin_file "$docs") || return 0
  echo "$pin" > "$file"
}

docs_repo_marker_pin() {
  local docs="$1" file
  file=$(docs_repo_marker_pin_file "$docs") || return 0
  [[ -f "$file" ]] && cat "$file"
  return 0
}

docs_repo_is_clean() {
  local dir="$1" subpath="${2:-docs/repo}" label="${3:-docs/repo}" colprefix="${4:-docs}"
  local expected_url="${5:-$DOCS_REPO_URL}" repo_slug="${6:-$docs_gh_repo}" is_ro="${7:-}"
  if [[ -z "$is_ro" ]]; then
    [[ "$subpath" == docs/repo ]] && is_ro=1 || is_ro=0
  fi
  local docs="$dir/$subpath"
  local note=""
  if (( is_ro )); then
    if [[ "$subpath" == docs/repo ]]; then
      note=" — $DOCS_READ_ONLY_NOTE"
    else
      note=" — $subpath is read-only"
    fi
  fi
  DOCS_PRUNABLE_BRANCHES=()

  if target_is_facts "$dir"; then
    facts_lines "$dir" clone | grep -qxF "$subpath" || return 0
    local df; df=$(facts_file "$dir" "$subpath")
    facts_of "$dir" "$subpath" >/dev/null
    if facts_unreadable "$df"; then
      DOCS_DIRTY_REASON="$label: could not read repository facts"
      DOCS_DIRTY_COLUMN="$colprefix:unknown"
      return 2
    fi
    local origin
    origin=$(fact "$df" origin)
    if [[ -z "$origin" ]]; then
      DOCS_DIRTY_REASON="$label: no origin remote — cannot verify delivery"
      DOCS_DIRTY_COLUMN="$colprefix:unknown"
      return 2
    fi
    if ! docs_origin_matches "$origin" "$expected_url"; then
      DOCS_DIRTY_REASON="$label: origin is '$origin', not $expected_url — a drifted mirror can only answer for itself, not GitHub"
      DOCS_DIRTY_COLUMN="$colprefix:unknown origin"
      return 2
    fi
    local v n
    v=$(facts_lines "$df" status)
    if [[ -n "$v" ]]; then
      n=$(printf '%s\n' "$v" | grep -c .)
      DOCS_DIRTY_REASON="$label: $n uncommitted change(s)"
      DOCS_DIRTY_COLUMN="$colprefix:uncommitted:$n"
      return 1
    fi
    v=$(facts_lines "$df" stash)
    if [[ -n "$v" ]]; then
      n=$(printf '%s\n' "$v" | grep -c .)
      DOCS_DIRTY_REASON="$label: $n stashed"
      DOCS_DIRTY_COLUMN="$colprefix:stash:$n"
      return 1
    fi
    local b btip _rest rc held=0 unknown=0 first_reason=""
    while IFS=$'\t' read -r b btip _rest; do
      [[ -n "$b" ]] || continue
      if branch_delivery_status "$df" "$b" "$repo_slug" "$dir" "$subpath"; then
        rc=0
      else
        rc=$?
      fi
      case "$rc" in
        0) if branch_is_prunable_candidate "$df" "$b"; then
             DOCS_PRUNABLE_BRANCHES+=("$b $btip")
           fi ;;
        1) held=$((held + 1)); [[ -n "$first_reason" ]] || first_reason="$BRANCH_REPORT" ;;
        2) unknown=$((unknown + 1)); [[ -n "$first_reason" ]] || first_reason="$BRANCH_REPORT" ;;
      esac
    done < <(facts_lines "$df" branch)

    if [[ -z "$(fact "$df" head | cut -f1)" ]]; then
      local tip reach pin marker
      tip=$(fact "$df" head | cut -f2)
      if [[ -n "$tip" ]]; then
        reach=$(fact "$df" headreach)
        pin=$(fact "$dir" docspin)
        marker=$(fact "$df" marker)
        if [[ "$tip" != "$pin" ]] \
           && [[ -z "$marker" || "$tip" != "$marker" ]] \
           && [[ "$reach" != 1 ]]; then
          held=$((held + 1))
          [[ -n "$first_reason" ]] || first_reason="detached HEAD not reachable from any remote ref"
        fi
      fi
    fi

    if (( unknown > 0 )); then
      DOCS_DIRTY_REASON="$label: $unknown branch(es) could not be verified"
      (( held > 0 )) && DOCS_DIRTY_REASON+=", $held held"
      DOCS_DIRTY_REASON+=" (e.g. $first_reason)$note"
      DOCS_DIRTY_COLUMN="$colprefix:unknown:$unknown"
      return 2
    fi
    if (( held > 0 )); then
      DOCS_DIRTY_REASON="$label: $held branch(es) held (e.g. $first_reason)$note"
      DOCS_DIRTY_COLUMN="$colprefix:$held unpushed"
      return 1
    fi
    return 0
  fi

  [[ -f "$docs/.git/HEAD" ]] || return 0

  local origin
  origin=$(git -C "$docs" config --get remote.origin.url 2>/dev/null) || {
    DOCS_DIRTY_REASON="$label: no origin remote — cannot verify delivery"
    DOCS_DIRTY_COLUMN="$colprefix:unknown"
    return 2
  }
  if ! docs_origin_matches "$origin" "$expected_url"; then
    DOCS_DIRTY_REASON="$label: origin is '$origin', not $expected_url — a drifted mirror can only answer for itself, not GitHub"
    DOCS_DIRTY_COLUMN="$colprefix:unknown origin"
    return 2
  fi

  local v n
  v=$(git -C "$docs" status --porcelain 2>/dev/null)
  if [[ -n "$v" ]]; then
    n=$(printf '%s\n' "$v" | grep -c .)
    DOCS_DIRTY_REASON="$label: $n uncommitted change(s)"
    DOCS_DIRTY_COLUMN="$colprefix:uncommitted:$n"
    return 1
  fi
  v=$(git -C "$docs" stash list 2>/dev/null)
  if [[ -n "$v" ]]; then
    n=$(printf '%s\n' "$v" | grep -c .)
    DOCS_DIRTY_REASON="$label: $n stashed"
    DOCS_DIRTY_COLUMN="$colprefix:stash:$n"
    return 1
  fi

  local b rc held=0 unknown=0 first_reason=""
  while IFS= read -r b; do
    [[ -n "$b" ]] || continue
    if branch_delivery_status "$docs" "$b" "$repo_slug"; then
      rc=0
    else
      rc=$?
    fi
    case "$rc" in
      0) if branch_is_prunable_candidate "$docs" "$b"; then
           DOCS_PRUNABLE_BRANCHES+=("$b $(git -C "$docs" rev-parse "refs/heads/$b")")
         fi ;;
      1) held=$((held + 1)); [[ -n "$first_reason" ]] || first_reason="$BRANCH_REPORT" ;;
      2) unknown=$((unknown + 1)); [[ -n "$first_reason" ]] || first_reason="$BRANCH_REPORT" ;;
    esac
  done < <(git -C "$docs" for-each-ref --format='%(refname:short)' refs/heads/)

  if [[ -z "$(git -C "$docs" symbolic-ref --quiet --short HEAD 2>/dev/null)" ]]; then
    ensure_remote_refs_fresh "$docs"
    local tip marker
    tip=$(git -C "$docs" rev-parse HEAD 2>/dev/null) && {
      marker=$(docs_repo_marker_pin "$docs")
      if [[ "$tip" != "$(docs_pin_of "$dir")" ]] \
         && [[ -z "$marker" || "$tip" != "$marker" ]] \
         && [[ -z "$(git -C "$docs" branch -r --contains "$tip" 2>/dev/null)" ]]; then
        held=$((held + 1))
        [[ -n "$first_reason" ]] || first_reason="detached HEAD not reachable from any remote ref"
      fi
    }
  fi

  if (( unknown > 0 )); then
    DOCS_DIRTY_REASON="$label: $unknown branch(es) could not be verified"
    (( held > 0 )) && DOCS_DIRTY_REASON+=", $held held"
    DOCS_DIRTY_REASON+=" (e.g. $first_reason)$note"
    DOCS_DIRTY_COLUMN="$colprefix:unknown:$unknown"
    return 2
  fi
  if (( held > 0 )); then
    DOCS_DIRTY_REASON="$label: $held branch(es) held (e.g. $first_reason)$note"
    DOCS_DIRTY_COLUMN="$colprefix:$held unpushed"
    return 1
  fi
  return 0
}

DOCS_READ_ONLY_NOTE="a slot's docs/repo is read-only, not the place for a commit or a branch; a docs fix is its own pull request from a clone of its own"

docs_pin_of() {
  local dir="$1"
  git -C "$dir" show "origin/$(default_branch "$dir"):$DOCS_PIN_FILE" 2>/dev/null \
    | grep -v '^[[:space:]]*#' | tr -d '[:space:]' || true
}

DOCS_PIN_REASON=""
docs_repo_off_pin() {
  local dir="$1" docs="$1/docs/repo" pin head
  if target_is_facts "$dir"; then
    facts_lines "$dir" clone | grep -qxF "docs/repo" || return 1
    pin=$(fact "$dir" docspin)
    [[ -n "$pin" ]] || return 1
    head=$(fact "$dir" docshead)
    [[ -n "$head" ]] || head="nothing"
    [[ "$head" == "$pin" ]] && return 1
    local def; def=$(fact "$dir" default); def=${def:-$DEFAULT_BRANCH}
    DOCS_PIN_REASON="docs/repo is at ${head:0:12}, origin/${def}'s $DOCS_PIN_FILE is ${pin:0:12}"
    return 0
  fi
  [[ -f "$docs/.git/HEAD" ]] || return 1
  pin=$(docs_pin_of "$dir")
  [[ -n "$pin" ]] || return 1
  head=$(git -C "$docs" rev-parse HEAD 2>/dev/null) || head="nothing"
  [[ "$head" == "$pin" ]] && return 1
  DOCS_PIN_REASON="docs/repo is at ${head:0:12}, origin/$(default_branch "$dir")'s $DOCS_PIN_FILE is ${pin:0:12}"
  return 0
}

fetch_pinned_commit_git() {
  local repo_dir="$1" auth="$2"
  shift 2
  if [[ "$auth" == "none" ]]; then
    GIT_TERMINAL_PROMPT=0 GIT_ASKPASS=/bin/false git -C "$repo_dir" -c "credential.${FORGE_HOST_ORIGIN}.helper=" "$@"
  else
    forge_git -C "$repo_dir" "$@"
  fi
}

fetch_pinned_commit() {
  local repo_dir="$1" pin="$2" auth="${3:-project}"
  git -C "$repo_dir" cat-file -e "$pin^{commit}" 2>/dev/null && return 0
  local err_file
  err_file=$(mktemp)
  local -a depth=()
  [[ "$(git -C "$repo_dir" rev-parse --is-shallow-repository 2>/dev/null)" == true ]] && depth=(--depth 1)
  fetch_pinned_commit_git "$repo_dir" "$auth" fetch --quiet ${depth[@]+"${depth[@]}"} origin "$pin" 2>"$err_file" || true
  if ! git -C "$repo_dir" cat-file -e "$pin^{commit}" 2>/dev/null \
     && [[ "$(git -C "$repo_dir" rev-parse --is-shallow-repository 2>/dev/null)" == true ]]; then
    fetch_pinned_commit_git "$repo_dir" "$auth" fetch --quiet --unshallow origin 2>"$err_file" || true
  fi
  if ! git -C "$repo_dir" cat-file -e "$pin^{commit}" 2>/dev/null; then
    fetch_pinned_commit_git "$repo_dir" "$auth" fetch --quiet origin "+refs/heads/*:refs/remotes/origin/*" "+refs/tags/*:refs/tags/*" 2>"$err_file" || true
  fi
  if git -C "$repo_dir" cat-file -e "$pin^{commit}" 2>/dev/null; then
    rm -f "$err_file"
    return 0
  fi
  [[ ! -s "$err_file" ]] || cat "$err_file" >&2
  rm -f "$err_file"
  return 1
}

sync_docs_repo_to_pin() {
  local dir="$1" docs="$1/docs/repo" pin
  [[ -f "$docs/.git/HEAD" ]] || return 0
  pin=$(docs_pin_of "$dir")
  if [[ -z "$pin" ]]; then
    echo "  ! no $DOCS_PIN_FILE on origin/$(default_branch "$dir") — docs/repo left where it is" >&2
    return 0
  fi
  docs_repo_off_pin "$dir" || return 0
  if [[ -n "$(git -C "$docs" status --porcelain 2>/dev/null)" ]]; then
    echo "  ! $DOCS_PIN_REASON — not moved: docs/repo has uncommitted changes" >&2
    return 0
  fi
  echo "▸ moving docs/repo to the docs pin — $DOCS_PIN_REASON"
  fetch_pinned_commit "$docs" "$pin" project || return 1
  git -C "$docs" checkout --quiet --detach "$pin"
  docs_repo_write_marker_pin "$docs" "$pin"
}

DOCS_REPO_PUSHURL="read-only://docs-repo-is-read-only--clone-docs-fix"
DOCS_REPO_REFUSAL="docs/repo is read-only; clone $docs_gh_repo into docs/fix and make the change there, on a branch off its default branch"
DOCS_REPO_HOOKS=".git/slot-readonly-hooks"
docs_repo_hook_script() {
  printf '#!/bin/sh\ncat >&2 <<'\''MSG'\''\n%s\nMSG\nexit 1\n' "${1:-$DOCS_REPO_REFUSAL}"
}
guard_repo_readonly() {
  local dir="$1" subpath="$2" alias="$3" rw_alias="${4:-}" legacy_docs="${5:-0}"
  local repo="$dir/$subpath" hooks h pushurl refusal
  [[ -f "$repo/.git/HEAD" ]] || return 0
  if [[ "$legacy_docs" == 1 || "$subpath" == docs/repo ]]; then
    pushurl="$DOCS_REPO_PUSHURL"
    refusal="$DOCS_REPO_REFUSAL"
  else
    pushurl="read-only://slot-repo-is-read-only--$alias"
    if [[ -n "$rw_alias" ]]; then
      refusal="$subpath ($alias) is read-only; attach rw_alias '$rw_alias' instead"
    else
      refusal="$subpath ($alias) is read-only"
    fi
  fi
  hooks="$repo/$DOCS_REPO_HOOKS"
  mkdir -p "$hooks"
  for h in pre-commit pre-push; do
    docs_repo_hook_script "$refusal" > "$hooks/$h"
    chmod +x "$hooks/$h"
  done
  git -C "$repo" config --local core.hooksPath "$DOCS_REPO_HOOKS"
  git -C "$repo" config --local remote.origin.pushurl "$pushurl"
}
guard_docs_repo_readonly() {
  guard_repo_readonly "$1" "docs/repo" "docs" "docs-fix" 1
}

DOCS_ORIGIN_DRIFT_REASON=""
slot_docs_origin_drifted() {
  local dir="$1" docs="$1/docs/repo"
  if target_is_facts "$dir"; then
    facts_lines "$dir" clone | grep -qxF "docs/repo" || return 1
    local origin; origin=$(fact "$dir" docsorigin)
    [[ -n "$origin" ]] || return 1
    docs_origin_matches "$origin" && return 1
    DOCS_ORIGIN_DRIFT_REASON="docs/repo origin is '$origin', not $DOCS_REPO_URL"
    return 0
  fi
  [[ -f "$docs/.git/HEAD" ]] || return 1
  local origin
  origin=$(git -C "$docs" config --get remote.origin.url 2>/dev/null) || return 1
  docs_origin_matches "$origin" && return 1
  DOCS_ORIGIN_DRIFT_REASON="docs/repo origin is '$origin', not $DOCS_REPO_URL"
  return 0
}
