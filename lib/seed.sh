# shellcheck shell=bash disable=SC2034,SC2154

# The starter files `adopt` leaves in a project's .devcontainer/: the compose
# file, the image that runs sshd, its entrypoint and a credential-file template.
# They come from the engine's templates/ directory and are SEEDED ONCE: a file
# that exists is the project's and is never touched, whatever the template says
# now; a missing one is added. The template version is stamped in the first
# lines (`# dev-container-engine template: NAME version: N`) as information:
# `doctor` says when the template has moved on and prints the diff command.
# Nothing here holds a secret.

# The version a file was seeded from: the stamp in its first lines, or empty.
seed_stamp_version() {
  head -n 3 "$1" 2>/dev/null | sed -n 's/^.*dev-container-engine template: [A-Za-z.-]* version: \([0-9][0-9]*\).*$/\1/p' | head -n 1
}

# Seeds DST from TEMPLATE unless DST exists. Placeholders @BASE_IMAGE@ is the
# only one, replaced with $3. MODE is the file mode ($4, default 0644).
seed_file() {
  local dst="$1" template="$2" base="${3:-}" mode="${4:-0644}" version body
  [[ ! -e "$dst" ]] || return 0
  [[ -f "$template" ]] || die "adopt: the engine has no template $template — the engine installation is incomplete."
  version=$(seed_stamp_version "$template")
  [[ -n "$version" ]] || die "adopt: $template carries no template version stamp."
  mkdir -p "$(dirname "$dst")"
  body=$(cat "$template"; printf x)
  body=${body%x}
  body=${body//@BASE_IMAGE@/"$base"}
  printf '%s' "$body" >"$dst.tmp.$$"
  chmod "$mode" "$dst.tmp.$$"
  mv "$dst.tmp.$$" "$dst"
  echo "▸ wrote ${dst#"${SEED_ROOT:-}"/} (template version $version)"
}

# The compose file for STACK ("" is the plain starter, node:* and python:* add
# their dependency volume), then the image and entrypoint it builds.
seed_stack() {
  local root="$1" stack="${2:-}" base="${3:-node:22-bookworm}" dir="$1/.devcontainer" t template compose
  t=$(adopt_templates_dir)
  SEED_ROOT="$root"
  case "$stack" in
    "") template="$t/docker-compose.yml" ;;
    node:*) template="$t/docker-compose.node.yml" ;;
    python:*) template="$t/docker-compose.python.yml" ;;
    *) die "seed_stack: unrecognised stack $stack" ;;
  esac
  compose="$dir/docker-compose.yml"
  seed_file "$compose" "$template"
  seed_sshd_image "$root" "$base"
}

seed_sshd_image() {
  local root="$1" base="$2" t
  t=$(adopt_templates_dir)
  SEED_ROOT="$root"
  seed_file "$root/.devcontainer/engine.Dockerfile" "$t/engine.Dockerfile" "$base"
  seed_file "$root/.devcontainer/engine-entrypoint.sh" "$t/engine-entrypoint.sh" "" 0755
}

# A compose file the engine seeded (it carries the stamp) is the engine's stack,
# so the image and entrypoint it builds are added when they are missing. A
# project's own compose file is never given an image.
seed_missing_image() {
  local root="$1" compose="$1/.devcontainer/docker-compose.yml" base=node:22-bookworm
  [[ -n "$(seed_stamp_version "$compose")" ]] || return 0
  grep -q '^  venv:' "$compose" && base=python:3.12-bookworm
  seed_sshd_image "$root" "$base"
}

# Which template the credential-file template is: the App's names when the
# project asks for FORGE_AUTH=app, else the token.
seed_credential_template_name() {
  case "$1" in
    app) echo agent.env.app.example ;;
    *) echo agent.env.example ;;
  esac
}

# NAME.example beside the credential file project.env names. It lists the names
# to fill in and holds no value; the credential file itself is the owner's.
seed_credential_template() {
  local root="$1" mode credential
  mode=$(adopt_env_setting "$root/project.env" FORGE_AUTH)
  credential=$(adopt_env_setting "$root/project.env" FORGE_AUTH_FILE)
  [[ -n "$credential" ]] || credential=".devcontainer/agent.env"
  SEED_ROOT="$root"
  seed_file "$root/$credential.example" "$(adopt_templates_dir)/$(seed_credential_template_name "$mode")"
}

# doctor: for each starter file, the template version it was seeded from and,
# when the template has moved on, the command that shows the difference.
# Changes nothing.
seed_report() {
  local root="$1" mode credential t name template file seeded current
  t=$(adopt_templates_dir)
  mode=$(adopt_env_setting "$root/project.env" FORGE_AUTH)
  credential=$(adopt_env_setting "$root/project.env" FORGE_AUTH_FILE)
  [[ -n "$credential" ]] || credential=".devcontainer/agent.env"
  local -a pairs=(
    ".devcontainer/docker-compose.yml:docker-compose.yml"
    ".devcontainer/engine.Dockerfile:engine.Dockerfile"
    ".devcontainer/engine-entrypoint.sh:engine-entrypoint.sh"
    "$credential.example:$(seed_credential_template_name "$mode")"
  )
  for name in "${pairs[@]}"; do
    file="$root/${name%%:*}"
    template="$t/${name#*:}"
    [[ -f "$file" && -f "$template" ]] || continue
    seeded=$(seed_stamp_version "$file")
    current=$(seed_stamp_version "$template")
    [[ -n "$seeded" && -n "$current" ]] || continue
    if (( 10#$seeded < 10#$current )); then
      echo "$(basename "$file") was seeded from template v$seeded, current is v$current — see what changed: diff -u $file $template. Nothing is changed; the file is yours."
    fi
  done
}
