import os
import re
import shutil
import subprocess
import unittest

from harness import DevContainerTestCase, REPO_ROOT, SCRIPT_REL, project_env_text

ENGINE = os.path.join(REPO_ROOT, SCRIPT_REL)
TEMPLATES = os.path.join(REPO_ROOT, "templates")
DOCS = ["README.md", "AGENTS.md"]
STAMP = re.compile(
    "^<!-- dev-container-engine template: (README[.]md|AGENTS[.]md) version: ([0-9]+) sha256: ([0-9a-f]{64}) -->$"
)
IDENTITY = "GIT_USER_NAME=demo-agent\nGIT_USER_EMAIL=demo-agent@example.com\n"


def output_of(result):
    return result.stdout.decode() + result.stderr.decode()


def read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


class AdoptDocsTestCase(DevContainerTestCase):
    def setUp(self):
        super().setUp()
        self.harness.build_fixture_repo(
            with_parent_env=IDENTITY,
            project_env=project_env_text(FORGE_AUTH="token", FORGE_AUTH_FILE=".devcontainer/agent.env"),
        )
        # A ready credential file, so that adopt has nothing to report about it.
        write(os.path.join(self.repo, ".devcontainer", "agent.env"), "GH_TOKEN=ghp_SENTINEL_docs\n")
        os.chmod(os.path.join(self.repo, ".devcontainer", "agent.env"), 0o600)

    @property
    def repo(self):
        return self.harness.fixture_repo

    def doc(self, name):
        return os.path.join(self.repo, ".devcontainer", name)

    def engine(self, *args, **env):
        overrides = dict(env)
        overrides["DEV_CONTAINER_CALLER_PWD"] = self.repo
        return subprocess.run(
            ["bash", ENGINE, *args],
            cwd=self.harness.tmp,
            env=self.harness._env(overrides),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=120,
        )

    def adopt(self, **env):
        return self.engine("adopt", **env)

    def stamp_of(self, name):
        first = read(self.doc(name)).split("\n")[0]
        match = STAMP.match(first)
        self.assertIsNotNone(match, first)
        return int(match.group(2)), match.group(3)

    def body_of(self, name):
        return "\n".join(read(self.doc(name)).split("\n")[1:])

    def newer_templates(self, marker="NEWER TEMPLATE BODY"):
        target = os.path.join(self.harness.tmp, "templates-v2")
        shutil.copytree(TEMPLATES, target)
        for name in DOCS:
            path = os.path.join(target, name)
            text = read(path).replace("version: 1", "version: 2", 1)
            write(path, text.rstrip("\n") + "\n" + marker + "\n")
        return target


