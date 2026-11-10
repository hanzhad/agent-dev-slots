import glob
import os
import shutil
import stat
import subprocess
import tempfile
import unittest

from harness import (
    DevContainerHarness,
    DevContainerTestCase,
    PROJECT_PREFIX,
    REPO_ROOT,
    SCRIPT_REL,
    project_env_text,
)

ENGINE = os.path.join(REPO_ROOT, SCRIPT_REL)
NL = chr(10)
CRED_REL = ".devcontainer/agent.env"
APP_NAMES = [
    "FORGE_APP_ID",
    "FORGE_APP_INSTALLATION_ID",
    "FORGE_APP_SLUG",
    "FORGE_APP_BOT_ID",
    "FORGE_APP_KEY_FILE",
    "FORGE_APP_REPOS",
]
OUTSIDE = "outside what the engine container can see"


def output_of(result):
    return result.stdout.decode() + result.stderr.decode()


def write(path, text, mode=0o600):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.chmod(path, mode)


def read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def lines(*items):
    return "".join(item + NL for item in items)


IDENTITY = lines("GIT_USER_NAME=demo-agent", "GIT_USER_EMAIL=demo-agent@example.com")


class CredentialTestCase(DevContainerTestCase):
    """A fixture project whose project.env asks for FORGE_AUTH."""

    MODE = "token"
    CRED_REL = CRED_REL

    def setUp(self):
        super().setUp()
        self.project(self.MODE)

    def project(self, mode, **keys):
        values = {"FORGE_AUTH": mode, "FORGE_AUTH_FILE": self.CRED_REL}
        values.update(keys)
        self.harness.build_fixture_repo(with_parent_env=IDENTITY, project_env=project_env_text(**values))

    @property
    def repo(self):
        return self.harness.fixture_repo

    def cred_path(self):
        return os.path.join(self.repo, self.CRED_REL)

    def credential(self, *items, mode=0o600):
        write(self.cred_path(), lines(*items), mode=mode)

    def app_values(self, key_path, **over):
        values = {
            "FORGE_APP_ID": "1234567",
            "FORGE_APP_INSTALLATION_ID": "7654321",
            "FORGE_APP_SLUG": "demo-agent",
            "FORGE_APP_BOT_ID": "424242",
            "FORGE_APP_KEY_FILE": key_path,
            "FORGE_APP_REPOS": "demo",
        }
        values.update(over)
        return [k + "=" + v for k, v in values.items()]

    def leases(self):
        return glob.glob(os.path.join(self.harness.state_dir, "leases", "*.json"))

    def engine(self, *args, caller_pwd=None, **env):
        overrides = dict(env)
        overrides["DEV_CONTAINER_CALLER_PWD"] = caller_pwd or self.repo
        return subprocess.run(
            ["bash", ENGINE, *args],
            cwd=self.harness.tmp,
            env=self.harness._env(overrides),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=120,
        )

    def refused(self, result):
        text = output_of(result)
        self.assertNotEqual(result.returncode, 0, text)
        self.assertEqual(self.leases(), [], "a refused acquire must leave no lease")
        return text


