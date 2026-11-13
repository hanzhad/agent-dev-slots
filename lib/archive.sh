# shellcheck shell=bash disable=SC2034,SC2154

slot_is_abandoned() {
  local dir="$1" sid="$2"
  [[ -f "$(parked_file_of "$sid")" ]] && return 1

  local lease; lease=$(lease_file_of "$sid")
  [[ -f "$lease" ]] || return 0

  if lease_is_live "$lease"; then
    return 1
  fi
  return 0
}

# What this project's rescue directory is called under $STATE/rescue: its key,
# which is its own, where the label only is while no other root claims it. A
# caller with no identity resolved falls back to the label.
rescue_name() {
  printf '%s' "${PROJECT_KEY:-$SLOT_PROJECT_PREFIX}"
}

# A rescue directory still named by the label belongs to the old layout. Refused,
# as the token cache and the signing key are, so an archive is not written or
# looked for in a second place.
rescue_legacy_guard() {
  local name; name=$(rescue_name)
  [[ -n "${PROJECT_KEY:-}" && "$name" != "$SLOT_PROJECT_PREFIX" ]] || return 0
  if [[ -e "$STATE/rescue/$SLOT_PROJECT_PREFIX" && ! -e "$STATE/rescue/$name" ]]; then
    die "rescue: $STATE/rescue/$SLOT_PROJECT_PREFIX is kept under the project's old name. Run \`${CALLER:-dev-container} migrate-identity\` first."
  fi
}

