import json
import os
import pty
import re
import shutil
import stat
import subprocess
import sys
import unittest

if os.path.dirname(__file__) not in sys.path:
    sys.path.insert(0, os.path.dirname(__file__))

from engine_stub import FORBIDDEN_TOOLS, FORBIDDEN_STUB
from harness import (
    DevContainerTestCase,
    OWNER_PORTS,
    PROJECT_PREFIX,
    REPO_ROOT,
    SLOT_PORT_BLOCK,
    project_env_text,
    slot_port,
)

MAIN_ENV = (
    "GIT_USER_NAME=demo-agent\n"
    "GIT_USER_EMAIL=demo-agent@example.com\n"
)
EXPOSE_LIB = os.path.join(REPO_ROOT, "lib", "expose.sh")
THIN = os.path.join(REPO_ROOT, "scripts", "dev-container-thin")
GLOBAL = os.path.join(REPO_ROOT, "scripts", "dev-container-global")


class TestExposePureCore(unittest.TestCase):
    """Pure Core unit tests for lib/expose.sh."""

    def test_expose_lib_defines_required_pure_core_functions(self):
        self.assertTrue(os.path.isfile(EXPOSE_LIB), f"missing {EXPOSE_LIB}")
        probe = subprocess.run(
            [
                "bash",
                "-c",
                '. "$1" && declare -F expose_parse_target >/dev/null && declare -F expose_candidate_ports >/dev/null',
                "test",
                EXPOSE_LIB,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=15,
        )
        self.assertEqual(
            probe.returncode,
            0,
            f"expose_parse_target/expose_candidate_ports missing in {EXPOSE_LIB}: {probe.stderr.decode()}",
        )

    def test_expose_parse_target_table(self):
        self.assertTrue(os.path.isfile(EXPOSE_LIB), f"missing {EXPOSE_LIB}")
        probe = subprocess.run(
            ["bash", "-c", '. "$1" && declare -F expose_parse_target >/dev/null', "test", EXPOSE_LIB],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=15,
        )
        self.assertEqual(probe.returncode, 0, probe.stderr.decode())

        valid_cases = [
            ("3000", "127.0.0.1 3000"),
            ("1", "127.0.0.1 1"),
            ("80", "127.0.0.1 80"),
            ("65535", "127.0.0.1 65535"),
            ("app:3000", "app 3000"),
            ("vite-dev:5173", "vite-dev 5173"),
            ("api_v2:8080", "api_v2 8080"),
            ("db.primary:5432", "db.primary 5432"),
        ]
        for raw, expected in valid_cases:
            with self.subTest(valid=raw):
                res = subprocess.run(
                    ["bash", "-c", '. "$1" && expose_parse_target "$2"', "test", EXPOSE_LIB, raw],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=15,
                )
                self.assertEqual(res.returncode, 0, res.stderr.decode())
                self.assertEqual(res.stdout.decode().strip(), expected)

        invalid_cases = [
            "",
            "0",
            "-1",
            "65536",
            "70000",
            "abc",
            "3000 ",
            ":3000",
            "app:",
            "app:0",
            "app:65536",
            "app:-1",
            "app:abc",
            "a:b:3000",
            "-bad:3000",
            ".bad:3000",
            "app/x:3000",
            "app 3000",
        ]
        for raw in invalid_cases:
            with self.subTest(invalid=raw):
                res = subprocess.run(
                    ["bash", "-c", '. "$1" && expose_parse_target "$2"', "test", EXPOSE_LIB, raw],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=15,
                )
                self.assertNotEqual(res.returncode, 0, f"{raw!r} was accepted: {res.stdout.decode()!r}")


class TestExposePlanEngine(DevContainerTestCase):

    def _acquire(self, holder="expose-holder", project_env=None):
        self.harness.build_fixture_repo(with_parent_env=MAIN_ENV, project_env=project_env)
        res = self.harness.run("acquire", "--for", holder, "--as", "Agent [exp1]")
        self.assertEqual(res.returncode, 0, res.stderr.decode())
        return self.harness.acquired_slot_name(res)

    def _read_lease(self, name):
        with open(self.harness.lease_path(name), encoding="utf-8") as handle:
            return json.load(handle)

    @staticmethod
    def _ports_in_text(text):
        return {int(m) for m in re.findall(r"\b(\d{1,5})\b", text)}

    def test_expose_plan_prints_ssh_details_unused_block_ports_and_stamps_activity(self):
        since = 1700000000
        self.harness.set_now(since)
        name = self._acquire("expose-holder")
        sid = self.harness.slot_project_of(name)
        lease = self._read_lease(name)
        idx = int(name.rsplit("-", 1)[1])

        self.harness.set_now(since + 45)
        res_local = self.harness.run("expose-plan", name, "3000", "--for", "expose-holder")
        self.assertEqual(res_local.returncode, 0, res_local.stderr.decode())
        out_local = res_local.stdout.decode()

        self.assertIn(str(lease["ssh_port"]), out_local)
        self.assertIn(lease["key"], out_local)
        self.assertIn("127.0.0.1", out_local)
        self.assertIn("3000", out_local)

        unused_block = {slot_port(idx, k) for k in range(3, SLOT_PORT_BLOCK)}
        nums_local = self._ports_in_text(out_local)
        self.assertTrue(nums_local & unused_block, f"no unused block port in:\n{out_local}")
        self.assertNotIn(slot_port(idx, 1), nums_local)
        self.assertNotIn(slot_port(idx, 2), nums_local)

        act_file = os.path.join(self.harness.state_dir, "activity", f"{sid}.run")
        self.assertTrue(os.path.isfile(act_file))
        with open(act_file, encoding="utf-8") as handle:
            self.assertEqual(handle.read().strip(), str(since + 45))

        res_svc = self.harness.run("expose-plan", name, "api:8080")
        self.assertEqual(res_svc.returncode, 0, res_svc.stderr.decode())
        out_svc = res_svc.stdout.decode()
        self.assertIn("api", out_svc)
        self.assertIn("8080", out_svc)
        self.assertTrue(self._ports_in_text(out_svc) & unused_block, out_svc)

    def test_expose_plan_refuses_invalid_slot_lease_holder_or_target(self):
        limits_path = os.path.join(self.harness.state_dir, "limits.json")
        with open(limits_path, "w", encoding="utf-8") as handle:
            json.dump({"machine_max": 20, "idle_limit": 60}, handle)

        since = 1700000000
        self.harness.set_now(since)
        name = self._acquire("real-holder")

        def assert_refused(*args):
            res = self.harness.run("expose-plan", *args)
            err = res.stderr.decode()
            self.assertNotEqual(res.returncode, 0, f"{args}: {res.stdout.decode() + err}")
            self.assertNotIn("Usage:", err, args)
            self.assertTrue(err.strip(), args)

        assert_refused("slot-99", "3000")
        assert_refused(name, "3000", "--for", "wrong-holder")
        assert_refused(name, "3000", "--for")
        for bad_target in ("0", "65536", ":3000", "app:", "abc", "-bad:3000"):
            assert_refused(name, bad_target)

        other = self.harness.__class__()
        self.addCleanup(other.cleanup)
        shutil.rmtree(other.state_dir)
        shutil.rmtree(other.docker_state)
        other.state_dir = self.harness.state_dir
        other.docker_state = self.harness.docker_state
        other.docker_log = self.harness.docker_log
        other.fixture_repo = os.path.join(other.tmp, "other-repo")
        other.build_fixture_repo(
            with_parent_env="GIT_USER_NAME=other-agent\nGIT_USER_EMAIL=other-agent@example.com\n",
            project_env=project_env_text(PROJECT="other"),
        )
        subprocess.run(
            [other.real_git_bin, "-C", other.fixture_repo, "remote", "set-url", "origin", "https://example.invalid/other.git"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        other_res = other.run("expose-plan", name, "3000")
        self.assertNotEqual(other_res.returncode, 0)
        self.assertNotIn("Usage:", other_res.stderr.decode())

        self.harness.set_now(since + 60)
        assert_refused(name, "3000")

        self.harness.set_now(since + 10)
        lease_path = self.harness.lease_path(name)
        with open(lease_path, encoding="utf-8") as handle:
            lease_data = json.load(handle)
        with open(lease_path, "w", encoding="utf-8") as handle:
            json.dump(dict(lease_data, setup="creating", pid=os.getpid()), handle)
        assert_refused(name, "3000")

        os.remove(lease_path)
        assert_refused(name, "3000")

        parked_path = self.harness.parked_path(name)
        os.makedirs(os.path.dirname(parked_path), exist_ok=True)
        with open(parked_path, "w", encoding="utf-8") as handle:
            json.dump({"holder": "real-holder", "project": self.harness.slot_project_of(name), "since": since, "reason": "dirty"}, handle)
        assert_refused(name, "3000")

    def test_expose_plan_excludes_forbidden_ports_and_refuses_exhausted_block(self):
        forbidden = list(OWNER_PORTS.values()) + [str(slot_port(1, 3)), str(slot_port(1, 4))]
        name = self._acquire(
            "forb-holder",
            project_env=project_env_text(SLOT_FORBIDDEN_PORTS=" ".join(forbidden)),
        )
        res = self.harness.run("expose-plan", name, "3000")
        self.assertEqual(res.returncode, 0, res.stderr.decode())
        nums = self._ports_in_text(res.stdout.decode())
        self.assertNotIn(slot_port(1, 3), nums)
        self.assertNotIn(slot_port(1, 4), nums)
        allowed = {slot_port(1, k) for k in range(5, SLOT_PORT_BLOCK)}
        self.assertTrue(nums & allowed, res.stdout.decode())

        all_forbidden = list(OWNER_PORTS.values()) + [str(slot_port(1, k)) for k in range(3, SLOT_PORT_BLOCK)]
        with open(os.path.join(self.harness.fixture_repo, "project.env"), "w", encoding="utf-8") as handle:
            handle.write(project_env_text(SLOT_FORBIDDEN_PORTS=" ".join(all_forbidden)))
        exhausted = self.harness.run("expose-plan", name, "3000")
        self.assertNotEqual(exhausted.returncode, 0, exhausted.stdout.decode())
        self.assertNotIn("Usage:", exhausted.stderr.decode())

        sixteen_names = ["SSH_PORT", "NOVNC_PORT", "VITE_DEV_PORT"] + [f"P{k}" for k in range(3, SLOT_PORT_BLOCK)]
        with open(os.path.join(self.harness.fixture_repo, "project.env"), "w", encoding="utf-8") as handle:
            handle.write(project_env_text(SLOT_PORTS=" ".join(sixteen_names)))
        full_block = self.harness.run("expose-plan", name, "3000")
        self.assertNotEqual(full_block.returncode, 0, full_block.stdout.decode())
        self.assertNotIn("Usage:", full_block.stderr.decode())


class TestExposeThinAndGlobal(DevContainerTestCase):

    def setUp(self):
        super().setUp()
        self.fake_bin = os.path.join(self.harness.tmp, "expose-fake-bin")
        os.makedirs(self.fake_bin, exist_ok=True)
        self.docker_log = os.path.join(self.harness.tmp, "expose-docker.log")
        self.forbidden_log = os.path.join(self.harness.tmp, "expose-forbidden.log")
        self.ssh_cap_dir = os.path.join(self.harness.tmp, "ssh-cap")
        os.makedirs(self.ssh_cap_dir, exist_ok=True)

        for tool in FORBIDDEN_TOOLS:
            self._write_exe(os.path.join(self.fake_bin, tool), FORBIDDEN_STUB)

        docker_wrapper = """#!/bin/sh
{ echo "@@CALL@@"; for a in "$@"; do printf '%s\\n' "$a"; done; } >> "${FAKE_DOCKER_LOG:?}"
case "$1" in
  inspect)
    case "$*" in
      *Running*) echo "true" ;;
      *Status*) echo "running" ;;
      *) echo '[{"State":{"Running":true,"Status":"running"}}]' ;;
    esac
    exit 0 ;;
  ps)
    echo "${FAKE_DOCKER_NAME:-dev-container}"
    exit 0 ;;
  exec)
    shift
    while [ "$#" -gt 0 ]; do
      case "$1" in
        -i|-t|-it|-ti|--interactive|--tty) shift ;;
        -e|--env)
          export "$2"
          shift 2 ;;
        -e=*|--env=*)
          export "${1#*=}"
          shift ;;
        -*) shift ;;
        *)
          shift
          if [ "${1:-}" = "dev-container" ]; then shift; fi
          break ;;
      esac
    done
    PATH="$ORIG_ENGINE_PATH" exec bash "$ENGINE_SCRIPT" "$@"
    ;;
esac
exit 0
"""
        self._write_exe(os.path.join(self.fake_bin, "docker"), docker_wrapper)

        ssh_stub = """#!/bin/sh
idx=0
while [ -e "$SSH_CAPTURE_DIR/$idx.argv" ]; do
  idx=$((idx + 1))
done
printf '%s\\0' "$@" > "$SSH_CAPTURE_DIR/$idx.argv"

if [ -n "${SSH_STUB_FAIL_COUNT:-}" ] && [ "$idx" -lt "$SSH_STUB_FAIL_COUNT" ]; then
  exit 255
fi
if [ "${SSH_STUB_FAIL_ALL:-0}" = "1" ]; then
  exit 255
fi

local_cmd=
prev=
for arg in "$@"; do
  case "$prev:$arg" in
    -o:LocalCommand=*) local_cmd=${arg#LocalCommand=} ;;
  esac
  case "$arg" in
    -oLocalCommand=*) local_cmd=${arg#-oLocalCommand=} ;;
  esac
  prev=$arg
done
if [ -n "$local_cmd" ]; then
  /bin/sh -c "$local_cmd"
fi
exit 0
"""
        self._write_exe(os.path.join(self.fake_bin, "ssh"), ssh_stub)
        self.elsewhere = os.path.join(self.harness.tmp, "not-a-project")
        os.makedirs(self.elsewhere, exist_ok=True)

    def _write_exe(self, path, body):
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(body)
        os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    def _thin_env(self, **extra):
        engine_env = self.harness._env(None)
        env = dict(engine_env)
        env["ORIG_ENGINE_PATH"] = engine_env["PATH"]
        env["ENGINE_SCRIPT"] = self.harness.script
        env["PATH"] = self.fake_bin + os.pathsep + "/usr/bin" + os.pathsep + "/bin"
        env["FAKE_DOCKER_LOG"] = self.docker_log
        env["FAKE_FORBIDDEN_LOG"] = self.forbidden_log
        env["SSH_CAPTURE_DIR"] = self.ssh_cap_dir
        env.update(extra)
        return env

    def _run_thin(self, *args, cwd=None, use_pty=False, script=THIN, **extra):
        stdin = subprocess.DEVNULL
        master = slave = None
        if use_pty:
            master, slave = pty.openpty()
            stdin = slave
        try:
            res = subprocess.run(
                ["/bin/sh", script, *args],
                cwd=cwd or self.harness.fixture_repo,
                env=self._thin_env(**extra),
                stdin=stdin,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=60,
            )
        finally:
            if slave is not None:
                os.close(slave)
            if master is not None:
                os.close(master)
        return res

    def _ssh_calls(self):
        files = sorted(
            (f for f in os.listdir(self.ssh_cap_dir) if f.endswith(".argv")),
            key=lambda fn: int(fn.split(".")[0]),
        )
        out = []
        for fn in files:
            with open(os.path.join(self.ssh_cap_dir, fn), "rb") as handle:
                raw = handle.read()
            parts = raw.split(b"\x00")
            if parts and parts[-1] == b"":
                parts = parts[:-1]
            out.append([p.decode("utf-8", errors="replace") for p in parts])
        return out

    def _clear_ssh_calls(self):
        for fn in os.listdir(self.ssh_cap_dir):
            os.remove(os.path.join(self.ssh_cap_dir, fn))

    def _exec_calls(self):
        if not os.path.isfile(self.docker_log):
            return []
        with open(self.docker_log, encoding="utf-8") as handle:
            blocks = handle.read().split("@@CALL@@\n")
        calls = [b.splitlines() for b in blocks if b.strip()]
        return [c for c in calls if c and c[0] == "exec"]

    def _forbidden_calls(self):
        if not os.path.isfile(self.forbidden_log):
            return []
        with open(self.forbidden_log, encoding="utf-8") as handle:
            return handle.read().splitlines()

    def _assert_ssh_call(self, args, expected_th, expected_tp, expected_key, expected_ssh_port):
        self.assertIn("-N", args)
        self.assertFalse(any("@" in a for a in args), f"@ syntax forbidden in ssh args: {args}")

        opts = []
        fwd = None
        key = None
        ssh_port = None
        user = None
        i = 0
        while i < len(args):
            a = args[i]
            if a == "-o" and i + 1 < len(args):
                opts.append(args[i + 1])
                i += 2
                continue
            if a.startswith("-o") and len(a) > 2:
                opts.append(a[2:])
            elif a == "-L" and i + 1 < len(args):
                fwd = args[i + 1]
                i += 2
                continue
            elif a.startswith("-L") and len(a) > 2:
                fwd = a[2:]
            elif a == "-i" and i + 1 < len(args):
                key = args[i + 1]
                i += 2
                continue
            elif a == "-p" and i + 1 < len(args):
                ssh_port = args[i + 1]
                i += 2
                continue
            elif a == "-l" and i + 1 < len(args):
                user = args[i + 1]
                i += 2
                continue
            i += 1

        for req_opt in (
            "ExitOnForwardFailure=yes",
            "StrictHostKeyChecking=no",
            "UserKnownHostsFile=/dev/null",
            "ServerAliveInterval=15",
            "PermitLocalCommand=yes",
        ):
            self.assertIn(req_opt, opts, opts)

        local_cmds = [o for o in opts if o.startswith("LocalCommand=")]
        self.assertEqual(len(local_cmds), 1, opts)
        self.assertIn("EXPOSED http://localhost:", local_cmds[0])

        self.assertEqual(key, expected_key)
        self.assertEqual(ssh_port, str(expected_ssh_port))
        self.assertEqual(user, "node")
        self.assertIn("127.0.0.1", args)

        self.assertIsNotNone(fwd, args)
        prefix = "127.0.0.1:"
        self.assertTrue(fwd.startswith(prefix), fwd)
        rest = fwd[len(prefix):]
        hp_s, th, tp_s = rest.split(":", 2)
        self.assertEqual(th, expected_th)
        self.assertEqual(tp_s, str(expected_tp))
        return int(hp_s)

    def test_thin_expose_port_and_service_port_runs_ssh_with_required_options(self):
        self.harness.build_fixture_repo(with_parent_env=MAIN_ENV)
        acq = self.harness.run("acquire", "--for", "exp-1", "--as", "Agent [e1]")
        self.assertEqual(acq.returncode, 0, acq.stderr.decode())
        name = self.harness.acquired_slot_name(acq)
        with open(self.harness.lease_path(name), encoding="utf-8") as handle:
            lease = json.load(handle)

        res1 = self._run_thin("expose", name, "3000", use_pty=True)
        self.assertEqual(res1.returncode, 0, res1.stderr.decode())
        calls1 = self._ssh_calls()
        self.assertEqual(len(calls1), 1)
        hp1 = self._assert_ssh_call(calls1[0], "127.0.0.1", 3000, lease["key"], lease["ssh_port"])
        unused_block = {slot_port(1, k) for k in range(3, SLOT_PORT_BLOCK)}
        self.assertIn(hp1, unused_block)
        self.assertIn(f"EXPOSED http://localhost:{hp1} -> 127.0.0.1:3000", res1.stdout.decode())
        self.assertEqual(self._forbidden_calls(), [])

        self._clear_ssh_calls()
        res2 = self._run_thin("expose", name, "api:8080", "--for", "exp-1")
        self.assertEqual(res2.returncode, 0, res2.stderr.decode())
        calls2 = self._ssh_calls()
        self.assertEqual(len(calls2), 1)
        hp2 = self._assert_ssh_call(calls2[0], "api", 8080, lease["key"], lease["ssh_port"])
        self.assertIn(hp2, unused_block)
        self.assertIn(f"EXPOSED http://localhost:{hp2} -> api:8080", res2.stdout.decode())
        self.assertEqual(self._forbidden_calls(), [])

    def test_thin_expose_slot_2_uses_slot_2_block_and_excludes_forbidden_ports(self):
        forbidden = list(OWNER_PORTS.values()) + [str(slot_port(2, 3)), str(slot_port(2, 4))]
        self.harness.build_fixture_repo(
            with_parent_env=MAIN_ENV,
            project_env=project_env_text(SLOT_FORBIDDEN_PORTS=" ".join(forbidden)),
        )
        acq1 = self.harness.run("acquire", "--for", "h1", "--as", "Agent [e1]")
        self.assertEqual(acq1.returncode, 0, acq1.stderr.decode())
        acq2 = self.harness.run("acquire", "--for", "h2", "--as", "Agent [e2]")
        self.assertEqual(acq2.returncode, 0, acq2.stderr.decode())
        name2 = self.harness.acquired_slot_name(acq2)
        self.assertEqual(name2, "slot-2")
        with open(self.harness.lease_path(name2), encoding="utf-8") as handle:
            lease2 = json.load(handle)

        res = self._run_thin("expose", name2, "5173")
        self.assertEqual(res.returncode, 0, res.stderr.decode())
        calls = self._ssh_calls()
        self.assertEqual(len(calls), 1)
        hp = self._assert_ssh_call(calls[0], "127.0.0.1", 5173, lease2["key"], lease2["ssh_port"])
        allowed_slot2 = {slot_port(2, k) for k in range(5, SLOT_PORT_BLOCK)}
        self.assertIn(hp, allowed_slot2)
        self.assertNotIn(hp, {slot_port(2, 0), slot_port(2, 1), slot_port(2, 2), slot_port(2, 3), slot_port(2, 4)})
        self.assertNotIn(hp, {slot_port(1, k) for k in range(SLOT_PORT_BLOCK)})

    def test_thin_expose_retries_next_candidate_on_bind_failure_and_fails_when_all_fail(self):
        self.harness.build_fixture_repo(with_parent_env=MAIN_ENV)
        acq = self.harness.run("acquire", "--for", "exp-retry", "--as", "Agent [er]")
        self.assertEqual(acq.returncode, 0, acq.stderr.decode())
        name = self.harness.acquired_slot_name(acq)
        with open(self.harness.lease_path(name), encoding="utf-8") as handle:
            lease = json.load(handle)

        res = self._run_thin("expose", name, "3000", SSH_STUB_FAIL_COUNT="1")
        self.assertEqual(res.returncode, 0, res.stderr.decode())
        calls = self._ssh_calls()
        self.assertEqual(len(calls), 2)
        hp1 = self._assert_ssh_call(calls[0], "127.0.0.1", 3000, lease["key"], lease["ssh_port"])
        hp2 = self._assert_ssh_call(calls[1], "127.0.0.1", 3000, lease["key"], lease["ssh_port"])
        self.assertNotEqual(hp1, hp2)
        unused_block = {slot_port(1, k) for k in range(3, SLOT_PORT_BLOCK)}
        self.assertIn(hp1, unused_block)
        self.assertIn(hp2, unused_block)
        out = res.stdout.decode()
        self.assertIn(f"EXPOSED http://localhost:{hp2} -> 127.0.0.1:3000", out)
        self.assertNotIn(f"EXPOSED http://localhost:{hp1}", out)

        self._clear_ssh_calls()
        fail_all = self._run_thin("expose", name, "3000", SSH_STUB_FAIL_ALL="1")
        self.assertNotEqual(fail_all.returncode, 0)

    def test_thin_expose_refuses_wrong_holder_without_invoking_ssh(self):
        self.harness.build_fixture_repo(with_parent_env=MAIN_ENV)
        acq = self.harness.run("acquire", "--for", "real-holder", "--as", "Agent [ew]")
        self.assertEqual(acq.returncode, 0, acq.stderr.decode())
        name = self.harness.acquired_slot_name(acq)

        res = self._run_thin("expose", name, "3000", "--for", "wrong-holder")
        self.assertNotEqual(res.returncode, 0)
        self.assertNotIn("Usage:", res.stderr.decode())
        self.assertTrue(res.stderr.decode().strip())
        execs = self._exec_calls()
        self.assertEqual(len(execs), 1)
        self.assertIn("expose-plan", execs[0])
        self.assertEqual(self._ssh_calls(), [])

    def test_global_cli_expose_requires_project_and_allows_help_outside(self):
        self.harness.build_fixture_repo(with_parent_env=MAIN_ENV)
        refused = self._run_thin("expose", "slot-1", "3000", cwd=self.elsewhere, script=GLOBAL)
        self.assertNotEqual(refused.returncode, 0)
        err = refused.stderr.decode()
        self.assertIn("project.env", err)
        self.assertIn("dev-container adopt", err)
        self.assertEqual(self._exec_calls(), [])
        self.assertEqual(self._ssh_calls(), [])

        help_res = self._run_thin("expose", "--help", cwd=self.elsewhere, script=GLOBAL)
        self.assertEqual(help_res.returncode, 0, help_res.stderr.decode())
        self.assertNotIn("project.env", help_res.stderr.decode())

        acq = self.harness.run("acquire", "--for", "glob-exp", "--as", "Agent [ge]")
        self.assertEqual(acq.returncode, 0, acq.stderr.decode())
        name = self.harness.acquired_slot_name(acq)
        nested = os.path.join(self.harness.fixture_repo, "sub")
        os.makedirs(nested, exist_ok=True)
        ok = self._run_thin("expose", name, "3000", cwd=nested, script=GLOBAL)
        self.assertEqual(ok.returncode, 0, ok.stderr.decode())
        self.assertEqual(len(self._ssh_calls()), 1)


if __name__ == "__main__":
    unittest.main()