class TheCredentialFileIsCheckedAtEveryAcquire(CredentialTestCase):
    def test_a_complete_file_with_mode_0600_is_accepted(self):
        self.credential("GH_TOKEN=ghp_SENTINEL_credcheck")

        result = self.harness.run_acquire()

        self.assertEqual(result.returncode, 0, output_of(result))

    def test_a_missing_file_is_refused_with_the_fix_and_the_next_command(self):
        text = self.refused(self.harness.run_acquire())

        self.assertIn(CRED_REL, text)
        self.assertRegex(text, "(?i)does not exist|is missing")
        self.assertIn("GH_TOKEN", text)
        self.assertIn("Fix:", text)
        self.assertIn("Next:", text)

    def test_a_file_that_others_can_read_is_refused_with_the_chmod_that_fixes_it(self):
        self.credential("GH_TOKEN=ghp_SENTINEL_credcheck", mode=0o644)

        text = self.refused(self.harness.run_acquire())

        self.assertIn(CRED_REL, text)
        self.assertIn("chmod 600", text)
        self.assertIn("644", text)
        self.assertNotIn("ghp_SENTINEL_credcheck", text)
        self.assertIn("Next:", text)

    def test_a_file_without_the_required_key_is_refused_naming_the_key(self):
        self.credential("SOMETHING_ELSE=1")

        text = self.refused(self.harness.run_acquire())

        self.assertIn("GH_TOKEN", text)
        self.assertIn(CRED_REL, text)
        self.assertIn("Fix:", text)
        self.assertIn("Next:", text)

    def test_the_file_is_checked_again_on_the_next_acquire(self):
        self.credential("GH_TOKEN=ghp_SENTINEL_credcheck")
        first = self.harness.run_acquire(holder="one")
        self.assertEqual(first.returncode, 0, output_of(first))

        os.chmod(self.cred_path(), 0o666)
        second = self.harness.run_acquire(holder="two")

        self.assertNotEqual(second.returncode, 0, output_of(second))
        self.assertIn("chmod 600", output_of(second))

    def test_a_project_that_asks_for_no_forge_auth_needs_no_file(self):
        other = DevContainerHarness()
        self.addCleanup(other.cleanup)
        other.build_fixture_repo(with_parent_env=IDENTITY, project_env=project_env_text())

        result = other.run_acquire()

        self.assertEqual(result.returncode, 0, output_of(result))


class AKeyFileTheEngineCannotSeeIsRefused(CredentialTestCase):
    MODE = "app"

    def setUp(self):
        super().setUp()
        self.elsewhere = tempfile.mkdtemp(prefix="outside-the-tree-")
        self.addCleanup(shutil.rmtree, self.elsewhere, ignore_errors=True)

    def key_at(self, path):
        write(path, "not a real key, the check never parses it" + NL)
        return path

    def test_a_key_outside_the_trees_the_engine_container_mounts_is_refused_with_the_exact_message(self):
        key = self.key_at(os.path.join(self.elsewhere, "app.pem"))
        self.credential(*self.app_values(key))

        text = self.refused(self.harness.run_acquire())

        self.assertIn(OUTSIDE, text)
        self.assertIn("FORGE_APP_KEY_FILE", text)
        self.assertIn(key, text)
        self.assertIn("Fix:", text)
        self.assertIn("Next:", text)

    def test_a_key_under_the_projects_root_is_accepted(self):
        key = self.key_at(os.path.join(self.harness.tmp, "keys", "app.pem"))
        self.credential(*self.app_values(key))

        result = self.harness.run_acquire()

        self.assertEqual(result.returncode, 0, output_of(result))

    def test_a_key_named_relative_to_the_checkout_is_accepted(self):
        self.key_at(os.path.join(self.repo, ".devcontainer", "app.pem"))
        self.credential(*self.app_values(".devcontainer/app.pem"))

        result = self.harness.run_acquire()

        self.assertEqual(result.returncode, 0, output_of(result))

    def test_a_key_that_is_not_there_is_refused_naming_the_setting_not_the_path(self):
        key = os.path.join(self.harness.tmp, "keys", "missing.pem")
        self.credential(*self.app_values(key))

        text = self.refused(self.harness.run_acquire())

        self.assertIn("FORGE_APP_KEY_FILE", text)
        self.assertNotIn(key, text)
        self.assertNotIn(OUTSIDE, text)

    def test_the_missing_names_of_app_mode_are_all_named(self):
        self.credential("FORGE_APP_ID=1")

        text = self.refused(self.harness.run_acquire())

        for name in APP_NAMES[1:]:
            self.assertIn(name, text)


class AdoptChecksTheCredentialFileAfterWritingEverythingElse(CredentialTestCase):
    def adopt(self):
        return self.engine("adopt")

    def test_a_missing_file_fails_adopt_with_the_fix_but_the_project_is_adopted_and_documented(self):
        result = self.adopt()

        text = output_of(result)
        self.assertNotEqual(result.returncode, 0, text)
        self.assertIn(CRED_REL, text)
        self.assertIn("Fix:", text)
        self.assertIn("Next:", text)
        self.assertIn("adopted", text)
        self.assertTrue(os.path.isfile(os.path.join(self.repo, ".devcontainer", "README.md")))

    def test_a_file_with_the_wrong_mode_fails_adopt(self):
        self.credential("GH_TOKEN=ghp_SENTINEL_credcheck", mode=0o644)

        result = self.adopt()

        self.assertNotEqual(result.returncode, 0, output_of(result))
        self.assertIn("chmod 600", output_of(result))

    def test_a_ready_file_lets_adopt_succeed(self):
        self.credential("GH_TOKEN=ghp_SENTINEL_credcheck")

        result = self.adopt()

        self.assertEqual(result.returncode, 0, output_of(result))


