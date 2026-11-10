"""Tests for facts collection, slot_sh, and container facts caching (#10)."""

import os
import shutil
import subprocess
import unittest

from harness import (
    DOCKER_STUB,
    DevContainerTestCase,
    PROJECT_PREFIX,
)


class FactsTestCase(DevContainerTestCase):

    DOCKER_WRAPPER = r"""#!/usr/bin/env bash
set -u
log="${DOCKER_WRAPPER_LOG:-/dev/null}"
state_dir="${DOCKER_STUB_STATE:-/tmp}"
base="$(dirname "$0")/docker-base"

printf 'CALL: %s\n' "$*" >> "$log"

verb="" proj="" svc=""
has_rm=0 has_no_deps=0
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
        --rm) has_rm=1 ;;
        --no-deps) has_no_deps=1 ;;
        -T|-u|-w) ;;
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

printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$verb" "$proj" "$svc" "$has_rm" "$has_no_deps" "${cmd[*]-}" >> "$log.tsv"

if [[ -n "$proj" && -f "$state_dir/$proj.fail" ]]; then
  echo "container execution failed" >&2
  exit 1
fi

if [[ "$verb" == exec || "$verb" == run ]]; then
  if [[ -n "${WORKSPACE_DIR:-}" && -d "$WORKSPACE_DIR" ]]; then
    cd "$WORKSPACE_DIR"
  fi
  if (( ${#cmd[@]} > 0 )); then
    exec "${cmd[@]}"
  fi
  exit 0
fi

exec "$base" "$@"
"""

    def setUp(self):
        super().setUp()
        self.scratch = os.path.join(self.harness.tmp, "scratch")
        os.makedirs(os.path.join(self.scratch, "facts"), exist_ok=True)
        self.harness.build_fixture_repo()

        self.docker_wrapper_log = os.path.join(self.harness.tmp, "docker-wrapper.log")
        self.harness._write_stub("docker-base", DOCKER_STUB)
        self.harness._write_stub("docker", self.DOCKER_WRAPPER)

    def _make_slot(self, name="slot-1", running=False):
        self.harness.make_bare_slot(name, local_identity=("Test Agent", "test@example.com"))
        slot_dir = self.harness.slot_dir(name)
        sid = f"{PROJECT_PREFIX}-{name}"
        running_path = os.path.join(self.harness.docker_state, f"{sid}.running")
        if running:
            open(running_path, "w").close()
        else:
            if os.path.exists(running_path):
                os.remove(running_path)
        return name, sid, slot_dir

    def _git(self, slot_dir):
        return lambda *args: subprocess.run(
            [
                self.harness.real_git_bin, "-C", slot_dir,
                "-c", "commit.gpgsign=false",
                "-c", "user.name=Test Agent",
                "-c", "user.email=test@example.com",
                *args,
            ],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )

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
                parts = line.split("\t", 5)
                if len(parts) == 6:
                    calls.append({
                        "verb": parts[0],
                        "proj": parts[1],
                        "svc": parts[2],
                        "has_rm": parts[3] == "1",
                        "has_no_deps": parts[4] == "1",
                        "cmd": parts[5],
                    })
        return calls

    def _clear_docker_calls(self):
        tsv_path = self.docker_wrapper_log + ".tsv"
        if os.path.exists(tsv_path):
            os.remove(tsv_path)

    def run_bash(self, script, *args, extra_env=None):
        env = self.harness._env(extra_env)
        env["DEV_CONTAINER_LIB"] = "1"
        env["SCRATCH"] = self.scratch
        env["DOCKER_WRAPPER_LOG"] = self.docker_wrapper_log
        dev_container = self.harness.script
        preamble = (
            f'. "{dev_container}"\n'
            f'lib_dir="$(CDPATH= cd -P "$(dirname "{dev_container}")/../lib" && pwd)"\n'
            '[[ -f "$lib_dir/facts.sh" ]] && . "$lib_dir/facts.sh"\n'
        )
        cmd = preamble + script
        return subprocess.run(
            ["bash", "-c", cmd, "test", *args],
            cwd=self.harness.fixture_repo,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )


