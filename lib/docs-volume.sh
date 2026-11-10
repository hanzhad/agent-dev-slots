# shellcheck shell=bash disable=SC2034,SC2154

# The read-only docs clone for a slot whose workspace is a named volume. A
# directory slot gets it from docs.sh on a host path; a volume has none, so the
# same steps run inside it, in a helper container started with a network for
# this one step. The clone, the pin and the guard use docs.sh's own constants
# and hook text, passed in as arguments.

DOCS_PLACEHOLDER_URL="https://github.com/example/demo.docs.git"

# An unset DOCS_REPO_URL leaves bin/dev-container's placeholder in its place:
# that means "no docs clone" for a volume slot.
docs_repo_configured() {
  [[ -n "${DOCS_REPO_URL:-}" && "$DOCS_REPO_URL" != "$DOCS_PLACEHOLDER_URL" ]]
}

# Runs in /workspace of the helper, read from its stdin so that no argument carries
# a newline. $1 docs url, $2 fallback default branch, $3 pin file, $4 refusal
# text, $5 hooks dir, $6 push url, $7 excluded paths.
# shellcheck disable=SC2016
DOCS_VOLUME_SCRIPT='set -e
url=$1; fallback=$2; pinfile=$3; refusal=$4; hooks=$5; pushurl=$6; excludes=$7
git clone --depth 1 --quiet -- "$url" docs/repo
exclude=$(git rev-parse --git-path info/exclude)
mkdir -p "$(dirname "$exclude")"
for p in $excludes; do grep -Fxq "$p" "$exclude" 2>/dev/null || echo "$p" >>"$exclude"; done
branch=$(git symbolic-ref --short refs/remotes/origin/HEAD 2>/dev/null) || branch=$fallback
branch=${branch#origin/}
git fetch --quiet origin "refs/heads/$branch" 2>/dev/null \
  || echo "  ! could not fetch origin — docs/repo follows the pin on the main checkouts own $branch, which may be behind" >&2
pin=$(git show "origin/$branch:$pinfile" 2>/dev/null | grep -v "^[[:space:]]*#" | tr -d "[:space:]" || true)
if [ -z "$pin" ]; then
  echo "  ! no $pinfile on origin/$branch — docs/repo left where it is" >&2
else
  head=$(git -C docs/repo rev-parse HEAD 2>/dev/null) || head=nothing
  if [ "$head" != "$pin" ]; then
    echo "▸ moving docs/repo to the docs pin"
    if ! git -C docs/repo cat-file -e "$pin^{commit}" 2>/dev/null; then
      git -C docs/repo fetch --quiet --depth 1 origin "$pin" \
        || echo "  ! could not fetch the docs pin into docs/repo, and anything that reads docs/repo will say so" >&2
    fi
    if git -C docs/repo checkout --quiet --detach "$pin"; then
      echo "$pin" >"$(git -C docs/repo rev-parse --absolute-git-dir)/slot-docs-pin"
    fi
  fi
fi
mkdir -p "docs/repo/$hooks"
for h in pre-commit pre-push; do
  printf "#!/bin/sh\ncat >&2 <<'MSG'\n%s\nMSG\nexit 1\n" "$refusal" >"docs/repo/$hooks/$h"
  chmod +x "docs/repo/$hooks/$h"
done
git -C docs/repo config --local core.hooksPath "$hooks"
git -C docs/repo config --local remote.origin.pushurl "$pushurl"'

clone_docs_repo_in_volume() {
  local proj="$1"
  docs_repo_configured || return 0
  echo "▸ cloning the docs repository into docs/repo"
  forge_container_env "$DOCS_REPO_URL" || die "the forge credential for '$proj' could not be produced — the reason is above this line."
  (
    export FORGE_HOST_SECRET
    slot_helper_run_online "$proj" sh -s -- \
      "$DOCS_REPO_URL" "$DEFAULT_BRANCH" "$DOCS_PIN_FILE" "$DOCS_REPO_REFUSAL" \
      "$DOCS_REPO_HOOKS" "$DOCS_REPO_PUSHURL" "$DOCS_CLONE_EXCLUDES" <<<"$DOCS_VOLUME_SCRIPT"
  ) || echo "  ! could not set up docs/repo in the volume of '$proj' — the reason is above this line" >&2
}
