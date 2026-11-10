import os
import shutil
import stat
import subprocess
import time
import unittest

import test_schedule
from harness import LIB_REL, PROJECT_PREFIX, REPO_ROOT, SCRIPT_REL

MARK = "SENTINEL_11_FORGE"
TOKEN = f"forge-read-{MARK}-new"
OLD_TOKEN = f"forge-read-{MARK}-old"

GH_STUB = r"""#!/usr/bin/env bash
set -u
cap="${GH_CAPTURE_DIR:?}"
printf "%s\n" "$*" >> "$cap/argv.log"
printf "%s\n" "${GH_TOKEN-UNSET}" >> "$cap/token.log"
ps -eo args= >> "$cap/ps.log" 2>/dev/null || ps -o args= >> "$cap/ps.log" 2>/dev/null || true
echo "[]"
"""


class UnlockHarness(test_schedule.ScheduleHarness):

    def __init__(self):
        self.run_dir = None
        self.gh_dir = None
        super().__init__()
        self.run_dir = os.path.join(self.tmp, "run-dev-container")
        self.gh_dir = os.path.join(self.tmp, "gh-capture")
        os.makedirs(self.run_dir, mode=0o700, exist_ok=True)
        os.makedirs(self.gh_dir, exist_ok=True)

    def _build_stub_bin(self):
        super()._build_stub_bin()
        self._write_stub("gh", GH_STUB)

    def _env(self, port_overrides):
        env = super()._env(port_overrides)
        env.pop("GH_TOKEN", None)
        env.pop("GITHUB_TOKEN", None)
        env["DEV_CONTAINER_RUN_DIR"] = self.run_dir
        env["GH_CAPTURE_DIR"] = self.gh_dir
        return env