class TestSlotSh(FactsTestCase):

    def test_slot_sh_exec_when_container_running(self):
        name, sid, slot_dir = self._make_slot("slot-exec", running=True)
        res = self.run_bash('slot_sh "$@" echo "running-test"', sid)
        self.assertEqual(res.returncode, 0, f"slot_sh failed: {res.stderr.decode()}")
        self.assertEqual(res.stdout.decode().strip(), "running-test")

        calls = [c for c in self._docker_calls() if c["proj"] == sid]
        self.assertTrue(
            any(c["verb"] == "exec" and c["svc"] == "app" for c in calls),
            f"Expected compose exec targeting app, got: {calls}",
        )
        self.assertFalse(
            any(c["verb"] == "run" for c in calls),
            "Unexpected compose run call while container is running",
        )

    def test_slot_sh_run_when_container_stopped(self):
        name, sid, slot_dir = self._make_slot("slot-run", running=False)
        res = self.run_bash('slot_sh "$@" echo "stopped-test"', sid)
        self.assertEqual(res.returncode, 0, f"slot_sh failed: {res.stderr.decode()}")
        self.assertEqual(res.stdout.decode().strip(), "stopped-test")

        calls = [c for c in self._docker_calls() if c["proj"] == sid]
        self.assertTrue(
            any(c["verb"] == "run" and c["svc"] == "app" and c["has_rm"] and c["has_no_deps"] for c in calls),
            f"Expected compose run --rm --no-deps targeting app, got: {calls}",
        )
        self.assertFalse(
            any(c["verb"] == "exec" for c in calls),
            "Unexpected compose exec call while container is stopped",
        )

    def test_slot_sh_preserves_exit_code_running(self):
        name, sid, slot_dir = self._make_slot("slot-fail-exec", running=True)
        res = self.run_bash('slot_sh "$@" sh -c "exit 42"', sid)
        self.assertEqual(res.returncode, 42)

    def test_slot_sh_preserves_exit_code_stopped(self):
        name, sid, slot_dir = self._make_slot("slot-fail-run", running=False)
        res = self.run_bash('slot_sh "$@" sh -c "exit 42"', sid)
        self.assertEqual(res.returncode, 42)

    def test_slot_sh_passes_multiple_arguments(self):
        name, sid, slot_dir = self._make_slot("slot-args", running=True)
        res = self.run_bash('slot_sh "$@" printf "%s\\n" "first" "second with space" "third"', sid)
        self.assertEqual(res.returncode, 0, res.stderr.decode())
        lines = res.stdout.decode().splitlines()
        self.assertEqual(lines, ["first", "second with space", "third"])


