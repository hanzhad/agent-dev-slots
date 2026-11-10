import json
import os
import shutil
import subprocess
import time
import unittest

import test_schedule
import test_unlock
from engine_stub import FakeDockerCase, THIN
from harness import PROJECT_PREFIX, REPO_ROOT, SCRIPT_REL


class StatusCase(test_unlock.UnlockCase):

    def setUp(self):
        super().setUp()
        self.ports_dir = os.path.join(self.state, "ports")
        os.makedirs(self.ports_dir, exist_ok=True)

    def write_ports_file(self, sid, ssh=42000, novnc=42001, vite=42002):
        path = os.path.join(self.ports_dir, f"{sid}.ports")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(f"SSH_PORT={ssh}\nNOVNC_PORT={novnc}\nVITE_DEV_PORT={vite}\n")

    def write_meta(self, project, lease, branches=None, timestamp=None, age=None):
        rescue_dir = os.path.join(self.rescue, project, lease)
        os.makedirs(rescue_dir, exist_ok=True)
        meta = {}
        if branches is not None:
            meta["branches"] = branches
        if timestamp is not None:
            meta["timestamp"] = timestamp
        if age is not None:
            meta["age"] = age
        with open(os.path.join(rescue_dir, "meta.json"), "w", encoding="utf-8") as handle:
            json.dump(meta, handle)


class TestStatusJsonBasic(StatusCase):

    def test_status_json_version_1(self):
        result = self.engine("status", "--json")
        self.assertEqual(result.returncode, 0, self.output(result))
        data = json.loads(result.stdout.decode())
        self.assertEqual(data.get("version"), 1)

    def test_status_json_top_level_keys(self):
        result = self.engine("status", "--json")
        self.assertEqual(result.returncode, 0, self.output(result))
        data = json.loads(result.stdout.decode())
        self.assertIn("version", data)
        self.assertIn("slots", data)
        self.assertIn("cleaners", data)
        self.assertIn("archives", data)
        self.assertIn(data.get("engine", data.get("status")), ("running", "ok"))

    def test_status_human_readable(self):
        result = self.engine("status")
        self.assertEqual(result.returncode, 0, self.output(result))
        out = result.stdout.decode()
        self.assertIn("Slots", out)
        self.assertIn("Cleaners", out)
        self.assertIn("Archives", out)


class TestStatusJsonSlots(StatusCase):

    def test_slots_empty_by_default(self):
        result = self.engine("status", "--json")
        self.assertEqual(result.returncode, 0, self.output(result))
        data = json.loads(result.stdout.decode())
        self.assertEqual(data.get("slots"), [])

    def test_slots_reports_live_lease(self):
        sid = f"{PROJECT_PREFIX}-slot-1"
        self.write_lease("slot-1", self.harness.tmp, age_seconds=30)
        self.write_ports_file(sid, ssh=42016, novnc=42017, vite=42018)

        result = self.engine("status", "--json")
        self.assertEqual(result.returncode, 0, self.output(result))
        data = json.loads(result.stdout.decode())
        slots = data.get("slots", [])
        self.assertEqual(len(slots), 1)
        slot = slots[0]
        self.assertEqual(slot.get("holder"), "task")
        self.assertIn(slot.get("project"), (PROJECT_PREFIX, sid, self.harness.tmp))
        self.assertIn("last_activity", slot)
        self.assertIn("ports", slot)
        ports = slot["ports"]
        self.assertIn("SSH_PORT", ports)
        self.assertEqual(int(ports["SSH_PORT"]), 42016)

    def test_slots_excludes_idle_expired_lease(self):
        self.write_lease("slot-1", self.harness.tmp, age_seconds=10000)

        result = self.engine("status", "--json")
        self.assertEqual(result.returncode, 0, self.output(result))
        data = json.loads(result.stdout.decode())
        self.assertEqual(data.get("slots"), [])

    def test_slots_excludes_in_flight_reservation(self):
        res_file = os.path.join(self.state, "leases", f"{PROJECT_PREFIX}-res-1.json")
        os.makedirs(os.path.dirname(res_file), exist_ok=True)
        with open(res_file, "w", encoding="utf-8") as handle:
            json.dump({
                "holder": "task-res",
                "project": f"{PROJECT_PREFIX}-res-1",
                "since": int(time.time()),
                "setup": "in-flight",
                "pid": os.getpid(),
                "boot_id": "boot-a",
            }, handle)

        result = self.engine("status", "--json")
        self.assertEqual(result.returncode, 0, self.output(result))
        data = json.loads(result.stdout.decode())
        self.assertEqual(data.get("slots"), [])

    def test_slots_excludes_abandoned_slot_dir_without_lease(self):
        abandoned = os.path.join(self.plain, "dev-containers", f"{PROJECT_PREFIX}-abandoned")
        os.makedirs(abandoned, exist_ok=True)

        result = self.engine("status", "--json")
        self.assertEqual(result.returncode, 0, self.output(result))
        data = json.loads(result.stdout.decode())
        slots = data.get("slots", [])
        self.assertEqual(len(slots), 0)