class AdoptWritesTheDocsForAgents(AdoptDocsTestCase):
    def test_adopt_writes_readme_and_agents_md_each_with_a_version_stamp(self):
        self.adopt()

        for name in DOCS:
            self.assertTrue(os.path.isfile(self.doc(name)), name)
            version, digest = self.stamp_of(name)
            self.assertEqual(version, 1)

    def test_readme_explains_the_credential_file_what_is_checked_and_the_signing_key(self):
        self.adopt()

        text = read(self.doc("README.md"))

        self.assertIn(".devcontainer/agent.env", text)
        self.assertIn("chmod 600", text)
        self.assertIn("GH_TOKEN", text)
        self.assertIn("FORGE_APP_KEY_FILE", text)
        self.assertIn("outside what the engine container can see", text)
        self.assertIn("doctor", text)
        self.assertRegex(text, "(?i)public key")
        self.assertRegex(text, "(?i)signing")

    def test_agents_md_holds_the_rules_for_an_agent_inside_a_slot(self):
        self.adopt()

        text = read(self.doc("AGENTS.md"))

        self.assertRegex(text, "(?i)release")
        self.assertRegex(text, "(?i)never")
        self.assertRegex(text, "(?i)credential|token")

    def test_the_docs_never_contain_a_secret_value(self):
        result = self.adopt()

        self.assertEqual(result.returncode, 0, output_of(result))

        for name in DOCS:
            self.assertNotIn("ghp_SENTINEL_docs", read(self.doc(name)))

    def test_a_project_that_names_no_credential_file_still_gets_both_docs(self):
        other = os.path.join(self.harness.tmp, "plain-app")
        os.makedirs(other)
        git = lambda *args: subprocess.run(
            [self.harness.real_git_bin, "-C", other, *args],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        git("init", "-q")
        git("config", "user.email", "f@example.com")
        git("config", "user.name", "f")
        write(os.path.join(other, "package.json"), "{}\n")
        git("add", "-A")
        git("commit", "-qm", "init")

        result = self.engine("adopt", other)

        self.assertEqual(result.returncode, 0, output_of(result))
        for name in DOCS:
            self.assertTrue(os.path.isfile(os.path.join(other, ".devcontainer", name)), name)

    def test_a_slot_receives_the_docs_and_never_the_credential_file(self):
        git = lambda *args: subprocess.run(
            [self.harness.real_git_bin, "-C", self.repo, *args],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        git("rm", "-r", "-q", "--cached", ".devcontainer")
        git("commit", "-qm", "untrack .devcontainer")
        write(os.path.join(self.repo, ".git", "info", "exclude"), "/.devcontainer/\n")
        write(os.path.join(self.repo, ".devcontainer", "agent.env"), "GH_TOKEN=ghp_SENTINEL_docs\n")
        os.chmod(os.path.join(self.repo, ".devcontainer", "agent.env"), 0o600)
        self.adopt()

        result = self.harness.run_acquire()

        self.assertEqual(result.returncode, 0, output_of(result))
        slot = self.harness.slot_dir(self.harness.acquired_slot_name(result))
        for name in DOCS:
            self.assertTrue(os.path.isfile(os.path.join(slot, ".devcontainer", name)), name)
        self.assertFalse(os.path.exists(os.path.join(slot, ".devcontainer", "agent.env")))


class AdoptSeedsTheDocsOnceAndTheProjectOwnsThem(AdoptDocsTestCase):
    def test_a_second_adopt_leaves_the_docs_byte_for_byte_alone(self):
        self.adopt()
        before = {name: read(self.doc(name)) for name in DOCS}
        times = {name: os.stat(self.doc(name)).st_mtime_ns for name in DOCS}

        result = self.adopt()

        self.assertEqual(result.returncode, 0, output_of(result))
        for name in DOCS:
            self.assertEqual(read(self.doc(name)), before[name])
            self.assertEqual(os.stat(self.doc(name)).st_mtime_ns, times[name])
            self.assertFalse(os.path.exists(self.doc(name) + ".new"))

    def test_an_owner_edit_is_kept_without_a_word(self):
        self.adopt()
        with open(self.doc("README.md"), "a", encoding="utf-8") as handle:
            handle.write("Our own note.\n")
        edited = read(self.doc("README.md"))

        result = self.adopt()

        self.assertEqual(read(self.doc("README.md")), edited)
        self.assertFalse(os.path.exists(self.doc("README.md.new")))
        self.assertNotIn("README.md", output_of(result))

    def test_a_newer_template_never_regenerates_an_unedited_doc_either(self):
        self.adopt()
        before = {name: read(self.doc(name)) for name in DOCS}
        templates = self.newer_templates()

        result = self.adopt(DEV_CONTAINER_TEMPLATES_DIR=templates)

        for name in DOCS:
            self.assertEqual(read(self.doc(name)), before[name])
            self.assertFalse(os.path.exists(self.doc(name) + ".new"))
        self.assertNotIn("NEWER TEMPLATE BODY", output_of(result))

    def test_a_doc_with_no_stamp_is_the_owners_and_no_new_file_appears(self):
        write(self.doc("AGENTS.md"), "Rules we wrote ourselves.\n")

        self.adopt()

        self.assertEqual(read(self.doc("AGENTS.md")), "Rules we wrote ourselves.\n")
        self.assertFalse(os.path.exists(self.doc("AGENTS.md.new")))
        self.assertTrue(os.path.isfile(self.doc("README.md")))

    def test_a_missing_doc_is_added_beside_the_ones_that_exist(self):
        self.adopt()
        os.remove(self.doc("AGENTS.md"))
        readme = read(self.doc("README.md"))

        self.adopt()

        self.assertTrue(os.path.isfile(self.doc("AGENTS.md")))
        self.assertEqual(read(self.doc("README.md")), readme)

    def test_the_stamp_digest_is_the_digest_of_the_body(self):
        self.adopt()
        version, digest = self.stamp_of("AGENTS.md")
        body = self.body_of("AGENTS.md")
        sums = subprocess.run(
            ["sha256sum"], input=body.encode(), stdout=subprocess.PIPE, check=True
        ).stdout.decode().split()[0]

        self.assertEqual(digest, sums)


class DoctorSaysWhenTheTemplateMovedOn(AdoptDocsTestCase):
    def test_doctor_prints_the_seeded_and_current_versions_with_the_diff_command_and_changes_nothing(self):
        self.adopt()
        before = {name: read(self.doc(name)) for name in DOCS}
        templates = self.newer_templates()

        result = self.engine("doctor", DEV_CONTAINER_TEMPLATES_DIR=templates)

        text = output_of(result)
        self.assertIn("README.md was seeded from template v1, current is v2", text)
        self.assertIn("diff -u", text)
        for name in DOCS:
            self.assertEqual(read(self.doc(name)), before[name])

    def test_doctor_says_nothing_about_the_docs_while_the_template_is_the_same(self):
        self.adopt()

        result = self.engine("doctor")

        self.assertNotIn("was seeded from template", output_of(result))


class AdoptMakesTheFolderIgnoreItself(AdoptDocsTestCase):
    def fresh_repo(self):
        other = os.path.join(self.harness.tmp, "ignored-app")
        os.makedirs(other)
        git = lambda *args: subprocess.run(
            [self.harness.real_git_bin, "-C", other, *args],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        git("init", "-q")
        git("config", "user.email", "f@example.com")
        git("config", "user.name", "f")
        write(os.path.join(other, "package.json"), "{}\n")
        git("add", "-A")
        git("commit", "-qm", "init")
        return other, git

    def test_git_status_shows_nothing_for_the_folder_even_with_a_secret_in_it(self):
        other, git = self.fresh_repo()
        write(os.path.join(other, ".devcontainer", "agent.env"), "GH_TOKEN=ghp_SENTINEL_ignore\n")

        result = self.engine("adopt", other)

        self.assertEqual(result.returncode, 0, output_of(result))
        status = git("status", "--short", "--", ".devcontainer").stdout.decode()
        self.assertEqual(status.strip(), "", status)
        ignore = read(os.path.join(other, ".devcontainer", ".gitignore"))
        self.assertIn("\n*\n", "\n" + ignore)
        self.assertTrue(ignore.startswith("#"), ignore)

    def test_the_ignore_file_is_the_only_mechanism_for_the_folder(self):
        other, git = self.fresh_repo()

        self.engine("adopt", other)
        self.engine("adopt", other)

        exclude = read(os.path.join(other, ".git", "info", "exclude"))
        self.assertNotIn(".devcontainer/", exclude.replace("/.devcontainer/.env", ""))
        self.assertEqual(read(os.path.join(other, ".devcontainer", ".gitignore")).count("\n*\n"), 1)

    def test_a_second_adopt_leaves_the_ignore_file_alone(self):
        other, git = self.fresh_repo()
        self.engine("adopt", other)
        path = os.path.join(other, ".devcontainer", ".gitignore")
        before = read(path)

        result = self.engine("adopt", other)

        self.assertEqual(read(path), before)
        self.assertNotIn("wrote .devcontainer/.gitignore", output_of(result))

    def test_a_project_that_tracks_the_folder_keeps_its_git_untouched_and_is_told(self):
        result = self.adopt()

        self.assertEqual(result.returncode, 0, output_of(result))
        self.assertFalse(os.path.exists(os.path.join(self.repo, ".devcontainer", ".gitignore")))
        self.assertIn("tracked in git", output_of(result))

    def test_a_slot_receives_the_ignore_file(self):
        git = lambda *args: subprocess.run(
            [self.harness.real_git_bin, "-C", self.repo, *args],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        git("rm", "-r", "-q", "--cached", ".devcontainer")
        git("commit", "-qm", "untrack .devcontainer")
        self.adopt()

        result = self.harness.run_acquire()

        self.assertEqual(result.returncode, 0, output_of(result))
        slot = self.harness.slot_dir(self.harness.acquired_slot_name(result))
        self.assertTrue(os.path.isfile(os.path.join(slot, ".devcontainer", ".gitignore")))


class AdoptMakesTheFolderIgnoreItself(AdoptDocsTestCase):
    def fresh_repo(self):
        other = os.path.join(self.harness.tmp, "ignored-app")
        os.makedirs(other)
        git = lambda *args: subprocess.run(
            [self.harness.real_git_bin, "-C", other, *args],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        git("init", "-q")
        git("config", "user.email", "f@example.com")
        git("config", "user.name", "f")
        write(os.path.join(other, "package.json"), "{}\n")
        git("add", "-A")
        git("commit", "-qm", "init")
        return other, git

    def test_git_status_shows_nothing_for_the_folder_even_with_a_secret_in_it(self):
        other, git = self.fresh_repo()
        write(os.path.join(other, ".devcontainer", "agent.env"), "GH_TOKEN=ghp_SENTINEL_ignore\n")

        result = self.engine("adopt", other)

        self.assertEqual(result.returncode, 0, output_of(result))
        status = git("status", "--short", "--", ".devcontainer").stdout.decode()
        self.assertEqual(status.strip(), "", status)
        ignore = read(os.path.join(other, ".devcontainer", ".gitignore"))
        self.assertIn("\n*\n", "\n" + ignore)
        self.assertTrue(ignore.startswith("#"), ignore)

    def test_the_ignore_file_is_the_only_mechanism_for_the_folder(self):
        other, git = self.fresh_repo()

        self.engine("adopt", other)
        self.engine("adopt", other)

        exclude = read(os.path.join(other, ".git", "info", "exclude"))
        self.assertNotIn("/.devcontainer/\n", exclude)
        self.assertEqual(read(os.path.join(other, ".devcontainer", ".gitignore")).count("\n*\n"), 1)

    def test_a_second_adopt_leaves_the_ignore_file_alone(self):
        other, git = self.fresh_repo()
        self.engine("adopt", other)
        path = os.path.join(other, ".devcontainer", ".gitignore")
        before = read(path)

        result = self.engine("adopt", other)

        self.assertEqual(read(path), before)
        self.assertNotIn("wrote .devcontainer/.gitignore", output_of(result))

    def test_a_project_that_tracks_the_folder_keeps_its_git_untouched_and_is_told(self):
        result = self.adopt()

        self.assertEqual(result.returncode, 0, output_of(result))
        self.assertFalse(os.path.exists(os.path.join(self.repo, ".devcontainer", ".gitignore")))
        self.assertIn("tracked in git", output_of(result))

    def test_a_slot_receives_the_ignore_file(self):
        git = lambda *args: subprocess.run(
            [self.harness.real_git_bin, "-C", self.repo, *args],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        git("rm", "-r", "-q", "--cached", ".devcontainer")
        git("commit", "-qm", "untrack .devcontainer")
        self.adopt()

        result = self.harness.run_acquire()

        self.assertEqual(result.returncode, 0, output_of(result))
        slot = self.harness.slot_dir(self.harness.acquired_slot_name(result))
        self.assertTrue(os.path.isfile(os.path.join(slot, ".devcontainer", ".gitignore")))


if __name__ == "__main__":
    unittest.main()