class TestFactsOf(FactsTestCase):

    def test_facts_of_executes_facts_script_in_container(self):
        name, sid, slot_dir = self._make_slot("slot-facts-exec")
        git = self._git(slot_dir)
        git("commit", "--allow-empty", "-m", "init commit")

        res = self.run_bash('facts_of "$@"', sid)
        self.assertEqual(res.returncode, 0, f"facts_of failed: {res.stderr.decode()}")
        calls = [c for c in self._docker_calls() if c["proj"] == sid]
        self.assertGreater(len(calls), 0, "facts_of never called slot_sh/docker")

    def test_facts_of_caches_result_under_scratch_facts(self):
        name, sid, slot_dir = self._make_slot("slot-cache-file")
        git = self._git(slot_dir)
        git("commit", "--allow-empty", "-m", "init commit")

        res = self.run_bash('facts_of "$@"', sid)
        self.assertEqual(res.returncode, 0, res.stderr.decode())

        cache_file = os.path.join(self.scratch, "facts", sid)
        self.assertTrue(os.path.exists(cache_file), f"Expected cache file at {cache_file}")
        with open(cache_file, encoding="utf-8") as f:
            content = f.read()
        self.assertEqual(content, res.stdout.decode())

    def test_facts_of_uses_cached_result_on_subsequent_calls(self):
        name, sid, slot_dir = self._make_slot("slot-cache-reuse")
        git = self._git(slot_dir)
        git("commit", "--allow-empty", "-m", "init commit")

        res1 = self.run_bash('facts_of "$@"', sid)
        self.assertEqual(res1.returncode, 0, res1.stderr.decode())
        calls_before = len(self._docker_calls())

        res2 = self.run_bash('facts_of "$@"', sid)
        self.assertEqual(res2.returncode, 0, res2.stderr.decode())
        calls_after = len(self._docker_calls())

        self.assertEqual(
            calls_after, calls_before,
            "facts_of re-executed container command instead of using scratch cache",
        )
        self.assertEqual(res1.stdout.decode(), res2.stdout.decode())

    def test_facts_of_contains_tab_separated_git_keys(self):
        name, sid, slot_dir = self._make_slot("slot-keys")
        git = self._git(slot_dir)
        git("remote", "set-url", "origin", "https://github.com/example/demo.git")
        git("checkout", "-b", "feature-work")
        git("commit", "--allow-empty", "-m", "feature commit")

        res = self.run_bash('facts_of "$@"', sid)
        self.assertEqual(res.returncode, 0, res.stderr.decode())
        out = res.stdout.decode()

        expected_keys = ["origin", "head", "headreach", "default", "branch", "status", "stash", "user"]
        for key in expected_keys:
            self.assertTrue(
                any(line.startswith(f"{key}\t") or line == key for line in out.splitlines()),
                f"Expected key '{key}' in tab-separated facts output:\n{out}",
            )

    def test_facts_of_isolation_between_slots(self):
        name1, sid1, slot_dir1 = self._make_slot("slot-iso-1")
        name2, sid2, slot_dir2 = self._make_slot("slot-iso-2")
        git1 = self._git(slot_dir1)
        git2 = self._git(slot_dir2)
        git1("checkout", "-b", "branch-one")
        git1("commit", "--allow-empty", "-m", "commit 1")
        git2("checkout", "-b", "branch-two")
        git2("commit", "--allow-empty", "-m", "commit 2")

        self.run_bash('facts_of "$@"', sid1)
        self.run_bash('facts_of "$@"', sid2)

        cache1 = os.path.join(self.scratch, "facts", sid1)
        cache2 = os.path.join(self.scratch, "facts", sid2)
        self.assertTrue(os.path.exists(cache1))
        self.assertTrue(os.path.exists(cache2))

        with open(cache1, encoding="utf-8") as f:
            c1 = f.read()
        with open(cache2, encoding="utf-8") as f:
            c2 = f.read()

        self.assertIn("branch-one", c1)
        self.assertNotIn("branch-two", c1)
        self.assertIn("branch-two", c2)
        self.assertNotIn("branch-one", c2)


