
import json
import os
import re
import subprocess
import time
import unittest

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
        return f"{PROJECT_PREFIX}-{branch.replace('/', '_')}-{tip}"

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


if __name__ == "__main__":
    unittest.main()
