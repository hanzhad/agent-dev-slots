import json
import os
import re
import shutil
import subprocess
import sys
import time
import unittest

if os.path.dirname(__file__) not in sys.path:
    sys.path.insert(0, os.path.dirname(__file__))

from harness import (
    DevContainerHarness,
    DevContainerTestCase,
    PROJECT_PREFIX,
    project_env_text,
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

    def write_limits(self, limits):
        path = os.path.join(self.harness.state_dir, "limits.json")
        prev_mtime = os.stat(path).st_mtime if os.path.exists(path) else None
        tmp_path = path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(limits, handle)
        if prev_mtime is not None:
            bumped = max(time.time(), prev_mtime + 2.0)
            os.utime(tmp_path, (bumped, bumped))
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


class ProjectLevelSlotLimits(ProjectLimitsTestCase):

    def test_project_default_allows_same_session_up_to_limit_and_refuses_third(self):
        self.write_limits({"machine_max": 20, "project_default": 2})

        first = self.harness.run_acquire(holder="h1", owner="Agent [u1]")
        self.assertEqual(first.returncode, 0, first.stderr.decode())

        second = self.harness.run_acquire(holder="h2", owner="Agent [u1]")
        self.assertEqual(second.returncode, 0, second.stderr.decode())
        self.assertNotEqual(
            self.harness.acquired_slot_name(first),
            self.harness.acquired_slot_name(second),
        )

        third = self.harness.run_acquire(holder="h3", owner="Agent [u1]")
        stderr = third.stderr.decode()
        self.assertEqual(third.returncode, 2, stderr)
        self.assertIn("demo", stderr)
        self.assertIn("2", stderr)
        self.assertIn("limits.json", stderr)

    def test_per_project_override_caps_demo_while_other_uses_project_default(self):
        other = self.make_second_project("other")
        self.write_limits(
            {"machine_max": 20, "project_default": 4, "projects": {"demo": 1}}
        )

        demo_first = self.harness.run_acquire(holder="d1", owner="Agent [u1]")
        self.assertEqual(demo_first.returncode, 0, demo_first.stderr.decode())

        demo_second = self.harness.run_acquire(holder="d2", owner="Agent [u2]")
        demo_err = demo_second.stderr.decode()
        self.assertEqual(demo_second.returncode, 2, demo_err)
        self.assertIn("demo", demo_err)
        self.assertIn("limits.json", demo_err)

        other_first = other.run_acquire(holder="o1", owner="Agent [u1]")
        self.assertEqual(other_first.returncode, 0, other_first.stderr.decode())
        self.assertTrue(self.acquired_id(other_first).startswith("other-"))

        other_second = other.run_acquire(holder="o2", owner="Agent [u1]")
        self.assertEqual(other_second.returncode, 0, other_second.stderr.decode())
        self.assertTrue(self.acquired_id(other_second).startswith("other-"))

    def test_per_project_pause_zero_refuses_demo_while_other_succeeds(self):
        other = self.make_second_project("other")
        self.write_limits({"machine_max": 20, "projects": {"demo": 0}})

        demo_res = self.harness.run_acquire(holder="d1", owner="Agent [u1]")
        demo_err = demo_res.stderr.decode()
        self.assertEqual(demo_res.returncode, 2, demo_err)
        self.assertIn("demo", demo_err)
        self.assertIn("limits.json", demo_err)

        other_res = other.run_acquire(holder="o1", owner="Agent [u1]")
        self.assertEqual(other_res.returncode, 0, other_res.stderr.decode())
        self.assertTrue(self.acquired_id(other_res).startswith("other-"))

    def test_as_bracketed_ref_still_required_with_project_limits_and_shared_across_sessions(self):
        self.write_limits({"machine_max": 20, "project_default": 1})

        missing_as = self.harness.run_acquire(holder="h0")
        self.assertNotEqual(missing_as.returncode, 0, missing_as.stdout.decode())
        self.assertIn("--as", missing_as.stderr.decode())

        dash_owner = self.harness.run_acquire(holder="h0", owner="-")
        self.assertNotEqual(dash_owner.returncode, 0, dash_owner.stdout.decode())
        self.assertIn("--as", dash_owner.stderr.decode())

        first = self.harness.run_acquire(holder="h1", owner="Agent [u1]")
        self.assertEqual(first.returncode, 0, first.stderr.decode())

        second = self.harness.run_acquire(holder="h2", owner="Other [u2]")
        self.assertEqual(second.returncode, 2, second.stderr.decode())

    def test_explicit_sessions_entry_enforced_while_legacy_default_session_key_is_ignored(self):
        self.write_limits(
            {
                "default": 1,
                "machine_max": 20,
                "project_default": 3,
                "sessions": {"u1": 1},
            }
        )

        u1_first = self.harness.run_acquire(holder="h1", owner="Agent [u1]")
        self.assertEqual(u1_first.returncode, 0, u1_first.stderr.decode())

        u1_second = self.harness.run_acquire(holder="h2", owner="Agent [u1]")
        self.assertEqual(u1_second.returncode, 4, u1_second.stderr.decode())

        u2_first = self.harness.run_acquire(holder="h3", owner="Agent [u2]")
        self.assertEqual(u2_first.returncode, 0, u2_first.stderr.decode())
        u2_second = self.harness.run_acquire(holder="h4", owner="Agent [u2]")
        self.assertEqual(u2_second.returncode, 0, u2_second.stderr.decode())

        u2_third = self.harness.run_acquire(holder="h5", owner="Agent [u2]")
        self.assertEqual(u2_third.returncode, 2, u2_third.stderr.decode())


class ReservationAndWaitWakeup(ProjectLimitsTestCase):

    @staticmethod
    def _read_text(path):
        if not os.path.isfile(path):
            return ""
        with open(path, encoding="utf-8") as handle:
            return handle.read()

    def _spawn_waiter(self, holder="waiter", timeout="15s"):
        log = os.path.join(self.harness.tmp, f"{holder}.log")
        proc = self.harness.spawn(
            "acquire",
            "--for",
            holder,
            "--as",
            "Agent [u1]",
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
        deadline = time.monotonic() + timeout
        while needle not in self._read_text(log):
            if proc.poll() is not None:
                raise AssertionError(
                    f"waiter exited early (rc={proc.returncode}) before logging {needle!r}:\n"
                    f"{self._read_text(log)}"
                )
            if time.monotonic() > deadline:
                raise AssertionError(
                    f"waiter never logged {needle!r}:\n{self._read_text(log)}"
                )
            time.sleep(0.1)

    def test_live_reservation_file_with_repo_root_counts_against_project_limit(self):
        self.write_limits({"machine_max": 20, "projects": {"demo": 1}})
        demo_root = os.path.realpath(self.harness.fixture_repo)
        res_path = self.harness.lease_path("slot-1")
        os.makedirs(os.path.dirname(res_path), exist_ok=True)
        with open(res_path, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "holder": "inflight-holder",
                    "owner": "Agent [u1]",
                    "project": f"{PROJECT_PREFIX}-slot-1",
                    "since": int(time.time()),
                    "key": "",
                    "ssh_port": 0,
                    "setup": "creating",
                    "pid": os.getpid(),
                    "boot_id": "boot-res-test",
                    "repo_root": demo_root,
                },
                handle,
            )

        result = self.harness.run(
            "acquire",
            "--for",
            "concurrent-holder",
            "--as",
            "Agent [u2]",
            port_overrides={"DEV_CONTAINER_BOOT_ID": "boot-res-test"},
        )

        stderr = result.stderr.decode()
        self.assertEqual(result.returncode, 2, stderr)
        self.assertIn("demo", stderr)

    def test_write_reservation_records_repo_root_and_blocks_concurrent_same_project_acquire(self):
        self.write_limits({"machine_max": 20, "projects": {"demo": 1}})
        self.harness.hold_compose_up("slot-1")
        self.addCleanup(self.harness.let_compose_up_go, "slot-1")

        first_log = os.path.join(self.harness.tmp, "inflight-1.log")
        first = self.harness.spawn(
            "acquire", "--for", "inflight-1", "--as", "Agent [u1]", log=first_log
        )

        def cleanup_first():
            self.harness.let_compose_up_go("slot-1")
            if first.poll() is None:
                first.kill()
            first.wait(timeout=30)

        self.addCleanup(cleanup_first)
        self.harness.wait_until_compose_up_held("slot-1", timeout=30)

        res_path = self.harness.lease_path("slot-1")
        self.assertTrue(os.path.isfile(res_path), "reservation file missing during create_slot")
        with open(res_path, encoding="utf-8") as handle:
            res_data = json.load(handle)
        self.assertIn(res_data.get("setup"), ("create", "creating"))
        self.assertEqual(
            res_data.get("repo_root"),
            os.path.realpath(self.harness.fixture_repo),
        )

        concurrent = self.harness.run_acquire(holder="inflight-2", owner="Agent [u2]")
        self.assertEqual(concurrent.returncode, 2, concurrent.stderr.decode())

        self.harness.let_compose_up_go("slot-1")
        rc = first.wait(timeout=30)
        self.assertEqual(rc, 0, self._read_text(first_log))

    def test_wait_wakes_and_succeeds_when_machine_max_updated_from_zero_to_one(self):
        self.write_limits({"machine_max": 0})
        proc, log = self._spawn_waiter(holder="wait-global", timeout="15s")

        self._wait_until_waiting_logged(proc, log, needle="limits.json")
        self.assertNotIn("using 1", self._read_text(log))
        self.assertIsNone(proc.poll(), self._read_text(log))

        self.write_limits({"machine_max": 1})

        rc = proc.wait(timeout=20)
        self.assertEqual(rc, 0, self._read_text(log))
        self.assertTrue(os.path.isfile(self.harness.lease_path("slot-1")))

    def test_wait_wakes_and_succeeds_when_project_pause_updated_from_zero_to_one(self):
        self.write_limits({"machine_max": 20, "projects": {"demo": 0}})
        proc, log = self._spawn_waiter(holder="wait-proj", timeout="15s")

        self._wait_until_waiting_logged(proc, log, needle="limits.json")
        self.assertIsNone(proc.poll(), self._read_text(log))

        self.write_limits({"machine_max": 20, "projects": {"demo": 1}})

        rc = proc.wait(timeout=20)
        self.assertEqual(rc, 0, self._read_text(log))
        self.assertTrue(os.path.isfile(self.harness.lease_path("slot-1")))


if __name__ == "__main__":
    unittest.main()
