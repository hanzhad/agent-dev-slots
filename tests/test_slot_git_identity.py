import os
import subprocess

import test_provision
from harness import project_env_text
from test_provision import ProvisionCase, text

NAME = "Demo Slot Bot"
EMAIL = "demo-slot-bot@example.com"
OTHER_NAME = "Second Demo Bot"
OTHER_EMAIL = "second-demo-bot@example.com"


def setUpModule():
    test_provision.setUpModule()


def tearDownModule():
    test_provision.tearDownModule()


class IdentityCase(ProvisionCase):
    """SLOT_GIT_NAME and SLOT_GIT_EMAIL in project.env are the slot's git identity."""

    def identity(self, mode_name="token"):
        self.project(mode_name, SLOT_GIT_NAME=NAME, SLOT_GIT_EMAIL=EMAIL)

    def checkout_email(self, name):
        return subprocess.run(
            ["git", "-C", self.harness.slot_dir(name), "config", "--local", "user.email"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        ).stdout.decode().strip()

    def rewrite_project_env(self, **values):
        path = os.path.join(self.harness.fixture_repo, "project.env")
        base = dict(FORGE_AUTH="token", FORGE_AUTH_FILE=test_provision.ENV_FILE_REL)
        base.update(values)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(project_env_text(**base))


class Validation(IdentityCase):
    def refused(self, **keys):
        self.project("token", **keys)
        result = self.harness.run("list")
        self.assertNotEqual(result.returncode, 0, text(result))
        return text(result)

    def test_a_name_without_an_email_is_refused_and_names_both_settings(self):
        out = self.refused(SLOT_GIT_NAME=NAME)
        self.assertIn("SLOT_GIT_NAME", out)
        self.assertIn("SLOT_GIT_EMAIL", out)

    def test_an_email_without_a_name_is_refused(self):
        self.assertIn("SLOT_GIT_NAME", self.refused(SLOT_GIT_EMAIL=EMAIL))

    def test_an_email_that_is_not_one_is_refused(self):
        self.assertIn("SLOT_GIT_EMAIL", self.refused(SLOT_GIT_NAME=NAME, SLOT_GIT_EMAIL="not an address"))

    def test_a_name_git_would_mangle_is_refused(self):
        self.assertIn("SLOT_GIT_NAME", self.refused(SLOT_GIT_NAME="Demo <bot>", SLOT_GIT_EMAIL=EMAIL))

    def test_both_set_is_accepted(self):
        self.identity()
        result = self.harness.run("list")
        self.assertEqual(result.returncode, 0, text(result))


class Identity(IdentityCase):
    def test_it_replaces_the_identity_in_the_compose_env_on_a_new_slot(self):
        self.identity()
        name = self.acquire()
        env = self.harness.read_env(name)
        self.assertEqual(env["GIT_USER_NAME"], NAME)
        self.assertEqual(env["GIT_USER_EMAIL"], EMAIL)
        self.assertEqual(self.harness.checkout_identity(name), NAME)
        self.assertEqual(self.checkout_email(name), EMAIL)

    def test_without_it_the_compose_env_still_decides(self):
        self.project("token")
        name = self.acquire()
        self.assertEqual(self.harness.read_env(name)["GIT_USER_NAME"], "demo-agent")
        self.assertEqual(self.harness.checkout_identity(name), "demo-agent")

    def test_a_change_in_project_env_reaches_an_existing_slot_on_restart(self):
        self.identity()
        name = self.acquire()
        self.rewrite_project_env(SLOT_GIT_NAME=OTHER_NAME, SLOT_GIT_EMAIL=OTHER_EMAIL)
        self.assertEqual(self.harness.run("restart", name).returncode, 0)
        env = self.harness.read_env(name)
        self.assertEqual((env["GIT_USER_NAME"], env["GIT_USER_EMAIL"]), (OTHER_NAME, OTHER_EMAIL))
        self.assertEqual(self.harness.checkout_identity(name), OTHER_NAME)

    def test_in_app_mode_it_wins_over_the_identity_derived_from_the_app(self):
        self.identity("app")
        name = self.acquire()
        env = self.harness.read_env(name)
        self.assertEqual((env["GIT_USER_NAME"], env["GIT_USER_EMAIL"]), (NAME, EMAIL))
        self.assertEqual(self.harness.checkout_identity(name), NAME)

    def test_in_app_mode_without_it_the_app_identity_is_kept(self):
        self.project("app")
        name = self.acquire()
        self.assertEqual(self.harness.read_env(name)["GIT_USER_NAME"], "demo-agent[bot]")


class InTheContainer(IdentityCase):
    def test_provisioning_sets_the_system_identity_so_commits_in_the_slot_carry_it(self):
        self.identity()
        name = self.acquire()
        self.assertEqual(self.config_values(name, "user.name"), [NAME])
        self.assertEqual(self.config_values(name, "user.email"), [EMAIL])

    def test_without_it_provisioning_sets_no_identity(self):
        self.project("token")
        name = self.acquire()
        self.assertEqual(self.config_values(name, "user.name"), [])
        self.assertEqual(self.config_values(name, "user.email"), [])
