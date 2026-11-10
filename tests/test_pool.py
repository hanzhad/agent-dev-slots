import json
import os
import shutil
import stat
import subprocess
import tempfile
import time
import unittest

from harness import (
    INSTALL_STUB,
    PROJECT_PREFIX,
    REPO_ROOT,
    DevContainerHarness,
    project_env_text,
    slot_port,
)

IDENTITY = "GIT_USER_NAME=pool-agent\nGIT_USER_EMAIL=pool-agent@example.com\n"
FIXTURES = os.path.join(REPO_ROOT, "tests", "fixtures")
SECOND_FIXTURE = os.path.join(FIXTURES, "second-project")

# `docker stats` is answered from <project>.cpu files, one percentage per
# project; --format is expanded for {{.CPUPerc}}, {{.Name}} and {{.Container}}.
# Every other call goes to the base stub.
STATS_WRAPPER = """#!/usr/bin/env bash
here="$(dirname "$0")"
if [[ "${1:-}" == stats ]]; then
  fmt=""
  prev=""
  for arg in "$@"; do
    [[ "$prev" == "--format" ]] && fmt="$arg"
    case "$arg" in --format=*) fmt="${arg#--format=}" ;; esac
    prev="$arg"
  done
  tok_cpu="{{.CPUPerc}}"
  tok_name="{{.Name}}"
  tok_container="{{.Container}}"
  [[ -n "$fmt" ]] || fmt="$tok_name $tok_cpu"
  state="${DOCKER_STUB_STATE:-/tmp}"
  for f in "$state"/*.cpu; do
    [[ -f "$f" ]] || continue
    project="$(basename "$f" .cpu)"
    [[ -f "$state/$project.running" ]] || continue
    name="$project-app-1"
    value="$(cat "$f")%"
    line="${fmt//"$tok_cpu"/$value}"
    line="${line//"$tok_name"/$name}"
    line="${line//"$tok_container"/$name}"
    echo "$line"
  done
  exit 0
fi
exec "$here/docker-base" "$@"
"""


class PoolHarness(DevContainerHarness):
    prefix = PROJECT_PREFIX
    port_names = ("SSH_PORT", "NOVNC_PORT", "VITE_DEV_PORT")

    def __init__(self):
        super().__init__()
        self.meminfo = os.path.join(self.tmp, "meminfo")
        self.set_available_mib(65536)

    def _build_stub_bin(self):
        super()._build_stub_bin()
        os.rename(
            os.path.join(self.stub_bin, "docker"), os.path.join(self.stub_bin, "docker-base")
        )
        self._write_stub("docker", STATS_WRAPPER)

    def _env(self, port_overrides):
        env = super()._env(port_overrides)
        env["DEV_CONTAINER_MEMINFO_PATH"] = self.meminfo
        return env

    def build(self, **kwargs):
        self.build_fixture_repo(with_parent_env=IDENTITY, **kwargs)

    def set_available_mib(self, mib):
        with open(self.meminfo, "w", encoding="utf-8") as handle:
            handle.write(f"MemTotal:       67108864 kB\nMemAvailable:   {mib * 1024} kB\n")

    @property
    def floor_path(self):
        return os.path.join(self.home, ".config", "machine-limits", "memory-floor-mb")

    def set_floor(self, text):
        os.makedirs(os.path.dirname(self.floor_path), exist_ok=True)
        if os.path.isdir(self.floor_path):
            shutil.rmtree(self.floor_path)
        with open(self.floor_path, "w", encoding="utf-8") as handle:
            handle.write(text)

    def floor_is_a_directory(self):
        if os.path.isfile(self.floor_path):
            os.remove(self.floor_path)
        os.makedirs(self.floor_path, exist_ok=True)

    def write_limits(self, **limits):
        with open(os.path.join(self.state_dir, "limits.json"), "w", encoding="utf-8") as handle:
            json.dump(limits, handle)

    def acquire(self, holder="test", *extra):
        return self.run("acquire", "--for", holder, "--as", f"{holder} [{holder}]", *extra)

    def slot_id(self, result):
        out = result.stdout.decode()
        assert result.returncode == 0, result.stderr.decode()
        marker = '"id": "'
        start = out.index(marker) + len(marker)
        return out[start:out.index('"', start)]

    def slot_name(self, slot_id):
        return slot_id[len(self.prefix) + 1:]

    def lease_exists(self, slot_id):
        return os.path.isfile(os.path.join(self.state_dir, "leases", f"{slot_id}.json"))

    def ports_of(self, slot_id):
        values = {}
        with open(os.path.join(self.state_dir, "ports", f"{slot_id}.ports"), encoding="utf-8") as handle:
            for line in handle:
                if "=" in line:
                    key, _, value = line.strip().partition("=")
                    values[key] = value
        return values

    def set_cpu(self, slot_id, percent):
        with open(os.path.join(self.docker_state, f"{slot_id}.cpu"), "w", encoding="utf-8") as handle:
            handle.write(f"{percent}\n")


