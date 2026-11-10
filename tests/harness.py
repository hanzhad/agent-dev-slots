
import hashlib
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import tempfile
import threading
import time
import unittest

REPO_ROOT = (
    subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], stdout=subprocess.PIPE, check=True
    )
    .stdout.decode()
    .strip()
)
SCRIPT_REL = "bin/dev-container"
LIB_REL = "lib"
DOCS_REPO_URL = "https://github.com/example/demo.docs.git"
PROJECT_PREFIX = "demo"


def engine_source_lines(root=REPO_ROOT):
    paths = [os.path.join(root, SCRIPT_REL)]
    lib_dir = os.path.join(root, LIB_REL)
    paths += [os.path.join(lib_dir, name) for name in sorted(os.listdir(lib_dir)) if name.endswith(".sh")]
    lines = []
    for path in paths:
        with open(path, encoding="utf-8") as handle:
            lines += handle.read().splitlines()
    return lines


SLOT_PORT_BLOCK = 16
SLOT_COUNT_HELD = 9
DEFAULT_PORT_NAMES = ("SSH_PORT", "NOVNC_PORT", "VITE_DEV_PORT")
OWNER_PORTS = {"SSH_PORT": "1101", "NOVNC_PORT": "1102", "VITE_DEV_PORT": "1103"}
_PORT_FLOOR, _PORT_CEIL = 39000, 40000


def _port_is_free(port):
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("127.0.0.1", port))
    except OSError:
        return False
    finally:
        probe.close()
    return True


def _free_port_base():
    width = SLOT_PORT_BLOCK * SLOT_COUNT_HELD
    span = (_PORT_CEIL - _PORT_FLOOR) // width
    first = os.getpid() % span
    for step in range(span):
        base = _PORT_FLOOR + ((first + step) % span) * width
        if all(_port_is_free(base + offset) for offset in range(width)):
            return base
    raise RuntimeError(
        f"no free {width}-port block between {_PORT_FLOOR} and {_PORT_CEIL} "
        "for this run's slot ports"
    )


_PORT_BASE = _free_port_base()


def slot_port(number, index):
    return _PORT_BASE + number * SLOT_PORT_BLOCK + index


TEST_SSH_PORT = slot_port(1, 0)


def project_env_text(**overrides):
    values = {
        "PROJECT": PROJECT_PREFIX,
        "DEFAULT_BRANCH": "master",
        "SLOT_PORTS": " ".join(DEFAULT_PORT_NAMES),
        "SLOT_FORBIDDEN_PORTS": " ".join(OWNER_PORTS.values()),
        "COMPOSE_FILES": "docker-compose.yml",
    }
    values.update(overrides)
    return "".join(f'{key}="{value}"\n' for key, value in values.items())


