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


class AllocatedPorts(DevContainerTestCase):
    IDENTITY = "GIT_USER_NAME=demo-agent\nGIT_USER_EMAIL=demo-agent@example.com\n"

    def _up_lines(self):
        with open(self.harness.docker_log, encoding="utf-8") as handle:
            log = handle.read()
        return [line for line in log.splitlines() if " up " in line or line.endswith(" up")]

    def _git_in_fixture(self, *args):
        subprocess.run(
            ["git", "-C", self.harness.fixture_repo, "-c", "user.email=fixture@example.com",
             "-c", "user.name=fixture", "-c", "commit.gpgsign=false", *args],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )

    def test_each_named_port_gets_its_place_in_the_slots_block(self):
        self.harness.build_fixture_repo(
            with_parent_env=self.IDENTITY,
            project_env=project_env_text(SLOT_PORTS="SSH_PORT WEB_PORT"),
        )

        result = self.harness.run_acquire()

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        name = self.harness.acquired_slot_name(result)
        self.assertEqual(name, "slot-1")
        expected = {"SSH_PORT": str(slot_port(1, 0)), "WEB_PORT": str(slot_port(1, 1))}
        env = self.harness.read_env(name)
        ports = self.harness.read_ports(name)
        for key, value in expected.items():
            self.assertEqual(env[key], value, key)
            self.assertEqual(ports[key], value, key)
        self.assertNotIn("NOVNC_PORT", env)
        self.assertIn(f"WEB_PORT={expected['WEB_PORT']}", self._up_lines()[-1])

    def test_without_a_project_env_the_default_names_are_allocated(self):
        self.harness.build_fixture_repo(with_parent_env=self.IDENTITY)

        result = self.harness.run_acquire()

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        ports = self.harness.read_ports(self.harness.acquired_slot_name(result))
        self.assertEqual(
            ports, {name: str(slot_port(1, k)) for k, name in enumerate(DEFAULT_PORT_NAMES)}
        )

    def test_the_next_slot_gets_a_block_of_its_own(self):
        self.harness.build_fixture_repo(with_parent_env=self.IDENTITY)
        self.assertEqual(self.harness.run_acquire(holder="first").returncode, 0)

        result = self.harness.run_acquire(holder="second")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        name = self.harness.acquired_slot_name(result)
        self.assertEqual(name, "slot-2")
        self.assertEqual(self.harness.read_ports(name)["SSH_PORT"], str(slot_port(2, 0)))

    def test_the_lease_carries_the_slots_ssh_port(self):
        self.harness.build_fixture_repo(with_parent_env=self.IDENTITY)

        result = self.harness.run_acquire()

        name = self.harness.acquired_slot_name(result)
        with open(self.harness.lease_path(name), encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["ssh_port"], slot_port(1, 0))

    def test_a_port_something_else_holds_is_skipped_and_the_next_number_is_used(self):
        self.harness.build_fixture_repo(with_parent_env=self.IDENTITY)
        # The engine asks docker, not its own loopback: another project's published port.
        with open(os.path.join(self.harness.docker_state, "foreign.ports"), "w", encoding="utf-8") as handle:
            handle.write(f"SSH_PORT={slot_port(1, 0)}\n")

        result = self.harness.run_acquire()

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertFalse(os.path.exists(self.harness.lease_path("slot-1")))
        self.assertEqual(self.harness.read_ports("slot-2")["SSH_PORT"], str(slot_port(2, 0)))

    def test_the_ports_are_recorded_under_the_state_and_not_in_the_checkout(self):
        self.harness.build_fixture_repo(
            with_parent_env=self.IDENTITY,
            project_env=project_env_text(COMPOSE_DIR="compose"),
        )
        self._git_in_fixture("mv", ".devcontainer", "compose")
        self._git_in_fixture("commit", "-q", "-m", "move the compose directory")

        result = self.harness.run_acquire()

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        name = self.harness.acquired_slot_name(result)
        self.assertTrue(os.path.isfile(self.harness.ports_path(name)))
        self.assertFalse(
            os.path.exists(os.path.join(self.harness.slot_dir(name), ".devcontainer", ".slot-ports"))
        )

    def test_restart_repairs_a_missing_port_from_the_slots_block(self):
        self.harness.build_fixture_repo(project_env=project_env_text())
        self.harness.make_bare_slot(
            "slot-1",
            env_content="GIT_USER_NAME=demo-agent\nGIT_USER_EMAIL=old@example.com\n",
            ports_content="SSH_PORT=39220\nNOVNC_PORT=39230\n",
        )

        result = self.harness.run("restart", "slot-1")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        env_port = self.harness.read_env("slot-1")["VITE_DEV_PORT"]
        self.assertEqual(env_port, str(slot_port(1, 2)))
        self.assertEqual(self.harness.read_ports("slot-1")["VITE_DEV_PORT"], env_port)
        self.assertEqual(self.harness.read_ports("slot-1")["SSH_PORT"], "39220")

    def test_restart_passes_every_port_to_docker_compose_up(self):
        self.harness.build_fixture_repo(project_env=project_env_text())
        self.harness.make_bare_slot(
            "slot-1",
            env_content="GIT_USER_NAME=demo-agent\nGIT_USER_EMAIL=old@example.com\n",
            ports_content="SSH_PORT=39220\nNOVNC_PORT=39230\nVITE_DEV_PORT=39240\n",
        )

        result = self.harness.run("restart", "slot-1")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        line = self._up_lines()[-1]
        for pair in ("SSH_PORT=39220", "NOVNC_PORT=39230", "VITE_DEV_PORT=39240"):
            self.assertIn(pair, line)


