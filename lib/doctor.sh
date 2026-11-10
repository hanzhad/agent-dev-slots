# shellcheck shell=bash disable=SC2034,SC2154

# `doctor`: what this machine needs before a slot can start. Every failed
# prerequisite is named, not only the first, and any failure exits non-zero.

DOCTOR_FAILURES=0

doctor_fail() {
  echo "FAIL  $*" >&2
  DOCTOR_FAILURES=$((DOCTOR_FAILURES + 1))
}

doctor_ok() { echo "ok    $*"; }

doctor_docker() {
  local in_container=0 where="the Docker daemon"
  [[ -f "${DEV_CONTAINER_DOCKERENV_PATH:-/.dockerenv}" ]] && in_container=1
  (( ! in_container )) || where="the Docker socket (${DOCKER_HOST:-/var/run/docker.sock}) — this runs in a container, which needs it mounted"
  if ! command -v docker >/dev/null 2>&1; then
    doctor_fail "docker is not on PATH — $where cannot be reached"
    return 1
  fi
  if ! docker info >/dev/null 2>&1; then
    doctor_fail "docker cannot reach $where"
    return 1
  fi
  doctor_ok "docker answers"
}

doctor_state() {
  local probe="$STATE/.doctor.$$"
  if [[ -n "$state_error" ]] || ! { : >"$probe"; } 2>/dev/null; then
    doctor_fail "the state directory $STATE is not writable"
    return
  fi
  rm -f "$probe"
  doctor_ok "state directory $STATE is writable"
}

# A port a slot would be given that something else already holds. A slot's own
# ports are busy by design, so a slot with a recorded port file is skipped.
doctor_ports() {
  local number index name port busy=""
  for (( number = 1; number <= SLOT_INDEX_CEILING; number++ )); do
    [[ ! -f "$STATE/ports/$SLOT_PROJECT_PREFIX-slot-$number.ports" ]] || continue
    index=0
    for name in $SLOT_PORT_NAMES; do
      port=$(( SLOT_PORT_BASE + number * SLOT_PORT_BLOCK + index ))
      index=$((index + 1))
      port_is_forbidden "$port" && continue
      ! port_in_use "$port" || busy+=" $port"
    done
  done
  if [[ -n "$busy" ]]; then
    doctor_fail "slot ports in use by something that is not a slot:$busy — stop what holds them, or set DEV_CONTAINER_PORT_BASE"
    return
  fi
  doctor_ok "the slot ports from $SLOT_PORT_BASE are free"
}

doctor_project_file() {
  if [[ ! -f "$repo_root/project.env" ]]; then
    doctor_fail "no project.env in $repo_root — it is not a connected project"
    return
  fi
  doctor_ok "project.env read ($SLOT_PROJECT_PREFIX, default branch $DEFAULT_BRANCH)"
}

