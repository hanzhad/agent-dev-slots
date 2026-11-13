'''The delivery cache is named by the project KEY; archives that are named by key show the label.

A cache entry named by label is a name the engine cannot vouch for (two projects
may share a label), so entries are keyed by the root's own key. A cache is
rebuildable, so what is still kept under the label is simply not read: a plain
cache miss, answered from git and the forge again, and migrate-identity does not
move it. status --json and the verdict command show a key-named rescue
directory under the project's label, and keep the key in a field of its own.
'''
import glob
import json
import os
import subprocess
import unittest

import test_delivery as td
from harness import PROJECT_PREFIX, DevContainerTestCase


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