class UnlockCase(test_schedule.ScheduleCase):
    harness_class = UnlockHarness

    def setUp(self):
        super().setUp()
        self.harness.build()
        self.run_dir = self.harness.run_dir
        self.gh_dir = self.harness.gh_dir
        self.rescue = os.path.join(self.state, "rescue")
        os.makedirs(self.rescue, exist_ok=True)

    def engine(self, *args, input_text=None, fast=1000, slow=1000, timeout=60, **extra):
        kwargs = {
            "cwd": self.harness.tmp,
            "env": self.env(fast, slow, **extra),
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "timeout": timeout,
        }
        if input_text is not None:
            kwargs["input"] = input_text.encode("utf-8")
        else:
            kwargs["stdin"] = subprocess.DEVNULL
        return subprocess.run(
            ["bash", os.path.join(self.plain, SCRIPT_REL), *args],
            **kwargs,
        )

    def unlock(self, token=TOKEN, *args, **extra):
        body = token if token.endswith("\n") else token + "\n"
        return self.engine("unlock", *args, input_text=body, **extra)

    def run_files(self):
        found = []
        if not os.path.isdir(self.run_dir):
            return found
        for root, _, names in os.walk(self.run_dir):
            for name in sorted(names):
                path = os.path.join(root, name)
                mode = stat.S_IMODE(os.stat(path).st_mode)
                with open(path, encoding="utf-8", errors="replace") as handle:
                    found.append((path, mode, handle.read()))
        return found

    def clear_run_dir(self):
        if os.path.isdir(self.run_dir):
            shutil.rmtree(self.run_dir)
        os.makedirs(self.run_dir, mode=0o700, exist_ok=True)

    def clear_gh_logs(self):
        if os.path.isdir(self.gh_dir):
            shutil.rmtree(self.gh_dir)
        os.makedirs(self.gh_dir, exist_ok=True)

    def _read_lines(self, name):
        path = os.path.join(self.gh_dir, name)
        if not os.path.isfile(path):
            return []
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read().splitlines()

    def gh_tokens(self):
        return self._read_lines("token.log")

    def gh_argv(self):
        return "\n".join(self._read_lines("argv.log"))

    def gh_ps(self):
        return "\n".join(self._read_lines("ps.log"))

    def plant_rescue_file(self, name, days_old=60):
        path = os.path.join(self.rescue, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("rescue-archive\n")
        stamp = time.time() - days_old * 86400
        os.utime(path, (stamp, stamp))
        return path

    def assert_no_leak(self, *results):
        for res in results:
            out = self.output(res)
            self.assertNotIn(MARK, out)
            self.assertNotIn(TOKEN, out)
            self.assertNotIn(OLD_TOKEN, out)
        for root, _, names in os.walk(self.state):
            for name in names:
                path = os.path.join(root, name)
                with open(path, encoding="utf-8", errors="replace") as handle:
                    body = handle.read()
                self.assertNotIn(MARK, body, path)
                self.assertNotIn(TOKEN, body, path)
                self.assertNotIn(OLD_TOKEN, body, path)


class UnlockAndCheckHelp(UnlockCase):

    def test_help_prints_usage_for_unlock_and_check(self):
        for verb in ("unlock", "check"):
            with self.subTest(verb=verb):
                result = self.engine(verb, "--help")
                self.assertEqual(result.returncode, 0, self.output(result))
                self.assertIn(verb, result.stdout.decode())


class UnlockReadsTokenOnStdin(UnlockCase):

    def test_unlock_reads_token_from_stdin_into_run_dir_with_mode_0600(self):
        self.addCleanup(os.umask, os.umask(0o022))

        result = self.unlock(TOKEN)

        self.assertEqual(result.returncode, 0, self.output(result))
        files = self.run_files()
        self.assertEqual(len(files), 1, files)
        _, mode, content = files[0]
        self.assertEqual(mode, 0o600)
        self.assertEqual(content.strip(), TOKEN)

    def test_after_unlock_check_gives_gh_the_token_in_gh_token(self):
        self.write_lease("slot-1", self.harness.fixture_repo)
        self.plant_rescue_file("demo-slot-1-20200101-000000-1.bundle", days_old=60)
        unlocked = self.unlock(TOKEN)
        self.assertEqual(unlocked.returncode, 0, self.output(unlocked))

        checked = self.engine("check")

        self.assertEqual(checked.returncode, 0, self.output(checked))
        self.assertNotIn("locked", self.output(checked).lower())
        self.assertIn(TOKEN, self.gh_tokens())

    def test_after_unlock_schedule_slow_job_runs_check_with_forge_token(self):
        self.write_lease("slot-1", self.harness.fixture_repo)
        self.plant_rescue_file("demo-slot-1-20200101-000000-1.bundle", days_old=60)
        self.write_stamp("fast", time.time())
        unlocked = self.unlock(TOKEN)
        self.assertEqual(unlocked.returncode, 0, self.output(unlocked))

        scheduled = self.engine("schedule", "run", "--once")

        self.assertEqual(scheduled.returncode, 0, self.output(scheduled))
        self.assertIn(TOKEN, self.gh_tokens())
        self.assertNotIn("locked", self.read_log("slow").lower())

    def test_clearing_tmpfs_on_container_restart_locks_check_again(self):
        self.write_lease("slot-1", self.harness.fixture_repo)
        self.assertEqual(self.unlock(TOKEN).returncode, 0)
        first = self.engine("check")
        self.assertEqual(first.returncode, 0, self.output(first))
        self.assertIn(TOKEN, self.gh_tokens())

        self.clear_run_dir()
        self.clear_gh_logs()
        restarted = self.engine("check", DEV_CONTAINER_BOOT_ID="boot-b")

        self.assertIn("locked", self.output(restarted).lower())
        self.assertEqual(self.gh_tokens(), [])


class TokenNeverLeaks(UnlockCase):

    def test_token_never_appears_in_state_files_slow_log_or_command_output(self):
        self.write_lease("slot-1", self.harness.fixture_repo)
        unlocked = self.unlock(TOKEN)
        checked = self.engine("check")
        scheduled = self.engine("schedule", "run", "--once")
        refused = self.engine("unlock", TOKEN)

        self.assertEqual(unlocked.returncode, 0, self.output(unlocked))
        self.assertEqual(checked.returncode, 0, self.output(checked))
        self.assertEqual(scheduled.returncode, 0, self.output(scheduled))
        self.assertNotEqual(refused.returncode, 0)
        self.assertTrue(os.path.isfile(self.log_path("slow")))
        self.assert_no_leak(unlocked, checked, scheduled, refused)

    def test_token_never_appears_in_gh_argv_or_ps_output(self):
        self.write_lease("slot-1", self.harness.fixture_repo)
        self.plant_rescue_file("demo-slot-1-20200101-000000-1.bundle", days_old=60)
        self.assertEqual(self.unlock(TOKEN).returncode, 0)

        checked = self.engine("check")

        self.assertEqual(checked.returncode, 0, self.output(checked))
        self.assertIn(TOKEN, self.gh_tokens())
        self.assertNotIn(MARK, self.gh_argv())
        self.assertNotIn(TOKEN, self.gh_argv())
        self.assertNotIn(MARK, self.gh_ps())
        self.assertNotIn(TOKEN, self.gh_ps())


class LockedWithoutToken(UnlockCase):

    def test_without_token_check_is_skipped_reports_locked_and_keeps_rescue_files(self):
        bundle = self.plant_rescue_file("demo-slot-9-20200101-000000-1.bundle", days_old=60)
        archive = self.plant_rescue_file(
            "demo-slot-9-20200101-000000-1-untracked.tar.gz", days_old=400
        )

        result = self.engine("check")

        self.assertIn("locked", self.output(result).lower())
        self.assertEqual(self.gh_tokens(), [])
        self.assertTrue(os.path.exists(bundle))
        self.assertTrue(os.path.exists(archive))

    def test_without_token_schedule_slow_check_reports_locked_and_keeps_rescue_files(self):
        bundle = self.plant_rescue_file("demo-slot-9-20200101-000000-2.bundle", days_old=60)
        self.write_stamp("fast", time.time())

        result = self.engine("schedule", "run", "--once")

        self.assertEqual(result.returncode, 0, self.output(result))
        self.assertIn("locked", self.read_log("slow").lower())
        self.assertEqual(self.gh_tokens(), [])
        self.assertTrue(os.path.exists(bundle))


class UnlockRefusesBadInput(UnlockCase):

    def test_empty_or_whitespace_stdin_is_refused_and_stores_no_token(self):
        for raw in ("", "\n", "   \n", "\t\r\n  "):
            with self.subTest(raw=repr(raw)):
                result = self.engine("unlock", input_text=raw)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("was not told which directory", self.output(result))
                self.assertNotIn("Usage: ", self.output(result))
                self.assertEqual(self.run_files(), [])

    def test_empty_or_whitespace_stdin_leaves_an_earlier_token_in_place(self):
        self.write_lease("slot-1", self.harness.fixture_repo)
        self.assertEqual(self.unlock(OLD_TOKEN).returncode, 0)

        for raw in ("", "\n", "   \t\n"):
            with self.subTest(raw=repr(raw)):
                refused = self.engine("unlock", input_text=raw)
                self.assertNotEqual(refused.returncode, 0)

        self.clear_gh_logs()
        checked = self.engine("check")
        self.assertEqual(checked.returncode, 0, self.output(checked))
        self.assertIn(OLD_TOKEN, self.gh_tokens())

    def test_positional_argument_to_unlock_is_refused_and_leaves_earlier_token_in_place(self):
        self.write_lease("slot-1", self.harness.fixture_repo)
        self.assertEqual(self.unlock(OLD_TOKEN).returncode, 0)

        refused_devnull = self.engine("unlock", TOKEN)
        refused_stdin = self.engine("unlock", TOKEN, input_text=TOKEN + "\n")

        self.assertNotEqual(refused_devnull.returncode, 0)
        self.assertNotEqual(refused_stdin.returncode, 0)
        self.assert_no_leak(refused_devnull, refused_stdin)
        self.clear_gh_logs()
        checked = self.engine("check")
        self.assertEqual(checked.returncode, 0, self.output(checked))
        self.assertIn(OLD_TOKEN, self.gh_tokens())
        self.assertNotIn(TOKEN, self.gh_tokens())

    def test_a_new_token_on_stdin_replaces_the_earlier_token(self):
        self.write_lease("slot-1", self.harness.fixture_repo)
        self.assertEqual(self.unlock(OLD_TOKEN).returncode, 0)
        self.assertEqual(self.unlock(TOKEN).returncode, 0)

        checked = self.engine("check")

        self.assertEqual(checked.returncode, 0, self.output(checked))
        self.assertIn(TOKEN, self.gh_tokens())
        self.assertNotIn(OLD_TOKEN, self.gh_tokens())


class FastCleanAndReleaseNeedNoToken(UnlockCase):

    def test_reap_and_reap_all_run_while_locked_with_no_token_in_run_dir(self):
        self.harness.write_limits(idle_limit=60)
        taken = self.harness.acquire("worker")
        slot_id = self.harness.slot_id(taken)
        name = self.harness.slot_name(slot_id)
        self.write_lease(name, self.harness.fixture_repo, age_seconds=3600)

        reaped = self.harness.run("reap")

        self.assertEqual(reaped.returncode, 0, self.output(reaped))
        self.assertFalse(self.harness.lease_exists(slot_id))
        self.assertEqual(self.run_files(), [])

        taken_all = self.harness.acquire("worker-2")
        slot_id_all = self.harness.slot_id(taken_all)
        name_all = self.harness.slot_name(slot_id_all)
        self.write_lease(name_all, self.harness.fixture_repo, age_seconds=3600)

        reaped_all = self.engine("reap", "--all")

        self.assertEqual(reaped_all.returncode, 0, self.output(reaped_all))
        self.assertFalse(self.harness.lease_exists(slot_id_all))
        self.assertEqual(self.run_files(), [])
        self.assertIn("locked", self.output(self.engine("check")).lower())

    def test_release_runs_while_locked_with_no_token_in_run_dir(self):
        taken = self.harness.acquire("worker")
        slot_id = self.harness.slot_id(taken)
        name = self.harness.slot_name(slot_id)

        released = self.harness.run("release", name, "--for", "worker")

        self.assertEqual(released.returncode, 0, self.output(released))
        self.assertFalse(self.harness.lease_exists(slot_id))
        self.assertEqual(self.run_files(), [])
        self.assertIn("locked", self.output(self.engine("check")).lower())


if __name__ == "__main__":
    unittest.main()