class TheDoctorChecksTheCredentialFile(CredentialTestCase):
    def doctor(self, *args):
        return self.harness.run("doctor", *args)

    def test_it_names_a_missing_file_a_wrong_mode_and_a_missing_key(self):
        text = output_of(self.doctor())
        self.assertRegex(text, "FAIL .*" + CRED_REL)

        self.credential("GH_TOKEN=ghp_SENTINEL_credcheck", mode=0o640)
        text = output_of(self.doctor())
        self.assertRegex(text, "FAIL .*chmod 600")

        self.credential("OTHER=1")
        text = output_of(self.doctor())
        self.assertRegex(text, "FAIL .*GH_TOKEN")

    def test_a_good_file_is_reported_ok_and_fails_nothing(self):
        self.credential("GH_TOKEN=ghp_SENTINEL_credcheck")

        result = self.doctor()

        self.assertRegex(result.stdout.decode(), "ok +credential file " + CRED_REL)
        self.assertNotIn("FAIL", result.stderr.decode())

    def test_the_token_is_not_sent_anywhere_unless_asked_to_with_live(self):
        self.credential("GH_TOKEN=ghp_SENTINEL_credcheck")
        called = os.path.join(self.harness.tmp, "gh-called")
        gh = os.path.join(self.harness.stub_bin, "gh")
        write(gh, "#!/usr/bin/env bash" + NL + "echo x >> " + called + NL, mode=0o755)

        self.doctor()

        self.assertFalse(os.path.exists(called), "doctor without --live must not call gh")

    def test_an_invalid_token_is_named_when_gh_says_so(self):
        self.credential("GH_TOKEN=ghp_SENTINEL_credcheck")
        gh = os.path.join(self.harness.stub_bin, "gh")
        write(gh, "#!/usr/bin/env bash" + NL + "echo 'HTTP 401: Bad credentials' >&2" + NL + "exit 1" + NL, mode=0o755)

        result = self.doctor("--live")

        self.assertNotEqual(result.returncode, 0)
        self.assertRegex(output_of(result), "FAIL .*(token|GH_TOKEN).*(401|Bad credentials|rejected)")
        self.assertNotIn("ghp_SENTINEL_credcheck", output_of(result))

    def test_a_live_token_is_reported_ok(self):
        self.credential("GH_TOKEN=ghp_SENTINEL_credcheck")
        gh = os.path.join(self.harness.stub_bin, "gh")
        write(gh, "#!/usr/bin/env bash" + NL + "echo octocat" + NL, mode=0o755)

        result = self.doctor("--live")

        self.assertRegex(result.stdout.decode(), "ok +.*token.*(live|accepted)")


class AComposeFileThatProjectEnvNamesMustExist(CredentialTestCase):
    MODE = None

    def project(self, mode, **keys):
        values = {"COMPOSE_DIR": "deploy", "COMPOSE_FILES": "compose.yml"}
        values.update(keys)
        self.harness.build_fixture_repo(with_parent_env=IDENTITY, project_env=project_env_text(**values))

    def test_acquire_refuses_when_the_named_compose_file_is_missing_and_says_how_to_fix_it(self):
        text = self.refused(self.harness.run_acquire())

        self.assertIn("deploy/compose.yml", text)
        self.assertRegex(text, "(?i)does not exist|is missing")
        self.assertIn("COMPOSE_DIR", text)
        self.assertIn("Fix:", text)
        self.assertIn("Next:", text)

    def test_doctor_names_it(self):
        text = output_of(self.harness.run("doctor"))

        self.assertRegex(text, "FAIL .*deploy/compose.yml")

    def test_acquire_works_once_the_file_is_there(self):
        write(os.path.join(self.repo, "deploy", "compose.yml"), "services:" + NL + "  app: {}" + NL, mode=0o644)
        write(os.path.join(self.repo, "deploy", ".env"), IDENTITY)

        result = self.harness.run_acquire()

        self.assertEqual(result.returncode, 0, output_of(result))


