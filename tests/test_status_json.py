import json
import os
import shutil
import subprocess
import sys
import time
import unittest

if os.path.dirname(__file__) not in sys.path:
    sys.path.insert(0, os.path.dirname(__file__))

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
        sid = self.harness.slot_project_of("slot-1")
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

    def test_slots_include_project_label_and_slot_num_for_both_new_and_legacy_sids(self):
        new_sid = f"0-slot-1-{PROJECT_PREFIX}"
        legacy_sid = f"{PROJECT_PREFIX}-slot-2"
        leases_dir = os.path.join(self.state, "leases")
        os.makedirs(leases_dir, exist_ok=True)
        now = int(time.time())
        with open(os.path.join(leases_dir, f"{new_sid}.json"), "w", encoding="utf-8") as handle:
            json.dump({
                "holder": "task-new",
                "owner": "-",
                "project": new_sid,
                "project_label": PROJECT_PREFIX,
                "slot_num": 1,
                "repo_root": self.harness.fixture_repo,
                "since": now - 10,
                "key": "/dev/null",
                "ssh_port": 42016,
            }, handle)
        with open(os.path.join(leases_dir, f"{legacy_sid}.json"), "w", encoding="utf-8") as handle:
            json.dump({
                "holder": "task-legacy",
                "owner": "-",
                "project": legacy_sid,
                "repo_root": self.harness.fixture_repo,
                "since": now - 10,
                "key": "/dev/null",
                "ssh_port": 42032,
            }, handle)

        result = self.engine("status", "--json")
        self.assertEqual(result.returncode, 0, self.output(result))
        slots = json.loads(result.stdout.decode()).get("slots", [])
        by_project = {s.get("project"): s for s in slots}
        self.assertIn(new_sid, by_project)
        self.assertIn(legacy_sid, by_project)

        self.assertEqual(by_project[new_sid].get("project_label"), PROJECT_PREFIX)
        self.assertIs(type(by_project[new_sid].get("slot_num")), int)
        self.assertEqual(by_project[new_sid].get("slot_num"), 1)

        self.assertEqual(by_project[legacy_sid].get("project_label"), PROJECT_PREFIX)
        self.assertIs(type(by_project[legacy_sid].get("slot_num")), int)
        self.assertEqual(by_project[legacy_sid].get("slot_num"), 2)

        filtered = self.engine("status", "--json", "--project", PROJECT_PREFIX)
        self.assertEqual(filtered.returncode, 0, self.output(filtered))
        filtered_projects = {s.get("project") for s in json.loads(filtered.stdout.decode()).get("slots", [])}
        self.assertEqual(filtered_projects, {new_sid, legacy_sid})


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

    def test_archives_without_meta_uses_dev_container_now_for_age(self):
        proj = "other-proj"
        lease = "slot-no-meta"
        rescue_dir = os.path.join(self.rescue, proj, lease)
        os.makedirs(rescue_dir, exist_ok=True)
        with open(os.path.join(rescue_dir, "payload.txt"), "w", encoding="utf-8") as handle:
            handle.write("payload content\n")

        for injected in (2000000000, 1700000000):
            with self.subTest(injected=injected):
                result = self.engine("status", "--json", DEV_CONTAINER_NOW=str(injected))
                self.assertEqual(result.returncode, 0, self.output(result))
                archives = json.loads(result.stdout.decode()).get("archives", [])
                self.assertEqual(len(archives), 1)
                self.assertEqual(archives[0].get("age"), 0)

        self.write_meta(proj, lease, timestamp=1700000000)
        result = self.engine("status", "--json", DEV_CONTAINER_NOW="1700000125")
        self.assertEqual(result.returncode, 0, self.output(result))
        archives = json.loads(result.stdout.decode()).get("archives", [])
        self.assertEqual(len(archives), 1)
        self.assertEqual(archives[0].get("age"), 125)


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


