# shellcheck shell=bash

# The key a project's commits are signed with. One ed25519 pair per project under
# $STATE/signing/<project>/, so the public half is registered on the agent account
# once and survives a slot recreate and a .devcontainer wipe. The functions take
# the state directory as an argument and hold no global; nothing here is called
# by bin/dev-container yet.

signing_key_path() {
  local state="$1" project="$2"
  if [[ -z "$state" || -z "$project" || "$project" == . || "$project" == */* || "$project" == *..* ]]; then
    echo "signing key: '$project' is not a project name (empty, '.', or it contains '/' or '..')." >&2
    return 1
  fi
  printf '%s\n' "$state/signing/$project/container_signing"
}

# What the owner does with the public half. MODE is AGENT_AUTH, ACCOUNT the GitHub
# login that pushes, PUBKEYFILE the .pub path.
signing_registration_hint() {
  local mode="$1" account="$2" pub="$3" project
  project=$(basename "$(dirname "$pub")")
  case "$mode" in
    container-env)
      cat <<HINT
Commits from this project are signed with $pub.
GitHub shows them as Verified once that key is registered as a signing key on $account. Do it once, logged in as $account (the token needs the admin:ssh_signing_key scope):
  gh ssh-key add $pub --type signing --title "dev-container signing $project"
HINT
      ;;
    github-app)
      cat <<HINT
Commits from this project are signed with $pub, but a GitHub App bot ($account) cannot register keys: there is no signing key to add for it.
Its commits are Verified only when GitHub creates them through the API (the contents or git data endpoints); a commit signed in the slot and pushed shows as unverified.
HINT
      ;;
    *)
      echo "signing key: '$mode' is not an AGENT_AUTH mode (container-env or github-app)." >&2
      return 1
      ;;
  esac
}

# Creates the project's pair when it has none and prints the private key's path.
# An existing key is never regenerated: its public half is already registered.
ensure_signing_key() {
  local state="$1" project="$2" key dir tmp
  key=$(signing_key_path "$state" "$project") || return 1
  dir=$(dirname "$key")
  if [[ -n "${PROJECT_KEY:-}" && -d "$state/signing/$project" && ! -e "$state/signing/$PROJECT_KEY" ]]; then
    echo "signing key: $state/signing/$project is kept under the project's old name. Run \`${CALLER:-dev-container} migrate-identity\` first, so the key is not generated a second time." >&2
    return 1
  fi
  (umask 077; mkdir -p "$dir") && chmod 700 "$dir" || { echo "signing key: cannot create $dir" >&2; return 1; }
  if [[ ! -e "$key" ]]; then
    if [[ -e "$key.pub" ]]; then
      echo "signing key: $key.pub exists without $key. Remove the public half to start over; the engine will not replace a key that may be registered." >&2
      return 1
    fi
    tmp=$(umask 077; mktemp -u "$dir/.new.XXXXXX") || return 1
    ssh-keygen -q -N "" -t ed25519 -C "dev-container-signing-$project" -f "$tmp" >/dev/null || { rm -f "$tmp" "$tmp.pub"; return 1; }
    # ln fails when a concurrent run installed its key first; that one wins.
    if ln "$tmp" "$key" 2>/dev/null; then mv -f "$tmp.pub" "$key.pub"; fi
    rm -f "$tmp" "$tmp.pub"
  fi
  chmod 600 "$key"
  if [[ ! -s "$key.pub" ]]; then
    tmp=$(umask 077; mktemp "$dir/.pub.XXXXXX") || return 1
    ssh-keygen -y -f "$key" >"$tmp" || { rm -f "$tmp"; return 1; }
    chmod 644 "$tmp"
    mv -f "$tmp" "$key.pub"
  fi
  printf '%s\n' "$key"
}
