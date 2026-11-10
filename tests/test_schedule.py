import json
import os
import shutil
import signal
import subprocess
import time
import unittest

import test_pool
from harness import LIB_REL, PROJECT_PREFIX, REPO_ROOT, SCRIPT_REL


class BootEnv:
    """The harness environment plus a boot id and a one-second tick."""

    boot_id = "boot-a"

    def _env(self, port_overrides):
        env = super()._env(port_overrides)
        env["DEV_CONTAINER_BOOT_ID"] = self.boot_id
        env["DEV_CONTAINER_SCHEDULE_TICK"] = "1"
        return env


class ScheduleHarness(BootEnv, test_pool.PoolHarness):
    pass


class SecondScheduleHarness(BootEnv, test_pool.SecondHarness):
    pass


class ScheduleCase(test_pool.PoolTestCase):
    harness_class = ScheduleHarness

    def setUp(self):
        super().setUp()
        self.state = self.harness.state_dir
        self.sched = os.path.join(self.state, "schedule")
        # An engine copy outside any git checkout: `schedule` and `reap --all`
        # must not need a repository root to start.
        self.plain = os.path.join(self.harness.tmp, "plain-engine")
        os.makedirs(os.path.join(self.plain, "bin"))
        shutil.copyfile(
            os.path.join(REPO_ROOT, SCRIPT_REL), os.path.join(self.plain, SCRIPT_REL)
        )
        shutil.copytree(os.path.join(REPO_ROOT, LIB_REL), os.path.join(self.plain, LIB_REL))

    def env(self, fast=1000, slow=1000, **extra):
        env = self.harness._env(None)
        env.pop("DEV_CONTAINER_CALLER_PWD", None)
        env["DEV_CONTAINER_PROJECTS_ROOT"] = os.path.dirname(self.harness.tmp)
        env["DEV_CONTAINER_SCHEDULE_FAST"] = str(fast)
        env["DEV_CONTAINER_SCHEDULE_SLOW"] = str(slow)
        env.update(extra)
        return env

    def engine(self, *args, fast=1000, slow=1000, timeout=60, **extra):
        return subprocess.run(
            ["bash", os.path.join(self.plain, SCRIPT_REL), *args],
            cwd=self.harness.tmp,
            env=self.env(fast, slow, **extra),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )

    def output(self, result):
        return result.stdout.decode() + result.stderr.decode()

    def stamp_path(self, job):
        return os.path.join(self.sched, job + ".last")

    def log_path(self, job):
        return os.path.join(self.sched, job + ".log")

    def write_stamp(self, job, epoch):
        os.makedirs(self.sched, exist_ok=True)
        with open(self.stamp_path(job), "w", encoding="utf-8") as handle:
            handle.write(f"{int(epoch)}\n")

    def read_stamp(self, job):
        self.assertTrue(os.path.isfile(self.stamp_path(job)), f"no {job}.last stamp")
        with open(self.stamp_path(job), encoding="utf-8") as handle:
            return int(handle.read().strip())

    def read_log(self, job):
        self.assertTrue(os.path.isfile(self.log_path(job)), f"no {job}.log job log")
        with open(self.log_path(job), encoding="utf-8") as handle:
            return handle.read()

    def slow_is_fresh(self):
        self.write_stamp("slow", time.time())

    def wait_for(self, path, timeout=20):
        deadline = time.monotonic() + timeout
        while not os.path.isfile(path):
            if time.monotonic() > deadline:
                raise AssertionError(f"{path} never appeared")
            time.sleep(0.1)

    def write_lease(self, name, repo_root, age_seconds=60):
        path = os.path.join(self.state, "leases", f"{PROJECT_PREFIX}-{name}.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "holder": "task",
                    "owner": "-",
                    "project": f"{PROJECT_PREFIX}-{name}",
                    "since": int(time.time()) - age_seconds,
                    "key": "/dev/null",
                    "ssh_port": test_pool.slot_port(1, 0),
                    "repo_root": repo_root,
                },
                handle,
            )

    def git_calls_naming(self, root):
        wanted = f"-C {root} remote get-url origin"
        try:
            with open(self.harness.git_call_log, encoding="utf-8") as handle:
                return [ln for ln in handle.read().splitlines() if ln.endswith(wanted)]
        except FileNotFoundError:
            return []


