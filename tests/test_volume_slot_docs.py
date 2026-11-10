import os
import subprocess

from harness import DevContainerTestCase

MAIN_ENV = "GIT_USER_NAME=demo-agent\nGIT_USER_EMAIL=demo-agent@example.com\n"


class VolumeSlotDocsClone(DevContainerTestCase):
    """A slot whose workspace is a named volume gets the same read-only docs
    clone, held at the pin, that a directory slot gets.

    The harness keeps a volume's contents in a directory it can read, which
    a real volume is not: the engine reaches a real volume only through a
    docker command. So the tests assert on both the contents the harness can
    see and on the docker commands the engine ran."""

    def _git(self, repo, *args):
        return subprocess.run(
            [
                self.harness.real_git_bin, "-C", repo,
                "-c", "commit.gpgsign=false",
                "-c", "user.email=fixture@example.com",
                "-c", "user.name=fixture",
                *args,
            ],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        ).stdout.decode().strip()

    def _docs_commit(self, name):
        stub = self.harness.docs_repo_stub
        with open(os.path.join(stub, name), "w", encoding="utf-8") as handle:
            handle.write(name + "\n")
        self._git(stub, "add", name)
        self._git(stub, "commit", "-q", "-m", name)
        return self._git(stub, "rev-parse", "HEAD")

    def _pin_in_fixture(self, sha):
        fixture = self.harness.fixture_repo
        os.makedirs(os.path.join(fixture, ".github"), exist_ok=True)
        with open(os.path.join(fixture, ".github", "docs-pin"), "w", encoding="utf-8") as handle:
            handle.write("# the docs commit CI checks citations against\n" + sha + "\n")
        self._git(fixture, "add", ".github/docs-pin")
        self._git(fixture, "commit", "-q", "-m", "pin")

    def _docker_log(self):
        if not os.path.isfile(self.harness.docker_log):
            return ""
        with open(self.harness.docker_log, encoding="utf-8") as handle:
            return handle.read()

    def _acquire_volume_slot_with_pin_behind_tip(self):
        self.harness.build_fixture_repo(with_parent_env=MAIN_ENV)
        pin = self._git(self.harness.docs_repo_stub, "rev-parse", "HEAD")
        self._pin_in_fixture(pin)
        tip = self._docs_commit("after-the-pin.md")
        self.assertNotEqual(pin, tip, "fixture assumption: the docs tip is past the pin")
        result = self.harness.run_acquire()
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        name = self.harness.acquired_slot_name(result)
        self.assertTrue(self.harness.volume_exists(name), "fixture assumption: the slot is volume-backed")
        return pin, name

    def test_volume_slot_has_docs_repo_at_the_pin_not_the_tip(self):
        pin, name = self._acquire_volume_slot_with_pin_behind_tip()
        docs = os.path.join(self.harness.slot_dir(name), "docs", "repo")
        self.assertTrue(os.path.isdir(os.path.join(docs, ".git")), "no docs/repo in the volume slot")
        self.assertEqual(self._git(docs, "rev-parse", "HEAD"), pin)

    def test_volume_slot_docs_repo_is_read_only_like_a_directory_slots(self):
        _, name = self._acquire_volume_slot_with_pin_behind_tip()
        docs = os.path.join(self.harness.slot_dir(name), "docs", "repo")
        self.assertEqual(self._git(docs, "config", "--local", "core.hooksPath"), ".git/slot-readonly-hooks")
        self.assertEqual(
            self._git(docs, "config", "--local", "remote.origin.pushurl"),
            "read-only://docs-repo-is-read-only--clone-docs-fix",
        )
        for hook in ("pre-commit", "pre-push"):
            self.assertTrue(os.path.isfile(os.path.join(docs, ".git", "slot-readonly-hooks", hook)), hook)

    def test_volume_slot_docs_clone_is_made_through_docker_not_a_host_path(self):
        # A real volume has no host path, so the clone must be a docker
        # command that names the docs repository.
        self._acquire_volume_slot_with_pin_behind_tip()
        self.assertIn(
            self.harness.docs_repo_stub,
            self._docker_log(),
            "no docker command names DOCS_REPO_URL, so the volume's docs/repo cannot have been cloned inside it",
        )

    def test_volume_slot_without_docs_repo_url_clones_no_docs_through_docker(self):
        self.harness.build_fixture_repo(with_parent_env=MAIN_ENV)
        real_env = self.harness._env

        def env_without_docs(port_overrides):
            env = real_env(port_overrides)
            env.pop("DOCS_REPO_URL", None)
            return env

        self.harness._env = env_without_docs
        result = self.harness.run_acquire()
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertNotIn(self.harness.docs_repo_stub, self._docker_log())

    def test_volume_slot_acquire_fails_when_the_docs_clone_fails_and_leaves_no_slot(self):
        # With DOCS_REPO_URL configured, a slot is only reported as created when
        # docs/repo exists; a directory slot stops the acquire on a failed clone.
        self.harness.build_fixture_repo(with_parent_env=MAIN_ENV)
        real_env = self.harness._env
        unreachable = os.path.join(self.harness.tmp, "no-such-docs-repo")

        def env_with_unreachable_docs(port_overrides):
            env = real_env(port_overrides)
            env["DOCS_REPO_URL"] = unreachable
            return env

        self.harness._env = env_with_unreachable_docs
        result = self.harness.run_acquire()
        self.assertNotEqual(result.returncode, 0, result.stdout.decode())
        self.assertEqual(self.harness.volume_names(), [], "a failed acquire left a slot volume behind")
        leases = os.path.join(self.harness.state_dir, "leases")
        self.assertEqual(os.listdir(leases) if os.path.isdir(leases) else [], [], "a failed acquire left a lease")