class TestFactsReset(FactsTestCase):

    def test_facts_reset_clears_slot_cache(self):
        name, sid, slot_dir = self._make_slot("slot-reset-1")
        git = self._git(slot_dir)
        git("commit", "--allow-empty", "-m", "init")

        self.run_bash('facts_of "$@"', sid)
        cache_path = os.path.join(self.scratch, "facts", sid)
        self.assertTrue(os.path.exists(cache_path))

        res = self.run_bash('facts_reset "$@"', sid)
        self.assertEqual(res.returncode, 0, res.stderr.decode())
        self.assertFalse(os.path.exists(cache_path), "facts_reset did not delete the cache file")

    def test_facts_reset_forces_recache_on_next_facts_of(self):
        name, sid, slot_dir = self._make_slot("slot-recache")
        git = self._git(slot_dir)
        git("commit", "--allow-empty", "-m", "init")

        self.run_bash('facts_of "$@"', sid)
        self.run_bash('facts_reset "$@"', sid)
        self._clear_docker_calls()

        res = self.run_bash('facts_of "$@"', sid)
        self.assertEqual(res.returncode, 0, res.stderr.decode())
        calls = [c for c in self._docker_calls() if c["proj"] == sid]
        self.assertGreater(len(calls), 0, "facts_of did not re-execute after facts_reset")

        cache_path = os.path.join(self.scratch, "facts", sid)
        self.assertTrue(os.path.exists(cache_path))

    def test_facts_reset_idempotent_when_no_cache(self):
        name, sid, slot_dir = self._make_slot("slot-reset-idempotent")
        cache_path = os.path.join(self.scratch, "facts", sid)
        self.assertFalse(os.path.exists(cache_path))

        res = self.run_bash('facts_reset "$@"', sid)
        self.assertEqual(res.returncode, 0, f"facts_reset failed: {res.stderr.decode()}")

    def test_facts_reset_does_not_affect_other_slots(self):
        name1, sid1, slot_dir1 = self._make_slot("slot-reset-a")
        name2, sid2, slot_dir2 = self._make_slot("slot-reset-b")
        git1 = self._git(slot_dir1)
        git2 = self._git(slot_dir2)
        git1("commit", "--allow-empty", "-m", "commit 1")
        git2("commit", "--allow-empty", "-m", "commit 2")

        self.run_bash('facts_of "$@"', sid1)
        self.run_bash('facts_of "$@"', sid2)

        cache1 = os.path.join(self.scratch, "facts", sid1)
        cache2 = os.path.join(self.scratch, "facts", sid2)
        self.assertTrue(os.path.exists(cache1))
        self.assertTrue(os.path.exists(cache2))

        self.run_bash('facts_reset "$@"', sid1)
        self.assertFalse(os.path.exists(cache1))
        self.assertTrue(os.path.exists(cache2), "Resetting slot 1 improperly removed slot 2 cache")


class TestFactsUnreadable(FactsTestCase):

    def test_facts_unreadable_true_when_container_fails(self):
        name, sid, slot_dir = self._make_slot("slot-fail")
        with open(os.path.join(self.harness.docker_state, f"{sid}.fail"), "w") as f:
            pass

        self.run_bash('facts_of "$@"', sid)
        res = self.run_bash('rc=0; facts_unreadable "$@" || rc=$?; exit "$rc"', sid)
        self.assertEqual(res.returncode, 0, f"facts_unreadable must be true (rc=0) when container failed: {res.stderr.decode()}")

    def test_facts_unreadable_true_when_norepo(self):
        name, sid, slot_dir = self._make_slot("slot-norepo")
        shutil.rmtree(os.path.join(slot_dir, ".git"), ignore_errors=True)

        self.run_bash('facts_of "$@"', sid)
        res = self.run_bash('rc=0; facts_unreadable "$@" || rc=$?; exit "$rc"', sid)
        self.assertEqual(res.returncode, 0, f"facts_unreadable must be true (rc=0) when directory has no repo: {res.stderr.decode()}")

    def test_facts_unreadable_false_when_repo_valid(self):
        name, sid, slot_dir = self._make_slot("slot-valid")
        git = self._git(slot_dir)
        git("commit", "--allow-empty", "-m", "valid commit")

        self.run_bash('facts_of "$@"', sid)
        res = self.run_bash('rc=0; facts_unreadable "$@" || rc=$?; exit "$rc"', sid)
        self.assertEqual(res.returncode, 1, f"facts_unreadable must be false (rc=1) when repo is valid: {res.stderr.decode()}")

    def test_facts_unreadable_piped_content(self):
        res_err = self.run_bash('rc=0; printf "error\\tcontainer died\\n" | facts_unreadable || rc=$?; exit "$rc"')
        self.assertEqual(res_err.returncode, 0, "facts_unreadable should match error from pipe")

        res_norepo = self.run_bash('rc=0; printf "norepo\\n" | facts_unreadable || rc=$?; exit "$rc"')
        self.assertEqual(res_norepo.returncode, 0, "facts_unreadable should match norepo from pipe")

        res_ok = self.run_bash('rc=0; printf "branch\\tmain\\nhead\\t1234\\n" | facts_unreadable || rc=$?; exit "$rc"')
        self.assertEqual(res_ok.returncode, 1, "facts_unreadable should not match valid facts from pipe")