class ScheduleRuns(ScheduleCase):

    def test_schedule_starts_with_no_repository_root(self):
        result = self.engine("schedule", "run", "--once")

        self.assertEqual(result.returncode, 0, self.output(result))
        self.assertTrue(os.path.isdir(self.sched), self.output(result))

    def test_help_prints_usage_for_schedule_and_reap_all(self):
        for args, expected in (
            (("schedule", "--help"), "schedule run [--once]"),
            (("schedule", "run", "--help"), "schedule run [--once]"),
            (("reap", "--all", "--help"), "reap --all"),
        ):
            with self.subTest(args=args):
                result = self.engine(*args)
                self.assertEqual(result.returncode, 0, self.output(result))
                self.assertIn(expected, result.stdout.decode())

    def test_the_table_is_a_visible_file_with_the_two_default_jobs(self):
        self.engine("schedule", "run", "--once")

        table = os.path.join(self.sched, "jobs")
        self.assertTrue(os.path.isfile(table), "no jobs table under schedule/")
        with open(table, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
        self.assertIn("fast=reap --all", lines)
        self.assertIn("slow=check", lines)

    def test_an_edited_table_is_what_runs(self):
        os.makedirs(self.sched)
        with open(os.path.join(self.sched, "jobs"), "w", encoding="utf-8") as handle:
            handle.write("fast=reap --all\n")

        result = self.engine("schedule", "run", "--once")

        self.assertEqual(result.returncode, 0, self.output(result))
        self.assertTrue(os.path.isfile(self.stamp_path("fast")))
        self.assertFalse(os.path.exists(self.stamp_path("slow")))
        self.assertFalse(os.path.exists(self.log_path("slow")))

    def test_a_job_that_has_never_run_is_due_and_gets_a_stamp_in_epoch_seconds(self):
        self.slow_is_fresh()
        before = int(time.time())

        result = self.engine("schedule", "run", "--once")

        self.assertEqual(result.returncode, 0, self.output(result))
        self.assertGreaterEqual(self.read_stamp("fast"), before)
        self.assertLessEqual(self.read_stamp("fast"), int(time.time()))

    def test_a_job_whose_last_run_is_inside_the_interval_does_not_run(self):
        self.slow_is_fresh()
        recent = int(time.time()) - 1
        self.write_stamp("fast", recent)

        result = self.engine("schedule", "run", "--once", fast=1000)

        self.assertEqual(result.returncode, 0, self.output(result))
        self.assertEqual(self.read_stamp("fast"), recent)

    def test_a_job_is_due_exactly_when_the_interval_has_been_reached(self):
        self.slow_is_fresh()
        old = int(time.time()) - 1000
        self.write_stamp("fast", old)

        self.engine("schedule", "run", "--once", fast=1000)

        self.assertGreater(self.read_stamp("fast"), old)

    def test_a_long_missed_run_happens_once(self):
        self.slow_is_fresh()
        self.write_stamp("fast", int(time.time()) - 86400)

        self.engine("schedule", "run", "--once", fast=60)
        first = self.read_stamp("fast")
        self.engine("schedule", "run", "--once", fast=60)

        self.assertGreater(first, int(time.time()) - 60)
        self.assertEqual(self.read_stamp("fast"), first)

    def test_the_interval_comes_from_the_environment_per_job(self):
        old = int(time.time()) - 500
        self.write_stamp("fast", old)
        self.write_stamp("slow", old)

        self.engine("schedule", "run", "--once", fast=100, slow=1000)

        self.assertGreater(self.read_stamp("fast"), old)
        self.assertEqual(self.read_stamp("slow"), old)

    def test_a_bad_interval_is_refused_and_names_the_variable(self):
        result = self.engine("schedule", "run", "--once", fast="soon")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("DEV_CONTAINER_SCHEDULE_FAST", self.output(result))

    def test_a_missing_job_is_reported_and_is_not_fatal(self):
        # `check` does not exist yet: the other job must still run.
        self.write_stamp("slow", int(time.time()) - 5000)

        result = self.engine("schedule", "run", "--once", slow=10)

        self.assertEqual(result.returncode, 0, self.output(result))
        self.assertTrue(os.path.isfile(self.stamp_path("fast")))
        self.assertIn("check", self.read_log("slow"))


class ScheduleLoop(ScheduleCase):

    def spawn_loop(self):
        proc = subprocess.Popen(
            ["bash", os.path.join(self.plain, SCRIPT_REL), "schedule", "run"],
            cwd=self.harness.tmp,
            env=self.env(fast=1000, slow=1000),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        return proc

    def test_the_loop_runs_a_due_job_once_across_many_ticks(self):
        self.slow_is_fresh()
        proc = self.spawn_loop()
        self.wait_for(self.stamp_path("fast"))
        first = self.read_stamp("fast")

        time.sleep(4)

        self.assertIsNone(proc.poll(), "the loop exited on its own")
        self.assertEqual(self.read_stamp("fast"), first)

    def test_sigterm_stops_the_loop_promptly_and_cleanly(self):
        self.slow_is_fresh()
        proc = self.spawn_loop()
        self.wait_for(self.stamp_path("fast"))

        proc.send_signal(signal.SIGTERM)

        try:
            code = proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.fail("the loop ignored SIGTERM for 10 seconds")
        self.assertEqual(code, 0)


class ReapAll(ScheduleCase):

    def setUp(self):
        super().setUp()
        self.harness.build()
        self.second = self.another(SecondScheduleHarness)
        self.second.build()

    def test_a_new_lease_records_the_root_of_the_project_that_made_it(self):
        taken = self.harness.acquire("worker")

        slot_id = self.harness.slot_id(taken)
        with open(os.path.join(self.state, "leases", slot_id + ".json"), encoding="utf-8") as handle:
            lease = json.load(handle)
        self.assertEqual(lease.get("repo_root"), self.harness.fixture_repo)

    def test_reap_all_visits_the_root_of_every_project_with_a_live_lease(self):
        self.write_lease("one", self.harness.fixture_repo)
        self.write_lease("two", self.second.fixture_repo)

        result = self.engine("reap", "--all")

        self.assertEqual(result.returncode, 0, self.output(result))
        self.assertEqual(len(self.git_calls_naming(self.harness.fixture_repo)), 1)
        self.assertEqual(len(self.git_calls_naming(self.second.fixture_repo)), 1)

    def test_two_leases_of_one_project_visit_its_root_once(self):
        self.write_lease("one", self.harness.fixture_repo)
        self.write_lease("two", self.harness.fixture_repo)

        result = self.engine("reap", "--all")

        self.assertEqual(result.returncode, 0, self.output(result))
        self.assertEqual(len(self.git_calls_naming(self.harness.fixture_repo)), 1)

    def test_an_unreachable_root_is_reported_and_skipped(self):
        gone = os.path.join(self.harness.tmp, "moved-away")
        self.write_lease("one", gone)
        self.write_lease("two", self.second.fixture_repo)

        result = self.engine("reap", "--all")

        self.assertEqual(result.returncode, 0, self.output(result))
        self.assertIn(gone, self.output(result))
        self.assertEqual(len(self.git_calls_naming(self.second.fixture_repo)), 1)

    def test_reap_all_with_no_leases_does_nothing_and_succeeds(self):
        result = self.engine("reap", "--all")

        self.assertEqual(result.returncode, 0, self.output(result))

    def test_the_fast_job_is_reap_all_and_writes_to_its_log(self):
        gone = os.path.join(self.harness.tmp, "moved-away")
        self.write_lease("one", gone)
        self.slow_is_fresh()

        result = self.engine("schedule", "run", "--once")

        self.assertEqual(result.returncode, 0, self.output(result))
        self.assertIn(gone, self.read_log("fast"))


class Boot(ScheduleCase):
    """State outlives a container, so a restart must not trust what the last one left."""

    def setUp(self):
        super().setUp()
        self.harness.build()
        self.lock = os.path.join(self.state, "lock")
        self.boot_file = os.path.join(self.state, "boot-id")

    def record_boot(self, boot_id):
        with open(self.boot_file, "w", encoding="utf-8") as handle:
            handle.write(boot_id + "\n")

    def take_lock(self):
        with open(self.lock, "w", encoding="utf-8"):
            pass

    def write_reservation(self, name, boot_id, pid, prefix=PROJECT_PREFIX):
        path = os.path.join(self.state, "leases", f"{prefix}-{name}.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "holder": "ghost",
                    "project": f"{prefix}-{name}",
                    "since": int(time.time()),
                    "key": "",
                    "ssh_port": 0,
                    "owner": "-",
                    "setup": "creating",
                    "pid": pid,
                    "boot_id": boot_id,
                },
                handle,
            )
        return path

    def test_a_lock_left_by_an_earlier_boot_is_cleared(self):
        self.record_boot("boot-old")
        self.take_lock()
        self.harness.boot_id = "boot-new"

        result = self.harness.run("list")

        self.assertEqual(result.returncode, 0, self.output(result))
        self.assertFalse(os.path.exists(self.lock), "the old boot's lock is still there")

    def test_the_current_boot_id_is_recorded(self):
        self.record_boot("boot-old")
        self.harness.boot_id = "boot-new"

        self.harness.run("list")

        with open(self.boot_file, encoding="utf-8") as handle:
            self.assertEqual(handle.read().strip(), "boot-new")

    def test_a_lock_taken_in_this_boot_is_left_alone(self):
        self.record_boot("boot-a")
        self.take_lock()

        self.harness.run("list")

        self.assertTrue(os.path.exists(self.lock), "a live lock was cleared")

    def test_a_reservation_carries_the_boot_id(self):
        self.harness.boot_id = "boot-reserved"
        self.harness.hold_compose_up("slot-1")
        first = self.harness.spawn("acquire", "--for", "first")
        self.addCleanup(lambda: first.poll() is None and first.kill())
        self.harness.wait_until_compose_up_held("slot-1")

        with open(self.harness.lease_path("slot-1"), encoding="utf-8") as handle:
            reservation = json.load(handle)
        self.harness.let_compose_up_go("slot-1")
        first.communicate(timeout=90)

        self.assertIn("pid", reservation)
        self.assertEqual(reservation.get("boot_id"), "boot-reserved")

    def test_a_reservation_from_another_boot_is_dropped_though_its_pid_is_alive(self):
        # A restarted container can hand the old acquire's pid to something else.
        self.record_boot("boot-a")
        ghost = self.write_reservation("ghost", "boot-old", os.getpid())

        result = self.harness.acquire("worker")

        self.assertEqual(result.returncode, 0, self.output(result))
        self.assertFalse(os.path.exists(ghost), "the old boot's reservation survived")
        self.assertIn("reservation", result.stderr.decode())

    def test_a_reservation_from_this_boot_with_a_live_pid_is_kept(self):
        self.record_boot("boot-a")
        ghost = self.write_reservation("ghost", "boot-a", os.getpid())

        result = self.harness.acquire("worker")

        self.assertEqual(result.returncode, 0, self.output(result))
        self.assertTrue(os.path.exists(ghost), "a live acquire's reservation was dropped")

    def test_a_reservation_for_another_project_from_an_earlier_boot_is_swept_and_not_counted_live(self):
        self.record_boot("boot-a")
        self.harness.write_limits(machine_max=1)
        foreign = self.write_reservation("slot-1", "boot-old", os.getpid(), prefix="second")

        listed = self.harness.run("list", "--brief")
        self.assertEqual(listed.returncode, 0, self.output(listed))
        self.assertNotIn("second-slot-1", listed.stdout.decode())

        result = self.harness.acquire("worker")

        self.assertEqual(result.returncode, 0, self.output(result))
        self.assertFalse(os.path.exists(foreign), "another project's old-boot reservation survived")

    def make_proc_root(self, name, kernel_boot_id, pid1_starttime):
        root = os.path.join(self.harness.tmp, name)
        boot_path = os.path.join(root, "sys", "kernel", "random", "boot_id")
        stat_path = os.path.join(root, "1", "stat")
        os.makedirs(os.path.dirname(boot_path), exist_ok=True)
        os.makedirs(os.path.dirname(stat_path), exist_ok=True)
        with open(boot_path, "w", encoding="utf-8") as handle:
            handle.write(kernel_boot_id + "\n")
        fields = ["S"] + ["0"] * 18 + [str(pid1_starttime), "0"]
        with open(stat_path, "w", encoding="utf-8") as handle:
            handle.write("1 (tini) " + " ".join(fields) + "\n")
        return root

    def test_a_new_pid_one_under_the_same_kernel_boot_id_starts_a_new_boot(self):
        proc_old = self.make_proc_root("proc-old", "host-boot-1", 1000)
        proc_new = self.make_proc_root("proc-new", "host-boot-1", 2000)
        first = self.engine(
            "reap", "--all", DEV_CONTAINER_BOOT_ID="", DEV_CONTAINER_PROC_ROOT=proc_old
        )
        self.assertEqual(first.returncode, 0, self.output(first))
        self.take_lock()

        second = self.engine(
            "reap", "--all", DEV_CONTAINER_BOOT_ID="", DEV_CONTAINER_PROC_ROOT=proc_new
        )

        self.assertEqual(second.returncode, 0, self.output(second))
        self.assertFalse(os.path.exists(self.lock), "the lock from the earlier container start survived")

    def test_a_concurrent_boot_check_does_not_clear_a_lock_taken_in_the_new_boot(self):
        self.record_boot("boot-old")
        self.harness.boot_id = "boot-new"
        guard = os.path.join(self.state, ".boot-lock")
        os.mkdir(guard)
        self.addCleanup(lambda: os.path.isdir(guard) and os.rmdir(guard))

        proc = self.harness.spawn("list")
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        time.sleep(0.2)
        self.record_boot("boot-new")
        self.take_lock()
        os.rmdir(guard)
        proc.communicate(timeout=30)

        self.assertEqual(proc.returncode, 0)
        self.assertTrue(os.path.exists(self.lock), "concurrent boot_check cleared the new boot's lock")


class RescueBundlesStay(ScheduleCase):
    """The 30-day delete of old bundles is gone; its replacement is a separate change."""

    def setUp(self):
        super().setUp()
        self.harness.build()
        self.rescue = os.path.join(self.state, "rescue")
        os.makedirs(self.rescue, exist_ok=True)

    def old_file(self, name, days):
        path = os.path.join(self.rescue, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("kept\n")
        stamp = time.time() - days * 86400
        os.utime(path, (stamp, stamp))
        return path

    def test_reap_leaves_a_bundle_older_than_thirty_days(self):
        bundle = self.old_file("demo-slot-9-20200101-000000-1.bundle", 60)
        archive = self.old_file("demo-slot-9-20200101-000000-1-untracked.tar.gz", 400)

        result = self.harness.run("reap")

        self.assertEqual(result.returncode, 0, self.output(result))
        self.assertTrue(os.path.exists(bundle), "reap deleted an old bundle")
        self.assertTrue(os.path.exists(archive), "reap deleted an old untracked archive")

    def test_reap_all_leaves_a_bundle_older_than_thirty_days(self):
        bundle = self.old_file("demo-slot-9-20200101-000000-2.bundle", 60)

        result = self.engine("reap", "--all")

        self.assertEqual(result.returncode, 0, self.output(result))
        self.assertTrue(os.path.exists(bundle), "reap --all deleted an old bundle")


if __name__ == "__main__":
    unittest.main()
