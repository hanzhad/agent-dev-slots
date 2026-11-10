#!/bin/sh
# Puts the `dev-container` command on PATH: a symlink to scripts/dev-container-global
# of this clone, in ~/.local/bin or in the directory given with --dir. Safe to run
# again, needs no root, and never edits a shell profile: when the directory is not on
# PATH it prints the line to add. Needs sh, nothing else (Linux, macOS, WSL).
#
#   scripts/install-cli.sh [--dir DIR]       install, or repoint a link to another clone
#   scripts/install-cli.sh --uninstall [--dir DIR]
set -eu

usage() {
  echo "Usage: $0 [--dir DIR] [--uninstall]" >&2
  exit 2
}

die() {
  echo "install-cli: $*" >&2
  exit 1
}

dir=
uninstall=0
while [ $# -gt 0 ]; do
  case $1 in
    --dir)
      [ $# -ge 2 ] || usage
      dir=$2
      shift 2 ;;
    --dir=*)
      dir=${1#--dir=}
      shift ;;
    --uninstall)
      uninstall=1
      shift ;;
    *) usage ;;
  esac
done

[ -n "${HOME:-}" ] && [ "${HOME#/}" != "$HOME" ] || die "HOME must be an absolute path."
shown=
if [ -z "$dir" ]; then
  dir=$HOME/.local/bin
  shown='$HOME/.local/bin'
fi

here=$(cd "$(dirname "$0")" && pwd -P)
target=$here/dev-container-global
name=dev-container

if [ "$uninstall" = 1 ]; then
  link=$dir/$name
  if [ ! -e "$link" ] && [ ! -L "$link" ]; then
    echo "nothing to remove: $link does not exist"
    exit 0
  fi
  if [ -L "$link" ]; then
    case $(readlink "$link") in
      */dev-container-global)
        rm -f "$link"
        echo "removed $link"
        exit 0 ;;
    esac
  fi
  die "$link is not a link to an engine's scripts/dev-container-global, so it was left alone."
fi

[ -f "$target" ] || die "$target is missing — run this from a complete engine clone."
[ -x "$target" ] || die "$target is not executable — run: chmod +x $target"

mkdir -p "$dir"
dir=$(cd "$dir" && pwd -P)
[ -n "$shown" ] || shown=$dir
link=$dir/$name

if [ -L "$link" ]; then
  current=$(readlink "$link")
  if [ "$current" = "$target" ]; then
    echo "already installed: $link -> $target"
  else
    rm -f "$link"
    ln -s "$target" "$link"
    echo "repointed $link (it was a link to $current) -> $target"
  fi
elif [ -e "$link" ]; then
  die "$link exists and is not a link this installer made — look at it, remove it if it is a project's old launcher, or pass another --dir. Nothing was changed."
else
  ln -s "$target" "$link"
  echo "installed $link -> $target"
fi

case ":$PATH:" in
  *":$dir:"*)
    first=$(command -v "$name" 2>/dev/null) || first=
    if [ -n "$first" ] && [ "$first" != "$link" ]; then
      echo "note: another $name comes first on PATH: $first — it is the one that runs, not the link above. Remove it or put $dir earlier."
    fi ;;
  *)
    case ${SHELL:-} in
      */zsh) rc='~/.zshrc' ;;
      */bash) rc='~/.bashrc (~/.bash_profile on macOS)' ;;
      *) rc='~/.profile' ;;
    esac
    echo "$dir is not on your PATH. Add this line to $rc, then open a new shell:"
    printf '    export PATH="%s:$PATH"\n' "$shown" ;;
esac
echo "Check it with: $name doctor"
