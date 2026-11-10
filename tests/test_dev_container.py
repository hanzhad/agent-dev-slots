
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
)


class GitIdentity(DevContainerTestCase):
    def test_acquire_sets_the_checkouts_local_identity_from_its_own_env(self):
        self.harness.build_fixture_repo(
            with_parent_env="GIT_USER_NAME=demo-agent\nGIT_USER_EMAIL=demo-agent@example.com\n"
        )

        result = self.harness.run_acquire()

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        name = self.harness.acquired_slot_name(result)
        self.assertEqual(self.harness.checkout_identity(name), "demo-agent")
        email = subprocess.run(
            ["git", "-C", self.harness.slot_dir(name), "config", "--local", "user.email"],
            stdout=subprocess.PIPE,
        ).stdout.decode().strip()
        self.assertEqual(email, "demo-agent@example.com")

    def test_acquire_syncs_the_checkout_to_whatever_env_says_even_the_owners_name(self):
        self.harness.build_fixture_repo(
            with_parent_env="GIT_USER_NAME=demo-owner\nGIT_USER_EMAIL=demo-owner@example.com\n"
        )

        result = self.harness.run_acquire()

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        name = self.harness.acquired_slot_name(result)
        self.assertEqual(self.harness.checkout_identity(name), "demo-owner")

    def test_acquire_refuses_before_the_docs_clone_when_env_names_no_git_user_name(self):
        self.harness.build_fixture_repo()

        result = self.harness.run_acquire()

        self.assertNotEqual(result.returncode, 0)
        stderr = result.stderr.decode()
        self.assertIn("GIT_USER_NAME", stderr)
        self.assertIn("which would commit (and push) as the owner", stderr)
        slot_dir = self.harness.slot_dir("slot-1")
        self.assertTrue(os.path.isdir(os.path.join(slot_dir, ".git")))
        self.assertFalse(os.path.exists(os.path.join(slot_dir, "docs")))
        self.assertFalse(os.path.exists(os.path.join(slot_dir, ".local", "archive")))
        self.assertFalse(os.path.isfile(self.harness.lease_path("slot-1")))

    def test_restart_repairs_a_checkout_whose_local_identity_has_drifted(self):
        self.harness.build_fixture_repo()
        self.harness.make_bare_slot(
            "probe",
            env_content="GIT_USER_NAME=demo-agent\nGIT_USER_EMAIL=demo-agent@example.com\n",
            ports_content="NOVNC_PORT=39230\nSSH_PORT=39220\nVITE_DEV_PORT=39240\n",
            local_identity=("demo-owner", "demo-owner@example.com"),
        )
        self.assertEqual(self.harness.checkout_identity("probe"), "demo-owner")

        result = self.harness.run("restart", "probe")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(self.harness.checkout_identity("probe"), "demo-agent")

    def test_restart_repairs_a_checkout_with_no_local_identity_at_all(self):
        self.harness.build_fixture_repo()
        self.harness.make_bare_slot(
            "probe",
            env_content="GIT_USER_NAME=demo-agent\nGIT_USER_EMAIL=demo-agent@example.com\n",
            ports_content="NOVNC_PORT=39230\nSSH_PORT=39220\nVITE_DEV_PORT=39240\n",
        )
        self.assertEqual(self.harness.checkout_identity("probe"), "")

        result = self.harness.run("restart", "probe")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(self.harness.checkout_identity("probe"), "demo-agent")

    def test_restart_refuses_when_env_names_no_git_user_name(self):
        self.harness.build_fixture_repo()
        self.harness.make_bare_slot(
            "probe",
            env_content="GIT_USER_EMAIL=demo-agent@example.com\n",
            ports_content="NOVNC_PORT=39230\nSSH_PORT=39220\nVITE_DEV_PORT=39240\n",
            local_identity=("demo-owner", "demo-owner@example.com"),
        )

        result = self.harness.run("restart", "probe")

        self.assertNotEqual(result.returncode, 0)
        stderr = result.stderr.decode()
        self.assertIn("GIT_USER_NAME", stderr)
        self.assertIn("which would commit (and push) as the owner", stderr)
        self.assertEqual(self.harness.checkout_identity("probe"), "demo-owner")


    @staticmethod
    def _local_config(dir_, key):
        result = subprocess.run(
            ["git", "-C", dir_, "config", "--local", "--get", key],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        return result.stdout.decode().strip()

    def test_list_shows_each_slots_actual_checkout_identity(self):
        self.harness.build_fixture_repo()
        self.harness.make_bare_slot(
            "synced",
            env_content="GIT_USER_NAME=demo-agent\nGIT_USER_EMAIL=demo-agent@example.com\n",
            ports_content="NOVNC_PORT=39230\nSSH_PORT=39220\nVITE_DEV_PORT=39240\n",
            local_identity=("demo-agent", "demo-agent@example.com"),
        )
        self.harness.make_bare_slot(
            "drifted",
            env_content="GIT_USER_NAME=demo-agent\nGIT_USER_EMAIL=demo-agent@example.com\n",
            ports_content="NOVNC_PORT=39231\nSSH_PORT=39221\nVITE_DEV_PORT=39241\n",
            local_identity=("demo-owner", "demo-owner@example.com"),
        )

        result = self.harness.run("list")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        stdout = result.stdout.decode()
        self.assertEqual(self._row(stdout, "synced")[9], "demo-agent")
        self.assertEqual(self._row(stdout, "drifted")[9], "demo-owner")

    def test_list_brief_skips_the_dirty_column(self):
        self.harness.build_fixture_repo()
        self.harness.make_bare_slot(
            "quiet",
            env_content="GIT_USER_NAME=demo-agent\nGIT_USER_EMAIL=demo-agent@example.com\n",
            ports_content="NOVNC_PORT=39232\nSSH_PORT=39222\nVITE_DEV_PORT=39242\n",
            local_identity=("demo-agent", "demo-agent@example.com"),
        )

        full = self.harness.run("list")
        brief = self.harness.run("list", "--brief")

        self.assertEqual(brief.returncode, 0, brief.stderr.decode())
        self.assertNotEqual(self._row(full.stdout.decode(), "quiet")[10], "-")
        self.assertEqual(self._row(brief.stdout.decode(), "quiet")[10], "-")
        self.assertIn("--brief", brief.stdout.decode().splitlines()[0])

    def test_list_rejects_an_unknown_argument(self):
        self.harness.build_fixture_repo()

        result = self.harness.run("list", "--everything")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--everything", result.stderr.decode())

    @staticmethod
    def _row(stdout, slot_name):
        for line in stdout.splitlines():
            fields = line.split()
            if fields and fields[0] == slot_name:
                return fields
        raise AssertionError(f"no '{slot_name}' row in:\n{stdout}")


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


class LeaseVerbs(DevContainerTestCase):
    ENV_WITH_IDENTITY = "GIT_USER_NAME=demo-agent\nGIT_USER_EMAIL=demo-agent@example.com\n"

    def test_acquire_writes_a_lease_and_release_removes_it(self):
        self.harness.build_fixture_repo(with_parent_env=self.ENV_WITH_IDENTITY)

        result = self.harness.run_acquire()
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        name = self.harness.acquired_slot_name(result)
        self.assertTrue(os.path.isfile(self.harness.lease_path(name)), result.stdout.decode())

        result = self.harness.run("release", name, "--for", "test")
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertFalse(os.path.isfile(self.harness.lease_path(name)))
        self.assertFalse(os.path.isdir(self.harness.slot_dir(name)), "release left the slot directory")

    def test_acquire_sets_ssh_public_key_in_env_and_release_removes_the_key_with_the_slot(self):
        self.harness.build_fixture_repo(with_parent_env=self.ENV_WITH_IDENTITY)

        result = self.harness.run_acquire()
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        name = self.harness.acquired_slot_name(result)
        key = self.harness.read_env(name)["SSH_PUBLIC_KEY"]
        self.assertTrue(key.startswith("ssh-ed25519 "), key)
        with open(self.harness.lease_path(name), encoding="utf-8") as handle:
            key_path = json.load(handle)["key"]
        self.assertTrue(os.path.isfile(key_path))

        result = self.harness.run("release", name, "--for", "test")
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertFalse(os.path.exists(key_path), "the private key of the lease outlived the release")
        self.assertFalse(os.path.exists(key_path + ".pub"), "the public key of the lease outlived the release")
        self.assertFalse(os.path.isdir(self.harness.slot_dir(name)), "release left the slot directory")


    def test_acquire_as_records_the_owner_in_the_lease_and_in_list(self):
        self.harness.build_fixture_repo(with_parent_env=self.ENV_WITH_IDENTITY)
        owner = "Sesión de compilación — vérification [a1b2c3]"

        result = self.harness.run_acquire(owner=owner)

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        name = self.harness.acquired_slot_name(result)
        with open(self.harness.lease_path(name), encoding="utf-8") as handle:
            lease = json.load(handle)
        self.assertEqual(lease["owner"], owner)
        listing = self.harness.run("list", "--brief")
        self.assertEqual(listing.returncode, 0, listing.stderr.decode())
        row = [line for line in listing.stdout.decode().splitlines() if line.startswith(name)]
        self.assertTrue(row, listing.stdout.decode())
        self.assertIn(owner, row[0], row[0])

    def test_acquire_without_as_records_a_dash_for_owner(self):
        self.harness.build_fixture_repo(with_parent_env=self.ENV_WITH_IDENTITY)

        result = self.harness.run_acquire()

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        name = self.harness.acquired_slot_name(result)
        with open(self.harness.lease_path(name), encoding="utf-8") as handle:
            lease = json.load(handle)
        self.assertEqual(lease["owner"], "-")
        listing = self.harness.run("list", "--brief")
        self.assertEqual(listing.returncode, 0, listing.stderr.decode())
        self.assertEqual(self._row(listing.stdout.decode(), name)[4], "-")

    def test_list_reads_a_lease_written_before_owner_existed_as_a_dash(self):
        self.harness.build_fixture_repo(with_parent_env=self.ENV_WITH_IDENTITY)
        self.harness.make_bare_slot(
            "legacy",
            env_content=self.ENV_WITH_IDENTITY,
            ports_content="NOVNC_PORT=39234\nSSH_PORT=39224\nVITE_DEV_PORT=39244\n",
        )
        path = self.harness.lease_path("legacy")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "holder": "task-5",
                    "project": PROJECT_PREFIX + "-legacy",
                    "since": int(time.time()),
                    "ttl_seconds": 14400,
                    "key": "/dev/null",
                    "ssh_port": 39224,
                },
                handle,
            )

        result = self.harness.run("list", "--brief")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(self._row(result.stdout.decode(), "legacy")[4], "-")

    @staticmethod
    def _row(stdout, slot_name):
        for line in stdout.splitlines():
            fields = line.split()
            if fields and fields[0] == slot_name:
                return fields
        raise AssertionError(f"no '{slot_name}' row in:\n{stdout}")