class FixtureHarness(PoolHarness):
    """A project whose compose layout and port names come from a fixture directory."""

    fixture = None
    compose_dir = None

    def build(self, **kwargs):
        fixture = os.path.join(FIXTURES, self.fixture)
        with open(os.path.join(fixture, "project.env"), encoding="utf-8") as handle:
            text = handle.read()
        self.build_fixture_repo(with_parent_env=IDENTITY, project_env=text)
        top = self.compose_dir.split("/")[0]
        shutil.copytree(os.path.join(fixture, top), os.path.join(self.fixture_repo, top))
        compose_dir = os.path.join(self.fixture_repo, *self.compose_dir.split("/"))
        with open(os.path.join(compose_dir, ".env"), "w", encoding="utf-8") as handle:
            handle.write(IDENTITY)
        install = os.path.join(compose_dir, "install.sh")
        with open(install, "w", encoding="utf-8") as handle:
            handle.write(INSTALL_STUB)
        os.chmod(install, os.stat(install).st_mode | stat.S_IEXEC)
        for args in (("add", "-A"), ("commit", "-q", "-m", f"{self.fixture} layout")):
            subprocess.run(
                ["git", "-C", self.fixture_repo, *args],
                check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )


class SecondHarness(FixtureHarness):
    prefix = "second"
    port_names = ("SSH_PORT", "WEB_PORT")
    fixture = "second-project"
    compose_dir = "deploy/compose"


class ThirdHarness(FixtureHarness):
    prefix = "third"
    port_names = ("GATEWAY_PORT", "API_PORT", "ADMIN_PORT", "METRICS_PORT")
    fixture = "third-project"
    compose_dir = "stack"


class PoolTestCase(unittest.TestCase):
    harness_class = PoolHarness

    def backdate(self, slot_id, seconds):
        path = os.path.join(self.harness.state_dir, "leases", f"{slot_id}.json")
        with open(path, encoding="utf-8") as handle:
            lease = json.load(handle)
        lease["since"] = int(time.time()) - seconds
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(lease, handle)

    def setUp(self):
        self.harness = self.harness_class()
        self.addCleanup(self.harness.cleanup)

    def another(self, harness_class):
        other = harness_class()
        self.addCleanup(other.cleanup)
        shutil.rmtree(other.state_dir)
        other.state_dir = self.harness.state_dir
        other.docker_state = self.harness.docker_state
        other.docker_log = self.harness.docker_log
        return other

    def output(self, result):
        return result.stdout.decode() + result.stderr.decode()


