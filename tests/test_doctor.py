import json
import os
import re
import shutil
import socket
import stat
import subprocess
import unittest
from unittest import mock

from harness import (
    PROJECT_PREFIX,
    REPO_ROOT,
    SLOT_COUNT_HELD,
    SLOT_PORT_BLOCK,
    _PORT_BASE,
    slot_port,
    DevContainerHarness,
    DevContainerTestCase,
    project_env_text,
)

FIXTURES = os.path.join(REPO_ROOT, "tests", "fixtures")
IMAGE_COMMIT = "0123456789abcdef0123456789abcdef01234567"
FOUR_GIB = 4 * 1024 ** 3
PORTS = "NOVNC_PORT=39230\nSSH_PORT=39220\nVITE_DEV_PORT=39240\n"
IDENTITY = "GIT_USER_NAME=demo-agent\nGIT_USER_EMAIL=demo-agent@example.com\n"

FAKE_DOCKER = """#!/usr/bin/env bash
# Answers the daemon probes a doctor makes. FAKE_DOCKER_FAIL lists the
# subcommands that refuse; any inspect prints the commit of the image.
for word in $FAKE_DOCKER_FAIL; do
  if [[ "${1:-}" == "$word" ]]; then echo "docker: $word refused" >&2; exit 1; fi
done
case "$*" in
  "compose ls"*) [[ -z "${FAKE_IMAGES:-}" ]] || echo '[{"Name":"demo-slot-1"}]'; exit 0 ;;
  "image ls"*) [[ -z "${FAKE_IMAGES:-}" ]] || echo 'demo-slot-1-app:latest'; exit 0 ;;
  "ps --format"*) [[ -z "${FAKE_BUSY_PORT:-}" ]] || echo "0.0.0.0:${FAKE_BUSY_PORT}->22/tcp"; exit 0 ;;
esac
case "$*" in
  *inspect*--format*|*inspect*-f*) echo "$FAKE_IMAGE_COMMIT" ;;
  *inspect*) printf '[{"Config":{"Labels":{"org.opencontainers.image.revision":"%s"}}}]\n' "$FAKE_IMAGE_COMMIT" ;;
esac
exit 0
"""


MOUNT = {"type": "bind", "source": "/somewhere", "target": "/workspace"}


