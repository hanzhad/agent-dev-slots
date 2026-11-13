import json
import os

import test_status_json
from harness import DevContainerTestCase, PROJECT_PREFIX, project_env_text

ENV = (
    "GIT_USER_NAME=demo-agent\nGIT_USER_EMAIL=demo-agent@example.com\n"
    "FIREWALL_EXTRA_DOMAINS=seed.example.invalid\n"
)
SEEDED = project_env_text(SLOT_ENV_KEYS="FIREWALL_EXTRA_DOMAINS")


class AllowHostCase(DevContainerTestCase):
    def acquire(self, *flags, build=True):
        if build:
            self.harness.build_fixture_repo(with_parent_env=ENV, project_env=SEEDED)
        return self.harness.run("acquire", "--for", "task-30", "--as", "Agent [aaaa]", *flags)

    def acquired(self, *flags, build=True):
        result = self.acquire(*flags, build=build)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        return self.harness.acquired_slot_name(result), result.stdout.decode()

    def firewall(self, name):
        value = self.harness.read_env(name).get("FIREWALL_EXTRA_DOMAINS", "")
        return [host for host in value.split(",") if host]

    def state_files(self):
        found = []
        for root, _, files in os.walk(self.harness.state_dir):
            found += [os.path.join(root, f) for f in files]
        return found

    def holding(self, text):
        found = []
        for path in self.state_files():
            with open(path, errors="replace") as handle:
                if text in handle.read():
                    found.append(path)
        return found


class PerSlotHosts(AllowHostCase):
    def test_a_host_is_added_to_the_projects_list_for_that_slot(self):
        name, _ = self.acquired("--allow-host", "api.example.com")

        self.assertEqual(self.firewall(name), ["seed.example.invalid", "api.example.com"])

    def test_the_flag_repeats_and_takes_a_comma_list(self):
        name, _ = self.acquired("--allow-host", "a.example.com,b.example.com", "--allow-host", "c.example.com")

        self.assertEqual(
            self.firewall(name),
            ["seed.example.invalid", "a.example.com", "b.example.com", "c.example.com"],
        )

    def test_a_duplicate_or_a_host_the_project_has_is_listed_once_and_case_is_folded(self):
        name, _ = self.acquired("--allow-host", "API.example.com,api.example.com,seed.example.invalid")

        self.assertEqual(self.firewall(name), ["seed.example.invalid", "api.example.com"])

    def test_without_the_flag_the_projects_list_is_untouched(self):
        name, out = self.acquired()

        self.assertEqual(self.firewall(name), ["seed.example.invalid"])
        self.assertNotIn("allowed hosts", out)

    def test_acquire_prints_the_effective_list(self):
        _, out = self.acquired("--allow-host", "api.example.com")

        self.assertIn("seed.example.invalid", out)
        self.assertIn("api.example.com", out)
        self.assertIn("firewall", out.lower())

    def test_the_hosts_are_not_written_to_the_project_or_the_main_env_and_do_not_reach_another_slot(self):
        first, _ = self.acquired("--allow-host", "api.example.com")
        second, _ = self.acquired(build=False)

        self.assertEqual(self.firewall(second), ["seed.example.invalid"])
        for path in (
            os.path.join(self.harness.fixture_repo, "project.env"),
            os.path.join(self.harness.main_devcontainer(), ".env"),
        ):
            with open(path, encoding="utf-8") as handle:
                self.assertNotIn("api.example.com", handle.read(), path)
        self.assertNotEqual(first, second)

    def test_release_takes_the_hosts_away(self):
        name, _ = self.acquired("--allow-host", "api.example.com")

        result = self.harness.run("release", name, "--for", "task-30")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(self.holding("api.example.com"), [])
        again, _ = self.acquired(build=False)
        self.assertEqual(self.firewall(again), ["seed.example.invalid"])


