import json
import os
import subprocess
import time
import unittest

from harness import (
    DevContainerTestCase,
    PROJECT_PREFIX,
    SCRIPT_REL,
    project_env_text,
)

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
ENGINE = os.path.join(REPO_ROOT, SCRIPT_REL)


def output_of(result):
    return result.stdout.decode() + result.stderr.decode()


class TestLocalGitOnly(DevContainerTestCase):

    def engine_run(self, *args, caller_pwd=None, **extra_env):
        overrides = dict(extra_env)
        if caller_pwd is not None:
            overrides["DEV_CONTAINER_CALLER_PWD"] = caller_pwd
        return subprocess.run(
            ["bash", ENGINE, *args],
            cwd=self.harness.tmp,
            env=self.harness._env(overrides),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=120,
        )

    def _git(self, repo_or_slot):
        return lambda *args: subprocess.run(
            [
                self.harness.real_git_bin,
                "-C",
                repo_or_slot,
                "-c",
                "commit.gpgsign=false",
                "-c",
                "user.name=fixture",
                "-c",
                "user.email=fixture@example.com",
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

    def make_local_repo(self, name="local-repo", branch="main", project=PROJECT_PREFIX):
        repo_dir = os.path.join(self.harness.tmp, name)
        os.makedirs(os.path.join(repo_dir, ".devcontainer"), exist_ok=True)
        os.makedirs(os.path.join(repo_dir, ".local"), exist_ok=True)
        with open(os.path.join(repo_dir, ".gitignore"), "w", encoding="utf-8") as handle:
            handle.write(".env\n.slot-holder\ndocs/*\n")
        with open(
            os.path.join(repo_dir, ".devcontainer", "docker-compose.yml"), "w", encoding="utf-8"
        ) as handle:
            handle.write("# fixture placeholder\n")
        with open(os.path.join(repo_dir, ".local", ".gitkeep"), "w", encoding="utf-8") as handle:
            pass
        with open(os.path.join(repo_dir, "README.md"), "w", encoding="utf-8") as handle:
            handle.write(f"{name}\n")
        with open(os.path.join(repo_dir, "project.env"), "w", encoding="utf-8") as handle:
            handle.write(project_env_text(PROJECT=project, DEFAULT_BRANCH=branch))

        git = self._git(repo_dir)
        git("init", "-q")
        git("config", "user.email", "fixture@example.com")
        git("config", "user.name", "fixture")
        git("config", "receive.denyCurrentBranch", "updateInstead")
        git("checkout", "-q", "-b", branch)
        git("add", "-A")
        git("commit", "-q", "-m", "initial")
        # Explicitly NO remote added
        return repo_dir

    def test_acquire_in_non_git_dir_refuses_and_suggests_git_init(self):
        plain_dir = os.path.join(self.harness.tmp, "plain-dir")
        os.makedirs(plain_dir, exist_ok=True)

        res = self.engine_run("acquire", "--for", "worker", caller_pwd=plain_dir)
        text = output_of(res)

        self.assertNotEqual(res.returncode, 0, text)
        self.assertNotIn("Usage:", text)
        self.assertRegex(text, r"(?i)git init|create (the )?repository first")

    def test_acquire_on_local_repo_with_no_remote(self):
        repo = self.make_local_repo(name="local-repo-acquire", branch="main")

        res = self.engine_run("acquire", "--for", "worker", caller_pwd=repo)
        text = output_of(res)
        self.assertEqual(res.returncode, 0, text)

        slot_name = self.harness.acquired_slot_name(res)
        slot_dir = self.harness.slot_dir(slot_name)
        self.assertTrue(os.path.isdir(slot_dir), f"slot dir not found: {slot_dir}")

        lease_path = self.harness.lease_path(slot_name)
        self.assertTrue(os.path.isfile(lease_path), f"lease file not found: {lease_path}")

        slot_git = self._git(slot_dir)
        slot_origin = slot_git("remote", "get-url", "origin").stdout.decode().strip()
        self.assertEqual(os.path.realpath(slot_origin), os.path.realpath(repo))

    def test_release_refuses_when_work_held_on_local_repo_with_no_remote(self):
        repo = self.make_local_repo(name="local-repo-held", branch="main")

        res = self.engine_run("acquire", "--for", "worker", caller_pwd=repo)
        self.assertEqual(res.returncode, 0, output_of(res))
        slot_name = self.harness.acquired_slot_name(res)
        slot_dir = self.harness.slot_dir(slot_name)

        uncommitted_path = os.path.join(slot_dir, "uncommitted.txt")
        with open(uncommitted_path, "w", encoding="utf-8") as handle:
            handle.write("uncommitted work\n")

        res_uncommitted = self.engine_run("release", slot_name, "--for", "worker", caller_pwd=repo)
        text_uncommitted = output_of(res_uncommitted)
        self.assertNotEqual(res_uncommitted.returncode, 0, text_uncommitted)
        self.assertIn("refusing to release", text_uncommitted)
        os.remove(uncommitted_path)

        slot_git = self._git(slot_dir)
        slot_git("checkout", "-q", "-b", "feat/held-work")
        held_file = os.path.join(slot_dir, "held.txt")
        with open(held_file, "w", encoding="utf-8") as handle:
            handle.write("held commit work\n")
        slot_git("add", "held.txt")
        slot_git("commit", "-q", "-m", "held commit")

        res_held = self.engine_run("release", slot_name, "--for", "worker", caller_pwd=repo)
        text_held = output_of(res_held)
        self.assertNotEqual(res_held.returncode, 0, text_held)
        self.assertIn("refusing to release", text_held)

    def test_release_succeeds_when_work_delivered_to_local_repo(self):
        repo = self.make_local_repo(name="local-repo-delivered", branch="main")

        res = self.engine_run("acquire", "--for", "worker", caller_pwd=repo)
        self.assertEqual(res.returncode, 0, output_of(res))
        slot_name = self.harness.acquired_slot_name(res)
        slot_dir = self.harness.slot_dir(slot_name)

        slot_git = self._git(slot_dir)
        slot_git("checkout", "-q", "-b", "feat/delivered-work")
        work_file = os.path.join(slot_dir, "delivered.txt")
        with open(work_file, "w", encoding="utf-8") as handle:
            handle.write("delivered work\n")
        slot_git("add", "delivered.txt")
        slot_git("commit", "-q", "-m", "work to deliver")

        repo_git = self._git(repo)
        repo_git("fetch", "-q", slot_dir, "feat/delivered-work")
        repo_git("merge", "-q", "--no-edit", "FETCH_HEAD")

        slot_git("checkout", "-q", "main")
        slot_git("fetch", "-q", "origin")
        slot_git("reset", "-q", "--hard", "origin/main")
        slot_git("branch", "-q", "-D", "feat/delivered-work")

        res_release = self.engine_run("release", slot_name, "--for", "worker", caller_pwd=repo)
        text_release = output_of(res_release)
        self.assertEqual(res_release.returncode, 0, text_release)

        lease_path = self.harness.lease_path(slot_name)
        self.assertFalse(os.path.exists(lease_path), f"lease file still exists: {lease_path}")
        repo_git("fsck")

    def test_archive_and_resume_on_local_repo_with_no_remote(self):
        repo = self.make_local_repo(name="local-repo-resume", branch="main")

        res_acq = self.engine_run("acquire", "--for", "worker", caller_pwd=repo)
        self.assertEqual(res_acq.returncode, 0, output_of(res_acq))
        slot_name = self.harness.acquired_slot_name(res_acq)
        slot_dir = self.harness.slot_dir(slot_name)

        slot_git = self._git(slot_dir)
        slot_git("checkout", "-q", "-b", "feat/local-work")

        feat_file = os.path.join(slot_dir, "feature.txt")
        with open(feat_file, "w", encoding="utf-8") as handle:
            handle.write("feature commit content\n")
        slot_git("add", "feature.txt")
        slot_git("commit", "-q", "-m", "feature commit")

        edit_file = os.path.join(slot_dir, "README.md")
        with open(edit_file, "a", encoding="utf-8") as handle:
            handle.write("uncommitted local edit\n")

        self._expire_lease(slot_name)

        res_reap = self.engine_run("reap", caller_pwd=repo)
        self.assertEqual(res_reap.returncode, 0, output_of(res_reap))

        archive_dir = os.path.join(self.harness.state_dir, "rescue", PROJECT_PREFIX, slot_name)
        self.assertTrue(os.path.isdir(archive_dir), f"archive dir missing: {archive_dir}")

        bundle_path = os.path.join(archive_dir, "commits.bundle")
        patch_path = os.path.join(archive_dir, "changes.patch")
        self.assertTrue(os.path.isfile(bundle_path), f"commits.bundle missing in {archive_dir}")
        self.assertTrue(os.path.isfile(patch_path), f"changes.patch missing in {archive_dir}")

        res_resume = self.engine_run("resume", "feat/local-work", caller_pwd=repo)
        text_resume = output_of(res_resume)
        self.assertEqual(res_resume.returncode, 0, text_resume)

        restored_name = self.harness.acquired_slot_name(res_resume)
        restored_dir = self.harness.slot_dir(restored_name)
        self.assertTrue(os.path.isdir(restored_dir), f"restored dir missing: {restored_dir}")

        restored_git = self._git(restored_dir)
        current_branch = restored_git("rev-parse", "--abbrev-ref", "HEAD").stdout.decode().strip()
        self.assertEqual(current_branch, "feat/local-work")

        restored_feat = os.path.join(restored_dir, "feature.txt")
        self.assertTrue(os.path.isfile(restored_feat), "feature.txt not restored from bundle")
        with open(restored_feat, "r", encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "feature commit content\n")

        restored_edit = os.path.join(restored_dir, "README.md")
        with open(restored_edit, "r", encoding="utf-8") as handle:
            self.assertIn("uncommitted local edit", handle.read())

    def test_resume_existing_local_branch_with_no_archive_on_local_repo(self):
        repo = self.make_local_repo(name="local-repo-branch", branch="main")

        repo_git = self._git(repo)
        repo_git("checkout", "-q", "-b", "feat/existing-local")
        local_file = os.path.join(repo, "local_branch.txt")
        with open(local_file, "w", encoding="utf-8") as handle:
            handle.write("local branch content\n")
        repo_git("add", "local_branch.txt")
        repo_git("commit", "-q", "-m", "local branch commit")
        repo_git("checkout", "-q", "main")

        res_resume = self.engine_run("resume", "feat/existing-local", caller_pwd=repo)
        text_resume = output_of(res_resume)
        self.assertEqual(res_resume.returncode, 0, text_resume)

        slot_name = self.harness.acquired_slot_name(res_resume)
        slot_dir = self.harness.slot_dir(slot_name)
        self.assertTrue(os.path.isdir(slot_dir), f"slot dir missing: {slot_dir}")

        slot_git = self._git(slot_dir)
        current_branch = slot_git("rev-parse", "--abbrev-ref", "HEAD").stdout.decode().strip()
        self.assertEqual(current_branch, "feat/existing-local")

        branch_file = os.path.join(slot_dir, "local_branch.txt")
        self.assertTrue(os.path.isfile(branch_file), "local_branch.txt missing in slot")
        with open(branch_file, "r", encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "local branch content\n")

    def test_existing_alternates_left_intact_on_acquire_and_release(self):
        base_repo = self.make_local_repo(name="base-shared-repo", branch="main")

        repo = os.path.join(self.harness.tmp, "local-repo-alternates")
        subprocess.run(
            [self.harness.real_git_bin, "clone", "--shared", "-b", "main", base_repo, repo],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        repo_git = self._git(repo)
        repo_git("remote", "remove", "origin")
        with open(os.path.join(repo, ".devcontainer", "docker-compose.yml"), "w", encoding="utf-8") as f:
            f.write("# fixture placeholder\n")
        with open(os.path.join(repo, "project.env"), "w", encoding="utf-8") as f:
            f.write(project_env_text(PROJECT=PROJECT_PREFIX, DEFAULT_BRANCH="main"))

        alt_file = os.path.join(repo, ".git", "objects", "info", "alternates")
        self.assertTrue(os.path.isfile(alt_file), f"alternates file missing in {repo}")
        with open(alt_file, "r", encoding="utf-8") as f:
            initial_alternates = f.read()
        self.assertTrue(initial_alternates.strip(), "alternates file was empty")

        res_acq = self.engine_run("acquire", "--for", "worker", caller_pwd=repo)
        self.assertEqual(res_acq.returncode, 0, output_of(res_acq))
        slot_name = self.harness.acquired_slot_name(res_acq)
        slot_dir = self.harness.slot_dir(slot_name)

        with open(alt_file, "r", encoding="utf-8") as f:
            current_alternates = f.read()
        self.assertEqual(current_alternates, initial_alternates)
        self.assertNotIn(slot_dir, current_alternates)

        res_release = self.engine_run("release", slot_name, "--for", "worker", caller_pwd=repo)
        self.assertEqual(res_release.returncode, 0, output_of(res_release))

        self.assertTrue(os.path.isfile(alt_file), f"alternates file was removed: {alt_file}")
        with open(alt_file, "r", encoding="utf-8") as f:
            final_alternates = f.read()
        self.assertEqual(final_alternates, initial_alternates)

        fsck_res = repo_git("fsck")
        self.assertEqual(fsck_res.returncode, 0, fsck_res.stderr.decode())


if __name__ == "__main__":
    unittest.main()
