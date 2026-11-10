# shellcheck shell=bash disable=SC2154

# What adopt does about the launcher: it writes none. A project carries no engine
# launcher; the global `dev-container` command (scripts/dev-container-global) is it,
# and it finds the project from any directory inside it. A launcher an older adopt
# wrote is left exactly as it is.
#
# The global command tells adopt, in DEV_CONTAINER_CLI_STATE / _CLONE, whether it is on
# PATH and which engine clone it runs from (see doctor_command). adopt then
#   - prints the one install command when the command is not on PATH, and
#   - seeds .devcontainer/start.sh, a script the project owns from then on: it installs
#     the command from that clone when it is missing, then runs it.

# The text of .devcontainer/start.sh for the engine clone $1. POSIX sh.
adopt_start_text() {
  local clone="$1"
  cat <<START
#!/bin/sh
# Runs dev-container for this project. If the command is not on PATH it is installed
# first, from the engine clone it was seeded with. Written once by 'dev-container adopt';
# from then on this file is the project's own, and adopt never rewrites it.
set -eu
if ! command -v dev-container >/dev/null 2>&1; then
  '$clone/scripts/install-cli.sh' || { echo "start.sh: installing the dev-container command failed" >&2; exit 1; }
  command -v dev-container >/dev/null 2>&1 || PATH="\$HOME/.local/bin:\$PATH"
  export PATH
fi
exec dev-container "\$@"
START
}

adopt_seed_start_script() {
  local root="$1" compose_dir="$2" clone="${DEV_CONTAINER_CLI_CLONE:-}" dir path
  [[ "$clone" == /* && "$clone" != *"'"* ]] || return 0
  [[ -n "$compose_dir" && "$compose_dir" != "." ]] || return 0
  dir="$root/${compose_dir%/}"
  [[ -d "$dir" ]] || return 0
  path="$dir/start.sh"
  [[ ! -e "$path" && ! -L "$path" ]] || return 0
  adopt_start_text "$clone" >"$path.tmp.$$"
  chmod 0755 "$path.tmp.$$"
  mv "$path.tmp.$$" "$path"
}

adopt_install_hint() {
  local state="${DEV_CONTAINER_CLI_STATE:-}" clone="${DEV_CONTAINER_CLI_CLONE:-}" cmd="scripts/install-cli.sh"
  case "$state" in
    path | windows-shim) return 0 ;;
  esac
  [[ -z "$clone" ]] || cmd="$clone/scripts/install-cli.sh"
  echo "note: the dev-container command is not on PATH here, and a project carries no launcher. Install it once (no root, no profile edits): $cmd" >&2
}

# Called where adopt used to write the launcher.
adopt_launcher_notes() {
  adopt_seed_start_script "$1" "$2"
  adopt_install_hint
}
