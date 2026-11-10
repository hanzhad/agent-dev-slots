# shellcheck shell=bash disable=SC2034,SC2154

detect_project_stack() {
  local root="$1"
  if [[ -f "$root/pnpm-lock.yaml" ]]; then
    echo "node:pnpm"
  elif [[ -f "$root/yarn.lock" ]]; then
    echo "node:yarn"
  elif [[ -f "$root/package-lock.json" || -f "$root/package.json" ]]; then
    echo "node:npm"
  elif [[ -f "$root/pyproject.toml" || -f "$root/requirements.txt" ]]; then
    echo "python:pip"
  else
    return 1
  fi
}

adopt_infer_project() {
  local name
  name=$(basename "$1" | tr "[:upper:]" "[:lower:]" | sed -E "s/[^a-z0-9_-]+/-/g; s/^[^a-z0-9]+//; s/[-_]+$//")
  echo "${name:-project}"
}

adopt_infer_branch() {
  local root="$1" ref=""
  ref=$(git -C "$root" symbolic-ref --short refs/remotes/origin/HEAD 2>/dev/null) || ref=""
  if [[ -n "$ref" ]]; then
    echo "${ref#origin/}"
    return 0
  fi
  ref=$(git -C "$root" symbolic-ref --short HEAD 2>/dev/null) || ref=""
  echo "${ref:-main}"
}

# The starter compose file, image and entrypoint are seeded from the engine's
# templates by lib/seed.sh, once: a file that exists is never rewritten.
adopt_write_sshd_image() {
  seed_sshd_image "$1" "$2"
}

adopt_write_starter_compose() {
  seed_stack "$1" "" node:22-bookworm
}

adopt_stack_compose() {
  local root="$1" stack="$2"
  if [[ -f "$root/.devcontainer/docker-compose.yml" || -f "$root/.devcontainer/compose.yaml" ]]; then
    return 0
  fi
  case "$stack" in
    node:*) seed_stack "$root" "$stack" node:22-bookworm ;;
    python:*) seed_stack "$root" "$stack" python:3.12-bookworm ;;
    *) die "adopt_stack_compose: unrecognised stack $stack" ;;
  esac
}

# The yml and yaml files of a checkout that may be its compose files. Directories
# of VCS metadata, dependencies and test fixtures are not searched: a repository
# that carries compose files as test data has not got that layout itself.
# SEARCH is ROOT, or a directory below it when the layout is already decided.
adopt_compose_candidates() {
  local root="$1" search="${2:-$1}"
  find "$search" -mindepth 1 \( -name .git -o -name .github -o -name node_modules \
      -o -name tests -o -name test -o -name fixtures -o -name testdata -o -name __fixtures__ \) -prune \
    -o -type f \( -name "*.yml" -o -name "*.yaml" \) \
    ! -name "*.secrets.yml" ! -name "*.secrets.yaml" -print 2>/dev/null | sort
}

adopt_has_compose() {
  local root="$1" f
  while IFS= read -r f; do
    [[ -n "$f" ]] || continue
    if grep -Eq "^services:[[:space:]]*$" "$f" 2>/dev/null; then
      return 0
    fi
  done < <(adopt_compose_candidates "$root")
  return 1
}

