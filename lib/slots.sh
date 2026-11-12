# shellcheck shell=bash disable=SC2034,SC2154

slot_name_of() {
  local base; base=$(basename "$1")
  base=${base%-workspace}
  echo "${base#"${SLOT_PROJECT_PREFIX}"-}"
}

slot_id_of() {
  local base; base=$(basename "$1")
  base=${base%-workspace}
  case "$base" in
    "${SLOT_PROJECT_PREFIX}-"*) echo "$base" ;;
    *) echo "${SLOT_PROJECT_PREFIX}-$base" ;;
  esac
}

slot_is_ours() {
  local target="$1"
  if [[ "$target" == /* && -d "$target" ]]; then
    [[ -d "$target/.git" ]] || return 1
    local o; o=$(git -C "$target" remote get-url origin 2>/dev/null) || return 1
    [[ "$o" == "$origin_url" ]]
    return
  fi
  local sid; sid=$(slot_id_of "$target")
  local spec v
  spec=$(spec_of "$sid")
  case "$spec" in
    vol:*)
      v=${spec#vol:}
      local o p
      o=$(docker volume inspect "$v" --format '{{index .Labels "dev-container.origin"}}' 2>/dev/null || true)
      p=$(docker volume inspect "$v" --format '{{index .Labels "dev-container.project"}}' 2>/dev/null || true)
      [[ "$p" == "$SLOT_PROJECT_PREFIX" ]] || return 1
      [[ -z "$origin_url" || "$o" == "$origin_url" ]]
      ;;
    dir:*)
      local dir="${spec#dir:}"
      [[ -d "$dir/.git" ]] || return 1
      local o; o=$(git -C "$dir" remote get-url origin 2>/dev/null) || return 1
      [[ "$o" == "$origin_url" ]]
      ;;
    *)
      local vol; vol=$(slot_volume_of "$sid")
      if docker volume inspect "$vol" >/dev/null 2>&1; then
        local o p
        o=$(docker volume inspect "$vol" --format '{{index .Labels "dev-container.origin"}}' 2>/dev/null || true)
        p=$(docker volume inspect "$vol" --format '{{index .Labels "dev-container.project"}}' 2>/dev/null || true)
        [[ "$p" == "$SLOT_PROJECT_PREFIX" ]] || return 1
        [[ -z "$origin_url" || "$o" == "$origin_url" ]]
        return
      fi
      return 1
      ;;
  esac
}

foreign_slot_exists() {
  local name="$1" v proj_lbl
  while IFS= read -r v; do
    [[ -n "$v" ]] || continue
    proj_lbl=$(docker volume inspect "$v" --format '{{index .Labels "dev-container.project"}}' 2>/dev/null || true)
    if [[ -n "$proj_lbl" && "$proj_lbl" != "$SLOT_PROJECT_PREFIX" ]]; then
      return 0
    fi
  done < <(docker volume ls --format '{{.Name}}' 2>/dev/null | grep -E -- "-${name}-workspace$" || true)
  return 1
}

our_slot_ids() {
  local -a vfilters=("--filter" "label=dev-container.project=$SLOT_PROJECT_PREFIX")
  if [[ -n "${origin_url:-}" ]]; then
    vfilters+=("--filter" "label=dev-container.origin=$origin_url")
  fi
  {
    docker volume ls "${vfilters[@]}" --format '{{.Name}}' 2>/dev/null \
      | sed -n 's/-workspace$//p'
    if [[ -n "${slots_dir:-}" && -d "$slots_dir" ]]; then
      local d sid
      for d in "$slots_dir"/*/; do
        [[ -d "$d" ]] || continue
        sid=$(slot_id_of "${d%/}")
        if slot_is_ours "$sid"; then echo "$sid"; fi
      done
    fi
  } | sort -u
}

slot_dir_is_leased() {
  local dir="$1" slot_dir="$1"
  case "$dir" in
    */docs/repo) slot_dir="${dir%/docs/repo}" ;;
  esac
  slot_is_ours "$slot_dir" 2>/dev/null || return 1
  local sid; sid=$(slot_id_of "$slot_dir")
  [[ -f "$(lease_file_of "$sid")" ]]
}

