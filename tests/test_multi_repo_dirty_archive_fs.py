"""Tests for #50: batched multi-repo facts_of, dirty/delivery checks,
prune_delivered_branches, rescue_and_reset, reset_slot cleanup,
multi-repo volume rescue archiving, and slot-fs ro guard + diff subdir.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

if os.path.dirname(__file__) not in sys.path:
    sys.path.insert(0, os.path.dirname(__file__))

from harness import (
    DOCKER_STUB,
    GIT_STUB,
    PROJECT_PREFIX,
    REPO_ROOT,
    SCRIPT_REL,
    DevContainerHarness,
    DevContainerTestCase,
    project_env_text,
)

ENGINE = os.path.join(REPO_ROOT, SCRIPT_REL)
IDENTITY_ENV = (
    "GIT_USER_NAME=demo-agent\n"
    "GIT_USER_EMAIL=demo-agent@example.com\n"
)


def output_of(result):
    return result.stdout.decode(errors="replace") + result.stderr.decode(errors="replace")


def sha256_hex(data):
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


DOCKER_FACTS_WRAPPER = r"""#!/usr/bin/env bash
set -u
log="${DOCKER_WRAPPER_LOG:-/dev/null}"
state_dir="${DOCKER_STUB_STATE:-/tmp}"
base="$(dirname "$0")/docker-base"

verb="" proj="" svc=""
cmd=()