class SlotEnvSetIsWrittenLiterally(DevContainerTestCase):
    IDENTITY = "GIT_USER_NAME=demo-agent\nGIT_USER_EMAIL=demo-agent@example.com\n"

    def _values(self):
        marks = [os.path.join(self.harness.tmp, f"ran-{n}") for n in range(3)]
        values = {
            "KEY_PIPE_E": "a|e",
            "KEY_SUBST": f"$(>{marks[0]})",
            "KEY_TICKS": f"`>{marks[1]}`",
            "KEY_SEMI": f"a;>{marks[2]}",
            "KEY_AMP": "a/e&b",
            "KEY_BACKSLASH": "a\\1b",
            "KEY_NEW": "x/e",
        }
        return marks, values

    def test_a_value_with_shell_and_sed_metacharacters_lands_literally_and_runs_nothing(self):
        marks, values = self._values()
        old = "".join(f"{key}=old\n" for key in values if key != "KEY_NEW")
        self.harness.build_fixture_repo(
            with_parent_env=self.IDENTITY + old,
            project_env=project_env_text(SLOT_ENV_SET=" ".join(f"{k}={v}" for k, v in values.items())),
        )

        result = self.harness.run_acquire()

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        env = self.harness.read_env(self.harness.acquired_slot_name(result))
        for key, value in values.items():
            self.assertEqual(env.get(key), value, key)
        for mark in marks:
            self.assertFalse(os.path.exists(mark), "a SLOT_ENV_SET value was executed")


class NoProjectEnvDefaults(DevContainerTestCase):
    IDENTITY = "GIT_USER_NAME=demo-agent\nGIT_USER_EMAIL=demo-agent@example.com\n"

    def test_a_checkout_path_with_spaces_adds_no_stray_entry_to_the_slots_env(self):
        self.harness.fixture_repo = os.path.join(self.harness.tmp, "fixture repo")
        self.harness.build_fixture_repo(with_parent_env=self.IDENTITY)

        result = self.harness.run_acquire()

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        name = self.harness.acquired_slot_name(result)
        with open(self.harness.env_path(name), encoding="utf-8") as handle:
            lines = [line.rstrip("\n") for line in handle if line.strip()]
        for line in lines:
            self.assertRegex(line, r"^[A-Za-z_][A-Za-z0-9_]*=", line)


