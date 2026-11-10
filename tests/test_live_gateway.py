import os
import re
import shutil
import signal
import subprocess
import time
import unittest
from unittest import mock

from harness import (
    DOCKER_STUB,
    DevContainerHarness,
    DevContainerTestCase,
    PROJECT_PREFIX,
    project_env_text,
)

GATEWAY = "edge-gate"
LIVE = "live"
OTHER_PROJECT = "other-demo"
IDENTITY = "GIT_USER_NAME=demo-agent\nGIT_USER_EMAIL=demo-agent@example.com\n"
WATCH = ["SEARCH_API_KEY", "LOOKUP_API_KEY", "VERIFY_API_KEY"]
VALUES = {
    "SEARCH_API_KEY": "sentinel-search-4c1e",
    "LOOKUP_API_KEY": "sentinel-lookup-77ab",
    "VERIFY_API_KEY": "sentinel-verify-91d0",
}
LIVE_KEYS = {"LIVE_ENV_FILE": ".env", "LIVE_SERVICE": LIVE, "LIVE_GATEWAY": GATEWAY}

# Stands in for docker. A compose call that names the gateway service is
# answered from marker files (gw-<project>, one per project) and never
# reaches the base stub; a `compose run` of the live service executes the
# command with exactly the variables it was told to pass by name. Every call
# is one line in events.log; the commands the tests run append CB/CE lines to
# the same file, so the order of everything is in one place.
DOCKER_WRAPPER = r"""#!/usr/bin/env bash
set -u
fake="${LG_FAKE:?}"
base="$(dirname "$0")/docker-base"
gw="${LG_GATEWAY:-edge-gate}"
lv="${LG_LIVE:-live}"
log="$fake/events.log"
verb="" proj="${COMPOSE_PROJECT_NAME-}" svc="" hit="" envset=""
names=() cmd=() rest=()
for n in ${LG_WATCH-} GH_TOKEN; do [ -n "${!n+x}" ] && envset+="$n,"; done
if [[ "${1:-}" == compose ]]; then
  args=("$@"); i=1
  while (( i < ${#args[@]} )); do
    case "${args[i]}" in
      -p) proj="${args[i+1]:-}"; i=$((i+1)) ;;
      -f|--env-file|--project-directory|--profile) i=$((i+1)) ;;
      up|down|stop|rm|run|exec|ps|ls|config|port) verb="${args[i]}"; break ;;
    esac
    i=$((i+1))
  done
  rest=("${args[@]:i+1}")
  case "$verb" in
    up|rm|stop|down)
      for a in ${rest[@]+"${rest[@]}"}; do
        [[ "$a" == "$gw" ]] && hit+="gw,"
        [[ "$a" == "$lv" ]] && hit+="live,"
      done ;;
    run)
      j=0
      while (( j < ${#rest[@]} )); do
        a="${rest[j]}"
        case "$a" in
          -e) names+=("${rest[j+1]}"); j=$((j+2)) ;;
          -u|--user|-w|--workdir) j=$((j+2)) ;;
          -*) j=$((j+1)) ;;
          *) svc="$a"; cmd=("${rest[@]:j+1}"); break ;;
        esac
      done ;;
  esac
else
  verb="${1:-}"
fi
printf 'CALL %s %s [%s] env=%s\n' "$verb" "$proj" "$hit" "$envset" >> "$log"
if [[ "$verb" == up && "$hit" == *gw,* ]]; then
  if [ -f "$fake/gw-fail" ]; then
    echo "GWFAIL $proj" >> "$log"
    echo "service $gw is unhealthy" >&2
    exit 1
  fi
  prior=none; [ -e "$fake/gw-$proj" ] && prior=up
  echo "GWSTART $proj prior=$prior" >> "$log"
  : > "$fake/gw-$proj"
  exit 0
fi
if [[ "$verb" == rm || "$verb" == stop || "$verb" == down ]] && [[ -n "$hit" ]]; then
  echo "GWRM $proj" >> "$log"
  [ -f "$fake/rm-fail" ] && exit 1
  [[ "$hit" == *gw,* ]] && rm -f "$fake/gw-$proj"
  exit 0
fi
if [[ "$verb" == run && "$svc" == "$lv" ]]; then
  state=down; [ -e "$fake/gw-$proj" ] && state=up
  echo "LIVERUN $proj gw=$state names=${names[*]-}" >> "$log"
  envargs=()
  for n in ${names[@]+"${names[@]}"}; do
    if [ -n "${!n+x}" ]; then envargs+=("$n=${!n}"); fi
  done
  exec env -i PATH="$PATH" HOME="${HOME:-/}" LG_PROJ="$proj" LG_FAKE="$fake" \
    ${envargs[@]+"${envargs[@]}"} "${cmd[@]}"
fi
[[ "$verb" == rm ]] && exit 0
exec "$base" "$@"
"""


