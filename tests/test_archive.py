import json
import os
import shutil
import stat
import subprocess
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(__file__))

from harness import (
    DevContainerTestCase,
    PROJECT_PREFIX,
)


class TestArchive(DevContainerTestCase):

    ENV = "GIT_USER_NAME=demo-agent\nGIT_USER_EMAIL=demo-agent@example.com\n"

    def setUp(self):
        super().setUp()
        self.harness.build_fixture_repo(with_parent_env=self.ENV)
        self.rescue_dir = os.path.join(self.harness.state_dir, "rescue")

    def _git(self, slot_dir):
        return lambda *args: subprocess.run(
            [
                self.harness.real_git_bin,
                "-C",
                slot_dir,
                "-c",
                "commit.gpgsign=false",
                "-c",
                "user.email=fixture@example.com",
                "-c",
                "user.name=fixture",
                *args,
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def _expire_lease(self, name, age_seconds=10000):
        path = self.harness.lease_path(name)
        self.assertTrue(os.path.isfile(path), f"lease file missing: {path}")
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        data["since"] = int(time.time()) - age_seconds
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(data, handle)

        prefix = f"{PROJECT_PREFIX}-{name}"
        for kind in ("run", "cpu"):
            stamp = os.path.join(self.harness.state_dir, "activity", f"{prefix}.{kind}")
            if os.path.exists(stamp):
                os.remove(stamp)

    def test_expired_slot_archived_with_bundle_patch_and_tar(self):
        result = self.harness.run_acquire("holder-1")
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        name = self.harness.acquired_slot_name(result)
        slot_dir = self.harness.slot_dir(name)

        git = self._git(slot_dir)
        git("checkout", "-b", "feature-work")
        work_file = os.path.join(slot_dir, "work.txt")
        with open(work_file, "w", encoding="utf-8") as handle:
            handle.write("unpushed commit data\n")
        git("add", "work.txt")
        git("commit", "-m", "add work.txt")

        tracked_file = os.path.join(slot_dir, ".gitignore")
        with open(tracked_file, "a", encoding="utf-8") as handle:
            handle.write("ignored.log\ntemp_pattern\n")

        new_file = os.path.join(slot_dir, "untracked.txt")
        with open(new_file, "w", encoding="utf-8") as handle:
            handle.write("untracked contents\n")

        ignored_file = os.path.join(slot_dir, "ignored.log")
        with open(ignored_file, "w", encoding="utf-8") as handle:
            handle.write("ignored file content\n")

        self._expire_lease(name)

        clean_res = self.harness.run("clean")
        self.assertEqual(clean_res.returncode, 0, clean_res.stderr.decode())

        self.assertFalse(os.path.exists(slot_dir), f"slot dir was not deleted: {slot_dir}")

        archive_dir = os.path.join(self.rescue_dir, self.harness.project_key, name)
        self.assertTrue(os.path.isdir(archive_dir), f"archive dir missing: {archive_dir}")

        bundle_path = os.path.join(archive_dir, "commits.bundle")
        self.assertTrue(os.path.isfile(bundle_path), "commits.bundle missing")

        patch_path = os.path.join(archive_dir, "changes.patch")
        self.assertTrue(os.path.isfile(patch_path), "changes.patch missing")
        with open(patch_path, "r", encoding="utf-8") as handle:
            patch_content = handle.read()
        self.assertIn("temp_pattern", patch_content)

        tar_path = os.path.join(archive_dir, "new-files.tar")
        self.assertTrue(os.path.isfile(tar_path), "new-files.tar missing")
        tar_list = subprocess.run(
            ["tar", "-tf", tar_path],
            check=True,
            stdout=subprocess.PIPE,
        ).stdout.decode()
        self.assertIn("untracked.txt", tar_list)
        self.assertNotIn("ignored.log", tar_list)

        meta_path = os.path.join(archive_dir, "meta.json")
        self.assertTrue(os.path.isfile(meta_path), "meta.json missing")
        with open(meta_path, "r", encoding="utf-8") as handle:
            meta = json.load(handle)
        self.assertEqual(meta["project"], PROJECT_PREFIX)
        self.assertEqual(meta["lease"], name)
        self.assertIn("feature-work", meta["branches"])
        self.assertIn("ignored.log", meta["ignored"])
        self.assertEqual(meta["omitted"], [])
        self.assertIsInstance(meta["created_at"], int)

    def test_thin_bundle_holds_only_commits_not_on_remote(self):
        result = self.harness.run_acquire("holder-thin")
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        name = self.harness.acquired_slot_name(result)
        slot_dir = self.harness.slot_dir(name)

        git = self._git(slot_dir)
        git("checkout", "-b", "local-branch")
        commit_file = os.path.join(slot_dir, "commit.txt")
        with open(commit_file, "w", encoding="utf-8") as handle:
            handle.write("local only\n")
        git("add", "commit.txt")
        git("commit", "-m", "local commit")

        full_bundle = os.path.join(self.harness.tmp, "full.bundle")
        subprocess.run(
            [self.harness.real_git_bin, "-C", slot_dir, "bundle", "create", full_bundle, "--all"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        self._expire_lease(name)
        clean_res = self.harness.run("clean")
        self.assertEqual(clean_res.returncode, 0, clean_res.stderr.decode())

        archive_dir = os.path.join(self.rescue_dir, self.harness.project_key, name)
        thin_bundle = os.path.join(archive_dir, "commits.bundle")
        self.assertTrue(os.path.isfile(thin_bundle))
        self.assertLess(os.path.getsize(thin_bundle), os.path.getsize(full_bundle))

    def test_new_files_tar_stops_at_size_limit_and_records_omitted_in_meta(self):
        result = self.harness.run_acquire("holder-size")
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        name = self.harness.acquired_slot_name(result)
        slot_dir = self.harness.slot_dir(name)

        small_file = os.path.join(slot_dir, "small.txt")
        with open(small_file, "w", encoding="utf-8") as handle:
            handle.write("small\n")

        large_file = os.path.join(slot_dir, "large.txt")
        with open(large_file, "w", encoding="utf-8") as handle:
            handle.write("x" * 2000)

        self._expire_lease(name)

        clean_res = self.harness.run("clean", port_overrides={"DEV_CONTAINER_ARCHIVE_SIZE_LIMIT": "500"})
        self.assertEqual(clean_res.returncode, 0, clean_res.stderr.decode())

        archive_dir = os.path.join(self.rescue_dir, self.harness.project_key, name)
        tar_path = os.path.join(archive_dir, "new-files.tar")
        self.assertTrue(os.path.isfile(tar_path))
        tar_list = subprocess.run(
            ["tar", "-tf", tar_path],
            check=True,
            stdout=subprocess.PIPE,
        ).stdout.decode()
        self.assertIn("small.txt", tar_list)
        self.assertNotIn("large.txt", tar_list)

        meta_path = os.path.join(archive_dir, "meta.json")
        with open(meta_path, "r", encoding="utf-8") as handle:
            meta = json.load(handle)
        omitted = meta.get("omitted", [])
        self.assertEqual(len(omitted), 1)
        self.assertEqual(omitted[0]["name"], "large.txt")
        self.assertEqual(omitted[0]["size"], 2000)

    def test_clean_slot_with_nothing_to_keep_is_deleted_without_archive(self):
        result = self.harness.run_acquire("holder-clean")
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        name = self.harness.acquired_slot_name(result)
        slot_dir = self.harness.slot_dir(name)

        self._expire_lease(name)
        clean_res = self.harness.run("clean")
        self.assertEqual(clean_res.returncode, 0, clean_res.stderr.decode())

        self.assertFalse(os.path.exists(slot_dir))
        archive_dir = os.path.join(self.rescue_dir, self.harness.project_key, name)
        self.assertFalse(os.path.exists(archive_dir), f"archive created for clean slot: {archive_dir}")

    def test_crash_with_target_in_place_only_deletes_slot(self):
        result = self.harness.run_acquire("holder-crash")
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        name = self.harness.acquired_slot_name(result)
        slot_dir = self.harness.slot_dir(name)

        archive_dir = os.path.join(self.rescue_dir, self.harness.project_key, name)
        os.makedirs(archive_dir, exist_ok=True)
        sentinel_path = os.path.join(archive_dir, "sentinel.txt")
        with open(sentinel_path, "w", encoding="utf-8") as handle:
            handle.write("crash sentinel\n")

        with open(os.path.join(slot_dir, "newfile.txt"), "w", encoding="utf-8") as handle:
            handle.write("slot data\n")

        self._expire_lease(name)
        clean_res = self.harness.run("clean")
        self.assertEqual(clean_res.returncode, 0, clean_res.stderr.decode())

        self.assertFalse(os.path.exists(slot_dir))
        self.assertTrue(os.path.isfile(sentinel_path), "target archive was overwritten instead of only deleting")

    def test_after_clean_slot_number_is_freed(self):
        result = self.harness.run_acquire("holder-num")
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        name = self.harness.acquired_slot_name(result)
        self.assertEqual(name, "slot-1")

        self._expire_lease(name)
        clean_res = self.harness.run("clean")
        self.assertEqual(clean_res.returncode, 0, clean_res.stderr.decode())

        result2 = self.harness.run_acquire("holder-num-2")
        self.assertEqual(result2.returncode, 0, result2.stderr.decode())
        name2 = self.harness.acquired_slot_name(result2)
        self.assertEqual(name2, "slot-1", "slot number was not freed after clean")

    def test_clean_makes_no_network_call(self):
        result = self.harness.run_acquire("holder-net")
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        name = self.harness.acquired_slot_name(result)
        slot_dir = self.harness.slot_dir(name)

        with open(os.path.join(slot_dir, "work.txt"), "w", encoding="utf-8") as handle:
            handle.write("work\n")

        self._expire_lease(name)

        fail_stub = "#!/usr/bin/env bash\necho 'network error' >&2\nexit 1\n"
        self.harness._write_stub("gh", fail_stub)

        clean_res = self.harness.run("clean")
        self.assertEqual(clean_res.returncode, 0, f"clean failed on network stub: {clean_res.stderr.decode()}")
        self.assertFalse(os.path.exists(slot_dir))

    def test_live_slot_with_recent_run_or_cpu_activity_is_untouched(self):
        result = self.harness.run_acquire("holder-live")
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        name = self.harness.acquired_slot_name(result)
        slot_dir = self.harness.slot_dir(name)

        self._expire_lease(name, age_seconds=10000)
        prefix = f"{PROJECT_PREFIX}-{name}"
        act_dir = os.path.join(self.harness.state_dir, "activity")
        os.makedirs(act_dir, exist_ok=True)
        with open(os.path.join(act_dir, f"{prefix}.run"), "w", encoding="utf-8") as handle:
            handle.write(str(int(time.time())) + "\n")

        clean_res = self.harness.run("clean")
        self.assertEqual(clean_res.returncode, 0, clean_res.stderr.decode())
        self.assertTrue(os.path.isdir(slot_dir), "slot with recent run activity was cleaned")

        with open(os.path.join(act_dir, f"{prefix}.run"), "w", encoding="utf-8") as handle:
            handle.write(str(int(time.time()) - 10000) + "\n")
        with open(os.path.join(act_dir, f"{prefix}.cpu"), "w", encoding="utf-8") as handle:
            handle.write(str(int(time.time())) + "\n")

        clean_res2 = self.harness.run("clean")
        self.assertEqual(clean_res2.returncode, 0, clean_res2.stderr.decode())
        self.assertTrue(os.path.isdir(slot_dir), "slot with recent cpu activity was cleaned")

    def test_idle_process_without_cpu_does_not_keep_slot(self):
        result = self.harness.run_acquire("holder-idle-proc")
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        name = self.harness.acquired_slot_name(result)
        slot_dir = self.harness.slot_dir(name)

        self.harness.mark_running(name)
        self.harness.set_container_procs(name, ["sshd", "node", "sleep"])
        self._expire_lease(name)

        clean_res = self.harness.run("clean")
        self.assertEqual(clean_res.returncode, 0, clean_res.stderr.decode())
        self.assertFalse(os.path.exists(slot_dir), "slot with zero cpu was kept by running procs")

    def test_failed_archive_parks_slot_and_reports(self):
        result = self.harness.run_acquire("holder-fail")
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        name = self.harness.acquired_slot_name(result)
        slot_dir = self.harness.slot_dir(name)

        with open(os.path.join(slot_dir, "work.txt"), "w", encoding="utf-8") as handle:
            handle.write("work\n")

        self._expire_lease(name)

        os.makedirs(self.rescue_dir, exist_ok=True)
        block_file = os.path.join(self.rescue_dir, self.harness.project_key)
        with open(block_file, "w", encoding="utf-8") as handle:
            handle.write("block\n")

        try:
            clean_res = self.harness.run("clean")
            self.assertTrue(os.path.isdir(slot_dir), "slot was removed despite archive failure")
            parked_file = self.harness.parked_path(name)
            self.assertTrue(os.path.isfile(parked_file), "slot was not parked on archive failure")
        finally:
            if os.path.isfile(block_file):
                os.remove(block_file)

    def test_non_project_folders_in_rescue_are_left_alone(self):
        os.makedirs(self.rescue_dir, exist_ok=True)
        flat_file = os.path.join(self.rescue_dir, "legacy-rescue.bundle")
        with open(flat_file, "w", encoding="utf-8") as handle:
            handle.write("legacy bundle\n")

        other_dir = os.path.join(self.rescue_dir, "other-thing")
        os.makedirs(other_dir, exist_ok=True)
        with open(os.path.join(other_dir, "data.txt"), "w", encoding="utf-8") as handle:
            handle.write("other\n")

        clean_res = self.harness.run("clean")
        self.assertEqual(clean_res.returncode, 0, clean_res.stderr.decode())

        self.assertTrue(os.path.isfile(flat_file), "flat file in rescue/ was touched")
        self.assertTrue(os.path.isdir(other_dir), "other directory in rescue/ was touched")

    def test_no_step_creates_commit_in_slot(self):
        result = self.harness.run_acquire("holder-no-commit")
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        name = self.harness.acquired_slot_name(result)
        slot_dir = self.harness.slot_dir(name)

        with open(os.path.join(slot_dir, "work.txt"), "w", encoding="utf-8") as handle:
            handle.write("uncommitted work\n")

        self._expire_lease(name)
        clean_res = self.harness.run("clean")
        self.assertEqual(clean_res.returncode, 0, clean_res.stderr.decode())

        archive_dir = os.path.join(self.rescue_dir, self.harness.project_key, name)
        meta_path = os.path.join(archive_dir, "meta.json")
        self.assertTrue(os.path.isfile(meta_path))
        bundle_path = os.path.join(archive_dir, "commits.bundle")
        self.assertFalse(os.path.exists(bundle_path), "bundle was created when there were only uncommitted edits")


if __name__ == "__main__":
    unittest.main()
