"""Shared pieces for the tests of the engine container: a fake docker that logs
every call, stubs that fail when a tool the installer and the thin must not
need is run, and readers for the logged calls."""

import os
import stat
import subprocess
import tempfile
import shutil
import unittest

from harness import REPO_ROOT

SEP = "@@CALL@@"
ENGINE = os.path.join(REPO_ROOT, "scripts", "engine")
THIN = os.path.join(REPO_ROOT, "scripts", "dev-container-thin")

# `FAKE_DOCKER_STATE` says what the engine container looks like: running,
# stopped or missing. `docker run --rm` is the gid probe and answers with
# FAKE_DOCKER_GID; `docker exec` exits with FAKE_EXEC_RC.
FAKE_DOCKER = """#!/bin/sh
{ echo "@@CALL@@"; for a in "$@"; do printf '%s\n' "$a"; done; } >> "${FAKE_DOCKER_LOG:?}"
state=${FAKE_DOCKER_STATE:-running}
case "$1" in
  inspect)
    [ "$state" = missing ] && { echo "Error: No such object: $*" >&2; exit 1; }
    r=true; s=running
    [ "$state" = stopped ] && { r=false; s=exited; }
    case "$*" in
      *Running*) echo "$r" ;;
      *Status*) echo "$s" ;;
      *) printf '[{"State":{"Running":%s,"Status":"%s"}}]\n' "$r" "$s" ;;
    esac ;;
  ps)
    case "$state:$*" in
      running:*|stopped:*-a*) echo "${FAKE_DOCKER_NAME:-dev-container}" ;;
    esac ;;
  run)
    case " $* " in
      *" --rm "*) echo "${FAKE_DOCKER_GID:-998}" ;;
      *) echo fakecontainerid ;;
    esac ;;
  exec) exit "${FAKE_EXEC_RC:-0}" ;;
esac
exit 0
"""

FORBIDDEN_STUB = """#!/bin/sh
echo "$0 $*" >> "${FAKE_FORBIDDEN_LOG:?}"
exit 99
"""

# Tools the installer and the thin must not need: the host has docker and sh.
FORBIDDEN_TOOLS = ("jq", "git", "bash", "perl", "python3", "yq")


class FakeDockerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="engine-stub-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.fake_bin = os.path.join(self.tmp, "fake-bin")
        os.makedirs(self.fake_bin)
        self.docker_log = os.path.join(self.tmp, "docker.log")
        self.forbidden_log = os.path.join(self.tmp, "forbidden.log")
        self._stub("docker", FAKE_DOCKER)
        for tool in FORBIDDEN_TOOLS:
            self._stub(tool, FORBIDDEN_STUB)
        self.home = os.path.join(self.tmp, "home")
        self.projects = os.path.join(self.tmp, "projects")
        os.makedirs(self.home)
        os.makedirs(self.projects)

    def _stub(self, name, body):
        path = os.path.join(self.fake_bin, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(body)
        os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    def env(self, **extra):
        env = {
            "PATH": self.fake_bin + os.pathsep + "/usr/bin" + os.pathsep + "/bin",
            "HOME": self.home,
            "FAKE_DOCKER_LOG": self.docker_log,
            "FAKE_FORBIDDEN_LOG": self.forbidden_log,
        }
        env.update(extra)
        return env

    def sh(self, script, *args, cwd=None, env=None, stdin=subprocess.DEVNULL):
        return subprocess.run(
            ["/bin/sh", script, *args],
            cwd=cwd or self.tmp, env=env if env is not None else self.env(),
            stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60,
        )

    def calls(self):
        if not os.path.isfile(self.docker_log):
            return []
        with open(self.docker_log, encoding="utf-8") as handle:
            blocks = handle.read().split(SEP + "\n")
        return [block.splitlines() for block in blocks if block.strip()]

    def calls_of(self, verb):
        return [call for call in self.calls() if call and call[0] == verb]

    def forbidden_calls(self):
        if not os.path.isfile(self.forbidden_log):
            return []
        with open(self.forbidden_log, encoding="utf-8") as handle:
            return handle.read().splitlines()


def option_values(call, *names):
    """Every value given to any of `names`, as `--name value` or `--name=value`."""
    values = []
    for index, word in enumerate(call):
        for name in names:
            if word == name and index + 1 < len(call):
                values.append(call[index + 1])
            elif name.startswith("--") and word.startswith(name + "="):
                values.append(word[len(name) + 1:])
    return values
