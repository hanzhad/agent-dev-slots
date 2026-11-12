# shellcheck shell=bash disable=SC2034,SC2154

# Container-executed script to collect repository facts.
# shellcheck disable=SC2016
FACTS_SCRIPT='
set -u
export GIT_OPTIONAL_LOCKS=0 GIT_TERMINAL_PROMPT=0
T=$(printf "\t")
sub=${1:-.}
cd "$sub" 2>/dev/null || { echo norepo; exit 0; }
[ -e .git ] || { echo norepo; exit 0; }
[ "$sub" = . ] || [ -f .git/HEAD ] || [ -f .git ] || { echo norepo; exit 0; }
git rev-parse --git-dir >/dev/null 2>&1 || { echo norepo; exit 0; }
if [ "${2:-fetch}" != fetch ]; then
  echo "fetch${T}skipped"
elif git fetch --quiet --prune origin >/dev/null 2>&1; then
  echo "fetch${T}ok"
else
  echo "fetch${T}failed"
fi
printf "origin%s%s\n" "$T" "$(git remote get-url origin 2>/dev/null)"
statuses=$(git status --porcelain 2>/dev/null)
if [ -n "$statuses" ]; then
  printf "%s\n" "$statuses" | sed "s/^/status$T/"
else
  echo "status"
fi
stashes=$(git stash list 2>/dev/null)
if [ -n "$stashes" ]; then
  printf "%s\n" "$stashes" | sed "s/^/stash$T/"
else
  echo "stash"