class SharedBehaviour:
    """Everything a connected project gets from the pool, whatever its layout."""

    def test_acquire_run_and_release_work(self):
        self.harness.build()

        acquired = self.harness.acquire("worker")
        slot_id = self.harness.slot_id(acquired)
        name = self.harness.slot_name(slot_id)
        self.assertTrue(self.harness.lease_exists(slot_id))
        ran = self.harness.run("run", name, "--", "sh", "-c", "echo pooled")
        self.assertEqual(ran.stdout.decode().strip(), "pooled", self.output(ran))
        released = self.harness.run("release", name, "--for", "worker")

        self.assertEqual(released.returncode, 0, self.output(released))
        self.assertFalse(self.harness.lease_exists(slot_id))

    def test_a_slot_gets_ports_for_exactly_the_projects_names(self):
        self.harness.build()

        slot_id = self.harness.slot_id(self.harness.acquire("worker"))

        ports = self.harness.ports_of(slot_id)
        self.assertEqual(set(ports), set(self.harness.port_names))
        self.assertEqual(len(set(ports.values())), len(ports))

    def test_ports_come_from_the_slots_number_in_the_order_the_project_names_them(self):
        self.harness.build()

        slot_id = self.harness.slot_id(self.harness.acquire("worker"))

        number = int(slot_id.rsplit("-", 1)[1])
        ports = self.harness.ports_of(slot_id)
        for index, name in enumerate(self.harness.port_names):
            self.assertEqual(int(ports[name]), slot_port(number, index), name)

    def test_ttl_is_refused_saying_activity_keeps_a_slot_live(self):
        self.harness.build()

        result = self.harness.acquire("worker", "--ttl", "1h")

        self.assertNotEqual(result.returncode, 0, self.output(result))
        err = result.stderr.decode().lower()
        self.assertIn("activity", err)
        self.assertIn("live", err)
        self.assertEqual(os.listdir(os.path.join(self.harness.state_dir, "leases")), [])

    def test_a_lease_idle_past_the_limit_frees_its_number(self):
        self.harness.build()
        self.harness.write_limits(machine_max=1, idle_limit=3)
        self.harness.slot_id(self.harness.acquire("first"))

        time.sleep(4)
        second = self.harness.acquire("second")

        self.assertEqual(second.returncode, 0, self.output(second))

    def test_a_lease_inside_the_limit_still_counts(self):
        self.harness.build()
        self.harness.write_limits(machine_max=1, idle_limit=600)
        self.harness.slot_id(self.harness.acquire("first"))

        second = self.harness.acquire("second")

        self.assertNotEqual(second.returncode, 0, self.output(second))
        self.assertIn("1/1", second.stderr.decode())


class DemoLayout(SharedBehaviour, PoolTestCase):
    harness_class = PoolHarness


class SecondLayout(SharedBehaviour, PoolTestCase):
    harness_class = SecondHarness


class ThirdLayout(SharedBehaviour, PoolTestCase):
    harness_class = ThirdHarness


