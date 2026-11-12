# shellcheck shell=bash disable=SC2034,SC2154

# What a project must have in place before a slot can start, checked the same
# way at every acquire, in doctor and in adopt:
#   - the one credential file project.env names (FORGE_AUTH_FILE): it exists,
#     is readable by its owner only, holds the keys its mode needs, and a key
#     file it points to can be read by the engine container;
#   - the compose file project.env names.
# The credential file belongs to the project owner, who fills it. The engine
# only reads and validates it; it never writes it and never copies it into a slot.
#
# Every check appends to PF_WHAT (what is wrong) and PF_FIX (what to do) and
# goes on, so one run names every problem, not only the first.

FORGE_APP_SETTINGS="FORGE_APP_ID FORGE_APP_INSTALLATION_ID FORGE_APP_SLUG FORGE_APP_BOT_ID FORGE_APP_KEY_FILE FORGE_APP_REPOS"

PF_WHAT=()
PF_FIX=()

preflight_reset() { PF_WHAT=(); PF_FIX=(); }
preflight_problem() { PF_WHAT+=("$1"); PF_FIX+=("$2"); }

# A setting from the credential file: the last line that sets it.
cred_value() {
  local file="$1" key="$2" value
  [[ -f "$file" ]] || return 0
  value=$(grep "^${key}=" "$file" 2>/dev/null | tail -1 | cut -d= -f2- || true)
  printf '%s\n' "$value"
}

cred_mode_of() {
  stat -c %a "$1" 2>/dev/null || stat -f %Lp "$1" 2>/dev/null || true
}

