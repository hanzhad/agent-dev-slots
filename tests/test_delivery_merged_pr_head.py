
import json
import os
import subprocess
import time
import unittest

from harness import DevContainerTestCase, PROJECT_PREFIX


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
            "printf \"%s\n\" \"$*\" >> \"$log\"",
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


if __name__ == "__main__":
    unittest.main()