class Status(test_status_json.StatusCase):
    def test_status_shows_the_effective_list_and_the_slots_own_hosts(self):
        sid = f"{PROJECT_PREFIX}-slot-1"
        self.write_lease("slot-1", self.harness.tmp, age_seconds=30)
        self.write_ports_file(sid)
        os.makedirs(os.path.join(self.state, "env"), exist_ok=True)
        with open(os.path.join(self.state, "env", f"{sid}.env"), "w", encoding="utf-8") as handle:
            handle.write("FIREWALL_EXTRA_DOMAINS=seed.example.invalid,api.example.com" + chr(10))
        os.makedirs(os.path.join(self.state, "allow-hosts"), exist_ok=True)
        with open(os.path.join(self.state, "allow-hosts", f"{sid}.hosts"), "w", encoding="utf-8") as handle:
            handle.write("api.example.com" + chr(10))

        result = self.engine("status", "--json")
        self.assertEqual(result.returncode, 0, self.output(result))
        slot = json.loads(result.stdout.decode())["slots"][0]
        self.assertEqual(slot["allowed_hosts"], ["seed.example.invalid", "api.example.com"])
        self.assertEqual(slot["slot_hosts"], ["api.example.com"])

        result = self.engine("status")
        self.assertEqual(result.returncode, 0, self.output(result))
        self.assertIn("api.example.com", result.stdout.decode())

    def test_a_slot_with_no_extra_hosts_reports_empty_lists(self):
        self.write_lease("slot-1", self.harness.tmp, age_seconds=30)

        result = self.engine("status", "--json")
        slot = json.loads(result.stdout.decode())["slots"][0]
        self.assertEqual(slot["allowed_hosts"], [])
        self.assertEqual(slot["slot_hosts"], [])


class Immutability(AllowHostCase):
    def test_restart_keeps_the_list_and_ignores_a_planted_env_in_the_checkout(self):
        name, _ = self.acquired("--allow-host", "api.example.com")
        slot_dev = os.path.join(self.harness.slot_dir(name), ".devcontainer")
        with open(os.path.join(slot_dev, ".env"), "w", encoding="utf-8") as handle:
            handle.write("FIREWALL_EXTRA_DOMAINS=evil.example.com\n")

        result = self.harness.run("restart", name)

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(self.firewall(name), ["seed.example.invalid", "api.example.com"])

    def test_restart_does_not_repeat_the_hosts(self):
        name, _ = self.acquired("--allow-host", "api.example.com")

        for _ in range(2):
            result = self.harness.run("restart", name)
            self.assertEqual(result.returncode, 0, result.stderr.decode())

        self.assertEqual(self.firewall(name), ["seed.example.invalid", "api.example.com"])

    def test_the_list_is_kept_outside_the_slot_workspace(self):
        name, _ = self.acquired("--allow-host", "api.example.com")
        workspace = os.path.realpath(self.harness.slot_dir(name))

        holders = self.holding("api.example.com")

        self.assertTrue(holders, "no state file holds the list")
        for path in holders:
            self.assertTrue(os.path.realpath(path).startswith(os.path.realpath(self.harness.state_dir) + os.sep), path)
            self.assertFalse(os.path.realpath(path).startswith(workspace + os.sep), path)


class Validation(AllowHostCase):
    def refused(self, value):
        result = self.acquire("--allow-host", value)
        self.assertNotEqual(result.returncode, 0, f"{value!r} was accepted")
        self.assertIn("--allow-host", result.stderr.decode())
        self.assertFalse(
            [p for p in self.state_files() if os.path.basename(os.path.dirname(p)) == "leases"],
            f"{value!r}: a slot was leased",
        )

    def test_a_url_is_refused(self):
        self.refused("https://api.example.com")

    def test_a_path_is_refused(self):
        self.refused("api.example.com/v1")

    def test_a_wildcard_is_refused(self):
        self.refused("*.example.com")

    def test_whitespace_is_refused(self):
        self.refused("api.example.com other.example.com")

    def test_a_port_is_refused(self):
        self.refused("api.example.com:443")

    def test_an_empty_entry_is_refused(self):
        self.refused("a.example.com,,b.example.com")

    def test_a_label_with_a_leading_hyphen_is_refused(self):
        self.refused("-bad.example.com")

    def test_an_env_line_smuggled_in_is_refused(self):
        self.refused("a.example.com\nGH_TOKEN=x")

    def test_the_flag_needs_a_value(self):
        result = self.acquire("--allow-host")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--allow-host needs a value", result.stderr.decode())