class OnePool(PoolTestCase):

    def test_live_leases_of_both_fixtures_count_against_machine_max(self):
        second = self.another(SecondHarness)
        self.harness.build()
        second.build()
        self.harness.write_limits(machine_max=2)
        first = self.harness.acquire("one")
        taken = second.acquire("two")
        self.assertEqual(first.returncode, 0, self.output(first))
        self.assertEqual(taken.returncode, 0, self.output(taken))

        third = self.harness.acquire("three")
        fourth = second.acquire("four")

        for refused in (third, fourth):
            self.assertNotEqual(refused.returncode, 0, self.output(refused))
            self.assertIn("2/2", refused.stderr.decode())

    def test_two_projects_never_get_overlapping_ports(self):
        second = self.another(SecondHarness)
        self.harness.build()
        second.build()
        taken = []

        for harness in (self.harness, second, self.harness, second):
            slot_id = harness.slot_id(harness.acquire("worker"))
            taken += list(harness.ports_of(slot_id).values())

        self.assertEqual(len(taken), 10)
        self.assertEqual(len(set(taken)), len(taken), taken)

    def test_three_layouts_with_different_port_names_share_one_set_of_numbers(self):
        others = [self.another(SecondHarness), self.another(ThirdHarness)]
        self.harness.build()
        for other in others:
            other.build()
        taken = []
        numbers = []

        for harness in (self.harness, *others, self.harness, *others):
            slot_id = harness.slot_id(harness.acquire("worker"))
            numbers.append(slot_id.rsplit("-", 1)[1])
            taken += list(harness.ports_of(slot_id).values())

        self.assertEqual(len(set(numbers)), 6, numbers)
        self.assertEqual(len(taken), 2 * (3 + 2 + 4))
        self.assertEqual(len(set(taken)), len(taken), taken)

    def test_run_still_refuses_another_projects_slot(self):
        second = self.another(SecondHarness)
        self.harness.build()
        second.build()
        subprocess.run(
            ["git", "-C", second.fixture_repo, "remote", "set-url", "origin", "https://example.invalid/second.git"],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        shutil.rmtree(second.slots_dir, ignore_errors=True)
        os.makedirs(self.harness.slots_dir, exist_ok=True)
        os.symlink(self.harness.slots_dir, second.slots_dir)
        name = self.harness.slot_name(self.harness.slot_id(self.harness.acquire("worker")))

        result = second.run("run", name, "--", "sh", "-c", "echo reached")

        self.assertNotEqual(result.returncode, 0, self.output(result))
        self.assertNotIn("reached", result.stdout.decode())
        self.assertIn("not this project's own slot", result.stderr.decode())

    def test_a_leftover_folder_without_a_lease_does_not_reduce_what_acquire_hands_out(self):
        self.harness.build()
        self.harness.write_limits(machine_max=1)
        for name in ("slot-1", "slot-2", "slot-3"):
            self.harness.make_bare_slot(name)

        result = self.harness.acquire("worker")

        self.assertEqual(result.returncode, 0, self.output(result))
        self.assertTrue(self.harness.lease_exists(self.harness.slot_id(result)))


class IdleLimitDefault(PoolTestCase):
    """With nothing set the idle limit is two hours, for every project alike."""

    def test_a_lease_idle_for_just_under_two_hours_still_counts(self):
        self.harness.build()
        self.harness.write_limits(machine_max=1)
        slot_id = self.harness.slot_id(self.harness.acquire("first"))
        self.backdate(slot_id, 2 * 3600 - 300)

        second = self.harness.acquire("second")

        self.assertNotEqual(second.returncode, 0, self.output(second))
        self.assertIn("1/1", second.stderr.decode())

    def test_a_lease_idle_for_just_over_two_hours_frees_its_number(self):
        self.harness.build()
        self.harness.write_limits(machine_max=1)
        slot_id = self.harness.slot_id(self.harness.acquire("first"))
        self.backdate(slot_id, 2 * 3600 + 300)

        second = self.harness.acquire("second")

        self.assertEqual(second.returncode, 0, self.output(second))

    def test_the_limit_is_one_value_for_the_machine_in_the_limits_file(self):
        self.harness.build()
        self.harness.write_limits(machine_max=1, idle_limit=60)
        slot_id = self.harness.slot_id(self.harness.acquire("first"))
        self.backdate(slot_id, 120)

        second = self.harness.acquire("second")

        self.assertEqual(second.returncode, 0, self.output(second))


class ActivityKeepsASlotLive(PoolTestCase):

    def fill_two(self):
        self.harness.build()
        self.harness.write_limits(machine_max=2, idle_limit=8)
        first = self.harness.slot_id(self.harness.acquire("first"))
        second = self.harness.slot_id(self.harness.acquire("second"))
        return first, second

    def assert_pool_holds(self):
        third = self.harness.acquire("third")
        self.assertEqual(third.returncode, 0, self.output(third))
        fourth = self.harness.acquire("fourth")
        self.assertNotEqual(fourth.returncode, 0, self.output(fourth))
        self.assertIn("2/2", fourth.stderr.decode())

    def test_a_run_counts_as_activity_and_the_idle_slot_is_freed(self):
        # Both leases are backdated well past the limit instead of sleeping, so
        # the outcome does not depend on how fast the runner is.
        self.harness.build()
        self.harness.write_limits(machine_max=2, idle_limit=60)
        first = self.harness.slot_id(self.harness.acquire("first"))
        second = self.harness.slot_id(self.harness.acquire("second"))
        self.backdate(first, 600)
        self.backdate(second, 600)
        ran = self.harness.run("run", self.harness.slot_name(first), "--", "true")
        self.assertEqual(ran.returncode, 0, self.output(ran))

        self.assert_pool_holds()

        self.assertTrue(self.harness.lease_exists(first))

    def test_cpu_above_the_threshold_keeps_a_lease_live(self):
        first, second = self.fill_two()
        self.harness.set_cpu(first, "95.00")
        self.harness.set_cpu(second, "0.00")
        time.sleep(10)

        self.assert_pool_holds()

        self.assertTrue(self.harness.lease_exists(first))

    def test_process_names_play_no_part(self):
        first, second = self.fill_two()
        self.harness.set_container_procs(self.harness.slot_name(first), ["node", "vite", "claude"])
        self.harness.set_container_procs(self.harness.slot_name(second), ["node", "vite", "claude"])
        time.sleep(10)

        third = self.harness.acquire("third")
        fourth = self.harness.acquire("fourth")

        self.assertEqual(third.returncode, 0, self.output(third))
        self.assertEqual(fourth.returncode, 0, self.output(fourth))


class NoRenewalAndNoTerm(PoolTestCase):

    def test_renew_is_refused_saying_activity_keeps_a_slot_live(self):
        self.harness.build()
        slot_id = self.harness.slot_id(self.harness.acquire("worker"))

        for extra in ((), ("--ttl", "1h")):
            result = self.harness.run("renew", self.harness.slot_name(slot_id), "--for", "worker", *extra)

            self.assertNotEqual(result.returncode, 0, self.output(result))
            err = result.stderr.decode().lower()
            self.assertIn("activity", err)
            self.assertIn("live", err)

    def test_the_usage_text_offers_neither_ttl_nor_renew(self):
        self.harness.build()

        result = self.harness.run("acquire", "--help")

        text = self.output(result)
        self.assertNotIn("--ttl", text)
        self.assertNotIn("renew", text)


class MemoryFloor(PoolTestCase):

    def wait_acquire(self, timeout="4"):
        return self.harness.acquire("worker", "--wait", "--timeout", timeout)

    def assert_waited_for_memory(self, result):
        self.assertEqual(result.returncode, 3, self.output(result))
        text = self.output(result).lower()
        self.assertIn("memory", text)
        self.assertIn("floor", text)

    def test_below_the_floor_acquire_waits_and_says_why(self):
        self.harness.build()
        self.harness.set_floor("4096\n")
        self.harness.set_available_mib(1024)

        result = self.wait_acquire()

        self.assert_waited_for_memory(result)
        self.assertEqual(os.listdir(os.path.join(self.harness.state_dir, "leases")), [])

    def test_above_the_floor_the_same_acquire_proceeds(self):
        self.harness.build()
        self.harness.set_floor("4096\n")
        self.harness.set_available_mib(8192)

        result = self.wait_acquire()

        self.assertEqual(result.returncode, 0, self.output(result))

    def test_a_changed_floor_applies_to_the_next_acquire_without_a_restart(self):
        self.harness.build()
        self.harness.set_available_mib(2048)
        self.harness.set_floor("4096\n")
        self.assert_waited_for_memory(self.wait_acquire())

        self.harness.set_floor("1024\n")
        result = self.wait_acquire()

        self.assertEqual(result.returncode, 0, self.output(result))

    def test_a_floor_changed_while_acquire_waits_is_picked_up(self):
        self.harness.build()
        self.harness.set_available_mib(2048)
        self.harness.set_floor("4096\n")
        log = os.path.join(self.harness.tmp, "waiting.log")
        proc = self.harness.spawn(
            "acquire", "--for", "worker", "--as", "worker [worker]", "--wait", "--timeout", "60", log=log
        )
        self.addCleanup(proc.kill)
        deadline = time.monotonic() + 30
        said = ""
        while time.monotonic() < deadline and "memory" not in said.lower():
            time.sleep(0.5)
            with open(log, encoding="utf-8") as handle:
                said = handle.read()
        self.assertIn("memory", said.lower())

        self.harness.set_floor("1024\n")

        self.assertEqual(proc.wait(timeout=50), 0)

    def test_an_absent_floor_file_means_the_default_of_2048(self):
        self.harness.build()
        self.assertFalse(os.path.exists(self.harness.floor_path))
        self.harness.set_available_mib(1024)
        self.assert_waited_for_memory(self.wait_acquire())

        self.harness.set_available_mib(4096)
        result = self.wait_acquire()

        self.assertEqual(result.returncode, 0, self.output(result))

    def test_an_unreadable_floor_file_means_the_default_not_a_crash(self):
        self.harness.build()
        for setup in (lambda: self.harness.set_floor("plenty\n"), self.harness.floor_is_a_directory):
            setup()
            self.harness.set_available_mib(1024)
            blocked = self.wait_acquire()
            self.assert_waited_for_memory(blocked)
            for crash in ("syntax error", "unbound variable", "Traceback", "integer expression"):
                self.assertNotIn(crash, self.output(blocked))

            self.harness.set_available_mib(4096)
            result = self.wait_acquire()

            self.assertEqual(result.returncode, 0, self.output(result))
            name = self.harness.slot_name(self.harness.slot_id(result))
            self.harness.run("release", name, "--for", "worker")


class PerProjectLimitsAreRefused(unittest.TestCase):
    REFUSED = ("SLOT_CEILING", "SLOT_COUNT", "SLOT_PORT_RANGE", "IDLE_LIMIT")

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pool-project-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def project_with(self, extra):
        with open(os.path.join(SECOND_FIXTURE, "project.env"), encoding="utf-8") as handle:
            text = handle.read()
        with open(os.path.join(self.tmp, "project.env"), "w", encoding="utf-8") as handle:
            handle.write(text + extra + "\n")
        return self.tmp

    def read(self, directory):
        env = dict(os.environ, DEV_CONTAINER_HOME=os.path.join(self.tmp, "state"))
        return subprocess.run(
            [os.path.join(REPO_ROOT, "bin/dev-container"), "project", directory],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, cwd=self.tmp, timeout=30,
        )

    def test_each_per_project_limit_is_refused_by_name_pointing_at_machine_max(self):
        for key in self.REFUSED:
            result = self.read(self.project_with(f"{key}=3"))

            self.assertNotEqual(result.returncode, 0, key)
            err = result.stderr.decode()
            self.assertIn(key, err)
            self.assertIn("machine_max", err)


class SeedCopiesOnlyAllowedKeysAndFiles(PoolTestCase):
    PARENT_ENV = IDENTITY + "LOG_LEVEL=debug\nTHEME=dark\nAPI_SECRET=hunter2\nDB_PASSWORD=swordfish\n"

    def acquire_seeded(self):
        self.harness.build_fixture_repo(
            with_parent_env=self.PARENT_ENV,
            project_env=project_env_text(
                SLOT_ENV_KEYS="LOG_LEVEL THEME", SLOT_COPY_FILES=".local/settings.json"
            ),
        )
        local = os.path.join(self.harness.fixture_repo, ".local")
        for name, text in (("settings.json", "{}"), ("credentials.json", "token"), ("notes.txt", "n")):
            with open(os.path.join(local, name), "w", encoding="utf-8") as handle:
                handle.write(text)
        result = self.harness.acquire("worker")
        return self.harness.slot_name(self.harness.slot_id(result))

    def test_only_listed_env_keys_reach_the_slot(self):
        name = self.acquire_seeded()

        env = self.harness.read_env(name)

        self.assertEqual(env["LOG_LEVEL"], "debug")
        self.assertEqual(env["THEME"], "dark")
        self.assertNotIn("API_SECRET", env)
        self.assertNotIn("DB_PASSWORD", env)
        with open(self.harness.env_path(name), encoding="utf-8") as handle:
            self.assertNotIn("hunter2", handle.read())

    def test_only_listed_files_are_copied_into_the_slot(self):
        name = self.acquire_seeded()
        local = os.path.join(self.harness.slot_dir(name), ".local")

        self.assertTrue(os.path.isfile(os.path.join(local, "settings.json")))
        self.assertFalse(os.path.exists(os.path.join(local, "credentials.json")))
        self.assertFalse(os.path.exists(os.path.join(local, "notes.txt")))


if __name__ == "__main__":
    unittest.main()
