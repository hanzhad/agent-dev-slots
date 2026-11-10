# shellcheck shell=bash disable=SC2034,SC2154

# The two documents adopt leaves in a project's .devcontainer/: README.md (how
# to fill the credential file, what is checked, how to register the signing
# key) and AGENTS.md (the rules for an agent working inside a slot). They come
# from the engine's templates/ directory and carry no secret, so a slot gets
# them with the rest of .devcontainer/.
#
# They are seeded once: a file that exists is the project's own and adopt never
# changes it, whatever the template says now. The first line of a written
# document is a stamp:
#   <!-- dev-container-engine template: README.md version: 1 sha256: <digest> -->
# the template's version at the time; it is information only. `doctor` reads it
# and says when the template has moved on, with the diff command to see how.

ADOPT_DOC_NAMES="README.md AGENTS.md"
ADOPT_STAMP_RE='^<!-- dev-container-engine template: [A-Za-z.]+ version: ([0-9]+) sha256: ([0-9a-f]{64}) -->$'

adopt_templates_dir() {
  printf '%s' "${DEV_CONTAINER_TEMPLATES_DIR:-$lib_dir/../templates}"
}

# A setting of a project.env that is not read through read_project_env yet.
adopt_env_setting() {
  local value
  value=$(grep -E "^$2=" "$1" 2>/dev/null | tail -1 | cut -d= -f2- || true)
  value=${value%$'\r'}
  value=${value#\"}; value=${value%\"}
  value=${value#\'}; value=${value%\'}
  printf '%s' "$value"
}

# The template's body, with the project's own names filled in.
adopt_doc_body() {
  local template="$1" project="$2" credential="$3" body
  body=$(tail -n +2 "$template"; printf x)
  body=${body%x}
  body=${body//@PROJECT@/"$project"}
  body=${body//@CREDENTIAL_FILE@/"$credential"}
  printf '%s' "$body"
}

adopt_write_doc_file() {
  local target="$1" name="$2" version="$3" body="$4" digest
  digest=$(printf '%s' "$body" | adopt_sha256)
  {
    printf '<!-- dev-container-engine template: %s version: %s sha256: %s -->\n' "$name" "$version" "$digest"
    printf '%s' "$body"
  } >"$target.tmp.$$"
  mv "$target.tmp.$$" "$target"
}

adopt_write_doc() {
  local root="$1" name="$2" project="$3" credential="$4"
  local template dst first version body cur_version cur_digest now_digest
  template="$(adopt_templates_dir)/$name"
  [[ -f "$template" ]] || die "adopt: the engine has no template $template — the engine installation is incomplete."
  dst="$root/.devcontainer/$name"
  first=$(head -n 1 "$template")
  version=$(printf '%s' "$first" | sed -n 's/^<!-- dev-container-engine template: [A-Za-z.]* version: \([0-9][0-9]*\) -->$/\1/p')
  [[ -n "$version" ]] || die "adopt: $template does not start with a template version line."
  # Seeded once: the project owns the file from then on, so an existing one is never touched.
  [[ ! -e "$dst" ]] || return 0
  body=$(adopt_doc_body "$template" "$project" "$credential"; printf x)
  body=${body%x}
  mkdir -p "$root/.devcontainer"
  adopt_write_doc_file "$dst" "$name" "$version" "$body"
  echo "▸ wrote .devcontainer/$name (template version $version)"
}

# .devcontainer/ ignores itself: a .gitignore holding `*` goes with the folder
# into every checkout and slot, where .git/info/exclude of one clone would not.
# A project that tracks files in .devcontainer/ keeps its git as it is.
adopt_ignore_devcontainer() {
  local root="$1" file="$1/.devcontainer/.gitignore"
  if [[ -n "$(git -C "$root" ls-files -- .devcontainer 2>/dev/null)" ]]; then
    echo "▸ .devcontainer/ has files tracked in git: its git ignore rules are left as they are"
    return 0
  fi
  mkdir -p "$root/.devcontainer"
  if [[ -f "$file" ]]; then
    grep -Fxq '*' "$file" && return 0
    echo "▸ .devcontainer/.gitignore exists without a \`*\` line: left as it is, add the line so secrets in this folder stay out of git"
    return 0
  fi
  printf '# Written by dev-container adopt: nothing in this folder is committed (credentials live here).\n*\n' >"$file"
  echo "▸ wrote .devcontainer/.gitignore"
}

adopt_write_docs() {
  local root="$1" project credential name
  adopt_ignore_devcontainer "$root"
  project=$(adopt_env_setting "$root/project.env" PROJECT)
  credential=$(adopt_env_setting "$root/project.env" FORGE_AUTH_FILE)
  [[ -n "$project" ]] || project=$(basename "$root")
  [[ -n "$credential" ]] || credential=".devcontainer/agent.env"
  for name in $ADOPT_DOC_NAMES; do
    adopt_write_doc "$root" "$name" "$project" "$credential"
  done
  seed_credential_template "$root"
  seed_missing_image "$root"
}

# doctor: for each document, which template version it was seeded from and, when
# the template has moved on, the command that shows the difference. Changes nothing.
adopt_docs_report() {
  local root="$1" name dst template first seeded current
  for name in $ADOPT_DOC_NAMES; do
    dst="$root/.devcontainer/$name"
    template="$(adopt_templates_dir)/$name"
    [[ -f "$dst" && -f "$template" ]] || continue
    first=$(head -n 1 "$dst")
    [[ "$first" =~ $ADOPT_STAMP_RE ]] || continue
    seeded=${BASH_REMATCH[1]}
    current=$(head -n 1 "$template" | sed -n 's/^<!-- dev-container-engine template: [A-Za-z.]* version: \([0-9][0-9]*\) -->$/\1/p')
    [[ -n "$current" ]] || continue
    if (( 10#$seeded < 10#$current )); then
      echo "$name was seeded from template v$seeded, current is v$current — see what changed: diff -u <(tail -n +2 $dst) <(tail -n +2 $template). Nothing is changed; the file is yours."
    fi
  done
}