fi
head=$(git symbolic-ref --quiet --short HEAD 2>/dev/null || true)
tip=$(git rev-parse HEAD 2>/dev/null || true)
printf "head%s%s%s%s\n" "$T" "$head" "$T" "$tip"
reach=0
[ -n "$tip" ] && [ -n "$(git branch -r --contains "$tip" 2>/dev/null)" ] && reach=1
printf "headreach%s%s\n" "$T" "$reach"
def=$(git symbolic-ref --short refs/remotes/origin/HEAD 2>/dev/null || true)
def=${def#origin/}
def=${def:-${3:-}}
printf "default%s%s\n" "$T" "$def"
printf "user%s%s\n" "$T" "$(git config user.name 2>/dev/null || true)"
[ ! -e docs/repo/.git ] || printf "docsrepo%s1\n" "$T"
cdir=${4:-'"$DEFAULT_COMPOSE_DIR"'}
[ ! -s "$cdir/.slot-ports" ] || printf "ports%s1\n" "$T"
[ ! -s "$cdir/.slot-ports" ] || sed "s/^/slotport$T/" "$cdir/.slot-ports"
for c in docs/repo docs/fix; do
  [ ! -f "$c/.git/HEAD" ] || printf "clone%s%s\n" "$T" "$c"
done
if [ -f docs/repo/.git/HEAD ]; then
  printf "docsorigin%s%s\n" "$T" "$(git -C docs/repo remote get-url origin 2>/dev/null)"
  printf "docshead%s%s\n" "$T" "$(git -C docs/repo rev-parse HEAD 2>/dev/null || echo nothing)"
fi
printf "shallow%s%s\n" "$T" "$(git rev-parse --is-shallow-repository 2>/dev/null)"
if [ -n "$def" ]; then
  pin=$( (git show "origin/$def:.github/docs-pin" 2>/dev/null || true) | grep -v "^[[:space:]]*#" | tr -d "[:space:]" )
  [ -z "$pin" ] || printf "docspin%s%s\n" "$T" "$pin"
fi
g=$(git rev-parse --git-dir 2>/dev/null || true)
[ -n "$g" ] && [ -f "$g/slot-docs-pin" ] && printf "marker%s%s\n" "$T" "$(cat "$g/slot-docs-pin")"
if wts=$(git worktree list --porcelain 2>/dev/null); then
  printf "wtok%s1\n" "$T"
  printf "%s\n" "$wts" | sed -n "s/^worktree /worktree$T/p" | tail -n +2
  printf "%s\n" "$wts" | sed -n "s,^branch refs/heads/,wtbranch$T,p"
fi
git for-each-ref --format="%(refname:short)" refs/heads/ 2>/dev/null | while IFS= read -r b; do
  [ -n "$b" ] || continue
  btip=$(git rev-parse "refs/heads/$b" 2>/dev/null || true)
  if git show-ref --verify --quiet "refs/remotes/origin/$b"; then
    has=1; ahead=$(git rev-list --count "origin/$b..$b" 2>/dev/null || echo 0)
  else
    has=0; ahead=0
  fi
  reach=0
  [ -n "$(git branch -r --contains "$btip" 2>/dev/null)" ] && reach=1
  cherry=na
  if [ -n "$def" ] && git show-ref --verify --quiet "refs/remotes/origin/$def"; then
    if out=$(git cherry "origin/$def" "$b" 2>/dev/null); then
      if printf "%s\n" "$out" | grep -q "^+"; then cherry=plus; else cherry=clean; fi
    else
      cherry=failed
    fi
  fi
  notremote=$(git rev-list --count "$b" --not --remotes 2>/dev/null || echo "?")
  unsigned=$( (git log --format=%G? "refs/heads/$b" --not --remotes=origin 2>/dev/null || true) | grep -c "^N" || true)
  mt=0
  if [ "$reach" = 0 ] && [ "$cherry" != clean ] && [ "$cherry" != na ] && [ -n "$def" ]; then
    m=$(git merge-tree --write-tree "origin/$def" "$btip" 2>/dev/null || true)
    tree_oid=$(git rev-parse "origin/$def^{tree}" 2>/dev/null || true)
    [ -n "$m" ] && [ "$m" = "$tree_oid" ] && mt=1
  fi
  printf "branch%s%s%s%s%s%s%s%s%s%s%s%s%s%s%s%s%s%s\n" "$T" "$b" "$T" "$btip" "$T" "$has" "$T" "$ahead" "$T" "$reach" "$T" "$cherry" "$T" "$notremote" "$T" "${unsigned:-0}" "$T" "$mt"
done
exit 0
'

# Container-executed script to test whether any merged oid covers tip.
# shellcheck disable=SC2016
OIDS_COVER_TIP_SCRIPT='
cd "$1" 2>/dev/null || exit 1
tip=$2
shift 2
parent=$(git rev-parse --verify "$tip^" 2>/dev/null || true)
tip_has_changes=1
if [ -n "$parent" ] && [ "$(git rev-parse "$tip^{tree}" 2>/dev/null)" = "$(git rev-parse "$parent^{tree}" 2>/dev/null)" ]; then
  tip_has_changes=0
fi
for oid in "$@"; do
  git cat-file -e "$oid^{commit}" 2>/dev/null || continue
  git merge-base --is-ancestor "$tip" "$oid" 2>/dev/null && exit 0
  if [ "$tip_has_changes" = 1 ]; then
    m=$(git merge-tree --write-tree "$oid" "$tip" 2>/dev/null)       && [ "$m" = "$(git rev-parse "$oid^{tree}" 2>/dev/null)" ] && exit 0
  fi
done
exit 1
'

FACTS_NO_FETCH=${FACTS_NO_FETCH:-0}

facts_file() {
  local scratch="${SCRATCH:-${DEV_CONTAINER_HOME:-$HOME/.dev-containers}/scratch}"
  local sid="$1" sub="${2:-.}"
  if [[ "$sub" == "." || -z "$sub" ]]; then
    echo "$scratch/facts/$sid"
  else
    echo "$scratch/facts/${sid}__${sub//\//_}"
  fi
}

facts_of() {
  local sid="$1" sub="${2:-.}" f fetch=fetch
  local scratch="${SCRATCH:-${DEV_CONTAINER_HOME:-$HOME/.dev-containers}/scratch}"
  f=$(facts_file "$sid" "$sub")
  if [[ ! -f "$f" ]]; then
    mkdir -p "$scratch/facts"
    if (( FACTS_NO_FETCH )); then
      fetch=skip
    elif ! compose_running "$sid"; then
      fetch=skip
      echo "▸ '$sid' is stopped — not fetched; delivered/held read from its refs as of its last fetch while running" >&2
    elif (( ${HOST_GIT_READ_ONLY:-0} )) && [[ -f "$(lease_file_of "$sid")" ]]; then
      fetch=skip
      echo "⚠ '$sid' is leased — not fetching origin while it may be in use; delivered/held verdicts are only as fresh as its last fetch" >&2
    fi
    if ! slot_sh "$sid" sh -c "$FACTS_SCRIPT" sh "$sub" "$fetch" "$DEFAULT_BRANCH" "$COMPOSE_DIR" >"$f.tmp" 2>"$f.err"; then
      echo "⚠ could not read '$sid' ($sub): its container would not run — $(tail -1 "$f.err" 2>/dev/null)" >&2
      echo error >"$f.tmp"
    elif grep -q "^fetch"$'	'"failed" "$f.tmp"; then
      echo "⚠ could not fetch origin in '$sid' ($sub) — delivered/held verdicts below are only as fresh as its last successful fetch" >&2
    fi
    mv "$f.tmp" "$f"
    rm -f "$f.err"
  fi
  cat "$f"
}

facts_reset() {
  local scratch="${SCRATCH:-${DEV_CONTAINER_HOME:-$HOME/.dev-containers}/scratch}"
  local sid="$1"
  rm -f "$scratch/facts/$sid" "$scratch/facts/${sid}__"*
}

fact() {
  if (( $# == 1 )); then
    grep "^${1}"$'	' 2>/dev/null | head -1 | cut -f2- || true
  else
    local target="$1" key="$2" src="$1"
    [[ -f "$src" ]] || src=$(facts_file "$target")
    [[ -f "$src" ]] || facts_of "$target" >/dev/null 2>&1 || true
    grep "^${key}"$'	' "$src" 2>/dev/null | head -1 | cut -f2- || true
  fi
}

facts_lines() {
  if (( $# == 1 )); then
    grep "^${1}"$'	' 2>/dev/null | cut -f2- || true
  else
    local target="$1" key="$2" src="$1"
    [[ -f "$src" ]] || src=$(facts_file "$target")
    [[ -f "$src" ]] || facts_of "$target" >/dev/null 2>&1 || true
    grep "^${key}"$'	' "$src" 2>/dev/null | cut -f2- || true
  fi
}

facts_unreadable() {
  if (( $# == 0 )); then
    grep -q -E '^(error|norepo)($|	)'
  else
    local target="$1" src="$1"
    [[ -f "$src" ]] || src=$(facts_file "$target")
    [[ -f "$src" ]] || facts_of "$target" >/dev/null 2>&1 || true
    [[ -f "$src" ]] || return 0
    grep -q -E '^(error|norepo)($|	)' "$src" 2>/dev/null
  fi
}

branch_fact_line() {
  local target="$1" branch="$2" src="$1"
  [[ -f "$src" ]] || src=$(facts_file "$target")
  [[ -f "$src" ]] || facts_of "$target" >/dev/null 2>&1 || true
  B="$branch" awk -F'	' '$1 == "branch" && $2 == ENVIRON["B"] { sub(/^branch	/, ""); print; exit }' "$src" 2>/dev/null || true
}

merged_oids_cover_tip() {
  local sid sub tip oids oid
  if (( $# == 3 )); then
    sid="$1"
    sub="."
    tip="$2"
    oids="$3"
  else
    sid="$1"
    sub="$2"
    tip="$3"
    oids="$4"
  fi
  local -a args=()
  while IFS= read -r oid; do
    if [[ "$oid" =~ ^[0-9a-f]{40,64}$ ]]; then args+=("$oid"); fi
  done <<<"$oids"
  (( ${#args[@]} > 0 )) || return 1
  slot_sh "$sid" sh -c "$OIDS_COVER_TIP_SCRIPT" sh "$sub" "$tip" "${args[@]}" 2>/dev/null
}
