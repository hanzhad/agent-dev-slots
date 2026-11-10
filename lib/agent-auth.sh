# shellcheck shell=bash disable=SC2034,SC2154

# What a slot pushes with. FORGE_AUTH in project.env picks the way:
#   token  GH_TOKEN from FORGE_AUTH_FILE is written to the slot's env file on
#          the host. That file only fills the compose file's ${VAR}s; the token
#          is in the container only if the project's compose file passes
#          GH_TOKEN on to the service.
#   app    the slot holds no token; a one-hour App installation token is
#          minted on the host per 'run', cached per project, and handed to that
#          one exec by name.
# Absent, the slot holds none. Names are reported, never values.

# FORGE_APP_SETTINGS, the names the App mode needs, and the checks of the
# credential file are in preflight.sh: adopt and doctor use them too.
AGENT_TOKEN=""
AGENT_TOKEN_ERROR=""
AGENT_TOKEN_MIN_LEFT=1800
AGENT_TOKEN_LOCK_HELD=0
AGENT_TOKEN_LOCK=""

agent_auth_file() { echo "$repo_root/$FORGE_AUTH_FILE"; }

# Where the App's private key is: as written when absolute, else under the checkout.
agent_key_file() { cred_key_path "$repo_root" "$(auth_value FORGE_APP_KEY_FILE)"; }

# A setting's value from the credential file.
auth_value() {
  [[ "$1" =~ ^[A-Z][A-Z0-9_]*$ && -f "$(agent_auth_file)" ]] || return 0
  env_value "$(agent_auth_file)" "$1"
}

# Refuses the first names of the App settings, by name.
refuse_legacy_auth_names() {
  [[ "$FORGE_AUTH" == app && -f "$(agent_auth_file)" ]] || return 0
  local key legacy used=""
  for key in $FORGE_APP_SETTINGS; do
    legacy=${key/FORGE_/AGENT_}
    if grep -q "^${legacy}=" "$(agent_auth_file)"; then
      used+="${used:+, }$legacy renamed to $key"
    fi
  done
  [[ -z "$used" ]] || die "$(agent_auth_file): $used; run \`${CALLER:-dev-container} migrate-names\`."
}

require_agent_auth() {
  [[ -n "$FORGE_AUTH" ]] || return 0
  refuse_legacy_auth_names
  preflight_reset
  credential_problems "$repo_root" "$FORGE_AUTH" "$FORGE_AUTH_FILE"
  (( ${#PF_WHAT[@]} == 0 )) || preflight_fail
}

drop_env_key() {
  local file="$1" key="$2" tmp line
  [[ -f "$file" ]] || return 0
  grep -q "^${key}=" "$file" || return 0
  tmp=$(umask 077; mktemp) || return 1
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ "$line" == "${key}="* ]] || printf '%s\n' "$line" >>"$tmp"
  done <"$file"
  mv "$tmp" "$file"
}

# Brings a slot's env file in step with FORGE_AUTH: the token in, or out, and
# for app the bot's commit identity. Runs on every acquire and restart.
sync_agent_auth_env() {
  local proj="$1" env_file slug bot_id
  env_file=$(slot_env_file_of "$proj")
  [[ -f "$env_file" ]] || return 0
  case "$FORGE_AUTH" in
    token)
      upsert_env_var "$env_file" GH_TOKEN "$(auth_value GH_TOKEN)" ;;
    app)
      drop_env_key "$env_file" GH_TOKEN
      slug=$(auth_value FORGE_APP_SLUG)
      bot_id=$(auth_value FORGE_APP_BOT_ID)
      upsert_env_var "$env_file" GIT_USER_NAME "$slug[bot]"
      upsert_env_var "$env_file" GIT_USER_EMAIL "$bot_id+$slug[bot]@users.noreply.github.com" ;;
    "")
      drop_env_key "$env_file" GH_TOKEN ;;
    *) die "internal: FORGE_AUTH $FORGE_AUTH has no rule for the slot env" ;;
  esac
  chmod 600 "$env_file"
}

b64url() { openssl base64 -A | tr '+/' '-_' | tr -d '='; }

