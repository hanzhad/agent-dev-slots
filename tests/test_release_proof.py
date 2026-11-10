import os
import subprocess
import unittest

import test_delivery

Fixture = test_delivery.BranchDeliveryAsksReachability


class ReleaseRefusesUnprovenBranches(Fixture):
    """`release --for <holder>` refuses a slot holding a branch it cannot prove delivered."""

    # Only the fixture's setUp is wanted, not the tests it carries.
    locals().update({n: None for n in dir(Fixture) if n.startswith("test_")})

    def setUp(self):
        # The delivery fixture: a bare slot with an origin, a lease and a gh stub.
        super().setUp()
        os.remove(os.path.join(self.slot, "leftover.txt"))
        self.commit_file(
            ".gitignore",
            ".devcontainer/.env" + chr(10) + ".devcontainer/.slot-ports" + chr(10)
            + ".devcontainer/.slot-holder" + chr(10) + "docs/" + chr(10),
            "ignore slot state",
        )
        self.git("add", ".devcontainer/docker-compose.yml")
        self.git("commit", "-q", "-m", "track compose file")
        self.git("push", "-q", "origin", "master")
        self.git("fetch", "-q", "origin")
        # Teardown only removes a checkout whose origin is the project origin.
        subprocess.run(
            [self.harness.real_git_bin, "-C", self.harness.fixture_repo,
             "remote", "set-url", "origin", self.remote],
            check=True,
        )
        self.key = os.path.join(self.harness.tmp, "delivery-fixture-key")
        for path in (self.key, self.key + ".pub"):
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("key material\n")

    def commit_file(self, name, text, message):
        with open(os.path.join(self.slot, name), "w", encoding="utf-8") as handle:
            handle.write(text)
        self.git("add", name)
        self.git("commit", "-q", "-m", message)

    def unpushed_branch(self, branch, name="work.txt"):
        self.git("checkout", "-q", "-b", branch)
        self.commit_file(name, branch + " content\n", branch + " work")
        self.git("checkout", "-q", "master")

    def rev(self, ref):
        return subprocess.run(
            [self.harness.real_git_bin, "-C", self.slot, "rev-parse", ref],
            check=True, stdout=subprocess.PIPE,
        ).stdout.decode().strip()

    def release(self):
        return self.harness.run("release", "delivery", "--for", "test")

    def combined(self, result):
        return result.stdout.decode() + result.stderr.decode()

    def docker_log(self):
        if not os.path.exists(self.harness.docker_log):
            return ""
        with open(self.harness.docker_log, encoding="utf-8") as handle:
            return handle.read()

    def archives(self):
        found = []
        for _root, _dirs, files in os.walk(self.harness.state_dir):
            found += [f for f in files if f.endswith(".bundle")]
        rescue = os.path.join(self.harness.state_dir, "rescue")
        if os.path.isdir(rescue):
            found += os.listdir(rescue)
        return found

    def lease_bytes(self):
        with open(self.harness.lease_path("delivery"), "rb") as handle:
            return handle.read()

    def test_a_branch_with_commits_and_no_proof_refuses_and_names_the_branch(self):
        self.unpushed_branch("still-brewing")

        result = self.release()

        self.assertNotEqual(result.returncode, 0, self.combined(result))
        self.assertIn("still-brewing", self.combined(result))

    def test_every_unproven_branch_is_printed(self):
        self.unpushed_branch("first-branch", "a.txt")
        self.unpushed_branch("second-branch", "b.txt")

        result = self.release()

        self.assertNotEqual(result.returncode, 0, self.combined(result))
        self.assertIn("first-branch", self.combined(result))
        self.assertIn("second-branch", self.combined(result))

    def test_a_single_commit_squash_merged_under_a_new_hash_counts_as_delivered(self):
        self.unpushed_branch("squashed-one", "feature.txt")
        self.commit_file("feature.txt", "squashed-one content\n", "squash of squashed-one")
        self.git("push", "-q", "origin", "master")
        self.git("fetch", "-q", "origin")

        result = self.release()

        self.assertEqual(result.returncode, 0, self.combined(result))
        self.assertNotIn("squashed-one", self.combined(result))
        self.assertFalse(os.path.exists(self.slot))

    def test_a_multi_commit_branch_squashed_into_one_commit_counts_as_delivered(self):
        self.git("checkout", "-q", "-b", "squashed-many")
        self.commit_file("one.txt", "one\n", "first")
        self.commit_file("two.txt", "two\n", "second")
        self.git("checkout", "-q", "master")
        self.commit_file("one.txt", "one\n", "squash part")
        self.commit_file("two.txt", "two\n", "squash rest")
        self.git("reset", "-q", "--soft", "HEAD~2")
        self.git("commit", "-q", "-m", "squash of squashed-many")
        self.git("push", "-q", "origin", "master")
        self.git("fetch", "-q", "origin")

        result = self.release()

        self.assertEqual(result.returncode, 0, self.combined(result))
        self.assertNotIn("squashed-many", self.combined(result))
        self.assertFalse(os.path.exists(self.slot))

    def test_a_merged_pull_request_on_github_with_the_branch_tip_counts_as_delivered(self):
        self.unpushed_branch("merged-on-github")
        tip = self.rev("merged-on-github")
        self.harness._write_stub(
            "gh",
            "#!/usr/bin/env bash\n"
            "echo '[{\"headRefOid\":\"" + tip + "\",\"state\":\"MERGED\",\"number\":7}]'\n",
        )

        result = self.release()

        self.assertEqual(result.returncode, 0, self.combined(result))
        self.assertNotIn("merged-on-github", self.combined(result))
        self.assertFalse(os.path.exists(self.slot))

    def test_when_everything_is_delivered_release_removes_folder_clone_volumes_and_token(self):
        result = self.release()

        self.assertEqual(result.returncode, 0, self.combined(result))
        self.assertFalse(os.path.exists(self.slot), "the slot folder is still there")
        self.assertFalse(os.path.exists(self.harness.lease_path("delivery")))
        self.assertFalse(os.path.exists(self.key), "the token is still there")
        self.assertFalse(os.path.exists(self.key + ".pub"))
        down = [l for l in self.docker_log().splitlines() if " down " in l]
        self.assertTrue(down, "compose was never taken down")
        self.assertTrue(any(" -v" in l for l in down), "volumes were not removed: " + repr(down))

    def test_after_a_refusal_the_slot_and_its_lease_are_untouched(self):
        self.unpushed_branch("still-brewing")
        lease_before = self.lease_bytes()
        tip_before = self.rev("still-brewing")

        result = self.release()

        self.assertNotEqual(result.returncode, 0, self.combined(result))
        self.assertTrue(os.path.isdir(self.slot), "the slot folder was removed")
        self.assertEqual(self.lease_bytes(), lease_before)
        self.assertTrue(os.path.exists(self.key), "the token was removed")
        self.assertTrue(os.path.exists(self.key + ".pub"))
        self.assertEqual(self.rev("still-brewing"), tip_before)
        self.assertNotIn(" down ", self.docker_log(), "containers were taken down")

    def test_a_refusal_writes_no_archive(self):
        self.unpushed_branch("still-brewing")

        result = self.release()

        self.assertNotEqual(result.returncode, 0, self.combined(result))
        self.assertEqual(self.archives(), [])

    def test_a_successful_release_writes_no_archive(self):
        result = self.release()

        self.assertEqual(result.returncode, 0, self.combined(result))
        self.assertEqual(self.archives(), [])


del Fixture


if __name__ == "__main__":
    unittest.main()