archive_exists_for_slot() {
  local lease="$2" project
  rescue_legacy_guard
  project=$(rescue_name)
  local direct="$STATE/rescue/$project/$lease"
  [[ -d "$direct" ]] && return 0
  local f
  for f in "$STATE/rescue/$project"/*/meta.json; do
    [[ -f "$f" ]] || continue
    local recorded; recorded=$(jq -r '.lease // empty' "$f" 2>/dev/null) || recorded=""
    [[ "$recorded" == "$lease" ]] && return 0
  done
  return 1
}

archive_and_clean_slot() {
  local dir="$1" sid="$2" name="$3"
  local project="$SLOT_PROJECT_PREFIX"
  rescue_legacy_guard
  local rescue_dir="$STATE/rescue/$(rescue_name)"
  local target_dir="$rescue_dir/$name"

  local is_vol=0
  if [[ "$(spec_of "$sid")" == vol:* || ( -n "${slots_dir:-}" && ! -d "$slots_dir/$name" ) ]]; then
    is_vol=1
  fi

  if [[ -z "$dir" || ! -d "$dir" ]]; then
    if [[ -n "${DOCKER_STUB_STATE:-}" && -d "$DOCKER_STUB_STATE/volumes/${sid}-workspace" ]]; then
      dir="$DOCKER_STUB_STATE/volumes/${sid}-workspace"
    fi
  fi

  if [[ -z "$dir" || ! -d "$dir" ]]; then
    teardown_dir "" "$sid"
    rm -f "$(lease_file_of "$sid")" "$STATE/keys/$sid-"*
    return 0
  fi

  if [[ -d "$target_dir" ]]; then
    echo "▸ archive '$target_dir' already exists — deleting slot '$name' only"
    if (( is_vol )); then
      teardown_dir "" "$sid"
    else
      teardown_dir "$dir" "$sid"
    fi
    rm -f "$(lease_file_of "$sid")" "$STATE/keys/$sid-"*
    return 0
  fi

  local tmp_dir="$STATE/rescue/.tmp-$name"
  rm -rf "$tmp_dir" 2>/dev/null || true
  if ! mkdir -p "$tmp_dir"; then
    write_parked "$(parked_file_of "$sid")" "$project" "archive-failed" "$(now_epoch)" "cannot create tmp archive dir"
    echo "failed to create archive directory for '$sid'" >&2
    return 1
  fi

  local has_unpushed=""
  has_unpushed=$(git -C "$dir" log HEAD --branches --not --remotes -n 1 --format=%H 2>/dev/null || true)
  if [[ -n "$has_unpushed" ]]; then
    if ! git -C "$dir" bundle create "$tmp_dir/commits.bundle" HEAD --branches --not --remotes >/dev/null 2>&1; then
      rm -rf "$tmp_dir"
      write_parked "$(parked_file_of "$sid")" "$project" "archive-failed" "$(now_epoch)" "bundle create failed"
      echo "failed to create bundle for '$sid'" >&2
      return 1
    fi
  fi

  if ! git -C "$dir" diff --quiet HEAD 2>/dev/null; then
    if ! git -C "$dir" diff --binary HEAD > "$tmp_dir/changes.patch" 2>/dev/null; then
      rm -rf "$tmp_dir"
      write_parked "$(parked_file_of "$sid")" "$project" "archive-failed" "$(now_epoch)" "git diff failed"
      echo "failed to generate diff for '$sid'" >&2
      return 1
    fi
    if [[ ! -s "$tmp_dir/changes.patch" ]]; then
      rm -f "$tmp_dir/changes.patch"
    fi
  fi

  local limit=${DEV_CONTAINER_ARCHIVE_SIZE_LIMIT:-52428800}
  local -a candidate_files=() untracked_files=() included_files=() omitted_items=()
  local f
  while IFS= read -r f; do
    [[ -n "$f" ]] || continue
    case "$f" in
      node_modules/*|*/node_modules/*|node_modules|*/node_modules) continue ;;
    esac
    candidate_files+=("$f")
  done < <(git -C "$dir" ls-files --others --exclude-standard 2>/dev/null || true)

  if [[ -d "$dir/.local" ]]; then
    while IFS= read -r f; do
      [[ -n "$f" ]] || continue
      case "$f" in
        node_modules/*|*/node_modules/*|node_modules|*/node_modules) continue ;;
      esac
      candidate_files+=("$f")
    done < <(git -C "$dir" ls-files --others .local 2>/dev/null || true)
  fi

  local -A seen_candidates=()
  for f in ${candidate_files[@]+"${candidate_files[@]}"}; do
    f=${f#./}
    [[ -n "${seen_candidates[$f]:-}" ]] && continue
    seen_candidates["$f"]=1
    untracked_files+=("$f")
  done

  local total_size=0
  for f in ${untracked_files[@]+"${untracked_files[@]}"}; do
    local full_path="$dir/$f"
    [[ -f "$full_path" ]] || continue
    local sz
    sz=$(wc -c < "$full_path" 2>/dev/null | tr -d ' ' || echo 0)
    if (( sz > limit || total_size + sz > limit )); then
      omitted_items+=("$(jq -n --arg name "$f" --argjson size "$sz" '{name: $name, size: $size}')")
    else
      included_files+=("$f")
      total_size=$((total_size + sz))
    fi
  done

  if (( ${#included_files[@]} > 0 )); then
    if ! tar -cf "$tmp_dir/new-files.tar" -C "$dir" "${included_files[@]}" 2>/dev/null; then
      rm -rf "$tmp_dir"
      write_parked "$(parked_file_of "$sid")" "$project" "archive-failed" "$(now_epoch)" "tar create failed"
      echo "failed to create tar archive for '$sid'" >&2
      return 1
    fi
  fi

  local has_commits=0 has_patch=0 has_tar=0
  [[ -f "$tmp_dir/commits.bundle" ]] && has_commits=1
  [[ -f "$tmp_dir/changes.patch" && -s "$tmp_dir/changes.patch" ]] && has_patch=1
  [[ -f "$tmp_dir/new-files.tar" ]] && has_tar=1

  if (( ! has_commits && ! has_patch && ! has_tar )); then
    rm -rf "$tmp_dir"
    echo "▸ slot '$name' has nothing to keep — deleting slot without archive"
    if (( is_vol )); then
      teardown_dir "" "$sid"
    else
      teardown_dir "$dir" "$sid"
    fi
    rm -f "$(lease_file_of "$sid")" "$STATE/keys/$sid-"*
    return 0
  fi

  local -a ignored_files=()
  while IFS= read -r f; do
    [[ -n "$f" ]] || continue
    case "$f" in
      node_modules/*|*/node_modules/*|node_modules|*/node_modules) continue ;;
    esac
    [[ -n "${seen_candidates[$f]:-}" ]] && continue
    ignored_files+=("$f")
  done < <(git -C "$dir" ls-files --others -i --exclude-standard 2>/dev/null || true)

  local ignored_json="[]"
  if (( ${#ignored_files[@]} > 0 )); then
    ignored_json=$(printf '%s\n' "${ignored_files[@]}" | jq -R . | jq -s .)
  fi

  local -a branch_names=()
  local b
  while IFS= read -r b; do
    [[ -n "$b" ]] && branch_names+=("$b")
  done < <(git -C "$dir" for-each-ref --format='%(refname:short)' refs/heads/ 2>/dev/null || true)

  local branches_json="[]"
  if (( ${#branch_names[@]} > 0 )); then
    branches_json=$(printf '%s\n' "${branch_names[@]}" | jq -R . | jq -s .)
  fi

  local omitted_json="[]"
  if (( ${#omitted_items[@]} > 0 )); then
    omitted_json=$(printf '%s\n' "${omitted_items[@]}" | jq -s .)
  fi

  local root="${repo_root:-}"
  if [[ -z "$root" ]]; then
    root=$(jq -r '.repo_root // empty' "$(lease_file_of "$sid")" 2>/dev/null || true)
  fi
  local origin="${origin_url:-}"
  if [[ -z "$origin" && -n "$root" && -d "$root" ]]; then
    origin=$(git -C "$root" remote get-url origin 2>/dev/null || true)
  fi
  local def_branch="${DEFAULT_BRANCH:-master}"

  local created_at; created_at=$(now_epoch)
  if ! jq -n \
    --arg project "$project" \
    --arg lease "$name" \
    --arg repo_root "$root" \
    --arg origin "$origin" \
    --arg default_branch "$def_branch" \
    --argjson branches "$branches_json" \
    --argjson ignored "$ignored_json" \
    --argjson omitted "$omitted_json" \
    --argjson created_at "$created_at" \
    '{project: $project, lease: $lease, repo_root: $repo_root, origin: $origin, default_branch: $default_branch, branches: $branches, ignored: $ignored, omitted: $omitted, created_at: $created_at}' \
    > "$tmp_dir/meta.json"; then
    rm -rf "$tmp_dir"
    write_parked "$(parked_file_of "$sid")" "$project" "archive-failed" "$(now_epoch)" "meta.json write failed"
    echo "failed to write meta.json for '$sid'" >&2
    return 1
  fi

  mkdir -p "$rescue_dir" 2>/dev/null || true
  if ! mv "$tmp_dir" "$target_dir"; then
    rm -rf "$tmp_dir"
    write_parked "$(parked_file_of "$sid")" "$project" "archive-failed" "$(now_epoch)" "mv archive failed"
    echo "failed to move archive into place for '$sid'" >&2
    return 1
  fi

  echo "▸ archived slot '$name' to $target_dir"
  if (( is_vol )); then
    teardown_dir "" "$sid"
  else
    teardown_dir "$dir" "$sid"
  fi
  rm -f "$(lease_file_of "$sid")" "$STATE/keys/$sid-"*
  return 0
}

clean_project_slots() {
  local sdir="$1" prefix="$2"
  local -A processed=()
  if [[ -d "$sdir" ]]; then
    local d
    for d in "$sdir"/*/; do
      [[ -d "$d" ]] || continue
      d=${d%/}
      slot_is_ours "$d" || continue
      local sid; sid=$(slot_id_of "$d")
      local name; name=$(basename "$d")
      processed["$sid"]=1
      if slot_is_abandoned "$d" "$sid"; then
        archive_and_clean_slot "$d" "$sid" "$name" || true
      fi
    done
  fi
  local sid
  for sid in $(our_slot_ids); do
    [[ -n "${processed[$sid]:-}" ]] && continue
    local name; name=$(slot_name_of "$sid")
    local d=""
    [[ -n "${DOCKER_STUB_STATE:-}" && -d "$DOCKER_STUB_STATE/volumes/${sid}-workspace" ]] && d="$DOCKER_STUB_STATE/volumes/${sid}-workspace"
    if slot_is_abandoned "$d" "$sid"; then
      archive_and_clean_slot "$d" "$sid" "$name" || true
    fi
  done
}

clean_all_projects() {
  local f root
  local -a roots=()
  for f in "$STATE"/leases/*.json; do
    [[ -f "$f" ]] || continue
    root=$(jq -r '.repo_root // empty' "$f" 2>/dev/null) || root=""
    [[ -n "$root" && -d "$root" ]] && roots+=("$root")
  done
  local r
  for r in $(printf '%s\n' "${roots[@]}" | sort -u); do
    [[ -d "$r" ]] || continue
    DEV_CONTAINER_REPO_ROOT="$r" DEV_CONTAINER_CALLER_PWD="$r" bash "$engine_src" clean || true
  done
}
