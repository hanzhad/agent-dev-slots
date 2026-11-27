"""Tests for dirty checks and rescue archiving on real volume slots without DOCKER_STUB_STATE (#44)."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

if os.path.dirname(__file__) not in sys.path:
    sys.path.insert(0, os.path.dirname(__file__))

from harness import (
    DOCKER_STUB,
    DevContainerTestCase,
    PROJECT_PREFIX,
    REPO_ROOT,
    SCRIPT_REL,
)

MAIN_ENV = (
    "GIT_USER_NAME=demo-agent\n"
    "GIT_USER_EMAIL=demo-agent@example.com\n"
)
ENGINE = os.path.join(REPO_ROOT, SCRIPT_REL)


class TestVolumeFactsPureCore(unittest.TestCase):
    """In-memory Pure Core table tests for facts-backed slot_acquire_verdict (zero docker/network)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="vol-facts-core-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = os.path.join(self.tmp, "home")
        self.state = os.path.join(self.tmp, "state")
        self.scratch = os.path.join(self.tmp, "scratch")
        self.repo = os.path.join(self.tmp, "repo")
        self.stub_bin = os.path.join(self.tmp, "stub-bin")
        self.docker_log = os.path.join(self.tmp, "forbidden-docker.log")
        for path in (
            self.home,
            self.stub_bin,
            os.path.join(self.state, "leases"),
            os.path.join(self.state, "parked"),
            os.path.join(self.state, "ports"),
            os.path.join(self.state, "env"),
            os.path.join(self.scratch, "facts"),
            os.path.join(self.scratch, "spec"),
            os.path.join(self.repo, ".devcontainer"),
        ):
            os.makedirs(path, exist_ok=True)

        docker_stub = os.path.join(self.stub_bin, "docker")
        with open(docker_stub, "w", encoding="utf-8") as handle:
            handle.write(
                "#!/usr/bin/env bash\n"
                f'printf "%s\\n" "$*" >> "{self.docker_log}"\n'
                "exit 99\n"
            )
        os.chmod(docker_stub, 0o755)

        gh_stub = os.path.join(self.stub_bin, "gh")
        with open(gh_stub, "w", encoding="utf-8") as handle:
            handle.write("#!/usr/bin/env bash\necho '[]'\n")
        os.chmod(gh_stub, 0o755)

        with open(os.path.join(self.repo, "project.env"), "w", encoding="utf-8") as handle:
            handle.write(
                f'PROJECT="{PROJECT_PREFIX}"\n'
                'DEFAULT_BRANCH="master"\n'
                'SLOT_PORTS="SSH_PORT NOVNC_PORT VITE_DEV_PORT"\n'
                'COMPOSE_FILES="docker-compose.yml"\n'
            )
        with open(os.path.join(self.repo, ".devcontainer", "docker-compose.yml"), "w", encoding="utf-8") as handle:
            handle.write("services: {}\n")

        git = shutil.which("git")
        self.assertTrue(git, "git is required")
        for args in (
            ("init", "-q", "-b", "master"),
            ("config", "user.name", "demo-agent"),
            ("config", "user.email", "demo-agent@example.com"),
            ("add", "-A"),
            ("-c", "commit.gpgsign=false", "commit", "-q", "-m", "base"),
            ("remote", "add", "origin", "https://example.invalid/demo.git"),
        ):
            subprocess.run([git, "-C", self.repo, *args], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def _plant_slot(
        self,
        sid,
        facts_text,
        *,
        ports=True,
        env_file=True,
        lease=None,
        parked=False,
        docs_repo_facts=None,
        docs_fix_facts=None,
    ):
        with open(os.path.join(self.scratch, "spec", sid), "w", encoding="utf-8") as handle:
            handle.write(f"vol:{sid}-workspace")
        ports_path = os.path.join(self.state, "ports", f"{sid}.ports")
        if ports:
            with open(ports_path, "w", encoding="utf-8") as handle:
                handle.write("SSH_PORT=42016\nNOVNC_PORT=42017\nVITE_DEV_PORT=42018\n")
        elif os.path.exists(ports_path):
            os.remove(ports_path)

        env_path = os.path.join(self.state, "env", f"{sid}.env")
        if env_file:
            with open(env_path, "w", encoding="utf-8") as handle:
                handle.write("GIT_USER_NAME=demo-agent\nGIT_USER_EMAIL=demo-agent@example.com\n")
        elif os.path.exists(env_path):
            os.remove(env_path)

        lease_path = os.path.join(self.state, "leases", f"{sid}.json")
        if lease is not None:
            with open(lease_path, "w", encoding="utf-8") as handle:
                json.dump(lease, handle)
        elif os.path.exists(lease_path):
            os.remove(lease_path)

        parked_path = os.path.join(self.state, "parked", f"{sid}.json")
        if parked:
            with open(parked_path, "w", encoding="utf-8") as handle:
                json.dump({"project": sid, "holder": "old", "since": 1700000000, "reason": "dirty"}, handle)
        elif os.path.exists(parked_path):
            os.remove(parked_path)

        with open(os.path.join(self.scratch, "facts", sid), "w", encoding="utf-8") as handle:
            handle.write(facts_text)

        if docs_repo_facts is not None:
            with open(os.path.join(self.scratch, "facts", f"{sid}__docs_repo"), "w", encoding="utf-8") as handle:
                handle.write(docs_repo_facts)
        if docs_fix_facts is not None:
            with open(os.path.join(self.scratch, "facts", f"{sid}__docs_fix"), "w", encoding="utf-8") as handle:
                handle.write(docs_fix_facts)

    def _run_verdict(self, sid, fast=0, now=1700000000):
        env = {
            "PATH": self.stub_bin + os.pathsep + os.environ.get("PATH", ""),
            "HOME": self.home,
            "DEV_CONTAINER_HOME": self.state,
            "SCRATCH": self.scratch,
            "DEV_CONTAINER_PROJECTS_ROOT": self.tmp,
            "DEV_CONTAINER_CALLER_PWD": self.repo,
            "DEV_CONTAINER_LIB": "1",
            "DEV_CONTAINER_NOW": str(now),
            "DOCS_REPO_URL": "https://example.invalid/demo.docs.git",
        }
        script = (
            '. "$1"\n'
            f'rm -f "{self.docker_log}"\n'
            'rc=0\n'
            'slot_acquire_verdict "" "$2" "$3" || rc=$?\n'
            'printf "RC=%d\\nREASON=%s\\n" "$rc" "${ACQUIRE_REASON:-}"\n'
        )
        res = subprocess.run(
            ["bash", "-c", script, "test", ENGINE, sid, str(fast)],
            cwd=self.tmp,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=15,
        )
        self.assertEqual(res.returncode, 0, res.stderr.decode())
        lines = dict(
            line.split("=", 1) for line in res.stdout.decode().splitlines() if "=" in line
        )
        return int(lines["RC"]), lines["REASON"]

    @staticmethod
    def _facts(
        *,
        status_lines=(),
        stash_lines=(),
        head=("master", "1111111111111111111111111111111111111111"),
        headreach=1,
        default="master",
        branches=(),
        clones=(),
        worktrees=(),
        origin="https://example.invalid/demo.git",
    ):
        out = [f"origin\t{origin}", f"default\t{default}", "user\tdemo-agent"]
        if status_lines:
            out += [f"status\t{line}" for line in status_lines]
        else:
            out.append("status")
        if stash_lines:
            out += [f"stash\t{line}" for line in stash_lines]
        else:
            out.append("stash")
        out.append(f"head\t{head[0]}\t{head[1]}")
        out.append(f"headreach\t{headreach}")
        for clone in clones:
            if clone == "docs/repo":
                out.append("docsrepo\t1")
            out.append(f"clone\t{clone}")
        if worktrees:
            out.append("wtok\t1")
            for wt in worktrees:
                out.append(f"worktree\t{wt}")
        for row in branches:
            out.append("branch\t" + "\t".join(str(x) for x in row))
        return "\n".join(out) + "\n"

    def test_slot_acquire_verdict_facts_table_without_docker(self):
        sha = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        tip2 = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        clean_docs = self._facts(
            head=("", sha),
            origin="https://example.invalid/demo.docs.git",
        )
        cases = [
            (
                "clean_delivered_master",
                self._facts(branches=[("master", sha, 1, 0, 1, "clean", 0, 0, 0)]),
                {"docs_repo_facts": clean_docs, "clones": ("docs/repo",)},
                0,
                0,
                "acquirable",
                None,
            ),
            (
                "clean_detached_delivered_head",
                self._facts(
                    head=("", sha),
                    headreach=1,
                    branches=[("master", sha, 1, 0, 1, "clean", 0, 0, 0)],
                ),
                {},
                0,
                0,
                "acquirable",
                None,
            ),
            (
                "clean_branch_squash_merged_cherry_clean",
                self._facts(
                    head=("squashed", tip2),
                    headreach=0,
                    branches=[
                        ("master", sha, 1, 0, 1, "clean", 0, 0, 0),
                        ("squashed", tip2, 0, 0, 0, "clean", 1, 0, 0),
                    ],
                ),
                {},
                0,
                0,
                "acquirable",
                None,
            ),
            (
                "clean_branch_squash_merged_merge_tree",
                self._facts(
                    head=("squashed-mt", tip2),
                    headreach=0,
                    branches=[
                        ("master", sha, 1, 0, 1, "clean", 0, 0, 0),
                        ("squashed-mt", tip2, 0, 0, 0, "plus", 1, 0, 1),
                    ],
                ),
                {},
                0,
                0,
                "acquirable",
                None,
            ),
            (
                "uncommitted_full",
                self._facts(
                    status_lines=(" M a.txt", "?? b.txt"),
                    branches=[("master", sha, 1, 0, 1, "clean", 0, 0, 0)],
                ),
                {},
                0,
                1,
                None,
                "dirty:uncommitted:2",
            ),
            (
                "uncommitted_fast",
                self._facts(
                    status_lines=(" M a.txt",),
                    branches=[("master", sha, 1, 0, 1, "clean", 0, 0, 0)],
                ),
                {},
                1,
                1,
                None,
                "dirty:uncommitted-or-stashed",
            ),
            (
                "stashed_full",
                self._facts(
                    stash_lines=("stash@{0}: WIP on master",),
                    branches=[("master", sha, 1, 0, 1, "clean", 0, 0, 0)],
                ),
                {},
                0,
                1,
                None,
                "dirty:stash:1",
            ),
            (
                "stashed_fast",
                self._facts(
                    stash_lines=("stash@{0}: WIP on master",),
                    branches=[("master", sha, 1, 0, 1, "clean", 0, 0, 0)],
                ),
                {},
                1,
                1,
                None,
                "dirty:uncommitted-or-stashed",
            ),
            (
                "head_unpushed_full",
                self._facts(
                    head=("wip", tip2),
                    headreach=0,
                    branches=[
                        ("master", sha, 1, 0, 1, "clean", 0, 0, 0),
                        ("wip", tip2, 0, 0, 0, "plus", 1, 0, 0),
                    ],
                ),
                {},
                0,
                1,
                None,
                "dirty:head:held",
            ),
            (
                "head_unpushed_fast_clean_tree",
                self._facts(
                    head=("wip", tip2),
                    headreach=0,
                    branches=[
                        ("master", sha, 1, 0, 1, "clean", 0, 0, 0),
                        ("wip", tip2, 0, 0, 0, "plus", 1, 0, 0),
                    ],
                ),
                {},
                1,
                0,
                "acquirable",
                None,
            ),
            (
                "detached_head_held_full",
                self._facts(
                    head=("", tip2),
                    headreach=0,
                    branches=[("master", sha, 1, 0, 1, "clean", 0, 0, 0)],
                ),
                {},
                0,
                1,
                None,
                "dirty:head:held",
            ),
            (
                "docs_repo_uncommitted_full",
                self._facts(
                    clones=("docs/repo",),
                    branches=[("master", sha, 1, 0, 1, "clean", 0, 0, 0)],
                ),
                {
                    "docs_repo_facts": self._facts(
                        status_lines=(" M README.md",),
                        head=("", sha),
                        origin="https://example.invalid/demo.docs.git",
                    )
                },
                0,
                1,
                None,
                "dirty:docs:",
            ),
            (
                "docs_repo_uncommitted_fast",
                self._facts(
                    clones=("docs/repo",),
                    branches=[("master", sha, 1, 0, 1, "clean", 0, 0, 0)],
                ),
                {
                    "docs_repo_facts": self._facts(
                        status_lines=(" M README.md",),
                        head=("", sha),
                        origin="https://example.invalid/demo.docs.git",
                    )
                },
                1,
                1,
                None,
                "dirty:docs uncommitted-or-stashed",
            ),
            (
                "docs_fix_held_branch_full",
                self._facts(
                    clones=("docs/repo", "docs/fix"),
                    branches=[("master", sha, 1, 0, 1, "clean", 0, 0, 0)],
                ),
                {
                    "docs_repo_facts": clean_docs,
                    "docs_fix_facts": self._facts(
                        head=("fix-wip", tip2),
                        headreach=0,
                        origin="https://example.invalid/demo.docs.git",
                        branches=[("fix-wip", tip2, 0, 0, 0, "plus", 1, 0, 0)],
                    ),
                },
                0,
                1,
                None,
                "dirty:docsfix:",
            ),
            (
                "facts_norepo",
                "norepo\n",
                {},
                0,
                0,
                "acquirable",
                None,
            ),
            (
                "facts_norepo_fast",
                "norepo\n",
                {},
                1,
                0,
                "acquirable",
                None,
            ),
            (
                "facts_norepo_dirty_docs_fix",
                "norepo\nclone\tdocs/fix\n",
                {
                    "docs_fix_facts": self._facts(
                        head=("fix-wip", tip2),
                        headreach=0,
                        origin="https://example.invalid/demo.docs.git",
                        branches=[("fix-wip", tip2, 0, 0, 0, "plus", 1, 0, 0)],
                    ),
                },
                0,
                1,
                None,
                "dirty:docsfix:",
            ),
            (
                "facts_error",
                "error\tcontainer failed\n",
                {},
                0,
                1,
                "unreadable:facts",
                None,
            ),
            (
                "facts_error_fast",
                "error\tcontainer failed\n",
                {},
                1,
                1,
                "unreadable:facts",
                None,
            ),
            (
                "unbuilt_missing_ports",
                self._facts(branches=[("master", sha, 1, 0, 1, "clean", 0, 0, 0)]),
                {"ports": False},
                0,
                1,
                "unbuilt",
                None,
            ),
            (
                "unbuilt_missing_env",
                self._facts(branches=[("master", sha, 1, 0, 1, "clean", 0, 0, 0)]),
                {"env_file": False},
                0,
                1,
                "unbuilt",
                None,
            ),
            (
                "leased_live",
                self._facts(branches=[("master", sha, 1, 0, 1, "clean", 0, 0, 0)]),
                {
                    "lease": {
                        "holder": "holder-1",
                        "owner": "-",
                        "project": "demo-slot-19",
                        "since": 1700000000,
                        "key": "/dev/null",
                        "ssh_port": 42016,
                    }
                },
                0,
                1,
                "leased:holder-1",
                None,
            ),
            (
                "idle_unreleased",
                self._facts(branches=[("master", sha, 1, 0, 1, "clean", 0, 0, 0)]),
                {
                    "lease": {
                        "holder": "holder-idle",
                        "owner": "-",
                        "project": "demo-slot-20",
                        "since": 1699900000,
                        "key": "/dev/null",
                        "ssh_port": 42016,
                    }
                },
                0,
                1,
                "idle-unreleased:holder-idle",
                None,
            ),
            (
                "parked",
                self._facts(branches=[("master", sha, 1, 0, 1, "clean", 0, 0, 0)]),
                {"parked": True},
                0,
                1,
                "parked",
                None,
            ),
        ]

        for idx, (label, facts_text, opts, fast, exp_rc, exact_reason, prefix_reason) in enumerate(cases, start=1):
            with self.subTest(case=label):
                sid = f"{PROJECT_PREFIX}-slot-{idx}"
                if opts.get("clones"):
                    facts_text = self._facts(
                        clones=opts["clones"],
                        branches=[("master", sha, 1, 0, 1, "clean", 0, 0, 0)],
                    )
                plant_kwargs = {
                    "ports": opts.get("ports", True),
                    "env_file": opts.get("env_file", True),
                    "lease": opts.get("lease"),
                    "parked": opts.get("parked", False),
                    "docs_repo_facts": opts.get("docs_repo_facts"),
                    "docs_fix_facts": opts.get("docs_fix_facts"),
                }
                self._plant_slot(sid, facts_text, **plant_kwargs)
                rc, reason = self._run_verdict(sid, fast=fast)
                self.assertEqual(rc, exp_rc, f"{label}: got rc={rc} reason={reason!r}")
                if exact_reason is not None:
                    self.assertEqual(reason, exact_reason, f"{label}: reason={reason!r}")
                elif prefix_reason is not None:
                    self.assertTrue(reason.startswith(prefix_reason), f"{label}: reason={reason!r}")
                else:
                    self.assertNotEqual(reason, "acquirable", f"{label} must never read as acquirable")

        self.assertFalse(os.path.exists(self.docker_log), "Pure Core verdict check invoked docker")


DOCKER_VOL_WRAPPER = r"""#!/usr/bin/env bash
set -u
export DOCKER_STUB_STATE="${TEST_DOCKER_STORE:?}"
base="${TEST_DOCKER_BASE:?}"
log="${DOCKER_VOL_CALL_LOG:-/dev/null}"
printf '%s\n' "$*" >> "$log"

project="${COMPOSE_PROJECT_NAME:-}"
prev=""
for a in "$@"; do
  [[ "$prev" == "-p" ]] && project="$a"
  if [[ -z "$project" && "$prev" == "-v" && "$a" == *-workspace:/workspace* ]]; then
    project="${a%%-workspace:/workspace*}"
  fi
  prev="$a"
done

if [[ -n "$project" && -f "$DOCKER_STUB_STATE/$project.factsfail" ]]; then
  case "$*" in
    *"rev-parse --git-dir"*|*"status --porcelain"*)
      echo "simulated container facts failure" >&2
      exit 1
      ;;
  esac
fi

if [[ -n "$project" && -f "$DOCKER_STUB_STATE/$project.helperfail" ]]; then
  case "${1:-}:$*" in
    run:*|*bundle*|*changes.patch*|*"tar "*)
      echo "simulated helper failure" >&2
      exit 1
      ;;
  esac
fi

if [[ ( "${1:-}" == compose || "${1:-}" == run ) && "$*" == *dev-container-clone* ]]; then
  vol="${SLOT_WORKSPACE_VOLUME:-${project}-workspace}"
  src="" origin="" prev=""
  seen_ws=0
  for a in "$@"; do
    if [[ "$prev" == "-v" && "$a" == *:/workspace* ]]; then
      vol="${a%%:/workspace*}"
    elif [[ "$prev" == "-v" && "$a" == *:/src* ]]; then
      src="${a%%:/src*}"
    fi
    if (( seen_ws )) && [[ -z "$origin" ]]; then
      origin="$a"
    fi
    [[ "$a" == "/workspace" ]] && seen_ws=1
    prev="$a"
  done
  voldir="$DOCKER_STUB_STATE/volumes/$vol"
  mkdir -p "$voldir"
  if [[ -n "$src" && -d "$src" ]]; then
    rm -rf "$voldir"
    mkdir -p "$voldir"
    git clone --quiet "$src" "$voldir" 2>/dev/null
    [[ -z "$origin" ]] || git -C "$voldir" remote set-url origin "$origin" 2>/dev/null || true
    for item in .devcontainer project.env; do
      if [[ -e "$src/$item" && ! -e "$voldir/$item" ]]; then
        cp -a "$src/$item" "$voldir/$item"
      fi
    done
    if [[ -f "$src/bin/dev-container" && ! -f "$voldir/bin/dev-container" ]]; then
      mkdir -p "$voldir/bin"
      cp -a "$src/bin/dev-container" "$voldir/bin/dev-container"
    fi
    excl="$voldir/.git/info/exclude"
    mkdir -p "$(dirname "$excl")"
    touch "$excl"
    for p in "/.devcontainer/" "/project.env" "/bin/dev-container"; do
      grep -Fxq "$p" "$excl" 2>/dev/null || echo "$p" >> "$excl"
    done
  fi
  exit 0
fi

exec "$base" "$@"
"""


class _NoStubStateVolumeCase(DevContainerTestCase):
    """Runs dev-container with DOCKER_STUB_STATE completely unset from the engine's env."""

    def setUp(self):
        super().setUp()
        self.harness._write_stub("gh", "#!/usr/bin/env bash\necho '[]'\n")
        self.harness.build_fixture_repo(with_parent_env=MAIN_ENV)
        with open(os.path.join(self.harness.fixture_repo, ".devcontainer", "install.sh"), "w", encoding="utf-8") as f:
            f.write("#!/usr/bin/env bash\nexit 0\n")
        os.chmod(os.path.join(self.harness.fixture_repo, ".devcontainer", "install.sh"), 0o755)
        with open(os.path.join(self.harness.fixture_repo, "tracked.txt"), "w", encoding="utf-8") as f:
            f.write("base tracked content\n")
        with open(os.path.join(self.harness.fixture_repo, ".gitignore"), "a", encoding="utf-8") as f:
            f.write("ignored.log\n.local/\nnode_modules/\n")
        self._git(self.harness.fixture_repo)("add", ".devcontainer/install.sh", "tracked.txt", ".gitignore")
        self._git(self.harness.fixture_repo)("commit", "-q", "-m", "noop install and base tracked")

        self.remote = os.path.join(self.harness.tmp, "origin-remote.git")
        subprocess.run(
            [self.harness.real_git_bin, "init", "--bare", "-q", self.remote],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        subprocess.run(
            [self.harness.real_git_bin, "-C", self.remote, "symbolic-ref", "HEAD", "refs/heads/master"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self._git(self.harness.fixture_repo)("remote", "set-url", "origin", self.remote)
        self._git(self.harness.fixture_repo)("branch", "-M", "master")
        self._git(self.harness.fixture_repo)("push", "-q", "origin", "master")

        self.docker_vol_log = os.path.join(self.harness.tmp, "docker-vol-calls.log")
        self.harness._write_stub("docker-base", DOCKER_STUB)
        self.harness._write_stub("docker", DOCKER_VOL_WRAPPER)
        self.rescue_dir = os.path.join(self.harness.state_dir, "rescue")

    def _git(self, repo_dir):
        return lambda *args: subprocess.run(
            [
                self.harness.real_git_bin,
                "-C",
                repo_dir,
                "-c",
                "commit.gpgsign=false",
                "-c",
                "user.name=demo-agent",
                "-c",
                "user.email=demo-agent@example.com",
                *args,
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def run_dc(self, *args, port_overrides=None, input=None, now=None, with_docs=True):
        env = self.harness._env(port_overrides)
        env.pop("DOCKER_STUB_STATE", None)
        env["TEST_DOCKER_STORE"] = self.harness.docker_state
        env["TEST_DOCKER_BASE"] = os.path.join(self.harness.stub_bin, "docker-base")
        env["DOCKER_VOL_CALL_LOG"] = self.docker_vol_log
        if not with_docs:
            env.pop("DOCS_REPO_URL", None)
        if now is not None:
            env["DEV_CONTAINER_NOW"] = str(now)
        return subprocess.run(
            ["bash", self.harness.script, *args],
            cwd=self.harness.tmp,
            env=env,
            input=input,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=120,
        )

    def acquire_vol(self, holder="vol-holder", ephemeral=False, with_docs=True, now=None):
        args = ["acquire"]
        if ephemeral:
            args.append("--ephemeral")
        args += ["--for", holder, "--as", "Agent [vol1]"]
        res = self.run_dc(*args, with_docs=with_docs, now=now)
        self.assertEqual(res.returncode, 0, res.stdout.decode() + res.stderr.decode())
        name = self.harness.acquired_slot_name(res)
        self.assertTrue(self.harness.volume_exists(name), f"volume missing for {name}")
        host_slot = os.path.join(self.harness.slots_dir, name)
        self.assertFalse(os.path.exists(host_slot), f"unexpected host dir at {host_slot}")
        return name

    def docker_vol_calls(self):
        if not os.path.isfile(self.docker_vol_log):
            return []
        with open(self.docker_vol_log, encoding="utf-8") as handle:
            return [line.rstrip("\n") for line in handle if line.strip()]

    def clear_logs(self):
        for path in (self.docker_vol_log, self.harness.docker_log, self.harness.git_call_log):
            if os.path.exists(path):
                os.remove(path)

    def free_lease(self, name):
        lease = self.harness.lease_path(name)
        if os.path.isfile(lease):
            os.remove(lease)

    def expire_lease(self, name, age_seconds=10000):
        path = self.harness.lease_path(name)
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        data["since"] = self.harness.now_epoch() - age_seconds
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        prefix = self.harness.slot_project_of(name)
        for kind in ("run", "cpu"):
            stamp = os.path.join(self.harness.state_dir, "activity", f"{prefix}.{kind}")
            if os.path.exists(stamp):
                os.remove(stamp)

    def _rescue_files(self):
        if not os.path.isdir(self.rescue_dir):
            return []
        found = []
        for root, _dirs, files in os.walk(self.rescue_dir):
            for fname in files:
                found.append(os.path.join(root, fname))
        return sorted(found)


class TestVolumeDirtyChecksWithoutStubState(_NoStubStateVolumeCase):

    def _assert_release_refused_and_preserved(self, name, holder, expected_terms):
        vol_dir = self.harness.slot_dir(name)
        lease_path = self.harness.lease_path(name)
        with open(lease_path, "rb") as handle:
            lease_before = handle.read()

        res = self.run_dc("release", name, "--for", holder)
        combined = res.stdout.decode() + res.stderr.decode()
        self.assertNotEqual(res.returncode, 0, f"release unexpectedly succeeded on dirty volume:\n{combined}")
        for term in expected_terms:
            self.assertIn(term, combined, f"missing {term!r} in dirty report:\n{combined}")
        self.assertTrue(self.harness.volume_exists(name), "volume was deleted on refused release")
        self.assertTrue(os.path.isdir(vol_dir), "volume backing store disappeared")
        self.assertTrue(os.path.isfile(lease_path), "lease was removed on refused release")
        with open(lease_path, "rb") as handle:
            self.assertEqual(handle.read(), lease_before)
        self.assertEqual(self._rescue_files(), [])

    def test_release_refuses_uncommitted_untracked_stash_unpushed_detached_worktree_and_docs_dirty(self):
        # 1. Tracked modification + untracked file
        s1 = self.acquire_vol("h1", with_docs=False)
        v1 = self.harness.slot_dir(s1)
        with open(os.path.join(v1, "tracked.txt"), "w", encoding="utf-8") as f:
            f.write("edited\n")
        with open(os.path.join(v1, "brand_new.txt"), "w", encoding="utf-8") as f:
            f.write("untracked\n")
        self._assert_release_refused_and_preserved(s1, "h1", ["uncommitted:", "tracked.txt", "brand_new.txt"])

        # 2. Stash only
        s2 = self.acquire_vol("h2", with_docs=False)
        v2 = self.harness.slot_dir(s2)
        with open(os.path.join(v2, "stashed.txt"), "w", encoding="utf-8") as f:
            f.write("stash me\n")
        self._git(v2)("stash", "push", "-q", "-u", "-m", "saved-wip")
        self._assert_release_refused_and_preserved(s2, "h2", ["stashed:", "saved-wip"])

        # 3. Unpushed local branch (while HEAD sits on clean master)
        s3 = self.acquire_vol("h3", with_docs=False)
        v3 = self.harness.slot_dir(s3)
        git3 = self._git(v3)
        git3("checkout", "-q", "-b", "still-brewing")
        with open(os.path.join(v3, "wip.txt"), "w", encoding="utf-8") as f:
            f.write("unpushed branch work\n")
        git3("add", "wip.txt")
        git3("commit", "-q", "-m", "wip commit")
        git3("checkout", "-q", "master")
        self._assert_release_refused_and_preserved(s3, "h3", ["still-brewing"])

        # 4. Detached HEAD with unpushed commit
        s4 = self.acquire_vol("h4", with_docs=False)
        v4 = self.harness.slot_dir(s4)
        git4 = self._git(v4)
        git4("checkout", "-q", "--detach", "HEAD")
        with open(os.path.join(v4, "detached.txt"), "w", encoding="utf-8") as f:
            f.write("detached commit\n")
        git4("add", "detached.txt")
        git4("commit", "-q", "-m", "detached work")
        self._assert_release_refused_and_preserved(s4, "h4", ["detached HEAD"])

        # 5. Extra worktree inside /workspace
        s5 = self.acquire_vol("h5", with_docs=False)
        v5 = self.harness.slot_dir(s5)
        git5 = self._git(v5)
        git5("worktree", "add", "-q", "-b", "wt-branch", os.path.join(v5, ".wt-sub"))
        self._assert_release_refused_and_preserved(s5, "h5", ["worktree"])

        # 6. Dirty docs/repo and dirty docs/fix
        s6 = self.acquire_vol("h6", with_docs=True)
        v6 = self.harness.slot_dir(s6)
        with open(os.path.join(v6, "docs", "repo", "README.md"), "a", encoding="utf-8") as f:
            f.write("uncommitted docs edit\n")
        self._assert_release_refused_and_preserved(s6, "h6", ["docs/repo", "uncommitted"])

        with open(os.path.join(v6, "docs", "repo", "README.md"), "w", encoding="utf-8") as f:
            f.write("docs repo stub\n")
        docs_fix = os.path.join(v6, "docs", "fix")
        subprocess.run(
            [self.harness.real_git_bin, "clone", "-q", self.harness.docs_repo_stub, docs_fix],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        git_fix = lambda *args: subprocess.run(
            [
                self.harness.real_git_bin,
                "-C",
                docs_fix,
                "-c",
                "commit.gpgsign=false",
                "-c",
                "user.name=demo-agent",
                "-c",
                "user.email=demo-agent@example.com",
                *args,
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        git_fix("checkout", "-q", "-b", "docs-fix-wip")
        with open(os.path.join(docs_fix, "fix.md"), "w", encoding="utf-8") as f:
            f.write("docs fix work\n")
        git_fix("add", "fix.md")
        git_fix("commit", "-q", "-m", "docs fix commit")
        self._assert_release_refused_and_preserved(s6, "h6", ["docs/fix", "docs-fix-wip"])

    def test_release_works_when_stopped_and_refreshes_facts_across_invocations(self):
        name = self.acquire_vol("stopped-rel", with_docs=False)
        vdir = self.harness.slot_dir(name)
        dirty_file = os.path.join(vdir, "wip.txt")
        with open(dirty_file, "w", encoding="utf-8") as f:
            f.write("uncommitted while stopped\n")

        self.harness.mark_stopped(name)
        self._assert_release_refused_and_preserved(name, "stopped-rel", ["uncommitted:", "wip.txt"])

        os.remove(dirty_file)
        clean_rel = self.run_dc("release", name, "--for", "stopped-rel", with_docs=False)
        self.assertEqual(clean_rel.returncode, 0, clean_rel.stdout.decode() + clean_rel.stderr.decode())
        self.assertFalse(self.harness.volume_exists(name))
        self.assertFalse(os.path.exists(self.harness.lease_path(name)))

    def test_release_allows_squash_and_pr_merged_branches_without_overblocking(self):
        name = self.acquire_vol("delivered-rel", with_docs=False)
        vdir = self.harness.slot_dir(name)
        git = self._git(vdir)

        # Squash-merged branch
        git("checkout", "-q", "-b", "squash-work")
        with open(os.path.join(vdir, "sq.txt"), "w", encoding="utf-8") as f:
            f.write("squashed content\n")
        git("add", "sq.txt")
        git("commit", "-q", "-m", "squash work")
        git("checkout", "-q", "master")
        git("merge", "-q", "--squash", "squash-work")
        git("commit", "-q", "-m", "squash work (landed)")
        git("push", "-q", "origin", "master")

        # PR-merged branch verified via gh + merged_oids_cover_tip
        git("checkout", "-q", "-b", "pr-landed")
        with open(os.path.join(vdir, "pr.txt"), "w", encoding="utf-8") as f:
            f.write("pr landed content\n")
        git("add", "pr.txt")
        git("commit", "-q", "-m", "pr work")
        pr_tip = git("rev-parse", "HEAD").stdout.decode().strip()
        git("checkout", "-q", "master")

        self.harness._write_stub(
            "gh",
            "#!/usr/bin/env bash\n"
            'head="" prev=""\n'
            'for a in "$@"; do [[ "$prev" == "--head" ]] && head="$a"; prev="$a"; done\n'
            'if [[ "$head" == "pr-landed" ]]; then\n'
            f'  echo \'[{{"headRefOid":"{pr_tip}","state":"MERGED","number":12}}]\'\n'
            "else\n"
            "  echo '[]'\n"
            "fi\n",
        )

        # Force dirty report via leftover.txt so we verify neither delivered branch is reported held
        leftover = os.path.join(vdir, "leftover.txt")
        with open(leftover, "w", encoding="utf-8") as f:
            f.write("force report\n")
        refused = self.run_dc("release", name, "--for", "delivered-rel", with_docs=False)
        self.assertNotEqual(refused.returncode, 0, refused.stdout.decode() + refused.stderr.decode())
        err = refused.stderr.decode()
        self.assertIn("leftover.txt", err)
        self.assertNotIn("squash-work", err)
        self.assertNotIn("pr-landed", err)

        os.remove(leftover)
        ok = self.run_dc("release", name, "--for", "delivered-rel", with_docs=False)
        self.assertEqual(ok.returncode, 0, ok.stdout.decode() + ok.stderr.decode())
        self.assertFalse(self.harness.volume_exists(name))

    def test_release_discard_and_ephemeral_bypass_dirty_checks_with_zero_rescue(self):
        # Non-ephemeral dirty: plain release refuses, --discard succeeds
        s_norm = self.acquire_vol("h-norm", with_docs=False)
        with open(os.path.join(self.harness.slot_dir(s_norm), "dirty.txt"), "w", encoding="utf-8") as f:
            f.write("dirty\n")

        plain = self.run_dc("release", s_norm, "--for", "h-norm", with_docs=False)
        self.assertNotEqual(plain.returncode, 0)
        self.assertTrue(self.harness.volume_exists(s_norm))

        self.clear_logs()
        discard = self.run_dc("release", s_norm, "--for", "h-norm", "--discard", with_docs=False)
        self.assertEqual(discard.returncode, 0, discard.stdout.decode() + discard.stderr.decode())
        self.assertFalse(self.harness.volume_exists(s_norm))
        self.assertFalse(os.path.exists(self.harness.lease_path(s_norm)))

        # Ephemeral dirty: wrong holder refuses, plain release succeeds without --discard
        s_eph = self.acquire_vol("h-eph", ephemeral=True, with_docs=False)
        with open(os.path.join(self.harness.slot_dir(s_eph), "eph_dirty.txt"), "w", encoding="utf-8") as f:
            f.write("eph dirty\n")

        wrong = self.run_dc("release", s_eph, "--for", "other-holder", with_docs=False)
        self.assertNotEqual(wrong.returncode, 0)
        self.assertTrue(self.harness.volume_exists(s_eph))

        rel_eph = self.run_dc("release", s_eph, "--for", "h-eph", with_docs=False)
        self.assertEqual(rel_eph.returncode, 0, rel_eph.stdout.decode() + rel_eph.stderr.decode())
        self.assertFalse(self.harness.volume_exists(s_eph))
        self.assertFalse(os.path.exists(self.harness.lease_path(s_eph)))
        self.assertEqual(self._rescue_files(), [])

    def test_release_refuses_when_facts_probe_fails(self):
        name = self.acquire_vol("h-fail", with_docs=False)
        sid = self.harness.slot_project_of(name)
        with open(os.path.join(self.harness.docker_state, f"{sid}.factsfail"), "w", encoding="utf-8"):
            pass

        res = self.run_dc("release", name, "--for", "h-fail", with_docs=False)
        self.assertNotEqual(res.returncode, 0, res.stdout.decode() + res.stderr.decode())
        self.assertTrue(self.harness.volume_exists(name), "volume deleted despite unreadable facts")
        self.assertTrue(os.path.isfile(self.harness.lease_path(name)), "lease deleted despite unreadable facts")

        discard = self.run_dc("release", name, "--for", "h-fail", "--discard", with_docs=False)
        self.assertEqual(discard.returncode, 0, discard.stdout.decode() + discard.stderr.decode())
        self.assertFalse(self.harness.volume_exists(name))

    def test_release_and_park_tolerate_dev_null_lease_key(self):
        s_clean = self.acquire_vol("h-devnull-clean", with_docs=False)
        lease_clean = self.harness.lease_path(s_clean)
        with open(lease_clean, encoding="utf-8") as f:
            data = json.load(f)
        data["key"] = "/dev/null"
        with open(lease_clean, "w", encoding="utf-8") as f:
            json.dump(data, f)

        rel_clean = self.run_dc("release", s_clean, "--for", "h-devnull-clean", with_docs=False)
        self.assertEqual(rel_clean.returncode, 0, rel_clean.stdout.decode() + rel_clean.stderr.decode())
        self.assertFalse(self.harness.volume_exists(s_clean))
        self.assertFalse(os.path.exists(lease_clean))

        s_discard = self.acquire_vol("h-devnull-discard", with_docs=False)
        with open(os.path.join(self.harness.slot_dir(s_discard), "wip.txt"), "w", encoding="utf-8") as f:
            f.write("dirty\n")
        lease_discard = self.harness.lease_path(s_discard)
        with open(lease_discard, encoding="utf-8") as f:
            data = json.load(f)
        data["key"] = "/dev/null"
        with open(lease_discard, "w", encoding="utf-8") as f:
            json.dump(data, f)

        rel_discard = self.run_dc("release", s_discard, "--for", "h-devnull-discard", "--discard", with_docs=False)
        self.assertEqual(rel_discard.returncode, 0, rel_discard.stdout.decode() + rel_discard.stderr.decode())
        self.assertFalse(self.harness.volume_exists(s_discard))
        self.assertFalse(os.path.exists(lease_discard))

        s_park = self.acquire_vol("h-devnull-park", with_docs=False)
        with open(os.path.join(self.harness.slot_dir(s_park), "wip.txt"), "w", encoding="utf-8") as f:
            f.write("dirty\n")
        lease_park = self.harness.lease_path(s_park)
        with open(lease_park, encoding="utf-8") as f:
            data = json.load(f)
        data["key"] = "/dev/null"
        with open(lease_park, "w", encoding="utf-8") as f:
            json.dump(data, f)

        parked = self.run_dc("park", s_park, with_docs=False)
        self.assertEqual(parked.returncode, 0, parked.stdout.decode() + parked.stderr.decode())
        self.assertFalse(os.path.exists(lease_park))
        self.assertTrue(os.path.isfile(self.harness.parked_path(s_park)))
        self.assertTrue(os.path.exists("/dev/null"))

    def test_down_refuses_dirty_unleased_volume_and_allows_clean_or_force(self):
        s_dirty = self.acquire_vol("h-down-dirty", with_docs=False)
        v_dirty = self.harness.slot_dir(s_dirty)
        self._git(v_dirty)("checkout", "-q", "-b", "unpushed-down")
        with open(os.path.join(v_dirty, "wip.txt"), "w", encoding="utf-8") as f:
            f.write("wip\n")
        self._git(v_dirty)("add", "wip.txt")
        self._git(v_dirty)("commit", "-q", "-m", "wip")
        with open(os.path.join(v_dirty, "untracked.txt"), "w", encoding="utf-8") as f:
            f.write("untracked\n")
        self.free_lease(s_dirty)
        self.harness.mark_stopped(s_dirty)

        refused = self.run_dc("down", s_dirty, with_docs=False)
        combined = refused.stdout.decode() + refused.stderr.decode()
        self.assertNotEqual(refused.returncode, 0, f"down succeeded on dirty volume:\n{combined}")
        self.assertIn("unpushed-down", combined)
        self.assertIn("untracked.txt", combined)
        self.assertTrue(self.harness.volume_exists(s_dirty), "down deleted dirty volume")

        forced = self.run_dc("down", s_dirty, "--force", with_docs=False)
        self.assertEqual(forced.returncode, 0, forced.stdout.decode() + forced.stderr.decode())
        self.assertFalse(self.harness.volume_exists(s_dirty))

        # Clean unleased volume slot: plain down succeeds
        s_clean = self.acquire_vol("h-down-clean", with_docs=False)
        self.free_lease(s_clean)
        clean_down = self.run_dc("down", s_clean, with_docs=False)
        self.assertEqual(clean_down.returncode, 0, clean_down.stdout.decode() + clean_down.stderr.decode())
        self.assertFalse(self.harness.volume_exists(s_clean))

    def test_list_populates_dirty_and_acquirable_for_volume_slots(self):
        s_clean = self.acquire_vol("h-list-clean", with_docs=False)
        s_dirty = self.acquire_vol("h-list-dirty", with_docs=False)
        v_dirty = self.harness.slot_dir(s_dirty)
        with open(os.path.join(v_dirty, "edit.txt"), "w", encoding="utf-8") as f:
            f.write("uncommitted\n")
        self._git(v_dirty)("checkout", "-q", "-b", "branch-held")
        with open(os.path.join(v_dirty, "b.txt"), "w", encoding="utf-8") as f:
            f.write("branch work\n")
        self._git(v_dirty)("add", "b.txt")
        self._git(v_dirty)("commit", "-q", "-m", "branch work")

        # Leased: list must NOT fetch on leased slot (HOST_GIT_READ_ONLY=1)
        self.clear_logs()
        leased_list = self.run_dc("list", with_docs=False)
        self.assertEqual(leased_list.returncode, 0, leased_list.stderr.decode())
        out_leased = leased_list.stdout.decode()
        row_clean_leased = next(l for l in out_leased.splitlines() if l.split()[:1] == [s_clean])
        row_dirty_leased = next(l for l in out_leased.splitlines() if l.split()[:1] == [s_dirty])
        self.assertIn("clean", row_clean_leased)
        self.assertIn("leased:h-list-clean", row_clean_leased)
        self.assertIn("uncommitted:1", row_dirty_leased)
        self.assertIn("head:held", row_dirty_leased)
        self.assertIn("leased:h-list-dirty", row_dirty_leased)
        self.assertFalse(
            any(" fetch " in call for call in self.harness.git_calls()),
            f"list fetched on leased volume slots: {self.harness.git_calls()}",
        )

        # Unleased: clean -> clean / acquirable; dirty -> dirty:...
        self.free_lease(s_clean)
        self.free_lease(s_dirty)
        unleased_list = self.run_dc("list", with_docs=False)
        self.assertEqual(unleased_list.returncode, 0, unleased_list.stderr.decode())
        out_unleased = unleased_list.stdout.decode()
        row_clean = next(l for l in out_unleased.splitlines() if l.split()[:1] == [s_clean])
        row_dirty = next(l for l in out_unleased.splitlines() if l.split()[:1] == [s_dirty])
        self.assertIn("clean", row_clean)
        self.assertIn("acquirable", row_clean)
        self.assertNotIn("unbuilt", row_clean)
        self.assertIn("dirty:", row_dirty)

        # --fast checks uncommitted/stash without delivery check
        s_unpushed_clean_tree = self.acquire_vol("h-list-fast", with_docs=False)
        v_fast = self.harness.slot_dir(s_unpushed_clean_tree)
        self._git(v_fast)("checkout", "-q", "-b", "fast-branch")
        with open(os.path.join(v_fast, "f.txt"), "w", encoding="utf-8") as f:
            f.write("committed only\n")
        self._git(v_fast)("add", "f.txt")
        self._git(v_fast)("commit", "-q", "-m", "committed")
        self.free_lease(s_unpushed_clean_tree)

        fast_list = self.run_dc("list", "--fast", with_docs=False)
        self.assertEqual(fast_list.returncode, 0, fast_list.stderr.decode())
        out_fast = fast_list.stdout.decode()
        row_fast_dirty = next(l for l in out_fast.splitlines() if l.split()[:1] == [s_dirty])
        row_fast_clean_tree = next(l for l in out_fast.splitlines() if l.split()[:1] == [s_unpushed_clean_tree])
        self.assertIn("dirty:uncommitted-or-stashed", row_fast_dirty)
        self.assertIn("acquirable", row_fast_clean_tree)

        # --brief skips dirty & acquirable without running facts probes
        self.clear_logs()
        brief_list = self.run_dc("list", "--brief", with_docs=False)
        self.assertEqual(brief_list.returncode, 0, brief_list.stderr.decode())
        row_brief = next(l for l in brief_list.stdout.decode().splitlines() if l.split()[:1] == [s_dirty])
        self.assertEqual(row_brief.split()[-2:], ["-", "-"])

    def test_non_repo_volume_and_host_slots_are_clean_and_acquirable_while_corrupt_git_blocks(self):
        # 1. Non-git vol:* slot -> release --for succeeds and removes volume/lease
        s_rel = self.acquire_vol("nogit-rel", with_docs=False)
        shutil.rmtree(os.path.join(self.harness.slot_dir(s_rel), ".git"))
        rel_res = self.run_dc("release", s_rel, "--for", "nogit-rel", with_docs=False)
        self.assertEqual(rel_res.returncode, 0, rel_res.stdout.decode() + rel_res.stderr.decode())
        self.assertFalse(self.harness.volume_exists(s_rel))
        self.assertFalse(os.path.exists(self.harness.lease_path(s_rel)))
        self.assertEqual(self._rescue_files(), [])

        # 2. Unleased built non-git vol:* slot -> list shows clean + acquirable, down succeeds
        s_list = self.acquire_vol("nogit-list", with_docs=False)
        shutil.rmtree(os.path.join(self.harness.slot_dir(s_list), ".git"))
        self.free_lease(s_list)
        for list_args in (("list",), ("list", "--fast")):
            listed = self.run_dc(*list_args, with_docs=False)
            self.assertEqual(listed.returncode, 0, listed.stderr.decode())
            row = next(l for l in listed.stdout.decode().splitlines() if l.split()[:1] == [s_list])
            self.assertIn("acquirable", row, f"{list_args}: {row}")
            if "--fast" not in list_args:
                self.assertIn("clean", row, f"{list_args}: {row}")

        down_ok = self.run_dc("down", s_list, with_docs=False)
        self.assertEqual(down_ok.returncode, 0, down_ok.stdout.decode() + down_ok.stderr.decode())
        self.assertFalse(self.harness.volume_exists(s_list))

        # 3. Leased non-git vol:* slot -> park releases instead of parking
        s_park = self.acquire_vol("nogit-park", with_docs=False)
        shutil.rmtree(os.path.join(self.harness.slot_dir(s_park), ".git"))
        park_res = self.run_dc("park", s_park, with_docs=False)
        self.assertEqual(park_res.returncode, 0, park_res.stdout.decode() + park_res.stderr.decode())
        self.assertFalse(os.path.exists(self.harness.lease_path(s_park)))
        self.assertFalse(os.path.exists(self.harness.parked_path(s_park)))
        self.run_dc("down", s_park, with_docs=False)

        # 4. Corrupt .git on vol:* slot -> unreadable blocks release, down, and list
        s_bad = self.acquire_vol("corrupt-git", with_docs=False)
        v_bad = self.harness.slot_dir(s_bad)
        shutil.rmtree(os.path.join(v_bad, ".git"))
        os.makedirs(os.path.join(v_bad, ".git"))

        rel_bad = self.run_dc("release", s_bad, "--for", "corrupt-git", with_docs=False)
        self.assertNotEqual(rel_bad.returncode, 0, rel_bad.stdout.decode() + rel_bad.stderr.decode())
        self.assertIn("unreadable", rel_bad.stderr.decode())
        self.assertTrue(self.harness.volume_exists(s_bad))
        self.assertTrue(os.path.isfile(self.harness.lease_path(s_bad)))

        self.free_lease(s_bad)
        listed_bad = self.run_dc("list", with_docs=False)
        self.assertEqual(listed_bad.returncode, 0, listed_bad.stderr.decode())
        row_bad = next(l for l in listed_bad.stdout.decode().splitlines() if l.split()[:1] == [s_bad])
        self.assertIn("unreadable", row_bad)
        self.assertIn("unreadable:facts", row_bad)

        down_bad = self.run_dc("down", s_bad, with_docs=False)
        self.assertNotEqual(down_bad.returncode, 0, down_bad.stdout.decode() + down_bad.stderr.decode())
        self.assertTrue(self.harness.volume_exists(s_bad))
        self.run_dc("down", s_bad, "--force", with_docs=False)

        # 5. Parity: host-dir slot without .git vs host-dir slot with corrupt .git
        host_clean = os.path.join(self.harness.slots_dir, "slot-91")
        host_bad = os.path.join(self.harness.slots_dir, "slot-92")
        for idx, (hname, hdir, corrupt) in enumerate((("slot-91", host_clean, False), ("slot-92", host_bad, True))):
            os.makedirs(os.path.join(hdir, "docs", "repo", ".git"), exist_ok=True)
            os.makedirs(os.path.join(hdir, ".devcontainer"), exist_ok=True)
            with open(os.path.join(hdir, ".devcontainer", ".slot-holder"), "w", encoding="utf-8") as f:
                f.write("holder=host-nogit\n")
            if corrupt:
                os.makedirs(os.path.join(hdir, ".git"), exist_ok=True)
            sid_h = self.harness.slot_project_of(hname)
            with open(os.path.join(self.harness.state_dir, "ports", f"{sid_h}.ports"), "w", encoding="utf-8") as f:
                f.write(f"SSH_PORT={43100 + idx}\nNOVNC_PORT={43110 + idx}\nVITE_DEV_PORT={43120 + idx}\n")
            with open(os.path.join(self.harness.state_dir, "env", f"{sid_h}.env"), "w", encoding="utf-8") as f:
                f.write("GIT_USER_NAME=demo-agent\nGIT_USER_EMAIL=demo-agent@example.com\n")

        env = self.harness._env(None)
        env.pop("DOCKER_STUB_STATE", None)
        env["TEST_DOCKER_STORE"] = self.harness.docker_state
        env["TEST_DOCKER_BASE"] = os.path.join(self.harness.stub_bin, "docker-base")
        env["DEV_CONTAINER_LIB"] = "1"
        probe = (
            '. "$0"\n'
            'slot_is_clean_for_teardown "$1" && echo "CLEAN_OK" || echo "CLEAN_FAIL"\n'
            'echo "COL=$(slot_dirty_column "$1")"\n'
            'slot_acquire_verdict "$1" "$2" 0 >/dev/null && echo "VERDICT=0:$ACQUIRE_REASON" || echo "VERDICT=1:$ACQUIRE_REASON"\n'
        )
        res_clean = subprocess.run(
            ["bash", "-c", probe, self.harness.script, host_clean, self.harness.slot_project_of("slot-91")],
            cwd=self.harness.tmp,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )
        out_clean = res_clean.stdout.decode()
        self.assertEqual(res_clean.returncode, 0, res_clean.stderr.decode())
        self.assertIn("CLEAN_OK", out_clean)
        self.assertIn("COL=clean", out_clean)
        self.assertIn("VERDICT=0:acquirable", out_clean)

        res_bad = subprocess.run(
            ["bash", "-c", probe, self.harness.script, host_bad, self.harness.slot_project_of("slot-92")],
            cwd=self.harness.tmp,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )
        out_bad = res_bad.stdout.decode()
        self.assertEqual(res_bad.returncode, 0, res_bad.stderr.decode())
        self.assertIn("CLEAN_FAIL", out_bad)
        self.assertIn("COL=unreadable", out_bad)
        self.assertIn("VERDICT=1:unreadable:git", out_bad)

        # CLI down on host-dir without .git succeeds, while corrupt .git refuses
        down_host_bad = self.run_dc("down", "slot-92", with_docs=False)
        self.assertNotEqual(down_host_bad.returncode, 0, down_host_bad.stdout.decode() + down_host_bad.stderr.decode())
        self.assertTrue(os.path.isdir(host_bad))
        down_host_ok = self.run_dc("down", "slot-91", with_docs=False)
        self.assertEqual(down_host_ok.returncode, 0, down_host_ok.stdout.decode() + down_host_ok.stderr.decode())
        self.assertFalse(os.path.exists(host_clean))


class TestVolumeRescueArchiveWithoutStubState(_NoStubStateVolumeCase):

    def _seed_dirty_volume_work(self, name):
        vdir = self.harness.slot_dir(name)
        git = self._git(vdir)

        tracked = os.path.join(vdir, "tracked.txt")

        # Unpushed branch + commit
        git("checkout", "-q", "-b", "feature-rescue")
        with open(os.path.join(vdir, "commit_work.txt"), "w", encoding="utf-8") as f:
            f.write("unpushed branch commit data\n")
        git("add", "commit_work.txt")
        git("commit", "-q", "-m", "feature rescue commit")

        # Full bundle for thin-bundle size comparison
        full_bundle = os.path.join(self.harness.tmp, f"{name}-full.bundle")
        subprocess.run(
            [self.harness.real_git_bin, "-C", vdir, "bundle", "create", full_bundle, "--all"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        # Working tree tracked edit
        with open(tracked, "w", encoding="utf-8") as f:
            f.write("modified tracked content\n")

        # Untracked normal file + .local files + ignored + node_modules
        with open(os.path.join(vdir, "untracked.txt"), "w", encoding="utf-8") as f:
            f.write("untracked file content\n")
        os.makedirs(os.path.join(vdir, ".local", "sub"), exist_ok=True)
        with open(os.path.join(vdir, ".local", "notes.txt"), "w", encoding="utf-8") as f:
            f.write("local notes\n")
        with open(os.path.join(vdir, ".local", "sub", "state.json"), "w", encoding="utf-8") as f:
            f.write('{"k":"v"}\n')
        with open(os.path.join(vdir, "ignored.log"), "w", encoding="utf-8") as f:
            f.write("ignored log data\n")
        nm_dir = os.path.join(vdir, "node_modules", "dep")
        os.makedirs(nm_dir, exist_ok=True)
        with open(os.path.join(nm_dir, "index.js"), "w", encoding="utf-8") as f:
            f.write("module.exports = {};\n")

        return full_bundle

    def _assert_archive_recoverable(self, name, full_bundle=None):
        archive_dir = os.path.join(self.rescue_dir, self.harness.project_key, name)
        self.assertTrue(os.path.isdir(archive_dir), f"missing rescue dir: {archive_dir}")

        bundle_path = os.path.join(archive_dir, "commits.bundle")
        patch_path = os.path.join(archive_dir, "changes.patch")
        tar_path = os.path.join(archive_dir, "new-files.tar")
        meta_path = os.path.join(archive_dir, "meta.json")
        for p in (bundle_path, patch_path, tar_path, meta_path):
            self.assertTrue(os.path.isfile(p), f"missing rescue artifact: {p}")

        if full_bundle is not None:
            self.assertLess(os.path.getsize(bundle_path), os.path.getsize(full_bundle))

        # Recover into a fresh clone of origin
        recover = os.path.join(self.harness.tmp, f"recover-{name}")
        subprocess.run(
            [self.harness.real_git_bin, "clone", "-q", self.remote, recover],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        subprocess.run(
            [self.harness.real_git_bin, "-C", recover, "fetch", bundle_path, "refs/heads/*:refs/heads/*"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        subprocess.run(
            [self.harness.real_git_bin, "-C", recover, "checkout", "-q", "feature-rescue"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        with open(os.path.join(recover, "commit_work.txt"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "unpushed branch commit data\n")

        subprocess.run(
            [self.harness.real_git_bin, "-C", recover, "apply", "--whitespace=nowarn", patch_path],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        with open(os.path.join(recover, "tracked.txt"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "modified tracked content\n")

        subprocess.run(["tar", "-xf", tar_path, "-C", recover], check=True)
        with open(os.path.join(recover, "untracked.txt"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "untracked file content\n")
        with open(os.path.join(recover, ".local", "notes.txt"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "local notes\n")
        with open(os.path.join(recover, ".local", "sub", "state.json"), encoding="utf-8") as f:
            self.assertEqual(f.read(), '{"k":"v"}\n')
        self.assertFalse(os.path.exists(os.path.join(recover, "ignored.log")))
        self.assertFalse(os.path.exists(os.path.join(recover, "node_modules")))

        with open(meta_path, encoding="utf-8") as f:
            meta = json.load(f)
        self.assertEqual(meta["project"], PROJECT_PREFIX)
        self.assertEqual(meta["lease"], name)
        self.assertIn("feature-rescue", meta["branches"])
        self.assertIn("ignored.log", meta["ignored"])
        for item in meta["ignored"]:
            self.assertNotIn("node_modules", item)
        self.assertEqual(meta["omitted"], [])
        self.assertIsInstance(meta["created_at"], int)

    def test_evict_stopped_and_running_dirty_volume_archives_before_teardown_with_zero_network(self):
        # Fail gh to verify zero network dependency during rescue archiving
        self.harness._write_stub("gh", "#!/usr/bin/env bash\necho 'network error' >&2\nexit 1\n")

        for stopped in (True, False):
            with self.subTest(stopped=stopped):
                shutil.rmtree(self.rescue_dir, ignore_errors=True)
                name = self.acquire_vol(f"evict-{'stopped' if stopped else 'running'}", with_docs=False)
                shutil.rmtree(os.path.join(self.harness.tmp, f"recover-{name}"), ignore_errors=True)
                full_bundle = self._seed_dirty_volume_work(name)
                if stopped:
                    self.harness.mark_stopped(name)

                refs_before = subprocess.run(
                    [self.harness.real_git_bin, "-C", self.remote, "for-each-ref", "--format=%(refname)"],
                    check=True,
                    stdout=subprocess.PIPE,
                ).stdout.decode()

                self.clear_logs()
                evict = self.run_dc("evict", name, "--yes", "--force", with_docs=False)
                self.assertEqual(evict.returncode, 0, evict.stdout.decode() + evict.stderr.decode())
                self.assertFalse(self.harness.volume_exists(name))
                self.assertFalse(os.path.exists(self.harness.lease_path(name)))

                # Verify helper/exec extraction occurred before volume rm / compose down -v
                calls = self.docker_vol_calls()
                rm_indices = [i for i, c in enumerate(calls) if c.startswith("volume rm ") or " down -v" in c]
                extract_indices = [
                    i
                    for i, c in enumerate(calls)
                    if (c.startswith("run ") and "--network none" in c) or " exec " in c
                ]
                self.assertTrue(rm_indices, calls)
                self.assertTrue(extract_indices, calls)
                self.assertLess(max(extract_indices), min(rm_indices), calls)

                refs_after = subprocess.run(
                    [self.harness.real_git_bin, "-C", self.remote, "for-each-ref", "--format=%(refname)"],
                    check=True,
                    stdout=subprocess.PIPE,
                ).stdout.decode()
                self.assertEqual(refs_before, refs_after, "evict must not push rescue branches")

                self._assert_archive_recoverable(name, full_bundle=full_bundle)

    def test_reap_archives_dirty_non_ephemeral_and_skips_archive_for_ephemeral_and_clean(self):
        since = 1700000000
        self.harness.set_now(since)

        s_dirty = self.acquire_vol("reap-dirty", with_docs=False, now=since)
        full_bundle = self._seed_dirty_volume_work(s_dirty)

        s_eph = self.acquire_vol("reap-eph", ephemeral=True, with_docs=False, now=since)
        with open(os.path.join(self.harness.slot_dir(s_eph), "eph_wip.txt"), "w", encoding="utf-8") as f:
            f.write("ephemeral discard\n")

        s_clean = self.acquire_vol("reap-clean", with_docs=False, now=since)
        nm_clean = os.path.join(self.harness.slot_dir(s_clean), "node_modules", "pkg")
        os.makedirs(nm_clean, exist_ok=True)
        with open(os.path.join(nm_clean, "a.js"), "w", encoding="utf-8") as f:
            f.write("ignored\n")

        self.harness.set_now(since + 7201)
        reap = self.run_dc("reap", with_docs=False, now=since + 7201)
        self.assertEqual(reap.returncode, 0, reap.stdout.decode() + reap.stderr.decode())

        for s in (s_dirty, s_eph, s_clean):
            self.assertFalse(self.harness.volume_exists(s), f"volume {s} survived reap")
            self.assertFalse(os.path.exists(self.harness.lease_path(s)), f"lease {s} survived reap")

        self._assert_archive_recoverable(s_dirty, full_bundle=full_bundle)
        self.assertFalse(
            os.path.exists(os.path.join(self.rescue_dir, self.harness.project_key, s_eph)),
            "ephemeral slot created a rescue archive",
        )
        self.assertFalse(
            os.path.exists(os.path.join(self.rescue_dir, self.harness.project_key, s_clean)),
            "clean slot created a rescue archive",
        )

    def test_clean_respects_size_limit_boundary_and_deduplicates_local_files(self):
        since = 1700000000
        self.harness.set_now(since)
        name = self.acquire_vol("clean-limit", with_docs=False, now=since)
        vdir = self.harness.slot_dir(name)

        # .local file NOT gitignored must not be duplicated or double-counted against limit
        os.makedirs(os.path.join(vdir, ".local"), exist_ok=True)
        with open(os.path.join(vdir, ".local", "exact.txt"), "wb") as f:
            f.write(b"a" * 100)
        with open(os.path.join(vdir, "fit.txt"), "wb") as f:
            f.write(b"b" * 100)
        with open(os.path.join(vdir, "over.txt"), "wb") as f:
            f.write(b"c" * 201)

        self.expire_lease(name)
        res = self.run_dc(
            "clean",
            port_overrides={"DEV_CONTAINER_ARCHIVE_SIZE_LIMIT": "200"},
            with_docs=False,
            now=since + 10000,
        )
        self.assertEqual(res.returncode, 0, res.stdout.decode() + res.stderr.decode())
        self.assertFalse(self.harness.volume_exists(name))

        archive_dir = os.path.join(self.rescue_dir, self.harness.project_key, name)
        tar_path = os.path.join(archive_dir, "new-files.tar")
        self.assertTrue(os.path.isfile(tar_path))
        tar_list = subprocess.run(["tar", "-tf", tar_path], check=True, stdout=subprocess.PIPE).stdout.decode().splitlines()
        self.assertIn(".local/exact.txt", tar_list)
        self.assertIn("fit.txt", tar_list)
        self.assertEqual(tar_list.count(".local/exact.txt"), 1)
        self.assertNotIn("over.txt", tar_list)

        with open(os.path.join(archive_dir, "meta.json"), encoding="utf-8") as f:
            meta = json.load(f)
        self.assertEqual(meta["omitted"], [{"name": "over.txt", "size": 201}])

    def test_archive_binary_diff_and_uncommitted_only_skips_bundle(self):
        name = self.acquire_vol("bin-diff", with_docs=False)
        vdir = self.harness.slot_dir(name)
        git = self._git(vdir)

        bin_path = os.path.join(vdir, "blob.bin")
        with open(bin_path, "wb") as f:
            f.write(b"\x00\x01\x02orig\xff\xfe")
        git("add", "blob.bin")
        git("commit", "-q", "-m", "add binary")
        git("push", "-q", "origin", "master")

        updated_bytes = b"\x00\x01\x02changed\x89PNG\r\n\x1a\n\x00"
        with open(bin_path, "wb") as f:
            f.write(updated_bytes)

        evict = self.run_dc("evict", name, "--yes", "--force", with_docs=False)
        self.assertEqual(evict.returncode, 0, evict.stdout.decode() + evict.stderr.decode())
        self.assertFalse(self.harness.volume_exists(name))

        archive_dir = os.path.join(self.rescue_dir, self.harness.project_key, name)
        self.assertFalse(os.path.exists(os.path.join(archive_dir, "commits.bundle")))
        patch_path = os.path.join(archive_dir, "changes.patch")
        self.assertTrue(os.path.isfile(patch_path))

        recover = os.path.join(self.harness.tmp, "recover-bin")
        subprocess.run(
            [self.harness.real_git_bin, "clone", "-q", self.remote, recover],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        subprocess.run(
            [self.harness.real_git_bin, "-C", recover, "apply", "--whitespace=nowarn", patch_path],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        with open(os.path.join(recover, "blob.bin"), "rb") as f:
            self.assertEqual(f.read(), updated_bytes)

    def test_failed_archive_parks_volume_slot_and_preserves_volume(self):
        since = 1700000000
        self.harness.set_now(since)

        # Case 1: helper extraction fails during archive
        s1 = self.acquire_vol("arch-fail-helper", with_docs=False, now=since)
        sid1 = self.harness.slot_project_of(s1)
        with open(os.path.join(self.harness.slot_dir(s1), "important.txt"), "w", encoding="utf-8") as f:
            f.write("must not be lost\n")
        self.expire_lease(s1)
        self.harness.mark_stopped(s1)
        with open(os.path.join(self.harness.docker_state, f"{sid1}.helperfail"), "w", encoding="utf-8"):
            pass

        reap1 = self.run_dc("reap", with_docs=False, now=since + 10000)
        self.assertTrue(self.harness.volume_exists(s1), "volume removed despite helper failure")
        self.assertTrue(os.path.isfile(self.harness.parked_path(s1)), "slot not parked on helper failure")
        with open(self.harness.parked_path(s1), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["holder"], "archive-failed")

        # Cleanup marker so second case can inspect rescue dir cleanly
        os.remove(os.path.join(self.harness.docker_state, f"{sid1}.helperfail"))
        os.remove(self.harness.parked_path(s1))
        self.run_dc("down", s1, "--force", with_docs=False)

        # Case 2: host rescue dir creation blocked
        s2 = self.acquire_vol("arch-fail-host", with_docs=False, now=since)
        with open(os.path.join(self.harness.slot_dir(s2), "important2.txt"), "w", encoding="utf-8") as f:
            f.write("must not be lost 2\n")
        self.expire_lease(s2)

        os.makedirs(self.rescue_dir, exist_ok=True)
        blocker = os.path.join(self.rescue_dir, self.harness.project_key)
        with open(blocker, "w", encoding="utf-8") as f:
            f.write("blocker\n")
        try:
            self.run_dc("reap", with_docs=False, now=since + 10000)
            self.assertTrue(self.harness.volume_exists(s2), "volume removed despite rescue mkdir failure")
            self.assertTrue(os.path.isfile(self.harness.parked_path(s2)), "slot not parked on host blocker")
        finally:
            if os.path.isfile(blocker):
                os.remove(blocker)

    def test_existing_archive_preserves_sentinel_and_legacy_guard_refuses(self):
        since = 1700000000
        self.harness.set_now(since)

        # Existing archive sentinel is not overwritten
        s1 = self.acquire_vol("crash-sentinel", with_docs=False, now=since)
        with open(os.path.join(self.harness.slot_dir(s1), "work.txt"), "w", encoding="utf-8") as f:
            f.write("work\n")
        target = os.path.join(self.rescue_dir, self.harness.project_key, s1)
        os.makedirs(target, exist_ok=True)
        sentinel = os.path.join(target, "sentinel.txt")
        with open(sentinel, "w", encoding="utf-8") as f:
            f.write("keep me\n")
        self.expire_lease(s1)

        res1 = self.run_dc("clean", with_docs=False, now=since + 10000)
        self.assertEqual(res1.returncode, 0, res1.stdout.decode() + res1.stderr.decode())
        self.assertFalse(self.harness.volume_exists(s1))
        with open(sentinel, encoding="utf-8") as f:
            self.assertEqual(f.read(), "keep me\n")

        # Legacy label-named rescue dir without key dir refuses without deleting volume
        shutil.rmtree(self.rescue_dir, ignore_errors=True)
        legacy = os.path.join(self.rescue_dir, PROJECT_PREFIX, "old-slot")
        os.makedirs(legacy, exist_ok=True)
        with open(os.path.join(legacy, "note.txt"), "w", encoding="utf-8") as f:
            f.write("legacy\n")

        s2 = self.acquire_vol("legacy-guard", with_docs=False, now=since)
        with open(os.path.join(self.harness.slot_dir(s2), "work.txt"), "w", encoding="utf-8") as f:
            f.write("work\n")
        self.expire_lease(s2)

        res2 = self.run_dc("evict", s2, "--yes", "--force", with_docs=False, now=since + 10000)
        combined = res2.stdout.decode() + res2.stderr.decode()
        self.assertNotEqual(res2.returncode, 0, combined)
        self.assertIn("migrate-identity", combined)
        self.assertTrue(self.harness.volume_exists(s2), "volume removed despite rescue_legacy_guard refusal")

        os.makedirs(os.path.join(self.rescue_dir, self.harness.project_key), exist_ok=True)
        res3 = self.run_dc("evict", s2, "--yes", "--force", with_docs=False, now=since + 10000)
        self.assertEqual(res3.returncode, 0, res3.stdout.decode() + res3.stderr.decode())
        self.assertFalse(self.harness.volume_exists(s2))
        self.assertTrue(
            os.path.isfile(os.path.join(self.rescue_dir, self.harness.project_key, s2, "new-files.tar")),
            "dirty volume was not archived after key rescue dir became present",
        )

    def test_non_repo_volume_and_host_slot_removed_by_reap_and_evict_without_parking(self):
        since = 1700000000
        self.harness.set_now(since)

        # 1. Expired non-ephemeral vol:* slot whose workspace has no .git -> reaped cleanly
        s_reap_leased = self.acquire_vol("nogit-reap-leased", with_docs=False, now=since)
        shutil.rmtree(os.path.join(self.harness.slot_dir(s_reap_leased), ".git"))
        self.expire_lease(s_reap_leased)

        # 2. Unleased stopped half-provisioned vol:* slot without .git -> reaped cleanly
        s_reap_unleased = self.acquire_vol("nogit-reap-unleased", with_docs=False, now=since)
        v_unleased = self.harness.slot_dir(s_reap_unleased)
        shutil.rmtree(os.path.join(v_unleased, ".git"))
        self.free_lease(s_reap_unleased)
        self.harness.mark_stopped(s_reap_unleased)

        reaped = self.run_dc("reap", with_docs=False, now=since + 10000)
        self.assertEqual(reaped.returncode, 0, reaped.stdout.decode() + reaped.stderr.decode())
        for s in (s_reap_leased, s_reap_unleased):
            self.assertFalse(self.harness.volume_exists(s), f"non-repo volume {s} survived reap")
            self.assertFalse(os.path.exists(self.harness.lease_path(s)), f"lease {s} survived reap")
            self.assertFalse(os.path.exists(self.harness.parked_path(s)), f"non-repo slot {s} was parked")

        # 3. Non-ephemeral vol:* slot without .git -> evict removes without parking
        s_evict = self.acquire_vol("nogit-evict", with_docs=False, now=since)
        shutil.rmtree(os.path.join(self.harness.slot_dir(s_evict), ".git"))
        evicted = self.run_dc("evict", s_evict, "--yes", "--force", with_docs=False, now=since + 10000)
        self.assertEqual(evicted.returncode, 0, evicted.stdout.decode() + evicted.stderr.decode())
        self.assertFalse(self.harness.volume_exists(s_evict))
        self.assertFalse(os.path.exists(self.harness.lease_path(s_evict)))
        self.assertFalse(os.path.exists(self.harness.parked_path(s_evict)))

        # 4. Parity: non-repo host-dir slot -> archive_and_clean_slot removes without parking
        host_name = "slot-9"
        host_sid = self.harness.slot_project_of(host_name)
        host_dir = os.path.join(self.harness.slots_dir, host_name)
        os.makedirs(os.path.join(host_dir, ".devcontainer"), exist_ok=True)
        with open(os.path.join(host_dir, ".devcontainer", ".slot-holder"), "w", encoding="utf-8") as f:
            f.write("holder=half-provisioned\n")
        env = self.harness._env(None)
        env.pop("DOCKER_STUB_STATE", None)
        env["TEST_DOCKER_STORE"] = self.harness.docker_state
        env["TEST_DOCKER_BASE"] = os.path.join(self.harness.stub_bin, "docker-base")
        env["DEV_CONTAINER_LIB"] = "1"
        host_res = subprocess.run(
            ["bash", "-c", '. "$0"; archive_and_clean_slot "$1" "$2" "$3"', self.harness.script, host_dir, host_sid, host_name],
            cwd=self.harness.tmp,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
        )
        self.assertEqual(host_res.returncode, 0, host_res.stdout.decode() + host_res.stderr.decode())
        self.assertFalse(os.path.exists(host_dir))
        self.assertFalse(os.path.exists(self.harness.parked_path(host_name)))
        self.assertEqual(self._rescue_files(), [])


if __name__ == "__main__":
    unittest.main()
