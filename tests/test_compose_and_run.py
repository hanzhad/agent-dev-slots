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


class UpBringsASlotsStackUpWithOrWithoutTheSecretsOverlay(DevContainerTestCase):
    ENV_WITH_IDENTITY = ENV_WITH_IDENTITY
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
    ENV_WITH_IDENTITY = ENV_WITH_IDENTITY

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
    ENV_WITH_IDENTITY = ENV_WITH_IDENTITY

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


if __name__ == "__main__":
    unittest.main()