# The credential file project.env names: present, private, complete, and a key
# file it points to readable by the engine. LIVE also asks GitHub about a token.
doctor_credential() {
  local live="$1" i
  [[ -f "$repo_root/project.env" && -n "$FORGE_AUTH" ]] || return 0
  preflight_reset
  credential_problems "$repo_root" "$FORGE_AUTH" "$FORGE_AUTH_FILE"
  (( ! live )) || credential_live_problems "$repo_root" "$FORGE_AUTH" "$FORGE_AUTH_FILE"
  if (( ${#PF_WHAT[@]} == 0 )); then
    doctor_ok "credential file $FORGE_AUTH_FILE: present, private, holds what FORGE_AUTH=$FORGE_AUTH needs"
    [[ -z "$CRED_LIVE_NOTE" ]] || doctor_ok "$CRED_LIVE_NOTE"
    return 0
  fi
  [[ -z "$CRED_LIVE_NOTE" ]] || doctor_ok "$CRED_LIVE_NOTE"
  for (( i = 0; i < ${#PF_WHAT[@]}; i++ )); do
    doctor_fail "${PF_WHAT[i]} — Fix: ${PF_FIX[i]}"
  done
}

doctor_compose() {
  local i
  [[ -f "$repo_root/project.env" ]] || return 0
  preflight_reset
  compose_problems "$repo_root" "$COMPOSE_DIR" "$COMPOSE_FILES"
  if (( ${#PF_WHAT[@]} == 0 )); then
    doctor_ok "compose file ${COMPOSE_DIR%/}/${COMPOSE_FILES%% *} is there"
    return 0
  fi
  for (( i = 0; i < ${#PF_WHAT[@]}; i++ )); do
    doctor_fail "${PF_WHAT[i]} — Fix: ${PF_FIX[i]}"
  done
}

# The contract the compose files keep with the engine: the service, the ports, no secret.
doctor_contract() {
  local i
  [[ -f "$repo_root/project.env" ]] || return 0
  preflight_reset
  contract_problems "$repo_root" "$COMPOSE_DIR" "$COMPOSE_FILES" "$SLOT_PORT_NAMES" "${LIVE_SERVICE:-}"
  if (( ${#PF_WHAT[@]} == 0 )); then
    doctor_ok "compose files keep the contract: service app, host ports from SLOT_PORTS, no secret written in"
    return 0
  fi
  for (( i = 0; i < ${#PF_WHAT[@]}; i++ )); do
    doctor_fail "${PF_WHAT[i]} — Fix: ${PF_FIX[i]}"
  done
}

doctor_docs() {
  local line
  [[ -f "$repo_root/project.env" ]] || return 0
  while IFS= read -r line; do
    [[ -z "$line" ]] || doctor_ok "$line"
  done < <(adopt_docs_report "$repo_root"; seed_report "$repo_root")
}

doctor_image_commit() {
  local ref="" proj img commit=""
  while IFS= read -r proj; do
    img=$(slot_image_names "$proj" | head -1)
    [[ -z "$img" ]] || { ref=$img; break; }
  done < <(slot_compose_projects)
  [[ -z "$ref" ]] || commit=$(docker image inspect --format "{{ index .Config.Labels \"$IMAGE_REVISION_LABEL\" }}" "$ref" 2>/dev/null | head -1) || commit=""
  if [[ -n "$commit" && "$commit" != "<no value>" ]]; then
    echo "image commit: $commit ($ref)"
  else
    echo "image commit: none — no image of this project carries $IMAGE_REVISION_LABEL; 'install' builds one"
  fi
}

# The global `dev-container` command (scripts/dev-container-global): whether it is on
# PATH and which engine clone it runs from. The command works that out on the host and
# hands it over in DEV_CONTAINER_CLI_*; from inside the container PATH is not the host's.
# A note, never a failure: a project's own launcher works without it.
doctor_command() {
  local state=${DEV_CONTAINER_CLI_STATE:-} found=${DEV_CONTAINER_CLI_FOUND:-} clone=${DEV_CONTAINER_CLI_CLONE:-}
  case "$state" in
    path) doctor_ok "dev-container command: $found -> engine clone $clone" ;;
    off-path) echo "note  dev-container command: not on PATH (this call ran from the engine clone $clone) — run scripts/install-cli.sh there to install it" ;;
    shadowed) echo "note  dev-container command: $found comes earlier on PATH than the one from the engine clone $clone, so it is the one that runs" ;;
    windows-shim) doctor_ok "dev-container command: the Windows shim, running in WSL from the engine clone $clone" ;;
    *) echo "note  dev-container command: not checked — this was not run through the dev-container command (scripts/install-cli.sh in an engine clone installs it)" ;;
  esac
}

cmd_doctor() {
  local live=0
  if [[ "${1:-}" == --live ]]; then
    live=1
    shift
  fi
  [[ $# -eq 0 ]] || die "doctor takes no arguments except --live, which also asks GitHub whether the token in the credential file is valid"
  local docker_ok=0
  doctor_docker && docker_ok=1
  doctor_state
  doctor_ports
  doctor_project_file
  doctor_command
  doctor_compose
  doctor_contract
  doctor_credential "$live"
  doctor_docs
  echo "SLOT_MEMORY_LIMIT = $(slot_memory_limit) ($(slot_memory_limit_source))"
  (( ! docker_ok )) || doctor_image_commit
  (( DOCTOR_FAILURES == 0 )) || exit 1
  echo "doctor: every prerequisite is met"
}