if [[ "${1:-}" == compose ]]; then
  i=2
  while (( i <= $# )); do
    arg="${!i}"
    case "$arg" in
      -p)
        i=$((i + 1))
        proj="${!i}"
        ;;
      exec|run|ps|config|up|down|stop|build|ls)
        verb="$arg"
        i=$((i + 1))
        break
        ;;
    esac
    i=$((i + 1))
  done

  if [[ "$verb" == exec || "$verb" == run ]]; then
    while (( i <= $# )); do
      arg="${!i}"
      case "$arg" in
        -T|-u|-w|--rm|--no-deps) ;;
        -*) ;;
        *)
          svc="$arg"
          i=$((i + 1))
          while (( i <= $# )); do
            cmd+=("${!i}")
            i=$((i + 1))
          done
          break
          ;;
      esac
      i=$((i + 1))
    done
  fi
fi

if [[ "$verb" == exec || "$verb" == run ]]; then
  printf '%s\t%s\t%s\t%s\n' "$verb" "$proj" "$svc" "${cmd[0]-}" >> "$log.tsv"
  if [[ -n "$proj" && -f "$state_dir/$proj.fail" ]]; then
    echo "container execution failed" >&2
    exit 1
  fi
  if [[ -n "${WORKSPACE_DIR:-}" && -d "$WORKSPACE_DIR" ]]; then
    cd "$WORKSPACE_DIR"
  elif [[ -n "$proj" && -d "$state_dir/volumes/${proj}-workspace" ]]; then
    cd "$state_dir/volumes/${proj}-workspace"
  fi
  if (( ${#cmd[@]} > 0 )); then
    exec "${cmd[@]}"
  fi
  exit 0
fi

exec "$base" "$@"
"""


GIT_PWD_URL_WRAPPER = r"""#!/usr/bin/env bash
set -u
real_git="${REAL_GIT_BIN:?REAL_GIT_BIN must be set}"
declare -a c_args=()
if [[ -n "${TEST_GIT_URL_MAP:-}" && -f "${TEST_GIT_URL_MAP:-}" ]]; then
  while IFS=$'\t' read -r remote_url local_path; do
    [[ -n "$remote_url" && -n "$local_path" ]] || continue
    c_args+=(-c "url.${local_path}.insteadOf=${remote_url}")
  done < "$TEST_GIT_URL_MAP"
fi

if [[ -n "${TEST_GIT_PWD_LOG:-}" ]]; then
  target="$PWD"
  prev=""
  for a in "$@"; do
    if [[ "$prev" == "-C" ]]; then
      if [[ "$a" == /* ]]; then
        target="$a"
      else
        target="$PWD/$a"
      fi
    fi
    prev="$a"
  done
  canon_target=$(cd "$target" 2>/dev/null && pwd -P || echo "$target")
  printf '%s|%s\n' "$canon_target" "$*" >> "$TEST_GIT_PWD_LOG"
fi

if [[ -n "${GIT_STUB_CALL_LOG:-}" ]]; then
  printf 'GIT_OPTIONAL_LOCKS=%s %s\n' "${GIT_OPTIONAL_LOCKS:-}" "$*" >> "$GIT_STUB_CALL_LOG"
fi
exec "$real_git" "${c_args[@]}" "$@"
"""


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
    for item in .devcontainer project.env config; do
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
      grep -Fxq "$p" "$excl" 2>/dev/null || echo "$p" >>"$excl"
    done
  fi
  exit 0
fi

exec "$base" "$@"
"""


DOCKER_CAPTURE_WRAPPER = r"""#!/usr/bin/env bash
set -u
here="$(dirname "$0")"
if [[ -n "${DOCKER_CAPTURE_DIR:-}" ]]; then
  idx=0
  while [[ -e "$DOCKER_CAPTURE_DIR/$idx.argv" ]]; do
    idx=$((idx + 1))
  done
  printf '%s\n' "$@" > "$DOCKER_CAPTURE_DIR/$idx.argv"
  env > "$DOCKER_CAPTURE_DIR/$idx.env"
  if [[ "${1:-}" == "run" ]] || [[ "${1:-}" == "compose" && " $* " == *" exec "* ]] || [[ "${1:-}" == "compose" && " $* " == *" run "* ]]; then
    cat > "$DOCKER_CAPTURE_DIR/$idx.stdin"
    exec "$here/docker-base" "$@" < "$DOCKER_CAPTURE_DIR/$idx.stdin"
  fi
fi
exec "$here/docker-base" "$@"
"""


class MultiRepoSetupMixin:
    """Shared local-bare-repo and project fixture helpers (zero network)."""

    def _init_wrappers(self, *, docker_wrapper=None, git_wrapper=GIT_PWD_URL_WRAPPER):
        self.url_map_file = os.path.join(self.harness.tmp, "git-url-map.tsv")
        self.url_map = {}
        self.git_pwd_log = os.path.join(self.harness.tmp, "git-pwd.log")
        self.harness._write_stub("docker-base", DOCKER_STUB)
        if docker_wrapper is not None:
            self.harness._write_stub("docker", docker_wrapper)
        self.harness._write_stub("git-base", GIT_STUB)
        self.harness._write_stub("git", git_wrapper)

    def real_git(self, repo, *args):
        c_args = []
        for remote_url, local_path in self.url_map.items():
            c_args.extend(["-c", f"url.{local_path}.insteadOf={remote_url}"])
        return subprocess.run(
            [
                self.harness.real_git_bin,
                "-C",
                repo,
                *c_args,
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
        ).stdout.decode().strip()

    def map_url(self, remote_url, local_bare):
        self.url_map[remote_url] = local_bare
        with open(self.url_map_file, "w", encoding="utf-8") as handle:
            for k, v in self.url_map.items():
                handle.write(f"{k}\t{v}\n")

    def make_bare_repo(self, name, *, commits=("c1", "c2"), extra_branch=None, github_url=None, default_branch="main"):
        bare = os.path.join(self.harness.tmp, "remotes", f"{name}.git")
        work = os.path.join(self.harness.tmp, "work", name)
        os.makedirs(bare, exist_ok=True)
        subprocess.run([self.harness.real_git_bin, "init", "-q", "--bare", bare], check=True)
        subprocess.run(
            [self.harness.real_git_bin, "-C", bare, "symbolic-ref", "HEAD", f"refs/heads/{default_branch}"],
            check=True,
        )
        subprocess.run([self.harness.real_git_bin, "clone", "-q", bare, work], check=True)
        self.real_git(work, "checkout", "-q", "-B", default_branch)
        shas = []
        for idx, msg in enumerate(commits):
            fname = f"{name}-{idx}.txt"
            with open(os.path.join(work, fname), "w", encoding="utf-8") as handle:
                handle.write(f"{msg}\n")
            self.real_git(work, "add", fname)
            self.real_git(work, "commit", "-q", "-m", msg)
            shas.append(self.real_git(work, "rev-parse", "HEAD"))
        self.real_git(work, "push", "-q", "origin", default_branch)
        branch_sha = None
        if extra_branch:
            self.real_git(work, "checkout", "-q", "-b", extra_branch)
            with open(os.path.join(work, "branch-only.txt"), "w", encoding="utf-8") as handle:
                handle.write(f"on {extra_branch}\n")
            self.real_git(work, "add", "branch-only.txt")
            self.real_git(work, "commit", "-q", "-m", f"branch {extra_branch}")
            branch_sha = self.real_git(work, "rev-parse", "HEAD")
            self.real_git(work, "push", "-q", "origin", extra_branch)
        if github_url:
            self.map_url(github_url, bare)
            return github_url, {"bare": bare, "work": work, "shas": shas, "branch_sha": branch_sha}
        return bare, {"bare": bare, "work": work, "shas": shas, "branch_sha": branch_sha}

    def build_project(
        self,
        *,
        repos_catalog=None,
        repos_rel=".devcontainer/repos.json",
        project_env_extra=None,
        parent_env=IDENTITY_ENV,
        origin_url=None,
        pin_files=None,
        tracked_files=None,
        default_branch="main",
    ):
        extra = dict(project_env_extra or {})
        if repos_catalog is not None:
            extra.setdefault("SLOT_REPOS_FILE", repos_rel)
        self.harness.build_fixture_repo(
            with_parent_env=parent_env,
            project_env=project_env_text(DEFAULT_BRANCH=default_branch, **extra),
        )
        repo = self.harness.fixture_repo
        self.real_git(repo, "branch", "-M", default_branch)
        with open(os.path.join(repo, ".gitignore"), "a", encoding="utf-8") as handle:
            handle.write("ignored.log\n.local/\nnode_modules/\n")
        self.real_git(repo, "add", ".gitignore")
        if repos_catalog is not None:
            repos_path = os.path.join(repo, extra["SLOT_REPOS_FILE"])
            os.makedirs(os.path.dirname(repos_path), exist_ok=True)
            with open(repos_path, "w", encoding="utf-8") as handle:
                json.dump(repos_catalog, handle)
            self.real_git(repo, "add", extra["SLOT_REPOS_FILE"])
        if pin_files:
            for rel, content in pin_files.items():
                full = os.path.join(repo, rel)
                os.makedirs(os.path.dirname(full), exist_ok=True)
                with open(full, "w", encoding="utf-8") as handle:
                    handle.write(content)
                self.real_git(repo, "add", rel)
        if tracked_files:
            for rel, content in tracked_files.items():
                full = os.path.join(repo, rel)
                os.makedirs(os.path.dirname(full), exist_ok=True)
                with open(full, "w", encoding="utf-8") as handle:
                    handle.write(content)
                self.real_git(repo, "add", rel)
        self.real_git(repo, "commit", "-q", "-m", "project setup")

        bare_origin = os.path.join(self.harness.tmp, "primary-origin.git")
        subprocess.run([self.harness.real_git_bin, "init", "-q", "--bare", bare_origin], check=True)
        subprocess.run(
            [self.harness.real_git_bin, "-C", bare_origin, "symbolic-ref", "HEAD", f"refs/heads/{default_branch}"],
            check=True,
        )
        self.real_git(repo, "remote", "set-url", "origin", bare_origin)
        self.real_git(repo, "push", "-q", "origin", default_branch)
        self.real_git(repo, "fetch", "-q", "origin")
        if origin_url:
            self.map_url(origin_url, bare_origin)
            self.real_git(repo, "remote", "set-url", "origin", origin_url)
        return bare_origin

    def run_engine(self, *args, unset_docs=True, extra_env=None, input=None, now=None):
        overrides = {
            "TEST_GIT_URL_MAP": self.url_map_file,
            "TEST_GIT_PWD_LOG": self.git_pwd_log,
        }
        if extra_env:
            overrides.update(extra_env)
        real_env = self.harness._env
        with mock.patch.object(
            self.harness,
            "_env",
            side_effect=lambda port_overrides=None: self._patched_env(
                real_env(port_overrides), overrides, unset_docs=unset_docs
            ),
        ):
            return self.harness.run(*args, input=input, now=now)

    @staticmethod
    def _patched_env(env, overrides, *, unset_docs):
        if unset_docs:
            env.pop("DOCS_REPO_URL", None)
        env.update(overrides)
        return env


class TestMultiRepoFactsCollection(DevContainerTestCase, MultiRepoSetupMixin):
    """Part 1A: batched multi-repo facts_of in a single container exec and pinned ro fetch-skip."""

    def setUp(self):
        super().setUp()
        self.scratch = os.path.join(self.harness.tmp, "scratch")
        os.makedirs(os.path.join(self.scratch, "facts"), exist_ok=True)
        self.docker_wrapper_log = os.path.join(self.harness.tmp, "docker-facts.log")
        self._init_wrappers(docker_wrapper=DOCKER_FACTS_WRAPPER)
        self.harness._write_stub("gh", "#!/usr/bin/env bash\necho '[]'\n")

    def _docker_calls(self):
        tsv_path = self.docker_wrapper_log + ".tsv"
        if not os.path.exists(tsv_path):
            return []
        calls = []
        with open(tsv_path, encoding="utf-8") as handle:
            for line in handle:
                line = line.rstrip("\n")
                if not line:
                    continue
                verb, proj, svc, cmd = line.split("\t", 3)
                calls.append({"verb": verb, "proj": proj, "svc": svc, "cmd": cmd})
        return calls

    def _clear_logs(self):
        for p in (self.docker_wrapper_log + ".tsv", self.git_pwd_log):
            if os.path.exists(p):
                os.remove(p)

    def _git_fetch_dirs(self):
        if not os.path.exists(self.git_pwd_log):
            return []
        dirs = []
        with open(self.git_pwd_log, encoding="utf-8") as handle:
            for line in handle:
                line = line.rstrip("\n")
                if "|" not in line:
                    continue
                target, args = line.split("|", 1)
                words = args.split()
                if "fetch" in words:
                    dirs.append(target)
        return dirs

    def _run_bash(self, script, *args, unset_docs=True, extra_env=None):
        env = self.harness._env(extra_env)
        if unset_docs:
            env.pop("DOCS_REPO_URL", None)
        env["DEV_CONTAINER_LIB"] = "1"
        env["SCRATCH"] = self.scratch
        env["DOCKER_WRAPPER_LOG"] = self.docker_wrapper_log
        env["TEST_GIT_URL_MAP"] = self.url_map_file
        env["TEST_GIT_PWD_LOG"] = self.git_pwd_log
        cmd = f'. "$0"\n{script}'
        return subprocess.run(
            ["bash", "-c", cmd, self.harness.script, *args],
            cwd=self.harness.fixture_repo,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )

    def _plant_subrepo_clone(self, slot_dir, rel_path, remote_url, *, branch="main", pin=None):
        dest = os.path.join(slot_dir, rel_path)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        local = self.url_map.get(remote_url, remote_url)
        subprocess.run(
            [self.harness.real_git_bin, "clone", "-q", local, dest],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.real_git(dest, "remote", "set-url", "origin", remote_url)
        if pin is not None:
            self.real_git(dest, "checkout", "-q", "--detach", pin)
            gitdir = self.real_git(dest, "rev-parse", "--absolute-git-dir")
            with open(os.path.join(gitdir, "slot-docs-pin"), "w", encoding="utf-8") as handle:
                handle.write(pin + "\n")
            for b in self.real_git(dest, "for-each-ref", "--format=%(refname:short)", "refs/heads/").splitlines():
                if b.strip():
                    self.real_git(dest, "branch", "-D", b.strip())
        elif branch:
            self.real_git(dest, "checkout", "-q", branch)
        return dest

    def test_facts_of_batches_root_and_present_attached_repos_in_single_exec(self):
        specs_url, specs_info = self.make_bare_repo(
            "specs", commits=("s1", "s2"), github_url="https://github.com/acme/specs.git"
        )
        notes_url, _ = self.make_bare_repo(
            "notes", commits=("n1",), github_url="https://github.com/acme/notes.git"
        )
        tools_url, _ = self.make_bare_repo(
            "tools", commits=("t1",), github_url="https://github.com/acme/tools.git"
        )
        extra_url, _ = self.make_bare_repo(
            "extra", commits=("e1",), github_url="https://github.com/acme/extra.git"
        )
        pin_sha = specs_info["shas"][0]
        catalog = {
            "version": 1,
            "repos": [
                {
                    "alias": "specs",
                    "path": "deps/specs",
                    "url": specs_url,
                    "mode": "ro",
                    "attach": "default",
                    "pin_file": ".github/specs-pin",
                },
                {
                    "alias": "notes",
                    "path": "deps/notes",
                    "url": notes_url,
                    "mode": "rw",
                    "attach": "default",
                },
                {
                    "alias": "tools",
                    "path": "extras/tools",
                    "url": tools_url,
                    "mode": "rw",
                    "attach": "optional",
                },
            ],
        }
        bare_origin = self.build_project(
            repos_catalog=catalog,
            pin_files={".github/specs-pin": pin_sha + "\n"},
            origin_url="https://github.com/acme/primary.git",
        )
        self.harness.make_bare_slot("slot-1", local_identity=("demo-agent", "demo-agent@example.com"))
        sid = f"{PROJECT_PREFIX}-slot-1"
        slot_dir = self.harness.slot_dir("slot-1")
        shutil.rmtree(os.path.join(slot_dir, "docs"), ignore_errors=True)
        self.real_git(slot_dir, "remote", "set-url", "origin", "https://github.com/acme/primary.git")
        self.map_url("https://github.com/acme/primary.git", bare_origin)
        self.real_git(slot_dir, "fetch", "-q", "origin")
        self.real_git(slot_dir, "checkout", "-f", "-q", "-B", "main", "origin/main")

        self._plant_subrepo_clone(slot_dir, "deps/specs", specs_url, pin=pin_sha)
        notes_dir = self._plant_subrepo_clone(slot_dir, "deps/notes", notes_url)
        with open(os.path.join(notes_dir, "note-edit.txt"), "w", encoding="utf-8") as handle:
            handle.write("uncommitted note\n")

        # Also plant an attached repo tracked only in $STATE/repos/$sid.json
        extra_dir = self._plant_subrepo_clone(slot_dir, "extras/state-only", extra_url)
        os.makedirs(os.path.join(self.harness.state_dir, "repos"), exist_ok=True)
        with open(os.path.join(self.harness.state_dir, "repos", f"{sid}.json"), "w", encoding="utf-8") as handle:
            json.dump(
                [
                    {"alias": "specs", "path": "deps/specs", "slug": "acme/specs", "mode": "ro", "attach": "default"},
                    {"alias": "notes", "path": "deps/notes", "slug": "acme/notes", "mode": "rw", "attach": "default"},
                    {"alias": "state-only", "path": "extras/state-only", "slug": "acme/extra", "mode": "rw", "attach": "optional"},
                ],
                handle,
            )
        # extras/tools is optional and ABSENT from slot_dir

        self.harness.mark_running("slot-1")
        self._clear_logs()

        res = self._run_bash('facts_of "$1"', sid)
        self.assertEqual(res.returncode, 0, output_of(res))

        calls = [c for c in self._docker_calls() if c["proj"] == sid]
        self.assertEqual(len(calls), 1, f"expected single slot_sh call, got {len(calls)}: {calls}")

        root_cache = os.path.join(self.scratch, "facts", sid)
        specs_cache = os.path.join(self.scratch, "facts", f"{sid}__deps_specs")
        notes_cache = os.path.join(self.scratch, "facts", f"{sid}__deps_notes")
        extra_cache = os.path.join(self.scratch, "facts", f"{sid}__extras_state-only")
        absent_cache = os.path.join(self.scratch, "facts", f"{sid}__extras_tools")

        self.assertTrue(os.path.isfile(root_cache))
        self.assertTrue(os.path.isfile(specs_cache), "batched facts_of did not populate deps/specs cache")
        self.assertTrue(os.path.isfile(notes_cache), "batched facts_of did not populate deps/notes cache")
        self.assertTrue(os.path.isfile(extra_cache), "batched facts_of did not populate state-only repo cache")
        self.assertFalse(os.path.exists(absent_cache), "absent optional repo produced a facts file")

        with open(root_cache, encoding="utf-8") as handle:
            root_text = handle.read()
        self.assertEqual(res.stdout.decode(), root_text)
        self.assertIn("clone\tdeps/specs", root_text)
        self.assertIn("clone\tdeps/notes", root_text)
        self.assertIn("clone\textras/state-only", root_text)
        self.assertNotIn("clone\textras/tools", root_text)
        self.assertNotIn(specs_url, root_text, "sub-repo facts leaked into root facts file")

        # Subsequent facts_of "$sid" "$sub" hits cache with zero extra container execs
        self._clear_logs()
        for sub, expected_needle in (
            ("deps/specs", f"origin\t{specs_url}"),
            ("deps/notes", "status\t?? note-edit.txt"),
            ("extras/state-only", f"origin\t{extra_url}"),
        ):
            sub_res = self._run_bash('facts_of "$1" "$2"', sid, sub)
            self.assertEqual(sub_res.returncode, 0, output_of(sub_res))
            self.assertEqual(self._docker_calls(), [], f"cache miss triggered container exec for {sub}")
            self.assertIn(expected_needle, sub_res.stdout.decode())

        # facts_reset clears root and all sub-repo batched cache files
        rst = self._run_bash('facts_reset "$1"', sid)
        self.assertEqual(rst.returncode, 0, output_of(rst))
        self.assertFalse(os.path.exists(root_cache))
        self.assertFalse(os.path.exists(specs_cache))
        self.assertFalse(os.path.exists(notes_cache))
        self.assertFalse(os.path.exists(extra_cache))

    def test_facts_of_detects_gitfile_subrepo_handles_root_norepo_and_corrupt_subrepo(self):
        notes_url, _ = self.make_bare_repo("notes", commits=("n1",), github_url="https://github.com/acme/notes.git")
        gitfile_url, _ = self.make_bare_repo("gf", commits=("g1",), github_url="https://github.com/acme/gf.git")
        catalog = {
            "version": 1,
            "repos": [
                {"alias": "notes", "path": "deps/notes", "url": notes_url, "mode": "rw", "attach": "default"},
                {"alias": "gf", "path": "deps/gf", "url": gitfile_url, "mode": "rw", "attach": "default"},
                {"alias": "bad", "path": "deps/bad", "url": "https://github.com/acme/bad.git", "mode": "rw", "attach": "default"},
            ],
        }
        self.build_project(repos_catalog=catalog, origin_url="https://github.com/acme/primary.git")
        self.harness.make_bare_slot("slot-2", local_identity=("demo-agent", "demo-agent@example.com"))
        sid = f"{PROJECT_PREFIX}-slot-2"
        slot_dir = self.harness.slot_dir("slot-2")
        shutil.rmtree(os.path.join(slot_dir, ".git"))
        shutil.rmtree(os.path.join(slot_dir, "docs"), ignore_errors=True)

        # Normal sub-repo
        self._plant_subrepo_clone(slot_dir, "deps/notes", notes_url)

        # Sub-repo where .git is a gitfile ([ -f "$c/.git" ]) rather than a directory
        gf_gitdir = os.path.join(self.harness.tmp, "separate-gitdirs", "gf.git")
        gf_dir = os.path.join(slot_dir, "deps", "gf")
        os.makedirs(os.path.dirname(gf_dir), exist_ok=True)
        os.makedirs(os.path.dirname(gf_gitdir), exist_ok=True)
        subprocess.run(
            [self.harness.real_git_bin, "clone", "-q", f"--separate-git-dir={gf_gitdir}", self.url_map[gitfile_url], gf_dir],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.real_git(gf_dir, "remote", "set-url", "origin", gitfile_url)
        self.assertTrue(os.path.isfile(os.path.join(gf_dir, ".git")))

        # Corrupt sub-repo
        os.makedirs(os.path.join(slot_dir, "deps", "bad", ".git"), exist_ok=True)

        self._clear_logs()
        res = self._run_bash('facts_of "$1"', sid)
        self.assertEqual(res.returncode, 0, output_of(res))
        self.assertEqual(len([c for c in self._docker_calls() if c["proj"] == sid]), 1)

        root_out = res.stdout.decode()
        self.assertTrue(root_out.startswith("norepo\n"), root_out)
        self.assertIn("clone\tdeps/notes", root_out)
        self.assertIn("clone\tdeps/gf", root_out)

        self._clear_logs()
        gf_res = self._run_bash('facts_of "$1" "deps/gf"', sid)
        self.assertEqual(gf_res.returncode, 0, output_of(gf_res))
        self.assertEqual(self._docker_calls(), [])
        self.assertIn(f"origin\t{gitfile_url}", gf_res.stdout.decode())

        notes_res = self._run_bash('facts_of "$1" "deps/notes"', sid)
        self.assertEqual(notes_res.returncode, 0, output_of(notes_res))
        self.assertEqual(self._docker_calls(), [])
        self.assertIn(f"origin\t{notes_url}", notes_res.stdout.decode())

        bad_res = self._run_bash('facts_of "$1" "deps/bad"', sid)
        self.assertEqual(bad_res.returncode, 0, output_of(bad_res))
        self.assertTrue(bad_res.stdout.decode().startswith("error\t"), bad_res.stdout.decode())

    def test_facts_of_skips_fetch_only_for_clean_unmodified_pinned_ro_repo(self):
        specs_url, specs_info = self.make_bare_repo(
            "specs", commits=("s1", "s2"), github_url="https://github.com/acme/specs.git"
        )
        unpinned_url, _ = self.make_bare_repo(
            "unpinned", commits=("u1",), github_url="https://github.com/acme/unpinned.git"
        )
        notes_url, _ = self.make_bare_repo(
            "notes", commits=("n1",), github_url="https://github.com/acme/notes.git"
        )
        pin_sha = specs_info["shas"][0]
        other_sha = specs_info["shas"][1]
        catalog = {
            "version": 1,
            "repos": [
                {
                    "alias": "specs",
                    "path": "deps/specs",
                    "url": specs_url,
                    "mode": "ro",
                    "attach": "default",
                    "pin_file": ".github/specs-pin",
                },
                {
                    "alias": "unpinned",
                    "path": "deps/unpinned",
                    "url": unpinned_url,
                    "mode": "ro",
                    "attach": "default",
                },
                {
                    "alias": "notes",
                    "path": "deps/notes",
                    "url": notes_url,
                    "mode": "rw",
                    "attach": "default",
                },
            ],
        }
        bare_origin = self.build_project(
            repos_catalog=catalog,
            pin_files={".github/specs-pin": pin_sha + "\n"},
            origin_url="https://github.com/acme/primary.git",
        )
        self.harness.make_bare_slot("slot-fetch", local_identity=("demo-agent", "demo-agent@example.com"))
        sid = self.harness.slot_project_of("slot-fetch")
        slot_dir = self.harness.slot_dir("slot-fetch")
        shutil.rmtree(os.path.join(slot_dir, "docs"), ignore_errors=True)
        self.real_git(slot_dir, "remote", "set-url", "origin", "https://github.com/acme/primary.git")
        self.map_url("https://github.com/acme/primary.git", bare_origin)
        self.real_git(slot_dir, "fetch", "-q", "origin")
        self.real_git(slot_dir, "checkout", "-f", "-q", "-B", "main", "origin/main")

        specs_dir = self._plant_subrepo_clone(slot_dir, "deps/specs", specs_url, pin=pin_sha)
        unpinned_dir = self._plant_subrepo_clone(slot_dir, "deps/unpinned", unpinned_url, branch="main")
        notes_dir = self._plant_subrepo_clone(slot_dir, "deps/notes", notes_url, branch="main")
        self.harness.mark_running("slot-fetch")

        # 1. Clean unmodified pinned ro repo skips git fetch; unpinned ro & rw fetch
        self._clear_logs()
        res = self._run_bash('HOST_GIT_READ_ONLY=0 FACTS_NO_FETCH=0 facts_of "$1"', sid)
        self.assertEqual(res.returncode, 0, output_of(res))
        fetch_dirs = self._git_fetch_dirs()
        self.assertIn(os.path.realpath(slot_dir), fetch_dirs)
        self.assertIn(os.path.realpath(unpinned_dir), fetch_dirs)
        self.assertIn(os.path.realpath(notes_dir), fetch_dirs)
        self.assertNotIn(os.path.realpath(specs_dir), fetch_dirs, "clean pinned ro repo should skip git fetch")
        with open(os.path.join(self.scratch, "facts", f"{sid}__deps_specs"), encoding="utf-8") as handle:
            self.assertIn("fetch\tskipped", handle.read())
        with open(os.path.join(self.scratch, "facts", f"{sid}__deps_notes"), encoding="utf-8") as handle:
            self.assertIn("fetch\tok", handle.read())

        # 2. Boundary cases where pinned ro repo MUST NOT skip fetch
        # (a) Local branch present
        self.real_git(specs_dir, "branch", "local-br", pin_sha)
        self._run_bash('facts_reset "$1"', sid)
        self._clear_logs()
        self._run_bash('HOST_GIT_READ_ONLY=0 FACTS_NO_FETCH=0 facts_of "$1"', sid)
        self.assertIn(os.path.realpath(specs_dir), self._git_fetch_dirs())
        with open(os.path.join(self.scratch, "facts", f"{sid}__deps_specs"), encoding="utf-8") as handle:
            self.assertIn("fetch\tok", handle.read())
        self.real_git(specs_dir, "branch", "-D", "local-br")

        # (b) Uncommitted change
        dirty_f = os.path.join(specs_dir, "dirty.txt")
        with open(dirty_f, "w", encoding="utf-8") as handle:
            handle.write("dirty\n")
        self._run_bash('facts_reset "$1"', sid)
        self._clear_logs()
        self._run_bash('HOST_GIT_READ_ONLY=0 FACTS_NO_FETCH=0 facts_of "$1"', sid)
        self.assertIn(os.path.realpath(specs_dir), self._git_fetch_dirs())
        with open(os.path.join(self.scratch, "facts", f"{sid}__deps_specs"), encoding="utf-8") as handle:
            self.assertIn("fetch\tok", handle.read())
        os.remove(dirty_f)

        # (c) Stash present
        with open(os.path.join(specs_dir, "specs-0.txt"), "a", encoding="utf-8") as handle:
            handle.write("stashed\n")
        self.real_git(specs_dir, "stash", "push", "-q", "-m", "s")
        self._run_bash('facts_reset "$1"', sid)
        self._clear_logs()
        self._run_bash('HOST_GIT_READ_ONLY=0 FACTS_NO_FETCH=0 facts_of "$1"', sid)
        self.assertIn(os.path.realpath(specs_dir), self._git_fetch_dirs())
        with open(os.path.join(self.scratch, "facts", f"{sid}__deps_specs"), encoding="utf-8") as handle:
            self.assertIn("fetch\tok", handle.read())
        self.real_git(specs_dir, "stash", "drop", "-q")

        # (d) HEAD != pin
        self.real_git(specs_dir, "checkout", "-q", "--detach", other_sha)
        self._run_bash('facts_reset "$1"', sid)
        self._clear_logs()
        self._run_bash('HOST_GIT_READ_ONLY=0 FACTS_NO_FETCH=0 facts_of "$1"', sid)
        self.assertIn(os.path.realpath(specs_dir), self._git_fetch_dirs())
        with open(os.path.join(self.scratch, "facts", f"{sid}__deps_specs"), encoding="utf-8") as handle:
            self.assertIn("fetch\tok", handle.read())


class TestMultiRepoDirtyDeliveryPureCore(unittest.TestCase):
    """Part 1B: Pure Core table tests for multi-repo cleanliness, verdict, column, and report."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mr-pure-core-")
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
            os.path.join(self.state, "repos"),
            os.path.join(self.scratch, "facts"),
            os.path.join(self.scratch, "spec"),
            os.path.join(self.repo, ".devcontainer"),
        ):
            os.makedirs(path, exist_ok=True)

        docker_stub = os.path.join(self.stub_bin, "docker")
        with open(docker_stub, "w", encoding="utf-8") as handle:
            handle.write(f'#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "{self.docker_log}"\nexit 99\n')
        os.chmod(docker_stub, 0o755)

        gh_stub = os.path.join(self.stub_bin, "gh")
        with open(gh_stub, "w", encoding="utf-8") as handle:
            handle.write("#!/usr/bin/env bash\necho '[]'\n")
        os.chmod(gh_stub, 0o755)

        self._git_init_project(
            catalog={
                "version": 1,
                "repos": [
                    {
                        "alias": "specs",
                        "path": "deps/specs",
                        "url": "https://github.com/acme/specs.git",
                        "mode": "ro",
                        "attach": "default",
                        "pin_file": ".github/specs-pin",
                    },
                    {
                        "alias": "notes",
                        "path": "deps/notes",
                        "url": "https://github.com/acme/notes.git",
                        "mode": "rw",
                        "attach": "default",
                    },
                    {
                        "alias": "tools",
                        "path": "extras/tools",
                        "url": "https://github.com/acme/tools.git",
                        "mode": "rw",
                        "attach": "optional",
                    },
                ],
            }
        )

    def _git_init_project(self, *, catalog=None, docs_url=None):
        lines = [
            f'PROJECT="{PROJECT_PREFIX}"\n',
            'DEFAULT_BRANCH="main"\n',
            'SLOT_PORTS="SSH_PORT NOVNC_PORT VITE_DEV_PORT"\n',
            'COMPOSE_FILES="docker-compose.yml"\n',
        ]
        if catalog is not None:
            lines.append('SLOT_REPOS_FILE=".devcontainer/repos.json"\n')
            with open(os.path.join(self.repo, ".devcontainer", "repos.json"), "w", encoding="utf-8") as handle:
                json.dump(catalog, handle)
        if docs_url is not None:
            lines.append(f'DOCS_REPO_URL="{docs_url}"\n')
        with open(os.path.join(self.repo, "project.env"), "w", encoding="utf-8") as handle:
            handle.writelines(lines)
        with open(os.path.join(self.repo, ".devcontainer", "docker-compose.yml"), "w", encoding="utf-8") as handle:
            handle.write("services: {}\n")
        git = shutil.which("git")
        self.assertTrue(git)
        if not os.path.exists(os.path.join(self.repo, ".git")):
            for args in (
                ("init", "-q", "-b", "main"),
                ("config", "user.name", "demo-agent"),
                ("config", "user.email", "demo-agent@example.com"),
                ("add", "-A"),
                ("-c", "commit.gpgsign=false", "commit", "-q", "-m", "base"),
                ("remote", "add", "origin", "https://github.com/acme/primary.git"),
            ):
                subprocess.run([git, "-C", self.repo, *args], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    @staticmethod
    def _facts(
        *,
        status_lines=(),
        stash_lines=(),
        head=("main", "1111111111111111111111111111111111111111"),
        headreach=1,
        default="main",
        branches=(),
        clones=(),
        origin="https://github.com/acme/primary.git",
        marker=None,
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
        if marker is not None:
            out.append(f"marker\t{marker}")
        for row in branches:
            out.append("branch\t" + "\t".join(str(x) for x in row))
        return "\n".join(out) + "\n"

    def _plant_slot(self, sid, root_facts, sub_facts, *, state_repos=None):
        with open(os.path.join(self.scratch, "spec", sid), "w", encoding="utf-8") as handle:
            handle.write(f"vol:{sid}-workspace\n")
        with open(os.path.join(self.state, "ports", f"{sid}.ports"), "w", encoding="utf-8") as handle:
            handle.write("SSH_PORT=42016\nNOVNC_PORT=42017\nVITE_DEV_PORT=42018\n")
        with open(os.path.join(self.state, "env", f"{sid}.env"), "w", encoding="utf-8") as handle:
            handle.write("GIT_USER_NAME=demo-agent\nGIT_USER_EMAIL=demo-agent@example.com\n")
        with open(os.path.join(self.scratch, "facts", sid), "w", encoding="utf-8") as handle:
            handle.write(root_facts)
        for sub_path, content in sub_facts.items():
            sub_key = sub_path.replace("/", "_")
            with open(os.path.join(self.scratch, "facts", f"{sid}__{sub_key}"), "w", encoding="utf-8") as handle:
                handle.write(content)
        state_path = os.path.join(self.state, "repos", f"{sid}.json")
        if state_repos is not None:
            with open(state_path, "w", encoding="utf-8") as handle:
                json.dump(state_repos, handle)
        elif os.path.exists(state_path):
            os.remove(state_path)

    def _eval_checks(self, sid, *, fast=0, docs_url=None):
        env = {
            "PATH": self.stub_bin + os.pathsep + os.environ.get("PATH", ""),
            "HOME": self.home,
            "DEV_CONTAINER_HOME": self.state,
            "SCRATCH": self.scratch,
            "DEV_CONTAINER_PROJECTS_ROOT": self.tmp,
            "DEV_CONTAINER_CALLER_PWD": self.repo,
            "DEV_CONTAINER_LIB": "1",
            "DEV_CONTAINER_NOW": "1700000000",
        }
        if docs_url is not None:
            env["DOCS_REPO_URL"] = docs_url
        script = (
            '. "$1"\n'
            f'rm -f "{self.docker_log}"\n'
            'reuse_rc=0; slot_is_clean_for_reuse "$2" || reuse_rc=$?\n'
            'tear_rc=0; slot_is_clean_for_teardown "$2" || tear_rc=$?\n'
            'verd_rc=0; slot_acquire_verdict "" "$2" "$3" || verd_rc=$?\n'
            'col=$(slot_dirty_column "$2")\n'
            'printf "REUSE=%d\\nTEARDOWN=%d\\nVERDICT=%d:%s\\nCOL=%s\\n" "$reuse_rc" "$tear_rc" "$verd_rc" "${ACQUIRE_REASON:-}" "$col"\n'
            'echo "---REPORT---"\n'
            'slot_dirty_report "$2"\n'
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
        stdout = res.stdout.decode()
        head, _, report = stdout.partition("\n---REPORT---\n")
        vals = dict(line.split("=", 1) for line in head.splitlines() if "=" in line)
        return {
            "reuse_rc": int(vals["REUSE"]),
            "tear_rc": int(vals["TEARDOWN"]),
            "verdict_rc": int(vals["VERDICT"].split(":", 1)[0]),
            "verdict_reason": vals["VERDICT"].split(":", 1)[1],
            "column": vals["COL"],
            "report": report,
        }

    def test_dirty_column_and_report_prefix_alias_for_catalog_and_state_repos(self):
        sha = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        tip2 = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        clean_root = lambda clones: self._facts(
            clones=clones, branches=[("main", sha, 1, 0, 1, "clean", 0, 0, 0)]
        )
        clean_ro = self._facts(
            head=("", sha),
            origin="https://github.com/acme/specs.git",
            marker=sha,
        )
        clean_notes = self._facts(
            origin="https://github.com/acme/notes.git",
            branches=[("main", sha, 1, 0, 1, "clean", 0, 0, 0)],
        )
        clean_extra = self._facts(
            origin="https://github.com/acme/extra.git",
            branches=[("main", sha, 1, 0, 1, "clean", 0, 0, 0)],
        )

        cases = [
            (
                "all_clean_with_absent_optional",
                clean_root(("deps/specs", "deps/notes")),
                {"deps/specs": clean_ro, "deps/notes": clean_notes},
                None,
                0,
                0,
                "acquirable",
                "clean",
                [],
            ),
            (
                "notes_uncommitted_full",
                clean_root(("deps/specs", "deps/notes")),
                {
                    "deps/specs": clean_ro,
                    "deps/notes": self._facts(
                        status_lines=(" M note.md",),
                        origin="https://github.com/acme/notes.git",
                        branches=[("main", sha, 1, 0, 1, "clean", 0, 0, 0)],
                    ),
                },
                None,
                0,
                1,
                "dirty:notes:uncommitted:1",
                "notes:uncommitted:1",
                ["notes:"],
            ),
            (
                "notes_uncommitted_fast",
                clean_root(("deps/specs", "deps/notes")),
                {
                    "deps/specs": clean_ro,
                    "deps/notes": self._facts(
                        status_lines=(" M note.md",),
                        origin="https://github.com/acme/notes.git",
                        branches=[("main", sha, 1, 0, 1, "clean", 0, 0, 0)],
                    ),
                },
                None,
                1,
                1,
                "dirty:notes",
                "notes:uncommitted:1",
                ["notes:"],
            ),
            (
                "tools_unpushed_branch",
                clean_root(("deps/specs", "deps/notes", "extras/tools")),
                {
                    "deps/specs": clean_ro,
                    "deps/notes": clean_notes,
                    "extras/tools": self._facts(
                        head=("feat-tool", tip2),
                        headreach=0,
                        origin="https://github.com/acme/tools.git",
                        branches=[("feat-tool", tip2, 0, 0, 0, "plus", 1, 0, 0)],
                    ),
                },
                None,
                0,
                1,
                "dirty:tools:1 unpushed",
                "tools:1 unpushed",
                ["tools:"],
            ),
            (
                "multiple_dirty_subrepos_and_stashed",
                clean_root(("deps/specs", "deps/notes", "extras/tools")),
                {
                    "deps/specs": clean_ro,
                    "deps/notes": self._facts(
                        stash_lines=("stash@{0}: WIP",),
                        origin="https://github.com/acme/notes.git",
                        branches=[("main", sha, 1, 0, 1, "clean", 0, 0, 0)],
                    ),
                    "extras/tools": self._facts(
                        status_lines=("?? a.txt", "?? b.txt"),
                        origin="https://github.com/acme/tools.git",
                        branches=[("main", sha, 1, 0, 1, "clean", 0, 0, 0)],
                    ),
                },
                None,
                0,
                1,
                "dirty:notes:stash:1,tools:uncommitted:2",
                "notes:stash:1,tools:uncommitted:2",
                ["notes:", "tools:"],
            ),
            (
                "state_repos_json_only_repo_clean",
                clean_root(("deps/specs", "deps/notes", "extras/state-only")),
                {
                    "deps/specs": clean_ro,
                    "deps/notes": clean_notes,
                    "extras/state-only": clean_extra,
                },
                [{"alias": "extra", "path": "extras/state-only", "slug": "acme/extra", "mode": "rw", "attach": "optional"}],
                0,
                0,
                "acquirable",
                "clean",
                [],
            ),
            (
                "state_repos_json_only_repo_dirty",
                clean_root(("deps/specs", "deps/notes", "extras/state-only")),
                {
                    "deps/specs": clean_ro,
                    "deps/notes": clean_notes,
                    "extras/state-only": self._facts(
                        status_lines=(" M x.txt",),
                        origin="https://github.com/acme/extra.git",
                        branches=[("main", sha, 1, 0, 1, "clean", 0, 0, 0)],
                    ),
                },
                [{"alias": "extra", "path": "extras/state-only", "slug": "acme/extra", "mode": "rw", "attach": "optional"}],
                0,
                1,
                "dirty:extra:uncommitted:1",
                "extra:uncommitted:1",
                ["extra:"],
            ),
            (
                "specs_ro_origin_drift",
                clean_root(("deps/specs", "deps/notes")),
                {
                    "deps/specs": self._facts(
                        head=("", sha),
                        origin="https://github.com/other/fork.git",
                        marker=sha,
                    ),
                    "deps/notes": clean_notes,
                },
                None,
                0,
                1,
                "dirty:specs:unknown origin",
                "specs:unknown origin",
                ["specs:"],
            ),
        ]

        for idx, (label, root_f, subs, st_repos, fast, exp_rc, exp_verdict_prefix, exp_col, rep_terms) in enumerate(cases, start=1):
            with self.subTest(case=label):
                sid = f"{PROJECT_PREFIX}-slot-{idx}"
                self._plant_slot(sid, root_f, subs, state_repos=st_repos)
                res = self._eval_checks(sid, fast=fast)
                self.assertEqual(res["reuse_rc"], exp_rc, f"{label}: {res}")
                self.assertEqual(res["tear_rc"], exp_rc, f"{label}: {res}")
                self.assertEqual(res["verdict_rc"], exp_rc, f"{label}: {res}")
                self.assertTrue(
                    res["verdict_reason"].startswith(exp_verdict_prefix),
                    f"{label}: expected verdict starting with {exp_verdict_prefix!r}, got {res['verdict_reason']!r}",
                )
                self.assertEqual(res["column"], exp_col, f"{label}: {res}")
                for term in rep_terms:
                    self.assertRegex(
                        res["report"],
                        rf"(?m)^\s+{re.escape(term)}",
                        f"{label}: missing {term!r} prefix in report:\n{res['report']}",
                    )

        self.assertFalse(os.path.exists(self.docker_log), "Pure Core test invoked docker")

        with self.subTest(case="legacy_docs_preserves_docs_and_docsfix"):
            self._git_init_project(catalog=None, docs_url="https://github.com/acme/demo.docs.git")
            sid = f"{PROJECT_PREFIX}-slot-9"
            self._plant_slot(
                sid,
                self._facts(
                    clones=("docs/repo", "docs/fix"),
                    branches=[("main", sha, 1, 0, 1, "clean", 0, 0, 0)],
                ),
                {
                    "docs/repo": self._facts(
                        status_lines=(" M r.md",),
                        head=("", sha),
                        origin="https://github.com/acme/demo.docs.git",
                        marker=sha,
                    ),
                    "docs/fix": self._facts(
                        head=("fix-wip", tip2),
                        headreach=0,
                        origin="https://github.com/acme/demo.docs.git",
                        branches=[("fix-wip", tip2, 0, 0, 0, "plus", 1, 0, 0)],
                    ),
                },
            )
            res = self._eval_checks(sid, fast=0, docs_url="https://github.com/acme/demo.docs.git")
            self.assertEqual(res["reuse_rc"], 1)
            self.assertIn("docs:uncommitted:1", res["column"])
            self.assertIn("docsfix:1 unpushed", res["column"])
            self.assertNotIn("docs-fix:", res["column"])
            self.assertIn("docs/repo", res["report"])
            self.assertIn("docs/fix", res["report"])
            self.assertFalse(os.path.exists(self.docker_log))


class TestMultiRepoLifecycleIntegration(DevContainerTestCase, MultiRepoSetupMixin):
    """Part 1B & 1C: release/down/list, branch_delivery_status isolation,
    prune_delivered_branches, rescue_and_reset, and reset_slot cleanup.
    """

    def setUp(self):
        super().setUp()
        self._init_wrappers(docker_wrapper=DOCKER_STUB)
        self.harness._write_stub("gh", "#!/usr/bin/env bash\necho '[]'\n")

    def _run_lib(self, script, *args, unset_docs=True, extra_env=None):
        env = self.harness._env(extra_env)
        if unset_docs:
            env.pop("DOCS_REPO_URL", None)
        env["DEV_CONTAINER_LIB"] = "1"
        env["TEST_GIT_URL_MAP"] = self.url_map_file
        env["TEST_GIT_PWD_LOG"] = self.git_pwd_log
        return subprocess.run(
            ["bash", "-c", f'. "$0"\n{script}', self.harness.script, *args],
            cwd=self.harness.fixture_repo,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
        )

    def _acquire(self, holder="worker", *extra):
        res = self.run_engine("acquire", "--for", holder, *extra)
        self.assertEqual(res.returncode, 0, output_of(res))
        return self.harness.acquired_slot_name(res)

    def test_release_down_and_list_enforce_multi_repo_dirty_and_delivery_checks(self):
        specs_url, specs_info = self.make_bare_repo(
            "specs", commits=("s1", "s2"), github_url="https://github.com/acme/specs.git"
        )
        notes_url, _ = self.make_bare_repo(
            "notes", commits=("n1",), github_url="https://github.com/acme/notes.git"
        )
        tools_url, _ = self.make_bare_repo(
            "tools", commits=("t1",), extra_branch="v2", github_url="https://github.com/acme/tools.git"
        )
        catalog = {
            "version": 1,
            "repos": [
                {
                    "alias": "specs",
                    "path": "deps/specs",
                    "url": specs_url,
                    "mode": "ro",
                    "attach": "default",
                    "pin_file": ".github/specs-pin",
                },
                {"alias": "notes", "path": "deps/notes", "url": notes_url, "mode": "rw", "attach": "default"},
                {"alias": "tools", "path": "extras/tools", "url": tools_url, "mode": "rw", "attach": "optional", "branch": "v2"},
            ],
        }
        self.build_project(
            repos_catalog=catalog,
            pin_files={".github/specs-pin": specs_info["shas"][0] + "\n"},
            origin_url="https://github.com/acme/primary.git",
        )
        name = self._acquire("w1", "--repo", "tools")
        slot_dir = self.harness.slot_dir(name)
        notes_dir = os.path.join(slot_dir, "deps", "notes")
        tools_dir = os.path.join(slot_dir, "extras", "tools")

        # 1. Uncommitted change in rw repo notes -> release refuses with notes: prefix, list shows notes:uncommitted:1
        dirty_note = os.path.join(notes_dir, "draft.txt")
        with open(dirty_note, "w", encoding="utf-8") as handle:
            handle.write("uncommitted\n")
        rel_ref = self.run_engine("release", name, "--for", "w1")
        self.assertNotEqual(rel_ref.returncode, 0, output_of(rel_ref))
        self.assertRegex(rel_ref.stderr.decode(), r"(?m)^\s+notes:")
        self.assertTrue(self.harness.volume_exists(name))
        listed = self.run_engine("list")
        self.assertEqual(listed.returncode, 0, output_of(listed))
        self.assertIn("notes:uncommitted:1", listed.stdout.decode())
        os.remove(dirty_note)

        # 2. Unpushed commit in attached optional rw repo tools -> release and down refuse with tools: prefix
        self.real_git(tools_dir, "checkout", "-q", "-b", "tools-wip")
        with open(os.path.join(tools_dir, "tool.txt"), "w", encoding="utf-8") as handle:
            handle.write("unpushed tool work\n")
        self.real_git(tools_dir, "add", "tool.txt")
        self.real_git(tools_dir, "commit", "-q", "-m", "tool wip")

        rel_tools = self.run_engine("release", name, "--for", "w1")
        self.assertNotEqual(rel_tools.returncode, 0, output_of(rel_tools))
        self.assertRegex(rel_tools.stderr.decode(), r"(?m)^\s+tools:")
        self.assertIn("tools-wip", rel_tools.stderr.decode())

        # Also test down refuses unleased dirty slot with tools: prefix
        lease_backup = os.path.join(self.harness.tmp, "lease.bak")
        shutil.copyfile(self.harness.lease_path(name), lease_backup)
        os.remove(self.harness.lease_path(name))
        down_ref = self.run_engine("down", name)
        self.assertNotEqual(down_ref.returncode, 0, output_of(down_ref))
        self.assertRegex(down_ref.stderr.decode(), r"(?m)^\s+tools:")
        self.assertTrue(self.harness.volume_exists(name))
        shutil.move(lease_backup, self.harness.lease_path(name))

        # 3. PR-merged branch on tools (verified via gh --repo acme/tools) + squash-merged branch on notes -> release succeeds
        pr_tip = self.real_git(tools_dir, "rev-parse", "HEAD")
        self.real_git(tools_dir, "checkout", "-q", "v2")
        self.real_git(notes_dir, "checkout", "-q", "-b", "notes-sq")
        with open(os.path.join(notes_dir, "sq.txt"), "w", encoding="utf-8") as handle:
            handle.write("squashed note\n")
        self.real_git(notes_dir, "add", "sq.txt")
        self.real_git(notes_dir, "commit", "-q", "-m", "notes sq")
        self.real_git(notes_dir, "checkout", "-q", "main")
        self.real_git(notes_dir, "merge", "-q", "--squash", "notes-sq")
        self.real_git(notes_dir, "commit", "-q", "-m", "notes sq (landed)")
        self.real_git(notes_dir, "push", "-q", "origin", "main")

        self.harness._write_stub(
            "gh",
            "#!/usr/bin/env bash\n"
            'repo="" head="" prev=""\n'
            'for a in "$@"; do\n'
            '  [[ "$prev" == "--repo" ]] && repo="$a"\n'
            '  [[ "$prev" == "--head" ]] && head="$a"\n'
            '  prev="$a"\n'
            "done\n"
            'if [[ "$repo" == "acme/tools" && "$head" == "tools-wip" ]]; then\n'
            f'  echo \'[{{"headRefOid":"{pr_tip}","state":"MERGED","number":17}}]\'\n'
            "else\n"
            "  echo '[]'\n"
            "fi\n",
        )
        rel_ok = self.run_engine("release", name, "--for", "w1")
        self.assertEqual(rel_ok.returncode, 0, output_of(rel_ok))
        self.assertFalse(self.harness.volume_exists(name))

    def test_branch_delivery_status_cache_isolation_for_empty_slug_repos(self):
        # Primary and two sub-repos all have non-GitHub bare origins (empty slugs) and
        # share an identical commit SHA on branch "shared-topic" that is squash-merged in
        # repo A but NOT delivered in repo B. Cache from repo A must NOT leak to repo B.
        bare_a, info_a = self.make_bare_repo("repoa", commits=("init",))
        bare_b = os.path.join(self.harness.tmp, "remotes", "repob.git")
        subprocess.run([self.harness.real_git_bin, "clone", "-q", "--bare", bare_a, bare_b], check=True)
        catalog = {
            "version": 1,
            "repos": [
                {"alias": "repoa", "path": "sub/a", "url": bare_a, "mode": "rw", "attach": "default"},
                {"alias": "repob", "path": "sub/b", "url": bare_b, "mode": "rw", "attach": "default"},
            ],
        }
        self.build_project(repos_catalog=catalog, origin_url=None)
        self.harness.make_bare_slot("slot-iso", local_identity=("demo-agent", "demo-agent@example.com"))
        slot_dir = self.harness.slot_dir("slot-iso")
        shutil.rmtree(os.path.join(slot_dir, "docs"), ignore_errors=True)
        sub_a = os.path.join(slot_dir, "sub", "a")
        sub_b = os.path.join(slot_dir, "sub", "b")
        subprocess.run([self.harness.real_git_bin, "clone", "-q", bare_a, sub_a], check=True)
        subprocess.run([self.harness.real_git_bin, "clone", "-q", bare_b, sub_b], check=True)

        # Create identical branch name & identical commit SHA in BOTH sub_a and sub_b
        for repo in (sub_a, sub_b):
            self.real_git(repo, "checkout", "-q", "-b", "shared-topic")
            with open(os.path.join(repo, "topic.txt"), "w", encoding="utf-8") as handle:
                handle.write("identical commit payload\n")
            self.real_git(repo, "add", "topic.txt")
            env_fixed = {
                **os.environ,
                "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z",
                "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z",
            }
            subprocess.run(
                [
                    self.harness.real_git_bin, "-C", repo,
                    "-c", "commit.gpgsign=false",
                    "-c", "user.name=demo-agent", "-c", "user.email=demo-agent@example.com",
                    "commit", "-q", "-m", "shared topic",
                ],
                check=True, env=env_fixed, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            self.real_git(repo, "checkout", "-q", "main")
        self.assertEqual(
            self.real_git(sub_a, "rev-parse", "shared-topic"),
            self.real_git(sub_b, "rev-parse", "shared-topic"),
        )

        # Squash-merge shared-topic into sub_a's origin/main ONLY; sub_b's origin/main has nothing!
        self.real_git(sub_a, "merge", "-q", "--squash", "shared-topic")
        self.real_git(sub_a, "commit", "-q", "-m", "land shared-topic in a")
        self.real_git(sub_a, "push", "-q", "origin", "main")

        res = self._run_lib(
            'rc_a=0; branch_delivery_status "$1" "shared-topic" "" || rc_a=$?\n'
            'rc_b=0; branch_delivery_status "$2" "shared-topic" "" || rc_b=$?\n'
            'printf "RC_A=%d\\nRC_B=%d\\n" "$rc_a" "$rc_b"\n',
            sub_a,
            sub_b,
        )
        self.assertEqual(res.returncode, 0, output_of(res))
        out = res.stdout.decode()
        self.assertIn("RC_A=0", out, out)
        self.assertIn("RC_B=1", out, f"cache key collided across distinct empty-slug repos:\n{out}")

    def test_prune_delivered_branches_across_root_and_attached_repos(self):
        notes_url, _ = self.make_bare_repo("notes", commits=("n1",), github_url="https://github.com/acme/notes.git")
        tools_url, _ = self.make_bare_repo("tools", commits=("t1",), github_url="https://github.com/acme/tools.git")
        catalog = {
            "version": 1,
            "repos": [
                {"alias": "notes", "path": "deps/notes", "url": notes_url, "mode": "rw", "attach": "default"},
                {"alias": "tools", "path": "extras/tools", "url": tools_url, "mode": "rw", "attach": "optional"},
            ],
        }
        bare_origin = self.build_project(repos_catalog=catalog, origin_url="https://github.com/acme/primary.git")
        self.harness.make_bare_slot("slot-prune", local_identity=("demo-agent", "demo-agent@example.com"))
        sid = f"{PROJECT_PREFIX}-slot-prune"
        slot_dir = self.harness.slot_dir("slot-prune")
        shutil.rmtree(os.path.join(slot_dir, "docs"), ignore_errors=True)
        self.real_git(slot_dir, "remote", "set-url", "origin", bare_origin)
        self.real_git(slot_dir, "fetch", "-q", "origin")
        self.real_git(slot_dir, "checkout", "-f", "-q", "-B", "main", "origin/main")

        notes_dir = os.path.join(slot_dir, "deps", "notes")
        tools_dir = os.path.join(slot_dir, "extras", "tools")
        subprocess.run([self.harness.real_git_bin, "clone", "-q", self.url_map[notes_url], notes_dir], check=True)
        subprocess.run([self.harness.real_git_bin, "clone", "-q", self.url_map[tools_url], tools_dir], check=True)
        self.real_git(notes_dir, "remote", "set-url", "origin", notes_url)
        self.real_git(tools_dir, "remote", "set-url", "origin", tools_url)
        self.real_git(notes_dir, "fetch", "-q", "origin")
        self.real_git(tools_dir, "fetch", "-q", "origin")

        for repo, done_br, wip_br in (
            (slot_dir, "root-done", "root-wip"),
            (notes_dir, "notes-done", "notes-wip"),
            (tools_dir, "tools-done", "tools-wip"),
        ):
            self.real_git(repo, "checkout", "-q", "-b", done_br)
            with open(os.path.join(repo, f"{done_br}.txt"), "w", encoding="utf-8") as handle:
                handle.write("done\n")
            self.real_git(repo, "add", f"{done_br}.txt")
            self.real_git(repo, "commit", "-q", "-m", done_br)
            self.real_git(repo, "checkout", "-q", "main")
            self.real_git(repo, "merge", "-q", "--ff-only", done_br)
            self.real_git(repo, "push", "-q", "origin", "main")

            self.real_git(repo, "checkout", "-q", "-b", wip_br)
            with open(os.path.join(repo, f"{wip_br}.txt"), "w", encoding="utf-8") as handle:
                handle.write("wip\n")
            self.real_git(repo, "add", f"{wip_br}.txt")
            self.real_git(repo, "commit", "-q", "-m", wip_br)
            self.real_git(repo, "checkout", "-q", "main")

        res = self._run_lib('prune_delivered_branches "$1" "$2"', slot_dir, sid)
        self.assertEqual(res.returncode, 0, output_of(res))

        for repo, done_br, wip_br in (
            (slot_dir, "root-done", "root-wip"),
            (notes_dir, "notes-done", "notes-wip"),
            (tools_dir, "tools-done", "tools-wip"),
        ):
            branches = self.real_git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/").splitlines()
            self.assertNotIn(done_br, branches, f"{done_br} was not pruned in {repo}: {branches}")
            self.assertIn(wip_br, branches, f"undelivered {wip_br} was wrongly deleted in {repo}: {branches}")

    def test_reset_slot_cleans_unkept_optional_resets_kept_and_default_and_skips_dirty(self):
        specs_url, specs_info = self.make_bare_repo(
            "specs", commits=("s1", "s2"), github_url="https://github.com/acme/specs.git"
        )
        notes_url, notes_info = self.make_bare_repo(
            "notes", commits=("n1",), github_url="https://github.com/acme/notes.git"
        )
        keep_url, keep_info = self.make_bare_repo(
            "optkeep", commits=("k1",), extra_branch="v2", github_url="https://github.com/acme/optkeep.git"
        )
        drop_url, _ = self.make_bare_repo(
            "optdrop", commits=("d1",), github_url="https://github.com/acme/optdrop.git"
        )
        dirty_url, _ = self.make_bare_repo(
            "optdirty", commits=("x1",), github_url="https://github.com/acme/optdirty.git"
        )
        pin_sha = specs_info["shas"][0]
        off_pin_sha = specs_info["shas"][1]
        catalog = {
            "version": 1,
            "repos": [
                {
                    "alias": "specs",
                    "path": "deps/specs",
                    "url": specs_url,
                    "mode": "ro",
                    "attach": "default",
                    "pin_file": ".github/specs-pin",
                },
                {"alias": "notes", "path": "deps/notes", "url": notes_url, "mode": "rw", "attach": "default"},
                {"alias": "opt-keep", "path": "extras/opt-keep", "url": keep_url, "mode": "rw", "attach": "optional", "branch": "v2"},
                {"alias": "opt-drop", "path": "extras/opt-drop", "url": drop_url, "mode": "rw", "attach": "optional"},
                {"alias": "opt-dirty", "path": "extras/opt-dirty", "url": dirty_url, "mode": "rw", "attach": "optional"},
            ],
        }
        bare_origin = self.build_project(
            repos_catalog=catalog,
            pin_files={".github/specs-pin": pin_sha + "\n"},
            origin_url="https://github.com/acme/primary.git",
        )
        self.harness.make_bare_slot(
            "slot-rst",
            env_content=IDENTITY_ENV,
            ports_content="SSH_PORT=42016\n",
            local_identity=("stale-user", "stale@example.com"),
        )
        sid = self.harness.slot_project_of("slot-rst")
        slot_dir = self.harness.slot_dir("slot-rst")
        shutil.rmtree(os.path.join(slot_dir, "docs"), ignore_errors=True)
        self.real_git(slot_dir, "remote", "set-url", "origin", "https://github.com/acme/primary.git")
        self.real_git(slot_dir, "fetch", "-q", "origin")
        self.real_git(slot_dir, "checkout", "-f", "-q", "-B", "main", "origin/main")

        # Remove .git/info/exclude so test proves slot_repos_exclude_in_dir runs BEFORE root git clean -fd
        exclude_path = os.path.join(slot_dir, ".git", "info", "exclude")
        if os.path.exists(exclude_path):
            os.remove(exclude_path)
        root_junk = os.path.join(slot_dir, "root-untracked.txt")
        with open(root_junk, "w", encoding="utf-8") as handle:
            handle.write("should be cleaned\n")

        specs_dir = os.path.join(slot_dir, "deps", "specs")
        notes_dir = os.path.join(slot_dir, "deps", "notes")
        keep_dir = os.path.join(slot_dir, "extras", "opt-keep")
        drop_dir = os.path.join(slot_dir, "extras", "opt-drop")
        dirty_dir = os.path.join(slot_dir, "extras", "opt-dirty")

        for url, target, br in (
            (specs_url, specs_dir, None),
            (notes_url, notes_dir, "main"),
            (keep_url, keep_dir, "v2"),
            (drop_url, drop_dir, "main"),
            (dirty_url, dirty_dir, "main"),
        ):
            subprocess.run([self.harness.real_git_bin, "clone", "-q", self.url_map[url], target], check=True)
            self.real_git(target, "remote", "set-url", "origin", url)
            if br:
                self.real_git(target, "checkout", "-q", br)
        # Put specs off-pin (at s2) without ro hooks so reset_slot must re-sync to pin & re-apply ro guard
        self.real_git(specs_dir, "checkout", "-q", "--detach", off_pin_sha)

        # Advance notes and opt-keep origins so reset_slot must fetch + reset --hard origin/<branch>
        for work_dir in (notes_info["work"], keep_info["work"]):
            with open(os.path.join(work_dir, "upstream-new.txt"), "w", encoding="utf-8") as handle:
                handle.write("upstream advance\n")
            self.real_git(work_dir, "add", "upstream-new.txt")
            self.real_git(work_dir, "commit", "-q", "-m", "upstream advance")
            self.real_git(work_dir, "push", "-q", "origin", "HEAD")
        notes_head_expected = self.real_git(notes_info["work"], "rev-parse", "HEAD")
        keep_head_expected = self.real_git(keep_info["work"], "rev-parse", "HEAD")

        # Make opt-dirty dirty with uncommitted file & unpushed branch
        self.real_git(dirty_dir, "checkout", "-q", "-b", "dirty-branch")
        dirty_file = os.path.join(dirty_dir, "preserve-me.txt")
        with open(dirty_file, "w", encoding="utf-8") as handle:
            handle.write("must survive reset_slot\n")

        state_entries = [
            {"alias": "specs", "path": "deps/specs", "slug": "acme/specs", "mode": "ro", "attach": "default"},
            {"alias": "notes", "path": "deps/notes", "slug": "acme/notes", "mode": "rw", "attach": "default"},
            {"alias": "opt-keep", "path": "extras/opt-keep", "slug": "acme/optkeep", "mode": "rw", "attach": "optional"},
            {"alias": "opt-drop", "path": "extras/opt-drop", "slug": "acme/optdrop", "mode": "rw", "attach": "optional"},
            {"alias": "opt-dirty", "path": "extras/opt-dirty", "slug": "acme/optdirty", "mode": "rw", "attach": "optional"},
        ]
        os.makedirs(os.path.join(self.harness.state_dir, "repos"), exist_ok=True)
        repos_state_file = os.path.join(self.harness.state_dir, "repos", f"{sid}.json")
        with open(repos_state_file, "w", encoding="utf-8") as handle:
            json.dump(state_entries, handle)

        os.makedirs(os.path.dirname(self.harness.lease_path("slot-rst")), exist_ok=True)
        with open(self.harness.lease_path("slot-rst"), "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "holder": "rst",
                    "project": sid,
                    "since": 1700000000,
                    "key": "/dev/null",
                    "ssh_port": 42016,
                    "repos": state_entries,
                },
                handle,
            )

        res = self._run_lib('reset_slot "$1" "opt-keep"', slot_dir)
        self.assertEqual(res.returncode, 0, output_of(res))
        stderr = res.stderr.decode()

        # Root cleaned & excluded sub-repos before git clean
        self.assertFalse(os.path.exists(root_junk))
        self.assertEqual(self.real_git(slot_dir, "status", "--porcelain"), "")
        self.assertEqual(self.real_git(slot_dir, "config", "--local", "user.name"), "demo-agent")

        # specs (default ro) synced to pin and ro guard applied
        self.assertEqual(self.real_git(specs_dir, "rev-parse", "HEAD"), pin_sha)
        self.assertEqual(
            self.real_git(specs_dir, "config", "--local", "remote.origin.pushurl"),
            "read-only://slot-repo-is-read-only--specs",
        )
        self.assertEqual(self.real_git(specs_dir, "config", "--local", "core.hooksPath"), ".git/slot-readonly-hooks")

        # notes (default rw) and opt-keep (kept optional rw) reset to origin/<branch> & identity re-applied
        self.assertEqual(self.real_git(notes_dir, "rev-parse", "HEAD"), notes_head_expected)
        self.assertEqual(self.real_git(keep_dir, "symbolic-ref", "--short", "HEAD"), "v2")
        self.assertEqual(self.real_git(keep_dir, "rev-parse", "HEAD"), keep_head_expected)
        self.assertEqual(self.real_git(keep_dir, "config", "--local", "user.name"), "demo-agent")

        # opt-drop (clean unkept optional) removed from disk, state, and lease
        self.assertFalse(os.path.exists(drop_dir), "clean unkept optional repo was not removed")

        # opt-dirty (dirty optional) skipped, preserved on disk and in state, stderr warned
        self.assertTrue(os.path.isfile(dirty_file), "dirty sub-repo was wiped by reset_slot")
        with open(dirty_file, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "must survive reset_slot\n")
        self.assertRegex(stderr, r"opt-dirty|extras/opt-dirty")

        with open(repos_state_file, encoding="utf-8") as handle:
            after_aliases = [e["alias"] for e in json.load(handle)]
        self.assertEqual(set(after_aliases), {"specs", "notes", "opt-keep", "opt-dirty"})
        with open(self.harness.lease_path("slot-rst"), encoding="utf-8") as handle:
            lease_aliases = [e["alias"] for e in json.load(handle)["repos"]]
        self.assertEqual(set(lease_aliases), {"specs", "notes", "opt-keep", "opt-dirty"})

    def test_reset_slot_skips_dirty_ro_drift_and_continues_other_resets(self):
        specs_url, specs_info = self.make_bare_repo(
            "specs", commits=("s1", "s2"), github_url="https://github.com/acme/specs.git"
        )
        notes_url, notes_info = self.make_bare_repo(
            "notes", commits=("n1",), github_url="https://github.com/acme/notes.git"
        )
        catalog = {
            "version": 1,
            "repos": [
                {"alias": "specs", "path": "deps/specs", "url": specs_url, "mode": "ro", "attach": "default"},
                {"alias": "notes", "path": "deps/notes", "url": notes_url, "mode": "rw", "attach": "default"},
            ],
        }
        self.build_project(repos_catalog=catalog, origin_url="https://github.com/acme/primary.git")
        self.harness.make_bare_slot(
            "slot-rodrift",
            env_content=IDENTITY_ENV,
            ports_content="SSH_PORT=42016\n",
            local_identity=("demo-agent", "demo-agent@example.com"),
        )
        slot_dir = self.harness.slot_dir("slot-rodrift")
        shutil.rmtree(os.path.join(slot_dir, "docs"), ignore_errors=True)
        self.real_git(slot_dir, "remote", "set-url", "origin", "https://github.com/acme/primary.git")
        self.real_git(slot_dir, "fetch", "-q", "origin")
        self.real_git(slot_dir, "checkout", "-f", "-q", "-B", "main", "origin/main")

        specs_dir = os.path.join(slot_dir, "deps", "specs")
        notes_dir = os.path.join(slot_dir, "deps", "notes")
        subprocess.run([self.harness.real_git_bin, "clone", "-q", self.url_map[specs_url], specs_dir], check=True)
        subprocess.run([self.harness.real_git_bin, "clone", "-q", self.url_map[notes_url], notes_dir], check=True)
        self.real_git(specs_dir, "remote", "set-url", "origin", "https://github.com/other/drifted.git")
        self.real_git(notes_dir, "remote", "set-url", "origin", notes_url)

        with open(os.path.join(notes_info["work"], "n2.txt"), "w", encoding="utf-8") as handle:
            handle.write("n2\n")
        self.real_git(notes_info["work"], "add", "n2.txt")
        self.real_git(notes_info["work"], "commit", "-q", "-m", "n2")
        self.real_git(notes_info["work"], "push", "-q", "origin", "main")
        notes_expected = self.real_git(notes_info["work"], "rev-parse", "HEAD")

        res = self._run_lib('reset_slot "$1"', slot_dir)
        self.assertEqual(res.returncode, 0, output_of(res))
        self.assertRegex(res.stderr.decode(), r"specs|deps/specs")
        self.assertEqual(self.real_git(specs_dir, "remote", "get-url", "origin"), "https://github.com/other/drifted.git")
        self.assertEqual(self.real_git(notes_dir, "rev-parse", "HEAD"), notes_expected)

    def test_rescue_and_reset_warns_for_all_dirty_subrepos_and_keeps_unpushed_root_reset(self):
        notes_url, _ = self.make_bare_repo("notes", commits=("n1",), github_url="https://github.com/acme/notes.git")
        tools_url, _ = self.make_bare_repo("tools", commits=("t1",), github_url="https://github.com/acme/tools.git")
        clean_opt_url, _ = self.make_bare_repo("cleanopt", commits=("c1",), github_url="https://github.com/acme/cleanopt.git")
        catalog = {
            "version": 1,
            "repos": [
                {"alias": "notes", "path": "deps/notes", "url": notes_url, "mode": "rw", "attach": "default"},
                {"alias": "tools", "path": "extras/tools", "url": tools_url, "mode": "rw", "attach": "optional"},
                {"alias": "clean-opt", "path": "extras/clean-opt", "url": clean_opt_url, "mode": "rw", "attach": "optional"},
            ],
        }
        bare_origin = self.build_project(repos_catalog=catalog, origin_url="https://github.com/acme/primary.git")
        # Install a pre-receive hook on bare_origin rejecting rescue pushes so we exercise already-bundled root reset
        hook = os.path.join(bare_origin, "hooks", "pre-receive")
        with open(hook, "w", encoding="utf-8") as handle:
            handle.write("#!/bin/sh\necho 'rejected for rescue test' >&2\nexit 1\n")
        os.chmod(hook, 0o755)

        self.harness.make_bare_slot(
            "slot-evict",
            env_content=IDENTITY_ENV,
            ports_content="SSH_PORT=42016\n",
            local_identity=("demo-agent", "demo-agent@example.com"),
        )
        sid = f"{PROJECT_PREFIX}-slot-evict"
        slot_dir = self.harness.slot_dir("slot-evict")
        shutil.rmtree(os.path.join(slot_dir, "docs"), ignore_errors=True)
        self.real_git(slot_dir, "remote", "set-url", "origin", "https://github.com/acme/primary.git")
        self.real_git(slot_dir, "fetch", "-q", "origin")
        self.real_git(slot_dir, "checkout", "-f", "-q", "-B", "main", "origin/main")
        origin_head = self.real_git(slot_dir, "rev-parse", "origin/main")

        # Unpushed branch + uncommitted file in root
        self.real_git(slot_dir, "checkout", "-q", "-b", "root-unpushed")
        with open(os.path.join(slot_dir, "root-wip.txt"), "w", encoding="utf-8") as handle:
            handle.write("root wip\n")

        notes_dir = os.path.join(slot_dir, "deps", "notes")
        tools_dir = os.path.join(slot_dir, "extras", "tools")
        clean_opt_dir = os.path.join(slot_dir, "extras", "clean-opt")
        for url, target in ((notes_url, notes_dir), (tools_url, tools_dir), (clean_opt_url, clean_opt_dir)):
            subprocess.run([self.harness.real_git_bin, "clone", "-q", self.url_map[url], target], check=True)
            self.real_git(target, "remote", "set-url", "origin", url)

        # Dirty notes + dirty tools
        with open(os.path.join(notes_dir, "dirty-notes.txt"), "w", encoding="utf-8") as handle:
            handle.write("dirty notes\n")
        self.real_git(tools_dir, "checkout", "-q", "-b", "tools-wip")
        with open(os.path.join(tools_dir, "dirty-tools.txt"), "w", encoding="utf-8") as handle:
            handle.write("dirty tools\n")
        self.real_git(tools_dir, "add", "dirty-tools.txt")
        self.real_git(tools_dir, "commit", "-q", "-m", "tools wip")

        res = self._run_lib('rescue_and_reset "$1" "$2"', slot_dir, sid)
        self.assertEqual(res.returncode, 0, output_of(res))
        stderr = res.stderr.decode()

        # Root was bundled & reset to origin/main without aborting
        self.assertEqual(self.real_git(slot_dir, "symbolic-ref", "--short", "HEAD"), "main")
        self.assertEqual(self.real_git(slot_dir, "rev-parse", "HEAD"), origin_head)
        rescue_dir = os.path.join(self.harness.state_dir, "rescue")
        self.assertTrue(
            any(f.endswith(".bundle") for f in os.listdir(rescue_dir)),
            f"expected rescue bundle in {rescue_dir}",
        )

        # Warnings emitted for BOTH dirty sub-repos; both kept intact; clean optional dropped
        self.assertRegex(stderr, r"notes|deps/notes")
        self.assertRegex(stderr, r"tools|extras/tools")
        self.assertTrue(os.path.isfile(os.path.join(notes_dir, "dirty-notes.txt")))
        self.assertTrue(os.path.isfile(os.path.join(tools_dir, "dirty-tools.txt")))
        self.assertFalse(os.path.exists(clean_opt_dir))


class TestMultiRepoVolumeRescueArchive(DevContainerTestCase, MultiRepoSetupMixin):
    """Part 2: multi-repo rescue archiving for volume slots (via slot_exec_stream, no DOCKER_STUB_STATE)
    and host-directory parity.
    """

    def setUp(self):
        super().setUp()
        self._init_wrappers(docker_wrapper=DOCKER_VOL_WRAPPER)
        self.harness._write_stub("gh", "#!/usr/bin/env bash\necho 'network disabled' >&2\nexit 1\n")
        self.docker_vol_log = os.path.join(self.harness.tmp, "docker-vol-calls.log")
        self.rescue_dir = os.path.join(self.harness.state_dir, "rescue")

    def run_dc(self, *args, unset_docs=True, extra_env=None, port_overrides=None, now=None):
        overrides = {
            "TEST_DOCKER_STORE": self.harness.docker_state,
            "TEST_DOCKER_BASE": os.path.join(self.harness.stub_bin, "docker-base"),
            "DOCKER_VOL_CALL_LOG": self.docker_vol_log,
            "TEST_GIT_URL_MAP": self.url_map_file,
        }
        if extra_env:
            overrides.update(extra_env)
        if port_overrides:
            overrides.update(port_overrides)
        real_env = self.harness._env
        with mock.patch.object(
            self.harness,
            "_env",
            side_effect=lambda p_over=None: self._no_stub_env(
                real_env(p_over), overrides, unset_docs=unset_docs
            ),
        ):
            return self.harness.run(*args, now=now)

    @staticmethod
    def _no_stub_env(env, overrides, *, unset_docs):
        env.pop("DOCKER_STUB_STATE", None)
        if unset_docs:
            env.pop("DOCS_REPO_URL", None)
        env.update(overrides)
        return env

    def _acquire_vol(self, holder="arch-h", *extra, ephemeral=False, unset_docs=True, now=None):
        args = ["acquire"]
        if ephemeral:
            args.append("--ephemeral")
        args += ["--for", holder, *extra]
        res = self.run_dc(*args, unset_docs=unset_docs, now=now)
        self.assertEqual(res.returncode, 0, output_of(res))
        name = self.harness.acquired_slot_name(res)
        self.assertTrue(self.harness.volume_exists(name))
        self.assertFalse(os.path.exists(os.path.join(self.harness.slots_dir, name)))
        return name

    def _standard_project(self):
        specs_url, specs_info = self.make_bare_repo(
            "specs", commits=("s1", "s2", "s3"), github_url="https://github.com/acme/specs.git"
        )
        notes_url, notes_info = self.make_bare_repo(
            "notes", commits=("n1",), github_url="https://github.com/acme/notes.git"
        )
        tools_url, tools_info = self.make_bare_repo(
            "tools", commits=("t1",), extra_branch="v2", github_url="https://github.com/acme/tools.git"
        )
        catalog = {
            "version": 1,
            "repos": [
                {
                    "alias": "specs",
                    "path": "deps/specs",
                    "url": specs_url,
                    "mode": "ro",
                    "attach": "default",
                    "pin_file": ".github/specs-pin",
                    "depth": 1,
                },
                {"alias": "notes", "path": "deps/notes", "url": notes_url, "mode": "rw", "attach": "default"},
                {"alias": "tools", "path": "extras/tools", "url": tools_url, "mode": "rw", "attach": "optional", "branch": "v2"},
            ],
        }
        self.build_project(
            repos_catalog=catalog,
            pin_files={".github/specs-pin": specs_info["shas"][0] + "\n"},
            origin_url="https://github.com/acme/primary.git",
        )
        return catalog, {"specs": specs_info, "notes": notes_info, "tools": tools_info}, {
            "specs": specs_url,
            "notes": notes_url,
            "tools": tools_url,
        }

    def test_archive_clean_root_with_dirty_subrepo_and_clean_pinned_shallow_ro(self):
        _, info, urls = self._standard_project()
        name = self._acquire_vol("arch-1", "--repo", "tools")
        vdir = self.harness.slot_dir(name)
        tools_dir = os.path.join(vdir, "extras", "tools")

        # Root, pinned shallow ro specs, and notes remain clean.
        # Make tools dirty with unpushed branch, tracked diff, untracked, .local, ignored, node_modules.
        with open(os.path.join(tools_dir, ".gitignore"), "w", encoding="utf-8") as handle:
            handle.write("ignored.log\n.local/\nnode_modules/\n")
        self.real_git(tools_dir, "add", ".gitignore")
        self.real_git(tools_dir, "commit", "-q", "-m", "add gitignore")
        self.real_git(tools_dir, "push", "-q", "origin", "v2")

        self.real_git(tools_dir, "checkout", "-q", "-b", "tools-rescue")
        with open(os.path.join(tools_dir, "tool-commit.txt"), "w", encoding="utf-8") as handle:
            handle.write("unpushed tool commit\n")
        self.real_git(tools_dir, "add", "tool-commit.txt")
        self.real_git(tools_dir, "commit", "-q", "-m", "tool rescue commit")

        with open(os.path.join(tools_dir, "tools-0.txt"), "w", encoding="utf-8") as handle:
            handle.write("modified tool tracked\n")
        with open(os.path.join(tools_dir, "untracked-tool.txt"), "w", encoding="utf-8") as handle:
            handle.write("untracked tool file\n")
        os.makedirs(os.path.join(tools_dir, ".local"), exist_ok=True)
        with open(os.path.join(tools_dir, ".local", "state.json"), "w", encoding="utf-8") as handle:
            handle.write('{"tool":true}\n')
        with open(os.path.join(tools_dir, "ignored.log"), "w", encoding="utf-8") as handle:
            handle.write("ignored tool log\n")
        nm_dir = os.path.join(tools_dir, "node_modules", "pkg")
        os.makedirs(nm_dir, exist_ok=True)
        with open(os.path.join(nm_dir, "index.js"), "w", encoding="utf-8") as handle:
            handle.write("module.exports = 1;\n")

        # Run evict while container is stopped to exercise helper container slot_exec_stream
        self.harness.mark_stopped(name)
        evict = self.run_dc("evict", name, "--yes", "--force")
        self.assertEqual(evict.returncode, 0, output_of(evict))
        self.assertFalse(self.harness.volume_exists(name))

        arch_dir = os.path.join(self.rescue_dir, self.harness.project_key, name)
        self.assertTrue(os.path.isdir(arch_dir), f"clean root with dirty sub-repo must still archive: {arch_dir}")

        # Root was clean: no root bundle/patch/tar, and NEVER untracked.tar.gz
        for absent in ("commits.bundle", "changes.patch", "new-files.tar", "untracked.tar.gz"):
            self.assertFalse(os.path.exists(os.path.join(arch_dir, absent)), absent)

        # Clean sub-repos produce zero repos/<alias>/ dir
        self.assertFalse(os.path.exists(os.path.join(arch_dir, "repos", "specs")))
        self.assertFalse(os.path.exists(os.path.join(arch_dir, "repos", "notes")))

        # Dirty tools archived under repos/tools/
        tools_arch = os.path.join(arch_dir, "repos", "tools")
        for fname in ("commits.bundle", "changes.patch", "new-files.tar"):
            self.assertTrue(os.path.isfile(os.path.join(tools_arch, fname)), f"missing {fname}")

        # Verify full recovery from repos/tools/
        recover = os.path.join(self.harness.tmp, "recover-tools")
        subprocess.run([self.harness.real_git_bin, "clone", "-q", info["tools"]["bare"], recover], check=True)
        self.real_git(recover, "fetch", os.path.join(tools_arch, "commits.bundle"), "refs/heads/*:refs/heads/*")
        self.real_git(recover, "checkout", "-q", "tools-rescue")
        with open(os.path.join(recover, "tool-commit.txt"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "unpushed tool commit\n")
        self.real_git(recover, "apply", "--whitespace=nowarn", os.path.join(tools_arch, "changes.patch"))
        with open(os.path.join(recover, "tools-0.txt"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "modified tool tracked\n")
        subprocess.run(["tar", "-xf", os.path.join(tools_arch, "new-files.tar"), "-C", recover], check=True)
        with open(os.path.join(recover, "untracked-tool.txt"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "untracked tool file\n")
        with open(os.path.join(recover, ".local", "state.json"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), '{"tool":true}\n')
        self.assertFalse(os.path.exists(os.path.join(recover, "ignored.log")))
        self.assertFalse(os.path.exists(os.path.join(recover, "node_modules")))

        # Verify meta.json exact shape
        with open(os.path.join(arch_dir, "meta.json"), encoding="utf-8") as handle:
            meta = json.load(handle)
        for key in ("project", "lease", "repo_root", "origin", "default_branch", "branches", "ignored", "omitted", "created_at", "repos"):
            self.assertIn(key, meta)
        self.assertEqual(len(meta["repos"]), 1, meta["repos"])
        entry = meta["repos"][0]
        self.assertEqual(
            set(entry.keys()),
            {"alias", "path", "url", "mode", "branches", "ignored", "omitted", "files"},
        )
        self.assertEqual(entry["alias"], "tools")
        self.assertEqual(entry["path"], "extras/tools")
        self.assertEqual(entry["url"], urls["tools"])
        self.assertEqual(entry["mode"], "rw")
        self.assertIn("tools-rescue", entry["branches"])
        self.assertIn("ignored.log", entry["ignored"])
        self.assertFalse(any("node_modules" in i for i in entry["ignored"]))
        self.assertEqual(entry["omitted"], [])
        self.assertEqual(sorted(entry["files"]), ["changes.patch", "commits.bundle", "new-files.tar"])

    def test_archive_dirty_root_with_clean_subrepos_and_all_clean_slot(self):
        self._standard_project()
        since = 1700000000
        self.harness.set_now(since)

        # 1. Dirty root + clean pinned shallow ro specs + clean rw notes -> root archived, meta.repos == []
        s_dirty_root = self._acquire_vol("dirty-root", now=since)
        with open(os.path.join(self.harness.slot_dir(s_dirty_root), "root-new.txt"), "w", encoding="utf-8") as handle:
            handle.write("root untracked\n")
        ev = self.run_dc("evict", s_dirty_root, "--yes", "--force", now=since)
        self.assertEqual(ev.returncode, 0, output_of(ev))
        arch_dir = os.path.join(self.rescue_dir, self.harness.project_key, s_dirty_root)
        self.assertTrue(os.path.isfile(os.path.join(arch_dir, "new-files.tar")))
        self.assertFalse(os.path.exists(os.path.join(arch_dir, "untracked.tar.gz")))
        self.assertFalse(os.path.exists(os.path.join(arch_dir, "repos", "specs")))
        self.assertFalse(os.path.exists(os.path.join(arch_dir, "repos", "notes")))
        with open(os.path.join(arch_dir, "meta.json"), encoding="utf-8") as handle:
            meta = json.load(handle)
        self.assertEqual(meta.get("repos"), [])

        # 2. Completely clean root + clean pinned shallow ro specs + clean rw notes -> zero archive
        shutil.rmtree(self.rescue_dir, ignore_errors=True)
        s_clean = self._acquire_vol("all-clean", now=since)
        lease_path = self.harness.lease_path(s_clean)
        with open(lease_path, encoding="utf-8") as handle:
            ldata = json.load(handle)
        ldata["since"] = since - 10000
        with open(lease_path, "w", encoding="utf-8") as handle:
            json.dump(ldata, handle)
        reap = self.run_dc("reap", now=since + 10000)
        self.assertEqual(reap.returncode, 0, output_of(reap))
        self.assertFalse(self.harness.volume_exists(s_clean))
        self.assertFalse(os.path.exists(os.path.join(self.rescue_dir, self.harness.project_key, s_clean)))

    def test_archive_subrepo_size_limit_dirty_ro_legacy_docs_discard_ephemeral_and_host_parity(self):
        _, _, urls = self._standard_project()
        since = 1700000000
        self.harness.set_now(since)

        # 1. Size limit + dirty ro repo in running volume slot
        s1 = self._acquire_vol("arch-limits", "--repo", "tools", now=since)
        v1 = self.harness.slot_dir(s1)
        tools_dir = os.path.join(v1, "extras", "tools")
        specs_dir = os.path.join(v1, "deps", "specs")

        with open(os.path.join(tools_dir, "small.txt"), "wb") as handle:
            handle.write(b"a" * 80)
        os.makedirs(os.path.join(tools_dir, ".local"), exist_ok=True)
        with open(os.path.join(tools_dir, ".local", "exact.txt"), "wb") as handle:
            handle.write(b"b" * 80)
        with open(os.path.join(tools_dir, "over.txt"), "wb") as handle:
            handle.write(b"c" * 150)

        with open(os.path.join(specs_dir, "ro-untracked.txt"), "w", encoding="utf-8") as handle:
            handle.write("edited ro\n")

        ev1 = self.run_dc(
            "evict", s1, "--yes", "--force",
            port_overrides={"DEV_CONTAINER_ARCHIVE_SIZE_LIMIT": "160"},
            now=since,
        )
        self.assertEqual(ev1.returncode, 0, output_of(ev1))
        arch1 = os.path.join(self.rescue_dir, self.harness.project_key, s1)
        tools_tar = os.path.join(arch1, "repos", "tools", "new-files.tar")
        self.assertTrue(os.path.isfile(tools_tar))
        self.assertFalse(os.path.exists(os.path.join(arch1, "repos", "tools", "commits.bundle")))
        self.assertFalse(os.path.exists(os.path.join(arch1, "repos", "tools", "changes.patch")))
        tar_list = subprocess.run(["tar", "-tf", tools_tar], check=True, stdout=subprocess.PIPE).stdout.decode().splitlines()
        self.assertIn("small.txt", tar_list)
        self.assertEqual(tar_list.count(".local/exact.txt"), 1)
        self.assertNotIn("over.txt", tar_list)

        self.assertTrue(os.path.isfile(os.path.join(arch1, "repos", "specs", "new-files.tar")))
        with open(os.path.join(arch1, "meta.json"), encoding="utf-8") as handle:
            meta1 = json.load(handle)
        by_alias = {r["alias"]: r for r in meta1["repos"]}
        self.assertEqual(set(by_alias), {"tools", "specs"})
        self.assertEqual(by_alias["tools"]["files"], ["new-files.tar"])
        self.assertEqual(by_alias["tools"]["omitted"], [{"name": "over.txt", "size": 150}])
        self.assertEqual(by_alias["specs"]["mode"], "ro")
        self.assertEqual(by_alias["specs"]["files"], ["new-files.tar"])

        # 2. --discard and --ephemeral skip archive even with dirty sub-repos
        shutil.rmtree(self.rescue_dir, ignore_errors=True)
        s_disc = self._acquire_vol("disc-sub", "--repo", "tools", now=since)
        with open(os.path.join(self.harness.slot_dir(s_disc), "extras", "tools", "wip.txt"), "w", encoding="utf-8") as handle:
            handle.write("discard me\n")
        rel_disc = self.run_dc("release", s_disc, "--for", "disc-sub", "--discard", now=since)
        self.assertEqual(rel_disc.returncode, 0, output_of(rel_disc))
        self.assertFalse(os.path.exists(os.path.join(self.rescue_dir, self.harness.project_key, s_disc)))

        s_eph = self._acquire_vol("eph-sub", "--repo", "tools", ephemeral=True, now=since)
        with open(os.path.join(self.harness.slot_dir(s_eph), "extras", "tools", "wip.txt"), "w", encoding="utf-8") as handle:
            handle.write("ephemeral discard\n")
        rel_eph = self.run_dc("release", s_eph, "--for", "eph-sub", now=since)
        self.assertEqual(rel_eph.returncode, 0, output_of(rel_eph))
        self.assertFalse(os.path.exists(os.path.join(self.rescue_dir, self.harness.project_key, s_eph)))

        # 3. Host-directory slot parity via archive_and_clean_slot
        host_name = "slot-19"
        host_sid = self.harness.slot_project_of(host_name)
        host_dir = os.path.join(self.harness.slots_dir, host_name)
        os.makedirs(os.path.join(host_dir, ".devcontainer"), exist_ok=True)
        subprocess.run(
            [self.harness.real_git_bin, "clone", "-q", self.harness.fixture_repo, host_dir + "-tmp"],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        shutil.move(os.path.join(host_dir + "-tmp", ".git"), os.path.join(host_dir, ".git"))
        shutil.rmtree(host_dir + "-tmp")
        self.real_git(host_dir, "remote", "set-url", "origin", "https://github.com/acme/primary.git")
        self.real_git(host_dir, "reset", "-q", "--hard")
        with open(os.path.join(host_dir, ".devcontainer", ".slot-holder"), "w", encoding="utf-8") as handle:
            handle.write("holder=host-par\n")
        host_tools = os.path.join(host_dir, "extras", "tools")
        subprocess.run([self.harness.real_git_bin, "clone", "-q", self.url_map[urls["tools"]], host_tools], check=True)
        self.real_git(host_tools, "remote", "set-url", "origin", urls["tools"])
        with open(os.path.join(host_tools, "host-dirty.txt"), "w", encoding="utf-8") as handle:
            handle.write("host subrepo work\n")

        env = self.harness._env(None)
        env.pop("DOCKER_STUB_STATE", None)
        env.pop("DOCS_REPO_URL", None)
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
        self.assertEqual(host_res.returncode, 0, output_of(host_res))
        self.assertFalse(os.path.exists(host_dir))
        host_arch = os.path.join(self.rescue_dir, self.harness.project_key, host_name)
        self.assertTrue(os.path.isfile(os.path.join(host_arch, "repos", "tools", "new-files.tar")))
        with open(os.path.join(host_arch, "meta.json"), encoding="utf-8") as handle:
            host_meta = json.load(handle)
        self.assertEqual([r["alias"] for r in host_meta["repos"]], ["tools"])

        # 4. Legacy DOCS_REPO_URL slot with clean docs/repo and dirty docs/fix
        self.setUp()
        self.build_project(
            repos_catalog=None,
            project_env_extra={"DOCS_REPO_URL": self.harness.docs_repo_stub},
            origin_url="https://github.com/acme/primary.git",
        )
        s_leg = self._acquire_vol("arch-legacy", unset_docs=False, now=since)
        docs_fix = os.path.join(self.harness.slot_dir(s_leg), "docs", "fix")
        subprocess.run([self.harness.real_git_bin, "clone", "-q", self.harness.docs_repo_stub, docs_fix], check=True)
        with open(os.path.join(docs_fix, "legacy-new.txt"), "w", encoding="utf-8") as handle:
            handle.write("legacy docs fix work\n")
        ev_leg = self.run_dc("evict", s_leg, "--yes", "--force", unset_docs=False, now=since)
        self.assertEqual(ev_leg.returncode, 0, output_of(ev_leg))
        leg_arch = os.path.join(self.rescue_dir, self.harness.project_key, s_leg)
        self.assertFalse(os.path.exists(os.path.join(leg_arch, "repos", "docs")))
        self.assertTrue(os.path.isfile(os.path.join(leg_arch, "repos", "docs-fix", "new-files.tar")))
        with open(os.path.join(leg_arch, "meta.json"), encoding="utf-8") as handle:
            leg_meta = json.load(handle)
        self.assertEqual([r["alias"] for r in leg_meta["repos"]], ["docs-fix"])


class TestMultiRepoSlotFsGuardsAndDiffSubdir(DevContainerTestCase, MultiRepoSetupMixin):
    """Part 3: slot-fs.pl ro repo write/edit guard, SLOT_REPOS_FILE guard,
    nested rw project.env/.devcontainer editing, and fs diff subdir validation & execution.
    """

    def setUp(self):
        super().setUp()
        self._init_wrappers(docker_wrapper=DOCKER_CAPTURE_WRAPPER)
        self.harness._write_stub("gh", "#!/usr/bin/env bash\necho '[]'\n")

    def _acquire(self, holder="fs-holder", *extra):
        res = self.run_engine("acquire", "--for", holder, *extra)
        self.assertEqual(res.returncode, 0, output_of(res))
        return self.harness.acquired_slot_name(res)

    def _read_token(self, name):
        path = self.harness.lease_token_path(name)
        with open(path, encoding="utf-8") as handle:
            return handle.read().strip()

    def _run_fs(self, op, slot, payload, *, unset_docs=True):
        raw = json.dumps(payload).encode("utf-8")
        return self.run_engine("fs", op, slot, "--stdin-json", unset_docs=unset_docs, input=raw)

    def _assert_fs_ok(self, res, ctx=""):
        out = res.stdout.decode(errors="replace")
        err = res.stderr.decode(errors="replace")
        msg = f"rc={res.returncode}\nstdout={out}\nstderr={err}\n{ctx}"
        self.assertEqual(res.returncode, 0, msg)
        data = json.loads(out)
        self.assertIs(data.get("ok"), True, msg)
        return data

    def _assert_fs_refused(self, res, ctx=""):
        out = res.stdout.decode(errors="replace").strip()
        err = res.stderr.decode(errors="replace").strip()
        msg = f"rc={res.returncode}\nstdout={out}\nstderr={err}\n{ctx}"
        self.assertNotIn("Usage:", out + err, msg)
        self.assertNotIn("unknown command", (out + err).lower(), msg)
        self.assertNotEqual(res.returncode, 0, msg)
        if out.startswith("{"):
            parsed = json.loads(out)
            self.assertIs(parsed.get("ok"), False, msg)
            self.assertTrue(parsed.get("error"), msg)

    def test_fs_refuses_write_edit_on_ro_repos_and_allows_prefix_siblings_and_reads(self):
        specs_url, specs_info = self.make_bare_repo("specs", commits=("s1", "s2"), github_url="https://github.com/acme/specs.git")
        sibling_url, _ = self.make_bare_repo("specs-extra", commits=("e1",), github_url="https://github.com/acme/specs-extra.git")
        pub_url, _ = self.make_bare_repo("pub", commits=("p1",), github_url="https://github.com/other/pub.git")
        catalog = {
            "version": 1,
            "repos": [
                {
                    "alias": "specs",
                    "path": "deps/specs",
                    "url": specs_url,
                    "mode": "ro",
                    "attach": "default",
                    "pin_file": ".github/specs-pin",
                },
                {
                    "alias": "specs-extra",
                    "path": "deps/specs-extra",
                    "url": sibling_url,
                    "mode": "rw",
                    "attach": "default",
                },
                {
                    "alias": "pub",
                    "path": "extras/pub",
                    "url": pub_url,
                    "mode": "ro",
                    "attach": "optional",
                    "auth": "none",
                },
            ],
        }
        self.build_project(
            repos_catalog=catalog,
            pin_files={".github/specs-pin": specs_info["shas"][0] + "\n"},
            origin_url="https://github.com/acme/primary.git",
        )
        name = self._acquire("fs-ro")
        att = self.run_engine("attach", name, "--repo", "pub", "--for", "fs-ro")
        self.assertEqual(att.returncode, 0, output_of(att))
        tok = self._read_token(name)
        slot_dir = self.harness.slot_dir(name)

        specs_file = os.path.join(slot_dir, "deps", "specs", "specs-0.txt")
        pub_file = os.path.join(slot_dir, "extras", "pub", "pub-0.txt")
        with open(specs_file, encoding="utf-8") as handle:
            specs_orig = handle.read()
        with open(pub_file, encoding="utf-8") as handle:
            pub_orig = handle.read()

        # Refused write/edit on default ro (deps/specs) and attached ro (extras/pub)
        for bad_target in ("deps/specs/specs-0.txt", "deps/specs/new.txt", "deps/specs", "extras/pub/pub-0.txt"):
            self._assert_fs_refused(
                self._run_fs("write", name, {"lease_token": tok, "path": bad_target, "content": "pwn\n"}),
                bad_target,
            )
            self._assert_fs_refused(
                self._run_fs("edit", name, {"lease_token": tok, "path": bad_target, "old_string": "s1", "new_string": "pwn"}),
                bad_target,
            )
        with open(specs_file, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), specs_orig)
        with open(pub_file, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), pub_orig)

        # Prefix boundary: rw sibling deps/specs-extra/ MUST NOT be blocked by deps/specs ro prefix
        self._assert_fs_ok(
            self._run_fs("write", name, {"lease_token": tok, "path": "deps/specs-extra/allowed.txt", "content": "ok\n"})
        )
        self._assert_fs_ok(
            self._run_fs("edit", name, {"lease_token": tok, "path": "deps/specs-extra/allowed.txt", "old_string": "ok", "new_string": "OK"})
        )

        # Read/grep/glob/diff MUST still work on ro repos
        rd = self._assert_fs_ok(self._run_fs("read", name, {"path": "deps/specs/specs-0.txt"}))
        self.assertEqual(rd["content"], specs_orig)
        gr = self._assert_fs_ok(self._run_fs("grep", name, {"pattern": specs_orig.strip(), "path": "deps/specs"}))
        self.assertTrue(any(m["path"] == "deps/specs/specs-0.txt" for m in gr["matches"]))
        gl = self._assert_fs_ok(self._run_fs("glob", name, {"pattern": "deps/specs/*.txt"}))
        self.assertIn("deps/specs/specs-0.txt", gl["paths"])

        with open(specs_file, "w", encoding="utf-8") as handle:
            handle.write("temp-ro-diff\n")
        df = self._assert_fs_ok(self._run_fs("diff", name, {"subdir": "deps/specs"}))
        self.assertIn("+temp-ro-diff", df["diff"])

    def test_fs_refuses_write_edit_on_legacy_docs_repo_and_allows_docs_fix(self):
        self.build_project(
            repos_catalog=None,
            project_env_extra={"DOCS_REPO_URL": self.harness.docs_repo_stub},
            origin_url="https://github.com/acme/primary.git",
        )
        name = self._acquire("fs-legacy")
        tok = self._read_token(name)
        slot_dir = self.harness.slot_dir(name)

        # Read allowed, write/edit refused on legacy docs/repo
        self._assert_fs_ok(self._run_fs("read", name, {"path": "docs/repo/README.md"}, unset_docs=False))
        self._assert_fs_refused(
            self._run_fs("write", name, {"lease_token": tok, "path": "docs/repo/README.md", "content": "bad\n"}, unset_docs=False)
        )
        self._assert_fs_refused(
            self._run_fs(
                "edit", name,
                {"lease_token": tok, "path": "docs/repo/README.md", "old_string": "docs", "new_string": "bad"},
                unset_docs=False,
            )
        )

        # Clone docs/fix and verify diff subdir works for both docs and docs-fix
        docs_fix = os.path.join(slot_dir, "docs", "fix")
        subprocess.run([self.harness.real_git_bin, "clone", "-q", self.harness.docs_repo_stub, docs_fix], check=True)
        self._assert_fs_ok(
            self._run_fs("write", name, {"lease_token": tok, "path": "docs/fix/README.md", "content": "fix edit\n"}, unset_docs=False)
        )
        df_fix = self._assert_fs_ok(self._run_fs("diff", name, {"subdir": "docs-fix"}, unset_docs=False))
        self.assertIn("+fix edit", df_fix["diff"])
        df_fix_path = self._assert_fs_ok(self._run_fs("diff", name, {"subdir": "docs/fix"}, unset_docs=False))
        self.assertIn("+fix edit", df_fix_path["diff"])

    def test_fs_refuses_write_edit_on_slot_repos_file_outside_and_blocks_all_inside_compose_dir(self):
        # 1. SLOT_REPOS_FILE outside .devcontainer (config/repos.json): read/grep/glob/diff allowed; write/edit refused
        catalog = {"version": 1, "repos": []}
        self.build_project(
            repos_catalog=catalog,
            repos_rel="config/repos.json",
            tracked_files={"config/repos.json.example": '{"version":1,"repos":[]}\n'},
            origin_url="https://github.com/acme/primary.git",
        )
        name1 = self._acquire("fs-cfg")
        tok1 = self._read_token(name1)
        cfg_file = os.path.join(self.harness.slot_dir(name1), "config", "repos.json")
        with open(cfg_file, encoding="utf-8") as handle:
            cfg_orig = handle.read()

        self._assert_fs_refused(
            self._run_fs("write", name1, {"lease_token": tok1, "path": "config/repos.json", "content": "{}"})
        )
        self._assert_fs_refused(
            self._run_fs("edit", name1, {"lease_token": tok1, "path": "config/repos.json", "old_string": "repos", "new_string": "x"})
        )
        with open(cfg_file, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), cfg_orig)

        self._assert_fs_ok(self._run_fs("read", name1, {"path": "config/repos.json"}))
        gr = self._assert_fs_ok(self._run_fs("grep", name1, {"pattern": "repos", "path": "config"}))
        self.assertTrue(any(m["path"] == "config/repos.json" for m in gr["matches"]))
        gl = self._assert_fs_ok(self._run_fs("glob", name1, {"pattern": "config/*"}))
        self.assertIn("config/repos.json", gl["paths"])
        self._assert_fs_ok(
            self._run_fs("write", name1, {"lease_token": tok1, "path": "config/repos.json.example", "content": "updated\n"})
        )

        # 2. SLOT_REPOS_FILE inside .devcontainer (.devcontainer/repos.json): blocked for all ops
        self.setUp()
        self.build_project(
            repos_catalog=catalog,
            repos_rel=".devcontainer/repos.json",
            origin_url="https://github.com/acme/primary.git",
        )
        name2 = self._acquire("fs-dc")
        tok2 = self._read_token(name2)
        for op, payload in (
            ("read", {"path": ".devcontainer/repos.json"}),
            ("write", {"lease_token": tok2, "path": ".devcontainer/repos.json", "content": "{}"}),
            ("edit", {"lease_token": tok2, "path": ".devcontainer/repos.json", "old_string": "repos", "new_string": "x"}),
        ):
            self._assert_fs_refused(self._run_fs(op, name2, payload), op)

    def test_fs_diff_subdir_resolves_aliases_paths_guards_and_nested_rw_rules(self):
        specs_url, specs_info = self.make_bare_repo("specs", commits=("s1",), github_url="https://github.com/acme/specs.git")
        tools_url, _ = self.make_bare_repo("tools", commits=("t1",), github_url="https://github.com/acme/tools.git")
        opt_url, _ = self.make_bare_repo("unused", commits=("u1",), github_url="https://github.com/acme/unused.git")
        catalog = {
            "version": 1,
            "repos": [
                {"alias": "specs", "path": "deps/specs", "url": specs_url, "mode": "ro", "attach": "default"},
                {"alias": "tools", "path": "extras/tools", "url": tools_url, "mode": "rw", "attach": "optional"},
                {"alias": "unused", "path": "extras/unused", "url": opt_url, "mode": "rw", "attach": "optional"},
            ],
        }
        self.build_project(
            repos_catalog=catalog,
            tracked_files={"src/root.txt": "root-before\n", "README.md": "readme-before\n"},
            origin_url="https://github.com/acme/primary.git",
        )
        name = self._acquire("fs-diff", "--repo", "tools")
        tok = self._read_token(name)
        slot_dir = self.harness.slot_dir(name)
        tools_dir = os.path.join(slot_dir, "extras", "tools")

        # Seed tracked files inside nested rw repo tools (including project.env, .devcontainer/, .env.local, .env.example, sub/f.txt, big.txt)
        for rel, body in (
            ("project.env", "PROJECT=tools\n"),
            (".devcontainer/compose.yml", "services: {}\n"),
            (".env.local", "SUB_SECRET=old\n"),
            (".env.example", "SUB_EXAMPLE=old\n"),
            ("sub/f.txt", "sub-before\n"),
            ("staged.txt", "staged-before\n"),
            ("big.txt", "x\n"),
        ):
            full = os.path.join(tools_dir, rel)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w", encoding="utf-8") as handle:
                handle.write(body)
        self.real_git(
            tools_dir, "add", "-f", "--",
            "project.env", ".devcontainer/compose.yml", ".env.local", ".env.example", "sub/f.txt", "staged.txt", "big.txt",
        )
        self.real_git(tools_dir, "commit", "-q", "-m", "seed tools files")

        # Edit nested rw repo's project.env and .devcontainer/compose.yml via slot-fs -> PERMITTED
        self._assert_fs_ok(
            self._run_fs("write", name, {"lease_token": tok, "path": "extras/tools/project.env", "content": "PROJECT=tools-v2\n"})
        )
        self._assert_fs_ok(
            self._run_fs(
                "edit", name,
                {"lease_token": tok, "path": "extras/tools/.devcontainer/compose.yml", "old_string": "{}", "new_string": "{app:{}}"},
            )
        )
        # Nested .env.local and .git/config remain blocked at every depth
        self._assert_fs_refused(self._run_fs("read", name, {"path": "extras/tools/.env.local"}))
        self._assert_fs_refused(self._run_fs("write", name, {"lease_token": tok, "path": "extras/tools/.env.local", "content": "x\n"}))
        self._assert_fs_refused(self._run_fs("read", name, {"path": "extras/tools/.git/config"}))
        # Root project.env and .devcontainer remain blocked
        self._assert_fs_refused(self._run_fs("read", name, {"path": "project.env"}))
        self._assert_fs_refused(self._run_fs("read", name, {"path": ".devcontainer/docker-compose.yml"}))

        # Modify files in root and in tools (including staged and .env.local secret and symlink inside tools)
        with open(os.path.join(slot_dir, "src", "root.txt"), "w", encoding="utf-8") as handle:
            handle.write("root-after\n")
        with open(os.path.join(tools_dir, ".env.local"), "w", encoding="utf-8") as handle:
            handle.write("SUB_SECRET=leaked\n")
        with open(os.path.join(tools_dir, ".env.example"), "w", encoding="utf-8") as handle:
            handle.write("SUB_EXAMPLE=new\n")
        with open(os.path.join(tools_dir, "sub", "f.txt"), "w", encoding="utf-8") as handle:
            handle.write("sub-after\n")
        with open(os.path.join(tools_dir, "staged.txt"), "w", encoding="utf-8") as handle:
            handle.write("staged-after\n")
        self.real_git(tools_dir, "add", "staged.txt")
        os.symlink("/etc/hosts", os.path.join(tools_dir, "hosts_link"))

        # 1. subdir omitted or null -> root diff only
        for payload in ({}, {"subdir": None}):
            res_root = self._assert_fs_ok(self._run_fs("diff", name, payload))
            self.assertIn("src/root.txt", res_root["diff"])
            self.assertNotIn("sub/f.txt", res_root["diff"])
            self.assertNotIn("SUB_SECRET", res_root["diff"])

        # 2. subdir by path and by alias -> tools diff only; .env.local excluded; project.env/.devcontainer/.env.example included
        for sub_val in ("extras/tools", "tools"):
            res_sub = self._assert_fs_ok(self._run_fs("diff", name, {"subdir": sub_val}), sub_val)
            d = res_sub["diff"]
            self.assertIn("sub/f.txt", d)
            self.assertIn("+sub-after", d)
            self.assertIn("project.env", d)
            self.assertIn(".devcontainer/compose.yml", d)
            self.assertIn(".env.example", d)
            self.assertNotIn("src/root.txt", d)
            self.assertNotIn("SUB_SECRET", d)
            self.assertNotIn(".env.local", d)

        # 3. paths relative to subdir, cached, and max_bytes
        res_rel = self._assert_fs_ok(self._run_fs("diff", name, {"subdir": "tools", "paths": ["sub/f.txt"]}))
        self.assertIn("sub/f.txt", res_rel["diff"])
        self.assertNotIn("project.env", res_rel["diff"])

        res_cached = self._assert_fs_ok(self._run_fs("diff", name, {"subdir": "tools", "cached": True}))
        self.assertIn("staged.txt", res_cached["diff"])
        self.assertIn("+staged-after", res_cached["diff"])
        self.assertNotIn("sub/f.txt", res_cached["diff"])

        with open(os.path.join(tools_dir, "big.txt"), "w", encoding="utf-8") as handle:
            handle.write("\u00e9" * 400 + "\n")
        res_cap = self._assert_fs_ok(self._run_fs("diff", name, {"subdir": "tools", "paths": ["big.txt"], "max_bytes": 32}))
        self.assertIs(res_cap["truncated"], True)
        self.assertLessEqual(len(res_cap["diff"].encode("utf-8")), 32)
        self.assertNotIn("\ufffd", res_cap["diff"])

        # 4. Guards inside subdir: .env.local, .git, symlink refused
        for bad_path in (".env.local", ".git/config", "hosts_link", "../src/root.txt"):
            self._assert_fs_refused(self._run_fs("diff", name, {"subdir": "tools", "paths": [bad_path]}), bad_path)

        # 5. Invalid, unknown, unattached, or non-repo subdirs refused
        for bad_sub in (
            "",
            123,
            ["tools"],
            "/workspace/extras/tools",
            "extras\\tools",
            "../other",
            "extras/../tools",
            ".",
            "./extras/tools",
            "extras//tools",
            ".git",
            ".git/hooks",
            "extras/tools\x00bad",
            "unknown",
            "src",
            "extras",
            "extras/tools/sub",
            "unused",
            "extras/unused",
        ):
            self._assert_fs_refused(self._run_fs("diff", name, {"subdir": bad_sub}), repr(bad_sub))

        # 6. Symlink or non-directory at subdir path refused via slot-fs.pl lstat guard
        outside_repo = os.path.join(self.harness.tmp, "outside-git-repo")
        os.makedirs(outside_repo)
        subprocess.run([self.harness.real_git_bin, "init", "-q", outside_repo], check=True)
        shutil.rmtree(tools_dir)
        os.symlink(outside_repo, tools_dir)
        self._assert_fs_refused(self._run_fs("diff", name, {"subdir": "extras/tools"}), "symlink subdir leaf")
        self._assert_fs_refused(self._run_fs("diff", name, {"subdir": "tools"}), "symlink subdir alias")

        os.unlink(tools_dir)
        with open(tools_dir, "w", encoding="utf-8") as handle:
            handle.write("not a dir\n")
        self._assert_fs_refused(self._run_fs("diff", name, {"subdir": "extras/tools"}), "regular file subdir")

        os.remove(tools_dir)
        outside_parent = os.path.join(self.harness.tmp, "outside-parent")
        os.makedirs(os.path.join(outside_parent, "tools"), exist_ok=True)
        subprocess.run([self.harness.real_git_bin, "init", "-q", os.path.join(outside_parent, "tools")], check=True)
        shutil.rmtree(os.path.join(slot_dir, "extras"))
        os.symlink(outside_parent, os.path.join(slot_dir, "extras"))
        self._assert_fs_refused(self._run_fs("diff", name, {"subdir": "extras/tools"}), "symlink intermediate dir")


if __name__ == "__main__":
    unittest.main()