DOCKER_STUB = """#!/usr/bin/env bash
# Fake docker for dev-container's tests. Records every invocation, then
# answers just enough of `compose up/down/ps/exec` and plain `docker ps`
# from its own per-project state ($DOCKER_STUB_STATE/<project>.ports, a
# marker file per running project, and <project>.procs for `exec ... ps -eo
# comm`) that acquire/release/restart's own port logic — including the
# allocator's busy-port check — has something real to read, without
# ever reaching an actual daemon. No container, volume or port this process
# touches is real.
set -u
log="${DOCKER_STUB_LOG:-/dev/null}"
state_dir="${DOCKER_STUB_STATE:-/tmp}"

# Real `docker compose` interpolates ${SSH_PORT:-...} etc. from a .env file
# in the invocation's own cwd when the var is not already in the
# environment — every real call site in dev-container `cd`s into
# .devcontainer first. A call that passes no port env vars relies on exactly
# this, so this stub does the same lookup, or its own "container" would
# forget the ports a real one would still remember.
ssh_port="${SSH_PORT:-}"
novnc_port="${NOVNC_PORT:-}"
vite_port="${VITE_DEV_PORT:-}"
web_port="${WEB_PORT:-}"
#
# this is compose's own rule, not a convenience. With --env-file the
# project directory's .env is NOT read; without one, the file read is
# <project directory>/.env, the project directory being --project-directory
# or else the first -f file's directory. The flags before the verb are parsed
# for exactly that, so a slot's planted .env is only ever seen by a call that
# really would have read it. Every compose call is also written, one line, to
# $DOCKER_STUB_COMPOSE_LOG: verb|project dir|WORKSPACE_DIR|--env-file|-f files|marks.
verb_seen="" explicit_env="" proj_dir="" first_file="" files="" prev=""
if [[ "${1:-}" == compose ]]; then
  for arg in "${@:2}"; do
    case "$arg" in up|down|stop|port|ps|exec|run|ls|config) verb_seen="$arg"; break ;; esac
    case "$prev" in
      --env-file) explicit_env="$arg" ;;
      --project-directory) proj_dir="$arg" ;;
      -f) files="${files:+$files,}$arg"; [[ -n "$first_file" ]] || first_file="$arg" ;;
    esac
    prev="$arg"
  done
  if [[ -z "$proj_dir" ]]; then
    if [[ -n "$first_file" ]]; then proj_dir=$(dirname "$first_file"); else proj_dir="$PWD"; fi
  fi
  proj_dir=$(cd "$proj_dir" 2>/dev/null && pwd -P || echo "$proj_dir")
fi
# `config --format json` names the service that mounts the checkout; the
# stand-in's container is called $DOCKER_STUB_SERVICE (default app); a comma
# list names several services that all mount it, and "none" names no service.
# DOCKER_STUB_CONFIG_JSON names a file whose content is the config to print
# instead; DOCKER_STUB_CONFIG_FAIL makes the call refuse.
if [[ "$verb_seen" == config ]]; then
  [[ -z "${DOCKER_STUB_CONFIG_FAIL:-}" ]] || { echo "config refused" >&2; exit 1; }
  [[ -z "${DOCKER_STUB_CONFIG_JSON:-}" ]] || { cat "$DOCKER_STUB_CONFIG_JSON"; exit 0; }
  jq -nc --arg names "${DOCKER_STUB_SERVICE:-app}" \\
    '{services: (($names | split(",") | map(select(. != "none") | {key: ., value: {volumes: [{type: "bind", target: "/workspace"}]}}) | from_entries) + {db: {}})}'
  exit 0
fi
env_file="${explicit_env:-$proj_dir/.env}"
workspace="${WORKSPACE_DIR:-}"
if [[ -f "$env_file" && "${1:-}" == compose ]]; then
  [[ -z "$ssh_port" ]] && ssh_port=$(sed -n 's/^SSH_PORT=//p' "$env_file" | tail -1)
  [[ -z "$novnc_port" ]] && novnc_port=$(sed -n 's/^NOVNC_PORT=//p' "$env_file" | tail -1)
  [[ -z "$vite_port" ]] && vite_port=$(sed -n 's/^VITE_DEV_PORT=//p' "$env_file" | tail -1)
  [[ -z "$web_port" ]] && web_port=$(sed -n 's/^WEB_PORT=//p' "$env_file" | tail -1)
  [[ -n "$workspace" ]] || workspace=$(sed -n 's/^WORKSPACE_DIR=//p' "$env_file" | tail -1)
fi
if [[ "${1:-}" == compose && -n "${DOCKER_STUB_COMPOSE_LOG:-}" ]]; then
  # What the -f files carry, as far as a test cares: any MARK-<word> token in
  # them. A test writes one into the main checkout's compose file and a
  # different one into a slot's, and the call says which it composed from.
  marks=""
  if [[ -n "$files" ]]; then
    IFS=, read -ra _fs <<< "$files"
    marks=$(cat "${_fs[@]}" 2>/dev/null | grep -o 'MARK-[A-Za-z0-9-]*' | sort -u | paste -sd, -)
  fi
  printf '%s|%s|%s|%s|%s|%s\n' "$verb_seen" "$proj_dir" "$workspace" "$explicit_env" "$files" "$marks" >> "$DOCKER_STUB_COMPOSE_LOG"
fi

printf 'ARGS: %s | COMPOSE_PROJECT_NAME=%s SSH_PORT=%s NOVNC_PORT=%s VITE_DEV_PORT=%s WEB_PORT=%s\\n' \\
  "$*" "${COMPOSE_PROJECT_NAME:-}" "$ssh_port" "$novnc_port" "$vite_port" "$web_port" >> "$log"


# create_slot/cmd_restart identify their project via
# COMPOSE_PROJECT_NAME (real docker compose honours either); everything
# that manages an already-existing project (install_lease_key,
# teardown_dir, ...) passes `-p`
# explicitly, which wins when both are present, matching real precedence.
project="${COMPOSE_PROJECT_NAME:-}"
prev=""
for arg in "$@"; do
  [[ "$prev" == "-p" ]] && project="$arg"
  prev="$arg"
done

case "${1:-}" in
  compose)
    verb=""
    for arg in "$@"; do
      # First match only: `exec ... <service> ps -eo comm` carries its own "ps" as
      # a later argument (the command run INSIDE the container), and
      # that must never overwrite the real verb, which is always the first
      # of these five words to appear (right after -f "$cf").
      if [[ -z "$verb" ]]; then
        case "$arg" in
          up|down|stop|port|ps|exec|run|ls|build) verb="$arg" ;;
        esac
      fi
    done
    case "$verb" in
      build)
        # a build leaves one image for the project, carrying the hash the
        # engine passed in, as a compose file that stamps its labels would.
        if [[ -n "$project" ]]; then
          printf '%s' "${DEV_CONTAINER_BUILD_HASH:-}" > "$state_dir/$project.image-hash"
        fi
        exit 0
        ;;
      ls)
        # `docker compose ls --format json` — slot_compose_projects's own
        # machine-wide read (the machine cap of try_acquire_once
        # apply), sourced from the same *.running markers `up`/`down`
        # above already write and remove — never a second, separate state.
        printf '['
        first=1
        for f in "$state_dir"/*.running; do
          [[ -f "$f" ]] || continue
          (( first )) || printf ','
          first=0
          printf '{"Name":"%s"}' "$(basename "$f" .running)"
        done
        printf ']\n'
        exit 0
        ;;
      up)
        # a slow, then optionally failing, `compose up` for one
        # project — create_slot's last slow step. `.hold-up` keeps this call
        # waiting until the test removes it (`.up-held` says it got here);
        # `.fail-up` makes it refuse once let go.
        if [[ -n "$project" && -f "$state_dir/$project.hold-up" ]]; then
          : > "$state_dir/$project.up-held"
          while [[ -f "$state_dir/$project.hold-up" ]]; do sleep 0.1; done
        fi
        [[ -n "$project" && -f "$state_dir/$project.fail-up" ]] && exit 1
        if [[ -n "$project" ]]; then
          {
            echo "SSH_PORT=$ssh_port"
            echo "NOVNC_PORT=$novnc_port"
            echo "VITE_DEV_PORT=$vite_port"
          } > "$state_dir/$project.ports"
          : > "$state_dir/$project.running"
          # what compose would have interpolated into the container's environment
          echo "SLOT_GH_TOKEN=${SLOT_GH_TOKEN-UNSET} GH_TOKEN=${GH_TOKEN-UNSET}" >> "$state_dir/$project.up-env"
          # the compose files an `up` was given are what a real
          # container's config_files label records, and what `list` reads
          # back to say which slots run with the secrets overlay.
          if [[ "$*" == *docker-compose.secrets.yml* ]]; then
            : > "$state_dir/$project.secrets"
          else
            rm -f "$state_dir/$project.secrets"
          fi
        fi
        exit 0
        ;;
      down|stop)
        # what a `down` can see of the lease directory at the moment
        # it runs, so a test can tell the lease outlives the teardown.
        if [[ "$verb" == down && -n "${DOCKER_STUB_LEASE_DIR:-}" ]]; then
          printf 'DOWN-LEASES %s: %s\n' "$project" "$(echo $(ls "$DOCKER_STUB_LEASE_DIR" 2>/dev/null))" >> "$log"
        fi
        # A stopped container publishes nothing, same as one taken down.
        [[ -n "$project" ]] && rm -f "$state_dir/$project.ports" "$state_dir/$project.running"
        exit 0
        ;;
      ps)
        [[ -n "$project" && -f "$state_dir/$project.running" ]] && echo "$project"
        exit 0
        ;;
      exec|run)
        # container_ps's `exec -T -u node app ps -eo comm | tail -n +2` — a
        # header line plus one process name per line, read back from
        # $state_dir/<project>.procs (the earlier list-state tests write it).
        # Nothing printed at all unless the project is marked running, same
        # as a real exec into a container that isn't there.
        #
        # A `.psfail` marker makes the exec itself refuse — nonzero,
        # nothing on stdout — the way a real one does into a container that
        # is up but loaded. Checked before `.running`, because that is the
        # case where "" must not read as "nothing is running in there".
        if [[ "$verb_seen" == exec && -n "$project" && -f "$state_dir/$project.psfail" ]]; then
          exit 1
        fi

        # Everything after the literal "app" token is the command `exec`
        # itself runs inside the container. Three shapes below are the
        # script's own internal probes — container_ps, wait_for_sshd, the
        # authorized_keys.d write — and stay answered from state files, same
        # as always. Anything else is `run`'s own command: genuinely
        # executed right here, so a test gets the command's real stdout and
        # real exit code back, standing in for "inside the container" well
        # enough for a command with no container-only side effect.
        cmd=() seen_app=0 entrypoint="" prev=""
        for arg in "$@"; do
          if (( seen_app )); then
            cmd+=("$arg")
          elif [[ "$arg" == "${DOCKER_STUB_SERVICE:-app}" ]]; then
            seen_app=1
          elif [[ "$prev" == "--entrypoint" ]]; then
            entrypoint="$arg"
          fi
          prev="$arg"
        done
        if [[ -n "$entrypoint" && "${cmd[0]:-}" != "$entrypoint" ]]; then
          cmd=("$entrypoint" "${cmd[@]}")
        fi

        case "${cmd[*]-}" in
          "ps -eo comm")
            if [[ -n "$project" && -f "$state_dir/$project.running" ]]; then
              echo "COMMAND"
              [[ -f "$state_dir/$project.procs" ]] && cat "$state_dir/$project.procs"
            fi
            exit 0
            ;;
          "pgrep sshd")
            [[ -n "$project" && -f "$state_dir/$project.running" ]] && exit 0
            exit 1
            ;;
          "sh -c cat > /etc/ssh/authorized_keys.d/node && chmod 0644 /etc/ssh/authorized_keys.d/node")
            cat >/dev/null
            exit 0
            ;;
          "find /tmp -mindepth 1 -delete")
            # finish_acquire's own wipe. `.wipe-tmp-fail` (set by a
            # test) makes it refuse, the same shape `.psfail` already gives
            # `ps -eo comm` above. Otherwise it removes `<project>.tmpfile`,
            # this stub's stand-in for "something a previous lease left in
            # /tmp" — there being no real /tmp inside a fake container to
            # empty.
            if [[ -n "$project" && -f "$state_dir/$project.wipe-tmp-fail" ]]; then
              exit 1
            fi
            [[ -n "$project" ]] && rm -f "$state_dir/$project.tmpfile"
            exit 0
            ;;
          ".devcontainer/node-modules-drift-check.sh")
            # acquire's own cheap drift signal. `.lockfile-drift` (set
            # by a test) says the stamp and the lockfile disagree; its
            # absence says they match — no real pnpm-lock.yaml/
            # node_modules needed for the check itself, the same shape
            # `.psfail` gives `ps -eo comm` above. `.drift-check-fail` makes
            # the exec itself refuse — nonzero, nothing on stdout; acquire
            # must warn and carry on.
            if [[ -n "$project" && -f "$state_dir/$project.drift-check-fail" ]]; then
              exit 1
            fi
            if [[ -n "$project" && -f "$state_dir/$project.lockfile-drift" ]]; then
              echo drift
            else
              echo match
            fi
            exit 0
            ;;
          "rm -rf --"*)
            # reset_slot's own retry, through the container as root,
            # for exactly the paths a plain `git clean -fd` on the host
            # could not remove. `.cleanfail` (set by a test) makes the
            # retry itself refuse too, the same shape `.wipe-tmp-fail`/
            # `.psfail` give the other exec probes above; otherwise this
            # simulates root's own privilege by clearing whatever
            # permission bit stood in the HOST's way before removing the
            # path for real, so a test can tell the two outcomes apart.
            if [[ -n "$project" && -f "$state_dir/$project.cleanfail" ]]; then
              exit 1
            fi
            target="${cmd[-1]}"
            chmod -R u+w -- "$target" 2>/dev/null
            rm -rf -- "$target"
            exit $?
            ;;
          "sh -c command -v gh")
            # provision_slot's probe for the forge CLI inside the slot. `.no-gh`
            # (set by a test), or DOCKER_STUB_NO_GH in the engine's environment, answers
            # the way a missing binary does.
            [[ -n "${DOCKER_STUB_NO_GH:-}" ]] && exit 1
            [[ -n "$project" && -f "$state_dir/$project.no-gh" ]] && exit 1
            exit 0
            ;;
          *dev-container-provision*)
            # provision_slot's exec as root. Nothing here touches the host's own
            # /etc or home: the script runs for real, as the current user, against a
            # sandbox that stands in for the container ($state_dir/<project>.container:
            # home/ and gitconfig, the file `git config --system` writes), so a test
            # reads the result back. Each call is logged to <project>.provision-calls.
            # `.provision-fail` makes the exec refuse, as one into a container that is
            # not there does.
            [[ -n "$project" ]] || exit 1
            as_user="" pv=""
            for arg in "$@"; do
              [[ "$pv" == "-u" ]] && as_user="$arg"
              pv="$arg"
            done
            printf 'user=%s home=%s owner=%s\n' "$as_user" "${cmd[4]:-}" "${cmd[5]:-}" >> "$state_dir/$project.provision-calls"
            [[ ! -f "$state_dir/$project.provision-fail" ]] || { echo "provision exec refused" >&2; exit 1; }
            sandbox="$state_dir/$project.container"
            mkdir -p "$sandbox/home"
            env -u GIT_CONFIG_NOSYSTEM GIT_CONFIG_SYSTEM="$sandbox/gitconfig" sh -c "${cmd[2]}" dev-container-provision "$sandbox/home" "$(id -un)" "${cmd[6]:-}" "${cmd[7]:-}" "${cmd[8]:-}" "${cmd[9]:-}"
            exit $?
            ;;
          *dev-container-clone*)
            # The volume clone step during create_slot.
            vol="${SLOT_WORKSPACE_VOLUME:-${project}-workspace}"
            voldir="$state_dir/volumes/$vol"
            mkdir -p "$voldir"
            src=""
            prev=""
            for a in "$@"; do
              if [[ "$prev" == "-v" && "$a" == *:/src* ]]; then
                src="${a%%:/src*}"
              fi
              prev="$a"
            done
            src="${src:-$proj_dir}"
            if [[ -n "$src" && -d "$src" ]]; then
              if [[ ! -d "$voldir/.git" ]]; then
                rm -rf "$voldir"
                mkdir -p "$voldir"
                git clone --quiet "$src" "$voldir" 2>/dev/null || true
              fi
              origin=""
              for (( i=0; i<${#cmd[@]}; i++ )); do
                if [[ "${cmd[i]}" == "/workspace" && $((i+1)) -lt ${#cmd[@]} ]]; then
                  origin="${cmd[i+1]}"
                  break
                fi
              done
              if [[ -n "$origin" && -d "$voldir/.git" ]]; then
                git -C "$voldir" remote set-url origin "$origin" 2>/dev/null || true
              fi
            fi
            exit 0
            ;;
          *)
            (( ${#cmd[@]} > 0 )) || exit 0
            if [[ -n "${WORKSPACE_DIR:-}" ]]; then
              if [[ -d "$WORKSPACE_DIR" ]]; then
                cd "$WORKSPACE_DIR"
              elif [[ -d "$state_dir/volumes/${SLOT_WORKSPACE_VOLUME:-${project}-workspace}" ]]; then
                cd "$state_dir/volumes/${SLOT_WORKSPACE_VOLUME:-${project}-workspace}"
              fi
            fi
            exec "${cmd[@]}"
            ;;
        esac
        ;;
      *) exit 0 ;;
    esac
    ;;
  image)
    # the image a build left, found by the compose-project label filter
    case "${2:-}" in
      ls)
        for arg in "$@"; do
          case "$arg" in
            label=dev-container.compose-project=*)
              p="${arg#label=dev-container.compose-project=}"
              [[ -f "$state_dir/$p.image-hash" ]] && echo "$p-app:latest"
              ;;
          esac
        done
        ;;
      inspect)
        ref="${@: -1}"
        p="${ref%-app:latest}"
        [[ -f "$state_dir/$p.image-hash" ]] && cat "$state_dir/$p.image-hash" && echo
        ;;
    esac
    exit 0
    ;;
  ps)
    # Plain `docker ps --format '{{.Ports}}'` (port_in_use's own check) —
    # every project's published ports, one line each, shaped for its
    # `grep -q ":PORT->"`. A `docker ps -a --filter ...` (the orphan-sweep
    # queries, not exercised by these tests) gets nothing back, which reads
    # as "no such project" to every caller that makes one — the correct
    # answer here, since nothing in this stub's state models a foreign
    # project's compose labels.
    # the one filtered query that is answered — a running project's
    # compose.project.config_files label, which names the secrets overlay
    # when the project was brought up with it.
    case "$*" in
      *config_files*)
        for arg in "$@"; do
          case "$arg" in
            label=com.docker.compose.project=*)
              p="${arg#label=com.docker.compose.project=}"
              if [[ -f "$state_dir/$p.running" && -f "$state_dir/$p.secrets" ]]; then
                echo "/slot/.devcontainer/docker-compose.yml,/slot/.devcontainer/docker-compose.secrets.yml"
                # A project with many containers: the overlay is on the first
                # line and the rest overflows a pipe buffer, so a consumer
                # that quits on the first match kills this writer.
                if [[ -f "$state_dir/flood-config-files" ]]; then
                  for _ in $(seq 1 4000); do
                    echo "/slot/.devcontainer/docker-compose.yml"
                  done
                fi
              elif [[ -f "$state_dir/$p.running" ]]; then
                echo "/slot/.devcontainer/docker-compose.yml"
              fi
              ;;
          esac
        done
        exit 0
        ;;
    esac
    case "$*" in *--filter*) exit 0 ;; esac
    for f in "$state_dir"/*.ports; do
      [[ -f "$f" ]] || continue
      SSH_PORT="" NOVNC_PORT="" VITE_DEV_PORT=""
      # shellcheck disable=SC1090
      source "$f"
      line=""
      [[ -n "$SSH_PORT" ]] && line="${line}127.0.0.1:$SSH_PORT->22/tcp, "
      [[ -n "$NOVNC_PORT" ]] && line="${line}127.0.0.1:$NOVNC_PORT->80/tcp, "
      [[ -n "$VITE_DEV_PORT" ]] && line="${line}127.0.0.1:$VITE_DEV_PORT->81/tcp"
      echo "$line"
    done
    exit 0
    ;;
  volume)
    vols="$state_dir/volumes"
    mkdir -p "$vols"
    case "${2:-}" in
      create)
        shift 2
        vlabels="" vname=""
        while [[ $# -gt 0 ]]; do
          case "$1" in
            --label|-l) vlabels+="$2"$'\n'; shift 2 ;;
            --label=*) vlabels+="${1#--label=}"$'\n'; shift ;;
            --driver|-d|--opt|-o|--name) shift 2 ;;
            -*) shift ;;
            *) vname="$1"; shift ;;
          esac
        done
        [[ -n "$vname" ]] || exit 1
        mkdir -p "$vols/$vname"
        [[ -z "$vlabels" ]] || printf '%s' "$vlabels" > "$vols/$vname.labels"
        echo "$vname"
        exit 0
        ;;
      inspect)
        shift 2
        vfmt="" vname=""
        while [[ $# -gt 0 ]]; do
          case "$1" in
            -f|--format) vfmt="$2"; shift 2 ;;
            -*) shift ;;
            *) vname="$1"; shift ;;
          esac
        done
        [[ -n "$vname" && -d "$vols/$vname" ]] || { echo "Error: no such volume: $vname" >&2; exit 1; }
        if [[ -n "$vfmt" ]]; then
          if [[ "$vfmt" == *"index .Labels"* ]]; then
            lbl=$(printf '%s' "$vfmt" | cut -d'"' -f2)
            val=""
            if [[ -f "$vols/$vname.labels" ]]; then
              val=$(grep "^${lbl}=" "$vols/$vname.labels" 2>/dev/null | head -1 | cut -d= -f2-)
            fi
            echo "$val"
            exit 0
          elif [[ "$vfmt" == *"json"* || "$vfmt" == *".Labels"* ]]; then
            obj=""
            if [[ -f "$vols/$vname.labels" ]]; then
              first=1
              while IFS='=' read -r k v; do
                [[ -n "$k" ]] || continue
                (( first )) || obj+=","
                first=0
                obj+="\"$k\":\"$v\""
              done < "$vols/$vname.labels"
            fi
            printf '{%s}\n' "$obj"
            exit 0
          fi
        fi
        exit 0
        ;;
      rm)
        shift 2
        for vname in "$@"; do
          [[ "$vname" == -* ]] && continue
          rm -rf "$vols/$vname" "$vols/$vname.labels"
        done
        exit 0
        ;;
      ls)
        shift 2
        vfmt=""
        filters=()
        while [[ $# -gt 0 ]]; do
          case "$1" in
            --format) vfmt="$2"; shift 2 ;;
            --filter) filters+=("$2"); shift 2 ;;
            *) shift ;;
          esac
        done
        for vdir in "$vols"/*; do
          [[ -d "$vdir" ]] || continue
          vn=$(basename "$vdir")
          match=1
          for filt in "${filters[@]}"; do
            if [[ "$filt" == label=* ]]; then
              req="${filt#label=}"
              if ! grep -q "^${req}$" "$vols/$vn.labels" 2>/dev/null; then
                match=0
                break
              fi
            fi
          done
          if (( match )); then
            if [[ "$vfmt" == *"{{.Name}}"* ]]; then
              echo "$vn"
            else
              echo "$vn"
            fi
          fi
        done
        exit 0
        ;;
      *) exit 0 ;;
    esac
    ;;
  run)
    shift
    vol=""
    cmd=()
    while [[ $# -gt 0 ]]; do
      case "$1" in
        -v|--volume)
          if [[ "$2" == *:/workspace* ]]; then
            vol="${2%%:/workspace*}"
          fi
          shift 2 ;;
        -v*)
          varg="${1#-v}"
          if [[ "$varg" == *:/workspace* ]]; then
            vol="${varg%%:/workspace*}"
          fi
          shift ;;
        --volume=*)
          varg="${1#--volume=}"
          if [[ "$varg" == *:/workspace* ]]; then
            vol="${varg%%:/workspace*}"
          fi
          shift ;;
        -w|--workdir) shift 2 ;;
        --workdir=*) shift ;;
        --user|-u) shift 2 ;;
        --user=*|-u=*) shift ;;
        --network|--cap-drop|--security-opt) shift 2 ;;
        --network=*|--cap-drop=*|--security-opt=*) shift ;;
        --rm|-i|-t) shift ;;
        -*) shift ;;
        *)
          shift
          cmd=("$@")
          break ;;
      esac
    done
    if [[ -n "$vol" ]]; then
      mkdir -p "$state_dir/volumes/$vol"
      cd "$state_dir/volumes/$vol" || exit 1
    fi
    if (( ${#cmd[@]} > 0 )); then
      exec "${cmd[@]}"
    fi
    exit 0
    ;;
  *) exit 0 ;;
esac
"""

