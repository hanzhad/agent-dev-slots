import json
import os
import subprocess
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(__file__))

from harness import (
    DevContainerTestCase,
    PROJECT_PREFIX,
)


class TestResume(DevContainerTestCase):

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

    def test_resume_on_live_slot_reports_slot_and_changes_nothing(self):
        result = self.harness.run_acquire("live-holder")
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        name = self.harness.acquired_slot_name(result)
        slot_dir = self.harness.slot_dir(name)

        res = self.harness.run("resume", name)
        self.assertEqual(res.returncode, 0, res.stderr.decode())
        out = res.stdout.decode()
        self.assertIn(name, out)
        self.assertTrue(os.path.isdir(slot_dir))

    def test_resume_on_live_slot_by_branch_reports_slot(self):
        result = self.harness.run_acquire("live-branch-holder")
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        name = self.harness.acquired_slot_name(result)
        slot_dir = self.harness.slot_dir(name)

        git = self._git(slot_dir)
        git("checkout", "-b", "live-feature-branch")

        res = self.harness.run("resume", "live-feature-branch")
        self.assertEqual(res.returncode, 0, res.stderr.decode())
        out = res.stdout.decode()
        self.assertIn(name, out)
        self.assertTrue(os.path.isdir(slot_dir))

    def test_resume_with_archive_restores_commits_edits_and_untracked_files(self):
        result = self.harness.run_acquire("archive-holder")
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        name = self.harness.acquired_slot_name(result)
        slot_dir = self.harness.slot_dir(name)

        git = self._git(slot_dir)
        git("checkout", "-b", "feat-resume")
        feature_file = os.path.join(slot_dir, "feature.txt")
        with open(feature_file, "w", encoding="utf-8") as handle:
            handle.write("feature commit data\n")
        git("add", "feature.txt")
        git("commit", "-m", "feature commit")

        tracked_file = os.path.join(slot_dir, ".gitignore")
        with open(tracked_file, "a", encoding="utf-8") as handle:
            handle.write("uncommitted-line\n")

        new_file = os.path.join(slot_dir, "untracked-resume.txt")
        with open(new_file, "w", encoding="utf-8") as handle:
            handle.write("untracked resume data\n")

        self._expire_lease(name)
        clean_res = self.harness.run("clean")
        self.assertEqual(clean_res.returncode, 0, clean_res.stderr.decode())
        self.assertFalse(os.path.exists(slot_dir))

        archive_dir = os.path.join(self.rescue_dir, PROJECT_PREFIX, name)
        self.assertTrue(os.path.isdir(archive_dir))

        resume_res = self.harness.run("resume", "feat-resume")
        self.assertEqual(resume_res.returncode, 0, resume_res.stderr.decode())
        out = resume_res.stdout.decode()

        self.assertIn("slot-", out)

        restored_name = self.harness.acquired_slot_name(resume_res)
        restored_dir = self.harness.slot_dir(restored_name)
        self.assertTrue(os.path.isdir(restored_dir))

        restored_git = self._git(restored_dir)
        current_branch = restored_git("rev-parse", "--abbrev-ref", "HEAD").stdout.decode().strip()
        self.assertEqual(current_branch, "feat-resume")

        restored_feat = os.path.join(restored_dir, "feature.txt")
        self.assertTrue(os.path.isfile(restored_feat))
        with open(restored_feat, "r", encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "feature commit data\n")

        restored_tracked = os.path.join(restored_dir, ".gitignore")
        with open(restored_tracked, "r", encoding="utf-8") as handle:
            self.assertIn("uncommitted-line", handle.read())

        restored_new = os.path.join(restored_dir, "untracked-resume.txt")
        self.assertTrue(os.path.isfile(restored_new))
        with open(restored_new, "r", encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "untracked resume data\n")

        self.assertFalse(os.path.exists(archive_dir), "archive was not removed after restore")

    def test_resume_by_lease_name_with_archive(self):
        result = self.harness.run_acquire("lease-holder")
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        name = self.harness.acquired_slot_name(result)
        slot_dir = self.harness.slot_dir(name)

        git = self._git(slot_dir)
        git("checkout", "-b", "lease-branch")
        test_file = os.path.join(slot_dir, "lease.txt")
        with open(test_file, "w", encoding="utf-8") as handle:
            handle.write("lease data\n")
        git("add", "lease.txt")
        git("commit", "-m", "lease commit")

        self._expire_lease(name)
        clean_res = self.harness.run("clean")
        self.assertEqual(clean_res.returncode, 0, clean_res.stderr.decode())

        resume_res = self.harness.run("resume", name)
        self.assertEqual(resume_res.returncode, 0, resume_res.stderr.decode())

        restored_name = self.harness.acquired_slot_name(resume_res)
        restored_dir = self.harness.slot_dir(restored_name)
        restored_git = self._git(restored_dir)
        current_branch = restored_git("rev-parse", "--abbrev-ref", "HEAD").stdout.decode().strip()
        self.assertEqual(current_branch, "lease-branch")

    def test_resume_with_no_archive_allocates_from_remote_branch(self):
        remote_git = lambda *args: subprocess.run(
            [
                self.harness.real_git_bin,
                "-C",
                self.harness.fixture_repo,
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
        remote_git("branch", "remote-feature")

        res = self.harness.run("resume", "remote-feature")
        self.assertEqual(res.returncode, 0, res.stderr.decode())

        restored_name = self.harness.acquired_slot_name(res)
        restored_dir = self.harness.slot_dir(restored_name)
        restored_git = self._git(restored_dir)
        current_branch = restored_git("rev-parse", "--abbrev-ref", "HEAD").stdout.decode().strip()
        self.assertEqual(current_branch, "remote-feature")

    def test_resume_with_neither_archive_nor_remote_branch_refuses(self):
        res = self.harness.run("resume", "nonexistent-branch")
        self.assertNotEqual(res.returncode, 0)
        err = res.stderr.decode()
        self.assertIn("nonexistent-branch", err)

    def test_run_on_collapsed_slot_refuses_and_prints_resume_command(self):
        result = self.harness.run_acquire("run-holder")
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        name = self.harness.acquired_slot_name(result)
        slot_dir = self.harness.slot_dir(name)

        with open(os.path.join(slot_dir, "work.txt"), "w", encoding="utf-8") as handle:
            handle.write("work\n")

        self._expire_lease(name)
        clean_res = self.harness.run("clean")
        self.assertEqual(clean_res.returncode, 0, clean_res.stderr.decode())
        self.assertFalse(os.path.exists(slot_dir))

        run_res = self.harness.run("run", name, "--", "ls")
        self.assertNotEqual(run_res.returncode, 0)
        output = run_res.stderr.decode() + run_res.stdout.decode()
        self.assertIn(f"resume {name}", output)


if __name__ == "__main__":
    unittest.main()
