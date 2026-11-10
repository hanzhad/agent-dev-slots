# shellcheck shell=bash disable=SC2154

# `migrate-identity`: moves a project's per-machine state from the names it was
# kept under (the label) to the names a project is known by now (the key, see
# identity.sh). Three kinds move, each to the same name with the key in place of
# the label:
#   tokens/<label>*   ->  tokens/<key>*     (same suffix: .lock, .XXXXXX)
#   signing/<label>/  ->  signing/<key>/
#   rescue/<label>/   ->  rescue/<key>/
# It runs once per project, after an upgrade and before any acquire. It never
# overwrites: a destination that exists while its source exists stops it before
# anything moves. A live lease for the label stops it too, because a running
# slot still reads the old names.

# Prints the plan as "<kind> <source> <destination>" lines for LABEL and KEY.
migrate_identity_plan() {
  local state="$1" label="$2" key="$3" f suffix kind
  for f in "$state/tokens/$label"*; do
    [[ -e "$f" || -L "$f" ]] || continue
    suffix=${f#"$state/tokens/$label"}
    # tokens/<label>.lock is this project's; tokens/<label>_<key> is another one's.
    [[ -z "$suffix" || "$suffix" == .* ]] || continue
    printf 'tokens %s %s\n' "$f" "$state/tokens/$key$suffix"
  done
  for kind in signing rescue; do
    [[ -e "$state/$kind/$label" || -L "$state/$kind/$label" ]] || continue
    printf '%s %s %s\n' "$kind" "$state/$kind/$label" "$state/$kind/$key"
  done
}

# Lease files that name LABEL's slots, one per line.
migrate_identity_leases() {
  local state="$1" label="$2" f
  for f in "$state/leases/$label"-slot-*.json; do
    [[ -f "$f" ]] && printf '%s\n' "$f"
  done
  return 0
}

cmd_migrate_identity() {
  local dry=0 arg kind src dst leases clash=""
  local -a plan=()
  for arg in "$@"; do
    case "$arg" in
      --dry-run) dry=1 ;;
      *) die "migrate-identity: unknown argument: $arg (usage: $CALLER migrate-identity [--dry-run])" ;;
    esac
  done
  [[ -n "${PROJECT_KEY:-}" && -n "${PROJECT_LABEL:-}" ]] || die "migrate-identity: the project's identity is not resolved"

  leases=$(migrate_identity_leases "$STATE" "$PROJECT_LABEL")
  if [[ -n "$leases" ]]; then
    echo "migrate-identity: $PROJECT_LABEL still holds a lease, so a slot is running on the old names. Release its slots first:" >&2
    printf '  %s\n' "$leases" >&2
    exit 1
  fi

  mapfile -t plan < <(migrate_identity_plan "$STATE" "$PROJECT_LABEL" "$PROJECT_KEY")
  if (( ${#plan[@]} == 0 )) || [[ "$PROJECT_LABEL" == "$PROJECT_KEY" ]]; then
    echo "nothing to migrate for $PROJECT_LABEL ($PROJECT_KEY)"
    exit 0
  fi

  for arg in "${plan[@]}"; do
    read -r kind src dst <<<"$arg"
    if [[ -e "$dst" || -L "$dst" ]]; then
      clash+="  $kind: $dst exists while $src exists"$'\n'
    fi
  done
  if [[ -n "$clash" ]]; then
    printf 'migrate-identity: refusing to overwrite; nothing was moved.\n%s' "$clash" >&2
    exit 1
  fi

  for arg in "${plan[@]}"; do
    read -r kind src dst <<<"$arg"
    if (( dry )); then
      echo "would move ($kind, key $PROJECT_KEY): $src -> $dst"
    else
      mv -- "$src" "$dst" || die "migrate-identity: could not move $src to $dst"
      echo "moved ($kind, key $PROJECT_KEY): $src -> $dst"
    fi
  done
  exit 0
}