agent_token_cached() {
  local exp="" tok="" file="$STATE/tokens/$SLOT_PROJECT_PREFIX"
  [[ -f "$file" ]] || return 1
  { IFS= read -r exp && IFS= read -r tok; } <"$file" 2>/dev/null || return 1
  [[ "$exp" =~ ^[0-9]{1,12}$ && -n "$tok" ]] || return 1
  (( 10#$exp - $(now_epoch) > AGENT_TOKEN_MIN_LEFT )) || return 1
  AGENT_TOKEN="$tok"
}

agent_token_lock() {
  local waited=0
  until lock_take "$AGENT_TOKEN_LOCK"; do
    waited=$((waited + 1))
    (( waited <= 150 )) || die "the installation-token lock at $AGENT_TOKEN_LOCK was busy for 30s — another run is minting. If nothing else is running, a stuck previous run left it: remove it by hand (rm -f $AGENT_TOKEN_LOCK)."
    sleep 0.2
  done
  AGENT_TOKEN_LOCK_HELD=1
}

agent_token_unlock() {
  rm -f "$AGENT_TOKEN_LOCK"
  AGENT_TOKEN_LOCK_HELD=0
}

# Mints a token and caches it under this project's own name. Returns 1 with
# AGENT_TOKEN_ERROR set (never a value) on any failure, leaving the cache as it
# was. The JWT goes to curl in a config on stdin, never in an argument.
agent_mint_token() {
  local id inst key repos hdr claims sig jwt body resp tok exp_iso exp_epoch tmp rc now
  id=$(auth_value FORGE_APP_ID)
  inst=$(auth_value FORGE_APP_INSTALLATION_ID)
  key=$(agent_key_file)
  repos=$(auth_value FORGE_APP_REPOS)
  [[ "$inst" =~ ^[0-9]+$ ]] || { AGENT_TOKEN_ERROR="FORGE_APP_INSTALLATION_ID is not a number"; return 1; }
  command -v openssl >/dev/null || { AGENT_TOKEN_ERROR="openssl is not on PATH"; return 1; }
  command -v curl >/dev/null || { AGENT_TOKEN_ERROR="curl is not on PATH"; return 1; }
  now=$(now_epoch)
  hdr=$(printf '%s' '{"alg":"RS256","typ":"JWT"}' | b64url) || { AGENT_TOKEN_ERROR="could not encode the JWT header"; return 1; }
  claims=$(jq -nc --argjson iat "$((now - 60))" --argjson exp "$((now + 540))" --arg iss "$id" '{iat:$iat,exp:$exp,iss:$iss}' | b64url) || { AGENT_TOKEN_ERROR="could not encode the JWT claims"; return 1; }
  sig=$(printf '%s' "$hdr.$claims" | openssl dgst -sha256 -sign "$key" 2>/dev/null | b64url) || sig=""
  [[ -n "$sig" ]] || { AGENT_TOKEN_ERROR="the key file named by FORGE_APP_KEY_FILE could not sign (not an RSA private key?)"; return 1; }
  jwt="$hdr.$claims.$sig"
  body=$(jq -nc --arg repos "$repos" '{repositories: [$repos | match("[^[:space:]]+"; "g").string], permissions: {contents: "write", pull_requests: "write", issues: "write", actions: "read", checks: "read", statuses: "read", metadata: "read"}}') || { AGENT_TOKEN_ERROR="could not build the token request"; return 1; }
  rc=0
  resp=$(
    {
      printf 'url = "https://api.github.com/app/installations/%s/access_tokens"\n' "$inst"
      printf 'header = "Accept: application/vnd.github+json"\n'
      printf 'header = "X-GitHub-Api-Version: 2022-11-28"\n'
      printf 'header = "Authorization: Bearer %s"\n' "$jwt"
      printf 'data = %s\n' "$(printf '%s' "$body" | jq -Rs .)"
    } | curl -sS -f --connect-timeout 10 --max-time 20 -K -
  ) || rc=$?
  jwt=""
  (( rc == 0 )) || { AGENT_TOKEN_ERROR="GitHub refused or did not answer the token request (curl exit $rc) — check FORGE_APP_ID, FORGE_APP_INSTALLATION_ID and the key in $(agent_auth_file)"; return 1; }
  tok=$(printf '%s' "$resp" | jq -r '.token // empty' 2>/dev/null) || tok=""
  exp_iso=$(printf '%s' "$resp" | jq -r '.expires_at // empty' 2>/dev/null) || exp_iso=""
  resp=""
  [[ -n "$tok" && -n "$exp_iso" ]] || { AGENT_TOKEN_ERROR="GitHub's answer carried no token or no expiry"; return 1; }
  exp_epoch=$(date -u -d "$exp_iso" +%s 2>/dev/null || date -u -j -f '%Y-%m-%dT%H:%M:%SZ' "$exp_iso" +%s 2>/dev/null || true)
  [[ "$exp_epoch" =~ ^[0-9]{1,12}$ ]] && (( exp_epoch > now )) || { AGENT_TOKEN_ERROR="GitHub's answer carried an expiry this host cannot read"; return 1; }
  tmp=$(umask 077; mktemp "$STATE/tokens/$SLOT_PROJECT_PREFIX.XXXXXX") || { AGENT_TOKEN_ERROR="could not create a file in $STATE/tokens"; return 1; }
  if ! { printf '%s\n%s\n' "$exp_epoch" "$tok" >"$tmp" && mv "$tmp" "$STATE/tokens/$SLOT_PROJECT_PREFIX"; }; then
    rm -f "$tmp"
    AGENT_TOKEN_ERROR="could not write the token cache under $STATE/tokens"
    return 1
  fi
  AGENT_TOKEN="$tok"
}

# Sets AGENT_TOKEN from this project's cache, or mints one under the project's
# lock after a second look at the cache, so two runs at once mint once. Dies
# with the reason; never falls back to another project's token.
agent_installation_token() {
  local key rc=0
  AGENT_TOKEN=""
  AGENT_TOKEN_ERROR=""
  require_agent_auth
  key=$(agent_key_file)
  [[ -f "$key" && -r "$key" ]] || die "FORGE_APP_KEY_FILE in $(agent_auth_file) does not name a readable file — the App cannot sign its token request."
  agent_token_cached && return 0
  mkdir -p -m 700 "$STATE/tokens"
  AGENT_TOKEN_LOCK="$STATE/tokens/$SLOT_PROJECT_PREFIX.lock"
  agent_token_lock
  agent_token_cached || agent_mint_token || rc=1
  agent_token_unlock
  (( rc == 0 )) || die "could not get a GitHub App installation token: $AGENT_TOKEN_ERROR"
}
