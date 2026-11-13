"""Rescue archives and the live-run lock are named by the project KEY, not by its label.

A directory or lock named by label is a name the engine cannot vouch for (two
projects may share a label); a root's key is its own. Whatever is still kept
under the old label name is refused with a pointer to migrate-identity, as the
token cache and the signing key are, and migrate-identity moves the live lock
along with the rest.
"""
import json
import os
import time
import unittest

import test_live_gateway as lg
from harness import PROJECT_PREFIX, DevContainerTestCase

IDENTITY = "GIT_USER_NAME=demo-agent\nGIT_USER_EMAIL=demo-agent@example.com\n"


class RescueCase(DevContainerTestCase):
    def setUp(self):
        super().setUp()
        self.harness.build_fixture_repo(with_parent_env=IDENTITY)
        self.state = self.harness.state_dir
        self.key = self.harness.project_key
        self.label = PROJECT_PREFIX
        self.assertNotEqual(self.key, self.label)

    def rescue(self, *parts):
        return os.path.join(self.state, "rescue", *parts)

    def expire(self, name, age_seconds=10000):
        path = self.harness.lease_path(name)
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        data["since"] = int(time.time()) - age_seconds
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        for kind in ("run", "cpu"):
            stamp = os.path.join(self.state, "activity", f"{self.label}-{name}.{kind}")
            if os.path.exists(stamp):
                os.remove(stamp)

    def abandoned_slot(self, holder="holder"):
        res = self.harness.run_acquire(holder)
        self.assertEqual(res.returncode, 0, res.stderr.decode())
        name = self.harness.acquired_slot_name(res)
        slot_dir = self.harness.slot_dir(name)
        with open(os.path.join(slot_dir, "work.txt"), "w", encoding="utf-8") as handle:
            handle.write("work\n")
        self.expire(name)
        return name, slot_dir

    def plant_legacy(self):
        os.makedirs(self.rescue(self.label, "old-slot"))
        with open(self.rescue(self.label, "old-slot", "note.txt"), "w", encoding="utf-8") as handle:
            handle.write("old\n")

    def text(self, res):
        return (res.stdout + res.stderr).decode()


class RescueDirectoryNamedByKey(RescueCase):
    def test_a_collapsed_slot_is_archived_under_the_key_and_not_the_label(self):
        name, slot_dir = self.abandoned_slot()

        res = self.harness.run("clean")

        self.assertEqual(res.returncode, 0, self.text(res))
        self.assertTrue(os.path.isdir(self.rescue(self.key, name)), os.listdir(self.rescue()))
        self.assertFalse(os.path.exists(self.rescue(self.label)))
        self.assertFalse(os.path.exists(slot_dir))

    def test_resume_finds_the_archive_under_the_key(self):
        name, slot_dir = self.abandoned_slot()
        self.assertEqual(self.harness.run("clean").returncode, 0)

        res = self.harness.run("resume", name)

        self.assertEqual(res.returncode, 0, self.text(res))
        self.assertFalse(os.path.exists(self.rescue(self.key, name)))

    def test_run_on_a_collapsed_slot_still_points_at_resume(self):
        name, slot_dir = self.abandoned_slot()
        self.assertEqual(self.harness.run("clean").returncode, 0)

        res = self.harness.run("run", name, "--", "ls")

        self.assertNotEqual(res.returncode, 0)
        self.assertIn(f"resume {name}", self.text(res))

    def test_run_does_not_see_an_archive_kept_only_under_the_label_as_the_collapsed_slot(self):
        self.plant_legacy()

        res = self.harness.run("run", "old-slot", "--", "ls")

        self.assertNotEqual(res.returncode, 0)
        self.assertIn("migrate-identity", self.text(res))


