# shellcheck shell=bash disable=SC2034,SC2154

# What the engine puts into a slot after the slot is up, so no project has to
# hand-write it into its compose or entrypoint files. The slot's env file only
# fills the compose file's ${VAR}s; it puts nothing into the container. This
# does, over one exec as root into the one service that mounts /workspace:
#   1. git's credential helper for https://github.com, which reads $GH_TOKEN
#      when git asks (token mode: the slot's token, app mode: the one minted
#      per 'run'; the helper is the same);
#   2. the project's signing key (lib/signing.sh) for the container user, and
#      the system git config that signs every commit with it;
#   3. a check that 'gh' is on the container user's PATH.
# It runs after every compose up (acquire, restart, and 'up', which restarts),
# so a recreated container gets it all again; the key itself lives on the host
# under $STATE/signing/<key>/ and is streamed in, never copied by hand.
# Safe to run again: every step replaces what an earlier run wrote. A project
# with no FORGE_AUTH has no forge identity to provision and is left alone.
# SLOT_GIT_NAME and SLOT_GIT_EMAIL, when project.env gives them, become the system
# git identity too, so a commit made anywhere in the slot carries it.

# git runs a '!' helper with 'get', 'store' or 'erase' appended. Nothing is
# printed without a token, so git falls through to its next source.
PROVISION_GIT_HELPER='!f() { test "$1" = get && test -n "$GH_TOKEN" && echo username=x-access-token && echo "password=$GH_TOKEN"; }; f'

# Runs as root in the container: $1 home, $2 owner, $3 public key line, $4 the
# helper; the private key arrives on stdin. The key is written beside its
# final name and moved over it, so a reader never sees half of one. One line
# of shell, like the engine's other exec scripts, so a log of the call is one
# line too.
PROVISION_SCRIPT='set -eu; home=$1 owner=$2 pub=$3 helper=$4 gname=${5-} gemail=${6-}; dir=$home/.ssh; '
PROVISION_SCRIPT+='install -d -m 0700 "$dir"; chown "$owner:" "$dir"; '
PROVISION_SCRIPT+='new=$dir/.container_signing.new; rm -f "$new" "$new.pub"; '
PROVISION_SCRIPT+='( umask 077; cat >"$new" ); '
PROVISION_SCRIPT+='[ -s "$new" ] || { rm -f "$new"; echo "no private key arrived on stdin" >&2; exit 4; }; '
PROVISION_SCRIPT+='chown "$owner:" "$new"; chmod 0600 "$new"; '
PROVISION_SCRIPT+='printf "%s\n" "$pub" >"$new.pub"; chown "$owner:" "$new.pub"; chmod 0644 "$new.pub"; '
PROVISION_SCRIPT+='mv -f "$new.pub" "$dir/container_signing.pub"; mv -f "$new" "$dir/container_signing"; '
PROVISION_SCRIPT+='git config --system --replace-all "credential.https://github.com.helper" "$helper"; '
PROVISION_SCRIPT+='git config --system --replace-all gpg.format ssh; '
PROVISION_SCRIPT+='git config --system --replace-all user.signingkey "$dir/container_signing"; '
PROVISION_SCRIPT+='git config --system --replace-all commit.gpgsign true; '
PROVISION_SCRIPT+='if [ -n "$gname" ]; then git config --system --replace-all user.name "$gname"; else git config --system --unset-all user.name || true; fi; '
PROVISION_SCRIPT+='if [ -n "$gemail" ]; then git config --system --replace-all user.email "$gemail"; else git config --system --unset-all user.email || true; fi'

# Provisions one slot. DIR is its checkout dir (empty for a volume slot), SID
# its project id. Returns 1 with the reason and the exact fix on stderr; the
# callers decide what that stops.
provision_slot() {
  local dir="$1" sid="$2" name key user svc
  [[ -n "$FORGE_AUTH" ]] || return 0
  name=$(slot_name_of "$sid")
  user=${CONTAINER_USER:-node}

  key=$(ensure_signing_key "$STATE" "$SLOT_PROJECT_PREFIX") || {
    echo "provision: '$name' has no signing key to install — the reason is above." >&2
    return 1
  }
  svc=$(container_service_of "$dir" "$sid") || {
    echo "provision: '$name' cannot be provisioned without the one service that mounts the checkout at /workspace." >&2
    return 1
  }
  slot_compose "$dir" "$sid" exec -T -u root "$svc" \
    sh -c "$PROVISION_SCRIPT" dev-container-provision "/home/$user" "$user" "$(cat "$key.pub")" "$PROVISION_GIT_HELPER" "${SLOT_GIT_NAME:-}" "${SLOT_GIT_EMAIL:-}" <"$key" || {
    echo "provision: could not install the git credentials and the signing key into '$name' (the reason is above). Is its container running? Fix that, then '$CALLER restart $name' runs this again." >&2
    return 1
  }
  slot_compose "$dir" "$sid" exec -T -u "$user" "$svc" sh -c 'command -v gh' >/dev/null 2>&1 || {
    echo "provision: 'gh' is not installed in '$name' — add it to the slot's image (Debian: apt-get install -y --no-install-recommends gh; the Dockerfile that '$CALLER adopt' writes has it), then '$CALLER restart $name'." >&2
    return 1
  }
}