GIT_STUB = """#!/usr/bin/env bash
# Fake `git` for dev-container's tests — passes every invocation straight to
# the real binary EXCEPT cloning the docs repository, which a fresh slot
# clones from a hardcoded public URL unconditionally. Redirected here to a
# local repo the fixture builds, so a fresh-slot `acquire`'s tests need no
# network and no credential — every other git call (rev-parse,
# remote, the fixture's own local clone, config --local,
# worktree/log/status/stash, fetch/checkout/reset/clean) is untouched, real
# git, real exit codes.
set -u
real_git="${REAL_GIT_BIN:?REAL_GIT_BIN must be set by the harness}"
if [[ "${1:-}" == clone ]]; then
  args=()
  for arg in "$@"; do
    if [[ "$arg" == "%(docs_repo_url)s" ]]; then
      args+=("${DOCS_REPO_STUB_PATH:?DOCS_REPO_STUB_PATH must be set by the harness}")
    else
      args+=("$arg")
    fi
  done
  exec "$real_git" "${args[@]}"
fi
# every invocation this stub sees, logged with whatever
# GIT_OPTIONAL_LOCKS the script under test set for it — a fetch that never
# reached the fixture's own unreachable origin looks the same, from outside,
# as one never attempted at all, and that is exactly the distinction some of
# these tests need to make.
if [[ -n "${GIT_STUB_CALL_LOG:-}" ]]; then
  printf 'GIT_OPTIONAL_LOCKS=%%s %%s\\n' "${GIT_OPTIONAL_LOCKS:-}" "$*" >> "$GIT_STUB_CALL_LOG"
fi
exec "$real_git" "$@"
""" % {"docs_repo_url": DOCS_REPO_URL}