class SessionSlotLimit(DevContainerTestCase):
    ENV_WITH_IDENTITY = LeaseVerbs.ENV_WITH_IDENTITY

    def write_limits(self, limits):
        path = os.path.join(self.harness.state_dir, "limits.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(limits, handle)

    def acquire_ok(self, holder, owner):
        result = self.harness.run_acquire(holder=holder, owner=owner)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        return self.harness.acquired_slot_name(result)

    def test_no_limits_file_means_no_limit(self):
        self.harness.build_fixture_repo(with_parent_env=self.ENV_WITH_IDENTITY)

        self.acquire_ok("first", "Agent [aaa]")
        self.acquire_ok("second", "Agent [aaa]")

    def test_limits_file_without_as_is_refused(self):
        self.harness.build_fixture_repo(with_parent_env=self.ENV_WITH_IDENTITY)
        self.write_limits({"default": 1})

        result = self.harness.run_acquire()
        self.assertEqual(result.returncode, 4, result.stderr.decode())
        self.assertIn("--as", result.stderr.decode())
        self.assertIn("[<ref>]", result.stderr.decode())

        result = self.harness.run_acquire(owner="no bracketed ref")
        self.assertEqual(result.returncode, 4, result.stderr.decode())

    def test_session_at_default_limit_is_refused_naming_the_held_slot(self):
        self.harness.build_fixture_repo(with_parent_env=self.ENV_WITH_IDENTITY)
        self.write_limits({"default": 1})
        held = self.acquire_ok("first", "Agent [aaa]")

        result = self.harness.run_acquire(holder="second", owner="Agent [aaa]")
        self.assertEqual(result.returncode, 4, result.stderr.decode())
        self.assertIn(held, result.stderr.decode())
        self.assertIn("limits.json", result.stderr.decode())

    def test_wait_does_not_wait_out_the_limit(self):
        self.harness.build_fixture_repo(with_parent_env=self.ENV_WITH_IDENTITY)
        self.write_limits({"default": 1})
        self.acquire_ok("first", "Agent [aaa]")

        result = self.harness.run(
            "acquire", "--for", "second", "--as", "Agent [aaa]", "--wait", "--timeout", "30s"
        )
        self.assertEqual(result.returncode, 4, result.stderr.decode())

    def test_per_session_override_lets_a_second_slot_through(self):
        self.harness.build_fixture_repo(with_parent_env=self.ENV_WITH_IDENTITY)
        self.write_limits({"default": 1, "sessions": {"aaa": 2}})

        first = self.acquire_ok("first", "Agent [aaa]")
        second = self.acquire_ok("second", "Agent [aaa]")
        self.assertNotEqual(first, second)

        result = self.harness.run_acquire(holder="third", owner="Agent [aaa]")
        self.assertEqual(result.returncode, 4, result.stderr.decode())

    def test_a_lease_owned_by_another_ref_does_not_count(self):
        self.harness.build_fixture_repo(with_parent_env=self.ENV_WITH_IDENTITY)
        self.write_limits({"default": 1})
        self.acquire_ok("first", "Other [bbb]")

        self.acquire_ok("second", "Agent [aaa]")

    def test_session_id_in_the_environment_keys_the_limit_whatever_ref_as_carries(self):
        self.harness.build_fixture_repo(with_parent_env=self.ENV_WITH_IDENTITY)
        self.harness.session_id = "local_x"
        self.write_limits({"default": 1})
        self.acquire_ok("first", "Agent [aaa]")

        result = self.harness.run_acquire(holder="second", owner="Agent [bbb]")
        self.assertEqual(result.returncode, 4, result.stderr.decode())
        self.assertIn("local_x", result.stderr.decode())

    def test_session_id_in_the_environment_selects_the_per_session_override(self):
        self.harness.build_fixture_repo(with_parent_env=self.ENV_WITH_IDENTITY)
        self.harness.session_id = "local_x"
        self.write_limits({"default": 1, "sessions": {"local_x": 2}})

        first = self.acquire_ok("first", "Agent [aaa]")
        second = self.acquire_ok("second", "Agent [aaa]")
        self.assertNotEqual(first, second)

    def test_session_id_in_the_environment_replaces_a_missing_as(self):
        self.harness.build_fixture_repo(with_parent_env=self.ENV_WITH_IDENTITY)
        self.harness.session_id = "local_x"
        self.write_limits({"default": 1})

        result = self.harness.run_acquire(holder="first")
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        name = self.harness.acquired_slot_name(result)
        with open(self.harness.lease_path(name), encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["owner"], "session [local_x]")

        result = self.harness.run_acquire(holder="second")
        self.assertEqual(result.returncode, 4, result.stderr.decode())


class ReleaseRefusesWithoutTheMatchingHolder(DevContainerTestCase):
    ENV_WITH_IDENTITY = LeaseVerbs.ENV_WITH_IDENTITY

    def _acquire(self, holder, owner=None):
        self.harness.build_fixture_repo(with_parent_env=self.ENV_WITH_IDENTITY)
        result = self.harness.run_acquire(holder=holder, owner=owner)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        return self.harness.acquired_slot_name(result)

    def _lease_snapshot(self, name):
        with open(self.harness.lease_path(name), encoding="utf-8") as handle:
            return json.load(handle)

    def test_release_with_no_for_refuses_and_touches_nothing(self):
        name = self._acquire(holder="task-6-holder", owner="Session A [aaaa]")
        before = self._lease_snapshot(name)
        key_path = before["key"]
        self.assertTrue(os.path.exists(key_path), "fixture's own key is missing before the test even runs")
        pub_key_before = self.harness.read_env(name)["SSH_PUBLIC_KEY"]

        result = self.harness.run("release", name)

        self.assertNotEqual(result.returncode, 0, result.stdout.decode())
        stderr = result.stderr.decode()
        self.assertIn("task-6-holder", stderr)
        self.assertIn("Session A [aaaa]", stderr)
        self.assertIn("evict", stderr)
        self.assertTrue(os.path.isfile(self.harness.lease_path(name)), "the lease was removed by a refused release")
        self.assertEqual(self._lease_snapshot(name), before, "the lease was rewritten by a refused release")
        self.assertTrue(os.path.exists(key_path), "the holder's own private key was deleted by a refused release")
        self.assertEqual(
            self.harness.read_env(name)["SSH_PUBLIC_KEY"], pub_key_before,
            "authorized_keys was blanked by a refused release",
        )

    def test_release_with_the_wrong_for_refuses_and_touches_nothing(self):
        name = self._acquire(holder="task-6-holder", owner="Session A [aaaa]")
        before = self._lease_snapshot(name)
        key_path = before["key"]
        pub_key_before = self.harness.read_env(name)["SSH_PUBLIC_KEY"]

        result = self.harness.run("release", name, "--for", "somebody-elses-issue")

        self.assertNotEqual(result.returncode, 0, result.stdout.decode())
        stderr = result.stderr.decode()
        self.assertIn("task-6-holder", stderr)
        self.assertIn("Session A [aaaa]", stderr)
        self.assertIn("somebody-elses-issue", stderr)
        self.assertIn("evict", stderr)
        self.assertTrue(os.path.isfile(self.harness.lease_path(name)))
        self.assertEqual(self._lease_snapshot(name), before)
        self.assertTrue(os.path.exists(key_path))
        self.assertEqual(self.harness.read_env(name)["SSH_PUBLIC_KEY"], pub_key_before)

    def test_release_with_no_lease_refuses_and_touches_nothing(self):
        self.harness.build_fixture_repo(with_parent_env=self.ENV_WITH_IDENTITY)
        self.harness.make_bare_slot("probe", env_content=self.ENV_WITH_IDENTITY)

        result = self.harness.run("release", "probe", "--for", "anyone")

        self.assertNotEqual(result.returncode, 0, result.stdout.decode())
        self.assertIn("no lease", result.stderr.decode())
        self.assertFalse(os.path.isfile(self.harness.lease_path("probe")))

    def test_release_with_the_matching_for_releases_as_before(self):
        name = self._acquire(holder="task-6-holder")

        result = self.harness.run("release", name, "--for", "task-6-holder")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertFalse(os.path.isfile(self.harness.lease_path(name)))
        self.assertFalse(os.path.isdir(self.harness.slot_dir(name)), "release left the slot directory")


class SlotHolderFile(DevContainerTestCase):
    ENV_WITH_IDENTITY = LeaseVerbs.ENV_WITH_IDENTITY

    def _slot_with_real_remote(self, name):
        self.harness.build_fixture_repo(with_parent_env=self.ENV_WITH_IDENTITY)
        self.harness.make_bare_slot(name, env_content=self.ENV_WITH_IDENTITY)
        slot = self.harness.slot_dir(name)
        git = lambda *args: subprocess.run(
            [self.harness.real_git_bin, "-C", slot, "-c", "commit.gpgsign=false", *args],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        git("config", "--local", "user.email", "fixture@example.com")
        git("config", "--local", "user.name", "fixture")
        remote = os.path.join(self.harness.tmp, name + "-remote.git")
        subprocess.run(
            ["git", "init", "--bare", "-q", remote],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        git("remote", "set-url", "origin", remote)
        subprocess.run(
            ["git", "-C", self.harness.fixture_repo, "remote", "set-url", "origin", remote],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        with open(os.path.join(slot, "README.md"), "w", encoding="utf-8") as handle:
            handle.write("slot fixture\n")
        with open(os.path.join(slot, ".gitignore"), "w", encoding="utf-8") as handle:
            handle.write(".devcontainer/.env\n.devcontainer/.slot-holder\ndocs/\n")
        git("add", "README.md", ".gitignore")
        git("commit", "-q", "-m", "base")
        git("branch", "-M", "master")
        git("push", "-q", "origin", "master")
        git("fetch", "-q", "origin")
        return slot

    def _write_lease(self, name, holder, age_seconds=60, ttl=14400):
        path = self.harness.lease_path(name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "holder": holder,
                    "project": PROJECT_PREFIX + "-" + name,
                    "since": int(time.time()) - age_seconds,
                    "ttl_seconds": ttl,
                    "key": "/dev/null",
                    "ssh_port": 39229,
                    "owner": "-",
                },
                handle,
            )

    def _write_holder_stub(self, name, holder):
        path = self.harness.holder_path(name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(
                f"holder={holder}\nowner=-\nslot_id={PROJECT_PREFIX}-{name}\n"
                "since_epoch=0\nsince_iso=1970-01-01T00:00:00Z\n"
            )

    def test_acquire_writes_the_holder_file_matching_the_lease(self):
        self.harness.build_fixture_repo(with_parent_env=self.ENV_WITH_IDENTITY)

        result = self.harness.run_acquire(holder="task-7", owner="Session A [aaaa]")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        name = self.harness.acquired_slot_name(result)
        self.assertTrue(os.path.isfile(self.harness.holder_path(name)))
        with open(self.harness.lease_path(name), encoding="utf-8") as handle:
            lease = json.load(handle)
        holder = self.harness.read_holder(name)
        self.assertEqual(holder["holder"], "task-7")
        self.assertEqual(holder["owner"], "Session A [aaaa]")
        self.assertEqual(holder["slot_id"], f"{PROJECT_PREFIX}-{name}")
        self.assertEqual(int(holder["since_epoch"]), lease["since"])
        self.assertNotIn("expiry_epoch", holder)
        self.assertNotIn("ttl_seconds", lease)

    def test_release_removes_the_holder_file(self):
        self.harness.build_fixture_repo(with_parent_env=self.ENV_WITH_IDENTITY)
        result = self.harness.run_acquire()
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        name = self.harness.acquired_slot_name(result)
        self.assertTrue(os.path.isfile(self.harness.holder_path(name)))

        release = self.harness.run("release", name, "--for", "test")

        self.assertEqual(release.returncode, 0, release.stderr.decode())
        self.assertFalse(os.path.isfile(self.harness.holder_path(name)))
        self.assertFalse(os.path.isdir(self.harness.slot_dir(name)), "the holder file went but the slot stayed")

    def test_evict_removes_the_holder_file(self):
        name = "evicted"
        self._slot_with_real_remote(name)
        self._write_lease(name, "task-7")
        self._write_holder_stub(name, "task-7")

        result = self.harness.run("evict", name, "--yes", "--force")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertFalse(os.path.isfile(self.harness.holder_path(name)))


    def test_acquire_on_a_checkout_predating_the_gitignore_line_still_reuse_cleans(self):
        self.harness.build_fixture_repo(with_parent_env=self.ENV_WITH_IDENTITY)
        gitignore_path = os.path.join(self.harness.fixture_repo, ".gitignore")
        with open(gitignore_path, encoding="utf-8") as handle:
            content = handle.read()
        self.assertIn(".slot-holder", content, "fixture's own .gitignore is missing the line this test removes")
        stale = content.replace(".slot-holder\n", "")
        self.assertNotIn(".slot-holder", stale)
        with open(gitignore_path, "w", encoding="utf-8") as handle:
            handle.write(stale)
        subprocess.run(
            ["git", "-C", self.harness.fixture_repo, "commit", "-q", "-am", "earlier .gitignore"],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )

        result = self.harness.run_acquire(holder="task-7-old-checkout")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        name = self.harness.acquired_slot_name(result)
        slot = self.harness.slot_dir(name)
        status = subprocess.run(
            ["git", "-C", slot, "status", "--porcelain"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
        ).stdout.decode()
        self.assertEqual(
            status, "", "the holder file reads as untracked work on a stale-.gitignore checkout"
        )

        release = self.harness.run("release", name, "--for", "task-7-old-checkout")

        self.assertEqual(release.returncode, 0, release.stderr.decode())


class ReleaseStopsTheSlotsContainers(DevContainerTestCase):
    ENV_WITH_IDENTITY = LeaseVerbs.ENV_WITH_IDENTITY
    _slot_with_real_remote = SlotHolderFile._slot_with_real_remote
    _write_lease = SlotHolderFile._write_lease

    def _running(self, name):
        return os.path.isfile(
            os.path.join(self.harness.docker_state, f"{PROJECT_PREFIX}-{name}.running")
        )

    def _docker_log(self):
        with open(self.harness.docker_log, encoding="utf-8") as handle:
            return handle.read()

    def _acquire(self):
        acquired = self.harness.run_acquire()
        self.assertEqual(acquired.returncode, 0, acquired.stderr.decode())
        name = self.harness.acquired_slot_name(acquired)
        self.assertTrue(self._running(name))
        return name

    def test_release_takes_the_compose_project_down_with_its_volumes_and_removes_the_slot(self):
        self.harness.build_fixture_repo(with_parent_env=self.ENV_WITH_IDENTITY)
        name = self._acquire()

        result = self.harness.run("release", name, "--for", "test")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertFalse(self._running(name), result.stdout.decode())
        self.assertRegex(self._docker_log(), rf"-p {PROJECT_PREFIX}-{name} .*down -v")
        self.assertFalse(os.path.isdir(self.harness.slot_dir(name)))
        self.assertFalse(os.path.isfile(self.harness.lease_path(name)))
        self.assertIn("removed", result.stdout.decode())

    def test_evict_stops_them(self):
        name = "evicted"
        slot = self._slot_with_real_remote(name)
        for args in (
            ["add", ".devcontainer/docker-compose.yml"],
            ["commit", "-q", "-m", "compose file"],
            ["push", "-q", "origin", "master"],
        ):
            subprocess.run(
                [self.harness.real_git_bin, "-C", slot, "-c", "commit.gpgsign=false", *args],
                check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
        self._write_lease(name, "task-8")
        self.harness.mark_running(name)

        result = self.harness.run("evict", name, "--yes", "--force")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertFalse(self._running(name), result.stdout.decode())
        self.assertTrue(os.path.isdir(self.harness.slot_dir(name)))
        self.assertFalse(os.path.isfile(self.harness.lease_path(name)))


class ForRefusesADuration(DevContainerTestCase):
    ENV_WITH_IDENTITY = LeaseVerbs.ENV_WITH_IDENTITY

    @staticmethod
    def _lease(path):
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)

    def test_acquire_for_4h_refuses_and_writes_no_lease(self):
        self.harness.build_fixture_repo()

        result = self.harness.run_acquire(holder="4h")

        self.assertNotEqual(result.returncode, 0)
        stderr = result.stderr.decode()
        self.assertIn("no term", stderr)
        self.assertIn("4h", stderr)
        self.assertFalse(os.path.exists(self.harness.slot_dir("slot-1")))
        self.assertFalse(os.path.isfile(self.harness.lease_path("slot-1")))


    def test_acquire_for_values_that_are_not_a_duration_are_accepted(self):
        self.harness.build_fixture_repo(with_parent_env=self.ENV_WITH_IDENTITY)
        for holder in ("task-1-details-demo", "manual", "-", "4hours", "h4"):
            with self.subTest(holder=holder):
                result = self.harness.run_acquire(holder=holder)
                self.assertEqual(result.returncode, 0, result.stderr.decode())
                self.assertNotIn("no term", result.stderr.decode())
                name = self.harness.acquired_slot_name(result)
                self.assertEqual(self._lease(self.harness.lease_path(name))["holder"], holder)



    def test_acquire_for_with_whitespace_refuses_and_writes_no_lease(self):
        self.harness.build_fixture_repo()

        result = self.harness.run_acquire(holder="task a b")

        self.assertNotEqual(result.returncode, 0)
        stderr = result.stderr.decode()
        self.assertIn("whitespace", stderr)
        self.assertFalse(os.path.exists(self.harness.slot_dir("slot-1")))
        self.assertFalse(os.path.isfile(self.harness.lease_path("slot-1")))



class ParkAVerbForGivingBackADirtySlot(DevContainerTestCase):
    ENV_WITH_IDENTITY = LeaseVerbs.ENV_WITH_IDENTITY

    @staticmethod
    def _dirty(slot_dir):
        with open(os.path.join(slot_dir, "scratch.txt"), "w", encoding="utf-8") as handle:
            handle.write("uncommitted, never staged\n")

    def test_park_removes_the_lease_and_writes_a_marker(self):
        self.harness.build_fixture_repo(with_parent_env=self.ENV_WITH_IDENTITY)
        result = self.harness.run_acquire(holder="task-11")
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        name = self.harness.acquired_slot_name(result)
        self._dirty(self.harness.slot_dir(name))

        park = self.harness.run("park", name)

        self.assertEqual(park.returncode, 0, park.stderr.decode())
        self.assertFalse(os.path.isfile(self.harness.lease_path(name)), "lease survived park")
        marker_path = self.harness.parked_path(name)
        self.assertTrue(os.path.isfile(marker_path), park.stdout.decode())
        with open(marker_path, encoding="utf-8") as handle:
            marker = json.load(handle)
        self.assertEqual(marker["project"], f"{PROJECT_PREFIX}-{name}")
        self.assertEqual(marker["holder"], "task-11")
        self.assertIn("uncommitted", marker["reason"])
        self.assertIsInstance(marker["since"], int)

    def test_park_blanks_the_ssh_key_same_as_release(self):
        self.harness.build_fixture_repo(with_parent_env=self.ENV_WITH_IDENTITY)
        result = self.harness.run_acquire()
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        name = self.harness.acquired_slot_name(result)
        self._dirty(self.harness.slot_dir(name))

        park = self.harness.run("park", name)

        self.assertEqual(park.returncode, 0, park.stderr.decode())
        self.assertEqual(self.harness.read_env(name)["SSH_PUBLIC_KEY"], "")

    def test_park_on_a_clean_slot_releases_instead_and_says_so(self):
        self.harness.build_fixture_repo(with_parent_env=self.ENV_WITH_IDENTITY)
        result = self.harness.run_acquire()
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        name = self.harness.acquired_slot_name(result)

        park = self.harness.run("park", name)

        self.assertEqual(park.returncode, 0, park.stderr.decode())
        self.assertFalse(os.path.isfile(self.harness.lease_path(name)))
        self.assertFalse(os.path.isfile(self.harness.parked_path(name)), "clean slot got a park marker")
        stdout = park.stdout.decode()
        self.assertIn("clean", stdout)
        self.assertIn("release", stdout)

    def test_park_refuses_a_slot_with_no_lease(self):
        self.harness.build_fixture_repo(with_parent_env=self.ENV_WITH_IDENTITY)
        self.harness.make_bare_slot("probe", env_content=self.ENV_WITH_IDENTITY)

        park = self.harness.run("park", "probe")

        self.assertNotEqual(park.returncode, 0, park.stdout.decode())
        self.assertIn("no lease", park.stderr.decode())
        self.assertFalse(os.path.isfile(self.harness.parked_path("probe")))

    def test_park_refuses_a_slot_that_is_not_this_projects_own(self):
        self.harness.build_fixture_repo(with_parent_env=self.ENV_WITH_IDENTITY)
        self.harness.make_bare_slot("probe", env_content=self.ENV_WITH_IDENTITY)
        subprocess.run(
            [
                "git", "-C", self.harness.slot_dir("probe"), "remote", "set-url", "origin",
                "https://example.invalid/somebody-elses.git",
            ],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )

        park = self.harness.run("park", "probe")

        self.assertNotEqual(park.returncode, 0, park.stdout.decode())
        self.assertIn("not this project's own slot", park.stderr.decode())
        self.assertFalse(os.path.isfile(self.harness.parked_path("probe")))


    def test_park_clear_removes_the_marker_without_cleaning_the_slot(self):
        self.harness.build_fixture_repo(with_parent_env=self.ENV_WITH_IDENTITY)
        result = self.harness.run_acquire()
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        name = self.harness.acquired_slot_name(result)
        slot_dir = self.harness.slot_dir(name)
        self._dirty(slot_dir)
        park = self.harness.run("park", name)
        self.assertEqual(park.returncode, 0, park.stderr.decode())
        self.assertTrue(os.path.isfile(self.harness.parked_path(name)))

        clear = self.harness.run("park", name, "--clear")

        self.assertEqual(clear.returncode, 0, clear.stderr.decode())
        self.assertFalse(os.path.isfile(self.harness.parked_path(name)))
        self.assertTrue(os.path.isfile(os.path.join(slot_dir, "scratch.txt")))
        self.assertFalse(
            os.path.isfile(self.harness.lease_path(name)), "clearing a park must not re-lease the slot"
        )

    def test_park_clear_refuses_when_nothing_is_parked(self):
        self.harness.build_fixture_repo(with_parent_env=self.ENV_WITH_IDENTITY)
        self.harness.make_bare_slot("probe", env_content=self.ENV_WITH_IDENTITY)

        clear = self.harness.run("park", "probe", "--clear")

        self.assertNotEqual(clear.returncode, 0, clear.stdout.decode())
        self.assertIn("nothing to clear", clear.stderr.decode())

    def test_list_shows_parked_as_its_own_state_with_the_reason(self):
        self.harness.build_fixture_repo(with_parent_env=self.ENV_WITH_IDENTITY)
        result = self.harness.run_acquire(holder="task-11")
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        name = self.harness.acquired_slot_name(result)
        self._dirty(self.harness.slot_dir(name))
        park = self.harness.run("park", name)
        self.assertEqual(park.returncode, 0, park.stderr.decode())

        listing = self.harness.run("list")

        self.assertEqual(listing.returncode, 0, listing.stderr.decode())
        row = [line for line in listing.stdout.decode().splitlines() if line.startswith(name)]
        self.assertTrue(row, listing.stdout.decode())
        fields = row[0].split()
        self.assertEqual(fields[2], "parked")
        self.assertNotIn("free", fields)
        self.assertIn("task-11", row[0])
        self.assertIn("uncommitted", row[0])


class SlotsTreeResolution(DevContainerTestCase):

    def _worktree(self, relative):
        self.harness.build_fixture_repo()
        repo = self.harness.fixture_repo
        path = os.path.join(repo, relative)
        subprocess.run(
            [self.harness.real_git_bin, "-C", repo, "worktree", "add", "-q", "-b", "wt", path],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        script = os.path.join(path, SCRIPT_REL)
        os.makedirs(os.path.dirname(script), exist_ok=True)
        shutil.copyfile(os.path.join(repo, SCRIPT_REL), script)
        shutil.copytree(os.path.join(repo, LIB_REL), os.path.join(path, LIB_REL), dirs_exist_ok=True)
        return script

    def _run_from(self, script, caller_pwd, env_extra=None):
        env = dict(os.environ)
        env["PATH"] = self.harness.stub_bin + os.pathsep + env.get("PATH", "")
        env["DOCKER_STUB_LOG"] = self.harness.docker_log
        env["DOCKER_STUB_STATE"] = self.harness.docker_state
        env["REAL_GIT_BIN"] = self.harness.real_git_bin
        env["DOCS_REPO_STUB_PATH"] = self.harness.docs_repo_stub
        env["DOCS_REPO_URL"] = self.harness.docs_repo_stub
        env["DEV_CONTAINER_HOME"] = self.harness.state_dir
        env["DEV_CONTAINER_PROJECTS_ROOT"] = self.harness.tmp
        env["DEV_CONTAINER_CALLER_PWD"] = caller_pwd
        env.update(env_extra or {})
        return subprocess.run(
            ["bash", script, "list", "--brief"],
            cwd=self.harness.tmp,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=120,
        )

    def test_a_worktree_outside_claude_uses_the_main_checkouts_slots_tree(self):
        script = self._worktree(os.path.join("..", "sibling-worktrees", "wt"))
        self.harness.make_bare_slot(
            "shared",
            env_content="GIT_USER_NAME=demo-agent\nGIT_USER_EMAIL=demo-agent@example.com\n",
            ports_content="NOVNC_PORT=39233\nSSH_PORT=39223\nVITE_DEV_PORT=39243\n",
            local_identity=("demo-agent", "demo-agent@example.com"),
        )

        result = self._run_from(script, os.path.dirname(os.path.dirname(script)))

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertIn("shared", result.stdout.decode())

    def test_a_claude_worktree_as_the_caller_uses_the_main_checkouts_slots_tree(self):
        # The engine no longer lives in the worktree, so there is no refusal to
        # make by name: the directory it is given resolves to the main checkout.
        script = self._worktree(os.path.join(".claude", "worktrees", "some-task"))
        self.harness.make_bare_slot(
            "shared",
            env_content="GIT_USER_NAME=demo-agent\nGIT_USER_EMAIL=demo-agent@example.com\n",
            ports_content="NOVNC_PORT=39234\nSSH_PORT=39224\nVITE_DEV_PORT=39244\n",
            local_identity=("demo-agent", "demo-agent@example.com"),
        )

        result = self._run_from(script, os.path.dirname(os.path.dirname(script)))

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertIn("shared", result.stdout.decode())


class HalfBuiltSlot(DevContainerTestCase):

    ENV = "GIT_USER_NAME=demo-agent\nGIT_USER_EMAIL=demo-agent@example.com\n"
    PORTS = "NOVNC_PORT=39235\nSSH_PORT=39225\nVITE_DEV_PORT=39245\n"


    def test_list_calls_it_unbuilt_rather_than_free(self):
        self.harness.build_fixture_repo()
        self.harness.make_bare_slot(
            "halfway",
            env_content=self.ENV,
            local_identity=("demo-agent", "demo-agent@example.com"),
            built=False,
        )

        result = self.harness.run("list")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(self._row(result.stdout.decode(), "halfway")[2], "unbuilt")

    def test_a_slot_with_no_docs_repo_at_all_is_not_called_docs_drift(self):
        self.harness.build_fixture_repo()
        self.harness.make_bare_slot(
            "halfway",
            env_content=self.ENV,
            local_identity=("demo-agent", "demo-agent@example.com"),
            built=False,
        )

        result = self.harness.run("list")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertNotEqual(self._row(result.stdout.decode(), "halfway")[2], "docs-drift")

    def test_a_slot_that_has_everything_is_not_called_unbuilt(self):
        self.harness.build_fixture_repo()
        self.harness.make_bare_slot(
            "complete",
            env_content=self.ENV,
            ports_content=self.PORTS,
            local_identity=("demo-agent", "demo-agent@example.com"),
        )

        result = self.harness.run("list")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertNotEqual(self._row(result.stdout.decode(), "complete")[2], "unbuilt")

    @staticmethod
    def _row(stdout, slot_name):
        for line in stdout.splitlines():
            fields = line.split()
            if fields and fields[0] == slot_name:
                return fields
        raise AssertionError(f"no '{slot_name}' row in:\n{stdout}")


class LocalIsLinkedNotCopied(DevContainerTestCase):

    ENV = "GIT_USER_NAME=demo-agent\nGIT_USER_EMAIL=demo-agent@example.com\n"

    def _write(self, rel, content="x\n"):
        path = os.path.join(self.harness.fixture_repo, ".local", rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(content)

    def _stub_copying_rsync(self):
        self.rsync_log = os.path.join(self.harness.tmp, "rsync-calls.log")
        self.harness._write_stub(
            "rsync",
            "#!/bin/sh\n"
            'echo "$@" >> "%s"\n' % self.rsync_log
            + 'for a in "$@"; do src="$dst"; dst="$a"; done\n'
            + 'mkdir -p "$dst" && cp -a "$src". "$dst"\n',
        )

    def _acquire(self, **kwargs):
        self.harness.build_fixture_repo(with_parent_env=self.ENV)
        self._stub_copying_rsync()
        self._write("handoff-demo/x")
        self._write("db-backup-x/y")
        self._write("db-snapshots/z.sql.gz")
        self._write("notes/n.md")
        result = self.harness.run_acquire(**kwargs)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        name = self.harness.acquired_slot_name(result)
        return name, os.path.join(self.harness.slot_dir(name), ".local")

    def test_handoff_directory_is_an_absolute_symlink_to_the_main_checkout(self):
        name, slot_local = self._acquire()

        link = os.path.join(slot_local, "handoff-demo")
        self.assertTrue(os.path.islink(link))
        target = os.readlink(link)
        self.assertTrue(os.path.isabs(target), target)
        main = os.path.join(self.harness.fixture_repo, ".local", "handoff-demo")
        self.assertIn(target, (main, os.path.realpath(main)))

    def test_no_db_backup_db_snapshots_or_other_local_content_is_in_the_slot(self):
        name, slot_local = self._acquire()

        self.assertFalse(os.path.lexists(os.path.join(slot_local, "db-backup-x")))
        self.assertFalse(os.path.lexists(os.path.join(slot_local, "db-snapshots")))
        self.assertFalse(os.path.lexists(os.path.join(slot_local, "notes")))

    def test_acquire_does_not_invoke_rsync(self):
        self._acquire()

        self.assertFalse(os.path.exists(self.rsync_log), "acquire called rsync")


class IdleLeaseIsListedAsIdle(DevContainerTestCase):

    ENV = "GIT_USER_NAME=demo-agent\nGIT_USER_EMAIL=demo-agent@example.com\n"
    PORTS = "NOVNC_PORT=39236\nSSH_PORT=39226\nVITE_DEV_PORT=39246\n"

    def _write_lease(self, name, age_seconds):
        path = self.harness.lease_path(name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "holder": "task-12",
                    "project": PROJECT_PREFIX + "-" + name,
                    "since": int(time.time()) - age_seconds,
                    "key": "/dev/null",
                    "ssh_port": 39226,
                },
                handle,
            )
        return path

    def _make_slot(self, name, local_identity=("demo-agent", "demo-agent@example.com")):
        self.harness.build_fixture_repo()
        self.harness.make_bare_slot(
            name,
            env_content=self.ENV,
            ports_content=self.PORTS,
            local_identity=local_identity,
        )

    def _make_clean_slot(self, name):
        self._make_slot(name)
        slot = self.harness.slot_dir(name)
        git = lambda *args: subprocess.run(
            [self.harness.real_git_bin, "-C", slot, "-c", "commit.gpgsign=false", *args],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        remote = os.path.join(self.harness.tmp, name + "-remote.git")
        subprocess.run(
            ["git", "init", "--bare", "-q", remote],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        git("add", "-A")
        git("commit", "-q", "-m", "fixture")
        git("branch", "-M", "master")
        git("push", "-q", remote, "master:refs/heads/master")
        git("fetch", "-q", remote, "+refs/heads/master:refs/remotes/origin/master")
        return slot

    def test_idle_past_the_limit_is_idle_whatever_runs_in_it(self):
        self._make_clean_slot("held-open")
        self.harness.mark_running("held-open")
        self.harness.set_container_procs("held-open", ["sshd", "sshd", "playwright"])
        self._write_lease("held-open", age_seconds=18000)

        result = self.harness.run("list")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        row = self._row(result.stdout.decode(), "held-open")
        self.assertEqual(row[10], "clean", "fixture is not actually clean — test proves nothing")
        self.assertEqual(row[2], "idle")

    def test_idle_and_dirty_is_idle(self):
        self._make_slot("left-dirty")
        self._write_lease("left-dirty", age_seconds=18000)

        result = self.harness.run("list")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(self._row(result.stdout.decode(), "left-dirty")[2], "idle")

    def test_a_live_lease_on_a_busy_slot_is_leased(self):
        self._make_clean_slot("working")
        self.harness.mark_running("working")
        self.harness.set_container_procs("working", ["sshd", "sshd"])
        self._write_lease("working", age_seconds=60)

        result = self.harness.run("list")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(self._row(result.stdout.decode(), "working")[2], "leased")

    def test_a_lease_inside_the_limit_that_nothing_uses_is_leased(self):
        self._make_clean_slot("quiet")
        self._write_lease("quiet", age_seconds=3600)

        result = self.harness.run("list", "--brief")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        row = self._row(result.stdout.decode(), "quiet")
        self.assertEqual(row[2], "leased")
        self.assertEqual(row[10], "-")

    @staticmethod
    def _row(stdout, slot_name):
        for line in stdout.splitlines():
            fields = line.split()
            if fields and fields[0] == slot_name:
                return fields
        raise AssertionError(f"no '{slot_name}' row in:\n{stdout}")


class HostGitNeverTouchesALeasedSlotsLocks(DevContainerTestCase):

    ENV = "GIT_USER_NAME=demo-agent\nGIT_USER_EMAIL=demo-agent@example.com\n"
    PORTS = "NOVNC_PORT=39236\nSSH_PORT=39226\nVITE_DEV_PORT=39246\n"

    def _write_lease(self, name, ttl=14400):
        path = self.harness.lease_path(name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "holder": "task-13",
                    "project": PROJECT_PREFIX + "-" + name,
                    "since": int(time.time()),
                    "ttl_seconds": ttl,
                    "key": "/dev/null",
                    "ssh_port": 39226,
                },
                handle,
            )

    def _make_slot(self, name):
        if not os.path.isdir(self.harness.fixture_repo):
            self.harness.build_fixture_repo()
        self.harness.make_bare_slot(
            name,
            env_content=self.ENV,
            ports_content=self.PORTS,
            local_identity=("demo-agent", "demo-agent@example.com"),
        )

    def test_list_does_not_fetch_a_leased_slot_but_still_fetches_a_free_one(self):
        self._make_slot("leased")
        self._write_lease("leased")
        self._make_slot("free")

        result = self.harness.run("list")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        fetch_calls = [c for c in self.harness.git_calls() if " fetch " in f" {c} "]
        leased_dir = self.harness.slot_dir("leased")
        free_dir = self.harness.slot_dir("free")
        self.assertFalse(
            any(leased_dir in c for c in fetch_calls),
            f"list fetched inside the leased slot: {fetch_calls}",
        )
        self.assertTrue(
            any(free_dir in c for c in fetch_calls),
            f"list never fetched the free slot at all — this test proves nothing: {fetch_calls}",
        )

    def test_host_side_status_and_stash_reads_carry_no_optional_locks(self):
        self._make_slot("quiet")

        result = self.harness.run("list")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        slot = self.harness.slot_dir("quiet")
        reads = [
            c for c in self.harness.git_calls()
            if slot in c and (" status " in f" {c} " or " stash " in f" {c} ")
        ]
        self.assertTrue(reads, "list never read this slot's status/stash at all — this test proves nothing")
        for call in reads:
            self.assertTrue(
                call.startswith("GIT_OPTIONAL_LOCKS=0 "),
                f"a host-side read ran without GIT_OPTIONAL_LOCKS=0: {call}",
            )


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


class SlotSigningIdentity(DevContainerTestCase):

    ENV = "GIT_USER_NAME=demo-agent\nGIT_USER_EMAIL=demo-agent@example.com\n"
    CONTAINER_KEY = "/home/node/.ssh/container_signing.pub"

    def _config(self, name, key):
        result = subprocess.run(
            ["git", "-C", self.harness.slot_dir(name), "config", "--local", key],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        return result.stdout.decode().strip()

    def test_restart_points_the_checkout_at_the_container_signing_key(self):
        self.harness.build_fixture_repo()
        self.harness.make_bare_slot(
            "signed",
            env_content=self.ENV,
            ports_content="NOVNC_PORT=39238\nSSH_PORT=39228\nVITE_DEV_PORT=39248\n",
            local_identity=("demo-agent", "demo-agent@example.com"),
        )

        result = self.harness.run("restart", "signed")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(self._config("signed", "user.signingkey"), self.CONTAINER_KEY)
        self.assertEqual(self._config("signed", "gpg.format"), "ssh")
        self.assertEqual(self._config("signed", "commit.gpgsign"), "true")

    @unittest.skipIf(
        os.path.exists("/home/node/.ssh/container_signing.pub"),
        "the container signing key is present here — a signed commit is expected",
    )
    def test_a_host_side_commit_in_the_slot_fails_instead_of_being_signed(self):
        self.harness.build_fixture_repo()
        self.harness.make_bare_slot(
            "signed",
            env_content=self.ENV,
            ports_content="NOVNC_PORT=39239\nSSH_PORT=39229\nVITE_DEV_PORT=39249\n",
            local_identity=("demo-agent", "demo-agent@example.com"),
        )
        self.harness.run("restart", "signed")

        slot = self.harness.slot_dir("signed")
        with open(os.path.join(slot, "host-authored.txt"), "w", encoding="utf-8") as handle:
            handle.write("written on the host\n")
        subprocess.run(
            [self.harness.real_git_bin, "-C", slot, "add", "host-authored.txt"],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        commit = subprocess.run(
            [self.harness.real_git_bin, "-C", slot, "commit", "-m", "from the host"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )

        self.assertNotEqual(commit.returncode, 0, "a host-side commit was allowed to sign")
        self.assertIn("container_signing", commit.stderr.decode())


class EvictSurvivesARefusedRescuePush(DevContainerTestCase):

    ENV = "GIT_USER_NAME=demo-agent\nGIT_USER_EMAIL=demo-agent@example.com\n"

    def setUp(self):
        super().setUp()
        self.remote = os.path.join(self.harness.tmp, "refusing-remote.git")
        subprocess.run(
            ["git", "init", "--bare", "-q", self.remote],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        hook = os.path.join(self.remote, "hooks", "pre-receive")
        with open(hook, "w", encoding="utf-8") as handle:
            handle.write(
                "#!/usr/bin/env bash\n"
                "while read -r _ _ ref; do\n"
                '  case "$ref" in\n'
                "    *aaa-refused*)\n"
                '      echo "- This ref is protected here." >&2\n'
                "      exit 1\n"
                "      ;;\n"
                "  esac\n"
                "done\n"
                "exit 0\n"
            )
        os.chmod(hook, 0o755)
        self.harness._write_stub("gh", "#!/usr/bin/env bash\nexit 1\n")

    def _held_branch(self, slot, branch, filename):
        with open(os.path.join(slot, filename), "w", encoding="utf-8") as handle:
            handle.write(branch + "\n")
        git = lambda *args: subprocess.run(
            [self.harness.real_git_bin, "-C", slot, "-c", "commit.gpgsign=false", *args],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        git("checkout", "-q", "-b", branch)
        git("add", filename)
        git("commit", "-q", "-m", "held work on " + branch)

    def _point_fixture_at_remote(self):
        subprocess.run(
            ["git", "-C", self.harness.fixture_repo, "remote", "set-url", "origin", self.remote],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )

    def _seed_master(self, slot, git):
        with open(os.path.join(slot, ".gitignore"), "w", encoding="utf-8") as handle:
            handle.write(".devcontainer/.env\n.devcontainer/.slot-holder\ndocs/\n")
        with open(os.path.join(slot, "README.md"), "w", encoding="utf-8") as handle:
            handle.write("slot fixture\n")
        git("add", "README.md", ".gitignore", ".devcontainer/docker-compose.yml")
        git("commit", "-q", "-m", "base")
        git("branch", "-M", "master")
        git("push", "-q", "origin", "master")
        git("fetch", "-q", "origin")

    def test_a_refused_push_costs_one_branch_not_the_whole_eviction(self):
        self.harness.build_fixture_repo()
        self._point_fixture_at_remote()
        self.harness.make_bare_slot("refused", env_content=self.ENV)
        slot = self.harness.slot_dir("refused")
        subprocess.run(
            [self.harness.real_git_bin, "-C", slot, "remote", "set-url", "origin", self.remote],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        subprocess.run(
            [self.harness.real_git_bin, "-C", slot, "config", "--local", "user.email", "fixture@example.com"],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        subprocess.run(
            [self.harness.real_git_bin, "-C", slot, "config", "--local", "user.name", "fixture"],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        git = lambda *args: subprocess.run(
            [self.harness.real_git_bin, "-C", slot, "-c", "commit.gpgsign=false", *args],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self._seed_master(slot, git)

        self._held_branch(slot, "aaa-refused", "refused.txt")
        self._held_branch(slot, "zzz-keeps-going", "kept.txt")

        result = self.harness.run("evict", "refused", "--yes")

        stderr = result.stderr.decode()
        self.assertEqual(result.returncode, 0, stderr)
        self.assertIn("aaa-refused", stderr)
        self.assertIn("the remote refused", stderr)

        rescued = subprocess.run(
            [self.harness.real_git_bin, "-C", self.remote, "for-each-ref", "--format=%(refname:short)", "refs/heads/"],
            stdout=subprocess.PIPE, check=True,
        ).stdout.decode().split()
        self.assertTrue(
            any(ref.endswith("/zzz-keeps-going") for ref in rescued),
            "the branch after the refused one was never pushed: " + str(rescued),
        )
        self.assertFalse(
            any(ref.endswith("/aaa-refused") for ref in rescued),
            "the remote was supposed to refuse this one: " + str(rescued),
        )

        bundles = os.listdir(os.path.join(self.harness.state_dir, "rescue"))
        self.assertTrue(
            any(name.startswith(PROJECT_PREFIX + "-refused") for name in bundles),
            "no bundle was written for a slot whose push was refused: " + str(bundles),
        )
        self.assertFalse(
            os.path.exists(os.path.join(slot, "kept.txt")),
            "the slot was never reset — eviction stopped at the refused push",
        )

    def test_uncommitted_work_survives_a_slot_configured_to_sign_with_the_container_key(self):
        self.harness.build_fixture_repo()
        self._point_fixture_at_remote()
        self.harness.make_bare_slot("signing", env_content=self.ENV)
        slot = self.harness.slot_dir("signing")
        git = lambda *args: subprocess.run(
            [self.harness.real_git_bin, "-C", slot, "-c", "commit.gpgsign=false", *args],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        git("remote", "set-url", "origin", self.remote)
        git("config", "--local", "user.email", "fixture@example.com")
        git("config", "--local", "user.name", "fixture")
        self._seed_master(slot, git)

        git("config", "--local", "gpg.format", "ssh")
        git("config", "--local", "user.signingkey", os.path.join(self.harness.tmp, "no-such-key.pub"))
        git("config", "--local", "commit.gpgsign", "true")

        with open(os.path.join(slot, "unsaved.txt"), "w", encoding="utf-8") as handle:
            handle.write("an hour of work nobody committed\n")

        result = self.harness.run("evict", "signing", "--yes")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertIn(
            "unsaved.txt",
            self.harness.bundle_head_file_list("signing"),
            "the rescue commit never happened and the reset deleted the work it was meant to save",
        )

    def test_a_signature_refusal_stops_the_next_unsigned_branch_being_offered(self):
        self.harness.build_fixture_repo()
        self._point_fixture_at_remote()
        self.harness.make_bare_slot("signatures", env_content=self.ENV)
        slot = self.harness.slot_dir("signatures")
        git = lambda *args: subprocess.run(
            [self.harness.real_git_bin, "-C", slot, "-c", "commit.gpgsign=false", *args],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        git("remote", "set-url", "origin", self.remote)
        git("config", "--local", "user.email", "fixture@example.com")
        git("config", "--local", "user.name", "fixture")
        self._seed_master(slot, git)

        with open(os.path.join(self.remote, "hooks", "pre-receive"), "w", encoding="utf-8") as handle:
            handle.write(
                "#!/usr/bin/env bash\n"
                'echo "- Commits must have verified signatures." >&2\n'
                "exit 1\n"
            )
        os.chmod(os.path.join(self.remote, "hooks", "pre-receive"), 0o755)

        for branch, filename in (("aaa-first", "one.txt"), ("bbb-second", "two.txt"), ("ccc-third", "three.txt")):
            self._held_branch(slot, branch, filename)

        result = self.harness.run("evict", "signatures", "--yes")

        stdout = result.stdout.decode()
        stderr = result.stderr.decode()
        self.assertEqual(result.returncode, 0, stderr)
        self.assertIn("rescue/demo-signatures/", stdout)
        self.assertEqual(
            stdout.count("▸ pushing"), 1,
            "a branch was offered after the remote had already named its rule:\n" + stdout,
        )
        self.assertIn("skipping 'bbb-second'", stdout)
        self.assertIn("skipping 'ccc-third'", stdout)
        self.assertIn("⚠ refused: aaa-first — Commits must have verified signatures.", stderr)
        self.assertNotIn("GH013", stderr)
        logs = [
            name for name in os.listdir(os.path.join(self.harness.state_dir, "rescue"))
            if name.endswith("-push.log")
        ]
        self.assertTrue(logs, "the refused push's own output was kept nowhere")
        with open(os.path.join(self.harness.state_dir, "rescue", logs[0]), encoding="utf-8") as handle:
            self.assertIn("aaa-first", handle.read())

    def test_the_rescue_commit_is_not_judged_by_the_repositorys_own_hooks(self):
        self.harness.build_fixture_repo()
        self._point_fixture_at_remote()
        self.harness.make_bare_slot("hooked", env_content=self.ENV)
        slot = self.harness.slot_dir("hooked")
        git = lambda *args: subprocess.run(
            [self.harness.real_git_bin, "-C", slot, "-c", "commit.gpgsign=false", *args],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        git("remote", "set-url", "origin", self.remote)
        git("config", "--local", "user.email", "fixture@example.com")
        git("config", "--local", "user.name", "fixture")
        self._seed_master(slot, git)

        hooks = os.path.join(slot, ".git", "hooks")
        with open(os.path.join(hooks, "commit-msg"), "w", encoding="utf-8") as handle:
            handle.write(
                "#!/usr/bin/env bash\n"
                'case "$(head -1 "$1")" in\n'
                "  feat*|fix*|chore*|ci*|docs*|test*) exit 0 ;;\n"
                '  *) echo "Commit type not allowed" >&2; exit 1 ;;\n'
                "esac\n"
            )
        with open(os.path.join(hooks, "pre-commit"), "w", encoding="utf-8") as handle:
            handle.write(
                "#!/usr/bin/env bash\n"
                'printf "ran\\n" > "$(git rev-parse --show-toplevel)/../pre-commit-ran"\n'
                "exit 0\n"
            )
        for name in ("commit-msg", "pre-commit"):
            os.chmod(os.path.join(hooks, name), 0o755)

        with open(os.path.join(slot, "unsaved.txt"), "w", encoding="utf-8") as handle:
            handle.write("work the hooks would have judged\n")

        result = self.harness.run("evict", "hooked", "--yes")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        sha = self.harness.bundle_head_sha("hooked")
        message = subprocess.run(
            [self.harness.real_git_bin, "-C", slot, "log", "-1", "--format=%s", sha],
            stdout=subprocess.PIPE, check=True,
        ).stdout.decode()
        self.assertIn(
            "unsaved.txt", self.harness.bundle_head_file_list("hooked"),
            "the rescue commit was refused by the hook again",
        )
        self.assertIn("chore(slot): rescue", message)
        self.assertFalse(
            os.path.exists(os.path.join(self.harness.slots_dir, "pre-commit-ran")),
            "a pre-commit hook ran over a rescue snapshot and could have rewritten it",
        )


class EvictLooksAtTheLease(DevContainerTestCase):

    def _slot_with_work(self, name):
        self.harness.build_fixture_repo()
        self.harness.make_bare_slot(name, env_content="GIT_USER_NAME=demo-agent\nGIT_USER_EMAIL=a@example.com\n")
        slot = self.harness.slot_dir(name)
        git = lambda *args: subprocess.run(
            [self.harness.real_git_bin, "-C", slot, "-c", "commit.gpgsign=false", *args],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        git("config", "--local", "user.email", "fixture@example.com")
        git("config", "--local", "user.name", "fixture")
        remote = os.path.join(self.harness.tmp, name + "-remote.git")
        subprocess.run(
            ["git", "init", "--bare", "-q", remote],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        git("remote", "set-url", "origin", remote)
        subprocess.run(
            ["git", "-C", self.harness.fixture_repo, "remote", "set-url", "origin", remote],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        with open(os.path.join(slot, ".gitignore"), "w", encoding="utf-8") as handle:
            handle.write(".devcontainer/.env\n.devcontainer/.slot-holder\ndocs/\n")
        with open(os.path.join(slot, "README.md"), "w", encoding="utf-8") as handle:
            handle.write("slot fixture\n")
        git("add", "README.md", ".gitignore", ".devcontainer/docker-compose.yml")
        git("commit", "-q", "-m", "base")
        git("branch", "-M", "master")
        git("push", "-q", "origin", "master")
        git("fetch", "-q", "origin")
        with open(os.path.join(slot, "unsaved.txt"), "w", encoding="utf-8") as handle:
            handle.write("unsaved work somebody is mid-way through\n")
        return slot

    def _write_lease(self, name, holder, age_seconds=60, ttl=14400):
        path = self.harness.lease_path(name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "holder": holder,
                    "project": PROJECT_PREFIX + "-" + name,
                    "since": int(time.time()) - age_seconds,
                    "ttl_seconds": ttl,
                    "key": "/dev/null",
                    "ssh_port": 39229,
                },
                handle,
            )
        return path

    def test_a_live_lease_stops_the_eviction_and_the_work_stays(self):
        slot = self._slot_with_work("busy")
        self._write_lease("busy", "task-15")

        result = self.harness.run("evict", "busy", "--yes")

        stderr = result.stderr.decode()
        self.assertNotEqual(result.returncode, 0, result.stdout.decode())
        self.assertIn("refusing to evict", stderr)
        self.assertIn("task-15", stderr)
        self.assertIn("--force", stderr)
        self.assertTrue(
            os.path.exists(os.path.join(slot, "unsaved.txt")),
            "the slot was reset despite a live lease",
        )
        self.assertTrue(os.path.exists(self.harness.lease_path("busy")), "the lease was removed anyway")

    def test_force_evicts_the_same_slot(self):
        slot = self._slot_with_work("forced")
        self._write_lease("forced", "task-15")

        result = self.harness.run("evict", "forced", "--yes", "--force")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertFalse(
            os.path.exists(os.path.join(slot, "unsaved.txt")),
            "--force did not actually evict",
        )
        self.assertIn(
            "unsaved.txt",
            self.harness.bundle_head_file_list("forced"),
            "the work was discarded rather than rescued",
        )

    def test_an_expired_lease_still_evicts_without_force(self):
        slot = self._slot_with_work("stale")
        self._write_lease("stale", "task-16", age_seconds=18000, ttl=14400)

        result = self.harness.run("evict", "stale", "--yes")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertFalse(os.path.exists(os.path.join(slot, "unsaved.txt")))
        self.assertFalse(os.path.exists(self.harness.lease_path("stale")))


class EvictSignsTheRescueCommitWhereTheKeyIs(DevContainerTestCase):

    def _slot(self, name):
        self.harness.build_fixture_repo()
        self.harness.make_bare_slot(name, env_content="GIT_USER_NAME=demo-agent\nGIT_USER_EMAIL=a@example.com\n")
        slot = self.harness.slot_dir(name)
        compose_file = os.path.join(self.harness.main_devcontainer(), "docker-compose.yml")
        with open(compose_file, "a", encoding="utf-8") as handle:
            handle.write("    volumes:\n      - ..:/workspace:cached\n")
        git = lambda *args: subprocess.run(
            [self.harness.real_git_bin, "-C", slot, "-c", "commit.gpgsign=false", *args],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        git("config", "--local", "user.email", "fixture@example.com")
        git("config", "--local", "user.name", "fixture")
        remote = os.path.join(self.harness.tmp, name + "-remote.git")
        subprocess.run(
            ["git", "init", "--bare", "-q", remote],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        git("remote", "set-url", "origin", remote)
        subprocess.run(
            ["git", "-C", self.harness.fixture_repo, "remote", "set-url", "origin", remote],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        with open(os.path.join(slot, ".gitignore"), "w", encoding="utf-8") as handle:
            handle.write(".devcontainer/.env\n.devcontainer/.slot-holder\ndocs/\n")
        with open(os.path.join(slot, "README.md"), "w", encoding="utf-8") as handle:
            handle.write("slot fixture\n")
        git("add", "README.md", ".gitignore", ".devcontainer/docker-compose.yml")
        git("commit", "-q", "-m", "base")
        git("branch", "-M", "master")
        git("push", "-q", "origin", "master")
        git("fetch", "-q", "origin")
        return slot, remote

    def test_the_commit_is_made_through_the_container_when_it_is_running(self):
        slot, _remote = self._slot("signed")
        self.harness.mark_running("signed")
        with open(os.path.join(slot, "unsaved.txt"), "w", encoding="utf-8") as handle:
            handle.write("an hour of work nobody committed\n")

        result = self.harness.run("evict", "signed", "--yes")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        with open(self.harness.docker_log, encoding="utf-8") as handle:
            log = handle.read()
        exec_lines = [
            line for line in log.splitlines()
            if " exec " in line and "-u node" in line and "git commit" in line
        ]
        self.assertTrue(
            exec_lines,
            "the rescue commit was never made through the container as node:\n" + log,
        )
        self.assertNotIn("commit.gpgsign=false", exec_lines[-1], exec_lines[-1])

    def test_bundle_only_when_the_container_is_not_running(self):
        slot, remote = self._slot("unsigned")
        with open(os.path.join(slot, "unsaved.txt"), "w", encoding="utf-8") as handle:
            handle.write("an hour of work nobody committed\n")

        result = self.harness.run("evict", "unsigned", "--yes")

        stdout = result.stdout.decode()
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertIn(
            "bundle only (container not running, so the commit is unsigned)", stdout
        )
        self.assertNotIn("▸ pushing refs/heads/rescue", stdout, "an unsigned commit was offered to the remote")

        rescued = subprocess.run(
            [self.harness.real_git_bin, "-C", remote, "for-each-ref", "--format=%(refname:short)", "refs/heads/"],
            stdout=subprocess.PIPE, check=True,
        ).stdout.decode().split()
        self.assertFalse(
            [ref for ref in rescued if ref.startswith("rescue/")],
            "the unsigned WIP commit reached the remote anyway: " + str(rescued),
        )
        self.assertIn(
            "unsaved.txt",
            self.harness.bundle_head_file_list("unsigned"),
            "the unsigned commit is not even in the bundle",
        )


class EvictTwiceInOneDayWritesDistinctBundles(DevContainerTestCase):

    def _slot(self, name):
        self.harness.build_fixture_repo()
        self.harness.make_bare_slot(name, env_content="GIT_USER_NAME=demo-agent\nGIT_USER_EMAIL=a@example.com\n")
        slot = self.harness.slot_dir(name)
        git = lambda *args: subprocess.run(
            [self.harness.real_git_bin, "-C", slot, "-c", "commit.gpgsign=false", *args],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        git("config", "--local", "user.email", "fixture@example.com")
        git("config", "--local", "user.name", "fixture")
        remote = os.path.join(self.harness.tmp, name + "-remote.git")
        subprocess.run(
            ["git", "init", "--bare", "-q", remote],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        git("remote", "set-url", "origin", remote)
        subprocess.run(
            ["git", "-C", self.harness.fixture_repo, "remote", "set-url", "origin", remote],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        with open(os.path.join(slot, ".gitignore"), "w", encoding="utf-8") as handle:
            handle.write(".devcontainer/.env\n.devcontainer/.slot-holder\ndocs/\n")
        with open(os.path.join(slot, "README.md"), "w", encoding="utf-8") as handle:
            handle.write("slot fixture\n")
        git("add", "README.md", ".gitignore")
        git("commit", "-q", "-m", "base")
        git("branch", "-M", "master")
        git("push", "-q", "origin", "master")
        git("fetch", "-q", "origin")
        return slot

    def test_two_evicts_leave_two_bundle_files(self):
        slot = self._slot("twice")
        with open(os.path.join(slot, "first.txt"), "w", encoding="utf-8") as handle:
            handle.write("first evict's own held work\n")
        result = self.harness.run("evict", "twice", "--yes")
        self.assertEqual(result.returncode, 0, result.stderr.decode())

        with open(os.path.join(slot, "second.txt"), "w", encoding="utf-8") as handle:
            handle.write("second evict's own held work, minutes later\n")
        result = self.harness.run("evict", "twice", "--yes")
        self.assertEqual(result.returncode, 0, result.stderr.decode())

        project = f"{PROJECT_PREFIX}-twice"
        bundle_dir = os.path.join(self.harness.state_dir, "rescue")
        bundles = sorted(
            f for f in os.listdir(bundle_dir)
            if f.startswith(project + "-") and f.endswith(".bundle")
        )
        self.assertEqual(
            len(bundles), 2,
            f"two evicts on the same day should leave two distinct bundles, found: {bundles}",
        )
        self.assertNotEqual(bundles[0], bundles[1])


class EvictRefusesADirectoryThatIsNotThisProjectsSlot(DevContainerTestCase):

    def _foreign_slot(self, name):
        self.harness.build_fixture_repo()
        self.harness.make_bare_slot(name, env_content="GIT_USER_NAME=demo-agent\nGIT_USER_EMAIL=a@example.com\n")
        slot = self.harness.slot_dir(name)
        git = lambda *args: subprocess.run(
            [self.harness.real_git_bin, "-C", slot, "-c", "commit.gpgsign=false", *args],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        git("config", "--local", "user.email", "fixture@example.com")
        git("config", "--local", "user.name", "fixture")
        remote = os.path.join(self.harness.tmp, name + "-other-project-remote.git")
        subprocess.run(
            ["git", "init", "--bare", "-q", remote],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        git("remote", "set-url", "origin", remote)
        with open(os.path.join(slot, "README.md"), "w", encoding="utf-8") as handle:
            handle.write("another project's own fixture\n")
        git("add", "README.md")
        git("commit", "-q", "-m", "base")
        git("branch", "-M", "master")
        git("push", "-q", "origin", "master")
        git("fetch", "-q", "origin")
        git("checkout", "-q", "-b", "topic/other-projects-work")
        with open(os.path.join(slot, "unsaved.txt"), "w", encoding="utf-8") as handle:
            handle.write("somebody else's uncommitted work\n")
        return slot, remote

    def test_refuses_before_pushing_resetting_or_cleaning_the_foreign_directory(self):
        slot, remote = self._foreign_slot("shared-name")

        result = self.harness.run("evict", "shared-name", "--yes", "--force")

        stderr = result.stderr.decode()
        self.assertNotEqual(result.returncode, 0, result.stdout.decode())
        self.assertIn(slot, stderr)
        self.assertIn(remote, stderr)

        branches = subprocess.run(
            [self.harness.real_git_bin, "-C", remote, "for-each-ref", "--format=%(refname)", "refs/heads/"],
            stdout=subprocess.PIPE, check=True,
        ).stdout.decode()
        self.assertNotIn(
            "topic/other-projects-work", branches,
            "the foreign checkout's branch was pushed to its own remote",
        )

        self.assertTrue(
            os.path.exists(os.path.join(slot, "unsaved.txt")),
            "the foreign checkout's uncommitted work was discarded",
        )
        current_branch = subprocess.run(
            [self.harness.real_git_bin, "-C", slot, "branch", "--show-current"],
            stdout=subprocess.PIPE, check=True,
        ).stdout.decode().strip()
        self.assertEqual(
            current_branch, "topic/other-projects-work",
            "the foreign checkout was reset off the branch it was on",
        )

    def test_force_does_not_override_the_refusal(self):
        slot, remote = self._foreign_slot("force-does-not-help")

        without_force = self.harness.run("evict", "force-does-not-help", "--yes")
        with_force = self.harness.run("evict", "force-does-not-help", "--yes", "--force")

        self.assertNotEqual(without_force.returncode, 0, without_force.stdout.decode())
        self.assertNotEqual(with_force.returncode, 0, with_force.stdout.decode())
        self.assertIn(slot, with_force.stderr.decode())
        self.assertTrue(
            os.path.exists(os.path.join(slot, "unsaved.txt")),
            "--force pushed the ownership check aside and evicted the foreign slot anyway",
        )


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


class DocsRepoRefusesCommitsAndPushes(DevContainerTestCase):

    ENV_WITH_IDENTITY = "GIT_USER_NAME=demo-agent\nGIT_USER_EMAIL=demo-agent@example.com\n"
    REFUSAL = "docs/repo is read-only"
    PUSHURL = "read-only://docs-repo-is-read-only--clone-docs-fix"

    def setUp(self):
        super().setUp()
        self.harness._write_stub("gh", "#!/usr/bin/env bash\necho '[]'\n")

    def _acquire(self, holder="docs-guard"):
        self.harness.build_fixture_repo(with_parent_env=self.ENV_WITH_IDENTITY)
        result = self.harness.run_acquire(holder=holder)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        name = self.harness.acquired_slot_name(result)
        slot = self.harness.slot_dir(name)
        docs = os.path.join(slot, "docs", "repo")
        self.assertTrue(os.path.isdir(os.path.join(docs, ".git")), "no real docs/repo clone — fixture is broken")
        return name, slot, docs

    def _docs_git(self, docs, *args):
        return subprocess.run(
            [
                self.harness.real_git_bin, "-C", docs,
                "-c", "commit.gpgsign=false",
                "-c", "user.email=fixture@example.com",
                "-c", "user.name=fixture",
                *args,
            ],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )

    def _stub_branches(self):
        return subprocess.run(
            [self.harness.real_git_bin, "-C", self.harness.docs_repo_stub,
             "for-each-ref", "--format=%(refname:short)", "refs/heads/"],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        ).stdout.decode().split()

    def test_commit_is_refused_with_where_a_docs_fix_goes(self):
        name, slot, docs = self._acquire()
        head = self._docs_git(docs, "rev-parse", "HEAD").stdout

        result = self._docs_git(docs, "commit", "--allow-empty", "-m", "x")

        self.assertNotEqual(result.returncode, 0, result.stdout.decode())
        stderr = result.stderr.decode()
        self.assertIn(self.REFUSAL, stderr)
        self.assertIn("docs/fix", stderr)
        self.assertIn("into docs/fix", stderr)
        self.assertEqual(self._docs_git(docs, "rev-parse", "HEAD").stdout, head)

    def test_commit_is_still_refused_where_the_container_sees_the_clone(self):
        name, slot, docs = self._acquire()
        hooks_path = self._docs_git(docs, "config", "--local", "--get", "core.hooksPath").stdout.decode().strip()
        self.assertFalse(os.path.isabs(hooks_path), hooks_path)

        elsewhere = os.path.join(self.harness.tmp, "container-view", "docs-repo")
        os.makedirs(os.path.dirname(elsewhere))
        os.rename(docs, elsewhere)

        result = self._docs_git(elsewhere, "commit", "--allow-empty", "-m", "x")

        self.assertNotEqual(result.returncode, 0, result.stdout.decode())
        self.assertIn(self.REFUSAL, result.stderr.decode())

    def test_push_is_refused_by_the_hook_and_without_it_by_the_pushurl(self):
        name, slot, docs = self._acquire()
        subprocess.run(
            [self.harness.real_git_bin, "-C", self.harness.docs_repo_stub,
             "config", "receive.denyCurrentBranch", "updateInstead"],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        before = self._stub_branches()

        hooked = self._docs_git(docs, "push", self.harness.docs_repo_stub, "HEAD:refs/heads/docs/topic-branch")
        self.assertNotEqual(hooked.returncode, 0)
        self.assertIn(self.REFUSAL, hooked.stderr.decode())

        bypassed = self._docs_git(docs, "push", "--no-verify", "origin", "HEAD:refs/heads/docs/topic-branch")
        self.assertNotEqual(bypassed.returncode, 0)
        self.assertIn("read-only", bypassed.stderr.decode())

        self.assertEqual(self._stub_branches(), before)
        self.assertEqual(
            self._docs_git(docs, "config", "remote.origin.pushurl").stdout.decode().strip(),
            self.PUSHURL,
        )

    def test_fetch_and_a_detached_checkout_of_a_new_pin_still_work(self):
        name, slot, docs = self._acquire()
        stub = self.harness.docs_repo_stub
        subprocess.run(
            [self.harness.real_git_bin, "-C", stub,
             "-c", "user.name=fixture", "-c", "user.email=fixture@example.com",
             "commit", "-q", "--allow-empty", "-m", "a newer pin"],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        pin = subprocess.run(
            [self.harness.real_git_bin, "-C", stub, "rev-parse", "HEAD"],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        ).stdout.decode().strip()

        fetched = self._docs_git(docs, "fetch", "--quiet", "--depth", "1", "origin", pin)
        self.assertEqual(fetched.returncode, 0, fetched.stderr.decode())
        checked_out = self._docs_git(docs, "checkout", "--quiet", "--detach", pin)
        self.assertEqual(checked_out.returncode, 0, checked_out.stderr.decode())
        self.assertEqual(self._docs_git(docs, "rev-parse", "HEAD").stdout.decode().strip(), pin)


class ListStaysCheapAndNeverSilent(ListVerdictTestCase):

    def _gh_calls(self):
        if not os.path.isfile(self._gh_log):
            return ""
        with open(self._gh_log, encoding="utf-8") as handle:
            return handle.read()

    def _undelivered_branch(self, slot, branch="wip"):
        git = self._git(slot)
        git("branch", branch)
        git("checkout", "-q", branch)
        with open(os.path.join(slot, "notes.md"), "w", encoding="utf-8") as handle:
            handle.write("never pushed anywhere\n")
        git("add", "notes.md")
        git("commit", "-q", "-m", "wip")
        git("checkout", "-q", "--detach", "origin/master")


    def test_fast_says_in_its_own_header_what_it_did_not_check(self):
        self._ready_slot("quick")

        fast = self.harness.run("list", "--fast")

        self.assertEqual(fast.returncode, 0, fast.stderr.decode())
        header = fast.stdout.decode().splitlines()[0]
        self.assertIn("--fast", header)
        self.assertIn("no delivery check", header)


    def test_a_listing_with_nothing_to_list_is_never_zero_bytes(self):
        for args in (("list",), ("list", "--brief"), ("list", "--fast")):
            with self.subTest(args=args):
                result = self.harness.run(*args)

                stdout = result.stdout.decode()
                self.assertEqual(result.returncode, 0, result.stderr.decode())
                self.assertNotEqual(stdout.strip(), "", "zero bytes and exit 0")
                self.assertTrue(stdout.startswith("SLOT "), stdout)
                self.assertIn("nothing to list", stdout)


class ReapFastForwardsTheMainCheckout(ListVerdictTestCase):

    def setUp(self):
        super().setUp()
        self.remote = self._shared_remote()
        self.main = self.harness.fixture_repo
        git = self._git(self.main)
        git("branch", "-M", "master")
        git("push", "-q", "origin", "master")
        git("fetch", "-q", "origin")
        self._ready_slot("free")

    def _head(self):
        return self._git(self.main)("rev-parse", "HEAD").stdout.decode().strip()

    def _advance_origin(self, name="upstream"):
        other = os.path.join(self.harness.tmp, "other-" + name)
        subprocess.run(
            [self.harness.real_git_bin, "clone", "-q", self.remote, other],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        git = self._git(other)
        with open(os.path.join(other, name + ".txt"), "w", encoding="utf-8") as handle:
            handle.write(name + "\n")
        git("add", "-A")
        git("commit", "-q", "-m", name)
        git("push", "-q", "origin", "master")
        return git("rev-parse", "HEAD").stdout.decode().strip()

    def _main_lines(self, result):
        out = result.stdout.decode() + result.stderr.decode()
        return [line for line in out.splitlines() if "main checkout" in line]

    def _assert_reap_ran_on(self, result):
        self.assertEqual(result.returncode, 0, result.stderr.decode())

    def test_1_behind_origin_it_fast_forwards_and_logs_one_line(self):
        old = self._head()
        new = self._advance_origin()

        result = self.harness.run("reap")

        self._assert_reap_ran_on(result)
        self.assertEqual(self._head(), new)
        lines = self._main_lines(result)
        self.assertEqual(len(lines), 1, lines)
        self.assertIn(f"{old[:7]}..{new[:7]}", lines[0])

    def test_2_a_dirty_checkout_is_refused_and_reap_still_runs(self):
        old = self._head()
        self._advance_origin()
        with open(os.path.join(self.main, ".gitignore"), "a", encoding="utf-8") as handle:
            handle.write("uncommitted\n")

        result = self.harness.run("reap")

        self._assert_reap_ran_on(result)
        self.assertEqual(self._head(), old)
        lines = self._main_lines(result)
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("uncommitted changes", lines[0])

    def test_3_untracked_files_alone_do_not_block_it(self):
        new = self._advance_origin()
        with open(os.path.join(self.main, "scratch.txt"), "w", encoding="utf-8") as handle:
            handle.write("untracked\n")

        result = self.harness.run("reap")

        self._assert_reap_ran_on(result)
        self.assertEqual(self._head(), new)

    def test_4_on_another_branch_it_is_refused(self):
        self._git(self.main)("checkout", "-q", "-b", "elsewhere")
        old = self._head()
        self._advance_origin()

        result = self.harness.run("reap")

        self._assert_reap_ran_on(result)
        self.assertEqual(self._head(), old)
        lines = self._main_lines(result)
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("'elsewhere', not master", lines[0])

    def test_4b_on_a_detached_head_it_is_refused(self):
        self._git(self.main)("checkout", "-q", "--detach", "HEAD")
        old = self._head()
        self._advance_origin()

        result = self.harness.run("reap")

        self._assert_reap_ran_on(result)
        self.assertEqual(self._head(), old)
        lines = self._main_lines(result)
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("detached HEAD", lines[0])
        self.assertTrue(lines[0].startswith("⚠"), lines[0])

    def test_5_diverged_from_origin_it_is_refused(self):
        self._advance_origin()
        git = self._git(self.main)
        with open(os.path.join(self.main, "local.txt"), "w", encoding="utf-8") as handle:
            handle.write("local only\n")
        git("add", "-A")
        git("commit", "-q", "-m", "local only")
        old = self._head()

        result = self.harness.run("reap")

        self._assert_reap_ran_on(result)
        self.assertEqual(self._head(), old)
        lines = self._main_lines(result)
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("diverged", lines[0])

    def test_6_up_to_date_it_says_nothing(self):
        old = self._head()

        result = self.harness.run("reap")

        self._assert_reap_ran_on(result)
        self.assertEqual(self._head(), old)
        self.assertEqual(self._main_lines(result), [])

    def test_7_a_failed_fetch_is_refused_and_reap_still_runs(self):
        old = self._head()
        os.rename(self.remote, self.remote + ".gone")
        self.addCleanup(os.rename, self.remote + ".gone", self.remote)

        result = self.harness.run("reap")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(self._head(), old)
        lines = self._main_lines(result)
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("fetch from origin failed", lines[0])


class DefaultBranchComesFromProjectEnv(ListVerdictTestCase):

    def setUp(self):
        super().setUp()
        self.remote = self._shared_remote()
        self.main = self.harness.fixture_repo
        with open(os.path.join(self.main, "project.env"), "w", encoding="utf-8") as handle:
            handle.write(project_env_text(DEFAULT_BRANCH="trunk"))
        git = self._git(self.main)
        git("add", "project.env")
        git("commit", "-q", "-m", "connect")
        git("branch", "-M", "trunk")
        git("push", "-q", "origin", "trunk")
        git("push", "-q", "origin", "trunk:master")
        git("fetch", "-q", "origin")

    def test_reap_fast_forwards_a_main_checkout_that_is_on_the_configured_branch(self):
        self._ready_slot("free")
        other = os.path.join(self.harness.tmp, "other")
        subprocess.run(
            [self.harness.real_git_bin, "clone", "-q", self.remote, other],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        git = self._git(other)
        with open(os.path.join(other, "upstream.txt"), "w", encoding="utf-8") as handle:
            handle.write("upstream\n")
        git("add", "-A")
        git("commit", "-q", "-m", "upstream")
        git("push", "-q", "origin", "HEAD:trunk")
        new = git("rev-parse", "HEAD").stdout.decode().strip()

        result = self.harness.run("reap")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(self._git(self.main)("rev-parse", "HEAD").stdout.decode().strip(), new)

    def test_an_unproven_branch_is_measured_against_the_configured_branch(self):
        slot = self._ready_slot("held")
        git = self._git(slot)
        git("checkout", "-q", "-b", "wip")
        with open(os.path.join(slot, "wip.txt"), "w", encoding="utf-8") as handle:
            handle.write("wip\n")
        git("add", "wip.txt")
        git("commit", "-q", "-m", "wip")
        self._write_lease("held", holder="h")

        # Only a forced release (evict) still warns by git cherry.
        result = self.harness.run("evict", "held", "--yes", "--force")

        stderr = result.stderr.decode()
        self.assertIn("wip: not proved to be in origin/trunk", stderr)
        self.assertNotIn("origin/master", stderr)

    def test_reap_keeps_a_delivered_configured_branch_and_prunes_one_named_after_the_old_default(self):
        slot = self._ready_slot("free")
        git = self._git(slot)
        git("branch", "-f", "trunk", "origin/trunk")
        git("branch", "-f", "master", "origin/trunk")

        result = self.harness.run("reap")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        heads = git("for-each-ref", "--format=%(refname:short)", "refs/heads/").stdout.decode().split()
        self.assertIn("trunk", heads)
        self.assertNotIn("master", heads)


class SlotChecksStatMinimal(ListVerdictTestCase):

    def _check_stat(self, repo):
        return subprocess.run(
            [self.harness.real_git_bin, "--git-dir", os.path.join(repo, ".git"),
             "config", "--local", "core.checkStat"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        ).stdout.decode().strip()

    def _ready_slot_with_docs_clone(self, name, index=0):
        slot = self._ready_slot(name, index=index)
        docs = os.path.join(slot, "docs", "repo")
        shutil.rmtree(docs)
        subprocess.run(
            [self.harness.real_git_bin, "clone", "-q", self.harness.docs_repo_stub, docs],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        return slot

    def test_a_fresh_slot_gets_check_stat_minimal_in_both_clones(self):
        with open(
            os.path.join(self.harness.fixture_repo, ".devcontainer", ".env"), "w", encoding="utf-8"
        ) as handle:
            handle.write(self.ENV)

        result = self.harness.run_acquire()

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        slot = self.harness.slot_dir(self.harness.acquired_slot_name(result))
        self.assertEqual(self._check_stat(slot), "minimal")
        self.assertEqual(self._check_stat(os.path.join(slot, "docs", "repo")), "minimal")


    def test_evict_sets_it_in_both_clones(self):
        self._shared_remote()
        slot = self._ready_slot_with_docs_clone("evictee", index=1)

        result = self.harness.run("evict", "evictee", "--yes")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(self._check_stat(slot), "minimal")
        self.assertEqual(self._check_stat(os.path.join(slot, "docs", "repo")), "minimal")


class ReleaseAcceptsTheAcquireShapedSlotFlag(DevContainerTestCase):
    ENV_WITH_IDENTITY = LeaseVerbs.ENV_WITH_IDENTITY

    def test_release_accepts_slot_flag_same_as_acquire(self):
        self.harness.build_fixture_repo(with_parent_env=self.ENV_WITH_IDENTITY)
        acquired = self.harness.run_acquire()
        self.assertEqual(acquired.returncode, 0, acquired.stderr.decode())
        name = self.harness.acquired_slot_name(acquired)

        result = self.harness.run("release", "--slot", name, "--for", "test")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertFalse(os.path.isfile(self.harness.lease_path(name)), result.stdout.decode())

    def test_release_accepts_the_prefixed_id_acquire_itself_prints(self):
        self.harness.build_fixture_repo(with_parent_env=self.ENV_WITH_IDENTITY)
        acquired = self.harness.run_acquire()
        self.assertEqual(acquired.returncode, 0, acquired.stderr.decode())
        name = self.harness.acquired_slot_name(acquired)
        prefixed = f"{PROJECT_PREFIX}-{name}"

        result = self.harness.run("release", prefixed, "--for", "test")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertFalse(os.path.isfile(self.harness.lease_path(name)), result.stdout.decode())


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
        self.harness.build_fixture_repo(with_parent_env=LeaseVerbs.ENV_WITH_IDENTITY)

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


class UpBringsASlotsStackUpWithOrWithoutTheSecretsOverlay(DevContainerTestCase):
    ENV_WITH_IDENTITY = LeaseVerbs.ENV_WITH_IDENTITY
    SENTINEL = "dummy-secret-value"
    OVERLAY = "docker-compose.secrets.yml"

    def _acquire(self, holder="task-20"):
        self.harness.build_fixture_repo(with_parent_env=self.ENV_WITH_IDENTITY)
        result = self.harness.run_acquire(holder=holder, owner="Session A [aaaa]")
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        name = self.harness.acquired_slot_name(result)
        self._add_overlay(name)
        return name

    def _add_overlay(self, name):
        slot = self.harness.slot_dir(name)
        with open(os.path.join(self.harness.main_devcontainer(), self.OVERLAY), "w", encoding="utf-8") as handle:
            handle.write("# fixture overlay\n")
        with open(os.path.join(slot, ".env.test.local"), "w", encoding="utf-8") as handle:
            handle.write(f"EXAMPLE_API_KEY={self.SENTINEL}\n")

    def _log(self):
        with open(self.harness.docker_log, encoding="utf-8") as handle:
            return handle.read()

    def _last_up_line(self, log):
        up_lines = [line for line in log.splitlines() if " up " in line or line.endswith(" up")]
        self.assertTrue(up_lines, log)
        return up_lines[-1]

    def test_up_without_the_flag_composes_from_the_default_files_only(self):
        self.harness.build_fixture_repo()
        self.harness.make_bare_slot(
            "probe",
            env_content="GIT_USER_NAME=demo-agent\nGIT_USER_EMAIL=old@example.com\n",
            ports_content="NOVNC_PORT=39230\nSSH_PORT=39220\nVITE_DEV_PORT=39240\n",
        )

        result = self.harness.run("up", "probe")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        line = self._last_up_line(self._log())
        self.assertIn("docker-compose.yml", line)
        self.assertNotIn(self.OVERLAY, line)

    def test_up_with_secrets_adds_the_overlay_by_path_after_the_default_file(self):
        name = self._acquire()

        result = self.harness.run("up", name, "--secrets", "--for", "task-20")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        line = self._last_up_line(self._log())
        overlay = os.path.join(self.harness.main_devcontainer(), self.OVERLAY)
        self.assertIn(f"-f {overlay}", line)
        self.assertLess(line.index("docker-compose.yml"), line.index(self.OVERLAY))

    def test_up_without_the_flag_is_the_way_back_to_the_default_stack(self):
        name = self._acquire()
        self.harness.run("up", name, "--secrets", "--for", "task-20")

        result = self.harness.run("up", name)

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertNotIn(self.OVERLAY, self._last_up_line(self._log()))

    def test_up_with_secrets_refuses_a_mismatched_holder_and_starts_nothing(self):
        name = self._acquire(holder="task-20")
        before = self._log()

        result = self.harness.run("up", name, "--secrets", "--for", "somebody-elses-issue")

        self.assertNotEqual(result.returncode, 0, result.stdout.decode())
        stderr = result.stderr.decode()
        self.assertIn("task-20", stderr)
        self.assertIn("somebody-elses-issue", stderr)
        self.assertEqual(self._log(), before, "a refused up still reached docker")

    def test_up_with_secrets_and_no_for_refuses(self):
        name = self._acquire()
        before = self._log()

        result = self.harness.run("up", name, "--secrets")

        self.assertNotEqual(result.returncode, 0, result.stdout.decode())
        self.assertIn("--for", result.stderr.decode())
        self.assertEqual(self._log(), before)

    def test_up_with_secrets_refuses_a_slot_with_no_lease(self):
        self.harness.build_fixture_repo()
        self.harness.make_bare_slot("probe", env_content=self.ENV_WITH_IDENTITY)
        self._add_overlay("probe")

        result = self.harness.run("up", "probe", "--secrets", "--for", "anyone")

        self.assertNotEqual(result.returncode, 0, result.stdout.decode())
        self.assertIn("no lease", result.stderr.decode())

    def test_up_with_secrets_refuses_when_the_slots_holder_file_is_gone(self):
        name = self._acquire()
        os.remove(self.harness.holder_path(name))
        before = self._log()

        result = self.harness.run("up", name, "--secrets", "--for", "task-20")

        self.assertNotEqual(result.returncode, 0, result.stdout.decode())
        self.assertIn(".slot-holder", result.stderr.decode())
        self.assertEqual(self._log(), before)

    def test_up_refuses_an_unknown_flag(self):
        self.harness.build_fixture_repo()

        result = self.harness.run("up", "probe", "--bogus")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--bogus", result.stderr.decode())

    def test_no_output_line_carries_a_value_from_the_overlays_env_file(self):
        name = self._acquire()

        up = self.harness.run("up", name, "--secrets", "--for", "task-20")
        listing = self.harness.run("list", "--fast")

        self.assertEqual(up.returncode, 0, up.stderr.decode())
        self.assertEqual(listing.returncode, 0, listing.stderr.decode())
        for text in (up.stdout.decode(), up.stderr.decode(), listing.stdout.decode(),
                     listing.stderr.decode(), self._log()):
            self.assertNotIn(self.SENTINEL, text)

    def test_list_names_the_slots_running_with_secrets_and_only_those(self):
        name = self._acquire()

        self.harness.run("up", name, "--secrets", "--for", "task-20")
        with_secrets = self.harness.run("list", "--fast").stdout.decode()
        self.harness.run("up", name)
        without = self.harness.run("list", "--fast").stdout.decode()

        self.assertRegex(with_secrets, rf"(?m)^\s+secrets {re.escape(name)}\b")
        self.assertNotRegex(without, r"(?m)^\s+secrets ")

    def test_list_names_the_secrets_slot_when_docker_ps_prints_far_more_than_a_pipe_holds(self):
        name = self._acquire()
        self.harness.run("up", name, "--secrets", "--for", "task-20")
        with open(os.path.join(self.harness.docker_state, "flood-config-files"), "w", encoding="utf-8"):
            pass

        listing = self.harness.run("list", "--fast").stdout.decode()

        self.assertRegex(listing, rf"(?m)^\s+secrets {re.escape(name)}\b")

    def test_up_help_says_secrets_is_main_loop_only_and_not_enforced(self):
        self.harness.build_fixture_repo()

        result = self.harness.run("up", "--help")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        stdout = result.stdout.decode()
        self.assertIn("--secrets", stdout)
        self.assertIn("main loop", stdout)
        self.assertIn("does not enforce", stdout)


class RunExecsAMechanicalCommandInASlot(DevContainerTestCase):
    ENV_WITH_IDENTITY = LeaseVerbs.ENV_WITH_IDENTITY

    def test_run_execs_the_command_and_prints_its_real_output(self):
        self.harness.build_fixture_repo(with_parent_env=self.ENV_WITH_IDENTITY)
        acquired = self.harness.run_acquire()
        self.assertEqual(acquired.returncode, 0, acquired.stderr.decode())
        name = self.harness.acquired_slot_name(acquired)

        result = self.harness.run("run", name, "--", "sh", "-c", "echo mechanical-command-output")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(result.stdout.decode().strip(), "mechanical-command-output")

    def test_run_passes_the_checkouts_mount_path_as_the_working_directory(self):
        self.harness.build_fixture_repo(with_parent_env=self.ENV_WITH_IDENTITY)
        acquired = self.harness.run_acquire()
        self.assertEqual(acquired.returncode, 0, acquired.stderr.decode())
        name = self.harness.acquired_slot_name(acquired)
        compose_file = os.path.join(self.harness.main_devcontainer(), "docker-compose.yml")
        with open(compose_file, "a", encoding="utf-8") as handle:
            handle.write("    volumes:\n      - ..:/workspace:cached\n")

        result = self.harness.run("run", name, "--", "sh", "-c", "echo hi")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        with open(self.harness.docker_log, encoding="utf-8") as handle:
            log = handle.read()
        exec_lines = [line for line in log.splitlines() if " exec " in line]
        self.assertTrue(exec_lines, log)
        self.assertIn("-w /workspace", exec_lines[-1], exec_lines[-1])

    def test_run_exits_with_the_commands_own_exit_code(self):
        self.harness.build_fixture_repo(with_parent_env=self.ENV_WITH_IDENTITY)
        acquired = self.harness.run_acquire()
        self.assertEqual(acquired.returncode, 0, acquired.stderr.decode())
        name = self.harness.acquired_slot_name(acquired)

        result = self.harness.run("run", name, "--", "sh", "-c", "exit 7")

        self.assertEqual(result.returncode, 7, result.stderr.decode())

    def test_run_refuses_a_free_slot_and_runs_nothing(self):
        self.harness.build_fixture_repo(with_parent_env=self.ENV_WITH_IDENTITY)
        self.harness.make_bare_slot("probe", env_content=self.ENV_WITH_IDENTITY)

        result = self.harness.run("run", "probe", "--", "sh", "-c", "echo should-not-run")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not leased", result.stderr.decode())
        self.assertNotIn("should-not-run", result.stdout.decode())

    def test_run_refuses_a_parked_slot_and_runs_nothing(self):
        self.harness.build_fixture_repo(with_parent_env=self.ENV_WITH_IDENTITY)
        acquired = self.harness.run_acquire(holder="task-21")
        self.assertEqual(acquired.returncode, 0, acquired.stderr.decode())
        name = self.harness.acquired_slot_name(acquired)
        with open(
            os.path.join(self.harness.slot_dir(name), "scratch.txt"), "w", encoding="utf-8"
        ) as handle:
            handle.write("uncommitted, never staged\n")
        park = self.harness.run("park", name)
        self.assertEqual(park.returncode, 0, park.stderr.decode())

        result = self.harness.run("run", name, "--", "sh", "-c", "echo should-not-run")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("parked", result.stderr.decode())
        self.assertNotIn("should-not-run", result.stdout.decode())

    def test_run_refuses_a_nonexistent_slot(self):
        self.harness.build_fixture_repo()

        result = self.harness.run("run", "slot-nope", "--", "sh", "-c", "echo should-not-run")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no slot directory", result.stderr.decode())
        self.assertNotIn("should-not-run", result.stdout.decode())

    def test_run_refuses_a_leased_slot_whose_containers_are_stopped_and_names_restart(self):
        self.harness.build_fixture_repo(with_parent_env=self.ENV_WITH_IDENTITY)
        acquired = self.harness.run_acquire()
        self.assertEqual(acquired.returncode, 0, acquired.stderr.decode())
        name = self.harness.acquired_slot_name(acquired)
        self.harness.mark_stopped(name)

        result = self.harness.run("run", name, "--", "sh", "-c", "echo should-not-run")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("restart", result.stderr.decode())
        self.assertNotIn("should-not-run", result.stdout.decode())

    def test_run_refuses_a_slot_that_is_not_this_projects_own(self):
        self.harness.build_fixture_repo(with_parent_env=self.ENV_WITH_IDENTITY)
        self.harness.make_bare_slot("probe", env_content=self.ENV_WITH_IDENTITY)
        subprocess.run(
            [
                "git", "-C", self.harness.slot_dir("probe"), "remote", "set-url", "origin",
                "https://example.invalid/somebody-elses.git",
            ],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )

        result = self.harness.run("run", "probe", "--", "sh", "-c", "echo should-not-run")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not this project's own slot", result.stderr.decode())
        self.assertNotIn("should-not-run", result.stdout.decode())

    def test_run_requires_the_double_dash_before_the_command(self):
        self.harness.build_fixture_repo(with_parent_env=self.ENV_WITH_IDENTITY)
        acquired = self.harness.run_acquire()
        self.assertEqual(acquired.returncode, 0, acquired.stderr.decode())
        name = self.harness.acquired_slot_name(acquired)

        result = self.harness.run("run", name, "sh", "-c", "echo should-not-run")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--", result.stderr.decode())
        self.assertNotIn("should-not-run", result.stdout.decode())

    def test_run_passes_help_after_the_double_dash_to_the_wrapped_command(self):
        self.harness.build_fixture_repo(with_parent_env=self.ENV_WITH_IDENTITY)
        acquired = self.harness.run_acquire()
        self.assertEqual(acquired.returncode, 0, acquired.stderr.decode())
        name = self.harness.acquired_slot_name(acquired)

        result = self.harness.run(
            "run", name, "--", "sh", "-c", 'printf "%s\\n" "$1"', "_", "--help"
        )

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(result.stdout.decode().strip(), "--help")
        self.assertNotIn("run <slot> -- <cmd>", result.stdout.decode())


class DbTargetPrintsComposeCoordinatesForAHostCaller(DevContainerTestCase):
    ENV_WITH_IDENTITY = LeaseVerbs.ENV_WITH_IDENTITY

    def test_prints_the_projects_id_and_compose_file_for_a_leased_running_slot(self):
        self.harness.build_fixture_repo(with_parent_env=self.ENV_WITH_IDENTITY)
        acquired = self.harness.run_acquire()
        self.assertEqual(acquired.returncode, 0, acquired.stderr.decode())
        name = self.harness.acquired_slot_name(acquired)
        compose_file = os.path.join(self.harness.main_devcontainer(), "docker-compose.yml")

        result = self.harness.run("db-target", name)

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        out = result.stdout.decode()
        self.assertIn(f"DB_RESTORE_PROJECT=demo-{name}", out)
        self.assertIn("DB_RESTORE_COMPOSE_ARGS=", out)
        self.assertIn(compose_file, out)
        self.assertNotIn("exec", out)

    def test_output_is_safe_to_eval_and_names_the_right_project(self):
        self.harness.build_fixture_repo(with_parent_env=self.ENV_WITH_IDENTITY)
        acquired = self.harness.run_acquire()
        self.assertEqual(acquired.returncode, 0, acquired.stderr.decode())
        name = self.harness.acquired_slot_name(acquired)
        compose_file = os.path.join(self.harness.main_devcontainer(), "docker-compose.yml")

        result = self.harness.run("db-target", name)
        self.assertEqual(result.returncode, 0, result.stderr.decode())

        script = (
            'eval "$(cat)"; '
            'echo "PROJECT=$DB_RESTORE_PROJECT"; '
            'echo "ARGS=${DB_RESTORE_COMPOSE_ARGS[*]}"'
        )
        evaluated = subprocess.run(
            ["bash", "-c", script],
            input=result.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
        out = evaluated.stdout.decode()
        self.assertIn(f"PROJECT=demo-{name}", out)
        self.assertIn(f"-f {compose_file}", out)

    def test_refuses_a_free_slot_and_prints_nothing(self):
        self.harness.build_fixture_repo(with_parent_env=self.ENV_WITH_IDENTITY)
        self.harness.make_bare_slot("probe", env_content=self.ENV_WITH_IDENTITY)

        result = self.harness.run("db-target", "probe")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not leased", result.stderr.decode())
        self.assertEqual(result.stdout.decode(), "")

    def test_refuses_a_slot_whose_containers_are_stopped_and_names_restart(self):
        self.harness.build_fixture_repo(with_parent_env=self.ENV_WITH_IDENTITY)
        acquired = self.harness.run_acquire()
        self.assertEqual(acquired.returncode, 0, acquired.stderr.decode())
        name = self.harness.acquired_slot_name(acquired)
        self.harness.mark_stopped(name)

        result = self.harness.run("db-target", name)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("restart", result.stderr.decode())
        self.assertEqual(result.stdout.decode(), "")

    def test_refuses_a_nonexistent_slot(self):
        self.harness.build_fixture_repo()

        result = self.harness.run("db-target", "slot-nope")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no slot directory", result.stderr.decode())
        self.assertEqual(result.stdout.decode(), "")

    def test_refuses_a_slot_that_is_not_this_projects_own(self):
        self.harness.build_fixture_repo(with_parent_env=self.ENV_WITH_IDENTITY)
        self.harness.make_bare_slot("probe", env_content=self.ENV_WITH_IDENTITY)
        subprocess.run(
            [
                "git", "-C", self.harness.slot_dir("probe"), "remote", "set-url", "origin",
                "https://example.invalid/somebody-elses.git",
            ],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )

        result = self.harness.run("db-target", "probe")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not this project's own slot", result.stderr.decode())
        self.assertEqual(result.stdout.decode(), "")

    def _relocate_harness_tmp_with_space(self):
        old_tmp = self.harness.tmp
        new_tmp = old_tmp + " with a space"
        os.rename(old_tmp, new_tmp)

        def relocate(path):
            self.assertTrue(path.startswith(old_tmp), path)
            return new_tmp + path[len(old_tmp):]

        self.harness.tmp = new_tmp
        self.harness.fixture_repo = relocate(self.harness.fixture_repo)
        self.harness.docker_log = relocate(self.harness.docker_log)
        self.harness.git_call_log = relocate(self.harness.git_call_log)
        self.harness.state_dir = relocate(self.harness.state_dir)
        self.harness.docker_state = relocate(self.harness.docker_state)
        self.harness.stub_bin = relocate(self.harness.stub_bin)
        self.harness.docs_repo_stub = relocate(self.harness.docs_repo_stub)

    def test_a_slot_root_containing_a_space_survives_the_eval_round_trip(self):
        self._relocate_harness_tmp_with_space()
        self.harness.build_fixture_repo(with_parent_env=self.ENV_WITH_IDENTITY)
        acquired = self.harness.run_acquire()
        self.assertEqual(acquired.returncode, 0, acquired.stderr.decode())
        name = self.harness.acquired_slot_name(acquired)
        compose_file = os.path.join(self.harness.main_devcontainer(), "docker-compose.yml")
        self.assertIn(" ", compose_file, "fixture didn't actually land under a spaced path")

        result = self.harness.run("db-target", name)
        self.assertEqual(result.returncode, 0, result.stderr.decode())

        script = (
            'eval "$(cat)"; '
            'for a in "${DB_RESTORE_COMPOSE_ARGS[@]}"; do printf "%s\\n" "$a"; done'
        )
        evaluated = subprocess.run(
            ["bash", "-c", script],
            input=result.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
        argv_elements = evaluated.stdout.decode().splitlines()
        self.assertIn(compose_file, argv_elements)
        self.assertEqual(argv_elements[argv_elements.index(compose_file) - 1], "-f", argv_elements)


class MachineCapFromLimitsFile(DevContainerTestCase):
    ENV = "GIT_USER_NAME=demo-agent\nGIT_USER_EMAIL=a@example.com\n"
    OWNER = "Agent [aaa]"
    CEILING = 20

    def _write_limits(self, limits):
        path = os.path.join(self.harness.state_dir, "limits.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(limits, handle)

    def _foreign_lease(self, project):
        path = os.path.join(self.harness.state_dir, "leases", f"{project}.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "holder": "someone-else",
                    "project": project,
                    "since": int(time.time()) - 60,
                    "ttl_seconds": 14400,
                    "key": "/dev/null",
                    "ssh_port": 39229,
                    "owner": "-",
                },
                handle,
            )
        return path

    def _fill(self, count):
        return [self._foreign_lease(f"othproj{i}-slot-{i}") for i in range(1, count + 1)]

    def _lease_names(self):
        return sorted(os.listdir(os.path.join(self.harness.state_dir, "leases")))

    def _acquire(self, holder="newcomer", *extra, **kwargs):
        return self.harness.run(
            "acquire", "--for", holder, "--as", self.OWNER, *extra, **kwargs
        )

    def _identity_repo(self):
        self.harness.build_fixture_repo(with_parent_env=self.ENV)

    def _assert_refused(self, result, count, cap):
        out = "STDOUT:\n" + result.stdout.decode() + "\nSTDERR:\n" + result.stderr.decode()
        self.assertNotEqual(result.returncode, 0, out)
        self.assertIn(f"{count}/{cap}", result.stderr.decode(), out)


    def test_two_leases_of_any_prefix_at_machine_max_2_refuse_naming_the_count_and_limits_file(self):
        self._identity_repo()
        self._write_limits({"machine_max": 2})
        self._foreign_lease("alpha-slot-1")
        self._foreign_lease("beta-slot-7")

        result = self._acquire()

        self._assert_refused(result, 2, 2)
        self.assertIn("limits.json", result.stderr.decode())
        self.assertEqual(self._lease_names(), ["alpha-slot-1.json", "beta-slot-7.json"])
        self.assertFalse(os.path.isdir(self.harness.slot_dir("slot-1")))


    def test_with_no_limits_file_the_cap_is_20(self):
        self._identity_repo()
        leases = self._fill(self.CEILING)

        result = self.harness.run("acquire", "--for", "newcomer")

        self._assert_refused(result, 20, 20)
        os.remove(leases[0])
        result = self.harness.run("acquire", "--for", "newcomer")
        self.assertEqual(result.returncode, 0, result.stderr.decode())

    def test_with_a_limits_file_lacking_machine_max_the_cap_is_20(self):
        self._identity_repo()
        self._write_limits({"default": 5})
        leases = self._fill(self.CEILING)

        result = self._acquire()

        self._assert_refused(result, 20, 20)
        os.remove(leases[0])
        result = self._acquire()
        self.assertEqual(result.returncode, 0, result.stderr.decode())


    def test_machine_max_0_is_clamped_to_1(self):
        self._identity_repo()
        self._write_limits({"machine_max": 0})
        self._foreign_lease("alpha-slot-1")

        result = self._acquire()

        self._assert_refused(result, 1, 1)

    def test_machine_max_25_is_clamped_to_20(self):
        self._identity_repo()
        self._write_limits({"machine_max": 25})
        self._fill(self.CEILING)

        result = self._acquire()

        self._assert_refused(result, 20, 20)

    def test_a_non_number_machine_max_is_20(self):
        self._identity_repo()
        self._write_limits({"machine_max": "lots"})
        self._fill(self.CEILING)

        result = self._acquire()

        self._assert_refused(result, 20, 20)

    def _assert_one_line_warning(self, value):
        self._identity_repo()
        self._write_limits({"machine_max": value})

        result = self._acquire()

        stderr = result.stderr.decode()
        self.assertEqual(result.returncode, 0, stderr)
        warned = [line for line in stderr.splitlines() if "machine_max" in line]
        self.assertEqual(len(warned), 1, f"expected a one-line warning for {value!r}:\n{stderr}")

    def test_machine_max_0_warns_in_one_line(self):
        self._assert_one_line_warning(0)

    def test_machine_max_25_warns_in_one_line(self):
        self._assert_one_line_warning(25)

    def test_a_non_number_machine_max_warns_in_one_line(self):
        self._assert_one_line_warning("lots")

    def _assert_one_warning_on_a_refused_acquire(self, value, leases):
        self._identity_repo()
        self._write_limits({"machine_max": value})
        self._fill(leases)

        result = self._acquire()

        stderr = result.stderr.decode()
        self.assertNotEqual(result.returncode, 0, stderr)
        warned = [line for line in stderr.splitlines() if line.startswith("warning:") and "machine_max in" in line]
        self.assertEqual(len(warned), 1, f"expected one warning for {value!r} on a refusal: {stderr}")

    def test_machine_max_25_warns_once_on_a_refused_acquire(self):
        self._assert_one_warning_on_a_refused_acquire(25, self.CEILING)

    def test_a_non_number_machine_max_warns_once_on_a_refused_acquire(self):
        self._assert_one_warning_on_a_refused_acquire("lots", self.CEILING)

    def test_machine_max_0_warns_once_on_a_refused_acquire(self):
        self._assert_one_warning_on_a_refused_acquire(0, 1)

    def test_an_in_range_machine_max_does_not_warn(self):
        self._identity_repo()
        self._write_limits({"machine_max": 2})

        result = self._acquire()

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertNotIn("machine_max", result.stderr.decode())


    def test_a_leased_slot_whose_containers_are_stopped_counts_against_the_cap(self):
        self._identity_repo()
        self._write_limits({"machine_max": 1})
        first = self._acquire("first")
        self.assertEqual(first.returncode, 0, first.stderr.decode())
        slot = self.harness.acquired_slot_name(first)
        self.harness.mark_stopped(slot)

        result = self._acquire("second")

        self._assert_refused(result, 1, 1)
        self.assertTrue(os.path.isfile(self.harness.lease_path(slot)))
        self.assertFalse(os.path.isdir(self.harness.slot_dir("slot-2")))


    def test_under_the_cap_a_new_slot_is_created(self):
        self._identity_repo()
        self._write_limits({"machine_max": 2})
        self._foreign_lease("alpha-slot-1")

        result = self._acquire()

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(self.harness.acquired_slot_name(result), "slot-2", "another project's live lease holds number 1")
        self.assertTrue(os.path.isfile(self.harness.lease_path("slot-2")))


    def test_a_new_directory_index_is_bounded_by_20_not_by_machine_max(self):
        self._identity_repo()
        self._write_limits({"machine_max": 3})
        for i in (1, 2, 3):
            self.harness.make_bare_slot(f"slot-{i}", built=False)

        result = self._acquire()

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(self.harness.acquired_slot_name(result), "slot-4")


    def test_wait_at_the_cap_waits_and_proceeds_once_a_lease_is_released(self):
        self._identity_repo()
        self._write_limits({"machine_max": 1})
        lease = self._foreign_lease("alpha-slot-1")
        log = os.path.join(self.harness.tmp, "waiter.log")
        proc = self.harness.spawn(
            "acquire", "--for", "waiter", "--as", self.OWNER, "--wait", log=log
        )

        def stop():
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=30)
        self.addCleanup(stop)

        def read():
            with open(log, encoding="utf-8") as handle:
                return handle.read()

        deadline = time.monotonic() + 60
        while "1/1" not in read():
            if time.monotonic() > deadline or proc.poll() is not None:
                raise AssertionError("never reported waiting at 1/1:\n" + read())
            time.sleep(0.2)
        self.assertIn("limits.json", read())
        self.assertIsNone(proc.poll(), "the waiter gave up instead of waiting:\n" + read())

        os.remove(lease)

        deadline = time.monotonic() + 90
        while proc.poll() is None:
            self.assertLess(time.monotonic(), deadline, "the waiter never took the freed capacity:\n" + read())
            time.sleep(0.2)
        self.assertEqual(proc.returncode, 0, read())
        self.assertIn("leased to waiter", read())


    def test_slot_max_in_the_environment_does_not_lower_the_cap(self):
        self._identity_repo()
        first = self._acquire("first")
        self.assertEqual(first.returncode, 0, first.stderr.decode())

        second = self._acquire("second", port_overrides={"SLOT_MAX": "1"})

        self.assertEqual(second.returncode, 0, second.stderr.decode())
        self.assertEqual(self.harness.acquired_slot_name(second), "slot-2")

    def test_slot_max_in_the_environment_does_not_raise_the_cap(self):
        self._identity_repo()
        self._write_limits({"machine_max": 2})
        self._foreign_lease("alpha-slot-1")
        self._foreign_lease("beta-slot-7")

        result = self._acquire(port_overrides={"SLOT_MAX": "100"})

        self._assert_refused(result, 2, 2)


class AcquireLockScope(DevContainerTestCase):
    ENV_WITH_IDENTITY = "GIT_USER_NAME=demo-agent\nGIT_USER_EMAIL=demo-agent@example.com\n"

    def _reap_on_failure(self, proc):
        def stop():
            for held in os.listdir(self.harness.docker_state):
                if held.endswith(".hold-up"):
                    os.remove(os.path.join(self.harness.docker_state, held))
            if proc.poll() is None:
                proc.kill()
            proc.communicate(timeout=30)
        self.addCleanup(stop)

    def _lease(self, name):
        with open(self.harness.lease_path(name), encoding="utf-8") as handle:
            return json.load(handle)

    def test_wait_outlasts_a_lock_held_past_30s_and_plain_acquire_names_it_busy(self):
        self.harness.build_fixture_repo(with_parent_env=self.ENV_WITH_IDENTITY)
        lock = os.path.join(self.harness.state_dir, "lock")
        os.mkdir(lock)
        self.addCleanup(lambda: os.path.isdir(lock) and os.rmdir(lock))

        waiting = self.harness.spawn("acquire", "--for", "waiter", "--wait")
        self._reap_on_failure(waiting)
        plain = self.harness.spawn("acquire", "--for", "plain")
        self._reap_on_failure(plain)

        _, plain_err = plain.communicate(timeout=90)
        self.assertNotEqual(plain.returncode, 0)
        self.assertIn(lock, plain_err.decode())
        self.assertIn("busy", plain_err.decode())

        time.sleep(4)
        self.assertIsNone(waiting.poll(), "acquire --wait gave up on a busy lock")
        os.rmdir(lock)

        out, err = waiting.communicate(timeout=90)
        self.assertEqual(waiting.returncode, 0, err.decode())
        self.assertIn("leased to waiter", out.decode())

    def test_a_second_acquire_is_not_held_up_by_a_slot_being_created(self):
        self.harness.build_fixture_repo(with_parent_env=self.ENV_WITH_IDENTITY)
        self.harness.hold_compose_up("slot-1")
        first = self.harness.spawn("acquire", "--for", "first")
        self._reap_on_failure(first)
        self.harness.wait_until_compose_up_held("slot-1")

        started = time.monotonic()
        second = self.harness.run_acquire(holder="second")
        elapsed = time.monotonic() - started

        self.assertEqual(second.returncode, 0, second.stderr.decode())
        self.assertLess(elapsed, 10, "the second acquire waited on the first one's creation")
        self.assertNotEqual(self.harness.acquired_slot_name(second), "slot-1")

        self.harness.let_compose_up_go("slot-1")
        out, err = first.communicate(timeout=90)
        self.assertEqual(first.returncode, 0, err.decode())
        self.assertIn("leased to first", out.decode())
        self.assertEqual(self._lease("slot-1")["holder"], "first")

    def test_a_failed_creation_leaves_no_reservation_behind(self):
        self.harness.build_fixture_repo(with_parent_env=self.ENV_WITH_IDENTITY)
        self.harness.hold_compose_up("slot-1", then_fail=True)
        first = self.harness.spawn("acquire", "--for", "first")
        self._reap_on_failure(first)
        self.harness.wait_until_compose_up_held("slot-1")
        self.assertEqual(self._lease("slot-1")["holder"], "first")

        self.harness.let_compose_up_go("slot-1")
        first.communicate(timeout=90)

        self.assertNotEqual(first.returncode, 0)
        self.assertFalse(os.path.exists(self.harness.lease_path("slot-1")))
        self.assertFalse(os.path.isdir(os.path.join(self.harness.state_dir, "lock")))

    def test_a_reservation_whose_acquire_was_killed_is_dropped_by_the_next_acquire(self):
        self.harness.build_fixture_repo(with_parent_env=self.ENV_WITH_IDENTITY)
        self.harness.hold_compose_up("slot-1")
        first = self.harness.spawn("acquire", "--for", "first")
        self._reap_on_failure(first)
        self.harness.wait_until_compose_up_held("slot-1")
        self.assertTrue(os.path.isfile(self.harness.lease_path("slot-1")))

        first.kill()
        self.harness.let_compose_up_go("slot-1")
        first.communicate(timeout=30)

        second = self.harness.run_acquire(holder="second")

        self.assertEqual(second.returncode, 0, second.stderr.decode())
        self.assertFalse(os.path.exists(self.harness.lease_path("slot-1")))
        self.assertIn("reservation", second.stderr.decode())


class AcquireWaitsForAFullPool(DevContainerTestCase):
    ENV_WITH_IDENTITY = "GIT_USER_NAME=demo-agent\nGIT_USER_EMAIL=demo-agent@example.com\n"
    OWNER = "Agent [aaa]"

    def setUp(self):
        super().setUp()
        self.harness.build_fixture_repo(with_parent_env=self.ENV_WITH_IDENTITY)
        with open(os.path.join(self.harness.state_dir, "limits.json"), "w", encoding="utf-8") as handle:
            json.dump({"machine_max": 1}, handle)
        taken = self.harness.run("acquire", "--for", "holder", "--as", self.OWNER)
        self.assertEqual(taken.returncode, 0, taken.stderr.decode())
        self.assertEqual(self.harness.acquired_slot_name(taken), "slot-1")

    def _spawn_waiter(self, holder):
        log = os.path.join(self.harness.tmp, f"{holder}.log")
        proc = self.harness.spawn(
            "acquire", "--for", holder, "--as", self.OWNER, "--wait", log=log
        )

        def stop():
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=30)
        self.addCleanup(stop)
        return proc, log

    @staticmethod
    def _read(log):
        with open(log, encoding="utf-8") as handle:
            return handle.read()

    def _wait_for_text(self, log, text, timeout=90):
        deadline = time.monotonic() + timeout
        while text not in self._read(log):
            if time.monotonic() > deadline:
                raise AssertionError(f"never printed {text!r}:\n{self._read(log)}")
            time.sleep(0.2)

    def test_plain_acquire_on_a_full_pool_says_how_to_wait_for_one(self):
        result = self.harness.run("acquire", "--for", "late", "--as", self.OWNER)

        self.assertEqual(result.returncode, 2, result.stderr.decode())
        self.assertIn(
            "run 'acquire --wait --for late' (e.g. in the background) to wait for one",
            result.stderr.decode(),
        )

    def test_wait_with_a_timeout_gives_up_when_nothing_frees(self):
        started = time.monotonic()
        result = self.harness.run(
            "acquire", "--for", "late", "--as", self.OWNER, "--wait", "--timeout", "5s"
        )
        elapsed = time.monotonic() - started

        self.assertEqual(result.returncode, 3, result.stderr.decode())
        self.assertIn("no slot became free within 5s", result.stderr.decode())
        self.assertLess(elapsed, 30)
        self.assertFalse(os.path.exists(self.harness.lease_path("slot-2")))

    def test_a_freed_slot_wakes_the_waiters_and_exactly_one_takes_it(self):
        a, a_log = self._spawn_waiter("waiter-a")
        b, b_log = self._spawn_waiter("waiter-b")
        self._wait_for_text(a_log, "1/1")
        self._wait_for_text(b_log, "1/1")
        self.assertIn("limits.json", self._read(a_log))

        released = self.harness.run("release", "slot-1", "--for", "holder")
        self.assertEqual(released.returncode, 0, released.stderr.decode())
        freed_at = time.monotonic()

        deadline = freed_at + 60
        while a.poll() is None and b.poll() is None:
            self.assertLess(time.monotonic(), deadline, "neither waiter took the freed slot")
            time.sleep(0.2)
        self.assertLess(time.monotonic() - freed_at, 20)

        winner, winner_log, loser = (a, a_log, b) if a.poll() is not None else (b, b_log, a)
        self.assertEqual(winner.returncode, 0, self._read(winner_log))
        holder = "waiter-a" if winner is a else "waiter-b"
        self.assertIn(f"leased to {holder}", self._read(winner_log))
        with open(self.harness.lease_path("slot-1"), encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["holder"], holder)

        time.sleep(5)
        self.assertIsNone(loser.poll(), "the second waiter stopped instead of waiting")


class AcquireAlwaysBuildsAFreshSlot(DevContainerTestCase):
    ENV = "GIT_USER_NAME=demo-agent\nGIT_USER_EMAIL=demo-agent@example.com\n"
    OWNER = "Agent [aaa]"

    def _plant_idle_stamp(self, name):
        stamp_dir = os.path.join(self.harness.state_dir, "idle")
        os.makedirs(stamp_dir, exist_ok=True)
        path = os.path.join(stamp_dir, f"{PROJECT_PREFIX}-{name}")
        open(path, "w", encoding="utf-8").close()
        return path

    def _leave_a_slot_behind(self):
        self.harness.build_fixture_repo(with_parent_env=self.ENV)
        first = self.harness.run_acquire(holder="first")
        self.assertEqual(first.returncode, 0, first.stderr.decode())
        name = self.harness.acquired_slot_name(first)
        self.harness.free_slot(name, stopped=True)
        self._plant_idle_stamp(name)
        return name

    def _stub_ssh_keygen(self):
        log = os.path.join(self.harness.tmp, "ssh-keygen.log")
        real = shutil.which("ssh-keygen")
        self.assertTrue(real, "no real ssh-keygen on PATH")
        self.harness._write_stub(
            "ssh-keygen",
            "#!/usr/bin/env bash\n"
            'if [[ "$1" == "-R" ]]; then\n'
            f'  printf "%s\n" "$*" >> "{log}"\n'
            "  exit 0\n"
            "fi\n"
            f'exec "{real}" "$@"\n',
        )
        return log

    def test_a_stale_directory_is_not_reused_and_a_free_index_is_built(self):
        stale = self._leave_a_slot_behind()

        result = self.harness.run_acquire(holder="second")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        fresh = self.harness.acquired_slot_name(result)
        self.assertNotEqual(fresh, stale, "acquire handed out the stale directory again")
        self.assertEqual(fresh, "slot-2")
        self.assertTrue(os.path.isdir(self.harness.slot_dir(stale)), "the stale directory was touched")
        self.assertFalse(os.path.isfile(self.harness.lease_path(stale)), "the stale directory was leased")

    def test_at_the_cap_a_stale_directory_is_refused_not_reused(self):
        stale = self._leave_a_slot_behind()
        with open(os.path.join(self.harness.state_dir, "limits.json"), "w", encoding="utf-8") as handle:
            json.dump({"machine_max": 1}, handle)
        foreign = os.path.join(self.harness.state_dir, "leases", "alpha-slot-7.json")
        with open(foreign, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "holder": "someone-else",
                    "project": "alpha-slot-7",
                    "since": int(time.time()) - 60,
                    "ttl_seconds": 14400,
                    "key": "/dev/null",
                    "ssh_port": 39229,
                    "owner": "-",
                },
                handle,
            )

        result = self.harness.run("acquire", "--for", "second", "--as", self.OWNER)

        self.assertNotEqual(result.returncode, 0, result.stdout.decode())
        self.assertIn("1/1", result.stderr.decode())
        self.assertFalse(os.path.isfile(self.harness.lease_path(stale)), "the stale directory was leased")
        self.assertFalse(os.path.isdir(self.harness.slot_dir("slot-2")), "a slot was built past the cap")

    def test_slot_is_rejected_as_an_unknown_option(self):
        stale = self._leave_a_slot_behind()

        result = self.harness.run("acquire", "--for", "second", "--slot", stale)

        self.assertNotEqual(result.returncode, 0, result.stdout.decode())
        self.assertIn("--slot", result.stderr.decode())
        self.assertFalse(os.path.isfile(self.harness.lease_path(stale)), "--slot leased the named directory")

    def test_acquire_does_not_touch_known_hosts_inside_the_engine(self):
        log = self._stub_ssh_keygen()
        self.harness.build_fixture_repo(with_parent_env=self.ENV)

        result = self.harness.run_acquire(holder="first")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        calls = open(log, encoding="utf-8").read().splitlines() if os.path.isfile(log) else []
        self.assertEqual(calls, [], "acquire ran ssh-keygen -R in the engine, whose known_hosts is not the caller's")

    def test_a_fresh_slot_prints_the_host_key_step_for_its_ssh_port(self):
        self.harness.build_fixture_repo(with_parent_env=self.ENV)

        result = self.harness.run_acquire(holder="first")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        name = self.harness.acquired_slot_name(result)
        port = self.harness.read_env(name)["SSH_PORT"]
        text = (result.stdout + result.stderr).decode()
        self.assertIn(f"ssh-keygen -R '[localhost]:{port}'", text)

    def test_nothing_creates_the_idle_state_directory(self):
        self.harness.build_fixture_repo(with_parent_env=self.ENV)
        first = self.harness.run_acquire(holder="first")
        self.assertEqual(first.returncode, 0, first.stderr.decode())
        name = self.harness.acquired_slot_name(first)

        for args in (["list"], ["release", name, "--for", "first"], ["reap"], ["list"]):
            result = self.harness.run(*args)
            self.assertEqual(result.returncode, 0, f"{args}: {result.stderr.decode()}")

        self.assertFalse(
            os.path.exists(os.path.join(self.harness.state_dir, "idle")),
            "a verb created $STATE/idle",
        )

    def test_reap_leaves_a_planted_idle_stamp_alone(self):
        self.harness.build_fixture_repo(with_parent_env=self.ENV)
        self.harness.make_bare_slot("slot-1", env_content=self.ENV, local_identity=("demo-agent", "a@example.com"))
        stamp = self._plant_idle_stamp("slot-1")

        result = self.harness.run("reap")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertTrue(os.path.isfile(stamp), "reap removed a stamp it should not know about")

    def test_the_script_has_no_code_naming_the_idle_state_directory(self):
        self.harness.build_fixture_repo()
        code = [line for line in engine_source_lines(self.harness.fixture_repo) if not line.lstrip().startswith("#")]
        offenders = [line.strip() for line in code if "STATE/idle" in line or "idle_stamp" in line]
        self.assertEqual(offenders, [], "code still reads or writes $STATE/idle")

    def test_no_comment_names_the_acquire_slot_flag_or_scan_steps(self):
        self.harness.build_fixture_repo()
        stale = re.compile(
            r"acquire.{0,20}--slot"
            r"|try_acquire_once step"
            r"|by `?--slot`? \(which refuses"
        )
        offenders = [
            f"{number}: {line.strip()}"
            for number, line in enumerate(engine_source_lines(self.harness.fixture_repo), 1)
            if line.lstrip().startswith("#") and stale.search(line)
        ]
        self.assertEqual(offenders, [], "comments describe an acquire --slot / scan step that no longer exists")


class AFreshSlotGetsAFreshImage(DevContainerTestCase):
    ENV = "GIT_USER_NAME=demo-agent\nGIT_USER_EMAIL=demo-agent@example.com\n"

    ORPHAN_AWARE_DOCKER = """#!/usr/bin/env bash
if [[ "${1:-}" == ps && "$*" == *working_dir* ]]; then
  for arg in "$@"; do
    case "$arg" in
      label=com.docker.compose.project=*)
        p="${arg#label=com.docker.compose.project=}"
        [[ -f "$DOCKER_STUB_STATE/$p.workdir" ]] && cat "$DOCKER_STUB_STATE/$p.workdir"
        ;;
    esac
  done
  exit 0
fi
exec "$(dirname "$0")/docker-real" "$@"
"""

    LABELLED_IMAGE_DOCKER = """#!/usr/bin/env bash
if [[ "${1:-}" == image && "${2:-}" == ls ]]; then
  echo "$(cat "$DOCKER_STUB_STATE/image.project")-app:latest"
  exit 0
fi
if [[ "${1:-}" == image && "${2:-}" == inspect ]]; then
  cat "$DOCKER_STUB_STATE/image.hash"
  exit 0
fi
exec "$(dirname "$0")/docker-real" "$@"
"""

    def _calls(self, project):
        with open(self.harness.docker_log, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
        return [
            line for line in lines
            if line.startswith("ARGS:")
            and (f"-p {project} " in line or f"COMPOSE_PROJECT_NAME={project} " in line)
        ]

    @staticmethod
    def _is_up(line):
        return re.search(r"\bup\b", line.split("|")[0]) is not None

    @staticmethod
    def _builds(line):
        args = line.split("|")[0]
        return "--build" in args or re.search(r"\bcompose\b.*\bbuild\b", args) is not None

    def _assert_built_before_the_stack_ran(self, project):
        calls = self._calls(project)
        ups = [i for i, line in enumerate(calls) if self._is_up(line)]
        self.assertTrue(ups, "no compose up ran:\n" + "\n".join(calls))
        built = [i for i, line in enumerate(calls) if self._builds(line)]
        ok = bool(built) and built[0] <= ups[0]
        self.assertTrue(ok, "the image was not built before the stack ran:\n" + "\n".join(calls))

    def _assert_image_removed(self, project):
        calls = self._calls(project)
        with open(self.harness.docker_log, encoding="utf-8") as handle:
            everything = handle.read().splitlines()
        down_rmi = [
            line for line in calls
            if re.search(r"\bdown\b", line.split("|")[0])
            and re.search(r"--rmi[ =]local", line.split("|")[0])
        ]
        image_rm = [
            line for line in everything
            if re.search(rf"\b(image rm|rmi)\b.*\b{re.escape(project)}-app\b", line.split("|")[0])
        ]
        self.assertTrue(
            down_rmi or image_rm,
            f"teardown left {project}-app behind:\n" + "\n".join(everything),
        )

    def _acquire(self):
        self.harness.build_fixture_repo(with_parent_env=self.ENV)
        result = self.harness.run_acquire(holder="first")
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        return self.harness.acquired_slot_name(result)

    def test_acquire_builds_the_image_before_the_stack_runs(self):
        name = self._acquire()

        self._assert_built_before_the_stack_ran(f"{PROJECT_PREFIX}-{name}")

    def test_release_removes_the_slots_image_with_its_containers(self):
        name = self._acquire()

        result = self.harness.run("release", name, "--for", "first")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self._assert_image_removed(f"{PROJECT_PREFIX}-{name}")

    def test_down_removes_the_slots_image_with_its_containers(self):
        name = self._acquire()
        self.harness.free_slot(name, stopped=True)

        result = self.harness.run("down", name)

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self._assert_image_removed(f"{PROJECT_PREFIX}-{name}")

    def test_reap_keeps_the_directory_of_an_orphan_with_a_foreign_origin(self):
        name = self._acquire()
        project = f"{PROJECT_PREFIX}-{name}"
        slot = self.harness.slot_dir(name)
        self.harness.free_slot(name)
        subprocess.run(
            [self.harness.real_git_bin, "-C", slot, "remote", "set-url", "origin", "/nonexistent/other.git"],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self.harness._write_stub("docker-real", DOCKER_STUB)
        self.harness._write_stub("docker", self.ORPHAN_AWARE_DOCKER)
        with open(os.path.join(self.harness.docker_state, f"{project}.workdir"), "w", encoding="utf-8") as handle:
            handle.write(os.path.join(slot, ".devcontainer") + "\n")

        result = self.harness.run("reap")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertIn("refusing to remove", result.stderr.decode())
        self.assertTrue(os.path.isdir(slot), "reap removed a directory that is not this project's checkout")

    def test_restart_of_a_leased_slot_does_not_build(self):
        name = self._acquire()
        project = f"{PROJECT_PREFIX}-{name}"
        # the image carries the hash the engine itself computes: the commit plus the compose dir
        head = subprocess.run(
            ["bash", "-c",
             'source "$1/lib/images.sh"; repo_root=$1 COMPOSE_DIR=.devcontainer; slot_build_hash',
             "bash", self.harness.fixture_repo],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        ).stdout.decode().strip()
        for fname, content in (("image.project", project), ("image.hash", head)):
            with open(os.path.join(self.harness.docker_state, fname), "w", encoding="utf-8") as handle:
                handle.write(content + chr(10))
        self.harness._write_stub("docker-real", DOCKER_STUB)
        self.harness._write_stub("docker", self.LABELLED_IMAGE_DOCKER)
        before = len(self._calls(project))

        result = self.harness.run("restart", name)

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        after = self._calls(project)[before:]
        self.assertTrue(any(self._is_up(line) for line in after), "restart never ran compose up")
        self.assertEqual([line for line in after if self._builds(line)], [], "restart built an image")


class ComposeFromTheMainCheckout(DevContainerTestCase):
    ENV = (
        "GIT_USER_NAME=demo-agent\nGIT_USER_EMAIL=demo-agent@example.com\n"
        "SSH_PUBLIC_KEY=ssh-ed25519 AAAAMAINKEY main@host\n"
        "FIREWALL_EXTRA_DOMAINS=seed.example.invalid\n"
    )

    @staticmethod
    def _real(path):
        return os.path.realpath(path)

    SEEDED_KEYS = project_env_text(SLOT_ENV_KEYS="FIREWALL_EXTRA_DOMAINS")

    def _acquire(self):
        self.harness.build_fixture_repo(with_parent_env=self.ENV, project_env=self.SEEDED_KEYS)
        result = self.harness.run_acquire(holder="task-22", owner="Agent [aaaa]")
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        return self.harness.acquired_slot_name(result)

    def _forget_compose_calls(self):
        if os.path.isfile(self.harness.compose_log):
            os.remove(self.harness.compose_log)

    def _main_env(self):
        return os.path.join(self.harness.main_devcontainer(), ".env")

    def _slot_env_in_checkout(self, name):
        return os.path.join(self.harness.slot_dir(name), ".devcontainer", ".env")

    def _env_text(self, name):
        self.assertTrue(os.path.isfile(self.harness.env_path(name)), f"no env file for '{name}' under $DEV_CONTAINER_HOME/env/")
        with open(self.harness.env_path(name), encoding="utf-8") as handle:
            return handle.read()

    def _assert_composed_from_main(self, calls, name, verbs=()):
        self.assertTrue(calls, "no compose call was made")
        seen = {call["verb"] for call in calls}
        self.assertTrue(set(verbs) <= seen, f"expected compose verbs {sorted(verbs)}, saw {sorted(seen)}")
        main = self._real(self.harness.main_devcontainer())
        env_file = self._real(self.harness.env_path(name))
        workspace = self._real(self.harness.slot_dir(name))
        for call in calls:
            if call["verb"] == "ls":
                continue
            where = f"{call['verb']}: {call}"
            self.assertEqual(self._real(call["project_dir"]), main, "project directory is not main's .devcontainer/ -- " + where)
            self.assertTrue(call["files"], "no -f file -- " + where)
            overlay = self._real(os.path.join(self.harness.state_dir, "overlays", f"{PROJECT_PREFIX}-{name}.yml"))
            volume_overlay = self._real(os.path.join(self.harness.state_dir, "compose", "slot-volume.yml"))
            for path in call["files"]:
                if self._real(path) in (overlay, volume_overlay):
                    continue
                self.assertEqual(self._real(os.path.dirname(path)), main, "compose file is not main's -- " + where)
            self.assertTrue(call["env_file"], "no --env-file -- " + where)
            self.assertEqual(self._real(call["env_file"]), env_file, "env file is not the slot's state env -- " + where)
            self.assertTrue(call["workspace"], "no WORKSPACE_DIR -- " + where)
            if any(self._real(p) == volume_overlay for p in call["files"]):
                self.assertEqual(call["workspace"], "slot-workspace", "WORKSPACE_DIR is not slot-workspace -- " + where)
            else:
                self.assertEqual(self._real(call["workspace"]), workspace, "WORKSPACE_DIR is not the slot's checkout -- " + where)


    def test_slot_compose_is_the_only_function_that_calls_compose(self):
        self.harness.build_fixture_repo()
        paths = [self.harness.script]
        lib_dir = os.path.join(self.harness.fixture_repo, LIB_REL)
        paths += [os.path.join(lib_dir, name) for name in sorted(os.listdir(lib_dir))]
        sites = []
        for path in paths:
            with open(path, encoding="utf-8") as handle:
                lines = handle.read().splitlines()
            sites += [
                (path, lines, i) for i, line in enumerate(lines)
                if not line.lstrip().startswith("#") and re.search(r"(^|[\s(])docker compose(\s|$)", line)
            ]
        self.assertEqual(len(sites), 1, f"{len(sites)} lines call docker compose, expected the one in slot_compose")
        _, lines, site = sites[0]
        start = next((i for i, line in enumerate(lines) if line.startswith("slot_compose()")), None)
        self.assertIsNotNone(start, "the file with the compose call has no slot_compose function")
        end = next(i for i in range(start + 1, len(lines)) if lines[i] == "}")
        self.assertTrue(start < site < end, "the one compose call is not inside slot_compose")

    def test_acquire_composes_from_the_main_checkout(self):
        name = self._acquire()

        self._assert_composed_from_main(self.harness.compose_calls(), name, {"up"})

    def test_list_composes_from_the_main_checkout(self):
        name = self._acquire()
        self._forget_compose_calls()

        result = self.harness.run("list")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self._assert_composed_from_main(self.harness.compose_calls(), name)

    def test_run_composes_from_the_main_checkout(self):
        name = self._acquire()
        self._forget_compose_calls()

        result = self.harness.run("run", name, "--", "true")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self._assert_composed_from_main(self.harness.compose_calls(), name, {"exec"})

    def test_restart_composes_from_the_main_checkout(self):
        name = self._acquire()
        self._forget_compose_calls()

        result = self.harness.run("restart", name)

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self._assert_composed_from_main(self.harness.compose_calls(), name, {"up"})

    def test_up_composes_from_the_main_checkout(self):
        name = self._acquire()
        self._forget_compose_calls()

        result = self.harness.run("up", name)

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self._assert_composed_from_main(self.harness.compose_calls(), name, {"up"})

    def test_down_composes_from_the_main_checkout(self):
        name = self._acquire()
        self._forget_compose_calls()

        result = self.harness.run("down", name, "--force")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self._assert_composed_from_main(self.harness.compose_calls(), name, {"down"})

    def test_every_verb_the_script_sends_to_compose_is_composed_from_main(self):
        name = self._acquire()
        self.harness.run("list")
        self.harness.run("run", name, "--", "true")
        self.harness.run("restart", name)
        self.harness.run("down", name, "--force")

        self._assert_composed_from_main(
            self.harness.compose_calls(), name, {"up", "down", "exec", "ps"}
        )


    def test_a_new_slots_env_is_under_the_script_state_and_its_checkout_has_none(self):
        name = self._acquire()

        self.assertTrue(os.path.isfile(self.harness.env_path(name)), "no env file under $DEV_CONTAINER_HOME/env/")
        self.assertFalse(
            os.path.lexists(self._slot_env_in_checkout(name)),
            "a new slot has a .devcontainer/.env in its checkout",
        )

    def test_the_env_is_seeded_from_mains_env_and_the_key_is_the_leases_not_mains(self):
        name = self._acquire()

        text = self._env_text(name)
        self.assertIn("FIREWALL_EXTRA_DOMAINS=seed.example.invalid", text.splitlines())
        self.assertIn("GIT_USER_NAME=demo-agent", text.splitlines())
        self.assertNotIn("AAAAMAINKEY", text, "main's SSH_PUBLIC_KEY was copied into the slot's env")
        with open(self.harness.lease_path(name), encoding="utf-8") as handle:
            key = json.load(handle)["key"]
        with open(key + ".pub", encoding="utf-8") as handle:
            lease_pub = handle.read().strip()
        self.assertEqual(self.harness.read_env(name)["SSH_PUBLIC_KEY"], lease_pub)

    def test_a_slot_with_no_env_is_seeded_from_mains_env_with_the_key_blanked(self):
        self.harness.build_fixture_repo(with_parent_env=self.ENV, project_env=self.SEEDED_KEYS)
        self.harness.make_bare_slot("slot-1", local_identity=("demo-agent", "demo-agent@example.com"))

        result = self.harness.run("restart", "slot-1")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertTrue(os.path.isfile(self.harness.env_path("slot-1")), "no env file was seeded")
        env = self.harness.read_env("slot-1")
        self.assertEqual(env["FIREWALL_EXTRA_DOMAINS"], "seed.example.invalid")
        self.assertEqual(env["SSH_PUBLIC_KEY"], "", "the seed carried a key")
        self.assertNotIn("AAAAMAINKEY", self._env_text("slot-1"))
        self.assertFalse(os.path.lexists(self._slot_env_in_checkout("slot-1")))

    def test_the_seed_is_taken_once_a_later_change_to_mains_env_does_not_reach_the_slot(self):
        name = self._acquire()
        with open(self._main_env(), "a", encoding="utf-8") as handle:
            handle.write("FIREWALL_EXTRA_DOMAINS=changed.example.invalid\nADDED_LATER=1\n")

        result = self.harness.run("restart", name)

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        env = self.harness.read_env(name)
        self.assertEqual(env["FIREWALL_EXTRA_DOMAINS"], "seed.example.invalid")
        self.assertNotIn("ADDED_LATER", env)


    def test_restart_ignores_hostile_edits_in_the_slots_compose_file_and_a_planted_env(self):
        name = self._acquire()
        slot_dev = os.path.join(self.harness.slot_dir(name), ".devcontainer")
        with open(os.path.join(slot_dev, "docker-compose.yml"), "a", encoding="utf-8") as handle:
            handle.write("    privileged: true\n    volumes:\n      - /:/host\n# MARK-slot-a\n")
        with open(os.path.join(slot_dev, ".env"), "w", encoding="utf-8") as handle:
            handle.write("WORKSPACE_DIR=/\nSSH_PUBLIC_KEY=ATTACKER-KEY\n")
        self._forget_compose_calls()

        result = self.harness.run("restart", name)

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        calls = self.harness.compose_calls()
        self._assert_composed_from_main(calls, name, {"up"})
        for call in calls:
            self.assertNotIn("MARK-slot-a", call["marks"], f"a compose call read the slot's compose file: {call}")
            self.assertNotEqual(self._real(call["workspace"]), "/", f"WORKSPACE_DIR=/ reached compose: {call}")
        self.assertNotIn("ATTACKER-KEY", self._env_text(name))

    def test_restart_removes_a_planted_env_and_says_so(self):
        name = self._acquire()
        planted = self._slot_env_in_checkout(name)
        with open(planted, "w", encoding="utf-8") as handle:
            handle.write("WORKSPACE_DIR=/\n")

        result = self.harness.run("restart", name)

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertFalse(os.path.lexists(planted), "the planted .env is still in the slot's checkout")
        output = result.stdout.decode() + result.stderr.decode()
        self.assertRegex(output, r"(?i)(removed|retired)[^\n]*\.env|\.env[^\n]*(removed|retired)")

    def test_a_change_to_mains_compose_file_reaches_the_slot_on_restart(self):
        name = self._acquire()
        with open(os.path.join(self.harness.main_devcontainer(), "docker-compose.yml"), "a", encoding="utf-8") as handle:
            handle.write("# MARK-main-a\n")
        self._forget_compose_calls()

        result = self.harness.run("restart", name)

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        ups = [call for call in self.harness.compose_calls() if call["verb"] == "up"]
        self.assertTrue(ups, "restart never reached compose up")
        self.assertEqual(ups[-1]["marks"], ["MARK-main-a"])


    LEGACY_ENV = (
        "GIT_USER_NAME=planted-name\nGIT_USER_EMAIL=planted@example.com\n"
        "SSH_PORT=1111\nNOVNC_PORT=1112\nVITE_DEV_PORT=1113\n"
        "WORKSPACE_DIR=/\nPLANTED_SENTINEL=1\n"
    )

    def _previous_script_slot(self):
        self.harness.build_fixture_repo(with_parent_env=self.ENV)
        self.harness.make_bare_slot(
            "slot-4",
            legacy_env_content=self.LEGACY_ENV,
            ports_content="NOVNC_PORT=1112\nSSH_PORT=1111\nVITE_DEV_PORT=1113\n",
            local_identity=("demo-agent", "demo-agent@example.com"),
        )

    def _assert_migrated(self, result):
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertFalse(os.path.lexists(self._slot_env_in_checkout("slot-4")), "the in-checkout .env was left")
        self.assertTrue(os.path.isfile(self.harness.env_path("slot-4")), "no env was rebuilt under $STATE/env/")
        text = self._env_text("slot-4")
        for planted in ("PLANTED_SENTINEL", "planted", "WORKSPACE_DIR=/"):
            self.assertNotIn(planted, text, "the old in-checkout .env was read")
        env = self.harness.read_env("slot-4")
        self.assertEqual(env["GIT_USER_NAME"], "demo-agent", "the rebuilt env is not seeded from main's")
        for index, key, old in ((0, "SSH_PORT", "1111"), (1, "NOVNC_PORT", "1112"), (2, "VITE_DEV_PORT", "1113")):
            self.assertNotEqual(env[key], old, f"{key} was carried over from the old .env")
            self.assertEqual(env[key], str(slot_port(4, index)), f"{key} was not allocated afresh")
        output = result.stdout.decode() + result.stderr.decode()
        self.assertRegex(output, r"(?i)(removed|retired)[^\n]*\.env|\.env[^\n]*(removed|retired)")
        self._assert_composed_from_main(self.harness.compose_calls(), "slot-4", {"up"})

    def test_restart_migrates_a_slot_from_the_previous_script(self):
        self._previous_script_slot()

        self._assert_migrated(self.harness.run("restart", "slot-4"))

    def test_up_migrates_a_slot_from_the_previous_script(self):
        self._previous_script_slot()

        self._assert_migrated(self.harness.run("up", "slot-4"))

    def test_list_counts_a_slot_from_the_previous_script_as_built_and_changes_nothing(self):
        self._previous_script_slot()

        result = self.harness.run("list")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        row = [line.split() for line in result.stdout.decode().splitlines() if line.split()[:1] == ["slot-4"]]
        self.assertTrue(row, result.stdout.decode())
        self.assertNotEqual(row[0][2], "unbuilt")
        self.assertTrue(os.path.isfile(self._slot_env_in_checkout("slot-4")), "list retired the old .env")
        self.assertFalse(os.path.exists(self.harness.env_path("slot-4")), "list rebuilt the env")


    def test_down_removes_the_slots_env_file(self):
        self.harness.build_fixture_repo(with_parent_env=self.ENV)
        self.harness.make_bare_slot("gone", env_content=self.ENV)
        self.assertTrue(os.path.isfile(self.harness.env_path("gone")))

        result = self.harness.run("down", "gone", "--force")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertFalse(os.path.isdir(self.harness.slot_dir("gone")))
        self.assertFalse(os.path.exists(self.harness.env_path("gone")), "down left the slot's env file")


class ReleaseRemovesTheSlotsEnvAndComposesFromMain(ListVerdictTestCase):
    def setUp(self):
        super().setUp()
        self.slot = self._ready_slot("rel")
        self.harness.mark_running("rel")
        key = os.path.join(self.harness.tmp, "rel-fixture-key")
        for path in (key, key + ".pub"):
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("key" + chr(10))
        lease = self.harness.lease_path("rel")
        os.makedirs(os.path.dirname(lease), exist_ok=True)
        with open(lease, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "holder": "test",
                    "project": PROJECT_PREFIX + "-rel",
                    "since": int(time.time()),
                    "ttl_seconds": 14400,
                    "key": key,
                    "ssh_port": TEST_SSH_PORT,
                },
                handle,
            )

    def test_release_removes_the_slots_env_file_with_the_slot(self):
        self.assertTrue(os.path.isfile(self.harness.env_path("rel")))

        result = self.harness.run("release", "rel", "--for", "test")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertFalse(os.path.isdir(self.slot), "the slot directory survived: " + result.stdout.decode())
        self.assertFalse(os.path.exists(self.harness.env_path("rel")), "release left the slot's env file")

    def test_release_takes_the_project_down_from_the_main_checkout(self):
        result = self.harness.run("release", "rel", "--for", "test")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        calls = [c for c in self.harness.compose_calls() if c["verb"] == "down"]
        self.assertTrue(calls, "release never took the project down")
        for call in calls:
            self.assertEqual(os.path.realpath(call["project_dir"]), os.path.realpath(self.harness.main_devcontainer()))
            self.assertEqual(os.path.realpath(call["workspace"]), os.path.realpath(self.slot))
            self.assertEqual(os.path.realpath(call["env_file"]), os.path.realpath(self.harness.env_path("rel")))


class ReapNeverDeletesADirectoryOutsideSlotsDir(DevContainerTestCase):
    ENV = "GIT_USER_NAME=demo-agent\nGIT_USER_EMAIL=demo-agent@example.com\n"
    OTHER = "other-demo-slot-1"

    LABEL_AWARE_DOCKER = """#!/usr/bin/env bash
if [[ "${1:-}" == ps ]]; then
  want=""
  case "$*" in
    *dev-container.workspace*) want=workspace ;;
    *working_dir*) want=workdir ;;
  esac
  if [[ -n "$want" ]]; then
    for arg in "$@"; do
      case "$arg" in
        label=com.docker.compose.project=*)
          p="${arg#label=com.docker.compose.project=}"
          [[ -f "$DOCKER_STUB_STATE/$p.$want" ]] && cat "$DOCKER_STUB_STATE/$p.$want"
          ;;
      esac
    done
    exit 0
  fi
fi
exec "$(dirname "$0")/docker-real" "$@"
"""

    def setUp(self):
        super().setUp()
        self.harness.build_fixture_repo(with_parent_env=self.ENV)
        self.harness._write_stub("docker-real", DOCKER_STUB)
        self.harness._write_stub("docker", self.LABEL_AWARE_DOCKER)

    def _git(self, *args):
        subprocess.run(
            [self.harness.real_git_bin, *args],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )

    def _foreign_repo(self, name):
        path = os.path.join(self.harness.tmp, name)
        self._git("clone", "-q", self.harness.fixture_repo, path)
        self._git("-C", path, "remote", "set-url", "origin", "/nonexistent/other.git")
        return path

    def _running_project(self, project, workspace=None, working_dir=None):
        state = self.harness.docker_state
        with open(os.path.join(state, f"{project}.running"), "w", encoding="utf-8"):
            pass
        for suffix, value in (("workspace", workspace), ("workdir", working_dir)):
            if value is not None:
                with open(os.path.join(state, f"{project}.{suffix}"), "w", encoding="utf-8") as handle:
                    handle.write(value + "\n")

    def _down_calls(self, project):
        with open(self.harness.docker_log, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
        return [
            line for line in lines
            if line.startswith("ARGS:") and f"-p {project} " in line
            and re.search(r"\bdown\b", line.split("|")[0])
        ]

    def _reap(self):
        result = self.harness.run("reap")
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        return result.stdout.decode()

    def _assert_untouched(self, path, project, out):
        self.assertTrue(os.path.isdir(path), f"reap removed {path}:\n{out}")
        self.assertTrue(os.path.isdir(os.path.join(path, ".git")), f"reap damaged {path}:\n{out}")
        self.assertEqual(self._down_calls(project), [], f"reap took {project} down:\n{out}")

    def test_a_workspace_label_outside_slots_dir_is_not_removed(self):
        other = self._foreign_repo("other-demo-slot-1")
        self._running_project(self.OTHER, workspace=other)

        out = self._reap()

        self._assert_untouched(other, self.OTHER, out)

    def test_a_working_dir_label_outside_slots_dir_is_not_removed(self):
        other = self._foreign_repo("other-demo-slot-1")
        self._running_project(self.OTHER, working_dir=os.path.join(other, ".devcontainer"))

        out = self._reap()

        self._assert_untouched(other, self.OTHER, out)

    def test_the_main_checkout_named_by_a_workspace_label_is_not_removed(self):
        main = self.harness.fixture_repo
        self._running_project(self.OTHER, workspace=main)

        out = self._reap()

        self._assert_untouched(main, self.OTHER, out)

    def test_the_main_checkout_named_by_a_working_dir_label_is_not_removed(self):
        main = self.harness.fixture_repo
        self._running_project(self.OTHER, working_dir=os.path.join(main, ".devcontainer"))

        out = self._reap()

        self._assert_untouched(main, self.OTHER, out)

    def test_a_project_outside_slots_dir_is_not_taken_down_with_its_volumes(self):
        other = self._foreign_repo("other-demo-slot-1")
        self._running_project(self.OTHER, workspace=other)

        out = self._reap()

        downs = self._down_calls(self.OTHER)
        self.assertEqual(
            [line for line in downs if re.search(r"(^|\s)(-v|--volumes)\b", line.split("|")[0])],
            [], f"reap ran down -v on another project:\n{out}",
        )
        self.assertNotIn("reaping orphan compose project", out)

    def test_an_orphan_slot_under_slots_dir_with_a_foreign_origin_is_kept(self):
        result = self.harness.run_acquire(holder="first")
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        name = self.harness.acquired_slot_name(result)
        project = f"{PROJECT_PREFIX}-{name}"
        slot = self.harness.slot_dir(name)
        self.harness.free_slot(name)
        self._git("-C", slot, "remote", "set-url", "origin", "/nonexistent/other.git")
        with open(os.path.join(self.harness.docker_state, f"{project}.workspace"), "w", encoding="utf-8") as handle:
            handle.write(slot + "\n")

        result = self.harness.run("reap")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertIn("refusing to remove", result.stderr.decode())
        self.assertTrue(os.path.isdir(slot), "reap removed a directory that is not this project's checkout")
        self.assertEqual(self._down_calls(project), [], "reap took the project down although it kept the directory")


if __name__ == "__main__":
    unittest.main()


IDENTITY_ENV = "GIT_USER_NAME=demo-agent\nGIT_USER_EMAIL=demo-agent@example.com\n"


class SlotMountDirs(DevContainerTestCase):
    DIRS = ".local/shared mounts/data"

    def _acquire(self):
        self.harness.build_fixture_repo(
            with_parent_env=IDENTITY_ENV,
            project_env=project_env_text(SLOT_MOUNT_DIRS=self.DIRS),
        )
        result = self.harness.run_acquire()
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        return self.harness.acquired_slot_name(result)

    def test_each_directory_exists_in_the_checkout_and_in_the_slot_after_acquire(self):
        name = self._acquire()

        for root in (self.harness.fixture_repo, self.harness.slot_dir(name)):
            for rel in self.DIRS.split():
                self.assertTrue(os.path.isdir(os.path.join(root, rel)), f"{rel} missing under {root}")

    def test_release_succeeds_with_the_directories_present(self):
        name = self._acquire()

        result = self.harness.run("release", name, "--for", "test")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertFalse(os.path.isdir(self.harness.slot_dir(name)), "release left the slot directory")
        self.assertTrue(os.path.isdir(os.path.join(self.harness.fixture_repo, ".local", "shared")))

    def test_restart_creates_the_directories_a_slot_lacks(self):
        self.harness.build_fixture_repo(project_env=project_env_text(SLOT_MOUNT_DIRS=self.DIRS))
        self.harness.make_bare_slot(
            "slot-1",
            env_content=IDENTITY_ENV,
            ports_content="SSH_PORT=39220\nNOVNC_PORT=39230\nVITE_DEV_PORT=39240\n",
        )

        result = self.harness.run("restart", "slot-1")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        for root in (self.harness.fixture_repo, self.harness.slot_dir("slot-1")):
            for rel in self.DIRS.split():
                self.assertTrue(os.path.isdir(os.path.join(root, rel)), f"{rel} missing under {root}")


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


class ProjectNeutralSteps(DevContainerTestCase):

    def _volume_lines(self):
        with open(self.harness.docker_log, encoding="utf-8") as handle:
            return [line for line in handle.read().splitlines() if "volume create" in line]

    def _up_files(self):
        return [c for c in self.harness.compose_calls() if c["verb"] == "up"][-1]["files"]

    def test_a_store_volume_is_created_only_when_the_project_names_one(self):
        self.harness.build_fixture_repo(
            with_parent_env=IDENTITY_ENV, project_env=project_env_text(PNPM_STORE_VOLUME="store-x")
        )
        self.assertEqual(self.harness.run_acquire().returncode, 0)
        self.assertTrue(any("store-x" in line for line in self._volume_lines()), self._volume_lines())

    def test_no_store_volume_is_created_when_the_project_names_none(self):
        self.harness.build_fixture_repo(with_parent_env=IDENTITY_ENV, project_env=project_env_text())
        self.assertEqual(self.harness.run_acquire().returncode, 0)
        self.assertFalse(any("pnpm" in line for line in self._volume_lines()), self._volume_lines())

    def test_the_container_service_is_the_one_the_compose_config_mounts_the_checkout_in(self):
        self.harness.build_fixture_repo(with_parent_env=IDENTITY_ENV, project_env=project_env_text())

        result = self.harness.run(
            "acquire", "--for", "test", port_overrides={"DOCKER_STUB_SERVICE": "workspace"}
        )

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        with open(self.harness.docker_log, encoding="utf-8") as handle:
            execs = [line for line in handle.read().splitlines() if " exec " in line]
        self.assertTrue(execs)
        for line in execs:
            self.assertIn(" exec -T workspace ", line)

    def test_no_service_mounting_the_checkout_refuses_acquire_and_says_so(self):
        self.harness.build_fixture_repo(with_parent_env=IDENTITY_ENV, project_env=project_env_text())

        result = self.harness.run(
            "acquire", "--for", "test", port_overrides={"DOCKER_STUB_SERVICE": "none"}
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no service", result.stderr.decode())

    def test_two_services_mounting_the_checkout_refuse_acquire_and_name_both(self):
        self.harness.build_fixture_repo(with_parent_env=IDENTITY_ENV, project_env=project_env_text())

        result = self.harness.run(
            "acquire", "--for", "test", port_overrides={"DOCKER_STUB_SERVICE": "app,worker"}
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("app, worker", result.stderr.decode())

    def test_release_refuses_when_two_services_mount_the_checkout_rather_than_keep_the_key(self):
        self.harness.build_fixture_repo(with_parent_env=IDENTITY_ENV, project_env=project_env_text())
        acquired = self.harness.run_acquire()
        self.assertEqual(acquired.returncode, 0, acquired.stderr.decode())
        name = self.harness.acquired_slot_name(acquired)

        result = self.harness.run(
            "release", name, "--for", "test", port_overrides={"DOCKER_STUB_SERVICE": "app,worker"}
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("app, worker", result.stderr.decode())

    def _without_install_script(self, **values):
        self.harness.build_fixture_repo(
            with_parent_env=IDENTITY_ENV, project_env=project_env_text(**values)
        )
        os.remove(os.path.join(self.harness.fixture_repo, ".devcontainer", "install.sh"))

    def test_a_store_volume_with_no_install_script_is_refused(self):
        self._without_install_script(PNPM_STORE_VOLUME="store-x")

        result = self.harness.run_acquire()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("PNPM_STORE_VOLUME", result.stderr.decode())

    def test_no_install_script_and_no_store_volume_skips_the_install_and_says_so(self):
        self._without_install_script()

        result = self.harness.run_acquire()

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertIn("install is skipped", result.stdout.decode())

    def test_an_overlay_file_named_like_an_echo_flag_still_joins_the_stack(self):
        self.harness.build_fixture_repo(
            with_parent_env=IDENTITY_ENV + "SHARED_DIR=/somewhere\n",
            project_env=project_env_text(SLOT_OVERLAYS="SHARED_DIR=-n", SLOT_ENV_KEYS="SHARED_DIR"),
        )

        self.assertEqual(self.harness.run_acquire().returncode, 0)

        self.assertTrue(any(f.endswith("/-n") for f in self._up_files()), self._up_files())

    def test_an_overlay_file_joins_the_stack_when_its_variable_is_set_in_the_slots_env(self):
        self.harness.build_fixture_repo(
            with_parent_env=IDENTITY_ENV + "SHARED_DIR=/somewhere\n",
            project_env=project_env_text(SLOT_OVERLAYS="SHARED_DIR=docker-compose.shared.yml", SLOT_ENV_KEYS="SHARED_DIR"),
        )

        self.assertEqual(self.harness.run_acquire().returncode, 0)

        self.assertTrue(any(f.endswith("docker-compose.shared.yml") for f in self._up_files()), self._up_files())

    def test_an_overlay_file_stays_out_when_its_variable_is_not_set(self):
        self.harness.build_fixture_repo(
            with_parent_env=IDENTITY_ENV,
            project_env=project_env_text(SLOT_OVERLAYS="SHARED_DIR=docker-compose.shared.yml"),
        )

        self.assertEqual(self.harness.run_acquire().returncode, 0)

        self.assertFalse(any(f.endswith("docker-compose.shared.yml") for f in self._up_files()), self._up_files())
