
import json
import os
import shutil
import subprocess
import time
import unittest

from harness import (
    DOCS_REPO_URL,
    DevContainerHarness,
    DevContainerTestCase,
    ListVerdictTestCase,
    PROJECT_PREFIX,
    TEST_SSH_PORT,
)


class BranchDeliveryAsksReachability(DevContainerTestCase):

    def setUp(self):
        super().setUp()
        self.harness._write_stub(
            "gh",
            "#!/usr/bin/env bash\n"
            'head="" prev=""\n'
            'for a in "$@"; do\n'
            '  [[ "$prev" == "--head" ]] && head="$a"\n'
            '  prev="$a"\n'
            "done\n"
            'case "$head" in\n'
            "  pr-mismatch|ahead-but-landed)\n"
            '    echo \'[{"headRefOid":"deadbeefdeadbeefdeadbeefdeadbeefdeadbeef","state":"MERGED","number":42}]\'\n'
            "    ;;\n"
            "  *)\n"
            "    echo '[]'\n"
            "    ;;\n"
            "esac\n",
        )
        self.harness.build_fixture_repo()
        self.harness.make_bare_slot(
            "delivery", local_identity=("fixture", "fixture@example.com")
        )
        self.slot = self.harness.slot_dir("delivery")
        self.remote = os.path.join(self.harness.tmp, "delivery-remote.git")
        subprocess.run(
            ["git", "init", "--bare", "-q", self.remote],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self.git = lambda *args: subprocess.run(
            [self.harness.real_git_bin, "-C", self.slot, "-c", "commit.gpgsign=false", *args],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self.git("remote", "set-url", "origin", self.remote)
        with open(os.path.join(self.slot, "README.md"), "w", encoding="utf-8") as handle:
            handle.write("delivery fixture\n")
        self.git("add", "README.md")
        self.git("commit", "-q", "-m", "base")
        self.git("branch", "-M", "master")
        self.git("push", "-q", "origin", "master")
        self.git("fetch", "-q", "origin")
        with open(os.path.join(self.slot, "leftover.txt"), "w", encoding="utf-8") as handle:
            handle.write("uncommitted, just to force the report\n")
        lease_path = self.harness.lease_path("delivery")
        os.makedirs(os.path.dirname(lease_path), exist_ok=True)
        with open(lease_path, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "holder": "test",
                    "project": PROJECT_PREFIX + "-delivery",
                    "since": int(time.time()),
                    "ttl_seconds": 14400,
                    "key": os.path.join(self.harness.tmp, "delivery-fixture-key"),
                    "ssh_port": 39220,
                },
                handle,
            )

    def test_a_pr_with_a_stale_head_ref_oid_is_delivered_if_the_tip_reached_the_remote_anyway(self):
        self.git("checkout", "-q", "-b", "pr-mismatch")
        with open(os.path.join(self.slot, "feature.txt"), "w", encoding="utf-8") as handle:
            handle.write("landed via a fast-forward merge\n")
        self.git("add", "feature.txt")
        self.git("commit", "-q", "-m", "feature work")
        self.git("checkout", "-q", "master")
        self.git("merge", "-q", "--ff-only", "pr-mismatch")
        self.git("push", "-q", "origin", "master")
        self.git("fetch", "-q", "origin")

        result = self.harness.run("release", "delivery", "--for", "test")

        self.assertEqual(result.returncode, 1, result.stdout.decode())
        stderr = result.stderr.decode()
        self.assertNotIn(
            "pr-mismatch", stderr,
            "a branch reachable from origin/master was reported held: " + stderr,
        )

    def test_a_branch_genuinely_ahead_of_every_remote_ref_still_reports_held(self):
        self.git("checkout", "-q", "-b", "still-brewing")
        with open(os.path.join(self.slot, "wip.txt"), "w", encoding="utf-8") as handle:
            handle.write("never landed anywhere\n")
        self.git("add", "wip.txt")
        self.git("commit", "-q", "-m", "wip")

        result = self.harness.run("release", "delivery", "--for", "test")

        self.assertEqual(result.returncode, 1, result.stdout.decode())
        stderr = result.stderr.decode()
        self.assertIn("still-brewing: never pushed, 1 commits", stderr)

    def test_a_branch_ahead_of_its_own_origin_ref_is_delivered_once_a_merge_commit_carries_its_tip_to_master(self):
        self.git("checkout", "-q", "-b", "ahead-but-landed")
        with open(os.path.join(self.slot, "first.txt"), "w", encoding="utf-8") as handle:
            handle.write("what origin/ahead-but-landed still points at\n")
        self.git("add", "first.txt")
        self.git("commit", "-q", "-m", "first")
        self.git("push", "-q", "origin", "ahead-but-landed")
        with open(os.path.join(self.slot, "second.txt"), "w", encoding="utf-8") as handle:
            handle.write("local-only, but its tip reaches master below\n")
        self.git("add", "second.txt")
        self.git("commit", "-q", "-m", "second")
        self.git("checkout", "-q", "master")
        self.git("merge", "-q", "--ff-only", "ahead-but-landed")
        self.git("push", "-q", "origin", "master")
        self.git("fetch", "-q", "origin")

        result = self.harness.run("release", "delivery", "--for", "test")

        self.assertEqual(result.returncode, 1, result.stdout.decode())
        stderr = result.stderr.decode()
        self.assertNotIn(
            "ahead-but-landed", stderr,
            "a branch reachable from origin/master via a merge commit was reported held: " + stderr,
        )

    def test_master_is_delivered_once_fetched_even_though_it_was_pushed_under_a_different_branch_name(self):
        with open(os.path.join(self.slot, "second.txt"), "w", encoding="utf-8") as handle:
            handle.write("committed on master, pushed elsewhere by 'the orchestrator'\n")
        self.git("add", "second.txt")
        self.git("commit", "-q", "-m", "second")

        orchestrator = os.path.join(self.harness.tmp, "orchestrator-clone")
        os.makedirs(orchestrator)
        run = lambda *args: subprocess.run(
            [self.harness.real_git_bin, "-C", orchestrator, *args],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        run("init", "-q")
        run("fetch", "-q", self.slot, "master:refs/heads/carry")
        run("push", "-q", self.remote, "refs/heads/carry:refs/heads/test/example-branch")

        result = self.harness.run("release", "delivery", "--for", "test")

        self.assertEqual(result.returncode, 1, result.stdout.decode())
        stderr = result.stderr.decode()
        self.assertNotIn(
            "commits not on origin/master", stderr,
            "master was reported held even though its tip reached the remote under a different name: " + stderr,
        )


class BranchDeliveryComparesPatchIds(DevContainerTestCase):

    def setUp(self):
        super().setUp()
        self.harness._write_stub("gh", "#!/usr/bin/env bash\nexit 1\n")
        self.harness.build_fixture_repo()
        self.harness.make_bare_slot(
            "delivery", local_identity=("fixture", "fixture@example.com")
        )
        self.slot = self.harness.slot_dir("delivery")
        self.remote = os.path.join(self.harness.tmp, "delivery-remote.git")
        subprocess.run(
            ["git", "init", "--bare", "-q", self.remote],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self.git = lambda *args: subprocess.run(
            [self.harness.real_git_bin, "-C", self.slot, "-c", "commit.gpgsign=false", *args],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self.git("remote", "set-url", "origin", self.remote)
        with open(os.path.join(self.slot, "README.md"), "w", encoding="utf-8") as handle:
            handle.write("delivery fixture\n")
        self.git("add", "README.md")
        self.git("commit", "-q", "-m", "base")
        self.git("branch", "-M", "master")
        self.git("push", "-q", "origin", "master")
        self.git("fetch", "-q", "origin")
        with open(os.path.join(self.slot, "leftover.txt"), "w", encoding="utf-8") as handle:
            handle.write("uncommitted, just to force the report\n")
        lease_path = self.harness.lease_path("delivery")
        os.makedirs(os.path.dirname(lease_path), exist_ok=True)
        with open(lease_path, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "holder": "test",
                    "project": PROJECT_PREFIX + "-delivery",
                    "since": int(time.time()),
                    "ttl_seconds": 14400,
                    "key": os.path.join(self.harness.tmp, "delivery-fixture-key"),
                    "ssh_port": 39220,
                },
                handle,
            )

    def _leave_squash_merged(self):
        self.git("checkout", "-q", "-b", "squash-work")
        with open(os.path.join(self.slot, "feature.txt"), "w", encoding="utf-8") as handle:
            handle.write("landed via a squash merge\n")
        self.git("add", "feature.txt")
        self.git("commit", "-q", "-m", "feature work")
        self.git("checkout", "-q", "master")
        self.git("merge", "-q", "--squash", "squash-work")
        self.git("commit", "-q", "-m", "feature work (squashed)")
        with open(os.path.join(self.slot, "unrelated.txt"), "w", encoding="utf-8") as handle:
            handle.write("master keeps moving after the squash lands\n")
        self.git("add", "unrelated.txt")
        self.git("commit", "-q", "-m", "unrelated later work")
        self.git("push", "-q", "origin", "master")
        self.git("fetch", "-q", "origin")

    def test_1_a_squash_merged_branch_is_not_reported_held_once_master_moves_on(self):
        self._leave_squash_merged()

        result = self.harness.run("release", "delivery", "--for", "test")

        self.assertEqual(result.returncode, 1, result.stdout.decode())
        stderr = result.stderr.decode()
        self.assertNotIn(
            "squash-work", stderr,
            "a squash-merged branch was reported held: " + stderr,
        )

    def test_2_a_branch_with_genuinely_undelivered_work_is_still_reported_held(self):
        self.git("checkout", "-q", "-b", "still-brewing")
        with open(os.path.join(self.slot, "wip.txt"), "w", encoding="utf-8") as handle:
            handle.write("never landed anywhere\n")
        self.git("add", "wip.txt")
        self.git("commit", "-q", "-m", "wip")

        result = self.harness.run("release", "delivery", "--for", "test")

        self.assertEqual(result.returncode, 1, result.stdout.decode())
        stderr = result.stderr.decode()
        self.assertIn("still-brewing", stderr)

    def test_3_gh_being_unavailable_degrades_to_the_patch_id_answer_not_to_could_not_verify(self):
        self._leave_squash_merged()

        result = self.harness.run("release", "delivery", "--for", "test")

        self.assertEqual(result.returncode, 1, result.stdout.decode())
        stderr = result.stderr.decode()
        self.assertNotIn("unknown (gh refused)", stderr)
        self.assertNotIn("unknown (no gh on PATH)", stderr)
        self.assertNotIn("squash-work", stderr)

    def test_4_a_multi_commit_branch_squashed_into_one_is_no_longer_reported_held(self):
        self.git("checkout", "-q", "-b", "multi-commit-work")
        with open(os.path.join(self.slot, "part-one.txt"), "w", encoding="utf-8") as handle:
            handle.write("first half of the change\n")
        self.git("add", "part-one.txt")
        self.git("commit", "-q", "-m", "part one")
        with open(os.path.join(self.slot, "part-two.txt"), "w", encoding="utf-8") as handle:
            handle.write("second half of the change\n")
        self.git("add", "part-two.txt")
        self.git("commit", "-q", "-m", "part two")
        self.git("checkout", "-q", "master")
        self.git("merge", "-q", "--squash", "multi-commit-work")
        self.git("commit", "-q", "-m", "multi-commit work (squashed)")
        self.git("push", "-q", "origin", "master")
        self.git("fetch", "-q", "origin")

        result = self.harness.run("release", "delivery", "--for", "test")

        self.assertEqual(result.returncode, 1, result.stdout.decode())
        stderr = result.stderr.decode()
        self.assertNotIn("    multi-commit-work:", stderr)

    def test_5_a_failed_git_cherry_is_not_reported_delivered_and_caches_nothing(self):
        self.git("checkout", "-q", "-b", "cherry-fails")
        with open(os.path.join(self.slot, "part-one.txt"), "w", encoding="utf-8") as handle:
            handle.write("an ancestor commit git cherry cannot actually read\n")
        self.git("add", "part-one.txt")
        self.git("commit", "-q", "-m", "part one")
        ancestor = self.git("rev-parse", "cherry-fails").stdout.decode().strip()
        with open(os.path.join(self.slot, "part-two.txt"), "w", encoding="utf-8") as handle:
            handle.write("the tip itself, still a perfectly valid object\n")
        self.git("add", "part-two.txt")
        self.git("commit", "-q", "-m", "part two")
        self.git("checkout", "-q", "master")
        obj_path = os.path.join(self.slot, ".git", "objects", ancestor[:2], ancestor[2:])
        self.assertTrue(os.path.isfile(obj_path), "fixture assumption: the ancestor is still a loose object")
        os.remove(obj_path)

        result = self.harness.run("release", "delivery", "--for", "test")

        self.assertEqual(result.returncode, 1, result.stdout.decode())
        stderr = result.stderr.decode()
        self.assertIn(
            "cherry-fails", stderr,
            "a branch git cherry could not actually compare was reported delivered: " + stderr,
        )
        import glob
        cache_glob = os.path.join(self.harness.state_dir, "delivered", "*cherry-fails*")
        self.assertEqual(
            glob.glob(cache_glob), [],
            "a failed git cherry must never be cached as delivered",
        )


class BranchDeliveryAsksGithubWhenShallow(DevContainerTestCase):

    def setUp(self):
        super().setUp()
        self.harness.build_fixture_repo()
        self.harness.make_bare_slot(
            "delivery", local_identity=("fixture", "fixture@example.com")
        )
        self.slot = self.harness.slot_dir("delivery")
        remote_bare = os.path.join(self.harness.tmp, "delivery-remote.git")
        subprocess.run(
            ["git", "init", "--bare", "-q", remote_bare],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        subprocess.run(
            ["git", "-C", remote_bare, "symbolic-ref", "HEAD", "refs/heads/master"],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        seed = os.path.join(self.harness.tmp, "delivery-seed")
        subprocess.run(
            [self.harness.real_git_bin, "clone", "-q", remote_bare, seed],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        git_seed = lambda *args: subprocess.run(
            [self.harness.real_git_bin, "-C", seed, "-c", "commit.gpgsign=false",
             "-c", "user.name=fixture", "-c", "user.email=fixture@example.com", *args],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        with open(os.path.join(seed, "README.md"), "w", encoding="utf-8") as handle:
            handle.write("base\n")
        git_seed("add", "README.md")
        git_seed("commit", "-q", "-m", "base")
        git_seed("branch", "-M", "master")
        git_seed("push", "-q", "origin", "master")

        shallow_clone = os.path.join(self.harness.tmp, "delivery-shallow-clone")
        subprocess.run(
            [self.harness.real_git_bin, "clone", "-q", "--depth", "1",
             "file://" + remote_bare, shallow_clone],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        shutil.rmtree(os.path.join(self.slot, ".git"))
        shutil.move(os.path.join(shallow_clone, ".git"), os.path.join(self.slot, ".git"))
        subprocess.run(
            [self.harness.real_git_bin, "-C", self.slot, "checkout", "-q", "-f", "master"],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self.assertTrue(
            os.path.isfile(os.path.join(self.slot, ".git", "shallow")),
            "fixture assumption: the slot's own checkout is actually shallow",
        )
        self.git = lambda *args: subprocess.run(
            [self.harness.real_git_bin, "-C", self.slot, "-c", "commit.gpgsign=false", *args],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self.git("config", "user.email", "fixture@example.com")
        self.git("config", "user.name", "fixture")
        with open(os.path.join(self.slot, "leftover.txt"), "w", encoding="utf-8") as handle:
            handle.write("uncommitted, just to force the report\n")
        lease_path = self.harness.lease_path("delivery")
        os.makedirs(os.path.dirname(lease_path), exist_ok=True)
        with open(lease_path, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "holder": "test",
                    "project": PROJECT_PREFIX + "-delivery",
                    "since": int(time.time()),
                    "ttl_seconds": 14400,
                    "key": os.path.join(self.harness.tmp, "delivery-fixture-key"),
                    "ssh_port": 39220,
                },
                handle,
            )

    def _stub_gh_compare(self, status):
        body = "exit 1\n" if status is None else f"echo '{status}'\n"
        self.harness._write_stub("gh", "#!/usr/bin/env bash\n" + body)

    def test_a_tracked_branch_ahead_only_because_history_was_never_fetched_is_delivered(self):
        self.git("checkout", "-q", "master")
        with open(os.path.join(self.slot, "feature.txt"), "w", encoding="utf-8") as handle:
            handle.write("actually already on origin, just never fetched down here\n")
        self.git("add", "feature.txt")
        self.git("commit", "-q", "-m", "feature work")
        self._stub_gh_compare("ahead")

        result = self.harness.run("release", "delivery", "--for", "test")

        self.assertEqual(result.returncode, 1, result.stdout.decode())
        stderr = result.stderr.decode()
        self.assertNotIn(
            "not on origin/master", stderr,
            "a shallow clone's tracked branch was reported held despite GitHub's compare saying otherwise: "
            + stderr,
        )

    def test_a_tracked_branch_the_compare_reports_behind_stays_held(self):
        self.git("checkout", "-q", "master")
        with open(os.path.join(self.slot, "feature.txt"), "w", encoding="utf-8") as handle:
            handle.write("genuinely never reached origin\n")
        self.git("add", "feature.txt")
        self.git("commit", "-q", "-m", "feature work")
        self._stub_gh_compare("behind")

        result = self.harness.run("release", "delivery", "--for", "test")

        self.assertEqual(result.returncode, 1, result.stdout.decode())
        self.assertIn("not on origin/master", result.stderr.decode())

    def test_an_untracked_branch_the_compare_reports_identical_is_delivered(self):
        self.git("checkout", "-q", "-b", "untracked-work")
        with open(os.path.join(self.slot, "feature.txt"), "w", encoding="utf-8") as handle:
            handle.write("squashed onto origin's default branch already\n")
        self.git("add", "feature.txt")
        self.git("commit", "-q", "-m", "feature work")
        self._stub_gh_compare("identical")

        result = self.harness.run("release", "delivery", "--for", "test")

        self.assertEqual(result.returncode, 1, result.stdout.decode())
        stderr = result.stderr.decode()
        self.assertNotIn(
            "held/unknown branches:", stderr,
            "a branch GitHub's compare called delivered still produced a held/unknown report: " + stderr,
        )

    def test_an_untracked_branch_the_compare_reports_diverged_stays_held(self):
        self.git("checkout", "-q", "-b", "untracked-work")
        with open(os.path.join(self.slot, "feature.txt"), "w", encoding="utf-8") as handle:
            handle.write("never actually landed\n")
        self.git("add", "feature.txt")
        self.git("commit", "-q", "-m", "feature work")
        self._stub_gh_compare("diverged")

        result = self.harness.run("release", "delivery", "--for", "test")

        self.assertEqual(result.returncode, 1, result.stdout.decode())
        self.assertIn("untracked-work", result.stderr.decode())

    def test_a_failing_compare_call_reports_unknown_never_delivered(self):
        self.git("checkout", "-q", "-b", "untracked-work")
        with open(os.path.join(self.slot, "feature.txt"), "w", encoding="utf-8") as handle:
            handle.write("gh itself is unreachable\n")
        self.git("add", "feature.txt")
        self.git("commit", "-q", "-m", "feature work")
        self._stub_gh_compare(None)

        result = self.harness.run("release", "delivery", "--for", "test")

        self.assertEqual(result.returncode, 1, result.stdout.decode())
        self.assertIn("unknown (compare api failed)", result.stderr.decode())


class ReapApiCallsAreCached(DevContainerTestCase):

    def setUp(self):
        super().setUp()
        self.harness.build_fixture_repo()
        self.harness.make_bare_slot(
            "reap-calls", local_identity=("fixture", "fixture@example.com")
        )
        self.slot = self.harness.slot_dir("reap-calls")
        self.remote = os.path.join(self.harness.tmp, "reap-calls-remote.git")
        subprocess.run(
            ["git", "init", "--bare", "-q", self.remote],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self.git = lambda *args: subprocess.run(
            [self.harness.real_git_bin, "-C", self.slot, "-c", "commit.gpgsign=false", *args],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self.git("remote", "set-url", "origin", self.remote)
        with open(os.path.join(self.slot, "README.md"), "w", encoding="utf-8") as handle:
            handle.write("reap-calls fixture\n")
        self.git("add", "README.md")
        self.git("commit", "-q", "-m", "base")
        self.git("branch", "-M", "master")
        self.git("push", "-q", "origin", "master")
        self.git("fetch", "-q", "origin")
        self.base_sha = subprocess.run(
            [self.harness.real_git_bin, "-C", self.slot, "rev-parse", "master"],
            check=True, stdout=subprocess.PIPE,
        ).stdout.decode().strip()

        lease_path = self.harness.lease_path("reap-calls")
        os.makedirs(os.path.dirname(lease_path), exist_ok=True)
        with open(lease_path, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "holder": "test",
                    "project": PROJECT_PREFIX + "-reap-calls",
                    "since": int(time.time()),
                    "ttl_seconds": 14400,
                    "key": os.path.join(self.harness.tmp, "reap-calls-fixture-key"),
                    "ssh_port": 39220,
                },
                handle,
            )

        self.gh_call_log = os.path.join(self.harness.tmp, "gh-calls.log")
        self.harness._write_stub(
            "gh",
            "#!/usr/bin/env bash\n"
            'log="${GH_CALL_LOG:-/dev/null}"\n'
            'printf \'%s\\n\' "$*" >> "$log"\n'
            'head="" prev=""\n'
            'for a in "$@"; do\n'
            '  [[ "$prev" == "--head" ]] && head="$a"\n'
            '  prev="$a"\n'
            "done\n"
            'case "$head" in\n'
            "  closed-elsewhere)\n"
            f'    echo \'[{{"headRefOid":"{self.base_sha}","state":"MERGED","number":7}}]\'\n'
            "    ;;\n"
            "  open-elsewhere)\n"
            f'    echo \'[{{"headRefOid":"{self.base_sha}","state":"OPEN","number":8}}]\'\n'
            "    ;;\n"
            "  gh-fails)\n"
            "    exit 1\n"
            "    ;;\n"
            "  *)\n"
            "    echo '[]'\n"
            "    ;;\n"
            "esac\n",
        )

        with open(os.path.join(self.slot, "leftover.txt"), "w", encoding="utf-8") as handle:
            handle.write("uncommitted, just to force the report\n")

    def _release(self):
        return self.harness.run(
            "release", "reap-calls", "--for", "test",
            port_overrides={"GH_CALL_LOG": self.gh_call_log},
        )

    def _gh_call_count(self):
        if not os.path.isfile(self.gh_call_log):
            return 0
        with open(self.gh_call_log, encoding="utf-8") as handle:
            return sum(1 for line in handle if line.strip())

    def test_1_a_branch_reachable_from_a_remote_ref_costs_zero_gh_calls(self):
        self.git("checkout", "-q", "-b", "reachable-only-via-master")
        with open(os.path.join(self.slot, "feature.txt"), "w", encoding="utf-8") as handle:
            handle.write("landed via a fast-forward merge\n")
        self.git("add", "feature.txt")
        self.git("commit", "-q", "-m", "feature work")
        self.git("checkout", "-q", "master")
        self.git("merge", "-q", "--ff-only", "reachable-only-via-master")
        self.git("push", "-q", "origin", "master")
        self.git("fetch", "-q", "origin")

        result = self._release()

        self.assertEqual(result.returncode, 1, result.stdout.decode())
        self.assertNotIn(
            "reachable-only-via-master", result.stderr.decode(),
            "a branch reachable from origin/master was reported held: " + result.stderr.decode(),
        )
        self.assertEqual(
            self._gh_call_count(), 0,
            "a branch reachable from a remote ref should never reach gh",
        )

    def test_2_a_merged_pr_at_a_different_tip_is_asked_once_then_cached(self):
        self.git("checkout", "-q", "-b", "closed-elsewhere")
        with open(os.path.join(self.slot, "wip.txt"), "w", encoding="utf-8") as handle:
            handle.write("never landed, never reachable from any remote ref\n")
        self.git("add", "wip.txt")
        self.git("commit", "-q", "-m", "wip")

        first = self._release()
        self.assertEqual(first.returncode, 1, first.stdout.decode())
        first_stderr = first.stderr.decode()
        self.assertIn("closed-elsewhere", first_stderr)
        self.assertEqual(self._gh_call_count(), 1, "first pass should ask gh exactly once")

        second = self._release()
        self.assertEqual(second.returncode, 1, second.stdout.decode())
        second_stderr = second.stderr.decode()
        self.assertEqual(
            self._gh_call_count(), 1,
            "second pass re-asked gh about a branch whose only PR is already merged/closed",
        )
        first_line = next(l for l in first_stderr.splitlines() if "closed-elsewhere" in l)
        second_line = next(l for l in second_stderr.splitlines() if "closed-elsewhere" in l)
        self.assertEqual(first_line.strip(), second_line.strip())

    def test_3_an_open_pr_at_a_different_tip_is_asked_every_pass(self):
        self.git("checkout", "-q", "-b", "open-elsewhere")
        with open(os.path.join(self.slot, "wip.txt"), "w", encoding="utf-8") as handle:
            handle.write("never landed, never reachable from any remote ref\n")
        self.git("add", "wip.txt")
        self.git("commit", "-q", "-m", "wip")

        first = self._release()
        self.assertEqual(first.returncode, 1, first.stdout.decode())
        self.assertEqual(self._gh_call_count(), 1)

        second = self._release()
        self.assertEqual(second.returncode, 1, second.stdout.decode())
        self.assertEqual(
            self._gh_call_count(), 2,
            "an OPEN PR at a different tip can still move — it must be asked again",
        )

    def test_4_a_gh_failure_is_unknown_and_never_cached(self):
        self.git("checkout", "-q", "-b", "gh-fails")
        with open(os.path.join(self.slot, "wip.txt"), "w", encoding="utf-8") as handle:
            handle.write("never landed, never reachable from any remote ref\n")
        self.git("add", "wip.txt")
        self.git("commit", "-q", "-m", "wip")

        first = self._release()
        self.assertEqual(first.returncode, 1, first.stdout.decode())
        self.assertIn("unknown (gh refused)", first.stderr.decode())
        self.assertEqual(self._gh_call_count(), 1)

        cache_glob_delivered = os.path.join(self.harness.state_dir, "delivered", "*gh-fails*")
        cache_glob_closed = os.path.join(self.harness.state_dir, "closed", "*gh-fails*")
        import glob
        self.assertEqual(glob.glob(cache_glob_delivered), [], "an unknown verdict must never be cached as delivered")
        self.assertEqual(glob.glob(cache_glob_closed), [], "an unknown verdict must never be cached as closed/merged")

        second = self._release()
        self.assertEqual(second.returncode, 1, second.stdout.decode())
        self.assertIn("unknown (gh refused)", second.stderr.decode())
        self.assertEqual(
            self._gh_call_count(), 2,
            "an unknown verdict (gh refused) must be re-asked, never cached",
        )


class WarnsAboutBranchesNotProvedInMaster(DevContainerTestCase):

    def setUp(self):
        super().setUp()
        self.harness._write_stub("gh", "#!/usr/bin/env bash\necho '[]'\n")
        self.harness.build_fixture_repo()
        self.harness.make_bare_slot(
            "unproven",
            env_content="GIT_USER_NAME=fixture\nGIT_USER_EMAIL=fixture@example.com\n",
            ports_content="NOVNC_PORT=39230\nSSH_PORT=39220\nVITE_DEV_PORT=39240\n",
            local_identity=("fixture", "fixture@example.com"),
        )
        self.slot = self.harness.slot_dir("unproven")
        self.remote = os.path.join(self.harness.tmp, "unproven-remote.git")
        subprocess.run(
            ["git", "init", "--bare", "-q", self.remote],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self.git = lambda *args: subprocess.run(
            [self.harness.real_git_bin, "-C", self.slot, "-c", "commit.gpgsign=false", *args],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self.git("remote", "set-url", "origin", self.remote)
        subprocess.run(
            [self.harness.real_git_bin, "-C", self.harness.fixture_repo,
             "remote", "set-url", "origin", self.remote],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        with open(os.path.join(self.slot, "README.md"), "w", encoding="utf-8") as handle:
            handle.write("unproven fixture\n")
        with open(os.path.join(self.slot, ".gitignore"), "w", encoding="utf-8") as handle:
            handle.write(".devcontainer/.env\n.devcontainer/.slot-ports\n.devcontainer/.slot-holder\ndocs/\n")
        self.git("add", "README.md", ".gitignore", ".devcontainer/docker-compose.yml")
        self.git("commit", "-q", "-m", "base")
        self.git("branch", "-M", "master")
        self.git("push", "-q", "origin", "master")
        self.git("fetch", "-q", "origin")
        lease_path = self.harness.lease_path("unproven")
        os.makedirs(os.path.dirname(lease_path), exist_ok=True)
        with open(lease_path, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "holder": "test",
                    "project": PROJECT_PREFIX + "-unproven",
                    "since": int(time.time()),
                    "ttl_seconds": 14400,
                    "key": os.path.join(self.harness.tmp, "unproven-fixture-key"),
                    "ssh_port": 39220,
                },
                handle,
            )


    def _leave_still_brewing(self):
        self.git("checkout", "-q", "-b", "still-brewing")
        with open(os.path.join(self.slot, "wip.txt"), "w", encoding="utf-8") as handle:
            handle.write("never landed anywhere\n")
        self.git("add", "wip.txt")
        self.git("commit", "-q", "-m", "wip")
        self.git("checkout", "-q", "master")

    def test_release_names_a_branch_not_proved_to_be_in_master(self):
        self._leave_still_brewing()

        result = self.harness.run("release", "unproven", "--for", "test")

        stderr = result.stderr.decode()
        self.assertIn("still-brewing", stderr)


    def _leave_squash_merged(self):
        self.git("checkout", "-q", "-b", "squash-work")
        with open(os.path.join(self.slot, "feature.txt"), "w", encoding="utf-8") as handle:
            handle.write("landed via a squash merge\n")
        self.git("add", "feature.txt")
        self.git("commit", "-q", "-m", "feature work")
        self.git("checkout", "-q", "master")
        self.git("merge", "-q", "--squash", "squash-work")
        self.git("commit", "-q", "-m", "feature work (squashed)")
        with open(os.path.join(self.slot, "unrelated.txt"), "w", encoding="utf-8") as handle:
            handle.write("master keeps moving after the squash lands\n")
        self.git("add", "unrelated.txt")
        self.git("commit", "-q", "-m", "unrelated later work")
        self.git("push", "-q", "origin", "master")
        self.git("fetch", "-q", "origin")

    def test_release_does_not_report_a_squash_merged_branch_after_master_moves_on(self):
        self._leave_squash_merged()

        result = self.harness.run("release", "unproven", "--for", "test")

        self.assertNotIn("squash-work", result.stderr.decode())


    def _leave_multi_commit_squash_merged(self):
        self.git("checkout", "-q", "-b", "multi-commit-work")
        with open(os.path.join(self.slot, "part-one.txt"), "w", encoding="utf-8") as handle:
            handle.write("first half of the change\n")
        self.git("add", "part-one.txt")
        self.git("commit", "-q", "-m", "part one")
        with open(os.path.join(self.slot, "part-two.txt"), "w", encoding="utf-8") as handle:
            handle.write("second half of the change\n")
        self.git("add", "part-two.txt")
        self.git("commit", "-q", "-m", "part two")
        self.git("checkout", "-q", "master")
        self.git("merge", "-q", "--squash", "multi-commit-work")
        self.git("commit", "-q", "-m", "multi-commit work (squashed)")
        self.git("push", "-q", "origin", "master")
        self.git("fetch", "-q", "origin")

    def test_multi_commit_branch_squashed_into_one_is_not_reported(self):
        self._leave_multi_commit_squash_merged()

        result = self.harness.run("release", "unproven", "--for", "test")

        self.assertNotIn("multi-commit-work", result.stderr.decode())


    def test_a_branch_that_only_lags_master_is_not_reported(self):
        base_sha = self.git("rev-parse", "HEAD").stdout.decode().strip()
        self.git("branch", "old-branch", base_sha)
        with open(os.path.join(self.slot, "later.txt"), "w", encoding="utf-8") as handle:
            handle.write("master moves on, old-branch does not\n")
        self.git("add", "later.txt")
        self.git("commit", "-q", "-m", "master moves on")
        self.git("push", "-q", "origin", "master")
        self.git("fetch", "-q", "origin")

        result = self.harness.run("release", "unproven", "--for", "test")

        self.assertNotIn("old-branch", result.stderr.decode())


    def test_the_warning_never_claims_work_will_be_lost(self):
        self._leave_still_brewing()

        result = self.harness.run("release", "unproven", "--for", "test")

        stderr = result.stderr.decode()
        self.assertIn("still-brewing", stderr)
        self.assertNotIn("will be lost", stderr)
        self.assertNotIn("lose this", stderr)


class UnprovenBranchesInDocsRepo(DevContainerTestCase):

    ENV_WITH_IDENTITY = "GIT_USER_NAME=demo-agent\nGIT_USER_EMAIL=demo-agent@example.com\n"

    def setUp(self):
        super().setUp()
        self.harness._write_stub("gh", "#!/usr/bin/env bash\necho '[]'\n")

    def _acquire(self, holder="docs-unproven"):
        self.harness.build_fixture_repo(with_parent_env=self.ENV_WITH_IDENTITY)
        result = self.harness.run_acquire(holder=holder)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        name = self.harness.acquired_slot_name(result)
        slot = self.harness.slot_dir(name)
        docs = os.path.join(slot, "docs", "repo")
        self.assertTrue(os.path.isdir(os.path.join(docs, ".git")), "no real docs/repo clone — fixture is broken")
        remote = os.path.join(self.harness.tmp, f"{name}-slot-remote.git")
        subprocess.run(
            [self.harness.real_git_bin, "init", "--bare", "-q", remote],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        for target in (slot, self.harness.fixture_repo):
            subprocess.run(
                [self.harness.real_git_bin, "-C", target, "remote", "set-url", "origin", remote],
                check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
        subprocess.run(
            [self.harness.real_git_bin, "-C", slot, "push", "-q", "origin", "master"],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        return name, slot, docs

    def _docs_git(self, docs):
        return lambda *args: subprocess.run(
            [
                self.harness.real_git_bin, "-C", docs,
                "-c", "core.hooksPath=/dev/null",
                "-c", "commit.gpgsign=false",
                "-c", "user.email=fixture@example.com",
                "-c", "user.name=fixture",
                *args,
            ],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )

    def test_release_names_a_docs_branch_not_proved_to_be_in_master(self):
        name, slot, docs = self._acquire()
        git = self._docs_git(docs)
        git("checkout", "-q", "-b", "wip-notes")
        with open(os.path.join(docs, "notes.md"), "w", encoding="utf-8") as handle:
            handle.write("never pushed anywhere\n")
        git("add", "notes.md")
        git("commit", "-q", "-m", "wip notes")

        result = self.harness.run("release", name, "--for", "docs-unproven")

        self.assertIn("wip-notes", result.stderr.decode())

    def _allow_pushing_master_to_the_docs_stub(self, docs):
        subprocess.run(
            [self.harness.real_git_bin, "-C", self.harness.docs_repo_stub,
             "config", "receive.denyCurrentBranch", "updateInstead"],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        subprocess.run(
            [self.harness.real_git_bin, "-C", docs, "config", "--unset", "remote.origin.pushurl"],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        return self._docs_git(docs)

    def test_release_does_not_report_a_squash_merged_docs_branch(self):
        name, slot, docs = self._acquire()
        git = self._allow_pushing_master_to_the_docs_stub(docs)
        git("checkout", "-q", "-b", "docs-squash-work")
        with open(os.path.join(docs, "notes.md"), "w", encoding="utf-8") as handle:
            handle.write("squash-merged, branch deleted afterward\n")
        git("add", "notes.md")
        git("commit", "-q", "-m", "docs squash work")
        git("checkout", "-q", "master")
        git("merge", "-q", "--squash", "docs-squash-work")
        git("commit", "-q", "-m", "docs squash work (squashed)")
        git("push", "-q", "origin", "master")

        result = self.harness.run("release", name, "--for", "docs-unproven")

        self.assertNotIn("docs-squash-work", result.stderr.decode())


class CleanlinessChecksSeeTheDocsClone(DevContainerTestCase):

    ENV_WITH_IDENTITY = "GIT_USER_NAME=demo-agent\nGIT_USER_EMAIL=demo-agent@example.com\n"

    def setUp(self):
        super().setUp()
        self._stub_gh_no_prs()

    def _stub_gh_no_prs(self):
        self.harness._write_stub("gh", "#!/usr/bin/env bash\necho '[]'\n")

    def _stub_gh_refuses(self):
        self.harness._write_stub("gh", "#!/usr/bin/env bash\nexit 1\n")

    def _stub_gh_pr_merged(self, branch, head_ref_oid, number=99):
        self.harness._write_stub(
            "gh",
            "#!/usr/bin/env bash\n"
            'head="" prev=""\n'
            'for a in "$@"; do\n'
            '  [[ "$prev" == "--head" ]] && head="$a"\n'
            '  prev="$a"\n'
            "done\n"
            f'if [[ "$head" == "{branch}" ]]; then\n'
            f'  echo \'[{{"headRefOid":"{head_ref_oid}","state":"MERGED","number":{number}}}]\'\n'
            "else\n"
            "  echo '[]'\n"
            "fi\n",
        )

    def _acquire(self, holder="docs-check"):
        self.harness.build_fixture_repo(with_parent_env=self.ENV_WITH_IDENTITY)
        result = self.harness.run_acquire(holder=holder)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        name = self.harness.acquired_slot_name(result)
        slot = self.harness.slot_dir(name)
        docs = os.path.join(slot, "docs", "repo")
        self.assertTrue(os.path.isdir(os.path.join(docs, ".git")), "no real docs/repo clone — fixture is broken")
        return name, slot, docs

    def _docs_git(self, docs):
        return lambda *args: subprocess.run(
            [
                self.harness.real_git_bin, "-C", docs,
                "-c", "core.hooksPath=/dev/null",
                "-c", "commit.gpgsign=false",
                "-c", "user.email=fixture@example.com",
                "-c", "user.name=fixture",
                *args,
            ],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )

    def _docs_tip(self, docs, branch):
        return subprocess.run(
            [self.harness.real_git_bin, "-C", docs, "rev-parse", branch],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        ).stdout.decode().strip()

    def _leave_an_undelivered_commit(self, docs):
        git = self._docs_git(docs)
        git("checkout", "-q", "-b", "wip-notes")
        with open(os.path.join(docs, "notes.md"), "w", encoding="utf-8") as handle:
            handle.write("never pushed anywhere\n")
        git("add", "notes.md")
        git("commit", "-q", "-m", "wip notes")

    def _row_line(self, stdout, slot_name):
        for line in stdout.splitlines():
            if line.split()[:1] == [slot_name]:
                return line
        raise AssertionError(f"no '{slot_name}' row in:\n{stdout}")


    def test_release_refuses_a_slot_whose_only_undelivered_work_is_in_docs_repo(self):
        name, slot, docs = self._acquire()
        self._leave_an_undelivered_commit(docs)

        result = self.harness.run("release", name, "--for", "docs-check")

        self.assertNotEqual(result.returncode, 0, result.stdout.decode())
        stderr = result.stderr.decode()
        self.assertIn("docs/repo", stderr)
        self.assertIn("wip-notes", stderr)
        self.assertNotIn("--force", stderr)
        self.assertTrue(os.path.exists(self.harness.lease_path(name)), "released despite the refusal")
        self.assertTrue(os.path.isdir(slot), "removed despite the refusal")

    def test_a_commit_in_docs_repo_is_told_it_is_the_wrong_place_for_one(self):
        name, slot, docs = self._acquire()
        self._leave_an_undelivered_commit(docs)

        result = self.harness.run("release", name, "--for", "docs-check")

        self.assertNotEqual(result.returncode, 0, result.stdout.decode())
        stderr = result.stderr.decode()
        self.assertIn("read-only", stderr)

    def test_release_force_is_refused_and_changes_nothing_over_an_undelivered_docs_commit(self):
        name, slot, docs = self._acquire()
        self._leave_an_undelivered_commit(docs)

        result = self.harness.run("release", name, "--for", "docs-check", "--force")

        self.assertNotEqual(result.returncode, 0, result.stdout.decode())
        self.assertIn("unknown flag to release", result.stderr.decode())
        self.assertTrue(os.path.exists(self.harness.lease_path(name)), "released despite the refusal")
        self.assertTrue(os.path.isdir(slot), "removed despite the refusal")


    def test_down_refuses_a_slot_whose_only_undelivered_work_is_in_docs_repo(self):
        name, slot, docs = self._acquire()
        self._leave_an_undelivered_commit(docs)
        self.harness.free_slot(name, stopped=True)

        result = self.harness.run("down", name)

        self.assertNotEqual(result.returncode, 0, result.stdout.decode())
        self.assertIn("docs/repo", result.stderr.decode())
        self.assertTrue(os.path.isdir(slot), "removed despite the refusal")


    def test_release_refuses_uncommitted_changes_in_docs_repo(self):
        name, slot, docs = self._acquire()
        with open(os.path.join(docs, "README.md"), "w", encoding="utf-8") as handle:
            handle.write("edited, never committed\n")

        result = self.harness.run("release", name, "--for", "docs-check")

        self.assertNotEqual(result.returncode, 0, result.stdout.decode())
        stderr = result.stderr.decode()
        self.assertIn("docs/repo", stderr)
        self.assertIn("uncommitted", stderr)

    def test_release_refuses_a_stash_in_docs_repo(self):
        name, slot, docs = self._acquire()
        git = self._docs_git(docs)
        with open(os.path.join(docs, "README.md"), "a", encoding="utf-8") as handle:
            handle.write("stashed away\n")
        git("stash", "push", "-u", "-m", "fixture")

        result = self.harness.run("release", name, "--for", "docs-check")

        self.assertNotEqual(result.returncode, 0, result.stdout.decode())
        stderr = result.stderr.decode()
        self.assertIn("docs/repo", stderr)
        self.assertIn("stash", stderr)


    def test_release_succeeds_when_docs_repo_is_clean_and_delivered(self):
        name, slot, docs = self._acquire()

        result = self.harness.run("release", name, "--for", "docs-check")

        self.assertEqual(result.returncode, 0, result.stderr.decode())

    def test_release_succeeds_when_a_docs_branch_was_squash_merged_and_its_remote_ref_is_gone(self):
        name, slot, docs = self._acquire()
        git = self._docs_git(docs)
        git("checkout", "-q", "-b", "squash-landed")
        with open(os.path.join(docs, "notes.md"), "w", encoding="utf-8") as handle:
            handle.write("squash-merged, branch deleted afterward\n")
        git("add", "notes.md")
        git("commit", "-q", "-m", "squash landed")
        tip = self._docs_tip(docs, "squash-landed")
        self._stub_gh_pr_merged("squash-landed", tip)

        result = self.harness.run("release", name, "--for", "docs-check")

        self.assertEqual(result.returncode, 0, result.stderr.decode())

    def test_release_says_it_could_not_verify_rather_than_guessing_when_gh_is_unavailable(self):
        name, slot, docs = self._acquire()
        self._leave_an_undelivered_commit(docs)
        self._stub_gh_refuses()

        result = self.harness.run("release", name, "--for", "docs-check")

        self.assertNotEqual(result.returncode, 0, result.stdout.decode())
        stderr = result.stderr.decode()
        self.assertIn("docs/repo", stderr)
        self.assertIn("could not be verified", stderr)


    def test_a_drifted_docs_repo_origin_is_never_reported_clean(self):
        name, slot, docs = self._acquire()
        elsewhere = os.path.join(self.harness.tmp, "docs-mirror-elsewhere")
        git = self._docs_git(docs)
        git("remote", "set-url", "origin", elsewhere)

        result = self.harness.run("release", name, "--for", "docs-check")

        self.assertNotEqual(result.returncode, 0, result.stdout.decode())
        stderr = result.stderr.decode()
        self.assertIn("docs/repo", stderr)
        self.assertIn("not", stderr)
        self.assertIn(elsewhere, stderr)
        self.assertNotIn("uncommitted", stderr)
        self.assertNotIn("stashed", stderr)


    def test_list_dirty_column_shows_the_docs_state(self):
        name, slot, docs = self._acquire()
        self._leave_an_undelivered_commit(docs)

        result = self.harness.run("list")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        row = self._row_line(result.stdout.decode(), name)
        self.assertIn("docs:", row)
        self.assertIn("unpushed", row)

    def test_list_dirty_column_reads_clean_when_docs_repo_is_untouched(self):
        name, slot, docs = self._acquire()

        result = self.harness.run("list")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        row = self._row_line(result.stdout.decode(), name)
        self.assertIn("clean", row)


    @staticmethod
    def _state(stdout, slot_name):
        for line in stdout.splitlines():
            fields = line.split()
            if fields and fields[0] == slot_name:
                return fields[2]
        raise AssertionError(f"no '{slot_name}' row in:\n{stdout}")

    def test_list_state_reports_a_re_pointed_docs_origin_leased(self):
        name, slot, docs = self._acquire()
        elsewhere = os.path.join(self.harness.tmp, "docs-mirror-elsewhere")
        git = self._docs_git(docs)
        git("remote", "set-url", "origin", elsewhere)

        for args in (("list",), ("list", "--brief"), ("list", "--fast")):
            with self.subTest(args=args):
                result = self.harness.run(*args)
                self.assertEqual(result.returncode, 0, result.stderr.decode())
                self.assertEqual(self._state(result.stdout.decode(), name), "docs-drift")

    def test_list_state_reports_a_drifted_origin_on_an_unleased_slot_too(self):
        name, slot, docs = self._acquire()
        elsewhere = os.path.join(self.harness.tmp, "docs-mirror-elsewhere")
        git = self._docs_git(docs)
        git("remote", "set-url", "origin", elsewhere)
        self.harness.free_slot(name, stopped=True)

        for args in (("list",), ("list", "--brief"), ("list", "--fast")):
            with self.subTest(args=args):
                result = self.harness.run(*args)
                self.assertEqual(result.returncode, 0, result.stderr.decode())
                self.assertEqual(self._state(result.stdout.decode(), name), "docs-drift")

    def test_list_state_is_not_docs_drift_when_origin_is_canonical(self):
        name, slot, docs = self._acquire()

        for args in (("list",), ("list", "--brief"), ("list", "--fast")):
            with self.subTest(args=args):
                result = self.harness.run(*args)
                self.assertEqual(result.returncode, 0, result.stderr.decode())
                self.assertNotEqual(self._state(result.stdout.decode(), name), "docs-drift")


class DocsFixCleanlinessChecks(CleanlinessChecksSeeTheDocsClone):

    def _clone_docs_fix(self, slot):
        docs_fix = os.path.join(slot, "docs", "fix")
        os.makedirs(os.path.dirname(docs_fix), exist_ok=True)
        subprocess.run(
            [self.harness.real_git_bin, "clone", "--quiet", self.harness.docs_repo_stub, docs_fix],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        return docs_fix

    def _assert_docs_fix_origin_spelling_is_recognised(self, spelling):
        stub = self.harness.docs_repo_stub
        if ".git" in spelling:
            os.symlink(stub, stub + ".git")
        name, slot, docs = self._acquire()
        docs_fix = self._clone_docs_fix(slot)
        self._docs_git(docs_fix)("remote", "set-url", "origin", stub + spelling)

        listed = self.harness.run("list")
        self.assertNotIn("unknown origin", listed.stdout.decode())
        result = self.harness.run("release", name, "--for", "docs-check")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertNotIn("unknown origin", result.stderr.decode())

    def test_release_recognises_a_docs_fix_origin_with_a_trailing_slash(self):
        self._assert_docs_fix_origin_spelling_is_recognised("/")

    def test_release_recognises_a_docs_fix_origin_with_dot_git(self):
        self._assert_docs_fix_origin_spelling_is_recognised(".git")

    def test_release_recognises_a_docs_fix_origin_with_dot_git_and_a_trailing_slash(self):
        self._assert_docs_fix_origin_spelling_is_recognised(".git/")

    def test_release_refuses_a_slot_whose_only_undelivered_work_is_in_docs_fix(self):
        name, slot, docs = self._acquire()
        docs_fix = self._clone_docs_fix(slot)
        self._leave_an_undelivered_commit(docs_fix)

        result = self.harness.run("release", name, "--for", "docs-check")

        self.assertNotEqual(result.returncode, 0, result.stdout.decode())
        stderr = result.stderr.decode()
        self.assertIn("docs/fix", stderr)
        self.assertIn("wip-notes", stderr)
        self.assertNotIn("--force", stderr)
        self.assertTrue(os.path.exists(self.harness.lease_path(name)), "released despite the refusal")
        self.assertTrue(os.path.isdir(docs_fix), "docs/fix removed despite the refusal")

    def test_release_force_is_refused_and_changes_nothing_over_an_undelivered_docs_fix_commit(self):
        name, slot, docs = self._acquire()
        docs_fix = self._clone_docs_fix(slot)
        self._leave_an_undelivered_commit(docs_fix)

        result = self.harness.run("release", name, "--for", "docs-check", "--force")

        self.assertNotEqual(result.returncode, 0, result.stdout.decode())
        self.assertIn("unknown flag to release", result.stderr.decode())
        self.assertTrue(os.path.exists(self.harness.lease_path(name)), "released despite the refusal")
        self.assertTrue(os.path.isdir(docs_fix), "docs/fix removed despite the refusal")


    def test_down_refuses_a_slot_whose_only_undelivered_work_is_in_docs_fix(self):
        name, slot, docs = self._acquire()
        docs_fix = self._clone_docs_fix(slot)
        self._leave_an_undelivered_commit(docs_fix)
        self.harness.free_slot(name, stopped=True)

        result = self.harness.run("down", name)

        self.assertNotEqual(result.returncode, 0, result.stdout.decode())
        self.assertIn("docs/fix", result.stderr.decode())
        self.assertTrue(os.path.isdir(slot), "removed despite the refusal")

    def test_release_refuses_uncommitted_changes_in_docs_fix(self):
        name, slot, docs = self._acquire()
        docs_fix = self._clone_docs_fix(slot)
        with open(os.path.join(docs_fix, "README.md"), "w", encoding="utf-8") as handle:
            handle.write("edited, never committed\n")

        result = self.harness.run("release", name, "--for", "docs-check")

        self.assertNotEqual(result.returncode, 0, result.stdout.decode())
        stderr = result.stderr.decode()
        self.assertIn("docs/fix", stderr)
        self.assertIn("uncommitted", stderr)

    def test_release_succeeds_when_a_docs_fix_branch_was_squash_merged_and_its_remote_ref_is_gone(self):
        name, slot, docs = self._acquire()
        docs_fix = self._clone_docs_fix(slot)
        git = self._docs_git(docs_fix)
        git("checkout", "-q", "-b", "squash-landed")
        with open(os.path.join(docs_fix, "notes.md"), "w", encoding="utf-8") as handle:
            handle.write("squash-merged, branch deleted afterward\n")
        git("add", "notes.md")
        git("commit", "-q", "-m", "squash landed")
        tip = self._docs_tip(docs_fix, "squash-landed")
        self._stub_gh_pr_merged("squash-landed", tip)

        result = self.harness.run("release", name, "--for", "docs-check")

        self.assertEqual(result.returncode, 0, result.stderr.decode())

    def test_list_dirty_column_shows_the_docs_fix_state(self):
        name, slot, docs = self._acquire()
        docs_fix = self._clone_docs_fix(slot)
        self._leave_an_undelivered_commit(docs_fix)

        result = self.harness.run("list")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        row = self._row_line(result.stdout.decode(), name)
        self.assertIn("docsfix:", row)
        self.assertIn("unpushed", row)

    def test_a_slot_with_no_docs_fix_at_all_behaves_exactly_as_before(self):
        name, slot, docs = self._acquire()
        self.assertFalse(os.path.isdir(os.path.join(slot, "docs", "fix")))

        list_result = self.harness.run("list")
        self.assertEqual(list_result.returncode, 0, list_result.stderr.decode())
        row = self._row_line(list_result.stdout.decode(), name)
        self.assertIn("clean", row)
        self.assertNotIn("docsfix", row)

        release_result = self.harness.run("release", name, "--for", "docs-check")
        self.assertEqual(release_result.returncode, 0, release_result.stderr.decode())


class ReapPrunesDeliveredBranches(ListVerdictTestCase):

    def _stub_gh(self, prs):
        cases = ""
        for branch, answer in prs.items():
            if answer is None:
                cases += f"  {branch}) exit 1 ;;\n"
            else:
                cases += f"  {branch}) echo '{json.dumps(answer)}' ;;\n"
        self.harness._write_stub(
            "gh",
            "#!/usr/bin/env bash\n"
            f'printf "%s\\n" "$*" >> "{self._gh_log}"\n'
            'head="" prev=""\n'
            'for a in "$@"; do\n'
            '  [[ "$prev" == "--head" ]] && head="$a"\n'
            '  prev="$a"\n'
            "done\n"
            'case "$head" in\n'
            f"{cases}"
            "  *) echo '[]' ;;\n"
            "esac\n",
        )

    def _commit_on(self, repo, branch, push_as=None, stay=False):
        git = self._git(repo)
        git("checkout", "-q", "-b", branch)
        with open(os.path.join(repo, branch.replace("/", "_") + ".txt"), "w", encoding="utf-8") as handle:
            handle.write(f"{branch}\n")
        git("add", "-A")
        git("commit", "-q", "-m", branch)
        if push_as:
            git("push", "-q", "origin", f"HEAD:refs/heads/{push_as}")
            git("fetch", "-q", "origin")
        tip = git("rev-parse", "HEAD").stdout.decode().strip()
        if not stay:
            git("checkout", "-q", "-")
        return tip

    def _has_branch(self, repo, branch):
        return subprocess.run(
            [self.harness.real_git_bin, "-C", repo, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        ).returncode == 0

    def _master_tip(self, slot):
        return self._git(slot)("rev-parse", "origin/master").stdout.decode().strip()

    def test_1_a_branch_reachable_from_a_remote_ref_is_deleted_and_logged_with_its_sha(self):
        slot = self._ready_slot("free")
        self._stub_gh({})
        tip = self._commit_on(slot, "landed", push_as="x")

        result = self.harness.run("reap")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertFalse(self._has_branch(slot, "landed"), result.stdout.decode())
        log = [line for line in result.stdout.decode().splitlines() if "landed" in line]
        self.assertTrue(log, "the deletion was not logged: " + result.stdout.decode())
        self.assertIn(tip, log[0])

    def test_2_a_branch_with_a_pr_at_exactly_its_tip_is_deleted(self):
        slot = self._ready_slot("free")
        tip = self._commit_on(slot, "pr-at-tip")
        self._stub_gh({"pr-at-tip": [{"headRefOid": tip, "state": "MERGED", "number": 5}]})

        result = self.harness.run("reap")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertFalse(self._has_branch(slot, "pr-at-tip"), result.stdout.decode())
        self.assertIn(tip, result.stdout.decode())

    def test_3_a_branch_whose_pr_merged_at_a_different_tip_is_kept(self):
        slot = self._ready_slot("free")
        self._commit_on(slot, "earlier-revision")
        self._stub_gh(
            {"earlier-revision": [{"headRefOid": self._master_tip(slot), "state": "MERGED", "number": 6}]}
        )

        result = self.harness.run("reap")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertTrue(self._has_branch(slot, "earlier-revision"), result.stdout.decode())

    def test_4_a_branch_with_no_pr_or_a_failing_gh_is_kept(self):
        slot = self._ready_slot("free")
        self._commit_on(slot, "no-pr")
        self._commit_on(slot, "gh-fails")
        self._stub_gh({"gh-fails": None})

        result = self.harness.run("reap")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertTrue(self._has_branch(slot, "no-pr"), result.stdout.decode())
        self.assertTrue(self._has_branch(slot, "gh-fails"), result.stdout.decode())

    def test_5_a_leased_or_parked_slot_keeps_its_delivered_branches(self):
        leased = self._ready_slot("leased", index=0)
        parked = self._ready_slot("parked", index=1)
        self._stub_gh({})
        self._commit_on(leased, "landed-leased", push_as="x-leased")
        self._commit_on(parked, "landed-parked", push_as="x-parked")
        self._write_lease("leased")
        self._write_parked("parked")

        result = self.harness.run("reap")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertTrue(self._has_branch(leased, "landed-leased"), result.stdout.decode())
        self.assertTrue(self._has_branch(parked, "landed-parked"), result.stdout.decode())

    def test_6_the_checked_out_branch_is_kept_even_if_delivered(self):
        slot = self._ready_slot("free")
        self._stub_gh({})
        self._commit_on(slot, "sitting-on-it", push_as="x", stay=True)

        result = self.harness.run("reap")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertTrue(self._has_branch(slot, "sitting-on-it"), result.stdout.decode())

    def test_7_list_counts_what_reap_would_delete(self):
        slot = self._ready_slot("free")
        self._stub_gh({})
        self._commit_on(slot, "landed", push_as="x")
        self._commit_on(slot, "no-pr")

        before = self.harness.run("list")

        self.assertEqual(before.returncode, 0, before.stderr.decode())
        self.assertIn("prunable:1", self._row_line(before.stdout.decode(), "free"))

        reaped = self.harness.run("reap")
        self.assertEqual(reaped.returncode, 0, reaped.stderr.decode())
        after = self.harness.run("list")

        self.assertEqual(after.returncode, 0, after.stderr.decode())
        self.assertNotIn("prunable", self._row_line(after.stdout.decode(), "free"))

    def test_a_delivered_branch_in_docs_repo_is_deleted_too(self):
        slot = self._ready_slot("free")
        self._stub_gh({})
        docs = os.path.join(slot, "docs", "repo")
        shutil.rmtree(docs)
        subprocess.run(
            [self.harness.real_git_bin, "clone", "-q", self.harness.docs_repo_stub, docs],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        tip = self._commit_on(docs, "docs-landed", push_as="docs-x")

        result = self.harness.run("reap")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertFalse(self._has_branch(docs, "docs-landed"), result.stdout.decode())
        self.assertIn(tip, result.stdout.decode())

    def test_8_a_squash_merged_branch_is_pruned_while_residue_branches_survive(self):
        slot = self._ready_slot("free")
        self._stub_gh({})
        git = self._git(slot)

        git("checkout", "-q", "-b", "squash-work")
        with open(os.path.join(slot, "feature.txt"), "w", encoding="utf-8") as handle:
            handle.write("landed via a squash merge\n")
        git("add", "feature.txt")
        git("commit", "-q", "-m", "feature work")
        git("checkout", "-q", "master")
        git("merge", "-q", "--squash", "squash-work")
        git("commit", "-q", "-m", "feature work (squashed)")
        with open(os.path.join(slot, "unrelated.txt"), "w", encoding="utf-8") as handle:
            handle.write("master keeps moving after the squash lands\n")
        git("add", "unrelated.txt")
        git("commit", "-q", "-m", "unrelated later work")

        git("checkout", "-q", "-b", "still-brewing")
        with open(os.path.join(slot, "wip.txt"), "w", encoding="utf-8") as handle:
            handle.write("never landed anywhere\n")
        git("add", "wip.txt")
        git("commit", "-q", "-m", "wip")
        git("checkout", "-q", "master")

        git("checkout", "-q", "-b", "multi-commit-work")
        with open(os.path.join(slot, "part-one.txt"), "w", encoding="utf-8") as handle:
            handle.write("first half of the change\n")
        git("add", "part-one.txt")
        git("commit", "-q", "-m", "part one")
        with open(os.path.join(slot, "part-two.txt"), "w", encoding="utf-8") as handle:
            handle.write("second half of the change\n")
        git("add", "part-two.txt")
        git("commit", "-q", "-m", "part two")
        git("checkout", "-q", "master")
        git("merge", "-q", "--squash", "multi-commit-work")
        git("commit", "-q", "-m", "multi-commit work (squashed)")

        git("push", "-q", "origin", "master")
        git("fetch", "-q", "-p", "origin")
        git("checkout", "-q", "--detach", "HEAD")

        result = self.harness.run("reap")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertFalse(self._has_branch(slot, "squash-work"), result.stdout.decode())
        self.assertTrue(self._has_branch(slot, "still-brewing"), result.stdout.decode())
        self.assertFalse(self._has_branch(slot, "multi-commit-work"), result.stdout.decode())

    def test_9_a_branch_git_cherry_cannot_actually_compare_is_kept_by_reap(self):
        slot = self._ready_slot("free")
        self._stub_gh({})
        git = self._git(slot)
        git("checkout", "-q", "-b", "cherry-fails")
        with open(os.path.join(slot, "part-one.txt"), "w", encoding="utf-8") as handle:
            handle.write("an ancestor commit git cherry cannot actually read\n")
        git("add", "part-one.txt")
        git("commit", "-q", "-m", "part one")
        ancestor = git("rev-parse", "cherry-fails").stdout.decode().strip()
        with open(os.path.join(slot, "part-two.txt"), "w", encoding="utf-8") as handle:
            handle.write("the tip itself, still a perfectly valid object\n")
        git("add", "part-two.txt")
        git("commit", "-q", "-m", "part two")
        git("checkout", "-q", "--detach", "master")
        obj_path = os.path.join(slot, ".git", "objects", ancestor[:2], ancestor[2:])
        self.assertTrue(os.path.isfile(obj_path), "fixture assumption: the ancestor is still a loose object")
        os.remove(obj_path)

        result = self.harness.run("reap")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertTrue(self._has_branch(slot, "cherry-fails"), result.stdout.decode())

    def test_10_a_genuinely_undelivered_branch_in_a_shallow_docs_repo_is_kept_by_reap(self):
        slot = self._ready_slot("free")
        self._stub_gh({})
        docs = os.path.join(slot, "docs", "repo")
        shutil.rmtree(docs)
        subprocess.run(
            [self.harness.real_git_bin, "clone", "-q", "--depth", "1",
             "file://" + self.harness.docs_repo_stub, docs],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self.assertTrue(
            os.path.isfile(os.path.join(docs, ".git", "shallow")),
            "fixture assumption: docs/repo is actually shallow",
        )
        self._commit_on(docs, "docs-still-brewing")

        result = self.harness.run("reap")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertTrue(self._has_branch(docs, "docs-still-brewing"), result.stdout.decode())


class ReleaseWipesADeliveredSlot(ListVerdictTestCase):
    def setUp(self):
        super().setUp()
        self.slot = self._ready_slot("rel")
        self.git = self._git(self.slot)
        self.harness.mark_running("rel")
        self.key = os.path.join(self.harness.tmp, "rel-fixture-key")
        for path in (self.key, self.key + ".pub"):
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("key" + chr(10))
        self.lease = self.harness.lease_path("rel")
        os.makedirs(os.path.dirname(self.lease), exist_ok=True)
        with open(self.lease, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "holder": "test",
                    "project": PROJECT_PREFIX + "-rel",
                    "since": int(time.time()),
                    "ttl_seconds": 14400,
                    "key": self.key,
                    "ssh_port": TEST_SSH_PORT,
                },
                handle,
            )

    _stub_gh = ReapPrunesDeliveredBranches._stub_gh
    _commit_on = ReapPrunesDeliveredBranches._commit_on
    _has_branch = ReapPrunesDeliveredBranches._has_branch
    _master_tip = ReapPrunesDeliveredBranches._master_tip

    def _head_branch(self):
        done = subprocess.run(
            [self.harness.real_git_bin, "-C", self.slot, "symbolic-ref", "-q", "--short", "HEAD"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        return done.stdout.decode().strip()

    def _release(self, *flags):
        return self.harness.run("release", "rel", "--for", "test", *flags)

    def _docker_log(self):
        if not os.path.isfile(self.harness.docker_log):
            return ""
        with open(self.harness.docker_log, encoding="utf-8") as handle:
            return handle.read()

    def _running(self):
        return os.path.isfile(os.path.join(self.harness.docker_state, f"{PROJECT_PREFIX}-rel.running"))

    def _assert_wiped(self, result):
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertFalse(os.path.isdir(self.slot), "the slot directory survived: " + result.stdout.decode())
        self.assertFalse(os.path.isfile(self.lease), "the lease survived the wipe")
        self.assertFalse(os.path.exists(self.key), "the private key survived the wipe")
        self.assertFalse(os.path.exists(self.key + ".pub"), "the public key survived the wipe")
        self.assertFalse(self._running())

    def _assert_untouched(self, result):
        self.assertNotEqual(result.returncode, 0, result.stdout.decode())
        self.assertTrue(os.path.isdir(self.slot), "the slot directory was removed")
        self.assertTrue(os.path.isfile(self.lease), "the lease was removed")
        self.assertTrue(os.path.exists(self.key), "the key was removed")
        self.assertTrue(self._running(), "the containers were stopped")
        self.assertNotIn("down -v", self._docker_log())

    def test_1_a_slot_whose_branches_are_all_delivered_is_wiped(self):
        self._commit_on(self.slot, "other", push_as="other-pushed")
        self._commit_on(self.slot, "mine", push_as="mine-pushed", stay=True)

        result = self._release()

        self._assert_wiped(result)
        self.assertRegex(self._docker_log(), rf"-p {PROJECT_PREFIX}-rel .*down -v")
        self.assertIn("removed", result.stdout.decode())

    def test_2_the_lease_is_still_there_when_the_project_is_taken_down(self):
        self._commit_on(self.slot, "mine", push_as="mine-pushed", stay=True)

        result = self._release()

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        seen = [line for line in self._docker_log().splitlines() if line.startswith("DOWN-LEASES")]
        self.assertEqual(len(seen), 1, self._docker_log())
        self.assertIn(f"{PROJECT_PREFIX}-rel.json", seen[0], "the lease was gone before the teardown ran")

    def test_3_a_branch_pushed_with_its_pull_request_still_open_counts_as_delivered(self):
        tip = self._commit_on(self.slot, "feat-open", push_as="feat-open")
        self._stub_gh({"feat-open": [{"headRefOid": tip, "state": "OPEN", "number": 9}]})

        result = self._release()

        self._assert_wiped(result)

    def test_4_head_on_a_delivered_branch_does_not_block_the_wipe(self):
        self._commit_on(self.slot, "mine", push_as="mine-pushed", stay=True)
        self.assertEqual(self._head_branch(), "mine")

        result = self._release()

        self._assert_wiped(result)

    def test_5_a_held_branch_refuses_names_it_and_leaves_everything_in_place(self):
        self._commit_on(self.slot, "mine", push_as="mine-pushed")
        self._commit_on(self.slot, "unpushed")

        result = self._release()

        self._assert_untouched(result)
        stderr = result.stderr.decode()
        self.assertIn("unpushed", stderr)
        self.assertIn("evict", stderr)
        self.assertNotIn("--force", stderr)
        self.assertTrue(self._has_branch(self.slot, "unpushed"), "a held branch was deleted")
        self.assertTrue(self._has_branch(self.slot, "mine"), "a refused release deleted a branch")

    def test_6_a_worktree_refuses_names_it_and_leaves_everything_in_place(self):
        worktree = os.path.join(self.harness.tmp, "rel-worktree")
        self.git("worktree", "add", "-q", "-b", "wt-branch", worktree)

        result = self._release()

        self._assert_untouched(result)
        self.assertIn("worktree", result.stderr.decode().lower())
        self.assertNotIn("--force", result.stderr.decode())

    def test_7_force_and_keep_up_are_unknown_flags_and_change_nothing(self):
        self._commit_on(self.slot, "mine", push_as="mine-pushed", stay=True)

        for flag in ("--force", "--keep-up"):
            with self.subTest(flag=flag):
                result = self._release(flag)

                self._assert_untouched(result)
                self.assertIn("unknown flag to release", result.stderr.decode())
                self.assertEqual(self._head_branch(), "mine")

    def test_8_a_refused_release_leaves_head_and_every_branch_as_they_were(self):
        self._commit_on(self.slot, "other", push_as="other-pushed")
        self._commit_on(self.slot, "mine", push_as="mine-pushed", stay=True)
        with open(os.path.join(self.slot, "uncommitted.txt"), "w", encoding="utf-8") as handle:
            handle.write("work that exists nowhere else" + chr(10))

        result = self._release()

        self._assert_untouched(result)
        self.assertEqual(self._head_branch(), "mine")
        for branch in ("mine", "other"):
            self.assertTrue(self._has_branch(self.slot, branch), f"a refused release deleted {branch}")

    def test_9_a_directory_that_cannot_be_removed_fails_loudly_and_keeps_the_lease(self):
        if os.geteuid() == 0:
            self.skipTest("root removes anything; the failure needs an unwritable directory")
        self._commit_on(self.slot, "mine", push_as="mine-pushed", stay=True)
        inner = os.path.join(self.slot, "docs", "locked", "inner")
        os.makedirs(inner)
        with open(os.path.join(inner, "root-owned"), "w", encoding="utf-8") as handle:
            handle.write("left behind by a container" + chr(10))
        os.chmod(inner, 0o500)
        self.addCleanup(os.chmod, inner, 0o700)

        result = self._release()

        self.assertNotEqual(result.returncode, 0, result.stdout.decode())
        self.assertIn(self.slot, result.stderr.decode(), "the failure does not name the directory")
        self.assertTrue(os.path.isfile(self.lease), "the lease went with a half-removed slot")


class DocsRepoSitsAtMastersPin(ListVerdictTestCase):

    def _git_in(self, repo):
        return lambda *args: subprocess.run(
            [
                self.harness.real_git_bin, "-C", repo,
                "-c", "commit.gpgsign=false",
                "-c", "user.email=fixture@example.com",
                "-c", "user.name=fixture",
                *args,
            ],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        ).stdout.decode().strip()

    def _docs_commit(self, name):
        git = self._git_in(self.harness.docs_repo_stub)
        with open(os.path.join(self.harness.docs_repo_stub, name), "w", encoding="utf-8") as handle:
            handle.write(name + "\n")
        git("add", name)
        git("commit", "-q", "-m", name)
        return git("rev-parse", "HEAD")

    def _docs_head(self):
        return self._git_in(self.harness.docs_repo_stub)("rev-parse", "HEAD")

    @staticmethod
    def _pin_file(sha):
        return "# the docs commit CI checks citations against\n" + sha + "\n"

    def _pin_in_fixture(self, sha):
        os.makedirs(os.path.join(self.harness.fixture_repo, ".github"), exist_ok=True)
        with open(os.path.join(self.harness.fixture_repo, ".github", "docs-pin"), "w", encoding="utf-8") as handle:
            handle.write(self._pin_file(sha))
        git = self._git_in(self.harness.fixture_repo)
        git("add", ".github/docs-pin")
        git("commit", "-q", "-m", "pin")

    def _push_pin(self, sha):
        remote = self._shared_remote()
        clone = os.path.join(self.harness.tmp, "pin-raiser")
        if not os.path.isdir(clone):
            subprocess.run(
                [self.harness.real_git_bin, "clone", "-q", remote, clone],
                check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
        git = self._git_in(clone)
        git("pull", "-q", "origin", "master")
        os.makedirs(os.path.join(clone, ".github"), exist_ok=True)
        with open(os.path.join(clone, ".github", "docs-pin"), "w", encoding="utf-8") as handle:
            handle.write(self._pin_file(sha))
        git("add", ".github/docs-pin")
        git("commit", "-q", "-m", "raise the docs pin")
        git("push", "-q", "origin", "master")

    def _slot_with_docs_at(self, name, sha, index=0, shallow=False):
        slot = self._ready_slot(name, index=index)
        docs = os.path.join(slot, "docs", "repo")
        shutil.rmtree(docs)
        if shallow:
            subprocess.run(
                [self.harness.real_git_bin, "clone", "-q", "--depth", "1",
                 "file://" + self.harness.docs_repo_stub, docs],
                check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            git = self._git_in(docs)
            git("remote", "set-url", "origin", self.harness.docs_repo_stub)
            git("fetch", "-q", "--depth", "1", "origin", sha)
            git("checkout", "-q", "--detach", sha)
        else:
            subprocess.run(
                [self.harness.real_git_bin, "clone", "-q", self.harness.docs_repo_stub, docs],
                check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            git = self._git_in(docs)
            git("reset", "-q", "--hard", sha)
        return slot

    def _head_of(self, repo):
        return self._git_in(repo)("rev-parse", "HEAD")

    def _catch_up(self, slot):
        git = self._git_in(slot)
        git("fetch", "-q", "origin")
        git("checkout", "-q", "--detach", "origin/master")


    def test_create_puts_docs_repo_at_the_pin_not_at_the_docs_tip(self):
        pin = self._docs_head()
        self._pin_in_fixture(pin)
        tip = self._docs_commit("after-the-pin.md")
        with open(
            os.path.join(self.harness.fixture_repo, ".devcontainer", ".env"), "w", encoding="utf-8"
        ) as handle:
            handle.write(self.ENV)

        result = self.harness.run_acquire()

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        docs = os.path.join(self.harness.slot_dir(self.harness.acquired_slot_name(result)), "docs", "repo")
        self.assertNotEqual(pin, tip, "fixture assumption: the docs tip is past the pin")
        self.assertEqual(self._head_of(docs), pin, result.stdout.decode() + result.stderr.decode())


    def _write_docs_marker(self, docs, sha):
        git_dir = self._git_in(docs)("rev-parse", "--git-dir")
        if not os.path.isabs(git_dir):
            git_dir = os.path.join(docs, git_dir)
        with open(os.path.join(git_dir, "slot-docs-pin"), "w", encoding="utf-8") as handle:
            handle.write(sha + "\n")

    def _write_release_ready_lease(self, name, holder):
        path = self.harness.lease_path(name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        key = os.path.join(self.harness.tmp, f"{name}-fixture-key")
        open(key, "w", encoding="utf-8").close()
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "holder": holder,
                    "owner": "-",
                    "project": PROJECT_PREFIX + "-" + name,
                    "since": int(time.time()) - 60,
                    "ttl_seconds": 14400,
                    "key": key,
                    "ssh_port": TEST_SSH_PORT,
                },
                handle,
            )
        return path

    def test_a_shallow_docs_repo_at_a_previous_pin_sync_recorded_is_not_held_work(self):
        old = self._docs_head()
        self._docs_commit("past-the-old-pin.md")
        slot = self._slot_with_docs_at("pinned", old, shallow=True)
        docs = os.path.join(slot, "docs", "repo")
        self._write_docs_marker(docs, old)
        new = self._docs_commit("current-pin.md")
        self._push_pin(new)
        self._catch_up(slot)
        self._write_release_ready_lease("pinned", "task-19")

        result = self.harness.run("release", "pinned", "--for", "task-19")

        self.assertEqual(result.returncode, 0, result.stderr.decode())

    def test_a_real_commit_on_top_of_the_recorded_marker_is_still_held_work(self):
        old = self._docs_head()
        self._docs_commit("past-the-old-pin.md")
        slot = self._slot_with_docs_at("pinned", old, shallow=True)
        docs = os.path.join(slot, "docs", "repo")
        self._write_docs_marker(docs, old)
        new = self._docs_commit("current-pin.md")
        self._push_pin(new)
        self._catch_up(slot)
        self._write_release_ready_lease("pinned", "task-19")
        git = self._git_in(docs)
        with open(os.path.join(docs, "notes.md"), "w", encoding="utf-8") as handle:
            handle.write("a real commit, not the marker sha\n")
        git("add", "notes.md")
        git("commit", "-q", "-m", "a real commit on top, still detached")

        result = self.harness.run("release", "pinned", "--for", "task-19")

        self.assertNotEqual(result.returncode, 0, result.stdout.decode())
        stderr = result.stderr.decode()
        self.assertIn("docs/repo", stderr)
        self.assertIn("read-only", stderr)


    def test_list_flags_a_clone_at_its_own_pin_when_masters_has_moved(self):
        old = self._docs_head()
        slot = self._slot_with_docs_at("stale", old)
        self._push_pin(old)
        self._catch_up(slot)
        new = self._docs_commit("raised.md")
        self._push_pin(new)
        self._git_in(slot)("fetch", "-q", "origin")

        result = self.harness.run("list")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        stdout = result.stdout.decode()
        self.assertIn("off-pin", self._row_line(stdout, "stale"), stdout)
        self.assertIn(old[:7], stdout)
        self.assertIn(new[:7], stdout)

    def test_list_flags_a_clone_behind_and_one_ahead_of_the_pin(self):
        before = self._docs_head()
        pin = self._docs_commit("pinned.md")
        after = self._docs_commit("after.md")
        self._slot_with_docs_at("behind", before, index=0)
        self._slot_with_docs_at("ahead", after, index=1)
        self._push_pin(pin)
        self._catch_up(self.harness.slot_dir("behind"))
        self._catch_up(self.harness.slot_dir("ahead"))

        result = self.harness.run("list")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        stdout = result.stdout.decode()
        self.assertIn("off-pin", self._row_line(stdout, "behind"), stdout)
        self.assertIn("off-pin", self._row_line(stdout, "ahead"), stdout)


if __name__ == "__main__":
    unittest.main()
