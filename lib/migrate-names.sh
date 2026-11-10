# shellcheck shell=bash disable=SC2034,SC2154

# `migrate-names`: rewrites the first names of the forge credential settings to
# the names they became, in project.env and in the credential file it names.
# Only names are rewritten and only names are printed; a value is copied
# through as it stands and never reaches the output.

MIGRATE_PROJECT_KEYS="AGENT_AUTH=FORGE_AUTH AGENT_AUTH_ENV_FILE=FORGE_AUTH_FILE"
MIGRATE_APP_KEYS="AGENT_APP_ID=FORGE_APP_ID AGENT_APP_INSTALLATION_ID=FORGE_APP_INSTALLATION_ID AGENT_APP_SLUG=FORGE_APP_SLUG AGENT_APP_BOT_ID=FORGE_APP_BOT_ID AGENT_APP_KEY_FILE=FORGE_APP_KEY_FILE AGENT_APP_REPOS=FORGE_APP_REPOS"
MIGRATE_MODES="container-env=token github-app=app"

MIGRATE_CHANGES=()
MIGRATE_AUTH_FILE=""

# The names the settings had. The reader refuses them by these, and
# migrate-names rewrites them by the same table.
migrate_renamed_key() {
  local pair
  for pair in $MIGRATE_PROJECT_KEYS $MIGRATE_APP_KEYS; do
    if [[ "${pair%%=*}" == "$1" ]]; then printf '%s' "${pair#*=}"; return 0; fi
  done
  return 1
}

migrate_renamed_mode() {
  local pair
  for pair in $MIGRATE_MODES; do
    if [[ "${pair%%=*}" == "$1" ]]; then printf '%s' "${pair#*=}"; return 0; fi
  done
  return 1
}

migrate_has_key() { grep -q "^${2}=" "$1"; }

