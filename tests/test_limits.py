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


ENV_WITH_IDENTITY = ProjectLimitsTestCase.ENV_WITH_IDENTITY


UUID = "4e0c1a6b-1111-4222-8333-444455556666"
PREFIXED = "local_" + UUID


class SessionLimitIdSpelling(DevContainerTestCase):
    ENV_WITH_IDENTITY = "GIT_USER_NAME=demo-agent" + chr(10) + "GIT_USER_EMAIL=demo-agent@example.com" + chr(10)

    def setUp(self):
        super().setUp()
        self.harness.build_fixture_repo(with_parent_env=self.ENV_WITH_IDENTITY)

    def write_limits(self, limits):
        path = os.path.join(self.harness.state_dir, "limits.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(limits, handle)

    def acquire_ok(self, holder, ref):
        result = self.harness.run_acquire(holder=holder, owner="Agent [" + ref + "]")
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        return self.harness.acquired_slot_name(result)

    def assert_refused(self, holder, ref):
        result = self.harness.run_acquire(holder=holder, owner="Agent [" + ref + "]")
        self.assertEqual(result.returncode, 4, result.stderr.decode())
        return result

    def test_a_limit_under_the_prefixed_id_applies_to_the_bare_id(self):
        self.write_limits({"default": 1, "sessions": {PREFIXED: 2}})

        self.acquire_ok("first", UUID)
        self.acquire_ok("second", UUID)
        self.assert_refused("third", UUID)

    def test_a_limit_under_the_bare_id_applies_to_the_prefixed_id(self):
        self.write_limits({"default": 1, "sessions": {UUID: 2}})

        self.acquire_ok("first", PREFIXED)
        self.acquire_ok("second", PREFIXED)
        self.assert_refused("third", PREFIXED)

    def test_an_unknown_id_still_gets_the_default(self):
        self.write_limits({"default": 1, "sessions": {PREFIXED: 5}})

        self.acquire_ok("first", "local_other")
        self.assert_refused("second", "other")

    def test_slots_held_under_one_spelling_count_for_the_other(self):
        self.write_limits({"default": 1})

        self.acquire_ok("first", PREFIXED)
        self.assert_refused("second", UUID)

    def test_slots_held_under_the_bare_spelling_count_for_the_prefixed_one(self):
        self.write_limits({"default": 1})

        self.acquire_ok("first", UUID)
        self.assert_refused("second", PREFIXED)

    def test_a_session_mixing_spellings_is_counted_once_against_a_raised_limit(self):
        self.write_limits({"default": 1, "sessions": {PREFIXED: 2}})

        self.acquire_ok("first", PREFIXED)
        self.acquire_ok("second", UUID)
        self.assert_refused("third", PREFIXED)
        self.assert_refused("fourth", UUID)

    def test_when_both_spellings_have_a_key_the_exact_one_wins(self):
        self.write_limits({"default": 1, "sessions": {PREFIXED: 3, UUID: 2}})

        self.acquire_ok("first", UUID)
        self.acquire_ok("second", UUID)
        self.assert_refused("third", UUID)

    def test_when_both_spellings_have_a_key_the_exact_one_wins_for_the_prefixed_call(self):
        self.write_limits({"default": 1, "sessions": {PREFIXED: 3, UUID: 2}})

        self.acquire_ok("first", PREFIXED)
        self.acquire_ok("second", PREFIXED)
        self.acquire_ok("third", PREFIXED)
        self.assert_refused("fourth", PREFIXED)

    def test_the_session_id_in_the_environment_matches_a_key_in_the_other_spelling(self):
        self.harness.session_id = UUID
        self.write_limits({"default": 1, "sessions": {PREFIXED: 2}})

        self.acquire_ok("first", "x")
        self.acquire_ok("second", "x")
        self.assert_refused("third", "x")

    def test_zero_session_limit_pauses_only_that_session(self):
        self.write_limits({"default": 2, "sessions": {UUID: 0}})

        self.assert_refused("first", UUID)
        self.assert_refused("second", PREFIXED)
        self.acquire_ok("other", "other-session")

    def test_negative_session_limit_clamps_to_zero_and_refuses(self):
        self.write_limits({"default": 2, "sessions": {PREFIXED: -4}})

        self.assert_refused("first", UUID)
        self.assert_refused("second", PREFIXED)

    def test_negative_default_limit_clamps_to_zero_and_refuses(self):
        self.write_limits({"default": -2})

        self.assert_refused("first", UUID)

    def test_session_limit_above_twenty_clamps_to_twenty_and_allows_acquires(self):
        self.write_limits({"default": 1, "sessions": {UUID: 25}})

        self.acquire_ok("first", UUID)
        self.acquire_ok("second", PREFIXED)


class SessionSlotLimit(DevContainerTestCase):
    ENV_WITH_IDENTITY = ENV_WITH_IDENTITY

    def write_limits(self, limits):
        path = os.path.join(self.harness.state_dir, "limits.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(limits, handle)

    def acquire_ok(self, holder, owner):
        result = self.harness.run_acquire(holder=holder, owner=owner)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        return self.harness.acquired_slot_name(result)

    def test_no_limits_file_means_no_limit(self):
        self.harness.build_fixture_repo(with_parent_env=self.ENV_WITH_IDENTITY)

        self.acquire_ok("first", "Agent [aaa]")
        self.acquire_ok("second", "Agent [aaa]")

    def test_limits_file_without_as_is_refused(self):
        self.harness.build_fixture_repo(with_parent_env=self.ENV_WITH_IDENTITY)
        self.write_limits({"default": 1})

        result = self.harness.run_acquire()
        self.assertEqual(result.returncode, 4, result.stderr.decode())
        self.assertIn("--as", result.stderr.decode())
        self.assertIn("[<ref>]", result.stderr.decode())

        result = self.harness.run_acquire(owner="no bracketed ref")
        self.assertEqual(result.returncode, 4, result.stderr.decode())

    def test_session_at_default_limit_is_refused_naming_the_held_slot(self):
        self.harness.build_fixture_repo(with_parent_env=self.ENV_WITH_IDENTITY)
        self.write_limits({"default": 1})
        held = self.acquire_ok("first", "Agent [aaa]")

        result = self.harness.run_acquire(holder="second", owner="Agent [aaa]")
        self.assertEqual(result.returncode, 4, result.stderr.decode())
        self.assertIn(held, result.stderr.decode())
        self.assertIn("limits.json", result.stderr.decode())

    def test_wait_does_not_wait_out_the_limit(self):
        self.harness.build_fixture_repo(with_parent_env=self.ENV_WITH_IDENTITY)
        self.write_limits({"default": 1})
        held = self.acquire_ok("first", "Agent [aaa]")

        non_wait = self.harness.run_acquire(holder="second-nowait", owner="Agent [aaa]")
        self.assertEqual(non_wait.returncode, 4, non_wait.stderr.decode())

        def _read(path):
            if not os.path.isfile(path):
                return ""
            with open(path, encoding="utf-8") as handle:
                return handle.read()

        log_release = os.path.join(self.harness.tmp, "wait-release.log")
        waiter_release = self.harness.spawn(
            "acquire", "--for", "second-wait", "--as", "Agent [aaa]", "--wait", "--timeout", "15s",
            log=log_release,
        )
        self.addCleanup(
            lambda: waiter_release.poll() is None and (waiter_release.kill(), waiter_release.wait(timeout=30))
        )

        wait_until(
            lambda: "limits.json" in _read(log_release),
            timeout=15,
            interval=0.02,
            message=lambda: f"waiter never logged waiting on session cap:\n{_read(log_release)}",
        )
        self.assertIsNone(waiter_release.poll(), _read(log_release))

        rel = self.harness.run("release", held, "--for", "first")
        self.assertEqual(rel.returncode, 0, rel.stderr.decode())
        self.assertEqual(waiter_release.wait(timeout=20), 0, _read(log_release))

        log_bump = os.path.join(self.harness.tmp, "wait-bump.log")
        waiter_bump = self.harness.spawn(
            "acquire", "--for", "third-wait", "--as", "Agent [aaa]", "--wait", "--timeout", "15s",
            log=log_bump,
        )
        self.addCleanup(
            lambda: waiter_bump.poll() is None and (waiter_bump.kill(), waiter_bump.wait(timeout=30))
        )

        wait_until(
            lambda: "limits.json" in _read(log_bump),
            timeout=15,
            interval=0.02,
            message=lambda: f"second waiter never logged waiting on session cap:\n{_read(log_bump)}",
        )
        self.write_limits({"default": 2})
        self.assertEqual(waiter_bump.wait(timeout=20), 0, _read(log_bump))

    def test_per_session_override_lets_a_second_slot_through(self):
        self.harness.build_fixture_repo(with_parent_env=self.ENV_WITH_IDENTITY)
        self.write_limits({"default": 1, "sessions": {"aaa": 2}})

        first = self.acquire_ok("first", "Agent [aaa]")
        second = self.acquire_ok("second", "Agent [aaa]")
        self.assertNotEqual(first, second)

        result = self.harness.run_acquire(holder="third", owner="Agent [aaa]")
        self.assertEqual(result.returncode, 4, result.stderr.decode())

    def test_a_lease_owned_by_another_ref_does_not_count(self):
        self.harness.build_fixture_repo(with_parent_env=self.ENV_WITH_IDENTITY)
        self.write_limits({"default": 1})
        self.acquire_ok("first", "Other [bbb]")

        self.acquire_ok("second", "Agent [aaa]")

    def test_session_id_in_the_environment_keys_the_limit_whatever_ref_as_carries(self):
        self.harness.build_fixture_repo(with_parent_env=self.ENV_WITH_IDENTITY)
        self.harness.session_id = "local_x"
        self.write_limits({"default": 1})
        self.acquire_ok("first", "Agent [aaa]")

        result = self.harness.run_acquire(holder="second", owner="Agent [bbb]")
        self.assertEqual(result.returncode, 4, result.stderr.decode())
        self.assertIn("local_x", result.stderr.decode())

    def test_session_id_in_the_environment_selects_the_per_session_override(self):
        self.harness.build_fixture_repo(with_parent_env=self.ENV_WITH_IDENTITY)
        self.harness.session_id = "local_x"
        self.write_limits({"default": 1, "sessions": {"local_x": 2}})

        first = self.acquire_ok("first", "Agent [aaa]")
        second = self.acquire_ok("second", "Agent [aaa]")
        self.assertNotEqual(first, second)

    def test_session_id_in_the_environment_replaces_a_missing_as(self):
        self.harness.build_fixture_repo(with_parent_env=self.ENV_WITH_IDENTITY)
        self.harness.session_id = "local_x"
        self.write_limits({"default": 1})

        result = self.harness.run_acquire(holder="first")
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        name = self.harness.acquired_slot_name(result)
        with open(self.harness.lease_path(name), encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["owner"], "session [local_x]")

        result = self.harness.run_acquire(holder="second")
        self.assertEqual(result.returncode, 4, result.stderr.decode())


class MachineCapFromLimitsFile(DevContainerTestCase):
    ENV = "GIT_USER_NAME=demo-agent\nGIT_USER_EMAIL=a@example.com\n"
    OWNER = "Agent [aaa]"
    CEILING = 20

    def _write_limits(self, limits):
        path = os.path.join(self.harness.state_dir, "limits.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(limits, handle)

    def _foreign_lease(self, project):
        path = os.path.join(self.harness.state_dir, "leases", f"{project}.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "holder": "someone-else",
                    "project": project,
                    "since": int(time.time()) - 60,
                    "ttl_seconds": 14400,
                    "key": "/dev/null",
                    "ssh_port": 39229,
                    "owner": "-",
                },
                handle,
            )
        return path

    def _fill(self, count):
        return [self._foreign_lease(f"othproj{i}-slot-{i}") for i in range(1, count + 1)]

    def _lease_names(self):
        return sorted(os.listdir(os.path.join(self.harness.state_dir, "leases")))

    def _acquire(self, holder="newcomer", *extra, **kwargs):
        return self.harness.run(
            "acquire", "--for", holder, "--as", self.OWNER, *extra, **kwargs
        )

    def _identity_repo(self):
        self.harness.build_fixture_repo(with_parent_env=self.ENV)

    def _assert_refused(self, result, count, cap):
        out = "STDOUT:\n" + result.stdout.decode() + "\nSTDERR:\n" + result.stderr.decode()
        self.assertNotEqual(result.returncode, 0, out)
        self.assertIn(f"{count}/{cap}", result.stderr.decode(), out)


    def test_two_leases_of_any_prefix_at_machine_max_2_refuse_naming_the_count_and_limits_file(self):
        self._identity_repo()
        self._write_limits({"machine_max": 2})
        self._foreign_lease("alpha-slot-1")
        self._foreign_lease("beta-slot-7")

        result = self._acquire()

        self._assert_refused(result, 2, 2)
        self.assertIn("limits.json", result.stderr.decode())
        self.assertEqual(self._lease_names(), ["alpha-slot-1.json", "beta-slot-7.json"])
        self.assertFalse(os.path.isdir(self.harness.slot_dir("slot-1")))


    def test_with_no_limits_file_the_cap_is_20(self):
        self._identity_repo()
        leases = self._fill(self.CEILING)

        result = self.harness.run("acquire", "--for", "newcomer")

        self._assert_refused(result, 20, 20)
        os.remove(leases[0])
        result = self.harness.run("acquire", "--for", "newcomer")
        self.assertEqual(result.returncode, 0, result.stderr.decode())

    def test_with_a_limits_file_lacking_machine_max_the_cap_is_20(self):
        self._identity_repo()
        self._write_limits({"default": 5})
        leases = self._fill(self.CEILING)

        result = self._acquire()

        self._assert_refused(result, 20, 20)
        os.remove(leases[0])
        result = self._acquire()
        self.assertEqual(result.returncode, 0, result.stderr.decode())


    def test_machine_max_negative_is_clamped_to_0(self):
        self._identity_repo()
        self._write_limits({"machine_max": -1})
        self._foreign_lease("alpha-slot-1")

        result = self._acquire()

        self._assert_refused(result, 1, 0)

    def test_machine_max_25_is_clamped_to_20(self):
        self._identity_repo()
        self._write_limits({"machine_max": 25})
        self._fill(self.CEILING)

        result = self._acquire()

        self._assert_refused(result, 20, 20)

    def test_a_non_number_machine_max_is_20(self):
        self._identity_repo()
        self._write_limits({"machine_max": "lots"})
        self._fill(self.CEILING)

        result = self._acquire()

        self._assert_refused(result, 20, 20)

    def _assert_one_line_warning(self, value):
        self._identity_repo()
        self._write_limits({"machine_max": value})

        result = self._acquire()

        stderr = result.stderr.decode()
        self.assertEqual(result.returncode, 0, stderr)
        warned = [line for line in stderr.splitlines() if "machine_max" in line]
        self.assertEqual(len(warned), 1, f"expected a one-line warning for {value!r}:\n{stderr}")

    def test_machine_max_0_pauses_without_warning(self):
        self._identity_repo()
        self._write_limits({"machine_max": 0})

        result = self._acquire()

        self._assert_refused(result, 0, 0)
        warned = [line for line in result.stderr.decode().splitlines() if line.startswith("warning:")]
        self.assertEqual(warned, [], result.stderr.decode())

    def test_machine_max_25_warns_in_one_line(self):
        self._assert_one_line_warning(25)

    def test_a_non_number_machine_max_warns_in_one_line(self):
        self._assert_one_line_warning("lots")

    def _assert_one_warning_on_a_refused_acquire(self, value, leases):
        self._identity_repo()
        self._write_limits({"machine_max": value})
        self._fill(leases)

        result = self._acquire()

        stderr = result.stderr.decode()
        self.assertNotEqual(result.returncode, 0, stderr)
        warned = [line for line in stderr.splitlines() if line.startswith("warning:") and "machine_max in" in line]
        self.assertEqual(len(warned), 1, f"expected one warning for {value!r} on a refusal: {stderr}")

    def test_machine_max_25_warns_once_on_a_refused_acquire(self):
        self._assert_one_warning_on_a_refused_acquire(25, self.CEILING)

    def test_a_non_number_machine_max_warns_once_on_a_refused_acquire(self):
        self._assert_one_warning_on_a_refused_acquire("lots", self.CEILING)

    def test_machine_max_negative_warns_once_on_a_refused_acquire(self):
        self._assert_one_warning_on_a_refused_acquire(-1, 1)

    def test_an_in_range_machine_max_does_not_warn(self):
        self._identity_repo()
        self._write_limits({"machine_max": 2})

        result = self._acquire()

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertNotIn("machine_max", result.stderr.decode())


    def test_a_leased_slot_whose_containers_are_stopped_counts_against_the_cap(self):
        self._identity_repo()
        self._write_limits({"machine_max": 1})
        first = self._acquire("first")
        self.assertEqual(first.returncode, 0, first.stderr.decode())
        slot = self.harness.acquired_slot_name(first)
        self.harness.mark_stopped(slot)

        result = self._acquire("second")

        self._assert_refused(result, 1, 1)
        self.assertTrue(os.path.isfile(self.harness.lease_path(slot)))
        self.assertFalse(os.path.isdir(self.harness.slot_dir("slot-2")))


    def test_under_the_cap_a_new_slot_is_created(self):
        self._identity_repo()
        self._write_limits({"machine_max": 2})
        self._foreign_lease("alpha-slot-1")

        result = self._acquire()

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(self.harness.acquired_slot_name(result), "slot-2", "another project's live lease holds number 1")
        self.assertTrue(os.path.isfile(self.harness.lease_path("slot-2")))


    def test_a_new_directory_index_is_bounded_by_20_not_by_machine_max(self):
        self._identity_repo()
        self._write_limits({"machine_max": 3})
        for i in (1, 2, 3):
            self.harness.make_bare_slot(f"slot-{i}", built=False)

        result = self._acquire()

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(self.harness.acquired_slot_name(result), "slot-4")


    def test_wait_at_the_cap_waits_and_proceeds_once_a_lease_is_released(self):
        self._identity_repo()
        self._write_limits({"machine_max": 1})
        lease = self._foreign_lease("alpha-slot-1")
        log = os.path.join(self.harness.tmp, "waiter.log")
        proc = self.harness.spawn(
            "acquire", "--for", "waiter", "--as", self.OWNER, "--wait", log=log
        )

        def stop():
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=30)
        self.addCleanup(stop)

        def read():
            with open(log, encoding="utf-8") as handle:
                return handle.read()

        def read_safe():
            return read() if os.path.isfile(log) else ""

        def logged_waiting():
            if proc.poll() is not None and "1/1" not in read_safe():
                raise AssertionError("never reported waiting at 1/1:\n" + read_safe())
            return "1/1" in read_safe()

        wait_until(
            logged_waiting,
            timeout=60.0,
            interval=0.02,
            message=lambda: "never reported waiting at 1/1:\n" + read_safe(),
        )
        self.assertIn("limits.json", read())
        self.assertIsNone(proc.poll(), "the waiter gave up instead of waiting:\n" + read())

        os.remove(lease)

        wait_until(
            lambda: proc.poll() is not None,
            timeout=90.0,
            interval=0.02,
            message=lambda: "the waiter never took the freed capacity:\n" + read_safe(),
        )
        self.assertEqual(proc.returncode, 0, read())
        self.assertIn("leased to waiter", read())


    def test_slot_max_in_the_environment_does_not_lower_the_cap(self):
        self._identity_repo()
        first = self._acquire("first")
        self.assertEqual(first.returncode, 0, first.stderr.decode())

        second = self._acquire("second", port_overrides={"SLOT_MAX": "1"})

        self.assertEqual(second.returncode, 0, second.stderr.decode())
        self.assertEqual(self.harness.acquired_slot_name(second), "slot-2")

    def test_slot_max_in_the_environment_does_not_raise_the_cap(self):
        self._identity_repo()
        self._write_limits({"machine_max": 2})
        self._foreign_lease("alpha-slot-1")
        self._foreign_lease("beta-slot-7")

        result = self._acquire(port_overrides={"SLOT_MAX": "100"})

        self._assert_refused(result, 2, 2)

    def _install_logging_sleep_stub(self):
        sleep_log = os.path.join(self.harness.tmp, "sleep-calls.log")
        real_sleep = shutil.which("sleep")
        self.assertTrue(real_sleep, "sleep binary not found on PATH")
        self.harness._write_stub(
            "sleep",
            f'#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "{sleep_log}"\nexec "{real_sleep}" 0.01\n',
        )
        return sleep_log

    @staticmethod
    def _read_lines(path):
        if not os.path.isfile(path):
            return []
        with open(path, encoding="utf-8") as handle:
            return [line.strip() for line in handle if line.strip()]

    def test_wait_loop_respects_dev_container_wait_interval_and_defaults_to_one(self):
        self._identity_repo()
        self._write_limits({"machine_max": 1})
        sleep_log = self._install_logging_sleep_stub()

        # Subcase A: custom DEV_CONTAINER_WAIT_INTERVAL is forwarded to sleep in --wait loop.
        lease = self._foreign_lease("alpha-slot-1")
        log_a = os.path.join(self.harness.tmp, "wait-interval-custom.log")
        proc_a = self.harness.spawn(
            "acquire",
            "--for",
            "waiter-custom",
            "--as",
            self.OWNER,
            "--wait",
            port_overrides={"DEV_CONTAINER_WAIT_INTERVAL": "0.123"},
            log=log_a,
        )
        self.addCleanup(lambda: proc_a.poll() is None and proc_a.kill())

        wait_until(
            lambda: len(self._read_lines(sleep_log)) >= 1,
            timeout=15.0,
            interval=0.02,
            message="acquire --wait never invoked sleep",
        )
        os.remove(lease)
        self.assertEqual(proc_a.wait(timeout=30), 0)
        custom_calls = self._read_lines(sleep_log)
        self.assertIn(
            "0.123",
            custom_calls,
            f"expected --wait loop to sleep for DEV_CONTAINER_WAIT_INTERVAL=0.123, got {custom_calls}",
        )
        self.assertNotIn(
            "1",
            custom_calls,
            f"--wait loop ignored DEV_CONTAINER_WAIT_INTERVAL and slept 1: {custom_calls}",
        )

        # Subcase B: unset DEV_CONTAINER_WAIT_INTERVAL preserves production default of 1.
        os.remove(sleep_log)
        self._write_limits({"machine_max": 1})
        env_unset = self.harness._env(None)
        env_unset.pop("DEV_CONTAINER_WAIT_INTERVAL", None)
        proc_b = subprocess.Popen(
            [
                "bash",
                self.harness.script,
                "acquire",
                "--for",
                "waiter-default",
                "--as",
                self.OWNER,
                "--wait",
            ],
            cwd=self.harness.tmp,
            env=env_unset,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.addCleanup(lambda: proc_b.poll() is None and proc_b.kill())

        wait_until(
            lambda: len(self._read_lines(sleep_log)) >= 1,
            timeout=15.0,
            interval=0.02,
            message="acquire --wait (default interval) never invoked sleep",
        )
        self._write_limits({"machine_max": 2})
        _, err_b = proc_b.communicate(timeout=30)
        self.assertEqual(proc_b.returncode, 0, err_b.decode())
        default_calls = self._read_lines(sleep_log)
        self.assertIn(
            "1",
            default_calls,
            f"expected --wait loop default interval to be 1 when unset, got {default_calls}",
        )

    def test_lock_acquire_respects_dev_container_wait_interval_and_defaults_to_one(self):
        self._identity_repo()
        sleep_log = self._install_logging_sleep_stub()
        lock = os.path.join(self.harness.state_dir, "lock")
        self.addCleanup(lambda: os.path.isdir(lock) and os.rmdir(lock))

        # Subcase A: custom DEV_CONTAINER_WAIT_INTERVAL is used in lock_acquire retries.
        os.mkdir(lock)
        proc_a = self.harness.spawn(
            "acquire",
            "--for",
            "lock-custom",
            "--as",
            self.OWNER,
            port_overrides={"DEV_CONTAINER_WAIT_INTERVAL": "0.07"},
        )
        self.addCleanup(lambda: proc_a.poll() is None and proc_a.kill())

        wait_until(
            lambda: len(self._read_lines(sleep_log)) >= 1,
            timeout=15.0,
            interval=0.02,
            message="lock_acquire never invoked sleep while lock was held",
        )
        os.rmdir(lock)
        _, err_a = proc_a.communicate(timeout=30)
        self.assertEqual(proc_a.returncode, 0, err_a.decode())
        custom_calls = self._read_lines(sleep_log)
        self.assertIn(
            "0.07",
            custom_calls,
            f"expected lock_acquire to sleep for DEV_CONTAINER_WAIT_INTERVAL=0.07, got {custom_calls}",
        )
        self.assertNotIn(
            "1",
            custom_calls,
            f"lock_acquire ignored DEV_CONTAINER_WAIT_INTERVAL and slept 1: {custom_calls}",
        )

        # Subcase B: unset DEV_CONTAINER_WAIT_INTERVAL preserves production default of 1.
        os.remove(sleep_log)
        os.mkdir(lock)
        env_unset = self.harness._env(None)
        env_unset.pop("DEV_CONTAINER_WAIT_INTERVAL", None)
        proc_b = subprocess.Popen(
            [
                "bash",
                self.harness.script,
                "acquire",
                "--for",
                "lock-default",
                "--as",
                self.OWNER,
            ],
            cwd=self.harness.tmp,
            env=env_unset,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.addCleanup(lambda: proc_b.poll() is None and proc_b.kill())

        wait_until(
            lambda: len(self._read_lines(sleep_log)) >= 1,
            timeout=15.0,
            interval=0.02,
            message="lock_acquire (default interval) never invoked sleep while lock was held",
        )
        os.rmdir(lock)
        _, err_b = proc_b.communicate(timeout=30)
        self.assertEqual(proc_b.returncode, 0, err_b.decode())
        default_calls = self._read_lines(sleep_log)
        self.assertIn(
            "1",
            default_calls,
            f"expected lock_acquire default interval to be 1 when unset, got {default_calls}",
        )


if __name__ == "__main__":
    unittest.main()
