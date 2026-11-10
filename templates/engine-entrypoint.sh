#!/usr/bin/env bash
# dev-container-engine template: engine-entrypoint.sh version: 1
# Seeded once by `adopt`: this file is the project's now and is never rewritten.
# Root: create the host key if the volume has none, start sshd, stay up.
# Any other user (one-off `compose run` commands from the engine): run the command.
set -eu
if [ "$(id -u)" != 0 ]; then
  exec "$@"
fi
key=/etc/ssh/keys/ssh_host_ed25519_key
[ -f "$key" ] || ssh-keygen -q -t ed25519 -N "" -f "$key"
mkdir -p /run/sshd
/usr/sbin/sshd -t
/usr/sbin/sshd -e
if [ "$#" -gt 0 ]; then exec "$@"; fi
exec sleep infinity