# Refuses a setting given under both names, whichever comes first.
migrate_check_pairs() {
  local file="$1" label="$2" pairs="$3" pair old new
  for pair in $pairs; do
    old=${pair%%=*} new=${pair#*=}
    if migrate_has_key "$file" "$old" && migrate_has_key "$file" "$new"; then
      die "$label: $new and $old both set the same thing — keep $new and remove $old."
    fi
  done
}

# migrate_rewrite <file> <out> <pairs> <label>: copies <file> to <out> with the
# names in <pairs> (old=new) replaced at the start of a KEY=value line, and
# FORGE_AUTH's old mode names replaced in its value. Appends one entry per
# replacement to MIGRATE_CHANGES, and records the credential file's path.
migrate_rewrite() {
  local file="$1" out="$2" pairs="$3" label="$4"
  local line eol key rest pair old new cr q inner mode seen=" " note
  : >"$out"
  while true; do
    if IFS= read -r line; then
      eol=$'\n'
    else
      [[ -n "$line" ]] || break
      eol=""
    fi
    if [[ "$line" =~ ^([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]]; then
      key=${BASH_REMATCH[1]} rest=${BASH_REMATCH[2]}
      for pair in $pairs; do
        old=${pair%%=*} new=${pair#*=}
        if [[ "$key" == "$old" ]]; then
          key=$new
          note="$old -> $new"
          case "$seen" in *" $note "*) ;; *) seen+="$note "; MIGRATE_CHANGES+=("$label|  $note") ;; esac
        fi
      done
      cr=""
      if [[ "$rest" == *$'\r' ]]; then cr=$'\r'; rest=${rest%$'\r'}; fi
      q="" inner=$rest
      if [[ ${#rest} -ge 2 && ( "${rest:0:1}" == '"' || "${rest:0:1}" == "'" ) && "${rest: -1}" == "${rest:0:1}" ]]; then
        q=${rest:0:1} inner=${rest:1:${#rest}-2}
      fi
      if [[ "$key" == FORGE_AUTH ]]; then
        for mode in $MIGRATE_MODES; do
          if [[ "$inner" == "${mode%%=*}" ]]; then
            note="${mode%%=*} -> ${mode#*=}"
            MIGRATE_CHANGES+=("$label|  FORGE_AUTH mode $note")
            inner=${mode#*=}
            rest=$q$inner$q
          fi
        done
      fi
      [[ "$key" != FORGE_AUTH_FILE ]] || MIGRATE_AUTH_FILE=$inner
      line="$key=$rest$cr"
    fi
    printf '%s%s' "$line" "$eol" >>"$out"
  done <"$file"
}

migrate_resolve_root() {
  local target="$1" common_dir root
  if [[ -z "$target" ]]; then
    target=${DEV_CONTAINER_CALLER_PWD:-}
    [[ -n "$target" ]] || die "migrate-names was not given a checkout directory and DEV_CONTAINER_CALLER_PWD is not set"
  elif [[ "$target" != /* && -n "${DEV_CONTAINER_CALLER_PWD:-}" ]]; then
    target="${DEV_CONTAINER_CALLER_PWD%/}/$target"
  fi
  [[ -d "$target" ]] || die "migrate-names: $target is not a directory"
  target=$(CDPATH= cd -P -- "$target" 2>/dev/null && pwd) || die "migrate-names: $target is not a directory"
  outside_root "$target"
  common_dir=$(git -C "$target" rev-parse --path-format=absolute --git-common-dir 2>/dev/null) \
    || die "migrate-names: $target is not inside a git checkout"
  root=$(dirname "$common_dir")
  outside_root "$root" strict
  printf '%s\n' "$root"
}

migrate_print() {
  local label="$1" entry shown=0
  for entry in "${MIGRATE_CHANGES[@]}"; do
    [[ "${entry%%|*}" == "$label" ]] || continue
    if (( ! shown )); then echo "$label:"; shown=1; fi
    echo "${entry#*|}"
  done
}

cmd_migrate_names() {
  local target="" dry=0 arg root project cred_rel cred tmp
  for arg in "$@"; do
    case "$arg" in
      --dry-run) dry=1 ;;
      -*) die "migrate-names: unknown flag: $arg" ;;
      *)
        [[ -z "$target" ]] || die "migrate-names takes at most one checkout directory — got $target and $arg"
        target=$arg ;;
    esac
  done
  root=$(migrate_resolve_root "$target")
  project="$root/project.env"
  [[ -f "$project" ]] || die "$root has no project.env — it is not a connected project."

  tmp=$(umask 077; mktemp -d) || die "migrate-names: could not create a temporary directory"
  trap 'rm -rf "$tmp"' EXIT
  MIGRATE_CHANGES=()
  MIGRATE_AUTH_FILE=""

  migrate_check_pairs "$project" "$project" "$MIGRATE_PROJECT_KEYS"
  migrate_rewrite "$project" "$tmp/project.env" "$MIGRATE_PROJECT_KEYS" project.env
  cred_rel=$MIGRATE_AUTH_FILE
  local cred_new="" cred_label=""
  if [[ -n "$cred_rel" ]]; then
    check_relative_paths "$project" FORGE_AUTH_FILE "$cred_rel"
    cred="$root/$cred_rel"
    cred_label=$cred_rel
    if [[ -f "$cred" ]]; then
      migrate_check_pairs "$cred" "$cred_label" "$MIGRATE_APP_KEYS"
      migrate_rewrite "$cred" "$tmp/credentials" "$MIGRATE_APP_KEYS" "$cred_label"
      cred_new=$tmp/credentials
    else
      echo "credential file $cred_rel not found — skipped"
    fi
  fi

  if (( ${#MIGRATE_CHANGES[@]} == 0 )); then
    echo "nothing to rename: project.env and its credential file already use the FORGE_* names"
    exit 0
  fi

  (( dry )) && echo "dry run — nothing is written"
  migrate_print project.env
  [[ -z "$cred_label" ]] || migrate_print "$cred_label"
  if (( dry )); then
    echo "dry run: ${#MIGRATE_CHANGES[@]} names would be renamed"
    exit 0
  fi

  # Both files are checked before either is touched.
  [[ -w "$project" ]] || die "$project is not writable"
  if [[ -n "$cred_new" ]] && ! cmp -s "$cred" "$cred_new"; then
    [[ -w "$cred" ]] || die "$cred is not writable"
  fi
  # Written in place, so the file keeps its mode, owner and any link it is behind.
  if [[ -n "$cred_new" ]] && ! cmp -s "$cred" "$cred_new"; then cat "$cred_new" >"$cred"; fi
  if ! cmp -s "$project" "$tmp/project.env"; then cat "$tmp/project.env" >"$project"; fi
  echo "renamed ${#MIGRATE_CHANGES[@]} names"
  exit 0
}