def text(res):
    return res.stdout.decode() + res.stderr.decode()


def write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(content)


def read_if(path):
    if not os.path.isfile(path):
        return ""
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def wait_for(what, check, timeout=10.0):
    deadline = time.monotonic() + timeout
    while not check():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(0.05)


class LiveGatewayTestCase(DevContainerTestCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("GH_TOKEN", None)
        self.fake = os.path.join(self.harness.tmp, "live-fake")
        os.makedirs(self.fake)
        os.environ["LG_FAKE"] = self.fake
        os.environ["LG_WATCH"] = " ".join(WATCH)
        self.logs = []
        self.name = self.start_project(self.harness, PROJECT_PREFIX)
        self.sid = f"{PROJECT_PREFIX}-{self.name}"
        self.reset_events()

    # -- the project and its slots ---------------------------------------

    def state_for(self, harness):
        return {"DEV_CONTAINER_HOME": self.harness.state_dir}

    def start_project(self, harness, project, **keys):
        harness._write_stub("docker-base", DOCKER_STUB)
        harness._write_stub("docker", DOCKER_WRAPPER)
        harness.build_fixture_repo(
            with_parent_env=IDENTITY, project_env=project_env_text(PROJECT=project)
        )
        # The slot is made under a plain project file; the live keys come after.
        name = self.acquire_on(harness, project)
        self.configure(harness, project, **keys)
        return name

    def acquire_on(self, harness, project, holder="test"):
        result = harness.run(
            "acquire", "--for", holder, port_overrides=self.state_for(harness)
        )
        self.assertEqual(result.returncode, 0, text(result))
        match = re.search(r'"id": "([^"]+)"', result.stdout.decode())
        self.assertTrue(match, result.stdout.decode())
        return match.group(1)[len(project) + 1:]

    def configure(self, harness, project, **keys):
        values = dict(LIVE_KEYS)
        values.update(keys)
        values = {k: v for k, v in values.items() if v is not None}
        write(
            os.path.join(harness.fixture_repo, "project.env"),
            project_env_text(PROJECT=project, **values),
        )
        self.owner_env(["%s=%s" % kv for kv in VALUES.items()], harness)

    def owner_env(self, lines, harness=None):
        harness = harness or self.harness
        write(os.path.join(harness.fixture_repo, ".env"), "".join(l + "\n" for l in lines))

    # -- running -----------------------------------------------------------

    def events_path(self):
        return os.path.join(self.fake, "events.log")

    def events(self):
        return [l for l in read_if(self.events_path()).split("\n") if l]

    def of(self, tag):
        return [l for l in self.events() if l.split()[0] == tag]

    def reset_events(self):
        write(self.events_path(), "")

    def flag(self, name, content=""):
        write(os.path.join(self.fake, name), content)

    def unflag(self, name):
        path = os.path.join(self.fake, name)
        if os.path.exists(path):
            os.remove(path)

    def gateways_up(self):
        return sorted(n for n in os.listdir(self.fake) if n.startswith("gw-") and n != "gw-fail")

    def probe(self, seconds=0, code=0):
        """A command that notes when it began, whether the gateway was up, and when it ended."""
        ev = '"$LG_FAKE/events.log"'
        up = '$([ -e "$LG_FAKE/gw-$LG_PROJ" ] && echo up || echo down)'
        return [
            "sh", "-c",
            f'echo "CB $LG_PROJ gw={up}" >> {ev}; sleep {seconds}; echo "CE $LG_PROJ" >> {ev}; exit {code}',
        ]

    def hang(self):
        ev = '"$LG_FAKE/events.log"'
        return [
            "sh", "-c",
            f'echo "CB $LG_PROJ gw=x" >> {ev}; : > "$LG_FAKE/hang-started"; '
            'while [ -f "$LG_FAKE/hang" ]; do sleep 0.05; done; : > "$LG_FAKE/hang-ended"',
        ]

    def argv_of(self, names, cmd, name=None):
        flags = [a for n in names for a in ("--live", n)]
        return ["run", name or self.name, *flags, "--", *cmd]

    def live(self, names=("SEARCH_API_KEY",), cmd=None, name=None, extra=None, harness=None):
        harness = harness or self.harness
        env = self.state_for(harness)
        env.update(extra or {})
        return harness.run(*self.argv_of(names, cmd or self.probe(), name), port_overrides=env)

    def ordinary(self, cmd=("true",), name=None, harness=None):
        harness = harness or self.harness
        return harness.run(
            "run", name or self.name, "--", *cmd, port_overrides=self.state_for(harness)
        )

    def spawn_live(self, names=("SEARCH_API_KEY",), cmd=None, name=None, harness=None):
        harness = harness or self.harness
        log = os.path.join(self.fake, f"spawn-{len(self.logs)}.log")
        self.logs.append(log)
        proc = harness.spawn(
            *self.argv_of(names, cmd or self.probe(), name),
            port_overrides=self.state_for(harness), log=log,
        )
        self.addCleanup(self.reap, proc)
        return proc

    def reap(self, proc):
        if proc.poll() is None:
            proc.kill()
        proc.wait()

    def leave_a_gateway(self, harness=None, name=None):
        """A live run killed past trapping: its gateway and its lock stay behind."""
        self.flag("hang")
        self.unflag("hang-started")
        self.unflag("hang-ended")
        proc = self.spawn_live(cmd=self.hang(), name=name, harness=harness)
        wait_for("the live command to be running", lambda: os.path.exists(os.path.join(self.fake, "hang-started")))
        proc.kill()
        proc.wait()
        self.unflag("hang")
        wait_for("the orphaned command to end", lambda: os.path.exists(os.path.join(self.fake, "hang-ended")))
        self.assertTrue(self.gateways_up(), "a killed run was expected to leave its gateway")
        self.reset_events()

    def assert_serial(self):
        running = {}
        for line in self.events():
            parts = line.split()
            tag, proj = parts[0], parts[1] if len(parts) > 1 else ""
            if tag == "CB":
                running[proj] = running.get(proj, 0) + 1
                self.assertLessEqual(running[proj], 1, "two live commands at once\n" + "\n".join(self.events()))
            elif tag == "CE":
                running[proj] = running.get(proj, 0) - 1
            elif tag == "GWSTART":
                self.assertEqual(running.get(proj, 0), 0, "a gateway started under a running command")
                self.assertEqual(parts[2], "prior=none", "a gateway was started over another one")
            elif tag == "GWRM":
                self.assertEqual(running.get(proj, 0), 0, "a gateway was removed under a running command")

    def index_of(self, prefix, start=0):
        for i, line in enumerate(self.events()):
            if i >= start and line.startswith(prefix):
                return i
        return -1


class LiveRunBracketsTheGateway(LiveGatewayTestCase):
    def test_the_gateway_is_up_for_the_command_and_removed_after_it(self):
        res = self.live()

        self.assertEqual(res.returncode, 0, text(res))
        start = self.index_of("GWSTART")
        begin = self.index_of("CB")
        end = self.index_of("CE")
        gone = self.index_of("GWRM", end)
        self.assertGreaterEqual(start, 0, "the gateway was never started")
        self.assertGreater(begin, start, "the command ran before the gateway")
        self.assertGreater(end, begin)
        self.assertGreater(gone, end, "the gateway was not removed after the command")
        self.assertIn("gw=up", self.events()[begin])
        self.assertEqual(self.gateways_up(), [])

    def test_the_command_runs_in_the_live_service_and_never_in_the_apps_exec(self):
        res = self.live()

        self.assertEqual(res.returncode, 0, text(res))
        self.assertEqual(len(self.of("LIVERUN")), 1)
        self.assertEqual([l for l in self.of("CALL") if l.startswith("CALL exec")], [])

    def test_the_slots_own_containers_keep_running(self):
        self.live()

        self.assertTrue(
            os.path.isfile(os.path.join(self.harness.docker_state, f"{self.sid}.running")),
            "removing the gateway took the slot's own services with it",
        )

    def test_the_command_runs_exactly_as_given(self):
        out = os.path.join(self.fake, "args")
        cmd = ["sh", "-c", f'printf "%s|" "$@" > "{out}"', "sh", "a b", "", "--live", "X y", "--"]

        res = self.live(cmd=cmd)

        self.assertEqual(res.returncode, 0, text(res))
        self.assertEqual(read_if(out), "a b||--live|X y|--|")

    def test_the_commands_exit_code_passes_through_and_the_gateway_still_goes(self):
        for code in (1, 37, 126, 255):
            with self.subTest(code=code):
                self.reset_events()
                res = self.live(cmd=self.probe(code=code))

                self.assertEqual(res.returncode, code, text(res))
                self.assertEqual(len(self.of("LIVERUN")), 1)
                self.assertEqual(self.gateways_up(), [])

    def test_the_commands_exit_code_survives_a_gateway_that_will_not_go(self):
        self.flag("rm-fail")

        res = self.live(cmd=self.probe(code=7))

        self.assertEqual(len(self.of("LIVERUN")), 1)
        self.assertEqual(res.returncode, 7, text(res))

    def test_a_killed_run_removes_its_gateway(self):
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            with self.subTest(signal=sig.name):
                self.reset_events()
                self.unflag("hang-started")
                self.flag("hang")
                proc = self.spawn_live(cmd=self.hang())
                wait_for("the command to be running", lambda: os.path.exists(os.path.join(self.fake, "hang-started")))
                self.assertTrue(self.gateways_up(), "the gateway must be up while the command runs")

                proc.send_signal(sig)
                proc.wait(timeout=20)

                self.assertNotEqual(proc.returncode, 0, "a killed run must not report success")
                self.assertEqual(self.gateways_up(), [], "the gateway outlived a killed run")
                self.assertGreater(self.index_of("GWRM", self.index_of("CB")), -1)
                self.unflag("hang")

    def test_the_next_live_run_does_not_wait_on_a_lock_a_killed_run_left(self):
        self.leave_a_gateway()

        res = self.live(names=["LOOKUP_API_KEY"])

        self.assertEqual(res.returncode, 0, text(res))
        self.assertEqual(len(self.of("LIVERUN")), 1)
        self.assertEqual(self.gateways_up(), [])


class ALeftoverGatewayIsSwept(LiveGatewayTestCase):
    def test_an_ordinary_run_sweeps_it_and_still_runs_its_command(self):
        self.leave_a_gateway()

        res = self.ordinary(("sh", "-c", "echo ordinary-output"))

        self.assertEqual(res.returncode, 0, text(res))
        self.assertIn("ordinary-output", res.stdout.decode())
        self.assertEqual(self.gateways_up(), [], "the next run did not sweep the gateway")
        self.assertGreater(self.index_of("GWRM"), -1)

    def test_a_live_run_sweeps_it_before_starting_its_own(self):
        self.leave_a_gateway()

        res = self.live()

        self.assertEqual(res.returncode, 0, text(res))
        swept = self.index_of("GWRM")
        started = self.index_of("GWSTART")
        self.assertGreater(swept, -1, "the stale gateway was not removed")
        self.assertGreater(started, swept)
        self.assertTrue(self.of("GWSTART")[0].endswith("prior=none"), "the stale gateway was the one used")

    def test_release_sweeps_it(self):
        self.leave_a_gateway()

        res = self.harness.run("release", self.name, "--for", "test")

        self.assertEqual(res.returncode, 0, text(res))
        self.assertEqual(self.gateways_up(), [], "release left the gateway behind")

    def test_release_of_a_slot_with_nothing_left_over_still_succeeds(self):
        res = self.harness.run("release", self.name, "--for", "test")

        self.assertEqual(res.returncode, 0, text(res))
        self.assertEqual(self.gateways_up(), [])


class AnOrdinaryRunKeepsTheGatewayDown(LiveGatewayTestCase):
    def test_it_starts_no_gateway_and_no_live_container_and_execs_as_before(self):
        res = self.ordinary(("sh", "-c", "echo plain"))

        self.assertEqual(res.returncode, 0, text(res))
        self.assertIn("plain", res.stdout.decode())
        self.assertEqual(self.of("GWSTART"), [])
        self.assertEqual(self.of("LIVERUN"), [])
        self.assertEqual(len([l for l in self.of("CALL") if l.startswith("CALL exec")]), 1)

    def test_it_hands_no_key_over_though_the_owner_file_holds_every_one(self):
        res = self.ordinary()

        self.assertEqual(res.returncode, 0, text(res))
        for line in self.of("CALL"):
            self.assertNotRegex(line, r"env=.*API_KEY", line)


class AGatewayThatDoesNotStartStopsTheRun(LiveGatewayTestCase):
    def test_no_command_runs_the_gateway_is_named_and_nothing_is_left(self):
        self.flag("gw-fail")

        res = self.live(names=["SEARCH_API_KEY", "LOOKUP_API_KEY"])

        self.assertNotEqual(res.returncode, 0, "a gateway that never came up must fail the run")
        self.assertTrue(self.of("GWFAIL"), "the fake should have refused the gateway")
        self.assertEqual(self.of("LIVERUN"), [], "the command ran without its gateway")
        self.assertEqual([l for l in self.of("CALL") if l.startswith("CALL exec")], [])
        self.assertRegex(res.stderr.decode(), r"(?i)gateway|" + GATEWAY)
        self.assertEqual(self.gateways_up(), [])
        for value in VALUES.values():
            self.assertNotIn(value, text(res))

    def test_no_key_reaches_any_docker_call_but_the_run(self):
        for fail in (True, False):
            with self.subTest(gateway_fails=fail):
                self.reset_events()
                (self.flag if fail else self.unflag)("gw-fail")

                self.live(names=["SEARCH_API_KEY"])

                calls = self.of("CALL")
                self.assertTrue(calls, "the script made no docker call at all")
                for line in calls:
                    if not line.startswith("CALL run "):
                        self.assertNotRegex(line, r"env=.*SEARCH_API_KEY", line)

    def test_a_key_missing_from_the_owner_file_refuses_before_anything_starts(self):
        self.owner_env(["LOOKUP_API_KEY=" + VALUES["LOOKUP_API_KEY"]])

        res = self.live(names=["LOOKUP_API_KEY", "SEARCH_API_KEY"])

        self.assertNotEqual(res.returncode, 0)
        self.assertIn("SEARCH_API_KEY", text(res))
        self.assertEqual(self.of("GWSTART"), [], "the gateway was started for a run that could not be made")
        self.assertEqual(self.of("LIVERUN"), [])
        self.assertNotIn(VALUES["LOOKUP_API_KEY"], text(res))


class OneLiveRunAtATimePerProject(LiveGatewayTestCase):
    def two_runs(self, first_name=None, second_name=None):
        first = self.spawn_live(cmd=self.probe(seconds=1), name=first_name)
        wait_for("the first command to be running", lambda: self.of("CB"))
        second = self.spawn_live(names=["LOOKUP_API_KEY"], cmd=self.probe(), name=second_name)
        first.wait(timeout=60)
        second.wait(timeout=60)
        return first, second

    def check_waited_or_refused(self, first, second):
        self.assertEqual(first.returncode, 0, read_if(self.logs[0]))
        ran = len(self.of("LIVERUN"))
        if second.returncode == 0:
            self.assertEqual(ran, 2, "the second run reported success without running its command")
        else:
            self.assertEqual(ran, 1, "a refused run must not have run its command")
        self.assert_serial()
        self.assertEqual(self.gateways_up(), [])

    def test_a_second_live_run_of_a_slot_waits_or_refuses(self):
        first, second = self.two_runs()

        self.check_waited_or_refused(first, second)

    def test_a_live_run_in_another_slot_of_the_project_waits_or_refuses(self):
        other = self.acquire_on(self.harness, PROJECT_PREFIX, holder="second")
        self.reset_events()

        first, second = self.two_runs(first_name=self.name, second_name=other)

        self.check_waited_or_refused(first, second)

    def test_an_ordinary_run_goes_through_and_its_sweep_leaves_the_live_gateway(self):
        first = self.spawn_live(cmd=self.probe(seconds=2))
        wait_for("the live command to be running", lambda: self.of("CB"))
        saw = os.path.join(self.fake, "ordinary-saw")

        res = self.ordinary(("sh", "-c", f'ls "{self.fake}" | grep -c "^gw-" > "{saw}"'))

        self.assertEqual(res.returncode, 0, text(res))
        self.assertEqual(read_if(saw).strip(), "1", "the ordinary run's sweep removed the live gateway")
        first.wait(timeout=60)
        self.assertEqual(first.returncode, 0, read_if(self.logs[0]))
        self.assert_serial()

    def test_a_live_run_of_another_project_is_not_held_up(self):
        other = DevContainerHarness()
        self.addCleanup(other.cleanup)
        other_name = self.start_project(other, OTHER_PROJECT)
        self.reset_events()
        first = self.spawn_live(cmd=self.probe(seconds=3))
        wait_for("the first command to be running", lambda: self.of("CB"))

        second = self.spawn_live(cmd=self.probe(), name=other_name, harness=other)
        wait_for("the other project's command to start while the first still runs", lambda: len(self.of("CB")) == 2, 2.5)

        self.assertNotIn(f"CE {self.sid}", self.of("CE"), "the first run had already finished: nothing was held up")
        first.wait(timeout=60)
        second.wait(timeout=60)
        self.assertEqual(first.returncode, 0, read_if(self.logs[0]))
        self.assertEqual(second.returncode, 0, read_if(self.logs[1]))
        self.assert_serial()

    def test_an_ordinary_run_of_one_project_leaves_the_leftover_gateway_of_another(self):
        other = DevContainerHarness()
        self.addCleanup(other.cleanup)
        other_name = self.start_project(other, OTHER_PROJECT)
        self.leave_a_gateway(harness=other, name=other_name)
        mine = self.gateways_up()

        res = self.ordinary()

        self.assertEqual(res.returncode, 0, text(res))
        self.assertEqual(self.gateways_up(), mine, "a sweep reached the gateway of another project")


class TheGatewayIsConfiguredInTheProjectFile(LiveGatewayTestCase):
    def test_the_gateway_key_is_a_known_key(self):
        res = self.ordinary()

        self.assertEqual(res.returncode, 0, text(res))
        self.assertNotIn("unknown key", text(res))

    def test_a_live_run_without_a_gateway_key_starts_no_gateway(self):
        self.configure(self.harness, PROJECT_PREFIX, LIVE_GATEWAY=None)

        res = self.live()

        self.assertEqual(res.returncode, 0, text(res))
        self.assertEqual(self.of("GWSTART"), [])
        self.assertEqual(len(self.of("LIVERUN")), 1)

    def test_a_gateway_name_that_is_not_a_service_name_is_refused_by_key(self):
        for bad in ("two words", "--all", "a/b"):
            with self.subTest(value=bad):
                self.reset_events()
                self.configure(self.harness, PROJECT_PREFIX, LIVE_GATEWAY=bad)

                res = self.live()

                self.assertNotEqual(res.returncode, 0)
                self.assertIn("LIVE_GATEWAY", text(res))
                self.assertNotIn("unknown key", text(res))
                self.assertEqual(self.of("LIVERUN"), [])


if __name__ == "__main__":
    unittest.main()