class TestFactAndFactsLines(FactsTestCase):

    def test_fact_extracts_single_keys(self):
        name, sid, slot_dir = self._make_slot("slot-single-fact")
        cache_path = os.path.join(self.scratch, "facts", sid)
        with open(cache_path, "w", encoding="utf-8") as f:
            f.write("origin\thttps://github.com/example/demo.git\n")
            f.write("branch\tfeature-facts\n")
            f.write("head\t0123456789abcdef0123456789abcdef01234567\n")
            f.write("default\tmaster\n")
            f.write("user\tTest Agent <test@example.com>\n")

        res_b = self.run_bash('fact "$1" branch', sid)
        self.assertEqual(res_b.returncode, 0, f"fact failed: {res_b.stderr.decode()}")
        self.assertEqual(res_b.stdout.decode().strip(), "feature-facts")

        res_h = self.run_bash('fact "$1" head', sid)
        self.assertEqual(res_h.returncode, 0, f"fact failed: {res_h.stderr.decode()}")
        self.assertEqual(res_h.stdout.decode().strip(), "0123456789abcdef0123456789abcdef01234567")

        res_o = self.run_bash('fact "$1" origin', sid)
        self.assertEqual(res_o.returncode, 0, f"fact failed: {res_o.stderr.decode()}")
        self.assertEqual(res_o.stdout.decode().strip(), "https://github.com/example/demo.git")

        res_d = self.run_bash('fact "$1" default', sid)
        self.assertEqual(res_d.returncode, 0, f"fact failed: {res_d.stderr.decode()}")
        self.assertEqual(res_d.stdout.decode().strip(), "master")

    def test_fact_missing_key_returns_empty(self):
        name, sid, slot_dir = self._make_slot("slot-fact-missing")
        cache_path = os.path.join(self.scratch, "facts", sid)
        with open(cache_path, "w", encoding="utf-8") as f:
            f.write("branch\tmain\n")

        res = self.run_bash('fact "$1" unknown_key', sid)
        self.assertEqual(res.returncode, 0, f"fact failed: {res.stderr.decode()}")
        self.assertEqual(res.stdout.decode().strip(), "")

    def test_facts_lines_extracts_multiline_status(self):
        name, sid, slot_dir = self._make_slot("slot-multiline-status")
        cache_path = os.path.join(self.scratch, "facts", sid)
        with open(cache_path, "w", encoding="utf-8") as f:
            f.write("branch\tmain\n")
            f.write("status\t M src/index.ts\n")
            f.write("status\t?? newfile.txt\n")
            f.write("status\tD  oldfile.txt\n")

        res = self.run_bash('facts_lines "$1" status', sid)
        self.assertEqual(res.returncode, 0, f"facts_lines failed: {res.stderr.decode()}")
        lines = res.stdout.decode().splitlines()
        self.assertEqual(lines, [" M src/index.ts", "?? newfile.txt", "D  oldfile.txt"])

    def test_facts_lines_extracts_multiline_stash(self):
        name, sid, slot_dir = self._make_slot("slot-multiline-stash")
        cache_path = os.path.join(self.scratch, "facts", sid)
        with open(cache_path, "w", encoding="utf-8") as f:
            f.write("stash\tstash@{0}: WIP on main\n")
            f.write("stash\tstash@{1}: older work\n")

        res = self.run_bash('facts_lines "$1" stash', sid)
        self.assertEqual(res.returncode, 0, f"facts_lines failed: {res.stderr.decode()}")
        lines = res.stdout.decode().splitlines()
        self.assertEqual(lines, ["stash@{0}: WIP on main", "stash@{1}: older work"])

    def test_facts_lines_single_or_empty_key(self):
        name, sid, slot_dir = self._make_slot("slot-lines-single")
        cache_path = os.path.join(self.scratch, "facts", sid)
        with open(cache_path, "w", encoding="utf-8") as f:
            f.write("branch\tmain\n")

        res_single = self.run_bash('facts_lines "$1" branch', sid)
        self.assertEqual(res_single.returncode, 0, f"facts_lines failed: {res_single.stderr.decode()}")
        self.assertEqual(res_single.stdout.decode().splitlines(), ["main"])

        res_empty = self.run_bash('facts_lines "$1" nonexistent', sid)
        self.assertEqual(res_empty.returncode, 0, f"facts_lines failed: {res_empty.stderr.decode()}")
        self.assertEqual(res_empty.stdout.decode().strip(), "")

    def test_fact_and_facts_lines_piped(self):
        pipe_input = "branch\\tdevelop\\nstatus\\tline1\\nstatus\\tline2\\n"
        res_fact = self.run_bash(f'printf "{pipe_input}" | fact branch')
        self.assertEqual(res_fact.returncode, 0, f"fact failed: {res_fact.stderr.decode()}")
        self.assertEqual(res_fact.stdout.decode().strip(), "develop")

        res_lines = self.run_bash(f'printf "{pipe_input}" | facts_lines status')
        self.assertEqual(res_lines.returncode, 0, f"facts_lines failed: {res_lines.stderr.decode()}")
        self.assertEqual(res_lines.stdout.decode().splitlines(), ["line1", "line2"])


