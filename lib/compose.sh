# shellcheck shell=bash disable=SC2034,SC2154

main_compose_dir="$repo_root/$COMPOSE_DIR"
compose_file_of() {
  local files
  read -ra files <<<"$COMPOSE_FILES"
  echo "$main_compose_dir/${files[0]}"
}

slot_volume_of() { echo "${1}-workspace"; }
slot_volume_overlay_path() { echo "$STATE/compose/slot-volume.yml"; }

write_slot_volume_overlay() {
  local p; p=$(slot_volume_overlay_path)
  [[ -f "$p" ]] && return 0
  mkdir -p "$(dirname "$p")"
  cat <<'EOF' >"$p"
volumes:
  slot-workspace:
    external: true
    name: ${SLOT_WORKSPACE_VOLUME}
EOF
}

spec_of() {
  local sid="$1" scratch="${SCRATCH:-${DEV_CONTAINER_HOME:-$HOME/.dev-containers}/scratch}"
  local f="$scratch/spec/$sid" spec="" vol name
  [[ -f "$f" ]] && { cat "$f"; return 0; }
  vol=$(slot_volume_of "$sid")
  if docker volume inspect "$vol" >/dev/null 2>&1; then
    spec="vol:$vol"
  else
    name=$(slot_name_of "$sid")
    if [[ -n "${slots_dir:-}" && -d "$slots_dir/$name" ]]; then
      spec="dir:$slots_dir/$name"
    fi
  fi
  mkdir -p "$scratch/spec"
  printf '%s' "$spec" >"$f"
  echo "$spec"
}

spec_reset() {
  local scratch="${SCRATCH:-${DEV_CONTAINER_HOME:-$HOME/.dev-containers}/scratch}"
  rm -f "$scratch/spec/$1"
}

slot_exists() {
  local sid; sid=$(slot_id_of "$1")
  [[ -n "$(spec_of "$sid")" ]]
}

slot_env_file_of() { echo "$STATE/env/$1.env"; }

env_value() {
  grep "^${2}=" "$1" 2>/dev/null | tail -1 | cut -d= -f2- || true
}

slot_overlay_files() {
  local env_file entry var
  local -a entries
  env_file=$(slot_env_file_of "$1")
  [[ -f "$env_file" ]] || return 0
  read -ra entries <<<"$SLOT_OVERLAYS"
  for entry in ${entries[@]+"${entries[@]}"}; do
    var=${entry%%=*}
    if grep -q "^$var=[^[:space:]]" "$env_file"; then
      printf '%s\n' "${entry#*=}"
    fi
  done
}

compose_args_of() {
  local proj="$1" env_file f
  local -a files
  env_file=$(slot_env_file_of "$proj")
  [[ -f "$env_file" ]] || env_file=/dev/null
  COMPOSE_ARGS=(--project-directory "$main_compose_dir" --env-file "$env_file")
  read -ra files <<<"$COMPOSE_FILES"
  for f in "${files[@]}"; do
    COMPOSE_ARGS+=(-f "$main_compose_dir/$f")
  done
  while IFS= read -r f; do
    COMPOSE_ARGS+=(-f "$main_compose_dir/$f")
  done < <(slot_overlay_files "$proj")
  if [[ "${COMPOSE_WITH_SECRETS:-0}" == 1 ]]; then
    COMPOSE_ARGS+=(-f "$(secrets_overlay_of)")
  fi
  local spec; spec=$(spec_of "$proj")
  if [[ "$spec" != dir:* ]]; then
    write_slot_volume_overlay
    COMPOSE_ARGS+=(-f "$(slot_volume_overlay_path)")
  fi
  if [[ "${COMPOSE_BASE_ONLY:-0}" != 1 ]]; then
    write_slot_overlay "$proj"
    COMPOSE_ARGS+=(-f "$SLOT_OVERLAY_PATH")
  fi
}

secrets_overlay_of() { echo "$main_compose_dir/$SECRETS_OVERLAY"; }