class TestStatusJsonCleaners(StatusCase):

    def test_cleaners_default_state(self):
        result = self.engine("status", "--json")
        self.assertEqual(result.returncode, 0, self.output(result))
        data = json.loads(result.stdout.decode())
        cleaners = data.get("cleaners", {})
        self.assertIn("fast", cleaners)
        self.assertIn("slow", cleaners)
        fast = cleaners["fast"]
        self.assertIsNone(fast.get("last_run"))
        self.assertIn("interval", fast)

    def test_cleaners_reports_fast_clean_last_run_and_result(self):
        now = int(time.time())
        self.write_stamp("fast", now)
        os.makedirs(self.sched, exist_ok=True)
        with open(self.log_path("fast"), "w", encoding="utf-8") as handle:
            handle.write("reap completed\nschedule: fast (reap --all) exited 0\n")

        result = self.engine("status", "--json", fast=300)
        self.assertEqual(result.returncode, 0, self.output(result))
        data = json.loads(result.stdout.decode())
        fast = data["cleaners"]["fast"]
        self.assertEqual(fast.get("last_run"), now)
        self.assertEqual(fast.get("result"), "ok")
        self.assertEqual(fast.get("interval"), 300)
        self.assertEqual(fast.get("next_planned_run"), now + 300)

    def test_cleaners_locked_state_when_no_token(self):
        token_path = os.path.join(self.run_dir, "token")
        if os.path.exists(token_path):
            os.remove(token_path)

        result = self.engine("status", "--json")
        self.assertEqual(result.returncode, 0, self.output(result))
        data = json.loads(result.stdout.decode())
        cleaners = data["cleaners"]
        self.assertEqual(cleaners["slow"].get("status"), "locked")
        self.assertNotEqual(cleaners["fast"].get("status"), "locked")

    def test_cleaners_slow_check_when_unlocked(self):
        token_path = os.path.join(self.run_dir, "token")
        with open(token_path, "w", encoding="utf-8") as handle:
            handle.write("sentinel-test-token\n")

        now = int(time.time())
        self.write_stamp("slow", now)
        with open(self.log_path("slow"), "w", encoding="utf-8") as handle:
            handle.write("check completed\nschedule: slow (check) exited 0\n")

        result = self.engine("status", "--json", slow=3600)
        self.assertEqual(result.returncode, 0, self.output(result))
        data = json.loads(result.stdout.decode())
        slow = data["cleaners"]["slow"]
        self.assertNotEqual(slow.get("status"), "locked")
        self.assertEqual(slow.get("result"), "ok")
        self.assertEqual(slow.get("interval"), 3600)
        self.assertEqual(slow.get("next_planned_run"), now + 3600)


