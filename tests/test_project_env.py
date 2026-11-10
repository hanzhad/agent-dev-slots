import os
import shutil
import subprocess
import tempfile
import unittest

from harness import LIB_REL, REPO_ROOT, SCRIPT_REL

FIXTURE = os.path.join(REPO_ROOT, "tests", "fixtures", "demo-project")

REQUIRED = {
    "PROJECT": "demo",
    "DEFAULT_BRANCH": "main",
    "SLOT_PORTS": "SSH_PORT",
    "COMPOSE_FILES": "docker-compose.yml",
}


class ProjectEnvTestCase(unittest.TestCase):
    """`dev-container project <dir>` prints the parsed keys as KEY=value lines."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="project-env-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def make_project(self, text=None, **values):
        directory = tempfile.mkdtemp(dir=self.tmp)
        if text is None:
            text = "".join(f"{k}={v}\n" for k, v in values.items())
        with open(os.path.join(directory, "project.env"), "w", encoding="utf-8") as handle:
            handle.write(text)
        return directory

    def read(self, directory):
        env = dict(os.environ, DEV_CONTAINER_HOME=os.path.join(self.tmp, "state"))
        result = subprocess.run(
            [os.path.join(REPO_ROOT, SCRIPT_REL), "project", directory],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            cwd=self.tmp,
            timeout=30,
        )
        out = result.stdout.decode()
        pairs = dict(line.split("=", 1) for line in out.split("\n") if "=" in line)
        return result.returncode, pairs, result.stderr.decode()


class ParsedValues(ProjectEnvTestCase):

    def test_reads_every_key_of_the_fixture_project(self):
        code, got, err = self.read(FIXTURE)

        self.assertEqual(code, 0, err)
        self.assertEqual(got["PROJECT"], "demo")
        self.assertEqual(got["DEFAULT_BRANCH"], "main")
        self.assertEqual(got["SLOT_PORTS"], "SSH_PORT WEB_PORT")
        self.assertEqual(got["SLOT_FORBIDDEN_PORTS"], "1101 1102")
        self.assertEqual(got["DOCS_REPO_URL"], "https://example.invalid/demo/demo.docs.git")
        self.assertEqual(got["DOCS_PIN_FILE"], "docs/.pin")
        self.assertEqual(got["SIGNING_KEY_VOLUME"], "demo-signing-key")
        self.assertEqual(got["SHARED_CACHE_VOLUME"], "demo-shared-cache")
        self.assertEqual(got["COMPOSE_DIR"], ".devcontainer")
        self.assertEqual(got["COMPOSE_FILES"], "docker-compose.yml")
        self.assertEqual(got["SECRETS_OVERLAY"], "docker-compose.secrets.yml")
        self.assertEqual(got["SLOT_ENV_KEYS"], "LOG_LEVEL THEME")
        self.assertEqual(got["SLOT_ENV_SET"], "SHARED_DIR=../.local/shared")
        self.assertEqual(got["SLOT_COPY_FILES"], ".local/settings.json")
        self.assertEqual(got["SLOT_MOUNT_DIRS"], ".local/shared")

    def test_only_the_required_keys_are_enough(self):
        code, got, err = self.read(self.make_project(**REQUIRED))

        self.assertEqual(code, 0, err)
        self.assertEqual(got["PROJECT"], "demo")

    def test_comments_blank_lines_and_quotes_are_handled(self):
        text = '# a comment\n\nPROJECT="demo"\nDEFAULT_BRANCH=\'main\'\n'
        text += "SLOT_PORTS=SSH_PORT\nCOMPOSE_FILES=docker-compose.yml\n"
        code, got, err = self.read(self.make_project(text))

        self.assertEqual(code, 0, err)
        self.assertEqual(got["PROJECT"], "demo")
        self.assertEqual(got["DEFAULT_BRANCH"], "main")


class NeverSourced(ProjectEnvTestCase):

    def test_command_substitution_in_a_value_is_not_executed(self):
        marker = os.path.join(self.tmp, "ran-substitution")
        values = dict(REQUIRED, DOCS_REPO_URL=f'"$(touch {marker})"')
        code, got, err = self.read(self.make_project(**values))

        self.assertFalse(os.path.exists(marker), "a command substitution was executed")
        self.assertEqual(code, 0, err)
        self.assertEqual(got["DOCS_REPO_URL"], f"$(touch {marker})")

    def test_backticks_in_a_value_are_not_executed(self):
        marker = os.path.join(self.tmp, "ran-backticks")
        values = dict(REQUIRED, DOCS_REPO_URL=f'"`touch {marker}`"')
        code, got, err = self.read(self.make_project(**values))

        self.assertFalse(os.path.exists(marker), "a backtick substitution was executed")
        self.assertEqual(code, 0, err)
        self.assertEqual(got["DOCS_REPO_URL"], f"`touch {marker}`")

    def test_a_semicolon_in_a_value_does_not_start_a_second_command(self):
        marker = os.path.join(self.tmp, "ran-semicolon")
        values = dict(REQUIRED, DOCS_REPO_URL=f'"demo; touch {marker}"')
        code, got, err = self.read(self.make_project(**values))

        self.assertFalse(os.path.exists(marker), "text after a semicolon was executed")
        self.assertEqual(code, 0, err)
        self.assertEqual(got["DOCS_REPO_URL"], f"demo; touch {marker}")

    def test_a_variable_reference_in_a_value_is_not_expanded(self):
        values = dict(REQUIRED, DOCS_REPO_URL='"$HOME-demo"')
        code, got, err = self.read(self.make_project(**values))

        self.assertEqual(code, 0, err)
        self.assertEqual(got["DOCS_REPO_URL"], "$HOME-demo")


class RequiredKeys(ProjectEnvTestCase):

    def test_each_missing_required_key_refuses_and_names_the_key(self):
        for key in REQUIRED:
            with self.subTest(missing=key):
                values = {k: v for k, v in REQUIRED.items() if k != key}
                code, _, err = self.read(self.make_project(**values))

                self.assertNotEqual(code, 0)
                self.assertIn(key, err)
                self.assertNotIn("Usage:", err)

    def test_an_empty_required_value_counts_as_missing(self):
        values = dict(REQUIRED, DEFAULT_BRANCH="")
        code, _, err = self.read(self.make_project(**values))

        self.assertNotEqual(code, 0)
        self.assertIn("DEFAULT_BRANCH", err)


class UnknownKeys(ProjectEnvTestCase):

    def test_an_unknown_key_refuses_and_names_the_key(self):
        values = dict(REQUIRED, DEFAULT_BRANCHH="main")
        code, _, err = self.read(self.make_project(**values))

        self.assertNotEqual(code, 0)
        self.assertIn("DEFAULT_BRANCHH", err)

    def test_dropped_keys_are_not_read(self):
        for key in ("TEST_COMMAND", "PACKAGE_MANAGER", "SLOT_COUNT"):
            with self.subTest(key=key):
                values = dict(REQUIRED, **{key: "leftover-value"})
                code, got, err = self.read(self.make_project(**values))

                self.assertNotEqual(code, 0)
                self.assertIn(key, err)
                self.assertNotIn(key, got)


class LineEndings(ProjectEnvTestCase):

    def test_a_crlf_file_parses_without_carriage_returns_in_values(self):
        text = "".join(f"{k}={v}\r\n" for k, v in REQUIRED.items())
        code, got, err = self.read(self.make_project(text))

        self.assertEqual(code, 0, err)
        for key, value in REQUIRED.items():
            self.assertEqual(got[key], value)

    def test_a_quoted_value_in_a_crlf_file_loses_its_quotes(self):
        text = "".join(f"{k}={v}\r\n" for k, v in REQUIRED.items() if k != "PROJECT")
        text += 'PROJECT="demo"\r\n'
        code, got, err = self.read(self.make_project(text))

        self.assertEqual(code, 0, err)
        self.assertEqual(got["PROJECT"], "demo")


class DuplicateKeys(ProjectEnvTestCase):

    def test_a_key_set_twice_refuses_and_names_the_key(self):
        text = "".join(f"{k}={v}\n" for k, v in REQUIRED.items()) + "DEFAULT_BRANCH=trunk\n"
        code, _, err = self.read(self.make_project(text))

        self.assertNotEqual(code, 0)
        self.assertIn("DEFAULT_BRANCH", err)
        self.assertIn("twice", err)


class SlotPorts(ProjectEnvTestCase):

    def test_a_list_of_names_is_read_as_it_stands(self):
        code, got, err = self.read(self.make_project(**dict(REQUIRED, SLOT_PORTS="SSH_PORT WEB_PORT API_PORT")))

        self.assertEqual(code, 0, err)
        self.assertEqual(got["SLOT_PORTS"], "SSH_PORT WEB_PORT API_PORT")

    def test_an_entry_with_a_port_or_a_range_refuses_and_names_the_entry(self):
        for entry in ("WEB_PORT:6081-6101", "WEB_PORT:6081", "WEB_PORT=6081", "6081-6101", "web_port"):
            with self.subTest(entry=entry):
                values = dict(REQUIRED, SLOT_PORTS=f"SSH_PORT {entry}")
                code, _, err = self.read(self.make_project(**values))

                self.assertNotEqual(code, 0)
                self.assertIn(entry, err)

    def test_more_names_than_a_block_holds_refuses(self):
        names = " ".join(["SSH_PORT"] + [f"EXTRA_{n}_PORT" for n in range(16)])
        code, _, err = self.read(self.make_project(**dict(REQUIRED, SLOT_PORTS=names)))

        self.assertNotEqual(code, 0)
        self.assertIn("SLOT_PORTS", err)

    def test_a_name_listed_twice_refuses_and_names_it(self):
        code, _, err = self.read(self.make_project(**dict(REQUIRED, SLOT_PORTS="SSH_PORT WEB_PORT WEB_PORT")))

        self.assertNotEqual(code, 0)
        self.assertIn("WEB_PORT", err)

    def test_the_first_name_need_not_be_ssh_port(self):
        code, got, err = self.read(self.make_project(**dict(REQUIRED, SLOT_PORTS="WEB_PORT")))

        self.assertEqual(code, 0, err)
        self.assertEqual(got["SLOT_PORTS"], "WEB_PORT")


class SlotMountDirs(ProjectEnvTestCase):

    def test_a_list_of_relative_paths_is_read_as_it_stands(self):
        code, got, err = self.read(self.make_project(**dict(REQUIRED, SLOT_MOUNT_DIRS=".local/shared data/cache")))

        self.assertEqual(code, 0, err)
        self.assertEqual(got["SLOT_MOUNT_DIRS"], ".local/shared data/cache")

    def test_an_absolute_path_or_one_that_climbs_out_refuses_and_names_it(self):
        for entry in ("/etc", "..", "../shared", "a/../b", "a/.."):
            with self.subTest(entry=entry):
                values = dict(REQUIRED, SLOT_MOUNT_DIRS=f"ok/dir {entry}")
                code, _, err = self.read(self.make_project(**values))

                self.assertNotEqual(code, 0)
                self.assertIn("SLOT_MOUNT_DIRS", err)
                self.assertIn(entry, err)

    def test_a_name_that_only_contains_two_dots_is_a_path_like_any_other(self):
        code, _, err = self.read(self.make_project(**dict(REQUIRED, SLOT_MOUNT_DIRS="a..b ..c")))

        self.assertEqual(code, 0, err)


class SlotOverlays(ProjectEnvTestCase):

    def test_variable_and_file_pairs_are_read_as_they_stand(self):
        code, got, err = self.read(
            self.make_project(**dict(REQUIRED, SLOT_OVERLAYS="SHARED_DIR=docker-compose.shared.yml"))
        )

        self.assertEqual(code, 0, err)
        self.assertEqual(got["SLOT_OVERLAYS"], "SHARED_DIR=docker-compose.shared.yml")

    def test_an_entry_that_is_not_a_variable_and_a_relative_file_refuses_and_names_it(self):
        for entry in ("SHARED_DIR", "shared=a.yml", "SHARED_DIR=", "SHARED_DIR=/a.yml", "SHARED_DIR=../a.yml"):
            with self.subTest(entry=entry):
                code, _, err = self.read(self.make_project(**dict(REQUIRED, SLOT_OVERLAYS=entry)))

                self.assertNotEqual(code, 0)
                self.assertIn("SLOT_OVERLAYS", err)
                self.assertIn(entry, err)


class DefaultBranch(ProjectEnvTestCase):

    def test_a_branch_name_with_a_slash_is_accepted(self):
        code, got, err = self.read(self.make_project(**dict(REQUIRED, DEFAULT_BRANCH="release/1.0")))

        self.assertEqual(code, 0, err)
        self.assertEqual(got["DEFAULT_BRANCH"], "release/1.0")

    def test_a_value_git_would_not_take_as_a_branch_refuses_and_names_the_key(self):
        for value in ("--upload-pack=touch-x", "-x", "a b", "a..b", "@{-1}", "a/", "x.lock"):
            with self.subTest(value=value):
                code, _, err = self.read(self.make_project(**dict(REQUIRED, DEFAULT_BRANCH=f'"{value}"')))

                self.assertNotEqual(code, 0)
                self.assertIn("DEFAULT_BRANCH", err)


class Connected(ProjectEnvTestCase):

    def test_a_directory_without_project_env_is_not_a_project(self):
        empty = tempfile.mkdtemp(dir=self.tmp)
        code, _, err = self.read(empty)

        self.assertNotEqual(code, 0)
        self.assertIn("project.env", err)

    def test_a_directory_with_project_env_is_connected_with_no_registry(self):
        code, got, err = self.read(self.make_project(**REQUIRED))

        self.assertEqual(code, 0, err)
        self.assertEqual(got["PROJECT"], "demo")
        state = os.path.join(self.tmp, "state")
        self.assertEqual(os.listdir(state) if os.path.isdir(state) else [], [])


class FixtureProject(unittest.TestCase):

    def test_fixture_has_a_project_env_and_a_compose_file(self):
        self.assertTrue(os.path.isfile(os.path.join(FIXTURE, "project.env")))
        self.assertTrue(
            os.path.isfile(os.path.join(FIXTURE, ".devcontainer", "docker-compose.yml"))
        )


class EngineLookup(ProjectEnvTestCase):
    """The entry point finds lib/ from its own real directory, and only beside bin/."""

    def make_engine(self, name="engine"):
        root = os.path.join(self.tmp, name)
        os.makedirs(os.path.join(root, "bin"))
        shutil.copy(os.path.join(REPO_ROOT, SCRIPT_REL), os.path.join(root, SCRIPT_REL))
        shutil.copytree(os.path.join(REPO_ROOT, LIB_REL), os.path.join(root, LIB_REL))
        return os.path.join(root, SCRIPT_REL)

    def run_project(self, command, cwd, **env_extra):
        env = dict(os.environ, DEV_CONTAINER_HOME=os.path.join(self.tmp, "state"), **env_extra)
        return subprocess.run(
            [command, "project", self.make_project(**REQUIRED)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            cwd=cwd,
            timeout=30,
        )

    def test_a_relative_symlink_is_followed(self):
        script = self.make_engine()
        link_dir = os.path.join(self.tmp, "links")
        os.makedirs(link_dir)
        os.symlink(os.path.relpath(script, link_dir), os.path.join(link_dir, "dc"))

        result = self.run_project("./dc", link_dir)

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertIn("PROJECT=demo", result.stdout.decode())

    def test_a_chain_of_symlinks_is_followed(self):
        script = self.make_engine()
        for name in ("one", "two"):
            os.makedirs(os.path.join(self.tmp, name))
        os.symlink(os.path.relpath(script, os.path.join(self.tmp, "two")), os.path.join(self.tmp, "two", "dc"))
        os.symlink("../two/dc", os.path.join(self.tmp, "one", "dc"))
        os.symlink(os.path.join(self.tmp, "one", "dc"), os.path.join(self.tmp, "dc"))

        result = self.run_project("./dc", self.tmp)

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertIn("PROJECT=demo", result.stdout.decode())

    def test_a_matching_cdpath_does_not_break_a_relative_call(self):
        script = self.make_engine()
        root = os.path.dirname(os.path.dirname(script))

        result = self.run_project(SCRIPT_REL, root, CDPATH=".:/nonexistent")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertIn("PROJECT=demo", result.stdout.decode())

    def test_the_bin_file_alone_is_refused(self):
        script = self.make_engine()
        alone = os.path.join(self.tmp, "alone", "bin")
        os.makedirs(alone)
        shutil.copy(script, alone)
        foreign = os.path.join(self.tmp, "alone", "lib")
        os.makedirs(foreign)
        marker = os.path.join(self.tmp, "foreign-ran")
        with open(os.path.join(foreign, "common.sh"), "w", encoding="utf-8") as handle:
            handle.write(f"touch {marker}\n")

        result = self.run_project(os.path.join(alone, "dev-container"), self.tmp)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must stay together", result.stderr.decode())
        self.assertFalse(os.path.exists(marker), "a foreign lib/common.sh was sourced")


if __name__ == "__main__":
    unittest.main()
