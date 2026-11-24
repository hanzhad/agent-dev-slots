import json
import os
import re
import shutil
import subprocess
import time
import unittest

from harness import (
    DOCKER_STUB,
    DOCS_REPO_URL,
    DevContainerHarness,
    DevContainerTestCase,
    ListVerdictTestCase,
    PROJECT_PREFIX,
    REPO_ROOT,
    LIB_REL,
    engine_source_lines,
    SCRIPT_REL,
    DEFAULT_PORT_NAMES,
    OWNER_PORTS,
    TEST_SSH_PORT,
    project_env_text,
    slot_port,
    wait_until,
)

ENV_WITH_IDENTITY = "GIT_USER_NAME=demo-agent\nGIT_USER_EMAIL=demo-agent@example.com\n"
IDENTITY_ENV = ENV_WITH_IDENTITY


class UnknownOrSurplusArgumentsAreRefusedByEveryVerb(DevContainerTestCase):
    def test_release_refuses_an_unknown_flag(self):
        self.harness.build_fixture_repo()

        result = self.harness.run("release", "--bogus")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--bogus", result.stderr.decode())

    def test_release_refuses_a_surplus_positional(self):
        self.harness.build_fixture_repo()

        result = self.harness.run("release", "probe", "extra")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("extra", result.stderr.decode())

    def test_down_refuses_an_unknown_flag(self):
        self.harness.build_fixture_repo()

        result = self.harness.run("down", "probe", "--bogus")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--bogus", result.stderr.decode())

    def test_restart_refuses_a_surplus_argument(self):
        self.harness.build_fixture_repo()

        result = self.harness.run("restart", "probe", "extra")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("extra", result.stderr.decode())

    def test_reap_refuses_a_surplus_argument(self):
        self.harness.build_fixture_repo()

        result = self.harness.run("reap", "extra")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("extra", result.stderr.decode())

    def test_up_refuses_a_surplus_argument(self):
        self.harness.build_fixture_repo(with_parent_env=ENV_WITH_IDENTITY)

        result = self.harness.run("up", "extra")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("extra", result.stderr.decode())

    def test_evict_stale_refuses_a_surplus_argument(self):
        self.harness.build_fixture_repo()

        result = self.harness.run("evict", "--stale", "7", "extra")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("extra", result.stderr.decode())


class AFlagWithNoValueRefusesByName(DevContainerTestCase):
    def test_release_slot_with_no_value_refuses_instead_of_exiting_silently(self):
        self.harness.build_fixture_repo()

        result = self.harness.run("release", "--slot")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--slot", result.stderr.decode())


class AFlagTypedAsAPositionalSlotNameIsRefused(DevContainerTestCase):
    def test_down_refuses_a_flag_used_as_the_slot_name(self):
        self.harness.build_fixture_repo()

        result = self.harness.run("down", "--slot")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--slot", result.stderr.decode())


class HelpPrintsTheVerbsOwnUsageLine(DevContainerTestCase):
    EXPECTED = {
        "acquire": "acquire --for <holder>",
        "release": "release <slot> --for <holder>",
        "park": "park <slot> --clear",
        "list": "list [--brief] [--fast]",
        "reap": " reap\n",
        "evict": "evict <slot> [--yes] [--force]",
        "up": "up [name]",
        "down": "down <name> [--force]",
        "restart": "restart <name>",
        "run": "run <slot> -- <cmd>",
    }

    def test_help_prints_only_that_verbs_own_line(self):
        self.harness.build_fixture_repo()
        for verb, expected in self.EXPECTED.items():
            with self.subTest(verb=verb):
                result = self.harness.run(verb, "--help")
                self.assertEqual(result.returncode, 0, result.stderr.decode())
                stdout = result.stdout.decode()
                self.assertIn(expected, stdout, stdout)
                for other_verb, other_expected in self.EXPECTED.items():
                    if other_verb == verb:
                        continue
                    self.assertNotIn(
                        other_expected, stdout,
                        f"'{verb} --help' also printed {other_verb}'s own line:\n{stdout}",
                    )

    def test_release_help_never_misreads_help_as_the_slot_name(self):
        self.harness.build_fixture_repo()

        result = self.harness.run("release", "--help")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertNotIn("no slot directory", result.stdout.decode() + result.stderr.decode())


if __name__ == "__main__":
    unittest.main()