class AMissingStarterComposeIsWrittenOnAcquire(CredentialTestCase):
    MODE = None

    def project(self, mode, **keys):
        self.harness.build_fixture_repo(with_parent_env=IDENTITY, project_env=project_env_text())

    def test_project_env_present_and_the_default_compose_file_gone_is_healed_not_ignored(self):
        compose = os.path.join(self.repo, ".devcontainer", "docker-compose.yml")
        os.remove(compose)
        subprocess.run(
            [self.harness.real_git_bin, "-C", self.repo, "commit", "-qam", "drop compose"],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )

        result = self.harness.run_acquire()

        self.assertTrue(os.path.isfile(compose), output_of(result))
        self.assertIn("services:", read(compose))
        self.assertRegex(output_of(result), "(?i)starter")


class SlotCopyMixin:
    def setUp(self):
        super().setUp()
        git = lambda *args: subprocess.run(
            [self.harness.real_git_bin, "-C", self.repo, *args],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        git("rm", "-r", "-q", "--cached", ".devcontainer")
        git("commit", "-qm", "untrack .devcontainer")
        write(os.path.join(self.repo, ".git", "info", "exclude"), "/.devcontainer/" + NL, mode=0o644)

    def acquire_with(self, extra=()):
        write(self.cred_path(), "GH_TOKEN=ghp_SENTINEL_credcheck" + NL)
        write(os.path.join(self.repo, ".devcontainer", "notes.txt"), "kept" + NL, mode=0o644)
        for rel in extra:
            write(os.path.join(self.repo, ".devcontainer", rel), "SECRET_SENTINEL" + NL)
        result = self.harness.run_acquire()
        self.assertEqual(result.returncode, 0, output_of(result))
        return self.harness.slot_dir(self.harness.acquired_slot_name(result))

    def files_in(self, slot):
        found = []
        for root, _dirs, names in os.walk(slot):
            if os.sep + ".git" in root + os.sep:
                continue
            found += [os.path.relpath(os.path.join(root, n), slot) for n in names]
        return found


class CredentialsAreNeverCopiedIntoASlot(SlotCopyMixin, CredentialTestCase):
    def test_the_credential_file_and_env_style_secrets_stay_out_but_other_files_arrive(self):
        slot = self.acquire_with(extra=["other.env", "app.pem", "deploy.key", ".env.local"])

        files = self.files_in(slot)

        self.assertIn(".devcontainer/notes.txt", files)
        for rel in [CRED_REL, ".devcontainer/.env", ".devcontainer/other.env",
                    ".devcontainer/app.pem", ".devcontainer/deploy.key", ".devcontainer/.env.local"]:
            self.assertNotIn(rel, files)
        self.assertNotIn("SECRET_SENTINEL", "".join(read(os.path.join(slot, f)) for f in files))


class ACredentialFileWithAnyNameStaysOutToo(SlotCopyMixin, CredentialTestCase):
    CRED_REL = ".devcontainer/creds.txt"

    def test_a_credential_file_with_any_name_inside_the_compose_directory_stays_out(self):
        slot = self.acquire_with()

        files = self.files_in(slot)

        self.assertIn(".devcontainer/notes.txt", files)
        self.assertNotIn(".devcontainer/creds.txt", files)


class TheEngineSaysWhereItRunsWhenItCannotSeeAPath(CredentialTestCase):
    def test_on_the_host_the_error_points_at_the_thin_command_not_at_a_missing_setting(self):
        result = self.engine("adopt", self.repo, DEV_CONTAINER_PROJECTS_ROOT="")

        text = output_of(result)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("scripts/dev-container-thin", text)
        self.assertNotIn("none was set", text)

    def test_in_the_container_a_path_outside_the_root_still_names_the_root(self):
        elsewhere = tempfile.mkdtemp(prefix="not-the-root-")
        self.addCleanup(shutil.rmtree, elsewhere, ignore_errors=True)

        result = self.engine("adopt", elsewhere)

        text = output_of(result)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("outside the projects root", text)
        self.assertIn(self.harness.tmp, text)


if __name__ == "__main__":
    unittest.main()
