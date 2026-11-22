# shellcheck shell=bash disable=SC2034,SC2154

toolbox_image_ref() {
  printf '%s\n' "${DEV_CONTAINER_TOOLBOX_IMAGE:-dev-container-toolbox:local}"
}

toolbox_marker_of() {
  printf '%s\n' "$STATE/toolbox/$1"
}

toolbox_enabled() {
  [[ -n "${STATE:-}" && -f "$(toolbox_marker_of "$1")" ]]
}

toolbox_enable() {
  mkdir -p "$STATE/toolbox"
  printf '1\n' >"$(toolbox_marker_of "$1")"
}

toolbox_clear() {
  [[ -n "${STATE:-}" ]] || return 0
  rm -f "$(toolbox_marker_of "$1")"
}

toolbox_context_dir() {
  local self_dir root
  self_dir=$(CDPATH= cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd)
  root=$(CDPATH= cd -P "$self_dir/.." && pwd)
  if [[ -d "$root/toolbox" && -f "$root/toolbox/Dockerfile" ]]; then
    printf '%s\n' "$root/toolbox"
  elif [[ -d "$root/image/toolbox" && -f "$root/image/toolbox/Dockerfile" ]]; then
    printf '%s\n' "$root/image/toolbox"
  else
    return 1
  fi
}

ensure_toolbox_image() {
  local image ctx
  image=$(toolbox_image_ref)
  if docker image inspect "$image" >/dev/null 2>&1; then
    return 0
  fi
  ctx=$(toolbox_context_dir) || {
    echo "toolbox Dockerfile directory not found" >&2
    return 1
  }
  echo "▸ building toolbox image $image from $ctx" >&2
  docker build -t "$image" "$ctx"
}
