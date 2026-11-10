# shellcheck shell=bash disable=SC2034,SC2154

docs_origin_matches() {
  local a="$1" b="$DOCS_REPO_URL"
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
  local docs="$dir/$subpath"
  local note=""
  [[ "$subpath" == docs/repo ]] && note=" — $DOCS_READ_ONLY_NOTE"
  DOCS_PRUNABLE_BRANCHES=()
  [[ -f "$docs/.git/HEAD" ]] || return 0

  local origin
  origin=$(git -C "$docs" remote get-url origin 2>/dev/null) || {
    DOCS_DIRTY_REASON="$label: no origin remote — cannot verify delivery"
    DOCS_DIRTY_COLUMN="$colprefix:unknown"
    return 2
  }
  if ! docs_origin_matches "$origin"; then
    DOCS_DIRTY_REASON="$label: origin is '$origin', not $DOCS_REPO_URL — a drifted mirror can only answer for itself, not GitHub"
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
    if branch_delivery_status "$docs" "$b" "$docs_gh_repo"; then
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
  [[ -f "$docs/.git/HEAD" ]] || return 1
  pin=$(docs_pin_of "$dir")
  [[ -n "$pin" ]] || return 1
  head=$(git -C "$docs" rev-parse HEAD 2>/dev/null) || head="nothing"
  [[ "$head" == "$pin" ]] && return 1
  DOCS_PIN_REASON="docs/repo is at ${head:0:12}, origin/$(default_branch "$dir")'s $DOCS_PIN_FILE is ${pin:0:12}"
  return 0
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
  if ! git -C "$docs" cat-file -e "$pin^{commit}" 2>/dev/null; then
    local -a depth=()
    [[ "$(git -C "$docs" rev-parse --is-shallow-repository 2>/dev/null)" == true ]] && depth=(--depth 1)
    forge_git -C "$docs" fetch --quiet ${depth[@]+"${depth[@]}"} origin "$pin" || return 1
  fi
  git -C "$docs" checkout --quiet --detach "$pin"
  docs_repo_write_marker_pin "$docs" "$pin"
}

DOCS_REPO_PUSHURL="read-only://docs-repo-is-read-only--clone-docs-fix"
DOCS_REPO_REFUSAL="docs/repo is read-only; clone $docs_gh_repo into docs/fix and make the change there, on a branch off its default branch"
DOCS_REPO_HOOKS=".git/slot-readonly-hooks"
guard_docs_repo_readonly() {
  local dir="$1" docs="$1/docs/repo" hooks h
  [[ -f "$docs/.git/HEAD" ]] || return 0
  hooks="$docs/$DOCS_REPO_HOOKS"
  mkdir -p "$hooks"
  for h in pre-commit pre-push; do
    printf '#!/bin/sh\ncat >&2 <<'\''MSG'\''\n%s\nMSG\nexit 1\n' "$DOCS_REPO_REFUSAL" > "$hooks/$h"
    chmod +x "$hooks/$h"
  done
  git -C "$docs" config --local core.hooksPath "$DOCS_REPO_HOOKS"
  git -C "$docs" config --local remote.origin.pushurl "$DOCS_REPO_PUSHURL"
}

DOCS_ORIGIN_DRIFT_REASON=""
slot_docs_origin_drifted() {
  local dir="$1" docs="$1/docs/repo"
  [[ -f "$docs/.git/HEAD" ]] || return 1
  local origin
  origin=$(git -C "$docs" remote get-url origin 2>/dev/null) || return 1
  docs_origin_matches "$origin" && return 1
  DOCS_ORIGIN_DRIFT_REASON="docs/repo origin is '$origin', not $DOCS_REPO_URL"
  return 0
}
