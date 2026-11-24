
import glob
import json
import os
import re
import subprocess
import time
import unittest

import test_delivery as td
from harness import DevContainerTestCase, PROJECT_PREFIX


class BranchDeliveryUsesMergeTree(DevContainerTestCase):

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
            "  pr-mismatch)\n"
            '    echo \'[{"headRefOid":"deadbeefdeadbeefdeadbeefdeadbeefdeadbeef","state":"MERGED","number":99}]\'\n'
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

    def _cache_key(self, branch, tip):
        return f"{self.harness.project_key}-{branch.replace('/', '_')}-{tip}"

    def test_1_a_squash_merged_multi_commit_branch_is_delivered(self):
        self.git("checkout", "-q", "-b", "squash-work")
        with open(os.path.join(self.slot, "part-one.txt"), "w", encoding="utf-8") as handle:
            handle.write("first half of the change\n")
        self.git("add", "part-one.txt")
        self.git("commit", "-q", "-m", "part one")
        with open(os.path.join(self.slot, "part-two.txt"), "w", encoding="utf-8") as handle:
            handle.write("second half of the change\n")
        self.git("add", "part-two.txt")
        self.git("commit", "-q", "-m", "part two")
        self.git("checkout", "-q", "master")
        self.git("merge", "-q", "--squash", "squash-work")
        self.git("commit", "-q", "-m", "squash work (squashed)")
        self.git("push", "-q", "origin", "master")
        self.git("fetch", "-q", "origin")

        result = self.harness.run("release", "delivery", "--for", "test")

        self.assertEqual(result.returncode, 1, result.stdout.decode())
        stderr = result.stderr.decode()
        self.assertNotIn(
            "    squash-work:", stderr,
            "a squash-merged multi-commit branch was reported held: " + stderr,
        )

    def test_2_a_branch_replayed_elsewhere_and_then_squash_merged_is_delivered(self):
        self.git("checkout", "-q", "-b", "replay-work")
        with open(os.path.join(self.slot, "replay-one.txt"), "w", encoding="utf-8") as handle:
            handle.write("first half, replayed elsewhere\n")
        self.git("add", "replay-one.txt")
        self.git("commit", "-q", "-m", "replay part one")
        with open(os.path.join(self.slot, "replay-two.txt"), "w", encoding="utf-8") as handle:
            handle.write("second half, replayed elsewhere\n")
        self.git("add", "replay-two.txt")
        self.git("commit", "-q", "-m", "replay part two")

        replica = os.path.join(self.harness.tmp, "replica-clone")
        subprocess.run(
            [self.harness.real_git_bin, "clone", "-q", self.remote, replica],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        replica_git = lambda *args: subprocess.run(
            [self.harness.real_git_bin, "-C", replica, "-c", "commit.gpgsign=false",
             "-c", "user.email=fixture@example.com", "-c", "user.name=fixture", *args],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        patch = subprocess.run(
            [self.harness.real_git_bin, "-C", self.slot, "format-patch", "-q", "--stdout",
             "master..replay-work"],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        ).stdout
        replica_git("checkout", "-q", "-b", "replica-work")
        am_result = subprocess.run(
            [self.harness.real_git_bin, "-C", replica,
             "-c", "user.email=fixture@example.com", "-c", "user.name=fixture",
             "am", "-q"],
            input=patch, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self.assertEqual(am_result.returncode, 0, am_result.stderr.decode())
        replica_git("checkout", "-q", "master")
        replica_git("merge", "-q", "--squash", "replica-work")
        replica_git("commit", "-q", "-m", "replay work (squashed)")
        replica_git("push", "-q", "origin", "master")
        self.git("fetch", "-q", "origin")

        result = self.harness.run("release", "delivery", "--for", "test")

        self.assertEqual(result.returncode, 1, result.stdout.decode())
        stderr = result.stderr.decode()
        self.assertNotIn(
            "    replay-work:", stderr,
            "a branch replayed elsewhere and squash-merged was reported held: " + stderr,
        )

    def test_3_a_branch_with_one_extra_commit_beyond_the_squash_stays_held(self):
        self.git("checkout", "-q", "-b", "extra-work")
        with open(os.path.join(self.slot, "extra.txt"), "w", encoding="utf-8") as handle:
            handle.write("first line, later squashed onto master\n")
        self.git("add", "extra.txt")
        self.git("commit", "-q", "-m", "extra work part one")
        self.git("checkout", "-q", "master")
        self.git("merge", "-q", "--squash", "extra-work")
        self.git("commit", "-q", "-m", "extra work (squashed)")
        self.git("push", "-q", "origin", "master")
        self.git("checkout", "-q", "extra-work")
        with open(os.path.join(self.slot, "extra.txt"), "a", encoding="utf-8") as handle:
            handle.write("second line, never squashed at all\n")
        self.git("commit", "-q", "-am", "one more commit beyond the squash")
        self.git("fetch", "-q", "origin")

        result = self.harness.run("release", "delivery", "--for", "test")

        self.assertEqual(result.returncode, 1, result.stdout.decode())
        stderr = result.stderr.decode()
        self.assertIn("extra-work", stderr)

    def test_4_a_branch_whose_change_was_later_reverted_on_master_stays_held(self):
        self.git("checkout", "-q", "-b", "reverted-work")
        with open(os.path.join(self.slot, "revert-one.txt"), "w", encoding="utf-8") as handle:
            handle.write("part one of a change master later reverts\n")
        self.git("add", "revert-one.txt")
        self.git("commit", "-q", "-m", "reverted work part one")
        with open(os.path.join(self.slot, "revert-two.txt"), "w", encoding="utf-8") as handle:
            handle.write("part two of the same change\n")
        self.git("add", "revert-two.txt")
        self.git("commit", "-q", "-m", "reverted work part two")
        self.git("checkout", "-q", "master")
        self.git("merge", "-q", "--squash", "reverted-work")
        self.git("commit", "-q", "-m", "reverted work (squashed)")
        squashed = self.git("rev-parse", "HEAD").stdout.decode().strip()
        self.git("revert", "--no-edit", squashed)
        self.git("push", "-q", "origin", "master")
        self.git("fetch", "-q", "origin")

        result = self.harness.run("release", "delivery", "--for", "test")

        self.assertEqual(result.returncode, 1, result.stdout.decode())
        stderr = result.stderr.decode()
        self.assertIn("reverted-work", stderr)

    def test_5_a_branch_that_conflicts_with_master_stays_held(self):
        self.git("checkout", "-q", "-b", "conflict-work")
        with open(os.path.join(self.slot, "README.md"), "w", encoding="utf-8") as handle:
            handle.write("changed by conflict-work\n")
        self.git("commit", "-q", "-am", "conflicting change")
        self.git("checkout", "-q", "master")
        with open(os.path.join(self.slot, "README.md"), "w", encoding="utf-8") as handle:
            handle.write("changed differently on master\n")
        self.git("commit", "-q", "-am", "an unrelated, conflicting master change")
        self.git("push", "-q", "origin", "master")
        self.git("fetch", "-q", "origin")

        result = self.harness.run("release", "delivery", "--for", "test")

        self.assertEqual(result.returncode, 1, result.stdout.decode())
        stderr = result.stderr.decode()
        self.assertIn("conflict-work", stderr)

    def test_6_a_cached_held_verdict_is_overridden_by_a_clean_merge_tree(self):
        self.git("checkout", "-q", "-b", "pr-mismatch")
        with open(os.path.join(self.slot, "mismatch-one.txt"), "w", encoding="utf-8") as handle:
            handle.write("landed via a squash, but a stale PR record disagrees\n")
        self.git("add", "mismatch-one.txt")
        self.git("commit", "-q", "-m", "mismatch work part one")
        with open(os.path.join(self.slot, "mismatch-two.txt"), "w", encoding="utf-8") as handle:
            handle.write("second half of the same change\n")
        self.git("add", "mismatch-two.txt")
        self.git("commit", "-q", "-m", "mismatch work part two")
        self.git("checkout", "-q", "master")
        self.git("merge", "-q", "--squash", "pr-mismatch")
        self.git("commit", "-q", "-m", "mismatch work (squashed)")
        self.git("push", "-q", "origin", "master")
        self.git("fetch", "-q", "origin")

        tip = self.git("rev-parse", "pr-mismatch").stdout.decode().strip()
        closed_cache = os.path.join(
            self.harness.state_dir, "closed", self._cache_key("pr-mismatch", tip)
        )
        os.makedirs(os.path.dirname(closed_cache), exist_ok=True)
        with open(closed_cache, "w", encoding="utf-8") as handle:
            handle.write("pr-mismatch: tip differs from PR #99 (stale, cached)\n")

        result = self.harness.run("release", "delivery", "--for", "test")

        self.assertEqual(result.returncode, 1, result.stdout.decode())
        stderr = result.stderr.decode()
        self.assertNotIn(
            "    pr-mismatch:", stderr,
            "a stale held cache entry was not overridden by a clean merge-tree: " + stderr,
        )

    def _stub_gh_pr(self, branch, merge_oid=None, head_ref_oid=None, state="MERGED", number=100):
        head_ref_oid = head_ref_oid or "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
        merge_commit = json.dumps({"oid": merge_oid}) if merge_oid else "null"
        pr = (
            f'{{"headRefOid":"{head_ref_oid}","state":"{state}",'
            f'"number":{number},"mergeCommit":{merge_commit}}}'
        )
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
            f"  {branch})\n"
            f"    echo '[{pr}]'\n"
            "    ;;\n"
            "  *)\n"
            "    echo '[]'\n"
            "    ;;\n"
            "esac\n",
        )

    def _gh_call_count(self, log_path):
        if not os.path.isfile(log_path):
            return 0
        with open(log_path, encoding="utf-8") as handle:
            return sum(1 for line in handle if line.strip())

    def test_7_a_pr_merge_commit_anchors_delivery_past_a_later_unrelated_master_edit(self):
        self.git("checkout", "-q", "-b", "busy-file-work")
        with open(os.path.join(self.slot, "shared.txt"), "w", encoding="utf-8") as handle:
            handle.write("line one\nline two\nline three\n")
        self.git("add", "shared.txt")
        self.git("commit", "-q", "-m", "add shared.txt")
        self.git("checkout", "-q", "master")
        self.git("merge", "-q", "--ff-only", "busy-file-work")
        self.git("push", "-q", "origin", "master")

        self.git("checkout", "-q", "-b", "busy-file-change")
        with open(os.path.join(self.slot, "shared.txt"), "w", encoding="utf-8") as handle:
            handle.write("line one\nCHANGED-BY-BRANCH\nline three\n")
        self.git("commit", "-q", "-am", "change the shared line")
        with open(os.path.join(self.slot, "other.txt"), "w", encoding="utf-8") as handle:
            handle.write("second commit, unrelated file\n")
        self.git("add", "other.txt")
        self.git("commit", "-q", "-m", "an unrelated second commit")
        self.git("checkout", "-q", "master")
        self.git("merge", "-q", "--squash", "busy-file-change")
        self.git("commit", "-q", "-m", "busy-file-change (squashed)")
        merge_sha = self.git("rev-parse", "master").stdout.decode().strip()
        self.git("push", "-q", "origin", "master")

        with open(os.path.join(self.slot, "shared.txt"), "w", encoding="utf-8") as handle:
            handle.write("line one\nCHANGED-BY-BRANCH-then-master-too\nline three\n")
        self.git("commit", "-q", "-am", "master edits the same line again later")
        self.git("push", "-q", "origin", "master")
        self.git("fetch", "-q", "origin")

        self._stub_gh_pr("busy-file-change", merge_oid=merge_sha, number=101)

        result = self.harness.run("release", "delivery", "--for", "test")

        self.assertEqual(result.returncode, 1, result.stdout.decode())
        stderr = result.stderr.decode()
        self.assertNotIn(
            "    busy-file-change:", stderr,
            "a merge-commit-anchored branch was still reported held: " + stderr,
        )

    def test_8_the_same_branch_with_one_extra_commit_beyond_the_pr_stays_held(self):
        self.git("checkout", "-q", "-b", "busy-file-work-2")
        with open(os.path.join(self.slot, "shared2.txt"), "w", encoding="utf-8") as handle:
            handle.write("line one\nline two\nline three\n")
        self.git("add", "shared2.txt")
        self.git("commit", "-q", "-m", "add shared2.txt")
        self.git("checkout", "-q", "master")
        self.git("merge", "-q", "--ff-only", "busy-file-work-2")
        self.git("push", "-q", "origin", "master")

        self.git("checkout", "-q", "-b", "busy-file-change-2")
        with open(os.path.join(self.slot, "shared2.txt"), "w", encoding="utf-8") as handle:
            handle.write("line one\nCHANGED-BY-BRANCH\nline three\n")
        self.git("commit", "-q", "-am", "change the shared line")
        self.git("checkout", "-q", "master")
        self.git("merge", "-q", "--squash", "busy-file-change-2")
        self.git("commit", "-q", "-m", "busy-file-change-2 (squashed)")
        merge_sha = self.git("rev-parse", "master").stdout.decode().strip()
        self.git("push", "-q", "origin", "master")

        with open(os.path.join(self.slot, "shared2.txt"), "w", encoding="utf-8") as handle:
            handle.write("line one\nCHANGED-BY-BRANCH-then-master-too\nline three\n")
        self.git("commit", "-q", "-am", "master edits the same line again later")
        self.git("push", "-q", "origin", "master")

        self.git("checkout", "-q", "busy-file-change-2")
        with open(os.path.join(self.slot, "never-merged.txt"), "w", encoding="utf-8") as handle:
            handle.write("beyond the PR's own merge commit\n")
        self.git("add", "never-merged.txt")
        self.git("commit", "-q", "-m", "one more commit never in the PR")
        self.git("fetch", "-q", "origin")

        self._stub_gh_pr("busy-file-change-2", merge_oid=merge_sha, number=102)

        result = self.harness.run("release", "delivery", "--for", "test")

        self.assertEqual(result.returncode, 1, result.stdout.decode())
        stderr = result.stderr.decode()
        self.assertIn("busy-file-change-2", stderr)

    def test_9_a_merge_commit_oid_absent_locally_is_never_fetched_and_stays_held(self):
        self.git("checkout", "-q", "-b", "unfetched-merge-commit")
        with open(os.path.join(self.slot, "wip.txt"), "w", encoding="utf-8") as handle:
            handle.write("never landed, never reachable from any remote ref\n")
        self.git("add", "wip.txt")
        self.git("commit", "-q", "-m", "wip")

        self._stub_gh_pr(
            "unfetched-merge-commit",
            merge_oid="cafefacecafefacecafefacecafefacecafeface",
            number=103,
        )

        result = self.harness.run("release", "delivery", "--for", "test")

        self.assertEqual(result.returncode, 1, result.stdout.decode())
        stderr = result.stderr.decode()
        self.assertIn("unfetched-merge-commit", stderr)

    def _busy_file_collision(self, work_branch, change_branch, filename):
        self.git("checkout", "-q", "-b", work_branch)
        with open(os.path.join(self.slot, filename), "w", encoding="utf-8") as handle:
            handle.write("line one\nline two\nline three\n")
        self.git("add", filename)
        self.git("commit", "-q", "-m", f"add {filename}")
        self.git("checkout", "-q", "master")
        self.git("merge", "-q", "--ff-only", work_branch)
        self.git("push", "-q", "origin", "master")

        self.git("checkout", "-q", "-b", change_branch)
        with open(os.path.join(self.slot, filename), "w", encoding="utf-8") as handle:
            handle.write("line one\nCHANGED-BY-BRANCH\nline three\n")
        self.git("commit", "-q", "-am", "change the shared line")
        other_filename = f"other-{filename}"
        with open(os.path.join(self.slot, other_filename), "w", encoding="utf-8") as handle:
            handle.write("second commit, unrelated file\n")
        self.git("add", other_filename)
        self.git("commit", "-q", "-m", "an unrelated second commit")
        self.git("checkout", "-q", "master")
        self.git("merge", "-q", "--squash", change_branch)
        self.git("commit", "-q", "-m", f"{change_branch} (squashed)")
        merge_sha = self.git("rev-parse", "master").stdout.decode().strip()
        self.git("push", "-q", "origin", "master")

        with open(os.path.join(self.slot, filename), "w", encoding="utf-8") as handle:
            handle.write("line one\nCHANGED-BY-BRANCH-then-master-too\nline three\n")
        self.git("commit", "-q", "-am", "master edits the same line again later")
        self.git("push", "-q", "origin", "master")
        self.git("fetch", "-q", "origin")
        return merge_sha

    def _closed_cache_path(self, branch, tip):
        path = os.path.join(self.harness.state_dir, "closed", self._cache_key(branch, tip))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        return path

    def test_10_a_legacy_closed_cache_entry_with_no_sidecar_is_re_asked_once_then_delivered(self):
        change_branch = "busy-file-legacy"
        merge_sha = self._busy_file_collision(
            "busy-file-legacy-work", change_branch, "shared-legacy.txt"
        )
        self._stub_gh_pr(change_branch, merge_oid=merge_sha, number=110)

        tip = self.git("rev-parse", change_branch).stdout.decode().strip()
        closed_cache = self._closed_cache_path(change_branch, tip)
        with open(closed_cache, "w", encoding="utf-8") as handle:
            handle.write(f"{change_branch}: tip differs from PR #10 (stale, no sidecar)\n")
        self.assertFalse(os.path.isfile(closed_cache + ".merge-commits"))

        log_path = os.path.join(self.harness.tmp, "gh-calls-legacy.log")
        result = self.harness.run(
            "release", "delivery", "--for", "test",
            port_overrides={"GH_CALL_LOG": log_path},
        )

        self.assertEqual(result.returncode, 1, result.stdout.decode())
        stderr = result.stderr.decode()
        self.assertNotIn(
            f"    {change_branch}:", stderr,
            "a legacy closed-cache entry was not re-asked and re-anchored: " + stderr,
        )
        self.assertEqual(
            self._gh_call_count(log_path), 1,
            "a legacy closed-cache entry (no sidecar) should cost exactly one gh call, ever",
        )

    def test_11_a_closed_cache_entry_with_a_sidecar_is_delivered_with_zero_gh_calls(self):
        change_branch = "busy-file-sidecar-delivered"
        merge_sha = self._busy_file_collision(
            "busy-file-sidecar-delivered-work", change_branch, "shared-sidecar-1.txt"
        )
        self._stub_gh_pr(change_branch, merge_oid=merge_sha, number=111)

        tip = self.git("rev-parse", change_branch).stdout.decode().strip()
        closed_cache = self._closed_cache_path(change_branch, tip)
        with open(closed_cache, "w", encoding="utf-8") as handle:
            handle.write(f"{change_branch}: tip differs from PR #11 (cached)\n")
        with open(closed_cache + ".merge-commits", "w", encoding="utf-8") as handle:
            handle.write(merge_sha + "\n")

        log_path = os.path.join(self.harness.tmp, "gh-calls-sidecar-delivered.log")
        result = self.harness.run(
            "release", "delivery", "--for", "test",
            port_overrides={"GH_CALL_LOG": log_path},
        )

        self.assertEqual(result.returncode, 1, result.stdout.decode())
        stderr = result.stderr.decode()
        self.assertNotIn(
            f"    {change_branch}:", stderr,
            "a sidecar-backed closed-cache entry was not re-anchored locally: " + stderr,
        )
        self.assertEqual(
            self._gh_call_count(log_path), 0,
            "a closed-cache entry with a sidecar must never re-ask gh",
        )

    def test_12_a_closed_cache_entry_with_a_sidecar_but_an_extra_commit_stays_held_with_zero_gh_calls(self):
        change_branch = "busy-file-sidecar-held"
        merge_sha = self._busy_file_collision(
            "busy-file-sidecar-held-work", change_branch, "shared-sidecar-2.txt"
        )
        self.git("checkout", "-q", change_branch)
        with open(os.path.join(self.slot, "never-merged.txt"), "w", encoding="utf-8") as handle:
            handle.write("beyond the PR's own merge commit\n")
        self.git("add", "never-merged.txt")
        self.git("commit", "-q", "-m", "one more commit never in the PR")
        self.git("fetch", "-q", "origin")
        self._stub_gh_pr(change_branch, merge_oid=merge_sha, number=112)

        tip = self.git("rev-parse", change_branch).stdout.decode().strip()
        closed_cache = self._closed_cache_path(change_branch, tip)
        cached_report = f"{change_branch}: 1 commits beyond PR #12"
        with open(closed_cache, "w", encoding="utf-8") as handle:
            handle.write(cached_report + "\n")
        with open(closed_cache + ".merge-commits", "w", encoding="utf-8") as handle:
            handle.write(merge_sha + "\n")

        log_path = os.path.join(self.harness.tmp, "gh-calls-sidecar-held.log")
        result = self.harness.run(
            "release", "delivery", "--for", "test",
            port_overrides={"GH_CALL_LOG": log_path},
        )

        self.assertEqual(result.returncode, 1, result.stdout.decode())
        self.assertIn(change_branch, result.stderr.decode())
        self.assertEqual(
            self._gh_call_count(log_path), 0,
            "a genuinely-held sidecar-backed entry must still cost zero gh calls",
        )


    def test_13_a_legacy_closed_cache_entry_stays_held_when_gh_cannot_answer(self):
        change_branch = "busy-file-legacy-gh-down"
        self._busy_file_collision(
            "busy-file-legacy-gh-down-work", change_branch, "shared-legacy-gh-down.txt"
        )
        tip = self.git("rev-parse", change_branch).stdout.decode().strip()
        closed_cache = self._closed_cache_path(change_branch, tip)
        cached_report = f"{change_branch}: tip differs from PR #13 (stale, no sidecar)"
        with open(closed_cache, "w", encoding="utf-8") as handle:
            handle.write(cached_report + "\n")
        self.assertFalse(os.path.isfile(closed_cache + ".merge-commits"))

        self.harness._write_stub("gh", "#!/usr/bin/env bash\nexit 1\n")

        first = self.harness.run("release", "delivery", "--for", "test")
        self.assertEqual(first.returncode, 1, first.stdout.decode())
        self.assertIn(f"    {cached_report}", first.stderr.decode())
        self.assertNotIn("unknown (gh refused)", first.stderr.decode())
        self.assertFalse(os.path.isfile(closed_cache + ".merge-commits"))

        second = self.harness.run("release", "delivery", "--for", "test")
        self.assertEqual(second.returncode, 1, second.stdout.decode())
        self.assertIn(f"    {cached_report}", second.stderr.decode())
        self.assertNotIn("unknown (gh refused)", second.stderr.decode())
        self.assertFalse(
            os.path.isfile(closed_cache + ".merge-commits"),
            "gh cannot answer, so a legacy entry must still write no sidecar",
        )


class BranchDeliveryMergedPrHead(DevContainerTestCase):

    def setUp(self):
        super().setUp()
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
        self.log_path = os.path.join(self.harness.tmp, "gh-calls.log")

    def _rev(self, ref):
        return self.git("rev-parse", ref).stdout.decode().strip()

    def _commit_file(self, name, text):
        with open(os.path.join(self.slot, name), "w", encoding="utf-8") as handle:
            handle.write(text)
        self.git("add", name)
        self.git("commit", "-q", "-m", f"add {name}")

    def _branch_with_unpushed_head(self, branch, extra_commits=1):
        self.git("checkout", "-q", "-b", branch, "master")
        self._commit_file(f"{branch}-a.txt", "the branch own change\n")
        tip = self._rev("HEAD")
        head = tip
        if extra_commits:
            self.git("checkout", "-q", "-b", branch + "-scratch")
            for i in range(extra_commits):
                self._commit_file(f"{branch}-extra-{i}.txt", "added by the other slot\n")
            head = self._rev("HEAD")
        self.git("checkout", "-q", "master")
        if extra_commits:
            self.git("branch", "-q", "-D", branch + "-scratch")
        return tip, head

    def _stub_gh(self, branch, prs):
        body = json.dumps(
            [
                {"headRefOid": oid, "state": state, "number": number, "mergeCommit": None}
                for state, number, oid in prs
            ]
        )
        q = chr(39)
        lines = [
            "#!/usr/bin/env bash",
            "log=\"${GH_CALL_LOG:-/dev/null}\"",
            "printf \"%s\\n\" \"$*\" >> \"$log\"",
            "head=\"\" prev=\"\"",
            "for a in \"$@\"; do",
            "  [[ \"$prev\" == \"--head\" ]] && head=\"$a\"",
            "  prev=\"$a\"",
            "done",
            "case \"$head\" in",
            f"  {branch})",
            f"    echo {q}{body}{q}",
            "    ;;",
            "  *)",
            f"    echo {q}[]{q}",
            "    ;;",
            "esac",
        ]
        self.harness._write_stub("gh", "\n".join(lines) + "\n")

    def _gh_calls(self):
        if not os.path.isfile(self.log_path):
            return 0
        with open(self.log_path, encoding="utf-8") as handle:
            return sum(1 for line in handle if line.strip())

    def _release(self):
        return self.harness.run(
            "release", "delivery", "--for", "test",
            port_overrides={"GH_CALL_LOG": self.log_path},
        )

    def _delivered_marker(self, branch, tip):
        key = f"{self.harness.project_key}-{branch.replace('/', '_')}-{tip}"
        return os.path.join(self.harness.state_dir, "delivered", key)

    def _closed_cache(self, branch, tip):
        key = f"{self.harness.project_key}-{branch.replace('/', '_')}-{tip}"
        path = os.path.join(self.harness.state_dir, "closed", key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        return path

    def _assert_held(self, result, branch):
        self.assertEqual(result.returncode, 1, result.stdout.decode())
        self.assertIn(f"    {branch}:", result.stderr.decode())

    def _assert_delivered(self, result, branch):
        self.assertEqual(result.returncode, 1, result.stdout.decode())
        self.assertNotIn(f"    {branch}:", result.stderr.decode(), result.stderr.decode())

    def test_1_a_tip_behind_a_merged_pr_head_is_delivered_and_cached(self):
        branch = "merged-behind"
        tip, head = self._branch_with_unpushed_head(branch)
        self._stub_gh(branch, [("MERGED", 201, head)])

        result = self._release()

        self._assert_delivered(result, branch)
        self.assertTrue(
            os.path.isfile(self._delivered_marker(branch, tip)),
            "a tip behind a merged PR head must be cached as delivered",
        )

    def test_2_a_tip_equal_to_a_merged_pr_head_is_delivered(self):
        branch = "merged-equal"
        tip, head = self._branch_with_unpushed_head(branch, extra_commits=0)
        self.assertEqual(tip, head)
        self._stub_gh(branch, [("MERGED", 202, head)])

        self._assert_delivered(self._release(), branch)

    def test_3_the_same_shape_against_a_closed_pr_stays_held(self):
        branch = "closed-behind"
        tip, head = self._branch_with_unpushed_head(branch)
        self._stub_gh(branch, [("CLOSED", 203, head)])

        self._assert_held(self._release(), branch)
        self.assertFalse(os.path.isfile(self._delivered_marker(branch, tip)))

    def test_4_a_matching_merged_pr_that_is_not_first_in_the_response_is_found(self):
        branch = "merged-second"
        tip, head = self._branch_with_unpushed_head(branch)
        other = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
        self._stub_gh(branch, [("CLOSED", 204, other), ("MERGED", 205, head)])

        result = self._release()

        self._assert_delivered(result, branch)
        self.assertTrue(os.path.isfile(self._delivered_marker(branch, tip)))

    def test_5_a_closed_cache_sidecar_listing_a_merged_pr_head_reads_delivered_without_gh(self):
        branch = "merged-sidecar"
        tip, head = self._branch_with_unpushed_head(branch)
        self._stub_gh(branch, [("MERGED", 206, head)])
        closed_cache = self._closed_cache(branch, tip)
        with open(closed_cache, "w", encoding="utf-8") as handle:
            handle.write(f"{branch}: 0 commits beyond PR #26 (stale)\n")
        with open(closed_cache + ".merge-commits", "w", encoding="utf-8") as handle:
            handle.write(head + "\n")

        result = self._release()

        self._assert_delivered(result, branch)
        self.assertEqual(
            self._gh_calls(), 0,
            "a sidecar listing a merged head the tip is behind must not re-ask gh",
        )

    def test_6_a_tip_with_commits_beyond_every_pr_head_stays_held(self):
        branch = "beyond-heads"
        tip, head = self._branch_with_unpushed_head(branch)
        self.git("checkout", "-q", branch)
        self._commit_file("beyond.txt", "never in any PR\n")
        self.git("checkout", "-q", "master")
        other = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
        self._stub_gh(branch, [("MERGED", 207, head), ("CLOSED", 208, other)])

        self._assert_held(self._release(), branch)


class DeliveryCase(td.BranchDeliveryAsksReachability):
    # the fixture of test_delivery, none of its tests
    for _name in dir(td.BranchDeliveryAsksReachability):
        if _name.startswith("test"):
            locals()[_name] = None

    def setUp(self):
        super().setUp()
        self.key = self.harness.project_key
        self.label = PROJECT_PREFIX
        self.assertNotEqual(self.key, self.label)
        self.delivered = os.path.join(self.harness.state_dir, "delivered")

    def commit_file(self, name):
        with open(os.path.join(self.slot, name), "w", encoding="utf-8") as handle:
            handle.write(name + chr(10))
        self.git("add", name)
        self.git("commit", "-q", "-m", name)

    def tip(self, branch):
        return self.git("rev-parse", branch).stdout.decode().strip()

    def entries(self, pattern="*"):
        return sorted(os.path.basename(p) for p in glob.glob(os.path.join(self.delivered, pattern)))

    def cherry_equivalent_branch(self, branch):
        # on master under another commit, so the branch tip itself never reaches the remote
        self.git("checkout", "-q", "-b", branch)
        self.commit_file(branch + ".txt")
        self.git("checkout", "-q", "master")
        self.commit_file("unrelated-" + branch + ".txt")
        self.git("cherry-pick", branch)
        self.git("push", "-q", "origin", "master")
        self.git("fetch", "-q", "origin")
        return self.tip(branch)

    def undelivered_branch(self, branch):
        self.git("checkout", "-q", "-b", branch)
        self.commit_file(branch + ".txt")
        tip = self.tip(branch)
        self.git("checkout", "-q", "master")
        return tip

    def text(self, res):
        return (res.stdout + res.stderr).decode()


class DeliveryCacheNamedByKey(DeliveryCase):
    def test_a_delivered_verdict_is_cached_under_the_key_and_not_the_label(self):
        tip = self.cherry_equivalent_branch("cache-me")

        self.harness.run("release", "delivery", "--for", "test")

        self.assertEqual(self.entries(self.key + "-*"), [self.key + "-cache-me-" + tip])
        self.assertEqual(self.entries(self.label + "-*"), [])

    def test_an_entry_kept_under_the_label_is_not_read_as_delivered(self):
        tip = self.undelivered_branch("legacy-planted")
        os.makedirs(self.delivered, exist_ok=True)
        planted = os.path.join(self.delivered, self.label + "-legacy-planted-" + tip)
        with open(planted, "w", encoding="utf-8"):
            pass

        res = self.harness.run("release", "delivery", "--for", "test")

        self.assertEqual(res.returncode, 1, self.text(res))
        self.assertIn("legacy-planted", res.stderr.decode())
        self.assertEqual(self.entries(self.key + "-*"), [])

    def test_an_entry_under_the_key_is_read_as_delivered(self):
        tip = self.undelivered_branch("key-planted")
        os.makedirs(self.delivered, exist_ok=True)
        with open(os.path.join(self.delivered, self.key + "-key-planted-" + tip), "w", encoding="utf-8"):
            pass

        res = self.harness.run("release", "delivery", "--for", "test")

        self.assertNotIn("key-planted", res.stderr.decode())


class ArchiveShownUnderItsLabel(DevContainerTestCase):
    def setUp(self):
        super().setUp()
        self.harness.build_fixture_repo()
        self.state = self.harness.state_dir
        self.key = self.harness.project_key
        self.label = PROJECT_PREFIX

    def plant(self, dirname, lease, meta):
        adir = os.path.join(self.state, "rescue", dirname, lease)
        os.makedirs(adir, exist_ok=True)
        if meta is not None:
            with open(os.path.join(adir, "meta.json"), "w", encoding="utf-8") as handle:
                json.dump(meta, handle)
        return adir

    def register(self, key, label):
        os.makedirs(os.path.join(self.state, "projects"), exist_ok=True)
        with open(os.path.join(self.state, "projects", key + ".json"), "w", encoding="utf-8") as handle:
            json.dump({"root": "/nowhere/" + label, "base": label, "label": label}, handle)

    def archives(self):
        res = self.harness.run("status", "--json")
        self.assertEqual(res.returncode, 0, (res.stdout + res.stderr).decode())
        return json.loads(res.stdout.decode())["archives"]

    def test_a_key_named_directory_shows_the_label_from_its_meta_and_keeps_the_key(self):
        self.plant(self.key, "slot-1", {"project": self.label, "lease": "slot-1", "branches": ["b"]})

        entry = self.archives()[0]

        self.assertEqual(entry["project"], self.label)
        self.assertEqual(entry["key"], self.key)
        self.assertEqual(entry["lease"], "slot-1")
        self.assertEqual(entry["branches"], ["b"])

    def test_without_a_project_in_the_meta_the_registry_gives_the_label(self):
        self.register("0badc0de", "registered")
        self.plant("0badc0de", "slot-2", {"branches": []})

        entry = self.archives()[0]

        self.assertEqual(entry["project"], "registered")
        self.assertEqual(entry["key"], "0badc0de")

    def test_a_directory_nothing_can_name_shows_its_own_name_as_project_and_key(self):
        self.plant("demo-project", "slot-3", None)

        entry = self.archives()[0]

        self.assertEqual(entry["project"], "demo-project")
        self.assertEqual(entry["key"], "demo-project")

    def test_the_text_status_lists_the_label(self):
        self.plant(self.key, "slot-1", {"project": self.label, "lease": "slot-1"})

        res = self.harness.run("status")

        self.assertEqual(res.returncode, 0, (res.stdout + res.stderr).decode())
        self.assertIn(self.label, res.stdout.decode())

    def test_verdict_takes_label_slash_lease_for_a_key_named_directory(self):
        self.register(self.key, self.label)
        os.makedirs(os.path.join(self.state, "projects", "labels"), exist_ok=True)
        with open(os.path.join(self.state, "projects", "labels", self.label), "w", encoding="utf-8") as handle:
            handle.write(self.key)
        adir = self.plant(self.key, "slot-4", {"project": self.label, "lease": "slot-4"})

        res = self.harness.run("verdict", self.label + "/slot-4")

        self.assertEqual(res.returncode, 0, (res.stdout + res.stderr).decode())
        self.assertTrue(os.path.isfile(os.path.join(adir, "verdict.md")))


if __name__ == "__main__":
    unittest.main()