class TestMergedOidsCoverTip(FactsTestCase):

    def test_merged_oids_cover_tip_true_when_ancestor(self):
        name, sid, slot_dir = self._make_slot("slot-cover-true")
        git = self._git(slot_dir)

        git("checkout", "-b", "master")
        git("commit", "--allow-empty", "-m", "base")

        git("checkout", "-b", "feature")
        git("commit", "--allow-empty", "-m", "feature commit")
        tip = git("rev-parse", "HEAD").stdout.decode().strip()

        git("checkout", "master")
        git("merge", "--no-ff", "-m", "merge feature", "feature")
        merge_oid = git("rev-parse", "HEAD").stdout.decode().strip()

        res = self.run_bash(
            'rc=0; merged_oids_cover_tip "$1" "$2" "$3" || rc=$?; exit "$rc"',
            sid, tip, merge_oid,
        )
        self.assertEqual(res.returncode, 0, f"merged_oids_cover_tip should be true (rc=0): {res.stderr.decode()}")

    def test_merged_oids_cover_tip_false_when_not_ancestor(self):
        name, sid, slot_dir = self._make_slot("slot-cover-false")
        git = self._git(slot_dir)

        git("checkout", "-b", "master")
        git("commit", "--allow-empty", "-m", "base")

        git("checkout", "-b", "feature-1")
        git("commit", "--allow-empty", "-m", "feature 1 commit")
        tip = git("rev-parse", "HEAD").stdout.decode().strip()

        git("checkout", "master")
        git("checkout", "-b", "feature-2")
        git("commit", "--allow-empty", "-m", "unrelated commit")
        unrelated_oid = git("rev-parse", "HEAD").stdout.decode().strip()

        res = self.run_bash(
            'rc=0; merged_oids_cover_tip "$1" "$2" "$3" || rc=$?; exit "$rc"',
            sid, tip, unrelated_oid,
        )
        self.assertEqual(res.returncode, 1, f"merged_oids_cover_tip should be false (rc=1) for unrelated commit: {res.stderr.decode()}")

    def test_merged_oids_cover_tip_false_when_empty_oids(self):
        name, sid, slot_dir = self._make_slot("slot-cover-empty")
        git = self._git(slot_dir)
        git("commit", "--allow-empty", "-m", "init")
        tip = git("rev-parse", "HEAD").stdout.decode().strip()

        res = self.run_bash(
            'rc=0; merged_oids_cover_tip "$1" "$2" "$3" || rc=$?; exit "$rc"',
            sid, tip, "",
        )
        self.assertEqual(res.returncode, 1, f"merged_oids_cover_tip should be false (rc=1) when oids is empty: {res.stderr.decode()}")

    def test_merged_oids_cover_tip_multiple_oids_with_one_covering(self):
        name, sid, slot_dir = self._make_slot("slot-cover-multi")
        git = self._git(slot_dir)

        git("checkout", "-b", "master")
        git("commit", "--allow-empty", "-m", "base")

        git("checkout", "-b", "feature")
        git("commit", "--allow-empty", "-m", "feature commit")
        tip = git("rev-parse", "HEAD").stdout.decode().strip()

        git("checkout", "master")
        git("merge", "--no-ff", "-m", "merge feature", "feature")
        merge_oid = git("rev-parse", "HEAD").stdout.decode().strip()

        unrelated = "0000000000000000000000000000000000000000"
        oids = f"{unrelated}\n{merge_oid}"

        res = self.run_bash(
            'rc=0; merged_oids_cover_tip "$1" "$2" "$3" || rc=$?; exit "$rc"',
            sid, tip, oids,
        )
        self.assertEqual(res.returncode, 0, f"merged_oids_cover_tip should be true (rc=0) when one oid covers tip: {res.stderr.decode()}")

    def test_merged_oids_cover_tip_runs_via_slot_sh(self):
        name, sid, slot_dir = self._make_slot("slot-cover-slotsh")
        git = self._git(slot_dir)
        git("commit", "--allow-empty", "-m", "init")
        tip = git("rev-parse", "HEAD").stdout.decode().strip()

        self._clear_docker_calls()
        self.run_bash('merged_oids_cover_tip "$1" "$2" "$3" || true', sid, tip, tip)
        calls = [c for c in self._docker_calls() if c["proj"] == sid]
        self.assertGreater(len(calls), 0, "merged_oids_cover_tip did not invoke slot_sh / docker")


