# shellcheck shell=bash disable=SC2034,SC2154

# The contract a project's compose files keep with the engine, checked at every
# acquire and by doctor. The project's files are the owner's; a failure names
# the file and line, prints the fix, and changes nothing:
#   - a service named app exists (and LIVE_SERVICE, when project.env names one);
#   - a host port comes from SLOT_PORTS, written ${NAME}, never a number;
#   - no secret is written into the file;
#   - ${WORKSPACE_DIR} is a host path only in the app service.
# Only a file with a top-level `services:` is judged: anything else is not a
# compose stack. Secret values are never printed, only the line and the name.

CONTRACT_SERVICE=app

# The service names a compose file defines, one per line.
contract_services() {
  awk '
    /^services:[ \t]*$/ { ins = 1; ind = -1; next }
    ins && /^[^ \t#]/ { ins = 0 }
    ins && /^[ \t]*#/ { next }
    ins && /^[ \t]*$/ { next }
    ins {
      match($0, /^ */); n = RLENGTH
      if (ind < 0) ind = n
      if (n == ind && match($0, /^ *[A-Za-z0-9_.-]+:/)) {
        s = $0; sub(/^ */, "", s); sub(/:.*/, "", s); print s
      }
    }' "$1"
}

# "LINE<TAB>ITEM" for each entry of a ports: list; the long syntax's published
# value comes as "LINE<TAB>H:VALUE".
contract_port_items() {
  awk '
    function indent(s) { match(s, /^ */); return RLENGTH }
    /^[ \t]*#/ { next }
    {
      if (inp && $0 !~ /^[ \t]*$/) {
        n = indent($0)
        if (n < pind || (n == pind && $0 !~ /^ *-/)) inp = 0
      }
    }
    /^ *ports:[ \t]*$/ { inp = 1; pind = indent($0); next }
    inp && /^ *-[ \t]/ {
      item = $0; sub(/^ *-[ \t]*/, "", item); sub(/[ \t]+#.*$/, "", item); gsub(/["\047]/, "", item)
      if (item ~ /^[A-Za-z_]+:[ \t]/) next
      print NR "\t" item; next
    }
    inp && /^ *published:/ {
      v = $0; sub(/^ *published:[ \t]*/, "", v); gsub(/["\047]/, "", v); print NR "\tH:" v
    }' "$1"
}

# The host part of a ports entry, or nothing for a container-only port.
contract_host_of() {
  local item="$1" rest
  if [[ "$item" == H:* ]]; then
    printf '%s' "${item#H:}"
    return 0
  fi
  item=${item%%/*}
  rest=$item
  if [[ "$rest" =~ ^(\[[0-9a-fA-F:]+\]|[0-9]+(\.[0-9]+){3}): ]]; then
    rest=${rest#"${BASH_REMATCH[0]}"}
  fi
  if [[ "$rest" == '${'* ]]; then
    printf '%s' "${rest%%\}*}}"
  elif [[ "$rest" == *:* ]]; then
    printf '%s' "${rest%%:*}"
  fi
  return 0
}

# "LINE<TAB>WHAT" for each secret written into a compose file.
contract_secret_lines() {
  grep -nE '(ghp|gho|ghs|ghu|ghr)_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}' "$1" 2>/dev/null |
    grep -vE '^[0-9]+:[[:space:]]*#' | cut -d: -f1 | sed 's/$/\ta GitHub token/' || true
  grep -nE -e '-----BEGIN [A-Z ]*PRIVATE KEY-----' "$1" 2>/dev/null | cut -d: -f1 | sed 's/$/\ta private key/' || true
  awk '
    /^[ \t]*#/ { next }
    match($0, /^[ \t]*(-[ \t]*)?[A-Za-z0-9_]+[ \t]*[:=][ \t]*[^ \t]/) {
      k = $0; sub(/^[ \t]*(-[ \t]*)?/, "", k)
      name = k; sub(/[ \t]*[:=].*$/, "", name)
      v = k; sub(/^[^:=]*[:=][ \t]*/, "", v)
      sub(/[ \t]+#.*$/, "", v); gsub(/^["\047]|["\047][ \t]*$/, "", v)
      u = toupper(name)
      if (u ~ /(TOKEN|SECRET|PASSWORD|PASSWD|API_KEY|PRIVATE_KEY)/ && u !~ /_(FILE|PATH)$/ \
          && v !~ /^\$/ && length(v) >= 20 && v !~ /[ \t]/) print NR "\tthe value of " name
    }' "$1"
}

# "LINE<TAB>SERVICE" for each volume outside the app service whose host path
# starts with ${WORKSPACE_DIR}: a short-form source or a long-form bind source.
contract_workspace_uses() {
  awk -v app="$CONTRACT_SERVICE" '
    function indent(s) { match(s, /^ */); return RLENGTH }
    /^services:[ \t]*$/ { ins = 1; ind = -1; next }
    ins && /^[^ \t#]/ { ins = 0 }
    !ins || /^[ \t]*#/ || /^[ \t]*$/ { next }
    {
      n = indent($0)
      if (ind < 0) ind = n
      if (n == ind && match($0, /^ *[A-Za-z0-9_.-]+:/)) {
        svc = $0; sub(/^ */, "", svc); sub(/:.*/, "", svc); inv = 0; next
      }
      if (inv && (n < vind || (n == vind && $0 !~ /^ *-/))) inv = 0
      if ($0 ~ /^ *volumes:[ \t]*$/) { inv = 1; vind = n; next }
      if (!inv || svc == app) next
      v = $0; sub(/^ *(-[ \t]*)?/, "", v)
      if (v ~ /^source:/) sub(/^source:[ \t]*/, "", v)
      else if (v ~ /^[A-Za-z_]+:[ \t]/) next
      gsub(/^["\047]/, "", v)
      if (v ~ /^\$\{WORKSPACE_DIR[:}-]/) print NR "\t" svc
    }' "$1"
}

# contract_problems ROOT DIR FILES PORTS LIVE_SERVICE — appends to PF_WHAT/PF_FIX.
contract_problems() {
  local root="$1" dir="${2:-.devcontainer}" files="${3:-docker-compose.yml}" ports="${4:-}" live="${5:-}"
  local base f rel line item host name what services="" judged="" first=""
  dir=${dir%/}
  [[ -n "$dir" ]] || dir=.
  base="$root"
  [[ "$dir" == . ]] || base="$root/$dir"
  for f in $files; do
    [[ -f "$base/$f" ]] || continue
    grep -Eq '^services:[[:space:]]*$' "$base/$f" || continue
    rel="$f"
    [[ "$dir" == . ]] || rel="$dir/$f"
    judged=1
    [[ -n "$first" ]] || first="$rel"
    services+=" $(contract_services "$base/$f" | tr '\n' ' ')"
    while IFS=$'\t' read -r line item; do
      [[ -n "$line" ]] || continue
      host=$(contract_host_of "$item")
      [[ -n "$host" ]] || continue
      if [[ "$host" =~ ^\$\{([A-Za-z_][A-Za-z0-9_]*) ]]; then
        name=${BASH_REMATCH[1]}
        case " $ports " in
          *" $name "*) ;;
          *)
            preflight_problem "$rel:$line publishes a host port from \${$name}, which SLOT_PORTS in project.env does not name" \
              "add $name to SLOT_PORTS in project.env (the engine then gives each slot its own port), or publish a variable SLOT_PORTS already names: ${ports:-none}."
            ;;
        esac
      else
        preflight_problem "$rel:$line publishes the fixed host port $host, but host ports come from SLOT_PORTS" \
          "write the host side as \${NAME} with NAME listed in SLOT_PORTS in project.env (SLOT_PORTS is now: ${ports:-none}) — a fixed number makes two slots fight over it."
      fi
    done < <(contract_port_items "$base/$f")
    while IFS=$'\t' read -r line what; do
      [[ -n "$line" ]] || continue
      preflight_problem "$rel:$line writes a secret into the compose file ($what)" \
        "take the value out of $rel: pass it by name or as \${NAME}, and keep the value in the credential file ($(dirname "${FORGE_AUTH_FILE:-.devcontainer/agent.env}")/), which is never committed or copied into a slot."
    done < <(contract_secret_lines "$base/$f")
    while IFS=$'\t' read -r line name; do
      [[ -n "$line" ]] || continue
      preflight_problem "$rel:$line uses \${WORKSPACE_DIR} as a host path in the $name service" \
        "\${WORKSPACE_DIR} is the slot's path on the host and only the $CONTRACT_SERVICE service may mount it: remove the mount from $name, or mount a path of its own."
    done < <(contract_workspace_uses "$base/$f")
  done
  [[ -n "$judged" ]] || return 0
  for name in $CONTRACT_SERVICE $live; do
    case " $services " in
      *" $name "*) ;;
      *)
        what="no compose file project.env names defines a service called $name (COMPOSE_FILES=$files in ${dir})"
        preflight_problem "$what" \
          "name a service $name in $first (or in another of COMPOSE_FILES)$([[ "$name" == "$CONTRACT_SERVICE" ]] && echo ' — the engine starts and reaches the slot through it' || echo ", or correct LIVE_SERVICE in project.env")."
        ;;
    esac
  done
}

# Whether a compose file mounts ${WORKSPACE_DIR} (any form) and whether it
# declares a top-level slot-workspace volume: prints "mounts" and/or "declares".
contract_workspace_facts() {
  awk '
    /^[ \t]*#/ { next }
    /WORKSPACE_DIR/ { mounts = 1 }
    /^volumes:[ \t]*$/ { inv = 1; next }
    /^[^ \t#]/ { inv = 0 }
    inv && /^[ \t]+slot-workspace:/ { declares = 1 }
    END { if (mounts) print "mounts"; if (declares) print "declares" }' "$1"
}

# workspace_volume_warning ROOT DIR FILES PROJECT — prints a warning, nothing when
# the files are fine. Not a contract failure: `run` adds its own overlay, so an
# acquire works; only `install` runs the project script without it.
workspace_volume_warning() {
  local root="$1" dir="${2:-.devcontainer}" files="${3:-docker-compose.yml}" project="${4:-project}"
  local base f rel facts mounting="" declared=""
  dir=${dir%/}
  [[ -n "$dir" ]] || dir=.
  base="$root"
  [[ "$dir" == . ]] || base="$root/$dir"
  for f in $files; do
    [[ -f "$base/$f" ]] || continue
    grep -Eq '^services:[[:space:]]*$' "$base/$f" || continue
    rel="$f"
    [[ "$dir" == . ]] || rel="$dir/$f"
    facts=$(contract_workspace_facts "$base/$f")
    case $'\n'"$facts"$'\n' in *$'\nmounts\n'*) [[ -n "$mounting" ]] || mounting="$rel" ;; esac
    case $'\n'"$facts"$'\n' in *$'\ndeclares\n'*) declared=1 ;; esac
  done
  [[ -n "$mounting" && -z "$declared" ]] || return 0
  echo "$mounting mounts \${WORKSPACE_DIR} without declaring a slot-workspace volume: 'run' works (the engine adds its own overlay), but 'install' fails with: service \"app\" refers to undefined volume slot-workspace. Add to $mounting:"
  echo "  volumes:"
  echo "    slot-workspace:"
  echo "      external: true"
  echo "      name: \${SLOT_WORKSPACE_VOLUME:-${project}-workspace}"
}
