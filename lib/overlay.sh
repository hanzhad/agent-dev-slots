# shellcheck shell=bash disable=SC2034,SC2154

# The overlay the engine adds to every slot's compose files, last, so nothing
# a project sets can lift it. It sets a memory ceiling on the slot's app
# service (the one that mounts /workspace) and, on each service that builds an image, stamps the labels the
# engine reads the image back by. It lives in the state directory: the main
# checkout's tracked files are never written.

SLOT_MEMORY_LIMIT_DEFAULT=4g

slot_memory_limit() { echo "${PE_SLOT_MEMORY_LIMIT:-$SLOT_MEMORY_LIMIT_DEFAULT}"; }

slot_memory_limit_source() {
  if [[ -n "${PE_SLOT_MEMORY_LIMIT:-}" ]]; then echo "project file"; else echo "default"; fi
}

slot_overlay_of() { echo "$STATE/overlays/$1.yml"; }

# The services of the merged config, asked of compose without this overlay
# (it does not exist yet, and must not feed itself). Sets OVERLAY_ALL,
# OVERLAY_CEILING and OVERLAY_BUILDS, space-joined; kept for the rest of the
# process for the same slot. The ceiling goes on the services that mount
# /workspace, else on every service. Only a service in the config is named.
discover_overlay_services() {
  local proj="$1" json parsed
  [[ "$OVERLAY_CACHE_PROJ" != "$proj" ]] || return 0
  # no token rides along: this call is not the exec that was handed one
  json=$(unset GH_TOKEN; COMPOSE_BASE_ONLY=1 slot_compose "" "$proj" config --format json) \
    || die "refusing to start '$proj' without a memory ceiling — 'docker compose config' on its compose files failed (above), so the services to limit are unknown."
  parsed=$(jq -r '
    (.services // {}) as $s
    | [$s | keys[]] | sort as $all
    | [$s | to_entries[] | select(any(.value.volumes[]?; .target == "/workspace")) | .key] | sort as $ws
    | [$s | to_entries[] | select(.value.build != null) | .key] | sort as $builds
    | ($all | join(" ")), (($ws | if length > 0 then . else $all end) | join(" ")), ($builds | join(" "))
  ' <<<"$json" 2>/dev/null) || parsed=""
  {
    IFS= read -r OVERLAY_ALL || OVERLAY_ALL=""
    IFS= read -r OVERLAY_CEILING || OVERLAY_CEILING=""
    IFS= read -r OVERLAY_BUILDS || OVERLAY_BUILDS=""
  } <<<"$parsed"
  [[ -n "$OVERLAY_ALL" ]] \
    || die "refusing to start '$proj' without a memory ceiling — its compose config names no service."
  OVERLAY_CACHE_PROJ=$proj
}

OVERLAY_CACHE_PROJ=""
OVERLAY_CACHE_HASH=""

# Writes the overlay for a slot and sets SLOT_OVERLAY_PATH to it.
write_slot_overlay() {
  local proj="$1" file tmp limit name hash rev
  file=$(slot_overlay_of "$proj")
  hash=$(slot_build_hash 2>/dev/null) || hash=""
  [[ "$hash" =~ ^[0-9a-f]+$ ]] || hash=""
  rev=$(slot_build_revision 2>/dev/null) || rev=""
  [[ "$rev" =~ ^[0-9a-f]+$ ]] || rev=""
  if [[ "$OVERLAY_CACHE_PROJ" == "$proj" && "$OVERLAY_CACHE_HASH" == "$hash" && -f "$file" ]]; then
    SLOT_OVERLAY_PATH=$file
    return 0
  fi
  discover_overlay_services "$proj"
  limit=$(slot_memory_limit)
  mkdir -p "$STATE/overlays"
  tmp=$(mktemp "$STATE/overlays/.tmp.XXXXXX")
  {
    echo "# written by dev-container for $proj; rewritten on every compose call"
    echo "services:"
    for name in $OVERLAY_ALL; do
      [[ " $OVERLAY_CEILING $OVERLAY_BUILDS " == *" $name "* ]] || continue
      echo "  $name:"
      if [[ " $OVERLAY_CEILING " == *" $name "* ]]; then
        echo "    mem_limit: $limit"
        # Into the container's environment, so docker exec and docker run see
        # it. Read from SLOT_GH_TOKEN, which slot_compose sets from the slot's
        # env file: a plain ${GH_TOKEN} would be won by the caller's own shell.
        # Only with a forge credential: without one a project's own GH_TOKEN
        # is not this overlay's to blank.
        if [[ -n "$FORGE_AUTH" ]]; then
          echo "    environment:"
          echo "      GH_TOKEN: \${SLOT_GH_TOKEN:-}"
        fi
      fi
      if [[ " $OVERLAY_BUILDS " == *" $name "* ]]; then
        echo "    build:"
        echo "      labels:"
        echo "        dev-container.compose-project: \${COMPOSE_PROJECT_NAME}"
        echo "        dev-container.build-hash: \"$hash\""
        echo "        org.opencontainers.image.revision: \"$rev\""
      fi
    done
  } >"$tmp"
  mv "$tmp" "$file"
  OVERLAY_CACHE_HASH=$hash
  SLOT_OVERLAY_PATH=$file
}
