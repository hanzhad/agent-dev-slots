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
# OVERLAY_CEILING, OVERLAY_BUILDS and OVERLAY_CONFIG_JSON; kept for the rest
# of the process for the same slot. The ceiling goes on the services that mount
# /workspace (excluding toolbox sidecars), else on every service.
discover_overlay_services() {
  local proj="$1" json parsed
  [[ "$OVERLAY_CACHE_PROJ" != "$proj" ]] || return 0
  # no token rides along: this call is not the exec that was handed one
  json=$(unset GH_TOKEN; COMPOSE_BASE_ONLY=1 slot_compose "" "$proj" config --format json) \
    || die "refusing to start '$proj' without a memory ceiling — 'docker compose config' on its compose files failed (above), so the services to limit are unknown."
  parsed=$(jq -r '
    (.services // {}) as $s
    | [$s | keys[]] | sort as $all
    | [$s | to_entries[]
        | select((if (.value.labels | type) == "object" then .value.labels["dev-container.role"] else null end) != "toolbox")
        | select(any(.value.volumes[]?; .target == "/workspace"))
        | .key] | sort as $ws
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
  OVERLAY_CONFIG_JSON=$json
  OVERLAY_CACHE_PROJ=$proj
}

OVERLAY_CACHE_PROJ=""
OVERLAY_CACHE_HASH=""
OVERLAY_CACHE_TOOLBOX=""
OVERLAY_CONFIG_JSON=""

# Pure renderer: prints the slot overlay YAML to stdout from a merged base
# compose config JSON, without calling Docker or touching the filesystem.
# Usage: render_slot_overlay_yaml <proj> <config_json> [limit] [hash] [rev]
render_slot_overlay_yaml() {
  local proj="$1" config_json="$2" limit="${3:-}" hash="${4:-}" rev="${5:-}"
  [[ -n "$limit" ]] || limit=$(slot_memory_limit)
  local tb_enabled=0
  [[ -n "${STATE:-}" && -f "$STATE/toolbox/$proj" ]] && tb_enabled=1

  local parsed all ceiling builds primary tb_nets tb_proxy_lines
  parsed=$(jq -r '
    (.services // {}) as $s
    | [$s | keys[]] | sort as $all
    | [$s | to_entries[]
        | select((if (.value.labels | type) == "object" then .value.labels["dev-container.role"] else null end) != "toolbox")
        | select(any(.value.volumes[]?; .target == "/workspace"))
        | .key] | sort as $ws
    | ($ws | if length > 0 then . else $all end) as $ceiling
    | [$s | to_entries[] | select(.value.build != null) | .key] | sort as $builds
    | ($ceiling[0] // "") as $primary
    | ($all | join(" ")),
      ($ceiling | join(" ")),
      ($builds | join(" ")),
      $primary
  ' <<<"$config_json" 2>/dev/null) || parsed=""

  {
    IFS= read -r all || all=""
    IFS= read -r ceiling || ceiling=""
    IFS= read -r builds || builds=""
    IFS= read -r primary || primary=""
  } <<<"$parsed"

  echo "# written by dev-container for $proj; rewritten on every compose call"
  echo "services:"
  local name
  for name in $all; do
    [[ " $ceiling $builds " == *" $name "* ]] || continue
    echo "  $name:"
    if [[ " $ceiling " == *" $name "* ]]; then
      echo "    mem_limit: $limit"
      # Into the container's environment, so docker exec and docker run see
      # it. Read from SLOT_GH_TOKEN, which slot_compose sets from the slot's
      # env file: a plain ${GH_TOKEN} would be won by the caller's own shell.
      # Only with a forge credential: without one a project's own GH_TOKEN
      # is not this overlay's to blank.
      if [[ -n "${FORGE_AUTH:-}" ]]; then
        echo "    environment:"
        echo "      GH_TOKEN: \${SLOT_GH_TOKEN:-}"
      fi
    fi
    if [[ " $builds " == *" $name "* ]]; then
      echo "    build:"
      echo "      labels:"
      echo "        dev-container.compose-project: \${COMPOSE_PROJECT_NAME}"
      echo "        dev-container.build-hash: \"$hash\""
      echo "        org.opencontainers.image.revision: \"$rev\""
    fi
  done

  if (( tb_enabled )); then
    echo "  toolbox:"
    echo "    image: \"\${DEV_CONTAINER_TOOLBOX_IMAGE:-dev-container-toolbox:local}\""
    echo "    init: true"
    echo "    shm_size: 1gb"
    echo "    mem_limit: $limit"
    echo "    user: \"\${CONTAINER_UID:-1000}:\${CONTAINER_GID:-1000}\""
    echo "    working_dir: /workspace"
    echo "    volumes:"
    echo "      - \"\${WORKSPACE_DIR:-..}:/workspace\""
    echo "    labels:"
    echo "      dev-container.role: \"toolbox\""

    if [[ -n "$primary" ]]; then
      tb_nets=$(jq -r --arg p "$primary" '
        (.services[$p].networks // null) as $n
        | if ($n | type) == "object" then ($n | keys)
          elif ($n | type) == "array" then $n
          else []
          end
        | .[]
      ' <<<"$config_json" 2>/dev/null) || tb_nets=""
      if [[ -n "$tb_nets" ]]; then
        echo "    networks:"
        local net
        while IFS= read -r net; do
          [[ -n "$net" ]] || continue
          echo "      - $net"
        done <<<"$tb_nets"
      fi

      tb_proxy_lines=$(jq -r --arg p "$primary" '
        def env_map:
          if type == "object" then with_entries(.value = (if .value == null then "" else (.value | tostring) end))
          elif type == "array" then
            map(capture("^(?<k>[^=]+)=(?<v>.*)$") | {(.k): .v}) | add // {}
          else {}
          end;
        def add_host($raw; $host):
          (($raw // "") | split(",") | map(gsub("^\\s+|\\s+$"; "")) | map(select(length > 0))) as $items
          | if ($items | index($host)) != null then $items else ($items + [$host]) end
          | join(",");
        ((.services[$p].environment // {}) | env_map) as $e
        | (["HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "NO_PROXY", "no_proxy"]
           | any($e[.] != null and ($e[.] | length) > 0)) as $has_proxy
        | if $has_proxy then
            (["HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"][] | select($e[.] != null and ($e[.] | length) > 0) | "      \(.): \($e[.] | @json)"),
            "      NO_PROXY: \(add_host(($e.NO_PROXY // $e.no_proxy // ""); $p) | @json)",
            "      no_proxy: \(add_host(($e.no_proxy // $e.NO_PROXY // ""); $p) | @json)"
          else empty
          end
      ' <<<"$config_json" 2>/dev/null) || tb_proxy_lines=""
      if [[ -n "$tb_proxy_lines" ]]; then
        echo "    environment:"
        printf '%s\n' "$tb_proxy_lines"
      fi
    fi
  fi
}

# Writes the overlay for a slot and sets SLOT_OVERLAY_PATH to it.
write_slot_overlay() {
  local proj="$1" file tmp limit hash rev tb_flag=0
  file=$(slot_overlay_of "$proj")
  hash=$(slot_build_hash 2>/dev/null) || hash=""
  [[ "$hash" =~ ^[0-9a-f]+$ ]] || hash=""
  rev=$(slot_build_revision 2>/dev/null) || rev=""
  [[ "$rev" =~ ^[0-9a-f]+$ ]] || rev=""
  [[ -n "${STATE:-}" && -f "$STATE/toolbox/$proj" ]] && tb_flag=1
  if [[ "$OVERLAY_CACHE_PROJ" == "$proj" && "$OVERLAY_CACHE_HASH" == "$hash" && "$OVERLAY_CACHE_TOOLBOX" == "$tb_flag" && -f "$file" ]]; then
    SLOT_OVERLAY_PATH=$file
    return 0
  fi
  discover_overlay_services "$proj"
  limit=$(slot_memory_limit)
  mkdir -p "$STATE/overlays"
  tmp=$(mktemp "$STATE/overlays/.tmp.XXXXXX")
  render_slot_overlay_yaml "$proj" "$OVERLAY_CONFIG_JSON" "$limit" "$hash" "$rev" >"$tmp"
  mv "$tmp" "$file"
  OVERLAY_CACHE_HASH=$hash
  OVERLAY_CACHE_TOOLBOX=$tb_flag
  SLOT_OVERLAY_PATH=$file
}
