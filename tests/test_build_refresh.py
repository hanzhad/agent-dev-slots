import os
import re
import subprocess
import unittest

from harness import DevContainerTestCase, project_env_text
from test_doctor import IDENTITY, MOUNT, output_of, read, stub_config


class BuildsUseTheCacheUnlessAskedNot(DevContainerTestCase):
    def setUp(self):
        super().setUp()
        h = self.harness
        stub_config(self, {"app": {"build": {"context": "/ctx"}, "volumes": [MOUNT]}})
        h.build_fixture_repo(with_parent_env=IDENTITY, project_env=project_env_text())
        with open(os.path.join(h.main_devcontainer(), "docker-compose.yml"), "w", encoding="utf-8") as handle:
            handle.write("services:\n  app:\n    build: .\n")
        subprocess.run(["git", "-C", h.fixture_repo, "commit", "-q", "-am", "build section"], check=True)
        subprocess.run(["git", "-C", h.fixture_repo, "branch", "-M", "master"], check=True)

    def build_lines(self):
        if not os.path.isfile(self.harness.docker_log):
            return []
        return [
            line for line in read(self.harness.docker_log).splitlines()
            if re.search(r"(^ARGS: |\s)build(\s|$)", line)
        ]

    def acquire(self, *flags):
        before = len(self.build_lines())
        result = self.harness.run("acquire", "--for", "task-27", *flags)
        self.assertEqual(result.returncode, 0, output_of(result))
        return self.harness.acquired_slot_name(result), self.build_lines()[before:]

    def assert_cached(self, lines):
        self.assertTrue(lines, "no image was built")
        self.assertFalse([line for line in lines if "--no-cache" in line], lines)

    def assert_from_scratch(self, lines):
        self.assertTrue(lines, "no image was built")
        self.assertTrue(all("--no-cache" in line for line in lines), lines)

    def test_a_plain_acquire_builds_with_the_cache(self):
        self.assert_cached(self.acquire()[1])

    def test_acquire_refresh_builds_from_scratch(self):
        self.assert_from_scratch(self.acquire("--refresh")[1])

    def test_install_refresh_builds_from_scratch_and_plain_install_does_not(self):
        name, _ = self.acquire()
        before = len(self.build_lines())
        self.assertEqual(self.harness.run("install", name).returncode, 0)
        self.assert_cached(self.build_lines()[before:])
        before = len(self.build_lines())
        result = self.harness.run("install", name, "--refresh")
        self.assertEqual(result.returncode, 0, output_of(result))
        self.assert_from_scratch(self.build_lines()[before:])

    def test_the_flag_is_not_remembered_by_the_next_acquire(self):
        self.acquire("--refresh")
        self.assert_cached(self.acquire()[1])

    def test_restart_refresh_rebuilds_an_image_that_is_not_stale(self):
        name, _ = self.acquire()
        before = len(self.build_lines())
        self.assertEqual(self.harness.run("restart", name).returncode, 0)
        self.assertEqual(self.build_lines()[before:], [])
        result = self.harness.run("restart", name, "--refresh")
        self.assertEqual(result.returncode, 0, output_of(result))
        self.assert_from_scratch(self.build_lines()[before:])

    def test_the_usage_text_names_the_flag(self):
        text = output_of(self.harness.run("acquire", "--help"))
        self.assertIn("--refresh", text)
        for verb in ("install", "restart"):
            self.assertIn("--refresh", output_of(self.harness.run(verb, "--help")))


if __name__ == "__main__":
    unittest.main()