class TestFactsScripts(FactsTestCase):

    def test_facts_script_defined(self):
        res = self.run_bash('printf "%s" "${FACTS_SCRIPT:-}"')
        self.assertEqual(res.returncode, 0, res.stderr.decode())
        self.assertGreater(len(res.stdout.decode().strip()), 0, "FACTS_SCRIPT must be defined and non-empty")

    def test_oids_cover_tip_script_defined(self):
        res = self.run_bash('printf "%s" "${OIDS_COVER_TIP_SCRIPT:-}"')
        self.assertEqual(res.returncode, 0, res.stderr.decode())
        self.assertGreater(len(res.stdout.decode().strip()), 0, "OIDS_COVER_TIP_SCRIPT must be defined and non-empty")

    def test_facts_script_execution_in_repo(self):
        name, sid, slot_dir = self._make_slot("slot-facts-script")
        git = self._git(slot_dir)
        git("remote", "set-url", "origin", "https://github.com/example/demo.git")
        git("checkout", "-b", "feature-xyz")
        git("commit", "--allow-empty", "-m", "commit on feature")

        script = 'cd "$1" && eval "$FACTS_SCRIPT"'
        res = self.run_bash(script, slot_dir)
        self.assertEqual(res.returncode, 0, res.stderr.decode())
        out = res.stdout.decode()
        self.assertIn("branch\tfeature-xyz", out)
        self.assertIn("origin\thttps://github.com/example/demo.git", out)
        self.assertIn("head\t", out)

    def test_facts_script_execution_in_non_repo(self):
        empty_dir = os.path.join(self.harness.tmp, "empty-non-repo")
        os.makedirs(empty_dir, exist_ok=True)
        script = 'cd "$1" && eval "$FACTS_SCRIPT"'
        res = self.run_bash(script, empty_dir)
        self.assertEqual(res.returncode, 0, res.stderr.decode())
        out = res.stdout.decode()
        self.assertIn("norepo", out)


if __name__ == "__main__":
    unittest.main()