# The path a key setting names: absolute as written, or relative to the checkout.
cred_key_path() {
  case "$2" in
    /*) printf '%s' "$2" ;;
    *) printf '%s/%s' "$1" "${2#./}" ;;
  esac
}

cred_names_for() {
  case "$1" in
    token) printf 'GH_TOKEN' ;;
    app) printf '%s' "$FORGE_APP_SETTINGS" ;;
  esac
}

# credential_problems ROOT MODE FILE — FILE is relative to ROOT. Appends to
# PF_WHAT/PF_FIX; MODE empty means the project asks for no credential.
credential_problems() {
  local root="$1" mode="$2" rel="$3" file key missing="" name stub="" perm keyval path base
  [[ -n "$mode" ]] || return 0
  file="$root/$rel"
  if [[ ! -f "$file" ]]; then
    for name in $(cred_names_for "$mode"); do stub+="${stub:+ }$name"; done
    preflight_problem "FORGE_AUTH=$mode: the credential file $file is missing: $stub" \
      "create it, readable by you only, and fill in $stub — (umask 077; printf '%s=\\n' $stub >> $file). The README beside it, $root/$COMPOSE_DIR/README.md, says what each value is."
    return 0
  fi
  perm=$(cred_mode_of "$file")
  if [[ "$perm" =~ ^[0-7]+$ ]] && (( (8#$perm & 8#077) != 0 )); then
    preflight_problem "FORGE_AUTH=$mode: the credential file $rel has mode $perm, so other accounts on this machine can read it" \
      "chmod 600 $file"
  fi
  for name in $(cred_names_for "$mode"); do
    [[ -n "$(cred_value "$file" "$name")" ]] || missing+="${missing:+ }$name"
  done
  if [[ -n "$missing" ]]; then
    preflight_problem "FORGE_AUTH=$mode: $file is missing: $missing" \
      "add $missing to $rel as NAME=value lines — the README beside it says what each one is."
  fi
  [[ "$mode" == app ]] || return 0
  keyval=$(cred_value "$file" FORGE_APP_KEY_FILE)
  [[ -n "$keyval" ]] || return 0
  path=$(cred_key_path "$root" "$keyval")
  base=$(basename "$path")
  if ! path_is_engine_visible "$path"; then
    preflight_problem "FORGE_APP_KEY_FILE in $rel points at $path, which is $(engine_outside_phrase)" \
      "put the key where the engine can read it — cp $path $root/$COMPOSE_DIR/$base && chmod 600 $root/$COMPOSE_DIR/$base — then set FORGE_APP_KEY_FILE=$COMPOSE_DIR/$base in $rel (that directory is git-ignored and never copied into a slot)."
  elif [[ ! -f "$path" || ! -r "$path" ]]; then
    preflight_problem "FORGE_APP_KEY_FILE in $rel does not name a readable file — the App cannot sign its token request" \
      "set FORGE_APP_KEY_FILE in $rel to the App's private key, a .pem you can read, e.g. FORGE_APP_KEY_FILE=$COMPOSE_DIR/app.pem"
  fi
}

# The sentence the exact message of an invisible path ends with.
engine_outside_phrase() {
  printf 'outside what the engine container can see (it mounts %s)' "$(engine_visible_summary)"
}

# compose_problems ROOT DIR FILES — the compose file(s) project.env names exist.
compose_problems() {
  local plan
  plan=$(adopt_compose_plan "$@")
  case "$plan" in
    ok) ;;
    starter)
      preflight_problem "project.env names $DEFAULT_COMPOSE_DIR/docker-compose.yml, which does not exist" \
        "run $CALLER adopt — it writes the starter stack — or change COMPOSE_DIR and COMPOSE_FILES in project.env to the compose file the project really has."
      ;;
    "missing "*)
      preflight_problem "project.env names ${plan#missing }, which does not exist (COMPOSE_DIR=${2:-$DEFAULT_COMPOSE_DIR} COMPOSE_FILES=${3:-docker-compose.yml})" \
        "create ${plan#missing }, or change COMPOSE_DIR and COMPOSE_FILES in project.env to the compose file the project really has."
      ;;
  esac
}

# Prints each problem, the fix, and the next command to stderr, then exits 1.
preflight_fail() {
  local i
  for (( i = 0; i < ${#PF_WHAT[@]}; i++ )); do
    printf '%s\nFix: %s\n' "${PF_WHAT[i]}" "${PF_FIX[i]}" >&2
  done
  printf 'Next: %s doctor — it checks all of this again and says what is still wrong; then re-run what you were doing.\n' "$CALLER" >&2
  exit 1
}

# Same, without exiting: for adopt, which finishes its writes first.
preflight_report() {
  local i
  for (( i = 0; i < ${#PF_WHAT[@]}; i++ )); do
    printf '%s\nFix: %s\n' "${PF_WHAT[i]}" "${PF_FIX[i]}" >&2
  done
  printf 'Next: %s doctor — it checks all of this again and says what is still wrong.\n' "$CALLER" >&2
}

# A token that GitHub refuses, found by asking it. Network: only when asked for.
# Appends nothing when gh is missing or the token is fine; sets CRED_LIVE_NOTE.
CRED_LIVE_NOTE=""
credential_live_problems() {
  local root="$1" mode="$2" rel="$3" file token out login
  CRED_LIVE_NOTE=""
  [[ "$mode" == token ]] || return 0
  file="$root/$rel"
  token=$(cred_value "$file" GH_TOKEN)
  [[ -n "$token" ]] || return 0
  if ! command -v gh >/dev/null 2>&1; then
    CRED_LIVE_NOTE="gh is not on PATH, so the token in $rel was not tried against GitHub"
    return 0
  fi
  if out=$(GH_TOKEN="$token" gh api user --jq .login 2>&1); then
    login=$(printf '%s' "$out" | head -1)
    CRED_LIVE_NOTE="token in $rel is live (GitHub answers as ${login:-an account})"
    return 0
  fi
  out=${out//"$token"/***}
  preflight_problem "the token in $rel was rejected by GitHub: $(printf '%s' "$out" | head -1)" \
    "replace GH_TOKEN in $rel with a token that is valid and has the access the slots need."
}
