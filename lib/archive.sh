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

archive_fail_park() {
  local tmp_dir="$1" sid="$2" project="$3" reason="$4" msg="$5"
  rm -rf "$tmp_dir"
  write_parked "$(parked_file_of "$sid")" "$project" "archive-failed" "$(now_epoch)" "$reason"
  echo "$msg" >&2
  return 1
}

archive_partition_untracked() {
  local limit="$1"
  included_files=()
  omitted_items=()
  local total_size=0 sz f
  for entry in ${candidate_entries[@]+"${candidate_entries[@]}"}; do
    sz=${entry%%$'\t'*}
    f=${entry#*$'\t'}
    if (( sz > limit || total_size + sz > limit )); then
      omitted_items+=("$(jq -n --arg name "$f" --argjson size "$sz" '{name: $name, size: $size}')")
    else
      included_files+=("$f")
      total_size=$((total_size + sz))
    fi
  done
}

archive_collect_repo_host_dir() {
  local dir="$1" out_dir="$2" tmp_dir="$3" sid="$4" project="$5" limit="$6"
  local is_ro="${7:-0}" pin_file="${8:-}" root_dir="${9:-}"
  git -C "$dir" rev-parse --git-dir >/dev/null 2>&1 || return 0
  repo_origin_url=$(git -C "$dir" config --get remote.origin.url 2>/dev/null || git -C "$dir" remote get-url origin 2>/dev/null || true)

  local skip_head_unpushed=0
  if (( is_ro )) && [[ -z "$(git -C "$dir" symbolic-ref --quiet --short HEAD 2>/dev/null || true)" ]]; then
    local cur_head marker_pin root_pin=""
    cur_head=$(git -C "$dir" rev-parse HEAD 2>/dev/null || true)
    marker_pin=$(docs_repo_marker_pin "$dir" 2>/dev/null || true)
    if [[ -n "$pin_file" && -n "$root_dir" ]]; then
      root_pin=$(git -C "$root_dir" show "origin/$(default_branch "$root_dir"):$pin_file" 2>/dev/null | grep -v '^[[:space:]]*#' | tr -d '[:space:]' || true)
    fi
    if [[ -n "$cur_head" ]] && [[ "$cur_head" == "$marker_pin" || ( -n "$root_pin" && "$cur_head" == "$root_pin" ) ]]; then
      skip_head_unpushed=1
    fi
  fi

  local has_unpushed=""
  if git -C "$dir" rev-parse --verify HEAD >/dev/null 2>&1; then
    if (( skip_head_unpushed )); then
      has_unpushed=$(git -C "$dir" log --branches --not --remotes -n 1 --format=%H 2>/dev/null || true)
      if [[ -n "$has_unpushed" ]]; then
        if ! git -C "$dir" bundle create "$out_dir/commits.bundle" --branches --not --remotes >/dev/null 2>&1; then
          archive_fail_park "$tmp_dir" "$sid" "$project" "bundle create failed" "failed to create bundle for '$sid'"
          return 1
        fi
      fi
    else
      has_unpushed=$(git -C "$dir" log HEAD --branches --not --remotes -n 1 --format=%H 2>/dev/null || true)
      if [[ -n "$has_unpushed" ]]; then
        if ! git -C "$dir" bundle create "$out_dir/commits.bundle" HEAD --branches --not --remotes >/dev/null 2>&1; then
          archive_fail_park "$tmp_dir" "$sid" "$project" "bundle create failed" "failed to create bundle for '$sid'"
          return 1
        fi
      fi
    fi

    if ! git -C "$dir" diff --quiet HEAD 2>/dev/null; then
      if ! git -C "$dir" diff --binary HEAD > "$out_dir/changes.patch" 2>/dev/null; then
        archive_fail_park "$tmp_dir" "$sid" "$project" "git diff failed" "failed to generate diff for '$sid'"
        return 1
      fi
      [[ -s "$out_dir/changes.patch" ]] || rm -f "$out_dir/changes.patch"
    fi
  fi

  local -a raw_candidates=()
  local f
  while IFS= read -r f; do
    [[ -n "$f" ]] || continue
    case "$f" in
      node_modules/*|*/node_modules/*|node_modules|*/node_modules) continue ;;
    esac
    raw_candidates+=("$f")
  done < <(git -C "$dir" ls-files --others --exclude-standard 2>/dev/null || true)

  if [[ -d "$dir/.local" ]]; then
    while IFS= read -r f; do
      [[ -n "$f" ]] || continue
      case "$f" in
        node_modules/*|*/node_modules/*|node_modules|*/node_modules) continue ;;
      esac
      raw_candidates+=("$f")
    done < <(git -C "$dir" ls-files --others .local 2>/dev/null || true)
  fi

  for f in ${raw_candidates[@]+"${raw_candidates[@]}"}; do
    f=${f#./}
    [[ -n "${seen_candidates[$f]:-}" ]] && continue
    seen_candidates["$f"]=1
    local full_path="$dir/$f"
    [[ -f "$full_path" ]] || continue
    local sz
    sz=$(wc -c < "$full_path" 2>/dev/null | tr -d ' ' || echo 0)
    candidate_entries+=("${sz}"$'\t'"${f}")
  done

  archive_partition_untracked "$limit"

  if (( ${#included_files[@]} > 0 )); then
    if ! tar -cf "$out_dir/new-files.tar" -C "$dir" "${included_files[@]}" 2>/dev/null; then
      archive_fail_park "$tmp_dir" "$sid" "$project" "tar create failed" "failed to create tar archive for '$sid'"
      return 1
    fi
  fi

  while IFS= read -r f; do
    [[ -n "$f" ]] || continue
    case "$f" in
      node_modules/*|*/node_modules/*|node_modules|*/node_modules) continue ;;
    esac
    [[ -n "${seen_candidates[$f]:-}" ]] && continue
    ignored_files+=("$f")
  done < <(git -C "$dir" ls-files --others -i --exclude-standard 2>/dev/null || true)

  local b
  while IFS= read -r b; do
    [[ -n "$b" ]] && branch_names+=("$b")
  done < <(git -C "$dir" for-each-ref --format='%(refname:short)' refs/heads/ 2>/dev/null || true)
}

archive_collect_repo_volume() {
  local subpath="$1" out_dir="$2" tmp_dir="$3" sid="$4" project="$5" limit="$6"
  local is_ro="${7:-0}" pin_file="${8:-}"

  local probe_out
  if ! probe_out=$(slot_exec_stream "$sid" sh -s -- "$subpath" "$is_ro" "$pin_file" 2>/dev/null <<'PROBE'
    set -e
    command -v git >/dev/null
    sub=$1 is_ro=$2 pin_file=$3
    expected_pin=""
    if [ "$is_ro" = 1 ] && [ -n "$pin_file" ] && [ -e .git ]; then
      def=$(git -c safe.directory="*" symbolic-ref --short refs/remotes/origin/HEAD 2>/dev/null || true)
      def=${def#origin/}
      if [ -n "$def" ]; then
        expected_pin=$( (git -c safe.directory="*" show "origin/$def:$pin_file" 2>/dev/null || true) | grep -v "^[[:space:]]*#" | tr -d "[:space:]" )
      fi
    fi
    cd "$sub" 2>/dev/null || exit 0
    [ -e .git ] || exit 0
    rc=0
    git -c safe.directory="*" rev-parse --git-dir >/dev/null 2>&1 || rc=$?
    if [ "$rc" -ne 0 ]; then
      [ "$rc" -eq 128 ] && exit 0
      exit "$rc"
    fi
    origin_u=$(git -c safe.directory="*" config --get remote.origin.url 2>/dev/null || git -c safe.directory="*" remote get-url origin 2>/dev/null || true)
    printf "O\t%s\n" "$origin_u"
    if git -c safe.directory="*" rev-parse --verify HEAD >/dev/null 2>&1; then
      skip_head=0
      if [ "$is_ro" = 1 ] && ! git -c safe.directory="*" symbolic-ref --quiet HEAD >/dev/null 2>&1; then
        cur_head=$(git -c safe.directory="*" rev-parse HEAD 2>/dev/null || true)
        gdir=$(git -c safe.directory="*" rev-parse --git-dir 2>/dev/null || true)
        marker_pin=""
        [ -n "$gdir" ] && [ -f "$gdir/slot-docs-pin" ] && marker_pin=$(cat "$gdir/slot-docs-pin" 2>/dev/null || true)
        if [ -n "$cur_head" ] && { [ "$cur_head" = "$marker_pin" ] || { [ -n "$expected_pin" ] && [ "$cur_head" = "$expected_pin" ]; }; }; then
          skip_head=1
        fi
      fi
      if [ "$skip_head" = 1 ]; then
        if [ -n "$(git -c safe.directory="*" log --branches --not --remotes -n 1 --format=%H 2>/dev/null || true)" ]; then
          printf "H\tBRANCHES\n"
        fi
      else
        if [ -n "$(git -c safe.directory="*" log HEAD --branches --not --remotes -n 1 --format=%H 2>/dev/null || true)" ]; then
          printf "H\tHEAD\n"
        fi
      fi
      if ! git -c safe.directory="*" diff --quiet HEAD 2>/dev/null; then
        printf "D\t1\n"
      fi
    fi
    git -c safe.directory="*" ls-files --others --exclude-standard | while IFS= read -r f; do
      [ -n "$f" ] || continue
      case "$f" in node_modules/*|*/node_modules/*|node_modules|*/node_modules) continue ;; esac
      [ -f "$f" ] || continue
      printf "U\t%s\t%s\n" "$(wc -c < "$f" | tr -d " ")" "$f"
    done
    if [ -d .local ]; then
      git -c safe.directory="*" ls-files --others .local | while IFS= read -r f; do
        [ -n "$f" ] || continue
        case "$f" in node_modules/*|*/node_modules/*|node_modules|*/node_modules) continue ;; esac
        [ -f "$f" ] || continue
        printf "U\t%s\t%s\n" "$(wc -c < "$f" | tr -d " ")" "$f"
      done
    fi
    git -c safe.directory="*" ls-files --others -i --exclude-standard | while IFS= read -r f; do
      [ -n "$f" ] || continue
      case "$f" in node_modules/*|*/node_modules/*|node_modules|*/node_modules) continue ;; esac
      printf "I\t%s\n" "$f"
    done
    git -c safe.directory="*" for-each-ref --format="%(refname:short)" refs/heads/ | while IFS= read -r b; do
      [ -n "$b" ] || continue
      printf "B\t%s\n" "$b"
    done
PROBE
  ); then
    archive_fail_park "$tmp_dir" "$sid" "$project" "archive probe failed" "failed to list files for '$sid'"
    return 1
  fi

  local kind rest sz f bundle_mode="" has_diff=0
  while IFS=$'\t' read -r kind rest; do
    [[ -n "$kind" ]] || continue
    case "$kind" in
      O) repo_origin_url="$rest" ;;
      H) bundle_mode="$rest" ;;
      D) has_diff=1 ;;
      U)
        sz=${rest%%$'\t'*}
        f=${rest#*$'\t'}
        f=${f#./}
        [[ -n "${seen_candidates[$f]:-}" ]] && continue
        seen_candidates["$f"]=1
        candidate_entries+=("${sz}"$'\t'"${f}")
        ;;
      I)
        f=${rest#./}
        [[ -n "${seen_candidates[$f]:-}" ]] && continue
        ignored_files+=("$f")
        ;;
      B)
        [[ -n "$rest" ]] && branch_names+=("$rest")
        ;;
    esac
  done <<<"$probe_out"

  if [[ -n "$bundle_mode" ]]; then
    if ! slot_exec_stream "$sid" sh -c 'cd "$1" && if [ "$2" = BRANCHES ]; then git -c safe.directory="*" bundle create - --branches --not --remotes 2>/dev/null; else git -c safe.directory="*" bundle create - HEAD --branches --not --remotes 2>/dev/null; fi' sh "$subpath" "$bundle_mode" > "$out_dir/commits.bundle" 2>/dev/null; then
      archive_fail_park "$tmp_dir" "$sid" "$project" "bundle create failed" "failed to create bundle for '$sid'"
      return 1
    fi
  fi

  if (( has_diff )); then
    if ! slot_exec_stream "$sid" sh -c 'cd "$1" && git -c safe.directory="*" diff --binary HEAD' sh "$subpath" > "$out_dir/changes.patch" 2>/dev/null; then
      archive_fail_park "$tmp_dir" "$sid" "$project" "git diff failed" "failed to generate diff for '$sid'"
      return 1
    fi
    [[ -s "$out_dir/changes.patch" ]] || rm -f "$out_dir/changes.patch"
  fi

  archive_partition_untracked "$limit"

  if (( ${#included_files[@]} > 0 )); then
    if ! printf '%s\n' "${included_files[@]}" | slot_exec_stream "$sid" tar -C "$subpath" -cf - -T - > "$out_dir/new-files.tar" 2>/dev/null; then
      archive_fail_park "$tmp_dir" "$sid" "$project" "tar create failed" "failed to create tar archive for '$sid'"
      return 1
    fi
  fi
}

archive_collect_host_dir() {
  archive_collect_repo_host_dir "$1" "$2" "$2" "$3" "$4" "$5" 0 "" "$1"
}

archive_collect_volume() {
  archive_collect_repo_volume "." "$1" "$1" "$2" "$3" "$4" 0 ""
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

  local lease; lease=$(lease_file_of "$sid")
  if lease_is_ephemeral "$lease"; then
    echo "▸ slot '$name' is ephemeral — discarding without rescue archive"
    if (( is_vol )); then
      teardown_dir "" "$sid"
    else
      teardown_dir "$dir" "$sid"
    fi
    rm -f "$lease" "$(lease_token_file_of "$sid")" "$STATE/keys/$sid-"*
    return 0
  fi

  if [[ -z "$dir" || ! -d "$dir" ]]; then
    if [[ -n "${DOCKER_STUB_STATE:-}" && -d "$DOCKER_STUB_STATE/volumes/${sid}-workspace" ]]; then
      dir="$DOCKER_STUB_STATE/volumes/${sid}-workspace"
    fi
  fi

  if [[ -d "$target_dir" ]]; then
    echo "▸ archive '$target_dir' already exists — deleting slot '$name' only"
    if (( is_vol )); then
      teardown_dir "" "$sid"
    else
      teardown_dir "$dir" "$sid"
    fi
    rm -f "$(lease_file_of "$sid")" "$(lease_token_file_of "$sid")" "$STATE/keys/$sid-"*
    return 0
  fi

  if [[ -z "$dir" || ! -d "$dir" ]] && [[ "$(spec_of "$sid")" != vol:* ]]; then
    teardown_dir "" "$sid"
    rm -f "$(lease_file_of "$sid")" "$(lease_token_file_of "$sid")" "$STATE/keys/$sid-"*
    return 0
  fi

  local tmp_dir="$STATE/rescue/.tmp-$name"
  rm -rf "$tmp_dir" 2>/dev/null || true
  if ! mkdir -p "$tmp_dir"; then
    write_parked "$(parked_file_of "$sid")" "$project" "archive-failed" "$(now_epoch)" "cannot create tmp archive dir"
    echo "failed to create archive directory for '$sid'" >&2
    return 1
  fi

  local limit=${DEV_CONTAINER_ARCHIVE_SIZE_LIMIT:-52428800}
  local -a candidate_entries=() included_files=() omitted_items=() ignored_files=() branch_names=()
  local -A seen_candidates=()
  local repo_origin_url=""

  if [[ -n "$dir" && -d "$dir" ]]; then
    archive_collect_host_dir "$dir" "$tmp_dir" "$sid" "$project" "$limit" || return 1
  else
    archive_collect_volume "$tmp_dir" "$sid" "$project" "$limit" || return 1
  fi

  local has_commits=0 has_patch=0 has_tar=0
  [[ -f "$tmp_dir/commits.bundle" ]] && has_commits=1
  [[ -f "$tmp_dir/changes.patch" && -s "$tmp_dir/changes.patch" ]] && has_patch=1
  [[ -f "$tmp_dir/new-files.tar" ]] && has_tar=1

  local ignored_json="[]"
  if (( ${#ignored_files[@]} > 0 )); then
    ignored_json=$(printf '%s\n' "${ignored_files[@]}" | jq -R . | jq -s .)
  fi

  local branches_json="[]"
  if (( ${#branch_names[@]} > 0 )); then
    branches_json=$(printf '%s\n' "${branch_names[@]}" | jq -R . | jq -s .)
  fi

  local omitted_json="[]"
  if (( ${#omitted_items[@]} > 0 )); then
    omitted_json=$(printf '%s\n' "${omitted_items[@]}" | jq -s .)
  fi

  local -a repo_meta_items=()
  local any_sub_archived=0
  local -a sub_rows=()
  local sub_row
  while IFS= read -r sub_row; do
    [[ -n "$sub_row" ]] && sub_rows+=("$sub_row")
  done < <(slot_repos_union_tsv "$sid")

  if (( ${#sub_rows[@]} > 0 )); then
    local present_paths=" "
    if [[ -n "$dir" && -d "$dir" ]]; then
      for sub_row in "${sub_rows[@]}"; do
        IFS=$'\037' read -r _ spath _ <<<"$sub_row"
        if [[ -f "$dir/$spath/.git/HEAD" || -f "$dir/$spath/.git" ]]; then
          present_paths+="$spath "
        fi
      done
    else
      local -a check_paths=()
      for sub_row in "${sub_rows[@]}"; do
        IFS=$'\037' read -r _ spath _ <<<"$sub_row"
        check_paths+=("$spath")
      done
      local present_out=""
      present_out=$(slot_exec_stream "$sid" sh -c 'for p in "$@"; do if [ -f "$p/.git/HEAD" ] || [ -f "$p/.git" ]; then printf "%s\n" "$p"; fi; done' sh "${check_paths[@]}" 2>/dev/null) || {
        archive_fail_park "$tmp_dir" "$sid" "$project" "archive probe failed" "failed to inspect attached repos for '$sid'"
        return 1
      }
      local pp
      while IFS= read -r pp; do
        [[ -n "$pp" ]] && present_paths+="$pp "
      done <<<"$present_out"
    fi

    local alias path url slug mode attach rbranch pin_file auth rw_alias legacy_docs
    for sub_row in "${sub_rows[@]}"; do
      IFS=$'\037' read -r alias path url slug mode attach rbranch pin_file auth rw_alias legacy_docs <<<"$sub_row"
      case "$present_paths" in
        *" $path "*) ;;
        *) continue ;;
      esac
      candidate_entries=(); included_files=(); omitted_items=(); ignored_files=(); branch_names=()
      seen_candidates=(); repo_origin_url=""
      local sub_out="$tmp_dir/repos/$alias"
      mkdir -p "$sub_out"
      local is_ro=0
      [[ "$mode" == "ro" ]] && is_ro=1
      if [[ -n "$dir" && -d "$dir" ]]; then
        archive_collect_repo_host_dir "$dir/$path" "$sub_out" "$tmp_dir" "$sid" "$project" "$limit" "$is_ro" "$pin_file" "$dir" || return 1
      else
        archive_collect_repo_volume "$path" "$sub_out" "$tmp_dir" "$sid" "$project" "$limit" "$is_ro" "$pin_file" || return 1
      fi
      local -a sub_files=()
      [[ -f "$sub_out/commits.bundle" ]] && sub_files+=("commits.bundle")
      [[ -f "$sub_out/changes.patch" && -s "$sub_out/changes.patch" ]] && sub_files+=("changes.patch")
      [[ -f "$sub_out/new-files.tar" ]] && sub_files+=("new-files.tar")
      if (( ${#sub_files[@]} == 0 )); then
        rm -rf "$sub_out"
        rmdir "$tmp_dir/repos" 2>/dev/null || true
        continue
      fi
      any_sub_archived=1
      local sub_url="$url"
      [[ -n "$sub_url" ]] || sub_url="$repo_origin_url"
      local sub_branches_json="[]" sub_ignored_json="[]" sub_omitted_json="[]" sub_files_json="[]"
      (( ${#branch_names[@]} == 0 )) || sub_branches_json=$(printf '%s\n' "${branch_names[@]}" | jq -R . | jq -s .)
      (( ${#ignored_files[@]} == 0 )) || sub_ignored_json=$(printf '%s\n' "${ignored_files[@]}" | jq -R . | jq -s .)
      (( ${#omitted_items[@]} == 0 )) || sub_omitted_json=$(printf '%s\n' "${omitted_items[@]}" | jq -s .)
      sub_files_json=$(printf '%s\n' "${sub_files[@]}" | jq -R . | jq -s .)
      local sub_meta
      sub_meta=$(jq -n \
        --arg alias "$alias" \
        --arg path "$path" \
        --arg url "$sub_url" \
        --arg mode "$mode" \
        --argjson branches "$sub_branches_json" \
        --argjson ignored "$sub_ignored_json" \
        --argjson omitted "$sub_omitted_json" \
        --argjson files "$sub_files_json" \
        '{alias: $alias, path: $path, url: $url, mode: $mode, branches: $branches, ignored: $ignored, omitted: $omitted, files: $files}')
      repo_meta_items+=("$sub_meta")
    done
  fi

  if (( ! has_commits && ! has_patch && ! has_tar && ! any_sub_archived )); then
    rm -rf "$tmp_dir"
    echo "▸ slot '$name' has nothing to keep — deleting slot without archive"
    if (( is_vol )); then
      teardown_dir "" "$sid"
    else
      teardown_dir "$dir" "$sid"
    fi
    rm -f "$(lease_file_of "$sid")" "$(lease_token_file_of "$sid")" "$STATE/keys/$sid-"*
    return 0
  fi

  local repos_json="[]"
  if (( ${#repo_meta_items[@]} > 0 )); then
    repos_json=$(printf '%s\n' "${repo_meta_items[@]}" | jq -s .)
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
    --argjson repos "$repos_json" \
    --argjson created_at "$created_at" \
    '{project: $project, lease: $lease, repo_root: $repo_root, origin: $origin, default_branch: $default_branch, branches: $branches, ignored: $ignored, omitted: $omitted, repos: $repos, created_at: $created_at}' \
    > "$tmp_dir/meta.json"; then
    archive_fail_park "$tmp_dir" "$sid" "$project" "meta.json write failed" "failed to write meta.json for '$sid'"
    return 1
  fi

  mkdir -p "$rescue_dir" 2>/dev/null || true
  if ! mv "$tmp_dir" "$target_dir"; then
    archive_fail_park "$tmp_dir" "$sid" "$project" "mv archive failed" "failed to move archive into place for '$sid'"
    return 1
  fi

  echo "▸ archived slot '$name' to $target_dir"
  if (( is_vol )); then
    teardown_dir "" "$sid"
  else
    teardown_dir "$dir" "$sid"
  fi
  rm -f "$(lease_file_of "$sid")" "$(lease_token_file_of "$sid")" "$STATE/keys/$sid-"*
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
