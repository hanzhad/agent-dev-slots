import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(__file__))

from harness import (
    DevContainerTestCase,
    PROJECT_PREFIX,
    engine_source_lines,
)


class TestSlowCheck(DevContainerTestCase):

    ENV = "GIT_USER_NAME=demo-agent\nGIT_USER_EMAIL=demo-agent@example.com\n"

    def setUp(self):
        super().setUp()
        self.run_dir = os.path.join(self.harness.tmp, "run-dev-container")
        os.makedirs(self.run_dir, mode=0o700, exist_ok=True)
        with open(os.path.join(self.run_dir, "token"), "w", encoding="utf-8") as handle:
            handle.write("test-forge-token\n")

        self.gh_log = os.path.join(self.harness.tmp, "gh.log")
        self.gh_fail_file = os.path.join(self.harness.tmp, "gh.fail")
        self.gh_resp_file = os.path.join(self.harness.tmp, "gh.response.json")
        self._write_gh_stub()

        self.harness.build_fixture_repo(with_parent_env=self.ENV)
        self.rescue_dir = os.path.join(self.harness.state_dir, "rescue")
        os.makedirs(self.rescue_dir, exist_ok=True)

        self._init_shared_remote()

    def _write_gh_stub(self):
        stub = (
            "#!/usr/bin/env bash\n"
            "set -u\n"
            f'log="{self.gh_log}"\n'
            'printf "%s\\n" "$*" >> "$log"\n'
            f'fail="{self.gh_fail_file}"\n'
            'if [[ -f "$fail" ]]; then\n'
            '  echo "gh: network error (unreachable)" >&2\n'
            '  exit 1\n'
            'fi\n'
            f'resp="{self.gh_resp_file}"\n'
            'if [[ -f "$resp" ]]; then\n'
            '  cat "$resp"\n'
            '  exit 0\n'
            'fi\n'
            'echo "[]"\n'
        )
        self.harness._write_stub("gh", stub)

    def _gh_calls(self):
        if not os.path.isfile(self.gh_log):
            return 0
        with open(self.gh_log, encoding="utf-8") as handle:
            return sum(1 for line in handle if line.strip())

    def _set_gh_prs(self, prs):
        with open(self.gh_resp_file, "w", encoding="utf-8") as handle:
            json.dump(prs, handle)

    def _git(self, *args):
        return subprocess.run(
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

    def _init_shared_remote(self):
        self.remote = os.path.join(self.harness.tmp, "shared-remote.git")
        subprocess.run(
            [self.harness.real_git_bin, "init", "--bare", "-q", self.remote],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        subprocess.run(
            [
                self.harness.real_git_bin,
                "-C",
                self.remote,
                "symbolic-ref",
                "HEAD",
                "refs/heads/master",
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self._git("remote", "set-url", "origin", self.remote)
        self._git("push", "-q", "origin", "master")

    def _run_check(self, *args, **extra_env):
        env = {"DEV_CONTAINER_RUN_DIR": self.run_dir}
        env.update(extra_env)
        return self.harness.run("check", *args, port_overrides=env)

    def _run_verdict(self, target, *args, **extra_env):
        env = {"DEV_CONTAINER_RUN_DIR": self.run_dir}
        env.update(extra_env)
        return self.harness.run("verdict", target, *args, port_overrides=env)

    def _create_branch_and_bundle(self, branch_name, file_commits=None, base="master"):
        self._git("checkout", "-q", "-b", branch_name, base)
        if file_commits:
            for fname, content, msg in file_commits:
                fpath = os.path.join(self.harness.fixture_repo, fname)
                os.makedirs(os.path.dirname(fpath), exist_ok=True)
                with open(fpath, "w", encoding="utf-8") as handle:
                    handle.write(content)
                self._git("add", fname)
                self._git("commit", "-q", "-m", msg)
        tip_sha = self._git("rev-parse", f"refs/heads/{branch_name}").stdout.decode().strip()
        bundle_path = os.path.join(self.harness.tmp, f"{branch_name}.bundle")
        self._git("bundle", "create", bundle_path, f"refs/heads/{branch_name}")
        self._git("checkout", "-q", "master")
        return tip_sha, bundle_path

    def _create_multi_branch_bundle(self, branch_specs, base="master"):
        tips = {}
        for bname, file_commits in branch_specs:
            self._git("checkout", "-q", "-b", bname, base)
            for fname, content, msg in file_commits:
                fpath = os.path.join(self.harness.fixture_repo, fname)
                os.makedirs(os.path.dirname(fpath), exist_ok=True)
                with open(fpath, "w", encoding="utf-8") as handle:
                    handle.write(content)
                self._git("add", fname)
                self._git("commit", "-q", "-m", msg)
            tips[bname] = self._git("rev-parse", f"refs/heads/{bname}").stdout.decode().strip()

        bundle_path = os.path.join(self.harness.tmp, "multi.bundle")
        refs = [f"refs/heads/{bname}" for bname, _ in branch_specs]
        self._git("bundle", "create", bundle_path, *refs)
        self._git("checkout", "-q", "master")
        return tips, bundle_path

    def _create_patch_diff(self, relpath, content):
        fpath = os.path.join(self.harness.fixture_repo, relpath)
        os.makedirs(os.path.dirname(fpath), exist_ok=True)
        with open(fpath, "w", encoding="utf-8") as handle:
            handle.write(content)
        self._git("add", "-N", relpath)
        patch = self._git("diff", "HEAD").stdout.decode()
        self._git("reset", "-q", "HEAD")
        if os.path.isfile(fpath):
            os.remove(fpath)
        return patch

    def _plant_archive(
        self,
        lease_name,
        branches=None,
        commits_bundle=None,
        patch_text=None,
        tar_files=None,
        created_at=None,
        project=PROJECT_PREFIX,
        is_restoring=False,
    ):
        if is_restoring:
            archive_dir = os.path.join(self.rescue_dir, project, f".restoring-{lease_name}")
        else:
            archive_dir = os.path.join(self.rescue_dir, project, lease_name)
        os.makedirs(archive_dir, exist_ok=True)

        if branches is None:
            branches = []
        if created_at is None:
            created_at = int(time.time())

        meta = {
            "project": project,
            "lease": lease_name,
            "branches": branches,
            "ignored": [],
            "omitted": [],
            "created_at": created_at,
            "repo_root": self.harness.fixture_repo,
        }
        with open(os.path.join(archive_dir, "meta.json"), "w", encoding="utf-8") as handle:
            json.dump(meta, handle)

        if commits_bundle is not None:
            shutil.copyfile(commits_bundle, os.path.join(archive_dir, "commits.bundle"))

        if patch_text is not None:
            with open(os.path.join(archive_dir, "changes.patch"), "w", encoding="utf-8") as handle:
                handle.write(patch_text)

        if tar_files is not None:
            tar_path = os.path.join(archive_dir, "new-files.tar")
            temp_tar_src = os.path.join(self.harness.tmp, f"tar-src-{lease_name}")
            os.makedirs(temp_tar_src, exist_ok=True)
            for relpath, content in tar_files.items():
                full_p = os.path.join(temp_tar_src, relpath)
                os.makedirs(os.path.dirname(full_p), exist_ok=True)
                mode = "wb" if isinstance(content, bytes) else "w"
                with open(full_p, mode, encoding=None if isinstance(content, bytes) else "utf-8") as handle:
                    handle.write(content)
            subprocess.run(
                ["tar", "-cf", tar_path, "-C", temp_tar_src, "."],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            shutil.rmtree(temp_tar_src, ignore_errors=True)

        return archive_dir

    @staticmethod
    def _file_hash(path):
        with open(path, "rb") as handle:
            return hashlib.sha256(handle.read()).hexdigest()

    # 1. Tip equals merged PR head: archive deleted.
    def test_tip_equals_merged_pr_head_deletes_archive(self):
        branch = "feat-merged-head"
        tip_sha, bundle = self._create_branch_and_bundle(
            branch, [("merged.txt", "merged change\n", "feat commit")]
        )
        self._git("branch", "-D", branch)

        archive_dir = self._plant_archive(
            "slot-merged-pr",
            branches=[branch],
            commits_bundle=bundle,
        )
        self.assertTrue(os.path.isdir(archive_dir))

        self._set_gh_prs([
            {
                "headRefOid": tip_sha,
                "state": "MERGED",
                "number": 42,
                "mergeCommit": None,
            }
        ])

        res = self._run_check()
        self.assertEqual(res.returncode, 0, res.stderr.decode())
        self.assertFalse(
            os.path.exists(archive_dir),
            f"archive was not deleted when tip equaled merged PR headRefOid: {archive_dir}",
        )

    # 2. Branch on the remote: archive deleted.
    def test_branch_on_remote_deletes_archive(self):
        branch = "feat-on-remote"
        tip_sha, bundle = self._create_branch_and_bundle(
            branch, [("remote_work.txt", "remote data\n", "add remote work")]
        )
        self._git("push", "-q", "origin", branch)
        self._git("branch", "-D", branch)

        archive_dir = self._plant_archive(
            "slot-remote",
            branches=[branch],
            commits_bundle=bundle,
        )
        self.assertTrue(os.path.isdir(archive_dir))

        res = self._run_check()
        self.assertEqual(res.returncode, 0, res.stderr.decode())
        self.assertFalse(
            os.path.exists(archive_dir),
            f"archive was not deleted when branch exists on remote: {archive_dir}",
        )

    # 3. Squash-merged commits (different hash, same content): archive deleted.
    def test_squash_merged_commits_deletes_archive(self):
        branch = "feat-squash-work"
        tip_sha, bundle = self._create_branch_and_bundle(
            branch,
            [
                ("part1.txt", "first part of change\n", "commit part 1"),
                ("part2.txt", "second part of change\n", "commit part 2"),
            ],
        )

        self._git("checkout", "-q", "master")
        self._git("merge", "-q", "--squash", branch)
        self._git("commit", "-q", "-m", "squashed feature commit")
        squashed_sha = self._git("rev-parse", "master").stdout.decode().strip()
        self.assertNotEqual(tip_sha, squashed_sha)
        self._git("push", "-q", "origin", "master")
        self._git("branch", "-D", branch)

        archive_dir = self._plant_archive(
            "slot-squash",
            branches=[branch],
            commits_bundle=bundle,
        )
        self.assertTrue(os.path.isdir(archive_dir))

        res = self._run_check()
        self.assertEqual(res.returncode, 0, res.stderr.decode())
        self.assertFalse(
            os.path.exists(archive_dir),
            f"archive was not deleted when commits were squash-merged into default branch: {archive_dir}",
        )

    # 4. One unproven branch among several: nothing deleted.
    def test_one_unproven_branch_among_several_keeps_archive(self):
        branch_proven = "branch-proven"
        branch_unproven = "branch-unproven"
        tips, bundle = self._create_multi_branch_bundle(
            [
                (branch_proven, [("proven.txt", "proven\n", "proven commit")]),
                (branch_unproven, [("unproven.txt", "unproven\n", "unproven commit")]),
            ]
        )
        self._git("push", "-q", "origin", branch_proven)
        self._git("branch", "-D", branch_proven)
        self._git("branch", "-D", branch_unproven)

        archive_dir = self._plant_archive(
            "slot-multi-branch",
            branches=[branch_proven, branch_unproven],
            commits_bundle=bundle,
        )
        self.assertTrue(os.path.isdir(archive_dir))

        res = self._run_check()
        self.assertEqual(res.returncode, 0, res.stderr.decode())
        self.assertTrue(
            os.path.isdir(archive_dir),
            "archive was deleted despite containing an unproven branch",
        )
        out = res.stdout.decode() + res.stderr.decode()
        self.assertIn(
            branch_unproven,
            out,
            f"expected report to name unproven branch: {out}",
        )

    # 5. Patch already in the default branch counts as proven; one not in it does not.
    def test_patch_in_default_branch_deletes_archive(self):
        relpath = "applied_feature.txt"
        patch_text = self._create_patch_diff(relpath, "applied content\n")

        # Apply and commit to master and push to origin
        fpath = os.path.join(self.harness.fixture_repo, relpath)
        with open(fpath, "w", encoding="utf-8") as handle:
            handle.write("applied content\n")
        self._git("add", relpath)
        self._git("commit", "-q", "-m", "apply patch directly to master")
        self._git("push", "-q", "origin", "master")

        archive_dir = self._plant_archive(
            "slot-patch-proven",
            branches=[],
            patch_text=patch_text,
        )
        self.assertTrue(os.path.isdir(archive_dir))

        res = self._run_check()
        self.assertEqual(res.returncode, 0, res.stderr.decode())
        self.assertFalse(
            os.path.exists(archive_dir),
            f"archive with patch already in default branch was not deleted: {archive_dir}",
        )

    def test_patch_not_in_default_branch_keeps_archive(self):
        relpath = "unapplied_feature.txt"
        patch_text = self._create_patch_diff(relpath, "unapplied content not on master\n")

        archive_dir = self._plant_archive(
            "slot-patch-unproven",
            branches=[],
            patch_text=patch_text,
        )
        self.assertTrue(os.path.isdir(archive_dir))

        res = self._run_check()
        self.assertEqual(res.returncode, 0, res.stderr.decode())
        self.assertTrue(
            os.path.isdir(archive_dir),
            f"archive with unproven patch was deleted: {archive_dir}",
        )
        out = res.stdout.decode() + res.stderr.decode()
        self.assertIn(
            "slot-patch-unproven",
            out,
            f"expected report to name unproven archive: {out}",
        )

    def test_new_files_tar_all_in_default_branch_deletes_archive(self):
        relpath = "new_tracked_file.txt"
        content = "identical blob content\n"
        fpath = os.path.join(self.harness.fixture_repo, relpath)
        with open(fpath, "w", encoding="utf-8") as handle:
            handle.write(content)
        self._git("add", relpath)
        self._git("commit", "-q", "-m", "commit untracked file to master")
        self._git("push", "-q", "origin", "master")

        archive_dir = self._plant_archive(
            "slot-tar-proven",
            branches=[],
            tar_files={relpath: content},
        )
        self.assertTrue(os.path.isdir(archive_dir))

        res = self._run_check()
        self.assertEqual(res.returncode, 0, res.stderr.decode())
        self.assertFalse(
            os.path.exists(archive_dir),
            f"archive with all tar files in default branch was not deleted: {archive_dir}",
        )

    def test_new_files_tar_missing_or_differing_keeps_archive(self):
        archive_dir = self._plant_archive(
            "slot-tar-unproven",
            branches=[],
            tar_files={"untracked_missing.txt": "never in origin\n"},
        )
        self.assertTrue(os.path.isdir(archive_dir))

        res = self._run_check()
        self.assertEqual(res.returncode, 0, res.stderr.decode())
        self.assertTrue(
            os.path.isdir(archive_dir),
            f"archive with missing tar file was deleted: {archive_dir}",
        )
        out = res.stdout.decode() + res.stderr.decode()
        self.assertIn(
            "slot-tar-unproven",
            out,
            f"expected report to name unproven archive: {out}",
        )

    # 6. GitHub unreachable: skipped, nothing deleted, reported.
    def test_github_unreachable_skips_deletion_and_reports(self):
        branch = "feat-need-gh"
        tip_sha, bundle = self._create_branch_and_bundle(
            branch, [("need_gh.txt", "unpushed\n", "need gh commit")]
        )
        self._git("branch", "-D", branch)

        archive_dir = self._plant_archive(
            "slot-gh-unreachable",
            branches=[branch],
            commits_bundle=bundle,
        )
        self.assertTrue(os.path.isdir(archive_dir))

        with open(self.gh_fail_file, "w", encoding="utf-8") as handle:
            handle.write("fail\n")

        res = self._run_check()
        self.assertEqual(res.returncode, 0, res.stderr.decode())
        self.assertTrue(
            os.path.isdir(archive_dir),
            "archive was deleted when GitHub was unreachable",
        )
        out = res.stdout.decode() + res.stderr.decode()
        self.assertTrue(
            "unreachable" in out.lower() or "gh refused" in out.lower() or "failed" in out.lower(),
            f"expected GitHub unreachable error in check output: {out}",
        )
        self.assertIn(
            branch,
            out,
            f"expected report to name branch: {out}",
        )

    # 7. Empty rescue/: zero network calls (network stubbed to fail).
    def test_empty_rescue_zero_network_calls(self):
        shutil.rmtree(self.rescue_dir, ignore_errors=True)
        os.makedirs(self.rescue_dir, exist_ok=True)

        with open(self.gh_fail_file, "w", encoding="utf-8") as handle:
            handle.write("fail if called\n")

        # Plant a lease to ensure engine has a registered project
        lease_path = self.harness.lease_path("slot-empty-test")
        os.makedirs(os.path.dirname(lease_path), exist_ok=True)
        with open(lease_path, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "holder": "test",
                    "project": f"{PROJECT_PREFIX}-slot-empty-test",
                    "since": int(time.time()),
                    "key": "/dev/null",
                    "ssh_port": 39220,
                    "repo_root": self.harness.fixture_repo,
                },
                handle,
            )

        if os.path.isfile(self.harness.git_call_log):
            os.remove(self.harness.git_call_log)

        res = self._run_check()
        self.assertEqual(res.returncode, 0, res.stderr.decode())
        self.assertEqual(
            self._gh_calls(),
            0,
            "gh was called when rescue/ was empty",
        )
        network_git_calls = [
            c for c in self.harness.git_calls()
            if "fetch" in c or "ls-remote" in c
        ]
        self.assertEqual(
            network_git_calls,
            [],
            f"remote git network calls were made when rescue/ was empty: {network_git_calls}",
        )

    # 8. A .restoring-<lease> directory and a concurrent run are left alone.
    def test_restoring_directory_is_left_alone(self):
        branch = "feat-restoring-branch"
        tip_sha, bundle = self._create_branch_and_bundle(
            branch, [("restoring.txt", "restoring work\n", "restoring commit")]
        )
        self._git("push", "-q", "origin", branch)
        self._git("branch", "-D", branch)

        restoring_dir = self._plant_archive(
            "slot-restoring-test",
            branches=[branch],
            commits_bundle=bundle,
            is_restoring=True,
        )
        self.assertTrue(os.path.isdir(restoring_dir))

        res = self._run_check()
        self.assertEqual(res.returncode, 0, res.stderr.decode())
        self.assertTrue(
            os.path.isdir(restoring_dir),
            f".restoring-<lease> directory was removed or modified: {restoring_dir}",
        )

    def test_concurrent_check_lock_prevents_overlap(self):
        branch = "feat-lock-branch"
        tip_sha, bundle = self._create_branch_and_bundle(
            branch, [("lock_work.txt", "lock work\n", "lock commit")]
        )
        self._git("push", "-q", "origin", branch)
        self._git("branch", "-D", branch)

        archive_dir = self._plant_archive(
            "slot-locked",
            branches=[branch],
            commits_bundle=bundle,
        )
        self.assertTrue(os.path.isdir(archive_dir))

        check_lock = os.path.join(self.harness.state_dir, ".check-lock")
        with open(check_lock, "w", encoding="utf-8") as handle:
            handle.write("concurrent-run-in-progress\n")

        res = self._run_check()
        self.assertTrue(
            os.path.isdir(archive_dir),
            "archive was deleted while check lock was held",
        )
        out = res.stdout.decode() + res.stderr.decode()
        self.assertIn(
            "lock",
            out.lower(),
            f"expected check output to note concurrent run or lock: {out}",
        )

    # 9. The verdict step writes verdict.md only (a test fails if it deletes or edits anything else).
    def test_verdict_writes_verdict_md_only(self):
        branch = "feat-verdict"
        tip_sha, bundle = self._create_branch_and_bundle(
            branch, [("verdict.txt", "verdict data\n", "verdict commit")]
        )
        self._git("branch", "-D", branch)

        patch_text = self._create_patch_diff("local_edit.txt", "local edit data\n")
        archive_dir = self._plant_archive(
            "slot-verdict-test",
            branches=[branch],
            commits_bundle=bundle,
            patch_text=patch_text,
            tar_files={"untracked.txt": "untracked content\n"},
        )

        files_to_check = ["meta.json", "commits.bundle", "changes.patch", "new-files.tar"]
        before_hashes = {}
        for fname in files_to_check:
            fpath = os.path.join(archive_dir, fname)
            self.assertTrue(os.path.isfile(fpath), f"expected {fname} to exist before verdict")
            before_hashes[fname] = self._file_hash(fpath)

        res = self._run_verdict(f"{PROJECT_PREFIX}/slot-verdict-test")
        self.assertEqual(res.returncode, 0, res.stderr.decode())

        verdict_path = os.path.join(archive_dir, "verdict.md")
        self.assertTrue(
            os.path.isfile(verdict_path),
            f"verdict.md was not written inside archive directory: {archive_dir}",
        )
        self.assertGreater(
            os.path.getsize(verdict_path),
            0,
            "verdict.md is empty",
        )

        for fname in files_to_check:
            fpath = os.path.join(archive_dir, fname)
            self.assertTrue(os.path.isfile(fpath), f"{fname} was removed by verdict")
            self.assertEqual(
                self._file_hash(fpath),
                before_hashes[fname],
                f"{fname} was modified by verdict",
            )

    def test_verdict_accepts_archive_path_directly(self):
        branch = "feat-verdict-direct"
        tip_sha, bundle = self._create_branch_and_bundle(
            branch, [("verdict_dir.txt", "data\n", "commit")]
        )
        self._git("branch", "-D", branch)

        archive_dir = self._plant_archive(
            "slot-verdict-direct",
            branches=[branch],
            commits_bundle=bundle,
        )

        res = self._run_verdict(archive_dir)
        self.assertEqual(res.returncode, 0, res.stderr.decode())
        verdict_path = os.path.join(archive_dir, "verdict.md")
        self.assertTrue(
            os.path.isfile(verdict_path),
            f"verdict.md was not written when verdict was given archive path: {archive_dir}",
        )

    # 10. No age threshold in the code.
    def test_fresh_unproven_archive_reported_immediately(self):
        branch = "feat-fresh-unproven"
        tip_sha, bundle = self._create_branch_and_bundle(
            branch, [("fresh.txt", "fresh\n", "fresh commit")]
        )
        self._git("branch", "-D", branch)

        now = int(time.time())
        archive_dir = self._plant_archive(
            "slot-fresh",
            branches=[branch],
            commits_bundle=bundle,
            created_at=now,
        )
        self.assertTrue(os.path.isdir(archive_dir))

        res = self._run_check()
        self.assertEqual(res.returncode, 0, res.stderr.decode())
        self.assertTrue(
            os.path.isdir(archive_dir),
            "fresh unproven archive was deleted",
        )
        out = res.stdout.decode() + res.stderr.decode()
        self.assertIn(
            "slot-fresh",
            out,
            f"expected report to name fresh unproven archive immediately: {out}",
        )

    def test_old_unproven_archive_kept_not_deleted_by_age(self):
        branch = "feat-old-unproven"
        tip_sha, bundle = self._create_branch_and_bundle(
            branch, [("old.txt", "old\n", "old commit")]
        )
        self._git("branch", "-D", branch)

        old_stamp = int(time.time()) - 100 * 86400
        archive_dir = self._plant_archive(
            "slot-old",
            branches=[branch],
            commits_bundle=bundle,
            created_at=old_stamp,
        )
        self.assertTrue(os.path.isdir(archive_dir))

        for root, dirs, files in os.walk(archive_dir):
            for d in dirs:
                p = os.path.join(root, d)
                os.utime(p, (old_stamp, old_stamp))
            for f in files:
                p = os.path.join(root, f)
                os.utime(p, (old_stamp, old_stamp))
        os.utime(archive_dir, (old_stamp, old_stamp))

        res = self._run_check()
        self.assertEqual(res.returncode, 0, res.stderr.decode())
        self.assertTrue(
            os.path.isdir(archive_dir),
            "old unproven archive was deleted by age",
        )

    def test_no_age_threshold_in_code(self):
        lines = engine_source_lines()
        forbidden_patterns = [
            re.compile(r"find\s+.*-mtime\s+\+\d+.*-delete"),
            re.compile(r"ARCHIVE_MAX_AGE"),
            re.compile(r"\b2592000\b"),
            re.compile(r"\b30\s*\*\s*86400\b"),
        ]
        violations = []
        for line in lines:
            # Skip comments
            trimmed = line.strip()
            if trimmed.startswith("#"):
                continue
            for pat in forbidden_patterns:
                if pat.search(line):
                    violations.append(f"{pat.pattern}: {line}")
        self.assertEqual(
            violations,
            [],
            f"found age threshold gating archive deletion in source code: {violations}",
        )

    # Auto-review findings tests (PR #45)

    # Finding 1: Uncommitted patch content matching working tree is unproven if not in origin/master
    def test_patch_matching_uncommitted_working_tree_not_proven(self):
        relpath = "tracked_modified.txt"
        fpath = os.path.join(self.harness.fixture_repo, relpath)
        with open(fpath, "w", encoding="utf-8") as handle:
            handle.write("initial committed content\n")
        self._git("add", relpath)
        self._git("commit", "-q", "-m", "commit tracked base")
        self._git("push", "-q", "origin", "master")

        with open(fpath, "a", encoding="utf-8") as handle:
            handle.write("uncommitted working tree edit\n")
        patch_text = self._git("diff", "HEAD").stdout.decode()
        self.addCleanup(self._git, "checkout", "--", relpath)

        archive_dir = self._plant_archive(
            "slot-uncommitted-patch",
            branches=[],
            patch_text=patch_text,
        )
        self.assertTrue(os.path.isdir(archive_dir))

        res = self._run_check()
        self.assertEqual(res.returncode, 0, res.stderr.decode())
        self.assertTrue(
            os.path.isdir(archive_dir),
            "archive was deleted because patch matched uncommitted working tree instead of origin/master index",
        )
        out = res.stdout.decode() + res.stderr.decode()
        self.assertIn(
            "slot-uncommitted-patch",
            out,
            f"expected report to name unproven archive: {out}",
        )

    # Finding 2: Archive with commits bundle but empty branches list must remain unproven
    def test_bundle_with_empty_branches_list_remains_unproven(self):
        branch = "feat-unlisted-bundle"
        tip_sha, bundle = self._create_branch_and_bundle(
            branch, [("unlisted.txt", "unlisted unpushed work\n", "unlisted commit")]
        )
        self._git("branch", "-D", branch)

        archive_dir = self._plant_archive(
            "slot-empty-branches",
            branches=[],
            commits_bundle=bundle,
        )
        self.assertTrue(os.path.isdir(archive_dir))

        res = self._run_check()
        self.assertEqual(res.returncode, 0, res.stderr.decode())
        self.assertTrue(
            os.path.isdir(archive_dir),
            "archive was deleted despite containing unproven commits bundle when branches list was empty",
        )
        out = res.stdout.decode() + res.stderr.decode()
        self.assertIn(
            "slot-empty-branches",
            out,
            f"expected report to name unproven archive: {out}",
        )

    # Finding 3: Branch in meta.json resolved against refs/remotes/origin/ when absent from bundle
    def test_branch_resolved_from_remote_tracking_ref_when_not_in_bundle(self):
        branch = "feat-remote-only"
        self._git("checkout", "-q", "-b", branch, "master")
        fpath = os.path.join(self.harness.fixture_repo, "remote_only.txt")
        with open(fpath, "w", encoding="utf-8") as handle:
            handle.write("remote only content\n")
        self._git("add", "remote_only.txt")
        self._git("commit", "-q", "-m", "remote only commit")
        self._git("checkout", "-q", "master")
        self._git("merge", "-q", branch)
        self._git("push", "-q", "origin", branch, "master")
        self._git("branch", "-D", branch)

        archive_dir = self._plant_archive(
            "slot-remote-ref-branch",
            branches=[branch],
            commits_bundle=None,
        )
        self.assertTrue(os.path.isdir(archive_dir))

        res = self._run_check()
        self.assertEqual(res.returncode, 0, res.stderr.decode())
        self.assertFalse(
            os.path.exists(archive_dir),
            f"archive was not deleted when branch was present at refs/remotes/origin/{branch}: {archive_dir}",
        )

    def _stub_gh_branch(self, branch, prs):
        body = json.dumps(prs)
        q = chr(39)
        lines = [
            "#!/usr/bin/env bash",
            "set -u",
            f'log="{self.gh_log}"',
            'printf "%s\\n" "$*" >> "$log"',
            'head="" prev=""',
            'for a in "$@"; do',
            '  [[ "$prev" == "--head" ]] && head="$a"',
            '  prev="$a"',
            'done',
            'case "$head" in',
            f"  {branch})",
            f"    echo {q}{body}{q}",
            "    ;;",
            "  *)",
            f"    echo {q}[]{q}",
            "    ;;",
            "esac",
        ]
        self.harness._write_stub("gh", "\n".join(lines) + "\n")

    def test_bundle_with_head_ref_proven_via_merged_pr_deleted(self):
        branch = "feat-pr-head-bundle"
        self._git("checkout", "-q", "-b", branch, "master")
        fpath = os.path.join(self.harness.fixture_repo, "pr_head_bundle.txt")
        with open(fpath, "w", encoding="utf-8") as handle:
            handle.write("pr head bundle data\n")
        self._git("add", "pr_head_bundle.txt")
        self._git("commit", "-q", "-m", "pr head bundle commit")
        tip_sha = self._git("rev-parse", "HEAD").stdout.decode().strip()

        bundle_path = os.path.join(self.harness.tmp, f"{branch}.bundle")
        self._git("bundle", "create", bundle_path, "HEAD", f"refs/heads/{branch}")
        self._git("checkout", "-q", "master")
        self._git("branch", "-D", branch)

        self._stub_gh_branch(
            branch,
            [
                {
                    "headRefOid": tip_sha,
                    "state": "MERGED",
                    "number": 88,
                    "mergeCommit": None,
                }
            ],
        )
        self.addCleanup(self._write_gh_stub)

        archive_dir = self._plant_archive(
            "slot-pr-head-bundle",
            branches=[branch],
            commits_bundle=bundle_path,
        )
        self.assertTrue(os.path.isdir(archive_dir))

        res = self._run_check()
        self.assertEqual(res.returncode, 0, res.stderr.decode())
        self.assertFalse(
            os.path.exists(archive_dir),
            f"archive was not deleted when bundle contained HEAD ref: {archive_dir}",
        )


if __name__ == "__main__":
    unittest.main()