class RescueKeptUnderTheLabelIsRefused(RescueCase):
    def test_clean_refuses_to_archive_while_the_label_named_directory_stands_alone(self):
        self.plant_legacy()
        name, slot_dir = self.abandoned_slot()

        res = self.harness.run("clean")

        # clean goes on to the next project after one fails, so the exit code is not the signal
        self.assertIn("migrate-identity", self.text(res))
        self.assertTrue(os.path.isdir(slot_dir), "the slot was torn down although its archive had nowhere safe to go")
        self.assertFalse(os.path.exists(self.rescue(self.key)))
        self.assertTrue(os.path.isfile(self.rescue(self.label, "old-slot", "note.txt")))

    def test_resume_refuses_while_the_label_named_directory_stands_alone(self):
        self.plant_legacy()

        res = self.harness.run("resume", "old-slot")

        self.assertNotEqual(res.returncode, 0)
        self.assertIn("migrate-identity", self.text(res))
        self.assertTrue(os.path.isfile(self.rescue(self.label, "old-slot", "note.txt")))

    def test_once_the_key_named_directory_exists_the_old_one_is_not_in_the_way(self):
        self.plant_legacy()
        os.makedirs(self.rescue(self.key))
        name, slot_dir = self.abandoned_slot()

        res = self.harness.run("clean")

        self.assertEqual(res.returncode, 0, self.text(res))
        self.assertTrue(os.path.isdir(self.rescue(self.key, name)))


class LiveLockNamedByKey(lg.LiveGatewayTestCase):
    def key(self):
        return self.harness.project_key

    def lock(self, stem):
        return os.path.join(self.harness.state_dir, "live", stem + ".lock")

    def plant_lock(self, stem):
        os.makedirs(os.path.dirname(self.lock(stem)), exist_ok=True)
        lg.write(self.lock(stem), "999999999" + chr(10))

    def test_the_lock_held_by_a_running_live_command_is_named_by_the_key(self):
        self.flag("hang")
        proc = self.spawn_live(cmd=self.hang())
        lg.wait_for("the live command to be running", lambda: os.path.exists(os.path.join(self.fake, "hang-started")))

        held = sorted(os.listdir(os.path.join(self.harness.state_dir, "live")))

        self.unflag("hang")
        proc.wait(timeout=60)
        self.assertEqual(held, [self.key() + ".lock"])

    def test_a_lock_kept_under_the_old_label_name_is_refused_and_points_at_migrate_identity(self):
        self.plant_lock(PROJECT_PREFIX)
        self.reset_events()

        res = self.live()

        self.assertNotEqual(res.returncode, 0)
        self.assertIn("migrate-identity", lg.text(res))
        self.assertEqual(self.of("CB"), [])
        self.assertTrue(os.path.exists(self.lock(PROJECT_PREFIX)))

    def test_once_the_key_named_lock_exists_a_leftover_old_name_is_not_in_the_way(self):
        self.plant_lock(PROJECT_PREFIX)
        self.plant_lock(self.key())
        self.reset_events()

        res = self.live()

        self.assertEqual(res.returncode, 0, lg.text(res))
        self.assertEqual(len(self.of("CB")), 1)


class MigrateIdentityMovesTheLiveLock(RescueCase):
    def lock(self, stem):
        return os.path.join(self.state, "live", stem + ".lock")

    def plant(self, stem, content):
        os.makedirs(os.path.dirname(self.lock(stem)), exist_ok=True)
        with open(self.lock(stem), "w", encoding="utf-8") as handle:
            handle.write(content)

    def test_the_label_named_lock_moves_to_the_key(self):
        self.plant(self.label, "999999999\n")
        self.plant("other", "keep\n")

        res = self.harness.run("migrate-identity")

        self.assertEqual(res.returncode, 0, self.text(res))
        self.assertFalse(os.path.exists(self.lock(self.label)))
        with open(self.lock(self.key), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "999999999\n")
        self.assertTrue(os.path.exists(self.lock("other")))

    def test_dry_run_lists_the_lock_and_moves_nothing(self):
        self.plant(self.label, "x\n")

        res = self.harness.run("migrate-identity", "--dry-run")

        self.assertEqual(res.returncode, 0, self.text(res))
        self.assertIn(self.lock(self.key), self.text(res))
        self.assertTrue(os.path.exists(self.lock(self.label)))
        self.assertFalse(os.path.exists(self.lock(self.key)))

    def test_it_refuses_to_overwrite_a_key_named_lock(self):
        self.plant(self.label, "a\n")
        self.plant(self.key, "b\n")

        res = self.harness.run("migrate-identity")

        self.assertNotEqual(res.returncode, 0)
        self.assertTrue(os.path.exists(self.lock(self.label)))
        with open(self.lock(self.key), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "b\n")


if __name__ == "__main__":
    unittest.main()