class SettingsFromProjectEnv(DevContainerTestCase):
    IDENTITY = "GIT_USER_NAME=demo-agent\nGIT_USER_EMAIL=demo-agent@example.com\n"

    def test_slot_env_keys_does_not_choose_the_git_identity_variables(self):
        self.harness.build_fixture_repo(
            with_parent_env=self.IDENTITY,
            project_env=project_env_text(SLOT_ENV_KEYS="LOG_LEVEL THEME"),
        )

        result = self.harness.run_acquire()

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        name = self.harness.acquired_slot_name(result)
        self.assertEqual(self.harness.checkout_identity(name), "demo-agent")

    def test_the_install_script_comes_from_the_projects_compose_directory(self):
        self.harness.build_fixture_repo(
            with_parent_env=self.IDENTITY,
            project_env=project_env_text(COMPOSE_DIR="compose"),
        )
        os.rename(
            os.path.join(self.harness.fixture_repo, ".devcontainer"),
            os.path.join(self.harness.fixture_repo, "compose"),
        )

        result = self.harness.run_acquire()

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        name = self.harness.acquired_slot_name(result)
        calls = os.path.join(self.harness.docker_state, f"{PROJECT_PREFIX}-{name}.install-calls")
        self.assertTrue(os.path.isfile(calls), "install.sh was not run from the compose directory")


class OwnersPortsAreNeverUsed(DevContainerTestCase):

    def _restart(self, **recorded):
        ports = {"SSH_PORT": "39220", "NOVNC_PORT": "39230", "VITE_DEV_PORT": "39240"}
        ports.update(recorded)
        self.harness.build_fixture_repo(project_env=project_env_text())
        self.harness.make_bare_slot(
            "slot-1",
            env_content="GIT_USER_NAME=demo-agent\nGIT_USER_EMAIL=old@example.com\n",
            ports_content="".join(f"{k}={v}\n" for k, v in ports.items()),
        )
        result = self.harness.run("restart", "slot-1")
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        return result

    def test_restart_repairs_a_recorded_ssh_port_that_is_the_owners_own(self):
        self._restart(SSH_PORT=OWNER_PORTS["SSH_PORT"])

        env_port = self.harness.read_env("slot-1")["SSH_PORT"]
        self.assertEqual(env_port, str(slot_port(1, 0)))
        self.assertEqual(self.harness.read_ports("slot-1")["SSH_PORT"], env_port)

    def test_restart_repairs_a_recorded_novnc_port_that_is_the_owners_own(self):
        self._restart(NOVNC_PORT=OWNER_PORTS["NOVNC_PORT"])

        env_port = self.harness.read_env("slot-1")["NOVNC_PORT"]
        self.assertEqual(env_port, str(slot_port(1, 1)))
        self.assertEqual(self.harness.read_ports("slot-1")["NOVNC_PORT"], env_port)

    def test_restart_repairs_a_recorded_vite_port_that_is_the_owners_own(self):
        self._restart(VITE_DEV_PORT=OWNER_PORTS["VITE_DEV_PORT"])

        env_port = self.harness.read_env("slot-1")["VITE_DEV_PORT"]
        self.assertEqual(env_port, str(slot_port(1, 2)))
        self.assertEqual(self.harness.read_ports("slot-1")["VITE_DEV_PORT"], env_port)

    def test_restart_reuses_sane_recorded_ports_unchanged(self):
        result = self._restart()

        ports = self.harness.read_ports("slot-1")
        self.assertEqual(ports["SSH_PORT"], "39220")
        self.assertEqual(ports["NOVNC_PORT"], "39230")
        self.assertEqual(ports["VITE_DEV_PORT"], "39240")
        self.assertNotIn("assigned", result.stdout.decode())


