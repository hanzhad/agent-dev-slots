import os
import subprocess

from harness import DevContainerTestCase, project_env_text

SEEDED = project_env_text(SLOT_ENV_KEYS="FIREWALL_EXTRA_DOMAINS")

# The few lines of a gateway entrypoint that read FIREWALL_EXTRA_DOMAINS:
# space- or comma-separated, empty entries dropped, one host per line.
BS = chr(92)
NL = chr(10)
STUB = NL.join([
    "#!/bin/sh",
    'FILTER="$1"',
    ': > "$FILTER"',
    "printf '%s" + BS + "n' \"$FIREWALL_EXTRA_DOMAINS\" | tr ', ' '" + BS + "n" + BS + "n' | grep -v '^$' >> \"$FILTER\"",
    "",
])


def parent_env(value):
    return NL.join([
        "GIT_USER_NAME=demo-agent",
        "GIT_USER_EMAIL=demo-agent@example.com",
        f"FIREWALL_EXTRA_DOMAINS={value}",
        "",
    ])


class FirewallStubCase(DevContainerTestCase):
    def stub_sees(self, name):
        """Runs the stub with the value the engine put in the slot's env file."""
        value = self.harness.read_env(name).get("FIREWALL_EXTRA_DOMAINS", "")
        script = os.path.join(self.harness.tmp, "firewall-stub.sh")
        filt = os.path.join(self.harness.tmp, f"filter-{name}.txt")
        with open(script, "w", encoding="utf-8") as handle:
            handle.write(STUB)
        env = dict(os.environ, FIREWALL_EXTRA_DOMAINS=value)
        result = subprocess.run(["sh", script, filt], env=env, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        with open(filt, encoding="utf-8") as handle:
            return handle.read().split(NL)[:-1]

    def acquire(self, project_value, *flags, build=True):
        if build:
            self.harness.build_fixture_repo(with_parent_env=parent_env(project_value), project_env=SEEDED)
        result = self.harness.run("acquire", "--for", "task-30", "--as", "Agent [aaaa]", *flags)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        return self.harness.acquired_slot_name(result)


class FirewallStub(FirewallStubCase):
    def test_the_slots_hosts_reach_the_stub_intact_as_separate_lines(self):
        name = self.acquire("seed.example.invalid", "--allow-host", "a.example.com,b.example.com")

        self.assertEqual(self.stub_sees(name), ["seed.example.invalid", "a.example.com", "b.example.com"])

    def test_a_project_value_with_commas_and_spaces_loses_and_glues_nothing(self):
        project = "x.example.com, y.example.com z.example.com"
        name = self.acquire(project, "--allow-host", "a.example.com,b.example.com")

        self.assertEqual(
            self.stub_sees(name),
            ["x.example.com", "y.example.com", "z.example.com", "a.example.com", "b.example.com"],
        )

    def test_a_space_separated_project_value_keeps_every_host(self):
        name = self.acquire("x.example.com y.example.com", "--allow-host", "a.example.com")

        self.assertEqual(self.stub_sees(name), ["x.example.com", "y.example.com", "a.example.com"])

    def test_the_next_slot_gets_only_the_project_value_after_release(self):
        project = "x.example.com, y.example.com z.example.com"
        first = self.acquire(project, "--allow-host", "a.example.com,b.example.com")
        result = self.harness.run("release", first, "--for", "task-30")
        self.assertEqual(result.returncode, 0, result.stderr.decode())

        second = self.acquire(project, build=False)

        self.assertEqual(self.stub_sees(second), ["x.example.com", "y.example.com", "z.example.com"])

    def test_a_host_nobody_asked_for_is_absent(self):
        name = self.acquire("seed.example.invalid", "--allow-host", "a.example.com")

        seen = self.stub_sees(name)
        self.assertNotIn("other.example.com", seen)
        self.assertEqual(seen, ["seed.example.invalid", "a.example.com"])
