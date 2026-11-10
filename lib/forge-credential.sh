# shellcheck shell=bash disable=SC2034,SC2154

# How the engine's own git and gh calls on the host authenticate. The project's
# FORGE_AUTH credential (the token in FORGE_AUTH_FILE, or an App installation
# token minted from it) is the only one used for https://github.com, and a call
# can never stop to ask for one:
#
#   - git gets the credential from a helper, defined here, that answers only for
#     protocol https and host github.com. The helper reads the secret from the
#     call's environment, so the secret is never in an argument, a file or a log.
#     The configuration reaches git through GIT_CONFIG_COUNT/KEY/VALUE, which
#     leaves the argument list as the caller wrote it.
#   - the owner's own helpers and askpass program are reset for that origin, so
#     what the project holds is what is used; GIT_TERMINAL_PROMPT=0 and a
#     failing GIT_ASKPASS make "no credential" an error, not a question.
#   - hooks are switched off in a call that carries the secret: a slot's hook is
#     the slot's, and this runs on the host.
#
# With no FORGE_AUTH there is nothing to supply: git and gh run as before, still
# unable to prompt, and a failure says what to set (forge_credential_hint).
# Messages name settings and files, never a value.

FORGE_HOST_ORIGIN="https://github.com"
FORGE_HOST_SECRET=""
FORGE_HOST_STATE=""
FORGE_HINT_SHOWN=0

# shellcheck disable=SC2016
FORGE_HOST_HELPER='!f() { [ "$1" = get ] || exit 0; p=; h=; while IFS= read -r l; do case $l in protocol=*) p=${l#protocol=};; host=*) h=${l#host=};; esac; done; [ "$p" = https ] && [ "$h" = github.com ] || exit 0; printf "username=x-access-token\npassword=%s\n" "$FORGE_HOST_SECRET"; }; f'

forge_credential_hint() {
  echo "no forge credential for ${SLOT_PROJECT_PREFIX:-this project}: set FORGE_AUTH (token or app) and FORGE_AUTH_FILE in project.env — host-side git and gh calls to $FORGE_HOST_ORIGIN use that credential and no other."
}

# The hint, once per process, and only when DIR's origin is one this credential
# would have served and the project has none. It reads FORGE_AUTH, not the load
# state: the call that failed often ran in a $(...), where the state is lost.
forge_hint_for() {
  [[ -z "${FORGE_AUTH:-}" ]] || return 0
  (( FORGE_HINT_SHOWN == 0 )) || return 0
  local url; url=$(git -C "$1" remote get-url origin 2>/dev/null) || return 0
  [[ "$url" == "$FORGE_HOST_ORIGIN"/* ]] || return 0
  FORGE_HINT_SHOWN=1
  forge_credential_hint >&2
}

forge_host_app_token() {
  ( agent_installation_token >&2 && printf '%s' "$AGENT_TOKEN" )
}

# Sets FORGE_HOST_SECRET. 0: loaded. 1: the project has no FORGE_AUTH. 2: it has
# one and the secret could not be had; the reason is on stderr.
forge_host_credential_load() {
  case "$FORGE_HOST_STATE" in ready) return 0 ;; none) return 1 ;; failed) return 2 ;; esac
  FORGE_HOST_SECRET=""
  case "${FORGE_AUTH:-}" in
    "") FORGE_HOST_STATE=none; return 1 ;;
    token)
      FORGE_HOST_SECRET=$(auth_value GH_TOKEN) || FORGE_HOST_SECRET=""
      if [[ -z "$FORGE_HOST_SECRET" ]]; then
        echo "FORGE_AUTH=token: $(agent_auth_file) is missing: GH_TOKEN — host-side git and gh need it; add it there." >&2
      fi ;;
    app)
      FORGE_HOST_SECRET=$(forge_host_app_token) || FORGE_HOST_SECRET="" ;;
    *)
      echo "FORGE_AUTH=$FORGE_AUTH has no host-side credential rule" >&2 ;;
  esac
  if [[ -z "$FORGE_HOST_SECRET" ]]; then FORGE_HOST_STATE=failed; return 2; fi
  FORGE_HOST_STATE=ready
}

# Does this git call go to FORGE_HOST_ORIGIN: a URL among its arguments, or the
# origin of the checkout it names with -C. Anything else (a local path, ssh,
# another host) is none of this credential's business and is not touched.
forge_git_targets_forge() {
  local dir=. prev="" a url
  for a in "$@"; do
    [[ "$a" != "$FORGE_HOST_ORIGIN"/* ]] || return 0
    [[ "$prev" != -C ]] || dir=$a
    prev=$a
  done
  url=$(git -C "$dir" remote get-url origin 2>/dev/null) || return 1
  [[ "$url" == "$FORGE_HOST_ORIGIN"/* ]]
}

# git, as the host runs it for the engine: forge_git -C DIR fetch ... Same
# arguments, same exit status as git.
forge_git() {
  if ! forge_git_targets_forge "$@"; then
    GIT_TERMINAL_PROMPT=0 git "$@"
    return
  fi
  local rc=0
  forge_host_credential_load || rc=$?
  (
    local base=${GIT_CONFIG_COUNT:-0} n
    [[ "$base" =~ ^[0-9]+$ ]] || base=0
    export GIT_TERMINAL_PROMPT=0 GIT_ASKPASS=/bin/false
    export "GIT_CONFIG_KEY_$base=credential.$FORGE_HOST_ORIGIN.helper" "GIT_CONFIG_VALUE_$base="
    n=$((base + 1))
    if (( rc == 0 )); then
      export "GIT_CONFIG_KEY_$n=credential.$FORGE_HOST_ORIGIN.helper" "GIT_CONFIG_VALUE_$n=$FORGE_HOST_HELPER"
      export "GIT_CONFIG_KEY_$((n + 1))=core.hooksPath" "GIT_CONFIG_VALUE_$((n + 1))=/dev/null"
      n=$((n + 2))
      export FORGE_HOST_SECRET
    fi
    export GIT_CONFIG_COUNT=$n
    exec git "$@"
  )
}

# gh, with the project's credential in place of whatever the owner is logged in
# as. A credential the project configured but cannot produce stops the call.
forge_gh() {
  local rc=0
  forge_host_credential_load || rc=$?
  (( rc != 2 )) || return 1
  (
    export GH_PROMPT_DISABLED=1
    (( rc != 0 )) || export GH_TOKEN="$FORGE_HOST_SECRET"
    exec gh "$@"
  )
}
