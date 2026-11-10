# shellcheck shell=bash disable=SC2034,SC2154

gh_here() { ( cd "$repo_root" && forge_gh "$@" ); }

FETCHED_DIRS=""
HOST_GIT_READ_ONLY=0

ensure_remote_refs_fresh() {
  local dir="$1"
  case " $FETCHED_DIRS " in
    *" $dir "*) return 0 ;;
  esac
  FETCHED_DIRS="$FETCHED_DIRS $dir"
  if (( HOST_GIT_READ_ONLY )) && slot_dir_is_leased "$dir"; then
    echo "⚠ '$dir' is leased — not fetching origin from the host while it may be in use; delivered/held verdicts below are only as fresh as its last fetch" >&2
    return 0
  fi
  forge_git -C "$dir" fetch --quiet --prune origin 2>/dev/null \
    || { echo "⚠ could not fetch origin for '$dir' — delivered/held verdicts below are only as fresh as its last successful fetch" >&2; forge_hint_for "$dir"; }
}

head_branch_of() {
  git -C "$1" symbolic-ref --quiet --short HEAD 2>/dev/null || true
}

slot_map_workspace_path() {
  local dir="$1" wt="$2"
  case "$wt" in
    /workspace/*) echo "$dir/${wt#/workspace/}" ;;
    /workspace) echo "$dir" ;;
    *) echo "$wt" ;;
  esac
}

slot_worktrees_of() {
  local dir="$1" line wt
  WORKTREES=()
  while IFS= read -r line; do
    [[ "$line" == worktree\ * ]] || continue
    wt=${line#worktree }
    [[ "$wt" == "$dir" ]] && continue
    WORKTREES+=("$wt")
  done < <(git -C "$dir" worktree list --porcelain 2>/dev/null || true)
}

apply_docs_clone_git_identity() {
  local dir="$1" name="$2" email="$3" sub d
  for sub in docs/repo docs/fix; do
    d="$dir/$sub"
    git -C "$d" rev-parse --git-dir >/dev/null 2>&1 || continue
    git -C "$d" config --local user.name "$name"
    [[ -n "$email" ]] && git -C "$d" config --local user.email "$email"
    git -C "$d" config --local gpg.format ssh
    git -C "$d" config --local user.signingkey /home/node/.ssh/container_signing.pub
    git -C "$d" config --local commit.gpgsign true
    git -C "$d" config --local tag.gpgsign true
  done
}

apply_checkout_git_identity() {
  local target="$1"
  local sid env_file name="" email=""
  if [[ "$target" == /* ]]; then
    sid=$(slot_id_of "$target")
  else
    sid="$target"
  fi
  env_file=$(slot_env_file_of "$sid")
  if [[ -f "$env_file" ]]; then
    name=$(env_value "$env_file" GIT_USER_NAME)
    email=$(env_value "$env_file" GIT_USER_EMAIL)
  fi
  if [[ -n "${SLOT_GIT_NAME:-}" ]]; then name=$SLOT_GIT_NAME; email=$SLOT_GIT_EMAIL; fi
  if [[ -z "$name" && "$origin_url" == "$repo_root" ]]; then
    name=$(git -C "$repo_root" config user.name 2>/dev/null || true)
    [[ -n "$name" ]] || name="local-user"
    email=$(git -C "$repo_root" config user.email 2>/dev/null || true)
  fi
  if [[ -z "$name" ]]; then
    {
      echo "$env_file names no GIT_USER_NAME — refusing to leave this checkout's own git" >&2
      echo "identity unset or drifted. An agent dispatched from the host commits with" >&2
      echo "THIS checkout's .git/config, never the container's, and one with no local" >&2
      echo "identity falls through to the HOST's own, which would commit (and push) as the owner." >&2
      echo "Set GIT_USER_NAME (and GIT_USER_EMAIL) in $env_file to the AGENT account's" >&2
      echo "identity, then re-run this." >&2
    } >&2
    exit 1
  fi

  if [[ "$target" == /* && -d "$target/.git" ]]; then
    git -C "$target" config --local user.name "$name"
    git -C "$target" config --local gpg.format ssh
    git -C "$target" config --local user.signingkey /home/node/.ssh/container_signing.pub
    git -C "$target" config --local commit.gpgsign true
    git -C "$target" config --local tag.gpgsign true
    if [[ -n "$email" ]]; then
      git -C "$target" config --local user.email "$email"
    else
      echo "  ! GIT_USER_EMAIL unset in .env — this checkout's local user.name is set, but" >&2
      echo "    a host-side 'git commit' in here will still fail without an email." >&2
    fi
    apply_docs_clone_git_identity "$target" "$name" "$email"
  else
    [[ -n "$email" ]] || {
      echo "  ! GIT_USER_EMAIL unset in .env — this checkout's local user.name is set, but" >&2
      echo "    a host-side 'git commit' in here will still fail without an email." >&2
    }
    slot_sh "$sid" sh -c \
      'set -e; for repo in . docs/repo docs/fix; do [ -e "$repo/.git" ] || continue; git -C "$repo" config --local user.name "$1"; [ -z "$2" ] || git -C "$repo" config --local user.email "$2"; git -C "$repo" config --local gpg.format ssh; git -C "$repo" config --local user.signingkey /home/node/.ssh/container_signing.pub; git -C "$repo" config --local commit.gpgsign true; git -C "$repo" config --local tag.gpgsign true; done' sh "$name" "$email" || die "could not set the git identity inside '$sid' — the reason is above this line."
  fi
  echo "▸ synced this checkout's local git identity to '$name' (from $env_file)"
}

default_branch() {
  local dir="${1:-$repo_root}" ref
  ref=$(git -C "$dir" symbolic-ref --short refs/remotes/origin/HEAD 2>/dev/null) || true
  if [[ -n "$ref" ]]; then
    echo "${ref#origin/}"
  else
    echo "$DEFAULT_BRANCH"
  fi
}

apply_checkout_check_stat() {
  local dir="$1" repo
  for repo in "$dir" "$dir/docs/repo"; do
    git --git-dir="$repo/.git" rev-parse --git-dir >/dev/null 2>&1 || continue
    git --git-dir="$repo/.git" config --local core.checkStat minimal
  done
}

pull_main_checkout() {
  local top; top=$(git -C "$self_dir" rev-parse --show-toplevel 2>/dev/null) || return 0
  [[ "$top" == "$repo_root" ]] || return 0
  local why="main checkout not fast-forwarded:"
  local branch; branch=$(git -C "$repo_root" symbolic-ref --quiet --short HEAD 2>/dev/null) || true
  if [[ "$branch" != "$DEFAULT_BRANCH" ]]; then
    echo "⚠ $why on '${branch:-detached HEAD}', not $DEFAULT_BRANCH" >&2; return 0
  fi
  if [[ -n "$(git -C "$repo_root" status --porcelain --untracked-files=no 2>/dev/null)" ]]; then
    echo "⚠ $why it has uncommitted changes" >&2; return 0
  fi
  if ! forge_git -C "$repo_root" fetch --quiet origin "refs/heads/$DEFAULT_BRANCH" >/dev/null 2>&1; then
    echo "⚠ $why fetch from origin failed" >&2; return 0
  fi
  local old new
  old=$(git -C "$repo_root" rev-parse HEAD) || return 0
  new=$(git -C "$repo_root" rev-parse "origin/$DEFAULT_BRANCH") || return 0
  [[ "$old" == "$new" ]] && return 0
  git -C "$repo_root" merge-base --is-ancestor "origin/$DEFAULT_BRANCH" HEAD && return 0
  if ! git -C "$repo_root" merge-base --is-ancestor HEAD "origin/$DEFAULT_BRANCH"; then
    echo "⚠ $why it has diverged from origin/$DEFAULT_BRANCH" >&2; return 0
  fi
  local err
  if ! err=$(git -C "$repo_root" merge --ff-only --quiet "origin/$DEFAULT_BRANCH" 2>&1 >/dev/null); then
    echo "⚠ $why ${err//$'\n'/ }" >&2; return 0
  fi
  echo "▸ main checkout ${old:0:7}..${new:0:7}"
}
