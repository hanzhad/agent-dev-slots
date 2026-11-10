# shellcheck shell=bash disable=SC2034,SC2154

# What the engine can see, and what it must never copy.
#
# The engine runs in a container (scripts/engine install) that mounts only the
# projects root, its state directory and ~/.config/machine-limits, plus the
# docker socket. A path anywhere else exists on the host and not for the engine:
# a key file there reads as "not found" at the moment it is needed. Inside the
# container DEV_CONTAINER_PROJECTS_ROOT is set; without it the engine is not in
# its container and sees what the host sees, so nothing is refused for being
# out of sight.

engine_in_container() { [[ -n "${DEV_CONTAINER_PROJECTS_ROOT:-}" ]]; }

engine_visible_roots() {
  [[ -z "${DEV_CONTAINER_PROJECTS_ROOT:-}" ]] || printf '%s\n' "${DEV_CONTAINER_PROJECTS_ROOT%/}"
  [[ -z "${STATE:-}" ]] || printf '%s\n' "${STATE%/}"
  printf '%s\n' "${HOME:-}/.config/machine-limits"
}

engine_path_normalise() {
  realpath -m -- "$1" 2>/dev/null || printf '%s\n' "$1"
}

path_is_engine_visible() {
  engine_in_container || return 0
  local path root
  path=$(engine_path_normalise "$1")
  while IFS= read -r root; do
    [[ -n "$root" ]] || continue
    root=$(engine_path_normalise "$root")
    [[ "$path" == "$root" || "$path" == "$root"/* ]] && return 0
  done < <(engine_visible_roots)
  return 1
}

# "the projects root /a, the state directory /b and ~/.config/machine-limits"
engine_visible_summary() {
  local out="the projects root ${DEV_CONTAINER_PROJECTS_ROOT:-}"
  out+=", the state directory ${STATE:-} and ~/.config/machine-limits"
  printf '%s' "$out"
}

# Why a path cannot be used by the engine, ending in what to do. Two causes:
# the engine is not in its container (it has no projects root at all), or the
# path is outside the one it has.
engine_cannot_see_message() {
  local path="$1"
  if engine_in_container; then
    printf '%s is outside the projects root (%s), the only place the engine can see — install it again with: scripts/engine install --projects-root <a directory that holds it>' \
      "$path" "${DEV_CONTAINER_PROJECTS_ROOT%/}"
  else
    printf '%s cannot be used: this engine is running on the host, not in its container, so it has no projects root to check against. Run the command through scripts/dev-container-thin (or the bin/dev-container that adopt writes into a project), which runs it in the engine container. Install that container once with: scripts/engine install --projects-root <the directory that holds your checkouts>' \
      "$path"
  fi
}

# Files of a checkout's .devcontainer/ that hold secrets and so never reach a
# slot: the credential file project.env names, a key file kept beside it, and
# anything named like an env file or a private key. The patterns are for
# tar --exclude and are relative to the checkout.
devcontainer_secret_patterns() {
  local root="$1" cred="${FORGE_AUTH_FILE:-}" key=""
  printf '%s\n' '.env' '*.env' '.env.*' '*.pem' '*.key'
  cred=${cred#./}
  [[ -z "$cred" ]] || printf '%s\n' "$cred"
  if [[ -n "$cred" && -f "$root/$cred" ]]; then
    key=$(grep '^FORGE_APP_KEY_FILE=' "$root/$cred" 2>/dev/null | tail -1 | cut -d= -f2- || true)
    key=${key#./}
    case "$key" in
      "") ;;
      "$root"/*) printf '%s\n' "${key#"$root"/}" ;;
      /*) ;;
      *) printf '%s\n' "$key" ;;
    esac
  fi
}

# The patterns as one line of --exclude= words, for a shell that cannot take
# an array (the clone step runs sh in the slot's own image).
devcontainer_exclude_words() {
  local pat out=""
  while IFS= read -r pat; do
    [[ -z "$pat" ]] || out+="${out:+ }--exclude=$pat"
  done < <(devcontainer_secret_patterns "$1")
  printf '%s' "$out"
}

# Copies SRC/.devcontainer to DST/.devcontainer without the secrets above.
copy_devcontainer_dir() {
  local src="$1" dst="$2"
  local -a words=()
  read -ra words <<<"$(devcontainer_exclude_words "$src")"
  mkdir -p "$dst"
  tar -C "$src" -cf - "${words[@]}" .devcontainer | tar -C "$dst" -xpf -
}