def stub_config(case, services, fail=False):
    """Make the stand-in compose print this `config --format json` (or refuse)."""
    values = {}
    if fail:
        values["DOCKER_STUB_CONFIG_FAIL"] = "1"
    else:
        path = os.path.join(case.harness.tmp, "config.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"services": services}, handle)
        values["DOCKER_STUB_CONFIG_JSON"] = path
    patcher = mock.patch.dict(os.environ, values)
    patcher.start()
    case.addCleanup(patcher.stop)


def output_of(result):
    return result.stdout.decode() + result.stderr.decode()


def read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


class MemoryCeilingTestCase(DevContainerTestCase):
    def fresh_harness(self):
        self.harness = DevContainerHarness()
        self.addCleanup(self.harness.cleanup)

    def start_probe(self, **project_values):
        h = self.harness
        h.build_fixture_repo(project_env=project_env_text(**project_values))
        h.make_bare_slot(
            "probe",
            env_content="GIT_USER_NAME=demo-agent\nGIT_USER_EMAIL=old@example.com\n",
            ports_content=PORTS,
        )
        return h.run("up", "probe")

    def up_calls(self):
        return [c for c in self.harness.compose_calls() if c["verb"] == "up"]

    def overlay_files(self):
        calls = self.up_calls()
        self.assertTrue(calls, "no compose up call")
        return [f for f in calls[-1]["files"] if os.path.basename(f) not in ("docker-compose.yml", "slot-volume.yml")]

    def overlay_text(self):
        files = self.overlay_files()
        self.assertEqual(len(files), 1, files)
        self.assertTrue(os.path.isfile(files[0]), files[0])
        return read(files[0])


class SlotMemoryCeiling(MemoryCeilingTestCase):
    def test_a_long_form_mount_puts_the_ceiling_on_that_service_only(self):
        # compose.yml: app mounts the checkout with `type: bind` / `target: /workspace`; db is a sidecar
        stub_config(self, {"app": {"volumes": [MOUNT]}, "db": {"image": "db:1"}})

        result = self.start_probe()

        self.assertEqual(result.returncode, 0, output_of(result))
        text = self.overlay_text()
        self.assertRegex(text, r"(?m)^  app:$")
        self.assertIn("mem_limit: 4g", text)
        self.assertNotRegex(text, r"(?m)^  db:$")

    def test_with_no_workspace_mount_every_service_of_the_config_is_limited_and_none_else(self):
        # compose.yml indented four spaces, no service called app
        stub_config(self, {"web": {"image": "web:1"}, "cache": {"image": "cache:1"}})

        result = self.start_probe()

        self.assertEqual(result.returncode, 0, output_of(result))
        text = self.overlay_text()
        self.assertRegex(text, r"(?m)^  web:$")
        self.assertRegex(text, r"(?m)^  cache:$")
        self.assertNotRegex(text, r"(?m)^  app:$")

    def test_a_config_that_fails_refuses_and_starts_nothing(self):
        stub_config(self, {}, fail=True)

        result = self.start_probe()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("memory ceiling", output_of(result))
        self.assertEqual(self.up_calls(), [])

    def test_up_composes_an_overlay_carrying_the_default_ceiling(self):
        result = self.start_probe()

        self.assertEqual(result.returncode, 0, output_of(result))
        self.assertIn("4g", self.overlay_text())

    def test_project_env_overrides_the_ceiling(self):
        result = self.start_probe(SLOT_MEMORY_LIMIT="2g")

        self.assertEqual(result.returncode, 0, output_of(result))
        text = self.overlay_text()
        self.assertIn("2g", text)
        self.assertNotIn("4g", text)

    def test_the_overlay_rides_along_with_the_secrets_overlay(self):
        h = self.harness
        h.build_fixture_repo(with_parent_env=IDENTITY, project_env=project_env_text())
        acquired = h.run_acquire(holder="task-20", owner="Session A [aaaa]")
        self.assertEqual(acquired.returncode, 0, output_of(acquired))
        name = h.acquired_slot_name(acquired)
        with open(os.path.join(h.main_devcontainer(), "docker-compose.secrets.yml"), "w", encoding="utf-8") as handle:
            handle.write("# fixture overlay\n")
        with open(os.path.join(h.slot_dir(name), ".env.test.local"), "w", encoding="utf-8") as handle:
            handle.write("EXAMPLE_API_KEY=dummy\n")

        result = h.run("up", name, "--secrets", "--for", "task-20")

        self.assertEqual(result.returncode, 0, output_of(result))
        files = self.up_calls()[-1]["files"]
        self.assertTrue(any("secrets" in os.path.basename(f) for f in files), files)
        self.assertTrue(any(os.path.isfile(f) and "4g" in read(f) for f in files), files)

    def test_acquire_starts_the_slot_under_the_ceiling(self):
        h = self.harness
        h.build_fixture_repo(with_parent_env=IDENTITY, project_env=project_env_text())

        result = h.run_acquire(holder="task-20", owner="Session A [aaaa]")

        self.assertEqual(result.returncode, 0, output_of(result))
        self.assertIn("4g", self.overlay_text())

    def test_an_unusable_ceiling_refuses_and_starts_nothing(self):
        for bad in ("lots", "0", "-1g"):
            with self.subTest(value=bad):
                self.fresh_harness()
                result = self.start_probe(SLOT_MEMORY_LIMIT=bad)

                self.assertNotEqual(result.returncode, 0)
                self.assertIn("SLOT_MEMORY_LIMIT", output_of(result))
                self.assertNotIn("unknown key", output_of(result))
                self.assertEqual(self.up_calls(), [])

    def test_the_main_checkouts_tracked_files_stay_unmodified(self):
        result = self.start_probe()

        self.assertEqual(result.returncode, 0, output_of(result))
        status = subprocess.run(
            ["git", "-C", self.harness.fixture_repo, "status", "--porcelain", "--untracked-files=no"],
            stdout=subprocess.PIPE,
        ).stdout.decode()
        self.assertEqual(status, "")
        self.assertTrue(self.overlay_files())


class OverlayBuildsOnTheFixtureProjects(MemoryCeilingTestCase):
    def fixture_projects(self):
        return sorted(
            d for d in os.listdir(FIXTURES) if os.path.isfile(os.path.join(FIXTURES, d, "project.env"))
        )

    def real_compose_available(self):
        if not shutil.which("docker"):
            return False
        probe = subprocess.run(
            ["docker", "compose", "version"], stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        return probe.returncode == 0

    def test_the_overlay_composes_with_each_fixture_projects_real_compose_file(self):
        real = self.real_compose_available()
        for project in self.fixture_projects():
            with self.subTest(project=project):
                self.fresh_harness()
                h = self.harness
                src = os.path.join(FIXTURES, project)
                services = {"app": {"volumes": [MOUNT]}} if project == "demo-project" else {"app": {"image": "x"}}
                stub_config(self, services)
                project_env = read(os.path.join(src, "project.env"))
                # the harness names every slot after its own prefix
                project_env = re.sub(r"^PROJECT=.*$", "PROJECT=" + PROJECT_PREFIX, project_env, flags=re.M)
                h.build_fixture_repo(project_env=project_env)
                found = re.search(r"^COMPOSE_DIR=(\S+)", project_env, re.M)
                compose_dir = found.group(1) if found else ".devcontainer"
                shutil.copytree(
                    os.path.join(src, compose_dir), os.path.join(h.fixture_repo, compose_dir), dirs_exist_ok=True
                )
                ports = f"SSH_PORT={slot_port(1, 0)}\nWEB_PORT={slot_port(1, 1)}\n"
                h.make_bare_slot("slot-1", env_content=IDENTITY, ports_content=ports)
                up = h.run("up", "slot-1")
                self.assertEqual(up.returncode, 0, output_of(up))
                self.assertTrue(self.overlay_files())
                if not real:
                    continue
                cmd = ["docker", "compose", "--project-directory", os.path.join(h.fixture_repo, compose_dir)]
                for f in self.up_calls()[-1]["files"]:
                    cmd += ["-f", f]
                shown = subprocess.run(
                    cmd + ["config", "--format", "json"],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60,
                )
                self.assertEqual(shown.returncode, 0, shown.stderr.decode())
                services = json.loads(shown.stdout)["services"].values()
                self.assertIn(str(FOUR_GIB), [str(s.get("mem_limit")) for s in services])


class DoctorTestCase(DevContainerTestCase):
    def setUp(self):
        super().setUp()
        h = self.harness
        h.build_fixture_repo(project_env=project_env_text())
        self.fake_bin = os.path.join(h.tmp, "fake-docker-bin")
        os.makedirs(self.fake_bin)
        path = os.path.join(self.fake_bin, "docker")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(FAKE_DOCKER)
        os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)

    def doctor(self, fail="", **extra):
        h = self.harness
        env = {
            "PATH": self.fake_bin + os.pathsep + h.stub_bin + os.pathsep + os.environ["PATH"],
            "FAKE_DOCKER_FAIL": fail,
            "FAKE_IMAGE_COMMIT": IMAGE_COMMIT,
            "FAKE_IMAGES": "1",
        }
        env.update(extra)
        return h.run("doctor", port_overrides=env)

    def line_with(self, text, needle):
        lines = [line for line in text.splitlines() if needle in line]
        self.assertTrue(lines, f"no line with {needle!r} in:\n{text}")
        return lines[0]

    def named(self, result, pattern):
        text = output_of(result)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("Usage:", text)
        self.assertRegex(text, pattern)

    def in_a_container(self):
        marker = os.path.join(self.harness.tmp, "in-a-container")
        with open(marker, "w", encoding="utf-8"):
            pass
        return marker


class DoctorChecksPrerequisites(DoctorTestCase):
    def test_exits_zero_when_every_prerequisite_is_met(self):
        result = self.doctor()

        self.assertEqual(result.returncode, 0, output_of(result))

    def test_a_daemon_that_refuses_is_named_and_fails_the_run(self):
        result = self.doctor(fail="info")

        self.named(result, r"(?i)docker")

    def test_an_unwritable_state_directory_is_named(self):
        blocker = os.path.join(self.harness.tmp, "a-file")
        with open(blocker, "w", encoding="utf-8"):
            pass

        result = self.doctor(DEV_CONTAINER_HOME=os.path.join(blocker, "state"))

        self.named(result, r"(?i)state")

    def test_busy_slot_ports_are_named(self):
        result = self.doctor(FAKE_BUSY_PORT=str(slot_port(1, 0)))

        self.named(result, r"(?i)port")

    def test_a_missing_project_file_is_named(self):
        os.remove(os.path.join(self.harness.fixture_repo, "project.env"))

        result = self.doctor()

        self.named(result, r"project\.env")

    def test_every_failed_prerequisite_is_named_not_only_the_first(self):
        os.remove(os.path.join(self.harness.fixture_repo, "project.env"))

        result = self.doctor(fail="info")

        self.named(result, r"(?i)docker")
        self.assertIn("project.env", output_of(result))

    def test_inside_a_container_a_missing_socket_is_named(self):
        result = self.doctor(fail="info", DEV_CONTAINER_DOCKERENV_PATH=self.in_a_container())

        self.named(result, r"(?i)socket")

    def test_inside_a_container_with_docker_reachable_it_passes(self):
        result = self.doctor(DEV_CONTAINER_DOCKERENV_PATH=self.in_a_container())

        self.assertEqual(result.returncode, 0, output_of(result))


class DoctorReportsTheCeiling(DoctorTestCase):
    def test_default_ceiling_is_printed_with_its_source(self):
        result = self.doctor()

        line = self.line_with(result.stdout.decode(), "SLOT_MEMORY_LIMIT")
        self.assertIn("4g", line)
        self.assertRegex(line, r"(?i)default")

    def test_overridden_ceiling_is_printed_with_the_project_file_as_source(self):
        with open(os.path.join(self.harness.fixture_repo, "project.env"), "w", encoding="utf-8") as handle:
            handle.write(project_env_text(SLOT_MEMORY_LIMIT="2g"))

        result = self.doctor()

        line = self.line_with(result.stdout.decode(), "SLOT_MEMORY_LIMIT")
        self.assertIn("2g", line)
        self.assertRegex(line, r"(?i)project")
        self.assertNotRegex(line, r"(?i)default")


class DoctorReportsTheImageCommit(DoctorTestCase):
    def test_the_commit_of_the_running_image_is_printed(self):
        result = self.doctor()

        self.assertIn(IMAGE_COMMIT, result.stdout.decode())

    def test_it_prints_the_commit_the_image_reports_not_a_fixed_one_or_the_checkouts(self):
        other = "fedcba9876543210fedcba9876543210fedcba98"
        head = subprocess.run(
            ["git", "-C", self.harness.fixture_repo, "rev-parse", "HEAD"], stdout=subprocess.PIPE, check=True
        ).stdout.decode().strip()

        result = self.doctor(FAKE_IMAGE_COMMIT=other)

        out = result.stdout.decode()
        self.assertIn(other, out)
        self.assertNotIn(IMAGE_COMMIT, out)
        self.assertNotIn(head, out)


    def test_no_labelled_image_prints_none_and_still_passes(self):
        result = self.doctor(FAKE_IMAGES="")

        self.assertEqual(result.returncode, 0, output_of(result))
        line = self.line_with(result.stdout.decode(), "image commit")
        self.assertRegex(line, r"(?i)none")
        self.assertNotIn(IMAGE_COMMIT, line)


class InstallRefusesAnUncleanCheckout(DevContainerTestCase):
    def leased_slot(self):
        h = self.harness
        h.build_fixture_repo(with_parent_env=IDENTITY, project_env=project_env_text())
        subprocess.run(["git", "-C", h.fixture_repo, "branch", "-M", "master"], check=True)
        result = h.run_acquire(holder="task-20", owner="Session A [aaaa]")
        self.assertEqual(result.returncode, 0, output_of(result))
        return h.acquired_slot_name(result)

    def install_calls(self, name):
        path = os.path.join(self.harness.docker_state, f"{PROJECT_PREFIX}-{name}.install-calls")
        return len(read(path).splitlines()) if os.path.isfile(path) else 0

    def make_dirty(self):
        with open(os.path.join(self.harness.main_devcontainer(), "docker-compose.yml"), "a", encoding="utf-8") as handle:
            handle.write("# edited\n")

    def leave_default_branch(self):
        subprocess.run(["git", "-C", self.harness.fixture_repo, "checkout", "-q", "-b", "side-work"], check=True)

    def test_a_dirty_checkout_is_refused_and_called_dirty(self):
        name = self.leased_slot()
        before = self.install_calls(name)
        self.make_dirty()

        result = self.harness.run("install", name)

        self.assertNotEqual(result.returncode, 0)
        self.assertRegex(output_of(result), r"(?i)dirty|uncommitted")
        self.assertEqual(self.install_calls(name), before)

    def test_an_off_default_branch_is_refused_and_names_both_branches(self):
        name = self.leased_slot()
        before = self.install_calls(name)
        self.leave_default_branch()

        result = self.harness.run("install", name)

        text = output_of(result)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("side-work", text)
        self.assertIn("master", text)
        self.assertEqual(self.install_calls(name), before)

    def test_the_two_refusals_say_different_things(self):
        name = self.leased_slot()
        self.make_dirty()
        dirty = output_of(self.harness.run("install", name))
        self.leave_default_branch()
        off_branch = output_of(self.harness.run("install", name))

        self.assertNotIn("side-work", dirty)
        self.assertNotRegex(off_branch, r"(?i)dirty|uncommitted")


class InstallBuildsTheImageFromTheCommit(DevContainerTestCase):
    """The image is labelled with the commit of the checkout it was built from."""

    LABEL = "org.opencontainers.image.revision"

    def leased_slot(self):
        h = self.harness
        stub_config(self, {"app": {"build": {"context": "/ctx"}, "volumes": [MOUNT]}})
        h.build_fixture_repo(with_parent_env=IDENTITY, project_env=project_env_text())
        with open(os.path.join(h.main_devcontainer(), "docker-compose.yml"), "w", encoding="utf-8") as handle:
            handle.write("services:\n  app:\n    build: .\n")
        subprocess.run(["git", "-C", h.fixture_repo, "commit", "-q", "-am", "build section"], check=True)
        subprocess.run(["git", "-C", h.fixture_repo, "branch", "-M", "master"], check=True)
        result = h.run_acquire(holder="task-20", owner="Session A [aaaa]")
        self.assertEqual(result.returncode, 0, output_of(result))
        return h.acquired_slot_name(result)

    def head(self):
        return subprocess.run(
            ["git", "-C", self.harness.fixture_repo, "rev-parse", "HEAD"], stdout=subprocess.PIPE, check=True
        ).stdout.decode().strip()

    def build_lines(self):
        return [
            line for line in read(self.harness.docker_log).splitlines()
            if re.search(r"(^ARGS: |\s)build(\s|$)", line)
        ]

    def commit_in_checkout(self, name):
        path = os.path.join(self.harness.fixture_repo, "added.txt")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(name + "\n")
        subprocess.run(["git", "-C", self.harness.fixture_repo, "add", "added.txt"], check=True)
        subprocess.run(["git", "-C", self.harness.fixture_repo, "commit", "-q", "-m", name], check=True)

    def overlay(self, name):
        return read(os.path.join(self.harness.state_dir, "overlays", f"{PROJECT_PREFIX}-{name}.yml"))

    def label_commit(self, name):
        found = re.search(re.escape(self.LABEL) + r': "?([0-9a-f]+)', self.overlay(name))
        self.assertTrue(found, "the overlay carries no " + self.LABEL + ":\n" + self.overlay(name))
        return found.group(1)

    def test_a_clean_install_builds_an_image_labelled_with_the_head_commit(self):
        name = self.leased_slot()
        built_before = len(self.build_lines())

        result = self.harness.run("install", name)

        self.assertEqual(result.returncode, 0, output_of(result))
        self.assertGreater(len(self.build_lines()), built_before, "install built no image")
        self.assertEqual(self.label_commit(name), self.head())

    def test_a_newer_commit_gives_the_next_build_the_newer_label(self):
        name = self.leased_slot()
        first = self.head()
        self.assertEqual(self.harness.run("install", name).returncode, 0)
        self.commit_in_checkout("second")
        built_before = len(self.build_lines())

        result = self.harness.run("install", name)

        self.assertEqual(result.returncode, 0, output_of(result))
        self.assertNotEqual(first, self.head())
        self.assertGreater(len(self.build_lines()), built_before, "install built no image")
        self.assertEqual(self.label_commit(name), self.head())

    def test_a_refused_install_builds_no_image(self):
        name = self.leased_slot()
        built_before = self.build_lines()
        with open(os.path.join(self.harness.main_devcontainer(), "docker-compose.yml"), "a", encoding="utf-8") as handle:
            handle.write("# edited\n")

        result = self.harness.run("install", name)

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.build_lines(), built_before)


