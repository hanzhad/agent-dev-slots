import os
import re
import socket
import subprocess
import unittest

from harness import DevContainerTestCase, REPO_ROOT, SCRIPT_REL

IDENTITY = "GIT_USER_NAME=demo-agent\nGIT_USER_EMAIL=demo-agent@example.com\n"
CALLER = "/somewhere/on/the/host/dev-container-thin"


class EngineInContainer(DevContainerTestCase):
    """The engine runs from its own clone, not from the checkout it acts on.

    These run `bin/dev-container` of this repository against a fixture
    project elsewhere, with the variables the thin passes in.
    """

    def setUp(self):
        super().setUp()
        self.harness.build_fixture_repo(with_parent_env=IDENTITY)
        self.engine = os.path.join(REPO_ROOT, SCRIPT_REL)

    def engine_run(self, *args, drop=(), **extra):
        env = self.harness._env(None)
        env.update(extra)
        for name in drop:
            env.pop(name, None)
        return subprocess.run(
            ["bash", self.engine, *args], cwd=self.harness.tmp, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120,
        )

    def a_slot(self, port):
        self.harness.make_bare_slot(
            "shared", env_content=IDENTITY,
            ports_content=f"NOVNC_PORT={port + 1}\nSSH_PORT={port}\nVITE_DEV_PORT={port + 2}\n",
            local_identity=("demo-agent", "demo-agent@example.com"),
        )

    def test_the_repo_root_is_the_passed_working_directory(self):
        self.a_slot(39251)
        result = self.engine_run("list", "--brief")
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertIn("shared", result.stdout.decode())

    def test_a_subdirectory_and_a_linked_worktree_resolve_to_the_main_checkout(self):
        self.a_slot(39254)
        worktree = os.path.join(self.harness.tmp, "linked-worktree")
        subprocess.run(
            [self.harness.real_git_bin, "-C", self.harness.fixture_repo, "worktree", "add", "-q", "-b", "wt", worktree],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        for passed in (os.path.join(self.harness.fixture_repo, ".devcontainer"), worktree):
            result = self.engine_run("list", "--brief", DEV_CONTAINER_CALLER_PWD=passed)
            self.assertEqual(result.returncode, 0, f"{passed}: {result.stderr.decode()}")
            self.assertIn("shared", result.stdout.decode(), passed)

    def test_with_no_passed_directory_it_refuses_instead_of_using_its_own_clone(self):
        self.a_slot(39257)
        result = self.engine_run("list", "--brief", drop=("DEV_CONTAINER_CALLER_PWD",))
        self.assertNotEqual(result.returncode, 0, result.stdout.decode())
        self.assertIn("dev-container-thin", result.stderr.decode())

    def test_a_checkout_outside_the_projects_root_is_refused_with_the_install_command(self):
        self.a_slot(39260)
        elsewhere = os.path.join(self.harness.tmp, "elsewhere")
        os.makedirs(elsewhere)
        # "fixture" is a string prefix of the checkout's name, "fixture-repo", but not a path ancestor.
        for root in (elsewhere, os.path.join(self.harness.tmp, "fixture")):
            result = self.engine_run("list", "--brief", DEV_CONTAINER_PROJECTS_ROOT=root)
            self.assertNotEqual(result.returncode, 0, f"{root}: {result.stdout.decode()}")
            text = result.stderr.decode()
            self.assertIn("engine install --projects-root", text, root)
            self.assertNotIn("shared", result.stdout.decode(), root)

    def test_a_checkout_that_is_the_projects_root_itself_is_refused(self):
        self.a_slot(39261)
        result = self.engine_run("list", "--brief", DEV_CONTAINER_PROJECTS_ROOT=self.harness.fixture_repo)
        self.assertNotEqual(result.returncode, 0, result.stdout.decode())
        self.assertIn("engine install --projects-root", result.stderr.decode())

    def test_a_directory_that_is_no_checkout_is_refused(self):
        plain = os.path.join(self.harness.tmp, "plain-dir")
        os.makedirs(plain)
        result = self.engine_run("list", "--brief", DEV_CONTAINER_CALLER_PWD=plain)
        self.assertNotEqual(result.returncode, 0, result.stdout.decode())

    def test_usage_names_the_caller_not_the_engines_own_path(self):
        result = self.engine_run(DEV_CONTAINER_CALLER=CALLER)
        self.assertNotEqual(result.returncode, 0)
        text = result.stderr.decode()
        self.assertIn(f"Usage: {CALLER} acquire", text)
        self.assertNotIn(self.engine, text)

    def test_verb_help_names_the_caller(self):
        self.a_slot(39263)
        result = self.engine_run("acquire", "--help", DEV_CONTAINER_CALLER=CALLER)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertIn(f"{CALLER} acquire", result.stdout.decode())
        self.assertNotIn(self.engine, result.stdout.decode())

    def test_the_slot_verbs_do_not_refuse_because_they_run_inside_a_container(self):
        self.a_slot(39266)
        marker = os.path.join(self.harness.tmp, "pretend-dockerenv")
        open(marker, "w", encoding="utf-8").close()
        refusal = re.compile(r"on the host|inside a container|not in one|from the host")
        for args in (["run", "shared", "--", "true"], ["db-target", "shared"], ["install", "shared"]):
            result = self.engine_run(*args, DEV_CONTAINER_DOCKERENV_PATH=marker)
            self.assertNotRegex(result.stderr.decode(), refusal, args)


class PortInUse(DevContainerTestCase):
    """`port_in_use` asks docker; the engine's own loopback says nothing about the host's ports."""

    def test_a_loopback_listener_is_not_a_reason_to_skip_a_port(self):
        self.harness.build_fixture_repo()
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(listener.close)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        env = self.harness._env(None)
        env["DEV_CONTAINER_LIB"] = "1"
        result = subprocess.run(
            ["bash", "-c", '. "$0"; port_in_use "$1" || echo "RC=$?"', self.harness.script, str(port)],
            cwd=self.harness.tmp, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60,
        )
        self.assertIn("RC=1", result.stdout.decode())

    def test_a_port_docker_publishes_is_still_in_use(self):
        self.harness.build_fixture_repo()
        stub = os.path.join(self.harness.tmp, "docker-ps-stub")
        os.makedirs(stub)
        with open(os.path.join(stub, "docker"), "w", encoding="utf-8") as handle:
            handle.write('#!/bin/sh\necho "0.0.0.0:39299->22/tcp"\n')
        os.chmod(os.path.join(stub, "docker"), 0o755)
        env = self.harness._env(None)
        env["DEV_CONTAINER_LIB"] = "1"
        env["PATH"] = stub + os.pathsep + env["PATH"]
        result = subprocess.run(
            ["bash", "-c", '. "$0"; port_in_use 39299; echo "RC=$?"', self.harness.script],
            cwd=self.harness.tmp, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60,
        )
        self.assertIn("RC=0", result.stdout.decode())


if __name__ == "__main__":
    unittest.main()
