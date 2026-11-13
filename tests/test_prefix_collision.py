import json
import os
import re
import shutil

from harness import DevContainerHarness, DevContainerTestCase, project_env_text

IDENTITY = "GIT_USER_NAME=identity-agent\nGIT_USER_EMAIL=identity-agent@example.com\n"


class PrefixCollisionCase(DevContainerTestCase):
    """Two projects whose labels collide as a prefix: "demo" and "demo-bar".

    The first one's "demo-*" glob also matches the second one's "demo-bar-slot-N".
    They share the state directory and the docker daemon, as two checkouts on one
    machine do, and nothing else.
    """

    def setUp(self):
        super().setUp()
        self.harness.build_fixture_repo(with_parent_env=IDENTITY)
        self.other = DevContainerHarness()
        self.addCleanup(self.other.cleanup)
        shutil.rmtree(self.other.state_dir)
        shutil.rmtree(self.other.docker_state)
        self.other.state_dir = self.harness.state_dir
        self.other.docker_state = self.harness.docker_state
        self.other.build_fixture_repo(
            with_parent_env=IDENTITY,
            project_env=project_env_text(PROJECT="demo-bar"),
        )
        self.a_sid = self.acquire_id(self.harness)
        self.b_sid = self.acquire_id(self.other)
        self.assertTrue(self.a_sid.startswith("demo-slot-"), self.a_sid)
        self.assertTrue(self.b_sid.startswith("demo-bar-slot-"), self.b_sid)

    def acquire_id(self, harness):
        result = harness.run_acquire()
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        match = re.search(r'"id": "([^"]+)"', result.stdout.decode())
        self.assertTrue(match, result.stdout.decode())
        return match.group(1)

    def lease_path(self, sid):
        return os.path.join(self.harness.state_dir, "leases", sid + ".json")

    def edit_lease(self, sid, drop_repo_root=False, idle=False):
        path = self.lease_path(sid)
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        if drop_repo_root:
            data.pop("repo_root")
        if idle:
            data["since"] = 1
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(data, handle)

    def volume(self, sid):
        return os.path.join(self.harness.volumes_dir, sid + "-workspace")

    def orphan_compose(self, project):
        with open(os.path.join(self.harness.docker_state, project + ".running"), "w", encoding="utf-8"):
            pass

    def output(self, result):
        return result.stdout.decode() + result.stderr.decode()

    def assert_untouched(self, sid):
        self.assertTrue(os.path.isfile(self.lease_path(sid)), sid + " lost its lease")
        self.assertTrue(os.path.isdir(self.volume(sid)), sid + " lost its volume")


class ListTest(PrefixCollisionCase):
    def test_each_project_lists_only_its_own_slots(self):
        mine = self.output(self.harness.run("list", "--brief"))
        theirs = self.output(self.other.run("list", "--brief"))

        self.assertIn(self.a_sid, mine)
        self.assertNotIn(self.b_sid, mine)
        self.assertIn(self.b_sid, theirs)
        self.assertNotIn(self.a_sid, theirs)

    def test_the_other_projects_orphan_compose_project_is_not_listed(self):
        self.orphan_compose("demo-bar-slot-9")

        mine = self.output(self.harness.run("list", "--brief"))
        theirs = self.output(self.other.run("list", "--brief"))

        self.assertNotIn("demo-bar-slot-9", mine)
        self.assertIn("demo-bar-slot-9", theirs)

    def test_an_orphan_compose_project_of_its_own_is_still_listed(self):
        self.orphan_compose("demo-slot-9")

        self.assertIn("demo-slot-9", self.output(self.harness.run("list", "--brief")))


class ReapTest(PrefixCollisionCase):
    def test_reap_leaves_the_other_projects_idle_lease_alone(self):
        self.edit_lease(self.b_sid, idle=True)

        result = self.harness.run("reap")

        self.assertEqual(result.returncode, 0, self.output(result))
        self.assert_untouched(self.b_sid)
        self.assertNotIn(self.b_sid, self.output(result))

    def test_reap_still_reaps_its_own_idle_lease(self):
        self.edit_lease(self.a_sid, idle=True)

        result = self.harness.run("reap")

        self.assertEqual(result.returncode, 0, self.output(result))
        self.assertFalse(os.path.isfile(self.lease_path(self.a_sid)), self.output(result))
        self.assert_untouched(self.b_sid)

    def test_reap_does_not_report_the_other_projects_orphan_compose_project(self):
        self.orphan_compose("demo-bar-slot-9")

        result = self.harness.run("reap")

        self.assertNotIn("demo-bar-slot-9", self.output(result))
        self.assertIn("demo-bar-slot-9", self.output(self.other.run("reap")))

    def test_a_legacy_lease_without_repo_root_is_still_not_taken_by_the_prefix(self):
        self.edit_lease(self.b_sid, drop_repo_root=True, idle=True)

        result = self.harness.run("reap")

        self.assertEqual(result.returncode, 0, self.output(result))
        self.assert_untouched(self.b_sid)

    def test_a_legacy_lease_without_repo_root_is_still_reaped_by_its_own_project(self):
        self.edit_lease(self.a_sid, drop_repo_root=True, idle=True)

        result = self.harness.run("reap")

        self.assertEqual(result.returncode, 0, self.output(result))
        self.assertFalse(os.path.isfile(self.lease_path(self.a_sid)), self.output(result))


class CleanEvictAcquireTest(PrefixCollisionCase):
    def test_clean_leaves_the_other_project_alone(self):
        self.edit_lease(self.b_sid, idle=True)

        result = self.harness.run("clean")

        self.assertEqual(result.returncode, 0, self.output(result))
        self.assert_untouched(self.b_sid)

    def test_evict_stale_leaves_the_other_project_alone(self):
        result = self.harness.run("evict", "--stale", "0")

        self.assertEqual(result.returncode, 0, self.output(result))
        self.assert_untouched(self.b_sid)

    def test_acquire_never_reuses_the_other_projects_idle_slot(self):
        self.edit_lease(self.b_sid, idle=True)

        again = self.acquire_id(self.harness)

        self.assertTrue(again.startswith("demo-slot-"), again)
        self.assert_untouched(self.b_sid)