slot_compose() {
  local dir="" proj=""
  if [[ $# -ge 2 && ( "$1" == /* || -z "$1" ) && ( -z "${2:-}" || ( "${2:-}" != -* && "${2:-}" != up && "${2:-}" != down && "${2:-}" != run && "${2:-}" != exec && "${2:-}" != config && "${2:-}" != ps && "${2:-}" != build && "${2:-}" != stop && "${2:-}" != rm && "${2:-}" != ls ) ) ]]; then
    dir="$1"; proj="$2"; shift 2
  elif [[ $# -ge 2 && "$1" == "$2" ]]; then
    dir=""; proj="$2"; shift 2
  else
    proj="$1"; shift
  fi
  local -a scope=()
  local env_file="" name value wsvol="" forge_token=""
  local -a port_env=()
  local wsdir="/nonexistent/dev-container-has-no-workspace"
  if [[ -n "$proj" ]]; then
    env_file=$(slot_env_file_of "$proj")
    if [[ -f "$env_file" ]]; then
      for name in $SLOT_PORT_NAMES; do
        value=$(env_value "$env_file" "$name")
        [[ -z "$value" ]] || port_env+=("$name=$value")
      done
      forge_token=$(env_value "$env_file" GH_TOKEN)
    else
      env_file=/dev/null
    fi
    local spec; spec=$(spec_of "$proj")
    case "$spec" in
      vol:*) wsdir="slot-workspace"; wsvol="${spec#vol:}" ;;
      dir:*) wsdir="${spec#dir:}" ;;
      *)
        if [[ -n "$dir" && -d "$dir" ]]; then
          wsdir="$dir"
        else
          wsdir="slot-workspace"
          wsvol=$(slot_volume_of "$proj")
        fi
        ;;
    esac
    local -a COMPOSE_ARGS
    compose_args_of "$proj"
    scope=(-p "$proj" "${COMPOSE_ARGS[@]}")
  fi
  (
    if [[ -n "$proj" ]]; then
      export WORKSPACE_DIR="$wsdir" SLOT_WORKSPACE_VOLUME="$wsvol" SLOT_ENV_FILE="$env_file" COMPOSE_PROJECT_NAME="$proj"
      export SLOT_GH_TOKEN="$forge_token"
      (( ${#port_env[@]} == 0 )) || export "${port_env[@]}"
    fi
    docker compose ${scope[@]+"${scope[@]}"} "$@"
  )
}

slot_runs_with_secrets() {
  local files
  files=$(docker ps --filter "label=com.docker.compose.project=$1" \
    --format '{{.Label "com.docker.compose.project.config_files"}}' 2>/dev/null) || return 1
  [[ "$files" == *"$SECRETS_OVERLAY"* ]]
}

docker_project_of_dir() {
  local dir="$1" proj
  proj=$(docker ps -a --filter "label=dev-container.workspace=$dir" \
    --format '{{.Label "com.docker.compose.project"}}' 2>/dev/null | head -1)
  [[ -n "$proj" ]] || proj=$(docker ps -a --filter "label=com.docker.compose.project.working_dir=$dir/$COMPOSE_DIR" \
    --format '{{.Label "com.docker.compose.project"}}' 2>/dev/null | head -1)
  echo "$proj"
}

workspace_of_project() {
  local proj="$1" dir wd
  dir=$(docker ps -a --filter "label=com.docker.compose.project=$proj" \
    --format '{{.Label "dev-container.workspace"}}' 2>/dev/null | grep . | head -1 || true)
  case "$dir" in
    /*) ;;
    *) dir="" ;;
  esac
  if [[ -z "$dir" ]]; then
    wd=$(docker ps -a --filter "label=com.docker.compose.project=$proj" \
      --format '{{.Label "com.docker.compose.project.working_dir"}}' 2>/dev/null | head -1)
    [[ -n "$wd" ]] || return 0
    dir=$(dirname "$wd")
  fi
  dir=$(realpath -m -- "$dir" 2>/dev/null) || return 0
  local parent; parent=$(realpath -m -- "$(dirname "$dir")" 2>/dev/null) || return 0
  local slots_root; slots_root=$(realpath -m -- "$slots_dir" 2>/dev/null) || slots_root=""
  local stub_vols="${DOCKER_STUB_STATE:+$DOCKER_STUB_STATE/volumes}"
  if [[ "$parent" != "$slots_root" && ( -z "$stub_vols" || "$parent" != "$(realpath -m -- "$stub_vols" 2>/dev/null)" ) ]]; then
    return 0
  fi
  echo "$dir"
}

# The services of a `config --format json` on stdin that mount the checkout at
# /workspace, sorted and space-joined.
workspace_services_of() {
  jq -r '[.services // {} | to_entries[] | select(any(.value.volumes[]?; .target == "/workspace")) | .key] | sort | join(" ")'
}

container_service_of() {
  local dir="$1" proj="$2" svcs
  svcs=$(slot_compose "$dir" "$proj" config --format json 2>/dev/null | workspace_services_of) || svcs=""
  case "$svcs" in
    "") echo "'$proj' — no service in its compose config mounts the checkout at /workspace." >&2; return 1 ;;
    *" "*) echo "'$proj' — more than one service in its compose config mounts the checkout at /workspace: ${svcs// /, }." >&2; return 1 ;;
  esac
  echo "$svcs"
}

container_ps() {
  local dir="$1" proj="$2" out rc svc
  compose_running "$dir" "$proj" || return 0
  svc=$(container_service_of "$dir" "$proj" 2>/dev/null) || return 1
  out=$(slot_compose "$dir" "$proj" exec -T -u node "$svc" ps -eo comm 2>/dev/null)
  rc=$?
  printf '%s\n' "$out" | tail -n +2
  [[ $rc -eq 0 && -n "$out" ]] || return 1
  return 0
}

is_base_proc() {
  case " $BASE_PROCS " in
    *" $1 "*) return 0 ;;
    *) return 1 ;;
  esac
}

ssh_session_count() {
  local n
  n=$(container_ps "$1" "$2" | grep -c '^sshd$' || true)
  n=${n:-0}
  (( n > 0 )) && n=$((n - 1)) || n=0
  echo "$n"
}

non_base_procs() {
  local raw p out="" ps_ok=0
  raw=$(container_ps "$1" "$2") || ps_ok=1
  if (( ps_ok )); then
    echo "unknown"
    return
  fi
  while IFS= read -r p; do
    [[ -n "$p" ]] || continue
    is_base_proc "$p" && continue
    [[ "$p" == sshd ]] && continue
    case ",$out," in *",$p,"*) continue ;; esac
    out="${out:+$out,}$p"
  done <<< "$raw"
  echo "$out"
}

wait_for_sshd() {
  local dir="$1" proj="$2" waited=0 svc
  svc=$(container_service_of "$dir" "$proj") || die "'$proj' — sshd cannot be checked without the one service that mounts /workspace."
  until slot_compose "$dir" "$proj" exec -T "$svc" pgrep sshd >/dev/null 2>&1; do
    waited=$((waited + 1))
    (( waited > 30 )) && die "'$proj' — sshd never came up within 30s of the container starting; check 'docker compose -p $proj logs app'."
    sleep 1
  done
}

install_lease_key() {
  local dir="$1" proj="$2" pubkey_file="$3" svc
  wait_for_sshd "$dir" "$proj"
  svc=$(container_service_of "$dir" "$proj") || die "'$proj' — the lease key cannot be installed without the one service that mounts /workspace."
  slot_compose "$dir" "$proj" exec -T "$svc" \
    sh -c 'cat > /etc/ssh/authorized_keys.d/node && chmod 0644 /etc/ssh/authorized_keys.d/node' <"$pubkey_file"
}

clear_lease_key() {
  local dir="$1" proj="$2" svc
  compose_running "$dir" "$proj" || return 0
  svc=$(container_service_of "$dir" "$proj") || die "'$proj' — the lease key cannot be removed without the one service that mounts /workspace."
  slot_compose "$dir" "$proj" exec -T "$svc" \
    sh -c 'cat > /etc/ssh/authorized_keys.d/node && chmod 0644 /etc/ssh/authorized_keys.d/node' </dev/null || true
}

sync_slot_ssh_key() {
  local target="$1" value="$2"
  local sid; sid=$(slot_id_of "$target")
  local env_file; env_file=$(slot_env_file_of "$sid")
  [[ -f "$env_file" ]] || return 0
  upsert_env_var "$env_file" SSH_PUBLIC_KEY "$value"
}

slot_name_of() {
  local sid="$1"
  if [[ "$sid" == "${SLOT_PROJECT_PREFIX}-"* ]]; then
    echo "${sid#"${SLOT_PROJECT_PREFIX}-"}"
  else
    echo "$sid"
  fi
}

compose_running() {
  local dir="" proj=""
  if (( $# == 1 )); then
    proj="$1"
    dir="$slots_dir/$(slot_name_of "$proj")"
  else
    dir="$1"
    proj="$2"
  fi
  [[ -f "$(compose_file_of)" ]] || return 1
  [[ -n "$(COMPOSE_BASE_ONLY=1 slot_compose "$dir" "$proj" ps --status running -q 2>/dev/null)" ]]
}

slot_sh() {
  local sid="$1"; shift
  local name dir ws="/workspace"
  name=$(slot_name_of "$sid")
  dir="$slots_dir/$name"
  if compose_running "$sid"; then
    COMPOSE_BASE_ONLY=1 slot_compose "$dir" "$sid" exec -T -u"${CONTAINER_USER:-node}" -w"$ws" app "$@"
  else
    COMPOSE_BASE_ONLY=1 slot_compose "$dir" "$sid" run --rm --no-deps -T -u"${CONTAINER_USER:-node}" -w"$ws" app "$@"
  fi
}

slot_helper_run() {
  local sid="$1"; shift
  local vol; vol=$(slot_volume_of "$sid")
  local image="${DEV_CONTAINER_ENGINE_IMAGE:-dev-container-engine:local}"
  local uid="${CONTAINER_UID:-1000}"
  docker run --rm -i --network none --cap-drop ALL --security-opt no-new-privileges \
    --user "$uid" -v "$vol:/workspace" -w /workspace "$image" "$@"
}

# The same helper container with a network, for the one step that has to reach
# the forge. FORGE_CONTAINER_ENV (the credential's --env=NAME arguments) goes in.
slot_helper_run_online() {
  local sid="$1"; shift
  local vol; vol=$(slot_volume_of "$sid")
  local image="${DEV_CONTAINER_ENGINE_IMAGE:-dev-container-engine:local}"
  local uid="${CONTAINER_UID:-1000}"
  docker run --rm -i --cap-drop ALL --security-opt no-new-privileges \
    --user "$uid" ${FORGE_CONTAINER_ENV[@]+"${FORGE_CONTAINER_ENV[@]}"} \
    -v "$vol:/workspace" -w /workspace "$image" "$@"
}

slot_exec_stream() {
  local sid="$1"; shift
  if compose_running "$sid"; then
    slot_sh "$sid" "$@"
  else
    slot_helper_run "$sid" "$@"
  fi
}

ensure_shared_volumes() {
  docker volume create "$SIGNING_KEY_VOLUME" >/dev/null
  docker volume create "$SHARED_CACHE_VOLUME" >/dev/null
  [[ -z "$PNPM_STORE_VOLUME" ]] || docker volume create "$PNPM_STORE_VOLUME" >/dev/null
}

create_mount_dirs() {
  local dir="$1" mount_dir
  local -a mount_dirs
  read -ra mount_dirs <<<"$SLOT_MOUNT_DIRS"
  for mount_dir in ${mount_dirs[@]+"${mount_dirs[@]}"}; do
    mkdir -p "$repo_root/$mount_dir" "$dir/$mount_dir"
  done
}

install_slot() {
  local dir="$1" proj="$2"
  local installer="$main_compose_dir/install.sh"
  local -a store=()
  [[ -f "$installer" ]] || die "no $installer — this project has no install script."
  if [[ -n "$PNPM_STORE_VOLUME" ]]; then
    store=(PNPM_STORE_VOLUME="$PNPM_STORE_VOLUME")
    echo "▸ installing dependencies into '$proj' from the project store '$PNPM_STORE_VOLUME'"
  else
    echo "▸ installing dependencies into '$proj'"
  fi
  local ws="${dir:-slot-workspace}"
  # The hook runs on the host with no forge credential: scrub the token names
  # the engine and gh read, whatever the caller's shell holds.
  local -a scrub=(-u GH_TOKEN -u GITHUB_TOKEN)
  env "${scrub[@]}" ${store[@]+"${store[@]}"} WORKSPACE_DIR="$ws" COMPOSE_PROJECT_NAME="$proj" \
    bash "$installer" \
    || die "installing dependencies into '$proj' failed — the reason is above this line."
}

run_slot_install() {
  local name="$1" dir="$2" proj="$3"
  if [[ -f "$main_compose_dir/install.sh" ]]; then
    install_slot "$dir" "$proj"
  elif [[ -n "$PNPM_STORE_VOLUME" ]]; then
    die "cannot install '$name': PNPM_STORE_VOLUME is set but $main_compose_dir has no install.sh to fill the store — unset it or add the script."
  else
    echo "▸ no install script in $main_compose_dir — install is skipped"
  fi
}

upsert_env_var() {
  local file="$1" key="$2" value="$3" line tmp found=0
  tmp=$(mktemp)
  while IFS= read -r line || [[ -n "$line" ]]; do
    if [[ "$line" == "$key="* ]]; then
      printf '%s=%s\n' "$key" "$value"
      found=1
    else
      printf '%s\n' "$line"
    fi
  done <"$file" >"$tmp"
  (( found )) || printf '%s=%s\n' "$key" "$value" >>"$tmp"
  mv "$tmp" "$file"
}

# What a new slot's env is seeded with: the keys in SLOT_ENV_KEYS, and the two
# the engine itself needs to give the checkout the agent's git identity.
# Nothing else in the project's .env reaches the slot.
ENGINE_ENV_KEYS="GIT_USER_NAME GIT_USER_EMAIL"

seed_slot_env() {
  local src="$1" dst="$2" key value seen=" "
  : >"$dst"
  if [[ -f "$src" ]]; then
    for key in $ENGINE_ENV_KEYS $SLOT_ENV_KEYS; do
      case "$seen" in *" $key "*) continue ;; esac
      seen+="$key "
      grep -q "^${key}=" "$src" || continue
      value=$(env_value "$src" "$key")
      printf '%s=%s\n' "$key" "$value" >>"$dst"
    done
  fi
  if [[ "$origin_url" == "$repo_root" ]]; then
    if ! grep -q "^GIT_USER_NAME=" "$dst"; then
      local local_name local_email
      local_name=$(git -C "$repo_root" config user.name 2>/dev/null || true)
      [[ -n "$local_name" ]] || local_name="local-user"
      local_email=$(git -C "$repo_root" config user.email 2>/dev/null || true)
      printf 'GIT_USER_NAME=%s\n' "$local_name" >>"$dst"
      [[ -n "$local_email" ]] && printf 'GIT_USER_EMAIL=%s\n' "$local_email" >>"$dst"
    fi
  fi
}

# The identity project.env gives every slot: it beats the compose .env seed and
# the one derived from an App, which is the default and not a choice.
sync_slot_git_identity() {
  local env_file; env_file=$(slot_env_file_of "$1")
  [[ -n "${SLOT_GIT_NAME:-}" && -f "$env_file" ]] || return 0
  upsert_env_var "$env_file" GIT_USER_NAME "$SLOT_GIT_NAME"
  upsert_env_var "$env_file" GIT_USER_EMAIL "$SLOT_GIT_EMAIL"
  chmod 600 "$env_file"
}

ensure_slot_env() {
  local dir="" proj=""
  if [[ $# -eq 1 ]]; then
    proj="$1"
  else
    dir="$1"
    proj="$2"
  fi
  local check_dir="${dir:-${DOCKER_STUB_STATE:+$DOCKER_STUB_STATE/volumes/${proj}-workspace}}"
  local env_file; env_file=$(slot_env_file_of "$proj")
  if [[ ! -f "$env_file" ]]; then
    mkdir -p "$(dirname "$env_file")"
    seed_slot_env "$main_compose_dir/.env" "$env_file"
    upsert_env_var "$env_file" SSH_PUBLIC_KEY ""
  fi
  sync_agent_auth_env "$proj"
  sync_slot_git_identity "$proj"
  if [[ -n "$check_dir" && -f "$check_dir/$COMPOSE_DIR/.env" ]]; then
    rm -f "$check_dir/$COMPOSE_DIR/.env" "$(slot_ports_file "$proj")"
    local sname; sname=$(slot_name_of "${dir:-$check_dir}")
    echo "▸ $sname: removed its in-checkout $COMPOSE_DIR/.env — the slot's env now lives at $env_file; its ports are allocated afresh on this bring-up"
  fi
}

sync_slot_env_identity() {
  local dir="$1" project="$2" name env_set
  local -a env_sets
  local env_file; env_file=$(slot_env_file_of "$project")
  [[ -f "$env_file" ]] || return 0
  upsert_env_var "$env_file" COMPOSE_PROJECT_NAME "$project"
  for name in $SLOT_PORT_NAMES; do
    upsert_env_var "$env_file" "$name" "$(slot_port "$dir" "$name")"
  done
  read -ra env_sets <<<"$SLOT_ENV_SET"
  for env_set in ${env_sets[@]+"${env_sets[@]}"}; do
    upsert_env_var "$env_file" "${env_set%%=*}" "${env_set#*=}"
  done
  echo "▸ synced COMPOSE_PROJECT_NAME and ${SLOT_PORT_NAMES// //} in $env_file to this slot's own values"
}

slot_compose_projects() {
  local n
  slot_compose "" "" ls --format json 2>/dev/null | jq -r '.[].Name' | while IFS= read -r n; do
    # shellcheck disable=SC2254
    case "$n" in
      $SLOT_PROJECT_GLOB) echo "$n" ;;
    esac
  done
}