class TestStatusJsonArchives(StatusCase):

    def test_archives_empty_by_default(self):
        result = self.engine("status", "--json")
        self.assertEqual(result.returncode, 0, self.output(result))
        data = json.loads(result.stdout.decode())
        self.assertEqual(data.get("archives"), [])

    def test_archives_scans_project_and_lease(self):
        proj = "demo-project"
        lease = "slot-3"
        rescue_dir = os.path.join(self.rescue, proj, lease)
        os.makedirs(rescue_dir, exist_ok=True)
        file_path = os.path.join(rescue_dir, "data.bin")
        with open(file_path, "wb") as handle:
            handle.write(b"x" * 1024)
        verdict_path = os.path.join(rescue_dir, "verdict.md")
        with open(verdict_path, "w", encoding="utf-8") as handle:
            handle.write("# Rescue Verdict\nAll changes saved.\n")
        self.write_meta(proj, lease, branches=["feat-a", "fix-b"], timestamp=1700000000)

        result = self.engine("status", "--json")
        self.assertEqual(result.returncode, 0, self.output(result))
        data = json.loads(result.stdout.decode())
        archives = data.get("archives", [])
        self.assertEqual(len(archives), 1)
        entry = archives[0]
        self.assertEqual(entry.get("project"), proj)
        self.assertEqual(entry.get("lease"), lease)
        self.assertGreaterEqual(entry.get("size", entry.get("size_bytes", 0)), 1024)
        self.assertEqual(entry.get("branches"), ["feat-a", "fix-b"])
        self.assertTrue(entry.get("verdict", entry.get("has_verdict")))

    def test_archives_verdict_false_and_no_meta(self):
        proj = "other-proj"
        lease = "slot-4"
        rescue_dir = os.path.join(self.rescue, proj, lease)
        os.makedirs(rescue_dir, exist_ok=True)
        with open(os.path.join(rescue_dir, "payload.txt"), "w", encoding="utf-8") as handle:
            handle.write("payload content\n")

        result = self.engine("status", "--json")
        self.assertEqual(result.returncode, 0, self.output(result))
        data = json.loads(result.stdout.decode())
        archives = data.get("archives", [])
        self.assertEqual(len(archives), 1)
        entry = archives[0]
        self.assertEqual(entry.get("project"), proj)
        self.assertEqual(entry.get("lease"), lease)
        self.assertEqual(entry.get("branches"), [])
        self.assertFalse(entry.get("verdict", entry.get("has_verdict")))
        self.assertIn("age", entry)


class TestStatusThin(FakeDockerCase):

    def thin(self, *args, state="running", **extra):
        cwd = os.path.join(self.projects, "some-project")
        os.makedirs(cwd, exist_ok=True)
        env = self.env(FAKE_DOCKER_STATE=state, PWD=cwd, **extra)
        return self.sh(THIN, *args, cwd=cwd, env=env)

    def test_status_thin_stopped_engine_human(self):
        result = self.thin("status", state="stopped")
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        out = result.stdout.decode() + result.stderr.decode()
        self.assertIn("not running", out)

    def test_status_thin_stopped_engine_json(self):
        result = self.thin("status", "--json", state="stopped")
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        data = json.loads(result.stdout.decode())
        self.assertEqual(data.get("version"), 1)
        self.assertIn(data.get("engine", data.get("status")), ("not running", "stopped", "exited"))

    def test_status_thin_missing_engine_human(self):
        result = self.thin("status", state="missing")
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        out = result.stdout.decode() + result.stderr.decode()
        self.assertIn("not installed", out)

    def test_status_thin_missing_engine_json(self):
        result = self.thin("status", "--json", state="missing")
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        data = json.loads(result.stdout.decode())
        self.assertEqual(data.get("version"), 1)
        self.assertIn(data.get("engine", data.get("status")), ("not installed", "missing"))


class TestOnDemandCommands(StatusCase):

    def test_schedule_run_fast_on_demand(self):
        result = self.engine("schedule", "run", "fast")
        self.assertEqual(result.returncode, 0, self.output(result))
        self.assertTrue(os.path.isfile(self.stamp_path("fast")))
        self.assertTrue(os.path.isfile(self.log_path("fast")))
        log = self.read_log("fast")
        self.assertIn("schedule: fast", log)

    def test_clean_fast_on_demand(self):
        result = self.engine("clean", "fast")
        self.assertEqual(result.returncode, 0, self.output(result))
        self.assertTrue(os.path.isfile(self.stamp_path("fast")))
        self.assertTrue(os.path.isfile(self.log_path("fast")))

    def test_clean_without_args_runs_fast_clean(self):
        result = self.engine("clean")
        self.assertEqual(result.returncode, 0, self.output(result))
        self.assertTrue(os.path.isfile(self.stamp_path("fast")))

    def test_schedule_run_slow_on_demand(self):
        result = self.engine("schedule", "run", "slow")
        self.assertEqual(result.returncode, 0, self.output(result))
        self.assertTrue(os.path.isfile(self.stamp_path("slow")))
        self.assertTrue(os.path.isfile(self.log_path("slow")))
        log = self.read_log("slow")
        self.assertIn("schedule: slow", log)

    def test_check_slow_on_demand(self):
        result = self.engine("check", "slow")
        self.assertEqual(result.returncode, 0, self.output(result))
        self.assertTrue(os.path.isfile(self.stamp_path("slow")))
        self.assertTrue(os.path.isfile(self.log_path("slow")))


if __name__ == "__main__":
    unittest.main()
