# shellcheck shell=bash disable=SC2034,SC2154

# Batched repository cloning for volume-backed slots. Clones sharing the same
# auth mode ("project" vs "none") are grouped into a single slot_helper_run_online
# invocation; "none" omits FORGE_HOST_SECRET and never triggers credential loading.

# POSIX sh script run inside /workspace of the helper container.
# $1: JSON array of repos to clone in this batch
# $2: fallback default branch of the primary repository
# $3: space-separated catalog paths to ensure in .git/info/exclude
# shellcheck disable=SC2016
REPOS_VOLUME_SCRIPT='set -e
batch=$1
fallback=$2
excludes=$3
SEP=$(printf "\037")

if [ -n "$excludes" ] && [ -e .git ]; then
  exclude=$(git rev-parse --path-format=absolute --git-path info/exclude 2>/dev/null || git rev-parse --git-path info/exclude 2>/dev/null || echo ".git/info/exclude")
  case "$exclude" in /*) ;; *) exclude="$PWD/$exclude" ;; esac
  mkdir -p "$(dirname "$exclude")"
  touch "$exclude"
  for p in $excludes; do
    [ -n "$p" ] || continue
    grep -Fxq "/$p" "$exclude" 2>/dev/null || printf "/%s\n" "$p" >>"$exclude"
  done
fi

has_pin=$(printf "%s" "$batch" | jq -r "[.[] | select((.pin_file // \"\") != \"\")] | length")
branch=$fallback
if [ "$has_pin" -gt 0 ] && [ -e .git ]; then
  branch=$(git symbolic-ref --short refs/remotes/origin/HEAD 2>/dev/null) || branch=$fallback
  branch=${branch#origin/}
  git fetch --quiet origin "refs/heads/$branch" 2>/dev/null \
    || echo "  ! could not fetch origin — pinned repo(s) follow the pin on the main checkouts own $branch, which may be behind" >&2
fi

entries=$(printf "%s" "$batch" | jq -r ".[] | [.alias, .path, .url, .mode, (.branch // \"\"), (.pin_file // \"\"), (.depth | tostring), (.rw_alias // \"\"), (if .legacy_docs then \"1\" else \"0\" end)] | join(\"\\u001f\")")
[ -n "$entries" ] || exit 0

printf "%s\n" "$entries" | while IFS="$SEP" read -r alias path url mode rbranch pinfile depth rw_alias legacy_docs; do
  [ -n "$alias" ] || continue
  if [ -e "$path/.git" ]; then
    continue
  fi
  mkdir -p "$(dirname "$path")"
  clone_url=$url
  if [ "$depth" != "full" ]; then
    case "$url" in
      /*) clone_url="file://$url" ;;
    esac
  fi
  set -- clone --quiet
  if [ "$depth" != "full" ]; then
    set -- "$@" --depth "$depth"
  fi
  if [ -n "$rbranch" ]; then
    set -- "$@" --branch "$rbranch"
  fi
  set -- "$@" -- "$clone_url" "$path"
  if ! git "$@"; then
    rm -rf "$path"
    exit 1
  fi
  if [ "$clone_url" != "$url" ]; then
    git -C "$path" remote set-url origin "$url"
  fi
  git -C "$path" config --local core.checkStat minimal

  if [ -n "$pinfile" ]; then
    pin=$(git show "origin/$branch:$pinfile" 2>/dev/null | grep -v "^[[:space:]]*#" | tr -d "[:space:]" || true)
    if [ -z "$pin" ]; then
      echo "  ! no $pinfile on origin/$branch — $path left where it is" >&2
    else
      head=$(git -C "$path" rev-parse HEAD 2>/dev/null) || head=nothing
      if [ "$head" != "$pin" ]; then
        echo "▸ moving $path to pin ($pinfile)"
        if ! git -C "$path" cat-file -e "$pin^{commit}" 2>/dev/null; then
          if [ "$depth" = "full" ]; then
            git -C "$path" fetch --quiet origin "$pin" \
              || echo "  ! could not fetch pin into $path" >&2
          else
            git -C "$path" fetch --quiet --depth 1 origin "$pin" \
              || echo "  ! could not fetch pin into $path" >&2
          fi
        fi
        if git -C "$path" checkout --quiet --detach "$pin"; then
          echo "$pin" >"$(git -C "$path" rev-parse --absolute-git-dir)/slot-docs-pin"
        fi
      fi
    fi
  fi

  if [ "$mode" = "ro" ]; then
    hooks=".git/slot-readonly-hooks"
    if [ "$legacy_docs" = "1" ]; then
      slug=$(printf "%s" "$url" | sed -E "s,^(https?://|ssh://)?(git@)?github[.]com[:/],,; s,/+$,,; s,[.]git$,,")
      pushurl="read-only://docs-repo-is-read-only--clone-docs-fix"
      refusal="docs/repo is read-only; clone ${slug:-$url} into docs/fix and make the change there, on a branch off its default branch"
    else
      pushurl="read-only://slot-repo-is-read-only--$alias"
      if [ -n "$rw_alias" ]; then
        refusal="$path ($alias) is read-only; attach rw_alias '\''$rw_alias'\'' instead"
      else
        refusal="$path ($alias) is read-only"
      fi
    fi
    mkdir -p "$path/$hooks"
    for h in pre-commit pre-push; do
      printf "#!/bin/sh\ncat >&2 <<'\''MSG'\''\n%s\nMSG\nexit 1\n" "$refusal" >"$path/$hooks/$h"
      chmod +x "$path/$hooks/$h"
    done
    git -C "$path" config --local core.hooksPath "$hooks"
    git -C "$path" config --local remote.origin.pushurl "$pushurl"
  fi
done'

clone_repos_batch_in_volume() {
  local proj="$1" auth_mode="$2" batch_json="$3" excludes="${4:-}"
  local excludes_sp
  excludes_sp=$(printf '%s' "$excludes" | tr '\n' ' ')
  local count
  count=$(printf '%s' "$batch_json" | jq 'length')
  (( count > 0 )) || return 0

  if [[ "$auth_mode" == "none" ]]; then
    forge_container_env_none
    (
      unset FORGE_HOST_SECRET
      slot_helper_run_online "$proj" sh -s -- "$batch_json" "$DEFAULT_BRANCH" "$excludes_sp" <<<"$REPOS_VOLUME_SCRIPT"
    )
    return $?
  fi

  local forge_url
  forge_url=$(printf '%s' "$batch_json" | jq -r --arg origin "$FORGE_HOST_ORIGIN" '[.[].url | select(startswith($origin + "/"))] | first // empty')
  if [[ -n "$forge_url" ]]; then
    forge_container_env "$forge_url" || return 1
  else
    forge_container_env "non-forge" || return 1
  fi
  (
    export FORGE_HOST_SECRET
    slot_helper_run_online "$proj" sh -s -- "$batch_json" "$DEFAULT_BRANCH" "$excludes_sp" <<<"$REPOS_VOLUME_SCRIPT"
  )
}

clone_repos_in_volume() {
  local proj="$1" repos_json="$2" excludes_nl="${3:-}"
  local project_batch none_batch
  project_batch=$(printf '%s' "$repos_json" | jq -c '[.[] | select((.auth // "project") == "project")]')
  none_batch=$(printf '%s' "$repos_json" | jq -c '[.[] | select((.auth // "project") == "none")]')

  if [[ "$(printf '%s' "$project_batch" | jq 'length')" -gt 0 ]]; then
    echo "▸ cloning project-authenticated repository batch into $proj"
    clone_repos_batch_in_volume "$proj" project "$project_batch" "$excludes_nl" || return 1
  fi
  if [[ "$(printf '%s' "$none_batch" | jq 'length')" -gt 0 ]]; then
    echo "▸ cloning public (auth=none) repository batch into $proj"
    clone_repos_batch_in_volume "$proj" none "$none_batch" "$excludes_nl" || return 1
  fi
  return 0
}

clone_repos_in_dir() {
  local target_dir="$1" repos_json="$2"
  local count
  count=$(printf '%s' "$repos_json" | jq 'length')
  (( count > 0 )) || return 0
  local alias path url mode auth rbranch pinfile depth rw_alias legacy_docs
  local -a clone_args
  local sep=$'\037'
  while IFS="$sep" read -r alias path url mode auth rbranch pinfile depth rw_alias legacy_docs; do
    [[ -n "$alias" ]] || continue
    [[ ! -e "$target_dir/$path/.git" ]] || continue
    mkdir -p "$(dirname "$target_dir/$path")"
    clone_args=(clone --quiet)
    [[ "$depth" == "full" ]] || clone_args+=(--depth "$depth")
    [[ -z "$rbranch" ]] || clone_args+=(--branch "$rbranch")
    clone_args+=(-- "$url" "$target_dir/$path")
    if [[ "$auth" == "none" ]]; then
      GIT_TERMINAL_PROMPT=0 git "${clone_args[@]}" || return 1
    else
      forge_git "${clone_args[@]}" || return 1
    fi
    ensure_excluded "$target_dir" "/$path"
    if [[ -n "$pinfile" ]]; then
      forge_git -C "$target_dir" fetch --quiet origin "refs/heads/$(default_branch "$target_dir")" 2>/dev/null \
        || echo "  ! could not fetch origin — $path follows the pin on $repo_root's own $(default_branch "$target_dir"), which may be behind" >&2
      local pin head
      pin=$(git -C "$target_dir" show "origin/$(default_branch "$target_dir"):$pinfile" 2>/dev/null | grep -v '^[[:space:]]*#' | tr -d '[:space:]' || true)
      if [[ -n "$pin" ]]; then
        head=$(git -C "$target_dir/$path" rev-parse HEAD 2>/dev/null || echo nothing)
        if [[ "$head" != "$pin" ]]; then
          if ! git -C "$target_dir/$path" cat-file -e "$pin^{commit}" 2>/dev/null; then
            local -a df=()
            [[ "$depth" == "full" ]] || df=(--depth 1)
            forge_git -C "$target_dir/$path" fetch --quiet ${df[@]+"${df[@]}"} origin "$pin" || true
          fi
          if git -C "$target_dir/$path" checkout --quiet --detach "$pin"; then
            docs_repo_write_marker_pin "$target_dir/$path" "$pin"
          fi
        fi
      fi
    fi
    if [[ "$mode" == "ro" ]]; then
      guard_repo_readonly "$target_dir" "$path" "$alias" "$rw_alias" "$legacy_docs"
    fi
  done < <(printf '%s' "$repos_json" | jq -r '.[] | [.alias, .path, .url, .mode, (.auth // "project"), (.branch // ""), (.pin_file // ""), (.depth | tostring), (.rw_alias // ""), (if .legacy_docs then "1" else "0" end)] | join("\u001f")')
}