INSTALL_STUB = r"""#!/usr/bin/env bash
set -euo pipefail
state_dir="${DOCKER_STUB_STATE:-/tmp}"
project="${COMPOSE_PROJECT_NAME:?install.sh stand-in: no COMPOSE_PROJECT_NAME}"
[[ -n "${WORKSPACE_DIR:-}" ]] || { echo "install.sh stand-in: no WORKSPACE_DIR" >&2; exit 2; }
echo "$WORKSPACE_DIR" >> "$state_dir/$project.install-calls"
calls=$(wc -l < "$state_dir/$project.install-calls")
if [[ -f "$state_dir/$project.install-fail-from" ]] && (( calls >= $(cat "$state_dir/$project.install-fail-from") )); then
  echo "install.sh stand-in: failing call $calls" >&2
  exit 1
fi
"""


class DevContainerHarness:

    def __init__(self):
        self.tmp = tempfile.mkdtemp(prefix="dev-container-test-")
        self.fixture_repo = os.path.join(self.tmp, "fixture-repo")
        self.docker_log = os.path.join(self.tmp, "docker.log")
        self.git_call_log = os.path.join(self.tmp, "git-calls.log")
        self.compose_log = os.path.join(self.tmp, "compose-calls.log")
        self.state_dir = os.path.join(self.tmp, "dev-container-state")
        self.docker_state = os.path.join(self.tmp, "docker-state")
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        os.makedirs(self.state_dir)
        os.makedirs(self.docker_state)
        self.real_git_bin = shutil.which("git")
        assert self.real_git_bin, "no real git on PATH — cannot build the stub"
        self._build_stub_bin()
        self._build_docs_repo_stub()

    def _write_stub(self, name, content):
        path = os.path.join(self.stub_bin, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(content)
        st = os.stat(path)
        os.chmod(path, st.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    def _build_stub_bin(self):
        self.stub_bin = os.path.join(self.tmp, "stub-bin")
        os.makedirs(self.stub_bin)
        self._write_stub("docker", DOCKER_STUB)
        self._write_stub("git", GIT_STUB)

    def _build_docs_repo_stub(self):
        self.docs_repo_stub = os.path.join(self.tmp, "docs-repo-stub")
        os.makedirs(self.docs_repo_stub)
        run = lambda *args: subprocess.run(
            [self.real_git_bin, "-C", self.docs_repo_stub, *args],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        run("init", "-q")
        run("config", "user.email", "fixture@example.com")
        run("config", "user.name", "fixture")
        with open(os.path.join(self.docs_repo_stub, "README.md"), "w", encoding="utf-8") as handle:
            handle.write("docs repo stub\n")
        run("add", "-A")
        run("commit", "-q", "-m", "fixture")

    def build_fixture_repo(self, with_parent_env=None, project_env=None):
        os.makedirs(os.path.join(self.fixture_repo, "bin"))
        os.makedirs(os.path.join(self.fixture_repo, ".devcontainer"))
        os.makedirs(os.path.join(self.fixture_repo, ".local"))
        shutil.copyfile(
            os.path.join(REPO_ROOT, SCRIPT_REL),
            os.path.join(self.fixture_repo, SCRIPT_REL),
        )
        shutil.copytree(
            os.path.join(REPO_ROOT, "templates"),
            os.path.join(self.fixture_repo, "templates"),
        )
        shutil.copytree(
            os.path.join(REPO_ROOT, LIB_REL),
            os.path.join(self.fixture_repo, LIB_REL),
        )
        st = os.stat(os.path.join(self.fixture_repo, SCRIPT_REL))
        os.chmod(os.path.join(self.fixture_repo, SCRIPT_REL), st.st_mode | stat.S_IEXEC)
        with open(
            os.path.join(self.fixture_repo, ".gitignore"), "w", encoding="utf-8"
        ) as handle:
            handle.write(".env\n.slot-holder\ndocs/*\n")
        with open(
            os.path.join(self.fixture_repo, ".devcontainer", "docker-compose.yml"),
            "w",
            encoding="utf-8",
        ) as handle:
            handle.write("# fixture placeholder, unparsed — docker is stubbed\n")
        with open(os.path.join(self.fixture_repo, ".local", ".gitkeep"), "w", encoding="utf-8"):
            pass
        install_stub = os.path.join(self.fixture_repo, ".devcontainer", "install.sh")
        with open(install_stub, "w", encoding="utf-8") as handle:
            handle.write(INSTALL_STUB)
        os.chmod(install_stub, 0o755)
        if with_parent_env is not None:
            with open(
                os.path.join(self.fixture_repo, ".devcontainer", ".env"), "w", encoding="utf-8"
            ) as handle:
                handle.write(with_parent_env)
            os.chmod(os.path.join(self.fixture_repo, ".devcontainer", ".env"), 0o600)
        if project_env is None:
            project_env = (
                f'PROJECT="{PROJECT_PREFIX}"\n'
                'DEFAULT_BRANCH="master"\n'
                f'SLOT_PORTS="{" ".join(DEFAULT_PORT_NAMES)}"\n'
                'COMPOSE_FILES="docker-compose.yml"\n'
            )
        if project_env:
            with open(os.path.join(self.fixture_repo, "project.env"), "w", encoding="utf-8") as handle:
                handle.write(project_env)

        run = lambda *args: subprocess.run(
            ["git", "-C", self.fixture_repo, *args], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        run("init", "-q")
        run("config", "user.email", "fixture@example.com")
        run("config", "user.name", "fixture")
        run("add", "-A")
        run("commit", "-q", "-m", "fixture")
        run("remote", "add", "origin", "https://example.invalid/fixture.git")

    @property
    def script(self):
        return os.path.join(self.fixture_repo, SCRIPT_REL)

    @property
    def project_prefix(self):
        return PROJECT_PREFIX

    @property
    def project_key(self):
        return hashlib.sha256(os.path.realpath(self.fixture_repo).encode()).hexdigest()[:8]

    @property
    def volumes_dir(self):
        return os.path.join(self.docker_state, "volumes")

    def volume_name(self, name):
        return f"{self.project_prefix}-{name}-workspace"

    def volume_exists(self, name):
        vol = self.volume_name(name) if not name.endswith("-workspace") else name
        return os.path.isdir(os.path.join(self.volumes_dir, vol))

    def volume_names(self):
        if not os.path.isdir(self.volumes_dir):
            return []
        return sorted(
            entry for entry in os.listdir(self.volumes_dir)
            if os.path.isdir(os.path.join(self.volumes_dir, entry)) and entry.endswith("-workspace")
        )

    def volume_labels(self, name):
        vol = self.volume_name(name) if not name.endswith("-workspace") else name
        path = os.path.join(self.volumes_dir, vol + ".labels")
        labels = {}
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if "=" in line:
                        k, v = line.split("=", 1)
                        labels[k] = v
        return labels

    def write_volume_labels(self, name, labels):
        vol = self.volume_name(name) if not name.endswith("-workspace") else name
        os.makedirs(os.path.join(self.volumes_dir, vol), exist_ok=True)
        path = os.path.join(self.volumes_dir, vol + ".labels")
        with open(path, "w", encoding="utf-8") as handle:
            for k, v in labels.items():
                handle.write(f"{k}={v}\n")

    @property
    def slots_dir(self):
        return os.path.join(self.tmp, "dev-containers")

    def slot_dir(self, name):
        vol_path = os.path.join(self.volumes_dir, self.volume_name(name))
        if os.path.isdir(vol_path):
            return vol_path
        if os.path.isdir(self.volumes_dir):
            for entry in os.listdir(self.volumes_dir):
                if (entry == name or entry.endswith(f"-{name}-workspace") or entry == f"{name}-workspace") and os.path.isdir(os.path.join(self.volumes_dir, entry)):
                    return os.path.join(self.volumes_dir, entry)
        return os.path.join(self.slots_dir, name)

    def make_bare_slot(
        self, name, env_content=None, ports_content=None, local_identity=None, built=True, legacy_env_content=None
    ):
        slot_dir = self.slot_dir(name)
        devcontainer = os.path.join(slot_dir, ".devcontainer")
        os.makedirs(devcontainer)
        with open(
            os.path.join(devcontainer, "docker-compose.yml"), "w", encoding="utf-8"
        ) as handle:
            handle.write("# fixture placeholder, unparsed — docker is stubbed\n")
        if env_content is not None:
            os.makedirs(os.path.dirname(self.env_path(name)), exist_ok=True)
            with open(self.env_path(name), "w", encoding="utf-8") as handle:
                handle.write(env_content)
        if legacy_env_content is not None:
            with open(os.path.join(devcontainer, ".env"), "w", encoding="utf-8") as handle:
                handle.write(legacy_env_content)
        if ports_content is not None:
            os.makedirs(os.path.dirname(self.ports_path(name)), exist_ok=True)
            with open(self.ports_path(name), "w", encoding="utf-8") as handle:
                handle.write(ports_content)
        if built:
            os.makedirs(os.path.join(slot_dir, "docs", "repo", ".git"), exist_ok=True)
        subprocess.run(
            ["git", "-C", slot_dir, "init", "-q"], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        subprocess.run(
            ["git", "-C", slot_dir, "remote", "add", "origin", "https://example.invalid/fixture.git"],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        if local_identity is not None:
            name_, email_ = local_identity
            subprocess.run(["git", "-C", slot_dir, "config", "--local", "user.name", name_], check=True)
            subprocess.run(["git", "-C", slot_dir, "config", "--local", "user.email", email_], check=True)

    def checkout_identity(self, name):
        result = subprocess.run(
            ["git", "-C", self.slot_dir(name), "config", "--local", "user.name"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        return result.stdout.decode().strip()

    def run(self, *args, port_overrides=None, input=None):
        return subprocess.run(
            ["bash", self.script, *args],
            cwd=self.tmp,
            env=self._env(port_overrides),
            input=input,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=120,
        )

    def spawn(self, *args, port_overrides=None, log=None):
        out = err = subprocess.PIPE
        if log is not None:
            out = open(log, "w", encoding="utf-8")
            err = subprocess.STDOUT
        proc = subprocess.Popen(
            ["bash", self.script, *args],
            cwd=self.tmp,
            env=self._env(port_overrides),
            stdout=out,
            stderr=err,
        )
        if log is not None:
            out.close()
        return proc

    def hold_compose_up(self, name, then_fail=False):
        project = f"{PROJECT_PREFIX}-{name}"
        with open(os.path.join(self.docker_state, f"{project}.hold-up"), "w", encoding="utf-8"):
            pass
        if then_fail:
            with open(os.path.join(self.docker_state, f"{project}.fail-up"), "w", encoding="utf-8"):
                pass

    def wait_until_compose_up_held(self, name, timeout=60):
        project = f"{PROJECT_PREFIX}-{name}"
        marker = os.path.join(self.docker_state, f"{project}.up-held")
        deadline = time.monotonic() + timeout
        while not os.path.isfile(marker):
            if time.monotonic() > deadline:
                raise AssertionError(f"compose up for {project} never started")
            time.sleep(0.1)

    def let_compose_up_go(self, name):
        project = f"{PROJECT_PREFIX}-{name}"
        path = os.path.join(self.docker_state, f"{project}.hold-up")
        if os.path.isfile(path):
            os.remove(path)

    def _env(self, port_overrides):
        env = dict(os.environ)
        env.pop("CLAUDE_CODE_HOST_SESSION_ID", None)
        if getattr(self, "session_id", None):
            env["CLAUDE_CODE_HOST_SESSION_ID"] = self.session_id
        env["PATH"] = self.stub_bin + os.pathsep + env.get("PATH", "")
        env["DOCKER_STUB_LOG"] = self.docker_log
        env["DOCKER_STUB_STATE"] = self.docker_state
        env["REAL_GIT_BIN"] = self.real_git_bin
        env["DOCS_REPO_STUB_PATH"] = self.docs_repo_stub
        env["GIT_STUB_CALL_LOG"] = self.git_call_log
        env["DOCKER_STUB_COMPOSE_LOG"] = self.compose_log
        env["DOCS_REPO_URL"] = self.docs_repo_stub
        env["DEV_CONTAINER_HOME"] = self.state_dir
        env["DEV_CONTAINER_PORT_BASE"] = str(_PORT_BASE)
        env["HOME"] = self.home
        env["DOCKER_STUB_LEASE_DIR"] = os.path.join(self.state_dir, "leases")
        env["DEV_CONTAINER_DOCKERENV_PATH"] = os.path.join(self.tmp, "not-a-container")
        # The engine takes its repo root from the directory its caller passes, under
        # the one projects root it was installed with.
        env["DEV_CONTAINER_PROJECTS_ROOT"] = self.tmp
        env["DEV_CONTAINER_CALLER_PWD"] = self.fixture_repo
        if port_overrides:
            env.update(port_overrides)
        return env

    def run_acquire(self, holder="test", owner=None):
        args = ["acquire", "--for", holder]
        if owner is not None:
            args += ["--as", owner]
        return self.run(*args)

    def acquired_slot_name(self, result):
        stdout = result.stdout.decode()
        match = re.search(r'"id": "([^"]+)"', stdout)
        assert match, stdout
        slot_id = match.group(1)
        prefix = PROJECT_PREFIX + "-"
        assert slot_id.startswith(prefix), slot_id
        return slot_id[len(prefix):]

    def lease_path(self, name):
        return os.path.join(self.state_dir, "leases", f"{PROJECT_PREFIX}-{name}.json")

    def parked_path(self, name):
        return os.path.join(self.state_dir, "parked", f"{PROJECT_PREFIX}-{name}.json")

    def holder_path(self, name):
        return os.path.join(self.slot_dir(name), ".devcontainer", ".slot-holder")

    def free_slot(self, name, stopped=False):
        lease = self.lease_path(name)
        with open(lease, encoding="utf-8") as handle:
            key = json.load(handle).get("key", "")
        doomed = [lease, self.holder_path(name)]
        if key.startswith(self.tmp):
            doomed += [key, key + ".pub"]
        for path in doomed:
            if os.path.exists(path):
                os.remove(path)
        if stopped:
            self.mark_stopped(name)

    def read_holder(self, name):
        with open(self.holder_path(name), encoding="utf-8") as handle:
            values = {}
            for line in handle:
                if "=" in line:
                    key, _, value = line.rstrip("\n").partition("=")
                    values[key] = value
            return values

    def break_published_port(self, name, key):
        project = f"{PROJECT_PREFIX}-{name}"
        path = os.path.join(self.docker_state, f"{project}.ports")
        values = {}
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as handle:
                for line in handle:
                    if "=" in line:
                        k, _, v = line.strip().partition("=")
                        values[k] = v
        values[key] = ""
        with open(path, "w", encoding="utf-8") as handle:
            for k, v in values.items():
                handle.write(f"{k}={v}\n")

    def mark_running(self, name):
        project = f"{PROJECT_PREFIX}-{name}"
        path = os.path.join(self.docker_state, f"{project}.running")
        with open(path, "w", encoding="utf-8"):
            pass

    def mark_stopped(self, name):
        project = f"{PROJECT_PREFIX}-{name}"
        path = os.path.join(self.docker_state, f"{project}.running")
        if os.path.isfile(path):
            os.remove(path)

    def bundle_head_sha(self, name):
        project = f"{PROJECT_PREFIX}-{name}"
        bundle_dir = os.path.join(self.state_dir, "rescue")
        bundles = [
            f for f in os.listdir(bundle_dir)
            if f.startswith(project + "-") and f.endswith(".bundle")
        ] if os.path.isdir(bundle_dir) else []
        assert bundles, f"no bundle was written for '{name}'"
        heads = subprocess.run(
            [self.real_git_bin, "bundle", "list-heads", os.path.join(bundle_dir, bundles[0])],
            stdout=subprocess.PIPE, check=True,
        ).stdout.decode()
        return heads.split()[0]

    def bundle_head_file_list(self, name):
        sha = self.bundle_head_sha(name)
        return subprocess.run(
            [self.real_git_bin, "-C", self.slot_dir(name), "show", "--name-only", "--format=", sha],
            stdout=subprocess.PIPE, check=True,
        ).stdout.decode()

    def set_container_procs(self, name, procs):
        project = f"{PROJECT_PREFIX}-{name}"
        path = os.path.join(self.docker_state, f"{project}.procs")
        with open(path, "w", encoding="utf-8") as handle:
            for proc in procs:
                handle.write(f"{proc}\n")

    def fail_container_ps(self, name):
        project = f"{PROJECT_PREFIX}-{name}"
        path = os.path.join(self.docker_state, f"{project}.psfail")
        with open(path, "w", encoding="utf-8"):
            pass

    def env_path(self, name):
        return os.path.join(self.state_dir, "env", f"{PROJECT_PREFIX}-{name}.env")

    def read_env(self, name):
        path = self.env_path(name)
        assert os.path.isfile(path), f"no env file for '{name}' under $DEV_CONTAINER_HOME/env/"
        values = {}
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                if "=" in line:
                    key, _, value = line.strip().partition("=")
                    values[key] = value
        return values

    def ports_path(self, name):
        return os.path.join(self.state_dir, "ports", f"{PROJECT_PREFIX}-{name}.ports")

    def read_ports(self, name):
        path = self.ports_path(name)
        values = {}
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                if "=" in line:
                    key, _, value = line.strip().partition("=")
                    values[key] = value
        return values

    def compose_calls(self):
        if not os.path.isfile(self.compose_log):
            return []
        calls = []
        with open(self.compose_log, encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                verb, project_dir, workspace, env_file, files, marks = line.rstrip("\n").split("|", 5)
                calls.append({
                    "verb": verb,
                    "project_dir": project_dir,
                    "workspace": workspace,
                    "env_file": env_file,
                    "files": [f for f in files.split(",") if f],
                    "marks": [m for m in marks.split(",") if m],
                })
        return calls

    def main_devcontainer(self):
        return os.path.join(self.fixture_repo, ".devcontainer")

    def git_calls(self):
        if not os.path.isfile(self.git_call_log):
            return []
        with open(self.git_call_log, encoding="utf-8") as handle:
            return [line.rstrip("\n") for line in handle if line.strip()]

    def cleanup(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class DevContainerTestCase(unittest.TestCase):
    def setUp(self):
        self.harness = DevContainerHarness()
        self.addCleanup(self.harness.cleanup)


class ListVerdictTestCase(DevContainerTestCase):

    ENV = "GIT_USER_NAME=demo-agent\nGIT_USER_EMAIL=a@example.com\n"

    def setUp(self):
        super().setUp()
        self._gh_log = os.path.join(self.harness.tmp, "gh.log")
        self.harness._write_stub(
            "gh",
            "#!/usr/bin/env bash\n"
            f'printf "%s\\n" "$*" >> "{self._gh_log}"\n'
            "echo '[]'\n",
        )
        self.harness.build_fixture_repo()

    def _git(self, slot):
        return lambda *args: subprocess.run(
            [
                self.harness.real_git_bin, "-C", slot,
                "-c", "commit.gpgsign=false",
                "-c", "user.email=fixture@example.com",
                "-c", "user.name=fixture",
                *args,
            ],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )

    def _shared_remote(self):
        if getattr(self, "_remote", None) is None:
            self._remote = os.path.join(self.harness.tmp, "shared-remote.git")
            subprocess.run(
                ["git", "init", "--bare", "-q", self._remote],
                check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            subprocess.run(
                [self.harness.real_git_bin, "-C", self._remote,
                 "symbolic-ref", "HEAD", "refs/heads/master"],
                check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            subprocess.run(
                [self.harness.real_git_bin, "-C", self.harness.fixture_repo,
                 "remote", "set-url", "origin", self._remote],
                check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
        return self._remote

    def _ports(self, index):
        return "".join(
            f"{name}={slot_port(index + 1, k)}\n" for k, name in enumerate(DEFAULT_PORT_NAMES)
        )

    def _ready_slot(self, name, index=0):
        ports = self._ports(index)
        remote = self._shared_remote()
        self.harness.make_bare_slot(
            name,
            env_content=self.ENV + ports,
            ports_content=ports,
            local_identity=("demo-agent", "a@example.com"),
        )
        slot = self.harness.slot_dir(name)
        git = self._git(slot)
        git("remote", "set-url", "origin", remote)
        seeded = subprocess.run(
            [self.harness.real_git_bin, "ls-remote", "--heads", remote, "master"],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        ).stdout.strip()
        if not seeded:
            with open(os.path.join(slot, ".gitignore"), "w", encoding="utf-8") as handle:
                handle.write(".devcontainer/.env\n.devcontainer/.slot-holder\ndocs/\n")
            git("add", "-A")
            git("commit", "-q", "-m", "base")
            git("branch", "-M", "master")
            git("push", "-q", "origin", "master")
        else:
            git("fetch", "-q", "origin")
            git("reset", "-q", "--hard", "origin/master")
        git("fetch", "-q", "origin")
        git("checkout", "-q", "--detach", "HEAD")
        return slot

    def _write_lease(self, name, holder="task-18", age_seconds=60):
        path = self.harness.lease_path(name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "holder": holder,
                    "owner": "-",
                    "project": PROJECT_PREFIX + "-" + name,
                    "since": int(time.time()) - age_seconds,
                    "key": "/dev/null",
                    "ssh_port": TEST_SSH_PORT,
                },
                handle,
            )
        return path

    def _write_parked(self, name, holder="task-11", reason="uncommitted:1"):
        path = self.harness.parked_path(name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "holder": holder,
                    "project": PROJECT_PREFIX + "-" + name,
                    "since": int(time.time()),
                    "reason": reason,
                },
                handle,
            )
        return path

    @staticmethod
    def _row_line(stdout, slot_name):
        for line in stdout.splitlines():
            if line.split()[:1] == [slot_name]:
                return line
        raise AssertionError(f"no '{slot_name}' row in:\n{stdout}")
