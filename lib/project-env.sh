# shellcheck shell=bash disable=SC2034,SC2154

SLOT_PORT_BLOCK=16

PROJECT_ENV_KEYS="PROJECT DEFAULT_BRANCH SLOT_PORTS SLOT_FORBIDDEN_PORTS DOCS_REPO_URL DOCS_PIN_FILE SIGNING_KEY_VOLUME SHARED_CACHE_VOLUME PNPM_STORE_VOLUME COMPOSE_DIR COMPOSE_FILES SECRETS_OVERLAY SLOT_ENV_KEYS SLOT_ENV_SET SLOT_COPY_FILES SLOT_MOUNT_DIRS SLOT_OVERLAYS FORGE_AUTH FORGE_AUTH_FILE LIVE_ENV_FILE LIVE_SERVICE LIVE_GATEWAY SLOT_MEMORY_LIMIT SLOT_GIT_NAME SLOT_GIT_EMAIL"
POOL_WIDE_KEYS="SLOT_CEILING SLOT_COUNT SLOT_PORT_RANGE IDLE_LIMIT"
PROJECT_ENV_REQUIRED="PROJECT DEFAULT_BRANCH SLOT_PORTS COMPOSE_FILES"
FORGE_AUTH_MODES="token app"

check_slot_ports() {
  local file="$1" entry seen=" "
  local -a entries
  read -ra entries <<<"$2"
  for entry in "${entries[@]}"; do
    if [[ ! "$entry" =~ ^[A-Z][A-Z0-9_]*$ ]]; then
      die "$file: SLOT_PORTS entry $entry is not a variable name — SLOT_PORTS lists names only, the ports are allocated."
    fi
    case "$seen" in
      *" $entry "*) die "$file: SLOT_PORTS lists $entry twice" ;;
    esac
    seen+="$entry "
  done
  if (( ${#entries[@]} > SLOT_PORT_BLOCK )); then
    die "$file: SLOT_PORTS lists ${#entries[@]} names; a slot's block holds $SLOT_PORT_BLOCK"
  fi
}

path_is_inside_checkout() {
  case "$1" in
    /*|..|../*|*/..|*/../*) return 1 ;;
  esac
}

check_relative_paths() {
  local file="$1" key="$2" value="$3" entry
  local -a entries
  read -ra entries <<<"$value"
  for entry in ${entries[@]+"${entries[@]}"}; do
    path_is_inside_checkout "$entry" || die "$file: $key entry $entry is not a path inside the checkout — it must be relative and must not contain '..'"
  done
}

check_slot_overlays() {
  local file="$1" entry
  local -a entries
  read -ra entries <<<"$2"
  for entry in ${entries[@]+"${entries[@]}"}; do
    if [[ ! "$entry" =~ ^[A-Z][A-Z0-9_]*=(.+)$ ]] || ! path_is_inside_checkout "${BASH_REMATCH[1]}"; then
      die "$file: SLOT_OVERLAYS entry $entry is not VARIABLE=file — the file is relative to the compose directory and is added when the variable is set in the slot's env."
    fi
  done
}

check_memory_limit() {
  local file="$1" value="$2"
  [[ -z "$value" ]] && return 0
  [[ "$value" =~ ^[1-9][0-9]*([bBkKmMgG]|[kKmMgG][bB])?$ ]]     || die "$file: SLOT_MEMORY_LIMIT $value is not a memory size — a whole number with an optional b, k, m or g suffix, e.g. 4g."
}

check_default_branch() {
  local file="$1" name="$2" checked
  checked=$(git check-ref-format --branch "$name" 2>/dev/null) || checked=""
  [[ "$checked" == "$name" ]] || die "$file: DEFAULT_BRANCH $name is not a branch name git accepts"
}

# SLOT_GIT_NAME and SLOT_GIT_EMAIL are the slot's git identity, both or neither.
check_slot_git_identity() {
  local file="$1" name="${PE_SLOT_GIT_NAME:-}" email="${PE_SLOT_GIT_EMAIL:-}"
  [[ -n "$name$email" ]] || return 0
  [[ -n "$name" ]] || die "$file: SLOT_GIT_EMAIL is set but SLOT_GIT_NAME is not - a slot's git identity is both."
  [[ -n "$email" ]] || die "$file: SLOT_GIT_NAME is set but SLOT_GIT_EMAIL is not - a slot's git identity is both."
  local nl=$'\n' cr=$'\r'
  case "$name" in *["$nl$cr<>"]*) die "$file: SLOT_GIT_NAME must not contain a line break, < or > - git would mangle it." ;; esac
  [[ "$email" =~ ^[^[:space:]@\<\>]+@[^[:space:]@\<\>]+$ ]] || die "$file: SLOT_GIT_EMAIL is not an email address."
}

check_agent_auth_keys() {
  local file="$1" entry
  local -a entries
  [[ "$PE_PROJECT" =~ ^[a-z0-9][a-z0-9_-]*$ ]] || die "$file: PROJECT $PE_PROJECT is not a name — it must be lower-case letters, digits, - and _, starting with a letter or digit, because it names the project's token cache."
  if [[ -n "${PE_FORGE_AUTH:-}" ]]; then
    case " $FORGE_AUTH_MODES " in
      *" $PE_FORGE_AUTH "*) ;;
      *) die "$file: FORGE_AUTH $PE_FORGE_AUTH is not a mode — the modes are: ${FORGE_AUTH_MODES// /, }." ;;
    esac
    [[ -n "${PE_FORGE_AUTH_FILE:-}" ]] || die "$file: FORGE_AUTH is set but FORGE_AUTH_FILE is not — it names the file, relative to the checkout, that holds the token or the App settings."
  fi
  check_relative_paths "$file" FORGE_AUTH_FILE "${PE_FORGE_AUTH_FILE:-}"
  check_relative_paths "$file" LIVE_ENV_FILE "${PE_LIVE_ENV_FILE:-}"
  [[ -z "${PE_LIVE_SERVICE:-}" || "$PE_LIVE_SERVICE" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]] || die "$file: LIVE_SERVICE $PE_LIVE_SERVICE is not a compose service name"
  [[ -z "${PE_LIVE_GATEWAY:-}" || "$PE_LIVE_GATEWAY" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]] || die "$file: LIVE_GATEWAY $PE_LIVE_GATEWAY is not a compose service name"
  read -ra entries <<<"${PE_SLOT_ENV_KEYS:-}"
  for entry in ${entries[@]+"${entries[@]}"}; do
    [[ "$entry" != GH_TOKEN ]] || die "$file: GH_TOKEN is listed in SLOT_ENV_KEYS — a forge token reaches a slot only through FORGE_AUTH, never through the seed."
  done
  read -ra entries <<<"${PE_SLOT_ENV_SET:-}"
  for entry in ${entries[@]+"${entries[@]}"}; do
    [[ "${entry%%=*}" != GH_TOKEN ]] || die "$file: GH_TOKEN is set in SLOT_ENV_SET — a forge token reaches a slot only through FORGE_AUTH, never through the seed."
  done
}

read_project_env() {
  local file="$1/project.env" line key value q rawvar new what renamed
  local -a legacy=()
  [[ -f "$file" ]] || die "$1 has no project.env — it is not a connected project."
  for key in $PROJECT_ENV_KEYS; do unset "PE_$key" "PE_RAW_$key"; done
  while IFS= read -r line || [[ -n "$line" ]]; do
    line=${line%$'\r'}
    [[ "$line" =~ ^[[:space:]]*(#.*)?$ ]] && continue
    [[ "$line" =~ ^([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]] || die "$file: not a KEY=value line: $line"
    key=${BASH_REMATCH[1]} value=${BASH_REMATCH[2]}
    q=${value:0:1}
    if [[ ${#value} -ge 2 && ( "$q" == '"' || "$q" == "'" ) && "${value: -1}" == "$q" ]]; then
      value=${value:1:${#value}-2}
    fi
    renamed=0
    if new=$(migrate_renamed_key "$key"); then
      legacy+=("$key renamed to $new")
      renamed=1
    fi
    if [[ "$key" == FORGE_AUTH || "$key" == AGENT_AUTH ]] && new=$(migrate_renamed_mode "$value"); then
      legacy+=("$value renamed to $new")
    fi
    (( ! renamed )) || continue
    case " $PROJECT_ENV_KEYS " in
      *" $key "*) ;;
      *)
        case " $POOL_WIDE_KEYS " in
          *" $key "*) die "$file: $key is not a per-project setting — the pool is one for the whole machine: its only cap is machine_max in the machine limits (limits.json), ports are allocated from the slot's number, and the idle limit is one machine value there too. Remove $key from project.env." ;;
        esac
        die "$file: unknown key $key" ;;
    esac
    rawvar="PE_RAW_$key"
    [[ -z "${!rawvar:-}" ]] || die "$file: $key is set twice"
    printf -v "$rawvar" %s "$key"
    printf -v "PE_$key" %s "$value"
  done <"$file"
  if (( ${#legacy[@]} )); then
    printf -v what '%s, ' "${legacy[@]}"
    die "$file: ${what%, }; run \`${CALLER:-dev-container} migrate-names\`."
  fi
  for key in $PROJECT_ENV_REQUIRED; do
    value="PE_$key"
    [[ -n "${!value:-}" ]] || die "$file: $key is required and empty or missing"
  done
  check_agent_auth_keys "$file"
  check_slot_git_identity "$file"
}

check_slot_env_keys() {
  local file="$1" entry
  local -a entries
  read -ra entries <<<"$2"
  for entry in ${entries[@]+"${entries[@]}"}; do
    [[ "$entry" =~ ^[A-Z][A-Z0-9_]*$ ]] || die "$file: SLOT_ENV_KEYS entry $entry is not a variable name — it lists the names of the .env keys a new slot is seeded with."
  done
}

check_project_env() {
  local label="$1" branch="$2" ports="$3" mount_dirs="$4" overlays="$5" env_keys="${6:-}" memory="${7:-}"
  check_default_branch "$label" "$branch"
  check_slot_ports "$label" "$ports"
  check_relative_paths "$label" SLOT_MOUNT_DIRS "$mount_dirs"
  check_slot_overlays "$label" "$overlays"
  check_slot_env_keys "$label" "$env_keys"
  check_memory_limit "$label" "$memory"
}

apply_project_env() {
  local key var
  DEFAULT_BRANCH=$PE_DEFAULT_BRANCH
  SLOT_PORT_NAMES=$PE_SLOT_PORTS
  for key in DOCS_REPO_URL DOCS_PIN_FILE SIGNING_KEY_VOLUME SHARED_CACHE_VOLUME PNPM_STORE_VOLUME COMPOSE_DIR COMPOSE_FILES SECRETS_OVERLAY SLOT_ENV_KEYS SLOT_ENV_SET SLOT_COPY_FILES SLOT_MOUNT_DIRS SLOT_OVERLAYS FORGE_AUTH FORGE_AUTH_FILE LIVE_ENV_FILE LIVE_SERVICE LIVE_GATEWAY SLOT_GIT_NAME SLOT_GIT_EMAIL; do
    var="PE_$key"
    [[ -z "${!var+x}" ]] || printf -v "$key" %s "${!var}"
  done
}
