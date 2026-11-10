# shellcheck shell=bash disable=SC2034,SC2154

SLOT_BUILD_HASH_ROOT=""
SLOT_BUILD_HASH_VALUE=""

# What goes into an image besides the commit: the files of the compose dir. The
# generated Dockerfile and entrypoint are excluded from git (adopt keeps
# /.devcontainer/ out of the checkout), so HEAD alone does not move when they are
# edited and a restart would reuse the stale image. Files that carry values for a
# running container rather than the build — the env files, the secrets overlays,
# anything named like a secret or a credential — are left out: they are not baked
# in, and a secret must not leave a trace in an image label.
#
# The digest is a pure function of sorted "path blobsha" lines.
compose_dir_digest() {
  git hash-object --stdin
}

# "path blobsha" for every file of the main checkout's compose dir that can end
# up in the image, sorted by path. A compose dir at the repository root lists only
# its own top-level files: everything below it is the project's, and tracked.
compose_dir_listing() {
  local rel="${COMPOSE_DIR:-.devcontainer}" dir f sha
  local -a depth=() files=()
  dir="$repo_root/$rel"
  [[ -d "$dir" ]] || return 0
  [[ "$rel" != "." ]] || depth=(-maxdepth 1)
  while IFS= read -r -d '' f; do
    files+=("${f#./}")
  done < <(cd "$dir" && find . "${depth[@]}" \
    \( -name .git -o -name node_modules \) -prune -o \
    -type f \
    ! -name '.env' ! -name '.env.*' ! -name '*.env' \
    ! -name '*.secrets.yml' ! -name '*.secrets.yaml' \
    ! -iname '*secret*' ! -iname '*credential*' ! -iname '*token*' \
    ! -name '*.pem' ! -name '*.key' ! -name 'id_*' \
    ! -name '.slot-holder' -print0)
  (( ${#files[@]} > 0 )) || return 0
  while IFS= read -r f; do
    sha=$(git -C "$repo_root" hash-object -- "$dir/$f") || return 1
    printf '%s %s\n' "$f" "$sha"
  done < <(printf '%s\n' "${files[@]}" | LC_ALL=C sort)
}

# The commit plus the compose dir's files. Hex, because the overlay writes it as
# a label and refuses anything else.
slot_build_hash() {
  if [[ "$SLOT_BUILD_HASH_ROOT" != "$repo_root" || -z "$SLOT_BUILD_HASH_VALUE" ]]; then
    local head listing
    head=$(git -C "$repo_root" rev-parse HEAD) || return 1
    listing=$(compose_dir_listing) || return 1
    SLOT_BUILD_HASH_VALUE=$(printf '%s\n%s\n' "$head" "$(printf '%s\n' "$listing" | compose_dir_digest)" | compose_dir_digest) || return 1
    SLOT_BUILD_HASH_ROOT=$repo_root
  fi
  echo "$SLOT_BUILD_HASH_VALUE"
}

# The commit the image is built from, for the revision label.
slot_build_revision() {
  git -C "$repo_root" rev-parse HEAD
}

IMAGE_REVISION_LABEL=org.opencontainers.image.revision

# An image is built only from a clean checkout of the default branch, so the
# commit and the compose dir's files (slot_build_hash) are all there is to it.
# The first rule that fails
# is the one named.
refuse_unclean_checkout() {
  local branch
  branch=$(head_branch_of "$repo_root")
  if [[ "$branch" != "$DEFAULT_BRANCH" ]]; then
    [[ -n "$branch" ]] && branch="branch $branch" || branch="no branch (a detached HEAD)"
    die "refusing to build the image — $repo_root is on $branch, and the image is built only from $DEFAULT_BRANCH."
  fi
  if [[ -n "$(git -C "$repo_root" status --porcelain 2>/dev/null)" ]]; then
    die "refusing to build the image — $repo_root is dirty: it has uncommitted changes, and the image is built only from a clean checkout."
  fi
}

# The images of a compose project, found by the label the compose file stamps
# from COMPOSE_PROJECT_NAME. A name prefix cannot tell slot 'foo' from slot
# 'foo-bar': the two read the same once a service name is joined on.
slot_image_names() {
  docker image ls --filter "label=dev-container.compose-project=$1" --format '{{.Repository}}:{{.Tag}}' 2>/dev/null || true
}

slot_image_label() {
  docker image inspect --format '{{ index .Config.Labels "dev-container.build-hash" }}' "$1" 2>/dev/null || true
}

slot_images_current() {
  local proj="$1" want="$2" img
  [[ -n "$want" ]] || return 1
  while IFS= read -r img; do
    [[ -n "$img" ]] || continue
    [[ "$(slot_image_label "$img")" == "$want" ]] || return 1
  done < <(slot_image_names "$proj")
}

slot_image_is_stale() {
  local proj="$1" want="$2"
  [[ -n "$(slot_image_names "$proj")" ]] || return 0
  ! slot_images_current "$proj" "$want"
}

# Leftovers of an earlier build of this slot (an image of a service the
# compose file no longer builds, say) carry the slot label with an old hash.
# They are not this build's output, so they must not fail its check. Only an
# image named <project>-<service> for a service the compose file does NOT build
# is dropped, and only when no container uses it: an image of a service that
# is built stays, so a build that failed to label its image is still caught.
drop_unused_stale_images() {
  local proj="$1" want="$2" img name svc built
  discover_overlay_services "$proj"
  built=" $OVERLAY_BUILDS "
  [[ "$built" != "  " ]] || return 0
  while IFS= read -r img; do
    [[ -n "$img" ]] || continue
    [[ "$(slot_image_label "$img")" == "$want" ]] && continue
    name=${img%%:*}
    [[ "$name" == "$proj"-* ]] || continue
    svc=${name#"$proj"-}
    [[ "$built" != *" $svc "* ]] || continue
    [[ -z "$(docker ps -aq --filter "ancestor=$img" 2>/dev/null)" ]] || continue
    docker image rm "$img" >/dev/null 2>&1 || true
  done < <(slot_image_names "$proj")
}

build_slot_image() {
  local dir="$1" proj="$2" hash
  hash=$(slot_build_hash) || die "cannot read the build hash of $repo_root."
  echo "▸ building a fresh image for '$proj'"
  if ! DEV_CONTAINER_BUILD_HASH="$hash" slot_compose "$dir" "$proj" build --no-cache; then
    die "building the image for '$proj' failed — nothing was started."
  fi
  if [[ -z "$(slot_image_names "$proj")" ]]; then
    die "no image of '$proj' carries the label dev-container.compose-project=$proj — no service of the compose file has a build section for the engine's overlay to label; nothing was started."
  fi
  drop_unused_stale_images "$proj" "$hash"
  if ! slot_images_current "$proj" "$hash"; then
    die "an image of '$proj' does not carry the label dev-container.build-hash=$hash — the build did not apply the engine's overlay labels; nothing was started."
  fi
}

remove_slot_images() {
  local proj="$1" img
  local -a imgs=()
  while IFS= read -r img; do
    [[ -z "$img" ]] || imgs+=("$img")
  done < <(slot_image_names "$proj")
  (( ${#imgs[@]} == 0 )) || docker image rm -f "${imgs[@]}" >/dev/null 2>&1 || true
  imgs=()
  while IFS= read -r img; do
    [[ -z "$img" ]] || imgs+=("$img")
  done < <(slot_image_names "$proj")
  if (( ${#imgs[@]} > 0 )); then
    echo "  ! image(s) of '$proj' remain: ${imgs[*]}" >&2
    return 1
  fi
}
