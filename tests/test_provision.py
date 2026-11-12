import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from unittest import mock

from harness import (
    DevContainerTestCase,
    PROJECT_PREFIX,
    project_env_text,
)

TOK = "ghs_SENTINEL_prov_slot"
OWNER_TOK = "ghp_SENTINEL_prov_owner"
ENV_FILE_REL = ".devcontainer/.env"
IDENTITY = ["GIT_USER_NAME=demo-agent", "GIT_USER_EMAIL=demo-agent@example.com"]
KEY_FILE = None
_KEY_DIR = None


def setUpModule():
    global KEY_FILE, _KEY_DIR
    _KEY_DIR = tempfile.mkdtemp(prefix="provision-app-key-")
    KEY_FILE = os.path.join(_KEY_DIR, "app.pem")
    subprocess.run(
        ["openssl", "genrsa", "-out", KEY_FILE, "2048"],
        check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )


def tearDownModule():
    shutil.rmtree(_KEY_DIR, ignore_errors=True)


def read(path):
    with open(path, "rb") as handle:
        return handle.read()


def text(result):
    return result.stdout.decode() + result.stderr.decode()


def lines(path):
    if not os.path.isfile(path):
        return []
    return [l for l in read(path).decode().splitlines() if l]


def mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


class ProvisionCase(DevContainerTestCase):
    """What the engine puts into a slot after `up`, against the harness's stand-in container.

    The stand-in runs the provisioning script for real, as the current user, in a sandbox
    that plays the container: <docker state>/<project>.container/{home,gitconfig}.
    """

    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("GH_TOKEN", None)

    def project(self, mode_name="token", **keys):
        values = dict(keys)
        env = list(IDENTITY)
        if mode_name == "token":
            values.update(FORGE_AUTH="token", FORGE_AUTH_FILE=ENV_FILE_REL)
            env.insert(0, "GH_TOKEN=" + TOK)
        elif mode_name == "app":
            values.update(FORGE_AUTH="app", FORGE_AUTH_FILE=ENV_FILE_REL)
            # The engine only sees the projects root: the key is copied under it.
            key = os.path.join(self.harness.tmp, "keys", "app.pem")
            os.makedirs(os.path.dirname(key), exist_ok=True)
            shutil.copyfile(KEY_FILE, key)
            os.chmod(key, 0o600)
            env = [
                "FORGE_APP_ID=1234567", "FORGE_APP_INSTALLATION_ID=7654321",
                "FORGE_APP_SLUG=demo-agent", "FORGE_APP_BOT_ID=424242",
                "FORGE_APP_KEY_FILE=" + key, "FORGE_APP_REPOS=demo demo.docs",
            ]
        self.harness.build_fixture_repo(
            with_parent_env="".join(l + "\n" for l in env),
            project_env=project_env_text(**values),
        )

    def acquire(self, holder="test", owner=None, extra=None):
        result = self.harness.run(
            "acquire", "--for", holder, *(["--as", owner] if owner else []), port_overrides=extra
        )
        self.assertEqual(result.returncode, 0, text(result))
        return self.harness.acquired_slot_name(result)

    def sid(self, name):
        return f"{PROJECT_PREFIX}-{name}"

    def sandbox(self, name):
        return os.path.join(self.harness.docker_state, self.sid(name) + ".container")

    def marker(self, name, kind):
        return os.path.join(self.harness.docker_state, f"{self.sid(name)}.{kind}")

    def provision_calls(self, name):
        return lines(self.marker(name, "provision-calls"))

    def gitconfig(self, name):
        return os.path.join(self.sandbox(name), "gitconfig")

    def config_values(self, name, key):
        result = subprocess.run(
            ["git", "config", "--file", self.gitconfig(name), "--get-all", key],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        return result.stdout.decode().splitlines()

    def container_key(self, name):
        return os.path.join(self.sandbox(name), "home", ".ssh", "container_signing")

    def host_key(self):
        return os.path.join(self.harness.state_dir, "signing", self.harness.project_key, "container_signing")

    def git_env(self, name):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "SSH_ASKPASS"))}
        home = tempfile.mkdtemp(prefix="provision-git-home-")
        self.addCleanup(shutil.rmtree, home, True)
        env.update(
            HOME=home, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=self.gitconfig(name),
            GIT_TERMINAL_PROMPT="0",
            GIT_AUTHOR_NAME="a", GIT_AUTHOR_EMAIL="a@example.com",
            GIT_COMMITTER_NAME="a", GIT_COMMITTER_EMAIL="a@example.com",
        )
        return env

    def overlay(self, name):
        return read(os.path.join(self.harness.state_dir, "overlays", self.sid(name) + ".yml")).decode()

    def up_envs(self, name):
        return lines(self.marker(name, "up-env"))