class TestStatusJsonProjectFilter(StatusCase):

    def _acquire_two_projects(self):
        second = self.another(test_schedule.SecondScheduleHarness)
        second.build()
        subprocess.run(
            [
                second.real_git_bin,
                "-C",
                second.fixture_repo,
                "remote",
                "set-url",
                "origin",
                "https://example.invalid/second.git",
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        acq_demo = self.harness.acquire("holder-demo")
        self.assertEqual(acq_demo.returncode, 0, self.output(acq_demo))
        acq_second = second.acquire("holder-second")
        self.assertEqual(acq_second.returncode, 0, self.output(acq_second))
        return (
            os.path.realpath(self.harness.fixture_repo),
            os.path.realpath(second.fixture_repo),
        )

    def test_slots_include_repo_root_matching_resolved_checkout(self):
        acq = self.harness.acquire("holder-demo")
        self.assertEqual(acq.returncode, 0, self.output(acq))

        result = self.engine("status", "--json")
        self.assertEqual(result.returncode, 0, self.output(result))
        data = json.loads(result.stdout.decode())
        slots = data.get("slots", [])
        self.assertEqual(len(slots), 1)
        self.assertEqual(
            slots[0].get("repo_root"),
            os.path.realpath(self.harness.fixture_repo),
        )

    def test_status_json_project_filter_by_repo_root_isolates_matching_project(self):
        demo_root, second_root = self._acquire_two_projects()

        unfiltered = self.engine("status", "--json")
        self.assertEqual(unfiltered.returncode, 0, self.output(unfiltered))
        all_slots = json.loads(unfiltered.stdout.decode()).get("slots", [])
        self.assertEqual(len(all_slots), 2)
        self.assertEqual(
            {s.get("repo_root") for s in all_slots},
            {demo_root, second_root},
        )

        demo_res = self.engine("status", "--json", "--project", demo_root)
        self.assertEqual(demo_res.returncode, 0, self.output(demo_res))
        demo_slots = json.loads(demo_res.stdout.decode()).get("slots", [])
        self.assertEqual(len(demo_slots), 1)
        self.assertEqual(demo_slots[0].get("repo_root"), demo_root)
        self.assertEqual(demo_slots[0].get("holder"), "holder-demo")

        second_res = self.engine("status", "--json", "--project", second_root)
        self.assertEqual(second_res.returncode, 0, self.output(second_res))
        second_slots = json.loads(second_res.stdout.decode()).get("slots", [])
        self.assertEqual(len(second_slots), 1)
        self.assertEqual(second_slots[0].get("repo_root"), second_root)
        self.assertEqual(second_slots[0].get("holder"), "holder-second")

    def test_status_json_project_filter_by_label_isolates_matching_project(self):
        demo_root, second_root = self._acquire_two_projects()

        demo_res = self.engine("status", "--json", "--project", "demo")
        self.assertEqual(demo_res.returncode, 0, self.output(demo_res))
        demo_slots = json.loads(demo_res.stdout.decode()).get("slots", [])
        self.assertEqual(len(demo_slots), 1)
        self.assertEqual(demo_slots[0].get("repo_root"), demo_root)
        self.assertEqual(demo_slots[0].get("holder"), "holder-demo")

        second_res = self.engine("status", "--json", "--project", "second")
        self.assertEqual(second_res.returncode, 0, self.output(second_res))
        second_slots = json.loads(second_res.stdout.decode()).get("slots", [])
        self.assertEqual(len(second_slots), 1)
        self.assertEqual(second_slots[0].get("repo_root"), second_root)
        self.assertEqual(second_slots[0].get("holder"), "holder-second")

        none_res = self.engine("status", "--json", "--project", "nonexistent-project")
        self.assertEqual(none_res.returncode, 0, self.output(none_res))
        self.assertEqual(json.loads(none_res.stdout.decode()).get("slots"), [])

    def test_status_json_project_filter_bare_label_ignores_same_named_cwd_dir(self):
        demo_root, _second_root = self._acquire_two_projects()
        unrelated_demo = os.path.join(self.harness.tmp, "demo")
        os.makedirs(unrelated_demo, exist_ok=True)

        label_res = self.engine("status", "--json", "--project", "demo")
        self.assertEqual(label_res.returncode, 0, self.output(label_res))
        label_slots = json.loads(label_res.stdout.decode()).get("slots", [])
        self.assertEqual(len(label_slots), 1)
        self.assertEqual(label_slots[0].get("repo_root"), demo_root)
        self.assertEqual(label_slots[0].get("holder"), "holder-demo")

        dot_demo_res = self.engine("status", "--json", "--project", "./demo")
        self.assertEqual(dot_demo_res.returncode, 0, self.output(dot_demo_res))
        self.assertEqual(json.loads(dot_demo_res.stdout.decode()).get("slots"), [])

        rel_fixture = "./" + os.path.relpath(demo_root, self.harness.tmp)
        rel_res = self.engine("status", "--json", "--project", rel_fixture)
        self.assertEqual(rel_res.returncode, 0, self.output(rel_res))
        rel_slots = json.loads(rel_res.stdout.decode()).get("slots", [])
        self.assertEqual(len(rel_slots), 1)
        self.assertEqual(rel_slots[0].get("repo_root"), demo_root)
        self.assertEqual(rel_slots[0].get("holder"), "holder-demo")

        caller_rel = "../" + os.path.basename(demo_root)
        caller_res = self.engine(
            "status",
            "--json",
            "--project",
            caller_rel,
            DEV_CONTAINER_CALLER_PWD=unrelated_demo,
        )
        self.assertEqual(caller_res.returncode, 0, self.output(caller_res))
        caller_slots = json.loads(caller_res.stdout.decode()).get("slots", [])
        self.assertEqual(len(caller_slots), 1)
        self.assertEqual(caller_slots[0].get("repo_root"), demo_root)
        self.assertEqual(caller_slots[0].get("holder"), "holder-demo")


if __name__ == "__main__":
    unittest.main()