slot_looks_complete() {
  local dir="$1"
  local sid="${2:-$(slot_id_of "$dir")}"
  [[ -s "$(slot_ports_file "$sid")" ]] || return 1
  [[ -f "$(slot_env_file_of "$sid")" || ( -n "$dir" && -f "$dir/.devcontainer/.env" ) ]] || return 1
  if [[ -n "$dir" && -d "$dir" ]]; then
    [[ -e "$dir/docs/repo/.git" ]] || return 1
  fi
  return 0
}

slot_unbuilt_reason() {
  local dir="$1"
  local sid="${2:-$(slot_id_of "$dir")}"
  slot_looks_complete "$dir" "$sid" && return 1
  local missing=""
  if [[ -n "$dir" && -d "$dir" && ! -e "$dir/docs/repo/.git" ]]; then
    missing="${missing:+$missing, }docs/repo (the docs clone is missing)"
  fi
  [[ -s "$(slot_ports_file "$sid")" ]] || missing="${missing:+$missing, }recorded ports"
  [[ -f "$(slot_env_file_of "$sid")" || ( -n "$dir" && -f "$dir/.devcontainer/.env" ) ]] \
    || missing="${missing:+$missing, }its env file ($(slot_env_file_of "$sid"))"
  echo "its creation never finished — missing $missing. Remove it with 'down $(slot_name_of "$sid") --force' and acquire again."
}

slot_is_clean_for_reuse() {
  local dir="$1"
  [[ -d "$dir/.git" ]] || return 1
  [[ -z "$(git -C "$dir" status --porcelain 2>/dev/null)" ]] || return 1
  [[ -z "$(git -C "$dir" stash list 2>/dev/null)" ]] || return 1
  docs_repo_is_clean "$dir" || return 1
  docs_repo_is_clean "$dir" docs/fix docs/fix docsfix || return 1
  head_is_delivered "$dir"
}