if __name__ == "__main__":
    unittest.main()


class DoctorReportsTheCommand(DoctorTestCase):
    """Whether the global `dev-container` command is on PATH, and which engine clone it runs from."""

    def command_line(self, **facts):
        result = self.doctor(**facts)
        self.assertEqual(result.returncode, 0, output_of(result))
        return self.line_with(result.stdout.decode(), "dev-container command")

    def test_on_path_the_line_is_ok_and_names_the_command_and_the_clone(self):
        line = self.command_line(
            DEV_CONTAINER_CLI_STATE="path", DEV_CONTAINER_CLI_FOUND="/u/bin/dev-container",
            DEV_CONTAINER_CLI_CLONE="/u/engine")
        self.assertTrue(line.startswith("ok "), line)
        self.assertIn("/u/bin/dev-container", line)
        self.assertIn("/u/engine", line)

    def test_not_on_path_the_line_is_a_note_that_names_the_clone_and_the_installer(self):
        line = self.command_line(DEV_CONTAINER_CLI_STATE="off-path", DEV_CONTAINER_CLI_CLONE="/u/engine")
        self.assertTrue(line.startswith("note "), line)
        self.assertIn("not on PATH", line)
        self.assertIn("/u/engine", line)
        self.assertIn("install-cli.sh", line)

    def test_another_command_earlier_on_path_is_named(self):
        line = self.command_line(
            DEV_CONTAINER_CLI_STATE="shadowed", DEV_CONTAINER_CLI_FOUND="/p/bin/dev-container",
            DEV_CONTAINER_CLI_CLONE="/u/engine")
        self.assertTrue(line.startswith("note "), line)
        self.assertIn("/p/bin/dev-container", line)
        self.assertRegex(line, r"(?i)earlier|shadow")

    def test_through_the_windows_shim_the_line_says_so_and_names_the_clone(self):
        line = self.command_line(DEV_CONTAINER_CLI_STATE="windows-shim", DEV_CONTAINER_CLI_CLONE="/u/engine")
        self.assertTrue(line.startswith("ok "), line)
        self.assertRegex(line, r"(?i)windows")
        self.assertIn("/u/engine", line)

    def test_run_some_other_way_it_says_the_command_was_not_checked(self):
        line = self.command_line()
        self.assertTrue(line.startswith("note "), line)
        self.assertRegex(line, r"(?i)not checked|not run through")

    def test_a_missing_command_never_fails_the_run(self):
        result = self.doctor(DEV_CONTAINER_CLI_STATE="off-path", DEV_CONTAINER_CLI_CLONE="/u/engine")
        self.assertEqual(result.returncode, 0, output_of(result))
        self.assertIn("every prerequisite is met", result.stdout.decode())
