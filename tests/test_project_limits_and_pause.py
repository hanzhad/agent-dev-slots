import json
import os
import re
import shutil
import subprocess
import sys
import unittest

if os.path.dirname(__file__) not in sys.path:
    sys.path.insert(0, os.path.dirname(__file__))

from harness import (
    DevContainerHarness,
    DevContainerTestCase,
    PROJECT_PREFIX,
    engine_source_lines,
    project_env_text,
    wait_until,
)


class ProjectLimitsTestCase(DevContainerTestCase):
    ENV_WITH_IDENTITY = (
        "GIT_USER_NAME=demo-agent\n"
        "GIT_USER_EMAIL=demo-agent@example.com\n"
    )
    OTHER_ENV = (
        "GIT_USER_NAME=other-agent\n"
        "GIT_USER_EMAIL=other-agent@example.com\n"
    )

    def setUp(self):
        super().setUp()
        self.harness.build_fixture_repo(with_parent_env=self.ENV_WITH_IDENTITY)

    def write_limits(self, limits, mtime=None):
        path = os.path.join(self.harness.state_dir, "limits.json")
        tmp_path = path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(limits, handle)
        if mtime is not None:
            os.utime(tmp_path, (mtime, mtime))
        os.replace(tmp_path, path)

    def make_second_project(self, label="other"):
        other = DevContainerHarness()
        self.addCleanup(other.cleanup)
        shutil.rmtree(other.state_dir)
        shutil.rmtree(other.docker_state)
        other.state_dir = self.harness.state_dir
        other.docker_state = self.harness.docker_state
        other.docker_log = self.harness.docker_log
        other.fixture_repo = os.path.join(other.tmp, label)
        other.build_fixture_repo(
            with_parent_env=self.OTHER_ENV,
            project_env=project_env_text(PROJECT=label),
        )
        subprocess.run(
            [
                other.real_git_bin,
                "-C",
                other.fixture_repo,
                "remote",
                "set-url",
                "origin",
                f"https://example.invalid/{label}.git",
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        return other

    @staticmethod
    def acquired_id(result):
        stdout = result.stdout.decode()
        match = re.search(r'"id": "([^"]+)"', stdout)
        assert match, stdout
        return match.group(1)

    def lease_files(self):
        leases_dir = os.path.join(self.harness.state_dir, "leases")
        if not os.path.isdir(leases_dir):
            return []
        return sorted(os.listdir(leases_dir))


class MachineMaxZeroGlobalPause(ProjectLimitsTestCase):

    def test_machine_max_zero_pauses_all_acquisitions_without_clamping_to_one(self):
        self.write_limits({"machine_max": 0})

        result = self.harness.run_acquire(holder="h1", owner="Agent [u1]")

        stderr = result.stderr.decode()
        self.assertEqual(result.returncode, 2, stderr)
        self.assertTrue("0/0" in stderr or "machine_max" in stderr, stderr)
        self.assertIn("limits.json", stderr)
        self.assertNotIn("below 1, using 1", stderr)
        self.assertNotIn("using 1", stderr)
        self.assertEqual(self.lease_files(), [])

    def test_negative_machine_max_clamps_to_zero_and_refuses(self):
        self.write_limits({"machine_max": -3})

        result = self.harness.run_acquire(holder="h1", owner="Agent [u1]")

        stderr = result.stderr.decode()
        self.assertEqual(result.returncode, 2, stderr)
        self.assertIn("0/0", stderr)
        self.assertNotIn("below 1, using 1", stderr)
        self.assertNotIn("using 1", stderr)
        self.assertEqual(self.lease_files(), [])


class TwoTierSessionAndMachineLimits(ProjectLimitsTestCase):

    def test_session_default_and_per_session_override(self):
        self.write_limits({"machine_max": 20, "default": 1, "sessions": {"u1": 2}})

        u1_first = self.harness.run_acquire(holder="h1", owner="Agent [u1]")
        self.assertEqual(u1_first.returncode, 0, u1_first.stderr.decode())
        u1_second = self.harness.run_acquire(holder="h2", owner="Agent [u1]")
        self.assertEqual(u1_second.returncode, 0, u1_second.stderr.decode())
        u1_third = self.harness.run_acquire(holder="h3", owner="Agent [u1]")
        self.assertEqual(u1_third.returncode, 4, u1_third.stderr.decode())

        u2_first = self.harness.run_acquire(holder="h4", owner="Agent [u2]")
        self.assertEqual(u2_first.returncode, 0, u2_first.stderr.decode())
        u2_second = self.harness.run_acquire(holder="h5", owner="Agent [u2]")
        self.assertEqual(u2_second.returncode, 4, u2_second.stderr.decode())

    def test_session_zero_pauses_session_with_clean_message_when_zero_held(self):
        self.write_limits({"machine_max": 20, "default": 2, "sessions": {"u1": 0}})

        u1_res = self.harness.run_acquire(holder="h1", owner="Agent [u1]")
        u1_err = u1_res.stderr.decode()
        self.assertEqual(u1_res.returncode, 4, u1_err)
        self.assertIn("limits.json", u1_err)
        self.assertIn("u1", u1_err)

        u2_res = self.harness.run_acquire(holder="h2", owner="Agent [u2]")
        self.assertEqual(u2_res.returncode, 0, u2_res.stderr.decode())

        self.write_limits({"machine_max": 20, "default": 0})
        u2_paused = self.harness.run_acquire(holder="h3", owner="Agent [u2]")
        self.assertEqual(u2_paused.returncode, 4, u2_paused.stderr.decode())

    def test_session_limits_clamp_to_zero_through_twenty(self):
        self.write_limits({"machine_max": 20, "default": -2, "sessions": {"u1": 25}})

        u2_neg = self.harness.run_acquire(holder="h1", owner="Agent [u2]")
        self.assertEqual(u2_neg.returncode, 4, u2_neg.stderr.decode())

        u1_first = self.harness.run_acquire(holder="h2", owner="Agent [u1]")
        self.assertEqual(u1_first.returncode, 0, u1_first.stderr.decode())
        u1_second = self.harness.run_acquire(holder="h3", owner="Agent [u1]")
        self.assertEqual(u1_second.returncode, 0, u1_second.stderr.decode())

        self.write_limits({"machine_max": 20, "default": 2, "sessions": {"u1": -5}})
        u1_neg = self.harness.run_acquire(holder="h4", owner="Agent [u1]")
        self.assertEqual(u1_neg.returncode, 4, u1_neg.stderr.decode())

    def test_missing_bracketed_ref_exits_four_even_under_wait(self):
        self.write_limits({"machine_max": 20, "default": 1})

        for owner in (None, "-", "plain owner"):
            args = ["acquire", "--for", "h0", "--wait", "--timeout", "5s"]
            if owner is not None:
                args.extend(["--as", owner])
            res = self.harness.run(*args)
            self.assertEqual(res.returncode, 4, f"owner={owner!r}: {res.stderr.decode()}")
            self.assertIn("[<ref>]", res.stderr.decode())

    def test_machine_max_and_session_limits_are_independent(self):
        # Sum of session limits (2 + 2 = 4) exceeds machine_max (2);
        # session limits are never summed or compared against machine_max.
        self.write_limits({"machine_max": 2, "sessions": {"u1": 2, "u2": 2}})

        u1_first = self.harness.run_acquire(holder="h1", owner="Agent [u1]")
        self.assertEqual(u1_first.returncode, 0, u1_first.stderr.decode())
        u1_second = self.harness.run_acquire(holder="h2", owner="Agent [u1]")
        self.assertEqual(u1_second.returncode, 0, u1_second.stderr.decode())

        # u2 has used 0/2 of its session cap, so machine_max (exit 2) blocks it, not session_cap (exit 4).
        u2_blocked = self.harness.run_acquire(holder="h3", owner="Agent [u2]")
        self.assertEqual(u2_blocked.returncode, 2, u2_blocked.stderr.decode())

    def test_legacy_projects_and_project_default_keys_are_ignored(self):
        other = self.make_second_project("other")
        self.write_limits(
            {
                "machine_max": 20,
                "project_default": 1,
                "projects": {"demo": 0, "other": 0},
                "default": 1,
                "sessions": {"u1": 2},
            }
        )

        u1_first = self.harness.run_acquire(holder="d1", owner="Agent [u1]")
        self.assertEqual(u1_first.returncode, 0, u1_first.stderr.decode())
        u1_second = self.harness.run_acquire(holder="d2", owner="Agent [u1]")
        self.assertEqual(u1_second.returncode, 0, u1_second.stderr.decode())
        u1_third = self.harness.run_acquire(holder="d3", owner="Agent [u1]")
        self.assertEqual(u1_third.returncode, 4, u1_third.stderr.decode())

        other_u2 = other.run_acquire(holder="o1", owner="Agent [u2]")
        self.assertEqual(other_u2.returncode, 0, other_u2.stderr.decode())
        self.assertTrue(self.acquired_id(other_u2).startswith("other-"))
        other_u2_second = other.run_acquire(holder="o2", owner="Agent [u2]")
        self.assertEqual(other_u2_second.returncode, 4, other_u2_second.stderr.decode())

    def test_no_project_cap_or_project_default_symbols_in_bin_or_lib(self):
        offenders = [
            line
            for line in engine_source_lines()
            if "project_cap" in line or "project_default" in line
        ]
        self.assertEqual(offenders, [], "\n".join(offenders))


class SessionWaitAndLiveness(ProjectLimitsTestCase):

    @staticmethod
    def _read_text(path):
        if not os.path.isfile(path):
            return ""
        with open(path, encoding="utf-8") as handle:
            return handle.read()

    def _spawn_waiter(self, holder="waiter", owner="Agent [u1]", timeout="15s"):
        log = os.path.join(self.harness.tmp, f"{holder}.log")
        proc = self.harness.spawn(
            "acquire",
            "--for",
            holder,
            "--as",
            owner,
            "--wait",
            "--timeout",
            timeout,
            log=log,
        )

        def stop():
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=30)

        self.addCleanup(stop)
        return proc, log

    def _wait_until_waiting_logged(self, proc, log, needle="limits.json", timeout=15):
        def ready():
            if proc.poll() is not None and needle not in self._read_text(log):
                raise AssertionError(
                    f"waiter exited early (rc={proc.returncode}) before logging {needle!r}:\n"
                    f"{self._read_text(log)}"
                )
            return needle in self._read_text(log)

        wait_until(
            ready,
            timeout=timeout,
            interval=0.02,
            message=lambda: f"waiter never logged {needle!r}:\n{self._read_text(log)}",
        )

    def test_wait_queues_on_session_cap_and_wakes_on_release(self):
        self.write_limits({"machine_max": 20, "default": 1})
        first = self.harness.run_acquire(holder="h1", owner="Agent [u1]")
        self.assertEqual(first.returncode, 0, first.stderr.decode())
        held = self.harness.acquired_slot_name(first)

        proc, log = self._spawn_waiter(holder="wait-rel", owner="Agent [u1]", timeout="15s")
        self._wait_until_waiting_logged(proc, log, needle="limits.json")
        self.assertIsNone(proc.poll(), self._read_text(log))

        rel = self.harness.run("release", held, "--for", "h1")
        self.assertEqual(rel.returncode, 0, rel.stderr.decode())

        rc = proc.wait(timeout=20)
        self.assertEqual(rc, 0, self._read_text(log))

    def test_wait_wakes_on_session_pause_zero_to_one_with_pinned_mtime(self):
        pinned_mtime = 1700000000.0
        self.write_limits({"machine_max": 20, "sessions": {"u1": 0}}, mtime=pinned_mtime)
        proc, log = self._spawn_waiter(holder="wait-pause", owner="Agent [u1]", timeout="15s")

        self._wait_until_waiting_logged(proc, log, needle="limits.json")
        self.assertIsNone(proc.poll(), self._read_text(log))

        self.write_limits({"machine_max": 20, "sessions": {"u1": 1}}, mtime=pinned_mtime)

        rc = proc.wait(timeout=20)
        self.assertEqual(rc, 0, self._read_text(log))
        self.assertTrue(os.path.isfile(self.harness.lease_path("slot-1")))

    def test_wait_wakes_on_session_cap_increase_with_pinned_mtime(self):
        pinned_mtime = 1700000100.0
        self.write_limits({"machine_max": 20, "default": 1}, mtime=pinned_mtime)
        first = self.harness.run_acquire(holder="h1", owner="Agent [u1]")
        self.assertEqual(first.returncode, 0, first.stderr.decode())

        proc, log = self._spawn_waiter(holder="wait-bump", owner="Agent [u1]", timeout="15s")
        self._wait_until_waiting_logged(proc, log, needle="limits.json")
        self.assertIsNone(proc.poll(), self._read_text(log))

        self.write_limits({"machine_max": 20, "default": 2}, mtime=pinned_mtime)

        rc = proc.wait(timeout=20)
        self.assertEqual(rc, 0, self._read_text(log))

    def test_wait_on_session_cap_times_out_with_exit_three(self):
        self.write_limits({"machine_max": 20, "sessions": {"u1": 0}})
        res = self.harness.run(
            "acquire", "--for", "wait-timeout", "--as", "Agent [u1]", "--wait", "--timeout", "1s"
        )
        self.assertEqual(res.returncode, 3, res.stderr.decode())

    def test_expired_ephemeral_lease_does_not_wedge_session_cap(self):
        since = 1700000000
        self.harness.set_now(since)
        self.write_limits({"machine_max": 20, "default": 1})

        first = self.harness.run("acquire", "--ephemeral", "--for", "eph-1", "--as", "Agent [u1]")
        self.assertEqual(first.returncode, 0, first.stderr.decode())

        self.harness.set_now(since + 901)
        second = self.harness.run_acquire(holder="h2", owner="Agent [u1]")
        self.assertEqual(second.returncode, 0, second.stderr.decode())

    def test_expired_idle_lease_does_not_wedge_session_cap(self):
        since = 1700000000
        self.harness.set_now(since)
        self.write_limits({"machine_max": 20, "idle_limit": 60, "default": 1})

        first = self.harness.run_acquire(holder="idle-1", owner="Agent [u1]")
        self.assertEqual(first.returncode, 0, first.stderr.decode())

        self.harness.set_now(since + 61)
        second = self.harness.run_acquire(holder="h2", owner="Agent [u1]")
        self.assertEqual(second.returncode, 0, second.stderr.decode())

    def test_dead_reservation_does_not_wedge_session_cap(self):
        self.write_limits({"machine_max": 20, "default": 1})
        dead = subprocess.Popen(["true"])
        dead.wait()
        res_path = self.harness.lease_path("slot-1")
        os.makedirs(os.path.dirname(res_path), exist_ok=True)
        with open(res_path, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "holder": "dead-holder",
                    "owner": "Agent [u1]",
                    "project": f"{PROJECT_PREFIX}-slot-1",
                    "since": self.harness.now_epoch(),
                    "key": "",
                    "ssh_port": 0,
                    "setup": "creating",
                    "pid": dead.pid,
                    "boot_id": "boot-res-test",
                },
                handle,
            )

        res = self.harness.run(
            "acquire",
            "--for",
            "live-holder",
            "--as",
            "Agent [u1]",
            port_overrides={"DEV_CONTAINER_BOOT_ID": "boot-res-test"},
        )
        self.assertEqual(res.returncode, 0, res.stderr.decode())


if __name__ == "__main__":
    unittest.main()