class Credentials(ProvisionCase):
    def test_acquire_installs_the_git_credential_helper_as_root_in_the_slot(self):
        self.project("token")
        name = self.acquire()
        calls = self.provision_calls(name)
        self.assertEqual(len(calls), 1, calls)
        self.assertTrue(calls[0].startswith("user=root "), calls[0])
        self.assertEqual(len(self.config_values(name, "credential.https://github.com.helper")), 1)

    def test_the_helper_hands_git_the_token_from_the_environment_at_the_time_it_asks(self):
        self.project("token")
        name = self.acquire()
        env = self.git_env(name)
        ask = lambda extra: subprocess.run(
            ["git", "credential", "fill"], input=b"protocol=https\nhost=github.com\n\n",
            env={**env, **extra}, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        with_token = ask({"GH_TOKEN": "ghs_asked_for_now"})
        self.assertEqual(with_token.returncode, 0, with_token.stderr.decode())
        out = with_token.stdout.decode().splitlines()
        self.assertIn("username=x-access-token", out)
        self.assertIn("password=ghs_asked_for_now", out)
        without = ask({})
        self.assertNotEqual(without.returncode, 0)
        self.assertNotIn("password=", without.stdout.decode())

    def test_the_helper_works_the_same_in_app_mode(self):
        self.project("app")
        name = self.acquire()
        env = {**self.git_env(name), "GH_TOKEN": "ghs_minted_per_run"}
        result = subprocess.run(
            ["git", "credential", "fill"], input=b"protocol=https\nhost=github.com\n\n",
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self.assertIn("password=ghs_minted_per_run", result.stdout.decode().splitlines())

    def test_no_token_and_no_credential_file_is_copied_into_the_slot(self):
        self.project("token")
        name = self.acquire()
        found = []
        for root, _, files in os.walk(self.sandbox(name)):
            for f in files:
                if TOK.encode() in read(os.path.join(root, f)):
                    found.append(os.path.join(root, f))
        self.assertEqual(found, [])
        self.assertNotIn(TOK, " ".join(self.provision_calls(name)))


class SigningKey(ProvisionCase):
    def test_the_projects_key_is_installed_for_the_container_user_and_private(self):
        self.project("token")
        name = self.acquire()
        self.assertEqual(read(self.container_key(name)), read(self.host_key()))
        self.assertEqual(read(self.container_key(name) + ".pub"), read(self.host_key() + ".pub"))
        self.assertEqual(mode(self.container_key(name)), 0o600)
        self.assertEqual(mode(os.path.dirname(self.container_key(name))), 0o700)
        self.assertIn("home=/home/node owner=node", self.provision_calls(name)[0])

    def test_git_is_told_to_sign_every_commit_with_it(self):
        self.project("token")
        name = self.acquire()
        self.assertEqual(self.config_values(name, "gpg.format"), ["ssh"])
        self.assertEqual(self.config_values(name, "commit.gpgsign"), ["true"])
        self.assertEqual(self.config_values(name, "user.signingkey"), [self.container_key(name)])

    def test_a_commit_made_with_that_config_carries_an_ssh_signature(self):
        self.project("token")
        name = self.acquire()
        repo = tempfile.mkdtemp(prefix="provision-signed-")
        self.addCleanup(shutil.rmtree, repo, True)
        env = self.git_env(name)
        for args in (["init", "-q"], ["commit", "-q", "--allow-empty", "-m", "signed"]):
            result = subprocess.run(["git", "-C", repo, *args], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.assertEqual(result.returncode, 0, result.stderr.decode())
        commit = subprocess.run(["git", "-C", repo, "cat-file", "-p", "HEAD"], env=env, stdout=subprocess.PIPE)
        self.assertIn("-----BEGIN SSH SIGNATURE-----", commit.stdout.decode())

    def test_signing_is_on_in_app_mode_too(self):
        self.project("app")
        name = self.acquire()
        self.assertEqual(self.config_values(name, "commit.gpgsign"), ["true"])
        self.assertEqual(self.config_values(name, "gpg.format"), ["ssh"])
        self.assertEqual(read(self.container_key(name)), read(self.host_key()))

    def test_the_key_survives_the_container_being_recreated(self):
        self.project("token")
        name = self.acquire()
        before = read(self.host_key())
        shutil.rmtree(self.sandbox(name))
        result = self.harness.run("restart", name)
        self.assertEqual(result.returncode, 0, text(result))
        self.assertEqual(read(self.container_key(name)), before)
        self.assertEqual(self.config_values(name, "commit.gpgsign"), ["true"])
        self.assertEqual(read(self.host_key()), before)

    def test_every_slot_of_a_project_gets_the_same_key_and_releasing_one_keeps_it(self):
        self.project("token")
        first = self.acquire(holder="a", owner="o-a")
        second = self.acquire(holder="b", owner="o-b")
        self.assertNotEqual(first, second)
        self.assertEqual(read(self.container_key(first)), read(self.container_key(second)))
        before = read(self.host_key())
        self.assertEqual(self.harness.run("release", first, "--for", "a").returncode, 0)
        self.assertEqual(read(self.host_key()), before)


class Rerun(ProvisionCase):
    def test_provisioning_twice_leaves_one_helper_one_key_and_the_same_files(self):
        self.project("token")
        name = self.acquire()
        key_before = read(self.container_key(name))
        config_before = read(self.gitconfig(name))
        result = self.harness.run("restart", name)
        self.assertEqual(result.returncode, 0, text(result))
        self.assertEqual(len(self.provision_calls(name)), 2)
        self.assertEqual(read(self.container_key(name)), key_before)
        self.assertEqual(read(self.gitconfig(name)), config_before)
        for key in ("credential.https://github.com.helper", "gpg.format", "commit.gpgsign", "user.signingkey"):
            self.assertEqual(len(self.config_values(name, key)), 1, key)
        leftovers = [f for f in os.listdir(os.path.dirname(self.container_key(name))) if f.startswith(".")]
        self.assertEqual(leftovers, [])

    def test_a_stale_helper_from_an_older_provision_is_replaced_not_added_to(self):
        self.project("token")
        name = self.acquire()
        subprocess.run(
            ["git", "config", "--file", self.gitconfig(name), "--add", "credential.https://github.com.helper", "!old"],
            check=True,
        )
        self.assertEqual(self.harness.run("restart", name).returncode, 0)
        values = self.config_values(name, "credential.https://github.com.helper")
        self.assertEqual(len(values), 1, values)
        self.assertNotEqual(values[0], "!old")


class Hooks(ProvisionCase):
    def test_acquire_restart_and_up_each_provision_once(self):
        self.project("token")
        name = self.acquire()
        self.assertEqual(len(self.provision_calls(name)), 1)
        self.assertEqual(self.harness.run("restart", name).returncode, 0)
        self.assertEqual(len(self.provision_calls(name)), 2)
        up = self.harness.run("up", name)
        self.assertEqual(up.returncode, 0, text(up))
        self.assertEqual(len(self.provision_calls(name)), 3)

    def test_it_runs_after_compose_up_not_before(self):
        self.project("token")
        name = self.acquire()
        self.assertTrue(os.path.isfile(self.marker(name, "running")))
        log = read(self.harness.docker_log).decode()
        provisioned = log.find("exec -T -u root app sh -c")
        last_up = log.rfind(" up -d")
        self.assertGreaterEqual(last_up, 0, "no compose up was logged")
        self.assertGreaterEqual(provisioned, 0, "no provisioning exec as root was logged")
        self.assertGreater(provisioned, last_up)

    def test_a_project_with_no_forge_auth_is_left_alone(self):
        self.project(None)
        name = self.acquire()
        self.assertEqual(self.provision_calls(name), [])
        self.assertFalse(os.path.exists(self.gitconfig(name)))
        self.assertFalse(os.path.exists(self.host_key()))


class Failures(ProvisionCase):
    def test_a_slot_without_gh_says_how_to_fix_it_and_still_has_everything_else(self):
        self.project("token")
        name = self.acquire()
        open(self.marker(name, "no-gh"), "w").close()
        result = self.harness.run("restart", name)
        out = text(result)
        self.assertNotEqual(result.returncode, 0, out)
        self.assertIn("gh", out)
        self.assertIn("is not installed", out)
        self.assertIn("apt-get install", out)
        self.assertIn(f"restart {name}", out)
        self.assertEqual(self.config_values(name, "commit.gpgsign"), ["true"])
        self.assertTrue(os.path.isfile(self.container_key(name)))

    def test_gh_missing_at_acquire_fails_the_acquire_but_the_slot_is_leased_and_named(self):
        self.project("token")
        result = self.harness.run("acquire", "--for", "test", port_overrides={"DOCKER_STUB_NO_GH": "1"})
        out = text(result)
        self.assertNotEqual(result.returncode, 0, out)
        self.assertIn("is not installed", out)
        self.assertIn("leased to test", out)
        self.assertTrue(os.listdir(os.path.join(self.harness.state_dir, "leases")))

    def test_an_exec_that_is_refused_names_the_slot_and_the_restart_that_reruns_it(self):
        self.project("token")
        name = self.acquire()
        open(self.marker(name, "provision-fail"), "w").close()
        result = self.harness.run("restart", name)
        out = text(result)
        self.assertNotEqual(result.returncode, 0, out)
        self.assertIn("provision", out)
        self.assertIn(name, out)
        self.assertIn(f"restart {name}", out)

    def test_a_signing_key_dir_that_cannot_be_made_stops_before_anything_is_sent(self):
        self.project("token")
        os.makedirs(os.path.join(self.harness.state_dir, "signing"), exist_ok=True)
        open(os.path.join(self.harness.state_dir, "signing", self.harness.project_key), "w").close()
        result = self.harness.run("acquire", "--for", "test")
        self.assertNotEqual(result.returncode, 0, text(result))
        self.assertIn("signing key", text(result))
        for f in os.listdir(self.harness.docker_state):
            self.assertFalse(f.endswith(".provision-calls"), f)


class TokenInTheContainerEnvironment(ProvisionCase):
    """`docker exec` and `docker run` see the container's own environment, set when compose creates it."""

    def test_the_overlay_passes_gh_token_to_the_service_that_mounts_the_checkout(self):
        self.project("token")
        name = self.acquire()
        overlay = self.overlay(name)
        self.assertIn("environment:", overlay)
        self.assertIn("GH_TOKEN: ${SLOT_GH_TOKEN:-}", overlay)
        self.assertEqual(overlay.count("\n      GH_TOKEN:"), 1)

    def test_in_token_mode_compose_is_given_the_slots_token_from_its_env_file(self):
        self.project("token")
        name = self.acquire()
        self.assertEqual(self.harness.run("restart", name).returncode, 0)
        self.assertEqual(self.up_envs(name)[-1].split()[0], "SLOT_GH_TOKEN=" + TOK)

    def test_the_owners_own_gh_token_in_the_callers_shell_never_reaches_the_container(self):
        self.project("token")
        extra = {"GH_TOKEN": OWNER_TOK}
        name = self.acquire(extra=extra)
        self.assertEqual(self.harness.run("restart", name, port_overrides=extra).returncode, 0)
        for line in self.up_envs(name):
            self.assertTrue(line.startswith("SLOT_GH_TOKEN=" + TOK), line)
        self.assertNotIn(OWNER_TOK.encode(), read(os.path.join(self.harness.state_dir, "overlays", self.sid(name) + ".yml")))

    def test_in_app_mode_the_container_starts_without_a_token(self):
        self.project("app")
        name = self.acquire()
        self.assertEqual(self.harness.run("restart", name).returncode, 0)
        for line in self.up_envs(name):
            self.assertTrue(line.startswith("SLOT_GH_TOKEN= "), line)


if __name__ == "__main__":
    unittest.main()