slot_is_clean_for_teardown() {
  local dir="$1"
  slot_is_clean_for_reuse "$dir" || return 1
  slot_worktrees_of "$dir"
  (( ${#WORKTREES[@]} == 0 )) || return 1
  collect_branch_statuses "$dir"
  (( HELD_COUNT == 0 && UNKNOWN_COUNT == 0 ))
}

slot_dirty_column() {
  local dir="$1" out="" n
  n=$(git -C "$dir" status --porcelain 2>/dev/null | grep -c . || true)
  (( n > 0 )) && out="uncommitted:$n"
  n=$(git -C "$dir" stash list 2>/dev/null | grep -c . || true)
  (( n > 0 )) && out="${out:+$out,}stash:$n"

  docs_repo_is_clean "$dir" || out="${out:+$out,}$DOCS_DIRTY_COLUMN"
  local docs_repo_prunable=("${DOCS_PRUNABLE_BRANCHES[@]}")
  docs_repo_is_clean "$dir" docs/fix docs/fix docsfix || out="${out:+$out,}$DOCS_DIRTY_COLUMN"
  local docs_fix_prunable=("${DOCS_PRUNABLE_BRANCHES[@]}")
  DOCS_PRUNABLE_BRANCHES=("${docs_repo_prunable[@]}" "${docs_fix_prunable[@]}")

  local head_branch head_held=0
  head_branch=$(head_branch_of "$dir")
  if head_is_delivered "$dir"; then
    head_held=0
  else
    head_held=1
  fi
  (( head_held )) && out="${out:+$out,}head:held"

  collect_branch_statuses "$dir"
  local held=$((HELD_COUNT + UNKNOWN_COUNT))
  [[ -n "$head_branch" ]] && (( head_held )) && held=$((held - 1))
  (( held > 0 )) && out="${out:+$out,}held:$held branches"

  slot_worktrees_of "$dir"
  (( ${#WORKTREES[@]} > 0 )) && out="${out:+$out,}worktrees:${#WORKTREES[@]}"

  [[ -n "$out" ]] || out="clean"
  local prunable=$(( ${#PRUNABLE_BRANCHES[@]} + ${#DOCS_PRUNABLE_BRANCHES[@]} ))
  if (( prunable > 0 )) && ! slot_dir_is_leased "$dir" \
     && [[ ! -f "$(parked_file_of "$(slot_id_of "$dir")")" ]]; then
    out+=",prunable:$prunable"
  fi
  echo "$out"
}

slot_dirty_report() {
  local dir="$1" out="" v
  v=$(git -C "$dir" status --porcelain 2>/dev/null)
  [[ -z "$v" ]] || out+="  uncommitted:"$'\n'"$(printf '%s\n' "$v" | sed 's/^/    /')"$'\n'
  v=$(git -C "$dir" stash list 2>/dev/null)
  [[ -z "$v" ]] || out+="  stashed:"$'\n'"$(printf '%s\n' "$v" | sed 's/^/    /')"$'\n'
  docs_repo_is_clean "$dir" || out+="  $DOCS_DIRTY_REASON"$'\n'
  docs_repo_is_clean "$dir" docs/fix docs/fix docsfix || out+="  $DOCS_DIRTY_REASON"$'\n'
  if [[ -z "$(head_branch_of "$dir")" ]] && ! head_is_delivered "$dir"; then
    out+="  detached HEAD at $(git -C "$dir" rev-parse --short HEAD 2>/dev/null), not reachable from any remote ref"$'\n'
  fi
  collect_branch_statuses "$dir"
  if (( ${#BRANCH_REPORTS[@]} > 0 )); then
    out+="  held/unknown branches:"$'\n'
    local r
    for r in "${BRANCH_REPORTS[@]}"; do out+="    $r"$'\n'; done
  fi
  slot_worktrees_of "$dir"
  if (( ${#WORKTREES[@]} > 0 )); then
    out+="  worktrees — a session may be working in one right now:"$'\n'
    local w
    for w in "${WORKTREES[@]}"; do out+="    $w"$'\n'; done
  fi
  printf '%s' "$out"
}

reset_slot() {
  local dir="$1" branch
  branch=$(default_branch)
  echo "▸ resetting $dir to origin/$branch"
  apply_checkout_check_stat "$dir"
  forge_git -C "$dir" fetch --quiet origin || { echo "  ! could not fetch origin - resetting to the last fetched origin/$branch" >&2; forge_hint_for "$dir"; }
  git -C "$dir" checkout --quiet "$branch" --
  git -C "$dir" reset --quiet --hard "origin/$branch"
  if ! git -C "$dir" clean -fd --quiet; then
    echo "  ! git clean -fd left path(s) behind in $dir — see 'git -C $dir clean -n -d'" >&2
  fi
  sync_docs_repo_to_pin "$dir" \
    || echo "  ! could not fetch the docs pin into docs/repo — $DOCS_PIN_REASON, and anything that reads docs/repo will say so" >&2
  guard_docs_repo_readonly "$dir"
  apply_checkout_git_identity "$dir"
  local sha url
  sha=$(git -C "$dir" rev-parse --short HEAD)
  url=$(git -C "$dir" remote get-url origin)
  echo "▸ reset to $sha from $url (origin/$branch)"
}

slot_dir_established() {
  local p="$1" name parent root
  SLOT_REAL_DIR=""
  if [[ -z "$p" ]]; then
    echo "refusing to remove an empty path." >&2
    return 1
  fi
  if [[ "$p" != /* ]]; then
    echo "refusing to remove '$p': not an absolute path." >&2
    return 1
  fi
  case "/$p/" in
    */../*|*/./*)
      echo "refusing to remove '$p': it has a '.' or '..' component." >&2
      return 1 ;;
  esac
  while [[ "$p" == */ && "$p" != / ]]; do p=${p%/}; done
  if [[ "$p" == / ]]; then
    echo "refusing to remove '/'." >&2
    return 1
  fi
  if [[ -L "$p" ]]; then
    echo "refusing to remove '$p': it is a symlink." >&2
    return 1
  fi
  if [[ ! -d "$p" ]]; then
    echo "refusing to remove '$p': not a directory." >&2
    return 1
  fi
  name=$(basename "$p")
  if ! parent=$(cd -P "$(dirname "$p")" 2>/dev/null && pwd -P); then
    echo "refusing to remove '$p': its parent cannot be resolved." >&2
    return 1
  fi
  local root=""
  [[ -d "$slots_dir" ]] && root=$(cd -P "$slots_dir" 2>/dev/null && pwd -P)
  local stub_root=""
  [[ -n "${DOCKER_STUB_STATE:-}" && -d "$DOCKER_STUB_STATE/volumes" ]] && stub_root=$(cd -P "$DOCKER_STUB_STATE/volumes" 2>/dev/null && pwd -P)
  if [[ ( -z "$root" || "$parent" != "$root" ) && ( -z "$stub_root" || "$parent" != "$stub_root" ) ]]; then
    echo "refusing to remove '$p': it is not a direct child of the slots root." >&2
    return 1
  fi
  p="$parent/$name"
  if [[ -f "$(holder_file_of "$p")" && ! -e "$p/.git" && ! -L "$p/.git" ]]; then
    :
  elif ! slot_is_ours "$p"; then
    echo "refusing to remove '$p': it is neither marked as a slot nor a checkout of this project." >&2
    return 1
  fi
  SLOT_REAL_DIR="$p"
}

slot_index_of() {
  local base; base=$(slot_name_of "$1")
  [[ "$base" =~ ^slot-([0-9]+)$ ]] || return 1
  echo "${BASH_REMATCH[1]}"
}

# A number belongs to a live lease of any project, never to a folder: every
# project's slots draw from the same numbers, and a number's block of ports is
# the same for all of them, so two live slots never share a port.
pool_number_taken() {
  local f
  for f in "$STATE"/leases/*-slot-"$1".json; do
    [[ -f "$f" ]] || continue
    lease_is_live "$f" && return 0
  done
  return 1
}

# A slot idle past the limit has no live lease but its containers may still be
# up, publishing the ports of its number.
pool_block_in_use() {
  local number="$1" index=0 name
  for name in $SLOT_PORT_NAMES; do
    port_in_use $(( SLOT_PORT_BASE + number * SLOT_PORT_BLOCK + index )) && return 0
    index=$((index + 1))
  done
  return 1
}

default_slot_name() {
  local limit=$SLOT_INDEX_CEILING i sid
  for (( i = 1; i <= limit; i++ )); do
    ! slot_block_has_forbidden_port "$i" || continue
    [[ -z "${slots_dir:-}" || ! -e "$slots_dir/slot-$i" ]] || continue
    sid=$(slot_id_of "slot-$i")
    ! docker volume inspect "$(slot_volume_of "$sid")" >/dev/null 2>&1 || continue
    ! pool_number_taken "$i" || continue
    ! pool_block_in_use "$i" || continue
    echo "slot-$i"; return
  done
  die "no free default name — slot-1..slot-$limit are all taken."
}

create_slot() {
  local name="$1"
  local proj="${SLOT_PROJECT_PREFIX}-${name}"
  local vol; vol=$(slot_volume_of "$proj")

  if docker volume inspect "$vol" >/dev/null 2>&1; then
    die "slot volume already exists: $vol"
  fi
  if [[ -n "${slots_dir:-}" && -e "$slots_dir/$name" ]]; then
    die "slot dir already exists: $slots_dir/$name"
  fi

  echo "▸ creating workspace volume $vol"
  docker volume create \
    --label "dev-container.sid=$proj" \
    --label "dev-container.project=$SLOT_PROJECT_PREFIX" \
    --label "dev-container.origin=$origin_url" \
    --label "dev-container.repo-root=$repo_root" \
    "$vol" >/dev/null || die "creating the volume $vol failed — the reason is above this line."
  spec_reset "$proj"

  rm -f "$(slot_env_file_of "$proj")" "$(slot_overlay_of "$proj")"
  ensure_slot_env "$proj"

  local svc
  svc=$(container_service_of "" "$proj") || exit 1

  echo "▸ cloning the main checkout into $vol"
  slot_compose "$proj" run --rm --no-deps -T -u root \
      -v "$repo_root:/src:ro" --entrypoint sh "$svc" -c \
      'set -e; ws=$1; origin=$2; user=$3; git config --global --add safe.directory "*"; git clone --quiet --no-hardlinks --no-checkout /src "$ws/.dev-container-clone"; mv "$ws/.dev-container-clone/.git" "$ws/.git"; rmdir "$ws/.dev-container-clone"; git -C "$ws" remote set-url origin "$origin"; git -C "$ws" reset --quiet --hard; git -C "$ws" config --local core.checkStat minimal; [ ! -d /src/.devcontainer ] || [ -d "$ws/.devcontainer" ] || { set -f; tar -C /src -cf - $4 .devcontainer | tar -C "$ws" -xpf -; set +f; }; [ ! -f /src/project.env ] || [ -f "$ws/project.env" ] || cp /src/project.env "$ws/project.env"; [ ! -f /src/bin/dev-container ] || [ -f "$ws/bin/dev-container" ] || { mkdir -p "$ws/bin"; cp /src/bin/dev-container "$ws/bin/dev-container"; }; exclude=$(git -C "$ws" rev-parse --path-format=absolute --git-path info/exclude 2>/dev/null || git -C "$ws" rev-parse --git-path info/exclude 2>/dev/null || echo "$ws/.git/info/exclude"); case "$exclude" in /*) ;; *) exclude="$ws/$exclude" ;; esac; mkdir -p "$(dirname "$exclude")"; for p in "/.devcontainer/" "/project.env" "/bin/dev-container"; do grep -Fxq "$p" "$exclude" 2>/dev/null || echo "$p" >>"$exclude"; done; mounts=$(awk -v ws="$ws/" "index(\$5, ws) == 1 { print \$5 }" /proc/self/mountinfo); set --; for m in $mounts; do set -- "$@" -path "$m" -prune -o; done; find "$ws" "$@" -exec chown -h "$user:$user" {} +' sh "/workspace" "$origin_url" "${CONTAINER_USER:-node}" "$(devcontainer_exclude_words "$repo_root")" \
    || die "cloning into '$proj' failed — the reason is above this line."

  apply_checkout_git_identity "$proj"

  local vol_dir=""
  if [[ -n "${DOCKER_STUB_STATE:-}" && -d "$DOCKER_STUB_STATE/volumes/$vol" ]]; then
    vol_dir="$DOCKER_STUB_STATE/volumes/$vol"
  fi
  local target_dir="${dir:-$vol_dir}"
  if [[ -n "$target_dir" && -d "$target_dir" ]]; then
    if [[ -d "$repo_root/.devcontainer" && ! -d "$target_dir/.devcontainer" ]]; then
      copy_devcontainer_dir "$repo_root" "$target_dir"
    fi
    if [[ -f "$repo_root/project.env" && ! -f "$target_dir/project.env" ]]; then
      cp "$repo_root/project.env" "$target_dir/project.env"
    fi
    if [[ -f "$repo_root/bin/dev-container" && ! -f "$target_dir/bin/dev-container" ]]; then
      mkdir -p "$target_dir/bin"
      cp "$repo_root/bin/dev-container" "$target_dir/bin/dev-container"
    fi
    adopt_exclude_project_files "$target_dir"

    local copy_file
    local -a copy_files
    read -ra copy_files <<<"$SLOT_COPY_FILES"
    for copy_file in ${copy_files[@]+"${copy_files[@]}"}; do
      [[ -f "$repo_root/$copy_file" ]] || continue
      echo "▸ copying $copy_file"
      mkdir -p "$(dirname "$target_dir/$copy_file")"
      cp "$repo_root/$copy_file" "$target_dir/$copy_file"
    done

    create_mount_dirs "$target_dir"

    mkdir -p "$target_dir/.local"
    local handoff
    for handoff in "$repo_root"/.local/handoff-*; do
      [[ -d "$handoff" ]] || continue
      echo "▸ linking .local/$(basename "$handoff")"
      ln -s "$handoff" "$target_dir/.local/$(basename "$handoff")"
    done

    echo "▸ cloning the docs repository into docs/repo"
    forge_git clone --depth 1 --quiet -- "$DOCS_REPO_URL" "$target_dir/docs/repo"
    exclude_docs_clones "$target_dir"
    forge_git -C "$target_dir" fetch --quiet origin "refs/heads/$(default_branch "$target_dir")" 2>/dev/null \
      || echo "  ! could not fetch origin — docs/repo follows the pin on $repo_root's own $(default_branch "$target_dir"), which may be behind" >&2
    sync_docs_repo_to_pin "$target_dir" \
      || echo "  ! could not fetch the docs pin into docs/repo — $DOCS_PIN_REASON, and anything that reads docs/repo will say so" >&2
    guard_docs_repo_readonly "$target_dir"
    apply_checkout_check_stat "$target_dir"
  else
    local copy_file
    local -a copy_files
    read -ra copy_files <<<"$SLOT_COPY_FILES"
    for copy_file in ${copy_files[@]+"${copy_files[@]}"}; do
      [[ -f "$repo_root/$copy_file" ]] || continue
      echo "▸ copying $copy_file"
      slot_helper_run "$proj" sh -c 'mkdir -p "$(dirname "$1")" && cat >"$1"' sh "$copy_file" <"$repo_root/$copy_file"
    done
  fi

  lock_acquire
  resolve_slot_ports "$proj" check-busy || die "cannot create '$name': the reason is above this line."
  lock_release

  ensure_shared_volumes

  sync_slot_env_identity "$proj" "$proj"

  run_slot_install "$name" "" "$proj"
  build_slot_image "" "$proj" "${2:-0}"
  echo "▸ starting compose project '$proj' on $(ports_summary "$proj")"
  local overlay
  while IFS= read -r overlay; do
    echo "  with the overlay $overlay"
  done < <(slot_overlay_files "$proj")
  slot_compose "$proj" up -d

  SLOT_DIR=""
  SLOT_ID="$proj"
}

finish_acquire() {
  local dir="$1" slot_id="$2" holder="$3" owner="$4"
  [[ -n "$slot_id" ]] || slot_id=$(slot_id_of "$dir")
  local name; name=$(slot_name_of "$slot_id")
  local target="${dir:-$slot_id}"
  local ssh_port
  ssh_port=$(slot_port "$target" "$PRIMARY_PORT")
  [[ -n "$ssh_port" && "$ssh_port" != 0 ]] || die "'$slot_id' has no $PRIMARY_PORT port recorded — its compose file may not expose one, or every path that should have repaired this already ran and still failed."

  local epoch; epoch=$(now_epoch)
  local key="$STATE/keys/${slot_id}-${epoch}"
  rm -f "$STATE/keys/${slot_id}-"* 2>/dev/null || true
  ssh-keygen -q -N '' -t ed25519 -C "$holder" -f "$key"
  chmod 600 "$key"

  install_lease_key "$dir" "$slot_id" "$key.pub"
  sync_slot_ssh_key "$target" "$(cat "$key.pub")"

  write_lease "$(lease_file_of "$slot_id")" "$holder" "$slot_id" "$epoch" "$key" "$ssh_port" "$owner" "$repo_root"
  RESERVED_LEASE=""
  write_holder_file "$dir" "$holder" "$owner" "$slot_id" "$epoch"

  warn_unproven_branches "$target" "⚠ '$slot_id' already holds branch(es) not proved to be in origin/$DEFAULT_BRANCH, from before this lease — not yours; check before assuming they're redundant:"

  echo
  echo "slot '$slot_id' leased to $holder: ${dir:-$slot_id}"
  echo "  it has no term: it stays live while it is in use — a '$CALLER run $name -- <cmd>', or container CPU, within $(fmt_duration "$IDLE_LIMIT")."
  echo "  the shared key no longer opens this slot — only $key does."
  echo "  ssh -i $key -p $ssh_port node@localhost"
  echo "           first connect asks to accept the host key — do it from here."
  echo "           if ssh refuses with a changed host key, clear the old one on your machine first:"
  echo "    ssh-keygen -R '[localhost]:$ssh_port'"
  echo
  echo "  paste into an agent tool's ssh target config:"
  cat <<JSON
  {
    "id": "$slot_id",
    "name": "$SLOT_PROJECT_PREFIX slot: $name",
    "sshHost": "node@localhost",
    "sshPort": $ssh_port,
    "sshUser": "node",
    "sshKey": "$key"
  }
JSON

  provision_slot "$dir" "$slot_id" \
    || die "'$slot_id' is leased to $holder but was not provisioned — the reason and the fix are above."
}

try_acquire_once() {
  sweep_dead_reservations

  ACQUIRE_BLOCKED=""
  if memory_floor_blocks; then
    ACQUIRE_BLOCKED=memory
    return 1
  fi
  ACQUIRE_BLOCKED=cap
  machine_cap_load
  (( $(machine_in_use) < MACHINE_CAP )) || return 1

  local name; name=$(default_slot_name)
  local idx; idx=$(slot_index_of "$name")
  if (( idx <= SLOT_INDEX_CEILING )); then
    echo "▸ creating '$name' ($(machine_in_use)/$MACHINE_CAP leased on this machine)"
    ACQUIRE_SETUP=create ACQUIRED_DIR="" ACQUIRED_ID="${SLOT_PROJECT_PREFIX}-${name}" ACQUIRED_NAME="$name"
    return 0
  fi

  return 1
}

resync_node_modules() {
  local name="$1" dir="$2" sid="$3"
  [[ -n "$PNPM_STORE_VOLUME" ]] || return 0
  compose_running "$dir" "$sid" || {
    echo "  ! '$name' has no running container — skipping the node_modules resync" >&2
    return 0
  }

  local check
  check=$(cmd_run "$name" -- "$COMPOSE_DIR/node-modules-drift-check.sh" 2>/dev/null) || check=""
  if [[ -z "$check" ]]; then
    echo "  ! '$name': could not check node_modules against pnpm-lock.yaml — leaving it as is" >&2
    return 0
  fi

  if [[ "$check" == *drift* ]]; then
    echo "▸ '$name': node_modules is stale against pnpm-lock.yaml — running pnpm install"
    if ( install_slot "$dir" "$sid" ); then
      echo "  node_modules resynced"
    else
      echo "  ! pnpm install failed for '$name' — node_modules is still stale; the lease stays valid, fix it yourself with '$CALLER install $name'" >&2
    fi
  else
    echo "▸ '$name': node_modules already matches pnpm-lock.yaml"
  fi
}

ACQUIRE_REASON=""
ACQUIRE_BLOCKED=""
slot_acquire_verdict() {
  local dir="$1" sid="$2" fast="${3:-0}"
  local check_dir="${dir:-${DOCKER_STUB_STATE:+$DOCKER_STUB_STATE/volumes/${sid}-workspace}}"
  ACQUIRE_REASON=""

  if slot_unbuilt_reason "$dir" >/dev/null; then
    ACQUIRE_REASON="unbuilt"
    return 1
  fi

  local lease; lease=$(lease_file_of "$sid")
  if [[ -f "$lease" ]]; then
    local holder
    holder=$(lease_field "$lease" holder)
    if ! lease_is_live "$lease"; then
      ACQUIRE_REASON="idle-unreleased:$holder"
    else
      ACQUIRE_REASON="leased:$holder"
    fi
    return 1
  fi

  if [[ -f "$(parked_file_of "$sid")" ]]; then
    ACQUIRE_REASON="parked"
    return 1
  fi

  if (( fast )); then
    if [[ -n "$check_dir" && -d "$check_dir" ]] \
       && { [[ -n "$(git -C "$check_dir" status --porcelain 2>/dev/null)" ]] \
            || [[ -n "$(git -C "$check_dir" stash list 2>/dev/null)" ]]; }; then
      ACQUIRE_REASON="dirty:uncommitted-or-stashed (fast: not delivery-checked)"
      return 1
    fi
    local docs="$check_dir/docs/repo"
    if [[ -f "$docs/.git/HEAD" ]] \
       && { [[ -n "$(git -C "$docs" status --porcelain 2>/dev/null)" ]] \
            || [[ -n "$(git -C "$docs" stash list 2>/dev/null)" ]]; }; then
      ACQUIRE_REASON="dirty:docs uncommitted-or-stashed (fast: not delivery-checked)"
      return 1
    fi
    local docs_fix="$check_dir/docs/fix"
    if [[ -f "$docs_fix/.git/HEAD" ]] \
       && { [[ -n "$(git -C "$docs_fix" status --porcelain 2>/dev/null)" ]] \
            || [[ -n "$(git -C "$docs_fix" stash list 2>/dev/null)" ]]; }; then
      ACQUIRE_REASON="dirty:docs/fix uncommitted-or-stashed (fast: not delivery-checked)"
      return 1
    fi
  else
    if [[ -n "$check_dir" && -d "$check_dir" ]] && ! slot_is_clean_for_reuse "$check_dir"; then
      ACQUIRE_REASON="dirty:$(slot_dirty_column "$check_dir")"
      return 1
    fi
  fi

  slot_worktrees_of "$check_dir"
  local unreadable=0 wt mapped_wt
  for wt in "${WORKTREES[@]}"; do
    mapped_wt=$(slot_map_workspace_path "$check_dir" "$wt")
    [[ -d "$mapped_wt" ]] || unreadable=$((unreadable + 1))
  done
  if (( unreadable > 0 )); then
    ACQUIRE_REASON="unreadable:$unreadable"
    return 1
  fi

  ACQUIRE_REASON="acquirable"
  return 0
}
