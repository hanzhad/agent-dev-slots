import json
import os
import re
import shutil
import unittest

from harness import PROJECT_PREFIX, DevContainerHarness, DevContainerTestCase

IDENTITY = "GIT_USER_NAME=identity-agent\nGIT_USER_EMAIL=identity-agent@example.com\n"


class IdentityCase(DevContainerTestCase):
    def setUp(self):
        super().setUp()
        self.harness.build_fixture_repo(with_parent_env=IDENTITY)

    def another_root(self, project_env=None):
        """A second checkout sharing this one's state directory, as a copy would."""
        other = DevContainerHarness()
        self.addCleanup(other.cleanup)
        shutil.rmtree(other.state_dir)
        other.state_dir = self.harness.state_dir
        other.build_fixture_repo(with_parent_env=IDENTITY, project_env=project_env)
        return other

    def acquire_id(self, harness):
        result = harness.run_acquire()
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        match = re.search(r'"id": "([^"]+)"', result.stdout.decode())
        self.assertTrue(match, result.stdout.decode())
        return match.group(1)

    def projects_path(self, *parts):
        return os.path.join(self.harness.state_dir, "projects", *parts)

    def registry(self, harness):
        with open(self.projects_path(harness.project_key + ".json"), encoding="utf-8") as handle:
            return json.load(handle)

    def label_owner(self, label):
        with open(self.projects_path("labels", label), encoding="utf-8") as handle:
            return handle.read().strip()

    def leases(self):
        return sorted(os.listdir(os.path.join(self.harness.state_dir, "leases")))


class SingleProjectTest(IdentityCase):
    def test_the_label_is_the_project_name_and_every_name_is_unchanged(self):
        name = self.harness.acquired_slot_name(self.harness.run_acquire())

        self.assertTrue(os.path.isfile(self.harness.lease_path(name)))
        self.assertTrue(self.harness.volume_exists(name))
        self.assertEqual(self.harness.volume_labels(name).get("dev-container.project"), PROJECT_PREFIX)
        self.assertTrue(os.path.isfile(self.harness.ports_path(name)))
        self.assertTrue(os.path.isfile(self.harness.env_path(name)))
        self.assertEqual(
            self.registry(self.harness),
            {"root": os.path.realpath(self.harness.fixture_repo), "base": PROJECT_PREFIX, "label": PROJECT_PREFIX},
        )
        self.assertEqual(self.label_owner(PROJECT_PREFIX), self.harness.project_key)

    def test_a_second_run_keeps_the_label(self):
        first = self.acquire_id(self.harness)
        second = self.acquire_id(self.harness)

        self.assertTrue(first.startswith(PROJECT_PREFIX + "-slot-"), first)
        self.assertTrue(second.startswith(PROJECT_PREFIX + "-slot-"), second)


class SameProjectTwoRootsTest(IdentityCase):
    def test_the_later_root_gets_a_label_made_unique_by_its_key(self):
        other = self.another_root()
        first = self.acquire_id(self.harness)
        second = self.acquire_id(other)

        self.assertTrue(first.startswith(PROJECT_PREFIX + "-slot-"), first)
        self.assertTrue(second.startswith(f"{PROJECT_PREFIX}_{other.project_key}-slot-"), second)
        self.assertEqual(self.label_owner(PROJECT_PREFIX), self.harness.project_key)
        self.assertEqual(self.label_owner(f"{PROJECT_PREFIX}_{other.project_key}"), other.project_key)
        self.assertEqual(self.registry(other)["base"], PROJECT_PREFIX)
        self.assertEqual(self.registry(self.harness)["label"], PROJECT_PREFIX)
        self.assertEqual(len(self.leases()), 2, self.leases())

    def test_the_later_root_is_not_refused_or_warned(self):
        other = self.another_root()
        self.acquire_id(self.harness)

        result = other.run_acquire()

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertNotRegex(result.stderr.decode(), r"(?i)copy|moved|duplicate|already|claimed")


class StickyLabelTest(IdentityCase):
    def test_the_label_stays_after_the_first_root_is_deleted(self):
        other = self.another_root()
        self.acquire_id(self.harness)
        label = f"{PROJECT_PREFIX}_{other.project_key}"
        self.assertTrue(self.acquire_id(other).startswith(label + "-"))

        shutil.rmtree(self.harness.fixture_repo)
        again = self.acquire_id(other)

        self.assertTrue(again.startswith(label + "-slot-"), again)
        self.assertEqual(self.registry(other)["label"], label)
        self.assertEqual(self.label_owner(PROJECT_PREFIX), self.harness.project_key)


class NoProjectEnvTest(IdentityCase):
    def test_the_folder_name_is_the_label_and_never_demo(self):
        shutil.rmtree(self.harness.fixture_repo)
        self.harness.build_fixture_repo(with_parent_env=IDENTITY, project_env="")

        result = self.harness.run("list", "--brief")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(self.registry(self.harness)["label"], "fixture-repo")
        self.assertEqual(self.registry(self.harness)["base"], "fixture-repo")
        self.assertFalse(os.path.exists(self.projects_path("labels", "demo")))

    def test_acquire_names_slots_after_the_folder(self):
        shutil.rmtree(self.harness.fixture_repo)
        self.harness.build_fixture_repo(with_parent_env=IDENTITY, project_env="")

        result = self.harness.run_acquire()

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        slot_id = re.search(r'"id": "([^"]+)"', result.stdout.decode()).group(1)
        self.assertTrue(slot_id.startswith("fixture-repo-slot-"), slot_id)


class SeedingTest(IdentityCase):
    def test_a_copy_cannot_take_the_label_of_a_project_with_slots(self):
        # State from before the registry existed: a lease for demo, no projects/.
        other = self.another_root()
        original = self.acquire_id(other)
        shutil.rmtree(self.projects_path())

        copy_id = self.acquire_id(self.harness)

        self.assertTrue(original.startswith(PROJECT_PREFIX + "-slot-"), original)
        self.assertTrue(copy_id.startswith(f"{PROJECT_PREFIX}_{self.harness.project_key}-slot-"), copy_id)
        self.assertEqual(self.label_owner(PROJECT_PREFIX), other.project_key)

        # The original, when it runs, finds its label waiting.
        again = self.acquire_id(other)
        self.assertTrue(again.startswith(PROJECT_PREFIX + "-slot-"), again)
        self.assertEqual(self.registry(other)["label"], PROJECT_PREFIX)


if __name__ == "__main__":
    unittest.main()