class PortsAreNeverEmptyOrTheOwners(DevContainerTestCase):

    ENV = "GIT_USER_NAME=demo-agent\nGIT_USER_EMAIL=demo-agent@example.com\n"

    def test_a_ports_file_missing_a_port_gets_it_allocated_not_obeyed_half_empty(self):
        self.harness.build_fixture_repo(project_env=project_env_text())
        self.harness.make_bare_slot(
            "slot-2",
            env_content=self.ENV,
            ports_content="NOVNC_PORT=39236\n",
            local_identity=("demo-agent", "demo-agent@example.com"),
        )

        result = self.harness.run("restart", "slot-2")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        ports = self.harness.read_ports("slot-2")
        self.assertEqual(ports["SSH_PORT"], str(slot_port(2, 0)))
        self.assertEqual(ports["NOVNC_PORT"], "39236")
        self.assertEqual(ports["VITE_DEV_PORT"], str(slot_port(2, 2)))
        for name, owners in OWNER_PORTS.items():
            self.assertNotEqual(ports[name], owners)


class ListCallsAPortClashAClash(DevContainerTestCase):

    ENV = "GIT_USER_NAME=demo-agent\nGIT_USER_EMAIL=a@example.com\n"

    def test_list_calls_a_port_clash_a_clash_rather_than_free(self):
        self.harness.build_fixture_repo()
        self.harness.make_bare_slot(
            "first", env_content=self.ENV,
            ports_content="NOVNC_PORT=39230\nSSH_PORT=39220\nVITE_DEV_PORT=39240\n",
        )
        self.harness.make_bare_slot(
            "second", env_content=self.ENV,
            ports_content="NOVNC_PORT=39230\nSSH_PORT=39220\nVITE_DEV_PORT=39241\n",
        )

        result = self.harness.run("list", "--brief", "--no-git")

        stdout = result.stdout.decode()
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        row = [line for line in stdout.splitlines() if line.startswith("second")]
        self.assertTrue(row, stdout)
        self.assertIn("portclash", row[0], row[0])
        self.assertIn("39220", row[0], row[0])
        first = [line for line in stdout.splitlines() if line.startswith("first")]
        self.assertNotIn("free", first[0], first[0])


class SlotEnvSetOnEverySync(DevContainerTestCase):

    def _restart(self, env_content):
        self.harness.build_fixture_repo(project_env=project_env_text(SLOT_ENV_SET="KEY_A=new KEY_B=1"))
        self.harness.make_bare_slot(
            "slot-1",
            env_content=env_content,
            ports_content="SSH_PORT=39220\nNOVNC_PORT=39230\nVITE_DEV_PORT=39240\n",
        )
        result = self.harness.run("restart", "slot-1")
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        return self.harness.read_env("slot-1")

    def test_restart_adds_a_value_the_slots_env_lacks(self):
        env = self._restart(IDENTITY_ENV)

        self.assertEqual(env["KEY_A"], "new")
        self.assertEqual(env["KEY_B"], "1")

    def test_restart_replaces_a_value_the_slots_env_holds(self):
        env = self._restart(IDENTITY_ENV + "KEY_A=old\n")

        self.assertEqual(env["KEY_A"], "new")