adopt_discover_compose() {
  local root="$1" template_mode="$2" f rel dir base dots search="$1"
  if [[ "$template_mode" == yes ]]; then
    if [[ ! -f "$root/.devcontainer/docker-compose.yml" ]]; then
      adopt_write_starter_compose "$root"
    fi
    search="$root/.devcontainer"
  fi
  local -a found=() dirs=()
  local seen_dirs=" "
  while IFS= read -r f; do
    [[ -n "$f" ]] || continue
    grep -Eq "^services:[[:space:]]*$" "$f" 2>/dev/null || continue
    rel=${f#"$root"/}
    if [[ "$rel" == */* ]]; then
      dir=${rel%/*}
    else
      dir="."
    fi
    found+=("$rel")
    case "$seen_dirs" in
      *" $dir "*) ;;
      *) seen_dirs+="$dir "; dirs+=("$dir") ;;
    esac
  done < <(adopt_compose_candidates "$root" "$search")

  if (( ${#dirs[@]} > 1 )); then
    die "adopt: ambiguous compose layout in $root — multiple directories hold compose files (${dirs[*]}). Write project.env to name COMPOSE_DIR and COMPOSE_FILES."
  fi
  if (( ${#dirs[@]} == 0 )); then
    [[ "$template_mode" != no ]] || die "adopt: no compose file found in $root and --no-template was given"
    adopt_write_starter_compose "$root"
    ADOPT_COMPOSE_DIR=".devcontainer"
    ADOPT_COMPOSE_FILES="docker-compose.yml"
    return 0
  fi

  ADOPT_COMPOSE_DIR="${dirs[0]}"
  local -a keyed=() sorted=()
  for rel in "${found[@]}"; do
    base=${rel##*/}
    dots=${base//[^.]/}
    keyed+=("${#dots}:$base")
  done
  while IFS= read -r item; do
    [[ -n "$item" ]] && sorted+=("${item#*:}")
  done < <(printf "%s\n" "${keyed[@]}" | sort)
  ADOPT_COMPOSE_FILES="${sorted[*]}"
}

adopt_infer_ports() {
  local dir="$1" files="$2" f token var seen=" "
  local -a f_list=() ports=()
  read -ra f_list <<<"$files"
  for f in "${f_list[@]}"; do
    [[ -f "$dir/$f" ]] || continue
    while IFS= read -r token; do
      [[ -n "$token" ]] || continue
      var=${token#??}
      var=${var%?}
      var=${var%%[:-]*}
      [[ "$var" =~ ^[A-Z][A-Z0-9_]*_PORT$ ]] || continue
      case "$seen" in
        *" $var "*) ;;
        *) seen+="$var "; ports+=("$var") ;;
      esac
    done < <(grep -oE "[$][{][A-Z][A-Z0-9_]*_PORT([:-][^}]*)?[}]" "$dir/$f" 2>/dev/null || true)
  done
  (( ${#ports[@]} > 0 )) || ports=("SSH_PORT")
  echo "${ports[*]}"
}

adopt_exclude_dotenv() {
  local root="$1" common_dir="$2" compose_dir="$3" exclude="$common_dir/info/exclude" rel pattern
  local -a rels=(".env" ".devcontainer/.env")
  if [[ -n "$compose_dir" && "$compose_dir" != ".devcontainer" && "$compose_dir" != "." ]]; then
    rels+=("${compose_dir%/}/.env")
  fi
  mkdir -p "$common_dir/info"
  for rel in "${rels[@]}"; do
    git -C "$root" check-ignore -q "$rel" 2>/dev/null && continue
    pattern="/$rel"
    if [[ -f "$exclude" ]] && grep -Fxq "$pattern" "$exclude" 2>/dev/null; then
      continue
    fi
    printf "%s\n" "$pattern" >>"$exclude"
  done
}

adopt_exclude_project_files() {
  local root="$1" exclude pat
  exclude=$(git -C "$root" rev-parse --path-format=absolute --git-path info/exclude 2>/dev/null || echo "$root/.git/info/exclude")
  case "$exclude" in /*) ;; *) exclude="$root/$exclude" ;; esac
  mkdir -p "$(dirname "$exclude")"
  local -a patterns=("/project.env" "/bin/dev-container")
  for pat in "${patterns[@]}"; do
    if [[ -f "$exclude" ]] && grep -Fxq "$pat" "$exclude" 2>/dev/null; then
      continue
    fi
    printf "%s\n" "$pat" >>"$exclude"
  done
}

adopt_write_dotenv() {
  local root="$1" compose_dir="${2:-.devcontainer}"
  local target_env
  if [[ "$compose_dir" == "." || -z "$compose_dir" ]]; then
    target_env="$root/.env"
  else
    target_env="$root/${compose_dir%/}/.env"
  fi
  # The directory is the compose directory: it exists once a compose file does.
  [[ -d "$(dirname "$target_env")" ]] || return 0
  touch "$target_env"
  local git_user git_email
  git_user=$(git -C "$root" config user.name 2>/dev/null || true)
  git_email=$(git -C "$root" config user.email 2>/dev/null || true)
  if ! grep -q "^[[:space:]]*GIT_USER_NAME=" "$target_env" 2>/dev/null; then
    echo "GIT_USER_NAME=${git_user:-dev-container-agent}" >>"$target_env"
  fi
  if ! grep -q "^[[:space:]]*GIT_USER_EMAIL=" "$target_env" 2>/dev/null; then
    echo "GIT_USER_EMAIL=${git_email:-dev-container-agent@users.noreply.github.com}" >>"$target_env"
  fi
}

# What to do about the compose files a project.env names, decided from strings
# and the files that exist: "ok" (all there), "starter" (the starter stack is
# what it asks for and none of it exists), or "missing <dir>/<file>".
adopt_compose_plan() {
  local root="$1" dir="${2:-.devcontainer}" files="${3:-docker-compose.yml}" f base first=""
  local -a names=()
  dir=${dir%/}
  [[ -n "$dir" ]] || dir="."
  base="$root"
  [[ "$dir" == "." ]] || base="$root/$dir"
  read -ra names <<<"$files"
  for f in "${names[@]}"; do
    [[ -f "$base/$f" ]] && continue
    if [[ -z "$first" ]]; then
      first="$f"
      [[ "$dir" == "." ]] || first="$dir/$f"
    fi
  done
  if [[ -z "$first" ]]; then
    echo ok
  elif [[ "$dir" == ".devcontainer" && "${names[*]}" == "docker-compose.yml" ]]; then
    echo starter
  else
    echo "missing $first"
  fi
}

adopt_ensure_compose() {
  local plan
  plan=$(adopt_compose_plan "$@")
  case "$plan" in
    ok) ;;
    starter)
      echo "▸ project.env names .devcontainer/docker-compose.yml, which does not exist: writing the starter stack"
      adopt_write_starter_compose "$1"
      ;;
    "missing "*)
      die "adopt: project.env names ${plan#missing }, which does not exist. Create it, or change COMPOSE_DIR and COMPOSE_FILES in project.env."
      ;;
  esac
}

adopt_auto_connect() {
  local root="$1" compose_dir=".devcontainer"
  if [[ -f "$root/project.env" ]]; then
    read_project_env "$root"
    compose_dir=${PE_COMPOSE_DIR:-.devcontainer}
    adopt_ensure_compose "$root" "$compose_dir" "${PE_COMPOSE_FILES:-docker-compose.yml}"
  elif ! adopt_has_compose "$root"; then
    local stack
    stack=$(detect_project_stack "$root") || {
      echo "dev-container: unrecognised project stack in $root — no supported lockfile or manifest found." >&2
      echo "Next: create .devcontainer/docker-compose.yml and project.env, or run: $CALLER adopt --template" >&2
      exit 1
    }
    echo "▸ auto-connecting repository with detected stack: $stack"
    adopt_stack_compose "$root" "$stack"
  fi
  local common_dir
  common_dir=$(git -C "$root" rev-parse --path-format=absolute --git-common-dir 2>/dev/null) || die "not a git checkout"
  if [[ ! -f "$root/project.env" ]]; then
    adopt_discover_compose "$root" auto
    compose_dir="$ADOPT_COMPOSE_DIR"
    local project branch ports compose_path="$root/$compose_dir"
    [[ "$compose_dir" != "." ]] || compose_path="$root"
    project=$(adopt_infer_project "$root")
    branch=$(adopt_infer_branch "$root")
    ports=$(adopt_infer_ports "$compose_path" "$ADOPT_COMPOSE_FILES")
    {
      printf "PROJECT=%s\n" "$project"
      printf "DEFAULT_BRANCH=%s\n" "$branch"
      printf "SLOT_PORTS=%s\n" "$ports"
      printf "COMPOSE_DIR=%s\n" "$compose_dir"
      printf "COMPOSE_FILES=%s\n" "$ADOPT_COMPOSE_FILES"
    } >"$root/project.env.tmp.$$"
    mv "$root/project.env.tmp.$$" "$root/project.env"
  fi
  adopt_write_dotenv "$root" "$compose_dir"
  adopt_launcher_notes "$root" "$compose_dir"
  adopt_exclude_dotenv "$root" "$common_dir" "$compose_dir"
  adopt_exclude_project_files "$root"
  adopt_write_docs "$root"
  AUTO_ADOPTED=1
}

cmd_adopt() {
  local target="" template_mode="auto" arg
  for arg in "$@"; do
    case "$arg" in
      --template)
        [[ "$template_mode" != no ]] || die "adopt: cannot pass both --template and --no-template"
        template_mode="yes"
        ;;
      --no-template)
        [[ "$template_mode" != yes ]] || die "adopt: cannot pass both --template and --no-template"
        template_mode="no"
        ;;
      -*)
        die "adopt: unknown flag: $arg"
        ;;
      *)
        [[ -z "$target" ]] || die "adopt takes at most one checkout directory — got $target and $arg"
        target="$arg"
        ;;
    esac
  done
  if [[ -z "$target" ]]; then
    target=${DEV_CONTAINER_CALLER_PWD:-}
    [[ -n "$target" ]] || die "adopt was not given a checkout directory and DEV_CONTAINER_CALLER_PWD is not set"
  elif [[ "$target" != /* && -n "${DEV_CONTAINER_CALLER_PWD:-}" ]]; then
    target="${DEV_CONTAINER_CALLER_PWD%/}/$target"
  fi
  [[ -d "$target" ]] || die "adopt: $target is not a directory"
  target=$(CDPATH= cd -P -- "$target" 2>/dev/null && pwd) || die "adopt: $target is not a directory"
  outside_root "$target"
  local common_dir root
  common_dir=$(git -C "$target" rev-parse --path-format=absolute --git-common-dir 2>/dev/null) \
    || die "adopt: $target is not inside a git checkout"
  root=$(dirname "$common_dir")
  outside_root "$root" strict

  local compose_dir=".devcontainer"
  if [[ -f "$root/project.env" ]]; then
    read_project_env "$root"
    check_project_env "$root/project.env" "$PE_DEFAULT_BRANCH" "$PE_SLOT_PORTS" "${PE_SLOT_MOUNT_DIRS:-}" "${PE_SLOT_OVERLAYS:-}" "${PE_SLOT_ENV_KEYS:-}" "${PE_SLOT_MEMORY_LIMIT:-}"
    compose_dir=${PE_COMPOSE_DIR:-.devcontainer}
    adopt_ensure_compose "$root" "$compose_dir" "${PE_COMPOSE_FILES:-docker-compose.yml}"
  else
    local stack
    if [[ "$template_mode" != yes ]] && ! adopt_has_compose "$root"; then
      if stack=$(detect_project_stack "$root" 2>/dev/null); then
        adopt_stack_compose "$root" "$stack"
      fi
    fi
    adopt_discover_compose "$root" "$template_mode"
    compose_dir="$ADOPT_COMPOSE_DIR"
    local project branch ports compose_path="$root/$compose_dir"
    [[ "$compose_dir" != "." ]] || compose_path="$root"
    project=$(adopt_infer_project "$root")
    branch=$(adopt_infer_branch "$root")
    ports=$(adopt_infer_ports "$compose_path" "$ADOPT_COMPOSE_FILES")
    {
      printf "PROJECT=%s\n" "$project"
      printf "DEFAULT_BRANCH=%s\n" "$branch"
      printf "SLOT_PORTS=%s\n" "$ports"
      printf "COMPOSE_DIR=%s\n" "$compose_dir"
      printf "COMPOSE_FILES=%s\n" "$ADOPT_COMPOSE_FILES"
    } >"$root/project.env.tmp.$$"
    mv "$root/project.env.tmp.$$" "$root/project.env"
    read_project_env "$root"
    check_project_env "$root/project.env" "$PE_DEFAULT_BRANCH" "$PE_SLOT_PORTS" "${PE_SLOT_MOUNT_DIRS:-}" "${PE_SLOT_OVERLAYS:-}" "${PE_SLOT_ENV_KEYS:-}" "${PE_SLOT_MEMORY_LIMIT:-}"
  fi

  adopt_write_dotenv "$root" "$compose_dir"

  adopt_launcher_notes "$root" "$compose_dir"
  adopt_exclude_dotenv "$root" "$common_dir" "$compose_dir"
  adopt_exclude_project_files "$root"
  adopt_write_docs "$root"
  echo "adopted $root ($PE_PROJECT)"
  local volume_warning
  volume_warning=$(workspace_volume_warning "$root" "$compose_dir" "${PE_COMPOSE_FILES:-${ADOPT_COMPOSE_FILES:-docker-compose.yml}}" "$PE_PROJECT")
  [[ -z "$volume_warning" ]] || printf 'warn  %s
' "$volume_warning"
  # The credential file is the owner's to fill: it is checked last, after everything else is written.
  preflight_reset
  credential_problems "$root" "${PE_FORGE_AUTH:-}" "${PE_FORGE_AUTH_FILE:-}"
  if (( ${#PF_WHAT[@]} )); then
    echo "adopt: $root is adopted, but its credential file is not ready:" >&2
    preflight_report
    exit 1
  fi
  exit 0
}
