# shellcheck shell=bash disable=SC2034,SC2154

# What a project is. The absolute path of its repo root is the identity: the
# KEY is the first 8 hex characters of the sha256 of that canonical path.
# PROJECT in project.env is only a display name the engine cannot vouch for, so
# it becomes the LABEL (the slot ids', volumes' and state files' prefix) only
# while no other root claims it. A copy, a move or a rename is just another
# project; nothing here judges it, refuses it or warns about it.
#
# The registry, in $STATE/projects/:
#   <key>.json        {root, base, label} - the label this root got for that base
#   labels/<label>    the key that owns the label; made with noclobber, so the
#                     first claim wins, also when two runs race
# A label is sticky: while <key>.json exists and its base is unchanged, its
# label is reused, so a docker name never moves because another root went away.
# A later root that asks for a taken base gets BASE_KEY - with _, not -, because
# a "<base>-*" glob (the slot ids' own) must not reach it.

PROJECT_LABEL_RE='^[a-z0-9][a-z0-9_-]*$'

project_canonical_root() {
  local canon
  canon=$(CDPATH= cd -P "$1" 2>/dev/null && pwd -P) || canon=$1
  printf '%s' "$canon"
}

project_key_of() {
  project_canonical_root "$1" | adopt_sha256 | cut -c1-8
}

# PROJECT when project.env gives one, else the folder's name; never a fixed one.
project_label_base() {
  if [[ -n "${2:-}" ]]; then
    printf '%s' "$2"
  else
    adopt_infer_project "$1"
  fi
}

# OWNER_KEY is whoever has claimed BASE, empty if nobody.
project_label_pick() {
  local base="$1" key="$2" owner="${3:-}"
  if [[ -z "$owner" || "$owner" == "$key" ]]; then
    printf '%s' "$base"
  else
    printf '%s_%s' "$base" "$key"
  fi
}

# The key to claim a base for before this root does: that of a root that already
# runs slots under it. Nothing when no such root exists, or this one is among them.
project_seed_owner() {
  local key="$1" root other="" k
  shift
  for root in "$@"; do
    [[ -n "$root" ]] || continue
    k=$(project_key_of "$root")
    [[ "$k" != "$key" ]] || return 0
    [[ -n "$other" ]] || other=$k
  done
  printf '%s' "$other"
}

# Roots that state already on this machine ties to BASE: a lease records its
# repo_root, a volume carries it as a label.
project_evidence_roots() {
  local base="$1" f v
  for f in "$STATE/leases/$base"-slot-*.json; do
    [[ -f "$f" ]] || continue
    jq -r '.repo_root // empty' "$f" 2>/dev/null || true
  done
  command -v docker >/dev/null 2>&1 || return 0
  while IFS= read -r v; do
    [[ -n "$v" ]] || continue
    docker volume inspect "$v" --format '{{index .Labels "dev-container.repo-root"}}' 2>/dev/null || true
  done < <(docker volume ls --filter "label=dev-container.project=$base" --format '{{.Name}}' 2>/dev/null || true)
}

# 0 when LABEL belongs to KEY after the call, whoever made it so.
project_claim_label() {
  local file="$1/labels/$2" owner
  (set -o noclobber; printf '%s\n' "$3" >"$file") 2>/dev/null || true
  owner=$(cat "$file" 2>/dev/null || true)
  [[ "$owner" == "$3" ]]
}

project_registry_write() {
  local file="$1/$2.json"
  jq -n --arg root "$3" --arg base "$4" --arg chosen "$5" '{root:$root, base:$base, label:$chosen}' >"$file.tmp.$$"
  mv "$file.tmp.$$" "$file"
}

# A base nobody has claimed goes first to the other root that already runs
# slots under it, so a copy cannot take the real project's label before that
# project has run once.
project_seed_label() {
  local dir="$1" base="$2" key="$3" seed
  local -a roots=()
  [[ ! -e "$dir/labels/$base" ]] || return 0
  mapfile -t roots < <(project_evidence_roots "$base")
  seed=$(project_seed_owner "$key" ${roots[@]+"${roots[@]}"})
  [[ -z "$seed" ]] || project_claim_label "$dir" "$base" "$seed" || true
}

# Sets PROJECT_KEY, PROJECT_LABEL and SLOT_PROJECT_PREFIX for ROOT. PROJECT is
# project.env's value, empty when there is none.
resolve_project_identity() {
  local root dir="$STATE/projects" key base label="" stored="" owner i
  root=$(project_canonical_root "$1")
  key=$(project_key_of "$root")
  base=$(project_label_base "$root" "${2:-}")
  [[ "$base" =~ $PROJECT_LABEL_RE ]] || die "$root: the project name $base is not a label - it must be lower-case letters, digits, - and _, starting with a letter or digit."
  PROJECT_KEY=$key
  PROJECT_LABEL=$base
  SLOT_PROJECT_PREFIX=$base
  # No state directory to keep a registry in: the commands that cope with that
  # (doctor) name it themselves.
  mkdir -p "$dir/labels" 2>/dev/null || return 0

  if [[ -f "$dir/$key.json" && "$(jq -r '.base // empty' "$dir/$key.json" 2>/dev/null || true)" == "$base" ]]; then
    stored=$(jq -r '.label // empty' "$dir/$key.json" 2>/dev/null || true)
  fi
  if [[ "$stored" =~ $PROJECT_LABEL_RE ]] && project_claim_label "$dir" "$stored" "$key"; then
    label=$stored
  else
    project_seed_label "$dir" "$base" "$key"
    for i in 1 2 3 4 5; do
      owner=$(cat "$dir/labels/$base" 2>/dev/null || true)
      label=$(project_label_pick "$base" "$key" "$owner")
      project_claim_label "$dir" "$label" "$key" && break
      label=""
      sleep 0.1
    done
    [[ -n "$label" ]] || die "could not claim a label for $root under $dir/labels."
    project_registry_write "$dir" "$key" "$root" "$base" "$label"
  fi
  PROJECT_LABEL=$label
  SLOT_PROJECT_PREFIX=$label
}