class PrimaryPort(DevContainerTestCase):

    def test_the_first_name_in_the_list_is_the_slots_primary_port_whatever_it_is_called(self):
        self.harness.build_fixture_repo(
            with_parent_env=IDENTITY_ENV,
            project_env=project_env_text(SLOT_PORTS="WEB_PORT SSH_PORT"),
        )

        result = self.harness.run_acquire()

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        name = self.harness.acquired_slot_name(result)
        with open(self.harness.lease_path(name), encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["ssh_port"], slot_port(1, 0))
        self.assertIn(f'"sshPort": {slot_port(1, 0)}', result.stdout.decode())

    def test_a_value_another_slot_holds_under_a_different_name_is_a_clash(self):
        self.harness.build_fixture_repo(project_env=project_env_text(SLOT_PORTS="SSH_PORT WEB_PORT"))
        self.harness.make_bare_slot(
            "first", env_content=IDENTITY_ENV, ports_content="SSH_PORT=39220\nWEB_PORT=39230\n"
        )
        self.harness.make_bare_slot(
            "second", env_content=IDENTITY_ENV, ports_content="SSH_PORT=39231\nWEB_PORT=39220\n"
        )

        result = self.harness.run("list", "--brief", "--no-git")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        row = [line for line in result.stdout.decode().splitlines() if line.startswith("second")]
        self.assertTrue(row)
        self.assertIn("portclash", row[0], row[0])
        self.assertIn("39220", row[0], row[0])

    def test_a_value_another_project_holds_under_a_name_this_project_lacks_is_a_clash(self):
        self.harness.build_fixture_repo(project_env=project_env_text(SLOT_PORTS="SSH_PORT WEB_PORT"))
        self.harness.make_bare_slot(
            "first", env_content=IDENTITY_ENV, ports_content="SSH_PORT=39231\nDB_PORT=39220\n"
        )
        self.harness.make_bare_slot(
            "second", env_content=IDENTITY_ENV, ports_content="SSH_PORT=39220\nWEB_PORT=39232\n"
        )

        result = self.harness.run("list", "--brief", "--no-git")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        row = [line for line in result.stdout.decode().splitlines() if line.startswith("second")]
        self.assertTrue(row)
        self.assertIn("portclash", row[0], row[0])
        self.assertIn("39220", row[0], row[0])


class PortBaseIsValidated(DevContainerTestCase):

    def setUp(self):
        super().setUp()
        self.harness.build_fixture_repo(project_env=project_env_text())

    def _list(self, value):
        return self.harness.run("list", "--brief", "--no-git", port_overrides={"DEV_CONTAINER_PORT_BASE": value})

    def test_a_value_that_is_not_a_positive_integer_up_to_the_ceiling_refuses(self):
        for value in ("abc", "0", "-1", "4200x", "65201", "1" * 30, "042000"):
            with self.subTest(value=value):
                result = self._list(value)

                self.assertNotEqual(result.returncode, 0)
                self.assertIn("DEV_CONTAINER_PORT_BASE", result.stderr.decode())

    def test_the_ceiling_itself_is_accepted(self):
        result = self._list("65200")

        self.assertEqual(result.returncode, 0, result.stderr.decode())


class InheritedValuesAreValidated(DevContainerTestCase):

    def _acquire(self, **inherited):
        self.harness.build_fixture_repo(with_parent_env=IDENTITY_ENV, project_env=project_env_text())
        return self.harness.run("acquire", "--for", "test", port_overrides=inherited)

    def test_a_mount_directory_inherited_from_the_environment_must_stay_inside_the_checkout(self):
        result = self._acquire(SLOT_MOUNT_DIRS="../outside")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("SLOT_MOUNT_DIRS", result.stderr.decode())

    def test_an_overlay_inherited_from_the_environment_must_be_variable_equals_file(self):
        result = self._acquire(SLOT_OVERLAYS="no-equals-sign")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("SLOT_OVERLAYS", result.stderr.decode())


class ForbiddenPortInABlock(DevContainerTestCase):

    def test_a_slot_number_whose_block_holds_a_forbidden_port_is_skipped_before_any_clone(self):
        self.harness.build_fixture_repo(
            with_parent_env=IDENTITY_ENV,
            project_env=project_env_text(SLOT_FORBIDDEN_PORTS=str(slot_port(1, 1))),
        )

        result = self.harness.run_acquire()

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(self.harness.acquired_slot_name(result), "slot-2")
        self.assertFalse(os.path.exists(self.harness.slot_dir("slot-1")), "slot-1 was cloned")


if __name__ == "__main__":
    unittest.main()
