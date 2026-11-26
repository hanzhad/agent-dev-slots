import json
import os
import pty
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

if os.path.dirname(__file__) not in sys.path:
    sys.path.insert(0, os.path.dirname(__file__))

from engine_stub import FAKE_DOCKER, FORBIDDEN_TOOLS, FORBIDDEN_STUB, option_values
from harness import (
    DevContainerTestCase,
    PROJECT_PREFIX,
    REPO_ROOT,
    project_env_text,
)

MAIN_ENV = (
    "GIT_USER_NAME=demo-agent\n"
    "GIT_USER_EMAIL=demo-agent@example.com\n"
)
THIN = os.path.join(REPO_ROOT, "scripts", "dev-container-thin")
GLOBAL = os.path.join(REPO_ROOT, "scripts", "dev-container-global")

PNG_SAMPLE = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR\x00\x01\xff\xfe\r\n\n\x00"
    + bytes(range(256))
    + b"\x00\x00\x00\x00IEND\xaeB`\x82"
)
LARGE_BIN_SAMPLE = PNG_SAMPLE * 750


class TestSlotGetEngine(DevContainerTestCase):

    def _build(self, extra_files=None, slot_copy_files=None, untracked_files=None, compose_dir=None):
        overrides = {}
        if slot_copy_files is not None:
            overrides["SLOT_COPY_FILES"] = slot_copy_files
        if compose_dir is not None:
            overrides["COMPOSE_DIR"] = compose_dir
        self.harness.build_fixture_repo(
            with_parent_env=MAIN_ENV,
            project_env=project_env_text(**overrides),
        )
        git = self.harness.real_git_bin
        if compose_dir is not None:
            subprocess.run(
                [git, "-C", self.harness.fixture_repo, "mv", ".devcontainer", compose_dir],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            subprocess.run(
                [git, "-C", self.harness.fixture_repo, "commit", "-q", "-m", "move compose dir"],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        if extra_files:
            for rel_path, content in extra_files.items():
                full = os.path.join(self.harness.fixture_repo, rel_path)
                os.makedirs(os.path.dirname(full), exist_ok=True)
                mode = "wb" if isinstance(content, bytes) else "w"
                kwargs = {} if isinstance(content, bytes) else {"encoding": "utf-8"}
                with open(full, mode, **kwargs) as handle:
                    handle.write(content)
            subprocess.run(
                [git, "-C", self.harness.fixture_repo, "add", "-f", "--", *extra_files.keys()],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            subprocess.run(
                [git, "-C", self.harness.fixture_repo, "commit", "-q", "-m", "seed get files"],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        if untracked_files:
            for rel_path, content in untracked_files.items():
                full = os.path.join(self.harness.fixture_repo, rel_path)
                os.makedirs(os.path.dirname(full), exist_ok=True)
                mode = "wb" if isinstance(content, bytes) else "w"
                kwargs = {} if isinstance(content, bytes) else {"encoding": "utf-8"}
                with open(full, mode, **kwargs) as handle:
                    handle.write(content)

    def _acquire(self, holder="get-holder"):
        res = self.harness.run("acquire", "--for", holder, "--as", "Agent [get1]")
        self.assertEqual(res.returncode, 0, res.stderr.decode())
        return self.harness.acquired_slot_name(res)

    def _assert_refused(self, res, context=""):
        stdout = res.stdout
        stderr = res.stderr.decode("utf-8", errors="replace")
        msg = f"rc={res.returncode} stdout={stdout!r} stderr={stderr!r} {context}"
        self.assertNotEqual(res.returncode, 0, msg)
        self.assertEqual(stdout, b"", msg)
        self.assertTrue(stderr.strip(), msg)
        self.assertNotIn("Usage:", stderr, msg)

    def test_get_stdout_streams_binary_png_large_binary_and_env_example_on_running_and_stopped_volume_slot(self):
        self._build(extra_files={".env.example": "PORT=3000\n"})
        name = self._acquire()
        slot_dir = self.harness.slot_dir(name)

        os.makedirs(os.path.join(slot_dir, "artifacts"), exist_ok=True)
        with open(os.path.join(slot_dir, "artifacts", "shot.png"), "wb") as handle:
            handle.write(PNG_SAMPLE)
        with open(os.path.join(slot_dir, "artifacts", "large.bin"), "wb") as handle:
            handle.write(LARGE_BIN_SAMPLE)

        res_png = self.harness.run("get", name, "--stdout", "artifacts/shot.png")
        self.assertEqual(res_png.returncode, 0, res_png.stderr.decode())
        self.assertEqual(res_png.stdout, PNG_SAMPLE)

        res_big = self.harness.run("get", name, "--stdout", "artifacts/large.bin")
        self.assertEqual(res_big.returncode, 0, res_big.stderr.decode())
        self.assertGreater(len(LARGE_BIN_SAMPLE), 131072)
        self.assertEqual(res_big.stdout, LARGE_BIN_SAMPLE)

        res_ex = self.harness.run("get", name, "--stdout", ".env.example")
        self.assertEqual(res_ex.returncode, 0, res_ex.stderr.decode())
        self.assertEqual(res_ex.stdout, b"PORT=3000\n")

        self.harness.mark_stopped(name)
        if os.path.exists(self.harness.docker_log):
            os.remove(self.harness.docker_log)

        res_stopped = self.harness.run("get", name, "--stdout", "artifacts/shot.png")
        self.assertEqual(res_stopped.returncode, 0, res_stopped.stderr.decode())
        self.assertEqual(res_stopped.stdout, PNG_SAMPLE)
        with open(self.harness.docker_log, encoding="utf-8") as handle:
            dlog = handle.read()
        self.assertIn("ARGS: run ", dlog)
        self.assertIn("--network none", dlog)

    def test_get_stdout_works_on_legacy_dir_slot_and_enforces_o_nofollow(self):
        self._build()
        self.harness.make_bare_slot("dir-slot")
        slot_dir = self.harness.slot_dir("dir-slot")
        lease_file = os.path.join(self.harness.state_dir, "leases", f"{PROJECT_PREFIX}-dir-slot.json")
        os.makedirs(os.path.dirname(lease_file), exist_ok=True)
        with open(lease_file, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "holder": "dir-holder",
                    "owner": "-",
                    "project": f"{PROJECT_PREFIX}-dir-slot",
                    "since": self.harness.now_epoch(),
                    "key": "/dev/null",
                    "ssh_port": 39100,
                    "repo_root": os.path.realpath(self.harness.fixture_repo),
                },
                handle,
            )

        os.makedirs(os.path.join(slot_dir, "artifacts"), exist_ok=True)
        with open(os.path.join(slot_dir, "artifacts", "shot.png"), "wb") as handle:
            handle.write(PNG_SAMPLE)
        outside = os.path.join(self.harness.tmp, "outside_legacy.txt")
        with open(outside, "w", encoding="utf-8") as handle:
            handle.write("LEGACY_OUTSIDE_SECRET\n")
        os.symlink(outside, os.path.join(slot_dir, "evil_link"))

        ok = self.harness.run("get", "dir-slot", "--stdout", "artifacts/shot.png")
        self.assertEqual(ok.returncode, 0, ok.stderr.decode())
        self.assertEqual(ok.stdout, PNG_SAMPLE)

        bad = self.harness.run("get", "dir-slot", "--stdout", "evil_link")
        self._assert_refused(bad, "legacy dir symlink")
        self.assertNotIn("LEGACY_OUTSIDE_SECRET", bad.stderr.decode())

    def test_get_stdout_enforces_max_bytes_ceiling(self):
        self._build()
        name = self._acquire()
        slot_dir = self.harness.slot_dir(name)
        exact = (b"\x00\x01\xff\n") * 16
        self.assertEqual(len(exact), 64)
        with open(os.path.join(slot_dir, "exact.bin"), "wb") as handle:
            handle.write(exact)

        ok = self.harness.run("get", name, "--stdout", "exact.bin", "--max-bytes", "64")
        self.assertEqual(ok.returncode, 0, ok.stderr.decode())
        self.assertEqual(ok.stdout, exact)

        over = self.harness.run("get", name, "--stdout", "exact.bin", "--max-bytes", "63")
        self._assert_refused(over, "max-bytes 63 on 64B file")

        for bad_max in ("0", "-1", "abc"):
            res = self.harness.run("get", name, "--stdout", "exact.bin", "--max-bytes", bad_max)
            self._assert_refused(res, f"invalid max-bytes={bad_max}")

    def test_get_refuses_symlinks_traversal_non_regular_missing_and_protected_paths(self):
        self._build(
            extra_files={"src/ok.bin": b"ok\x00\n"},
            slot_copy_files="config/master.key",
            untracked_files={"config/master.key": "MASTER_KEY_SECRET\n"},
        )
        name = self._acquire()
        slot_dir = self.harness.slot_dir(name)

        for rel, content in (
            (".env", "DOT_ENV_SECRET=1\n"),
            (".env.local", "DOT_ENV_LOCAL_SECRET=1\n"),
            ("src/.env.production", "PROD_ENV_SECRET=1\n"),
            (".devcontainer/agent.env", "AGENT_ENV_SECRET=1\n"),
        ):
            full = os.path.join(slot_dir, rel)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w", encoding="utf-8") as handle:
                handle.write(content)

        outside_file = os.path.join(self.harness.tmp, "outside_secret.txt")
        with open(outside_file, "w", encoding="utf-8") as handle:
            handle.write("OUTSIDE_SECRET\n")
        outside_dir = os.path.join(self.harness.tmp, "outside_dir")
        os.makedirs(outside_dir, exist_ok=True)
        with open(os.path.join(outside_dir, "inner.txt"), "w", encoding="utf-8") as handle:
            handle.write("OUTSIDE_DIR_SECRET\n")

        os.symlink("/etc/hosts", os.path.join(slot_dir, "hosts_link"))
        os.symlink(outside_file, os.path.join(slot_dir, "evil_link"))
        os.symlink(outside_dir, os.path.join(slot_dir, "sub_link"))
        os.symlink(".git/config", os.path.join(slot_dir, "git_alias"))

        bad_paths = (
            "../outside_secret.txt",
            "src/../../outside_secret.txt",
            "/etc/hosts",
            "/workspace/src/ok.bin",
            ".git/config",
            ".git/HEAD",
            ".devcontainer/agent.env",
            ".devcontainer/docker-compose.yml",
            "project.env",
            ".env",
            ".env.local",
            "src/.env.production",
            "config/master.key",
            "hosts_link",
            "evil_link",
            "sub_link/inner.txt",
            "git_alias",
            "src",
            "missing.png",
            "src\\ok.bin",
        )
        for bad in bad_paths:
            with self.subTest(path=bad):
                res = self.harness.run("get", name, "--stdout", bad)
                self._assert_refused(res, bad)
                combined = res.stdout.decode("utf-8", errors="replace") + res.stderr.decode("utf-8", errors="replace")
                for leak in (
                    "MASTER_KEY_SECRET",
                    "DOT_ENV_SECRET",
                    "DOT_ENV_LOCAL_SECRET",
                    "PROD_ENV_SECRET",
                    "AGENT_ENV_SECRET",
                    "OUTSIDE_SECRET",
                    "OUTSIDE_DIR_SECRET",
                ):
                    self.assertNotIn(leak, combined, bad)

    def test_get_respects_custom_compose_dir(self):
        self._build(extra_files={"src/ok.bin": b"ok\x00\n"}, compose_dir="stack")
        name = self._acquire()
        slot_dir = self.harness.slot_dir(name)
        with open(os.path.join(slot_dir, "stack", "secret.env"), "w", encoding="utf-8") as handle:
            handle.write("STACK_SECRET=1\n")

        ok = self.harness.run("get", name, "--stdout", "src/ok.bin")
        self.assertEqual(ok.returncode, 0, ok.stderr.decode())
        self.assertEqual(ok.stdout, b"ok\x00\n")

        for rel in ("stack/secret.env", "stack/docker-compose.yml"):
            res = self.harness.run("get", name, "--stdout", rel)
            self._assert_refused(res, rel)
            self.assertNotIn("STACK_SECRET", res.stderr.decode())

    def test_get_validates_ownership_live_lease_and_stamps_activity(self):
        self._build(extra_files={"ok.bin": b"payload\x00\n"})
        limits_path = os.path.join(self.harness.state_dir, "limits.json")
        with open(limits_path, "w", encoding="utf-8") as handle:
            json.dump({"machine_max": 20, "idle_limit": 60}, handle)

        since = 1700000000
        self.harness.set_now(since)
        name = self._acquire("get-owner")
        sid = self.harness.slot_project_of(name)

        self._assert_refused(self.harness.run("get", "slot-99", "--stdout", "ok.bin"), "missing slot")

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
        self._assert_refused(other.run("get", name, "--stdout", "ok.bin"), "foreign project")

        self.harness.set_now(since + 50)
        stamped = self.harness.run("get", name, "--stdout", "ok.bin")
        self.assertEqual(stamped.returncode, 0, stamped.stderr.decode())
        self.assertEqual(stamped.stdout, b"payload\x00\n")
        act_file = os.path.join(self.harness.state_dir, "activity", f"{sid}.run")
        self.assertTrue(os.path.isfile(act_file))
        with open(act_file, encoding="utf-8") as handle:
            self.assertEqual(handle.read().strip(), str(since + 50))

        self.harness.set_now(since + 70)
        still_live = self.harness.run("get", name, "--stdout", "ok.bin")
        self.assertEqual(still_live.returncode, 0, still_live.stderr.decode())

        self.harness.set_now(since + 130)
        self._assert_refused(self.harness.run("get", name, "--stdout", "ok.bin"), "expired lease")

        self.harness.set_now(since + 10)
        lease_path = self.harness.lease_path(name)
        with open(lease_path, encoding="utf-8") as handle:
            lease_data = json.load(handle)
        setup_lease = dict(lease_data, setup="creating", pid=os.getpid())
        with open(lease_path, "w", encoding="utf-8") as handle:
            json.dump(setup_lease, handle)
        self._assert_refused(self.harness.run("get", name, "--stdout", "ok.bin"), "setup in flight")

        os.remove(lease_path)
        self._assert_refused(self.harness.run("get", name, "--stdout", "ok.bin"), "unleased")


class TestSlotGetThinAndGlobal(DevContainerTestCase):

    def setUp(self):
        super().setUp()
        self.harness.build_fixture_repo(with_parent_env=MAIN_ENV)
        self.fake_bin = os.path.join(self.harness.tmp, "thin-fake-bin")
        os.makedirs(self.fake_bin, exist_ok=True)
        self.docker_log = os.path.join(self.harness.tmp, "thin-docker.log")
        self.forbidden_log = os.path.join(self.harness.tmp, "thin-forbidden.log")

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

        self.outside_dir = tempfile.mkdtemp(prefix="outside-get-")
        self.addCleanup(shutil.rmtree, self.outside_dir, ignore_errors=True)
        self.elsewhere = os.path.join(self.harness.tmp, "not-a-project")
        os.makedirs(self.elsewhere, exist_ok=True)

    def _write_exe(self, path, body):
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(body)
        os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    def _thin_env(self):
        engine_env = self.harness._env(None)
        env = dict(engine_env)
        env["ORIG_ENGINE_PATH"] = engine_env["PATH"]
        env["ENGINE_SCRIPT"] = self.harness.script
        env["PATH"] = self.fake_bin + os.pathsep + "/usr/bin" + os.pathsep + "/bin"
        env["FAKE_DOCKER_LOG"] = self.docker_log
        env["FAKE_FORBIDDEN_LOG"] = self.forbidden_log
        return env

    def _run_thin(self, *args, cwd=None, use_pty=False, script=THIN):
        stdin = subprocess.DEVNULL
        master = slave = None
        if use_pty:
            master, slave = pty.openpty()
            stdin = slave
        try:
            res = subprocess.run(
                ["/bin/sh", script, *args],
                cwd=cwd or self.harness.fixture_repo,
                env=self._thin_env(),
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

    def test_thin_get_to_host_dst_outside_projects_root_copies_binary_png_atomically(self):
        acq = self.harness.run("acquire", "--for", "thin-get", "--as", "Agent [tg1]")
        self.assertEqual(acq.returncode, 0, acq.stderr.decode())
        name = self.harness.acquired_slot_name(acq)
        slot_dir = self.harness.slot_dir(name)

        os.makedirs(os.path.join(slot_dir, "artifacts"), exist_ok=True)
        with open(os.path.join(slot_dir, "artifacts", "shot.png"), "wb") as handle:
            handle.write(PNG_SAMPLE)

        host_dst = os.path.join(self.outside_dir, "shot.png")
        res = self._run_thin("get", name, "artifacts/shot.png", host_dst, use_pty=True)
        self.assertEqual(res.returncode, 0, res.stderr.decode())
        self.assertTrue(os.path.isfile(host_dst))
        with open(host_dst, "rb") as handle:
            self.assertEqual(handle.read(), PNG_SAMPLE)
        self.assertEqual(os.listdir(self.outside_dir), ["shot.png"])
        self.assertEqual(self._forbidden_calls(), [])
        execs = self._exec_calls()
        self.assertEqual(len(execs), 1)
        self.assertFalse({"-t", "-it", "-ti", "--tty"} & set(execs[0]), execs[0])

    def test_thin_get_refused_path_leaves_no_partial_or_tmp_destination_file(self):
        acq = self.harness.run("acquire", "--for", "thin-get-fail", "--as", "Agent [tg2]")
        self.assertEqual(acq.returncode, 0, acq.stderr.decode())
        name = self.harness.acquired_slot_name(acq)
        slot_dir = self.harness.slot_dir(name)
        with open(os.path.join(slot_dir, ".env"), "w", encoding="utf-8") as handle:
            handle.write("SECRET=1\n")

        missing_dst = os.path.join(self.outside_dir, "blocked.txt")
        res1 = self._run_thin("get", name, ".env", missing_dst)
        self.assertNotEqual(res1.returncode, 0)
        self.assertNotIn("Usage:", res1.stderr.decode())
        self.assertEqual(self._exec_calls()[-1][-5:], ["dev-container", "get", name, "--stdout", ".env"])
        self.assertFalse(os.path.exists(missing_dst))
        self.assertEqual(os.listdir(self.outside_dir), [])

        existing_dst = os.path.join(self.outside_dir, "existing.txt")
        with open(existing_dst, "wb") as handle:
            handle.write(b"keep-me")
        res2 = self._run_thin("get", name, "../escape.txt", existing_dst)
        self.assertNotEqual(res2.returncode, 0)
        self.assertNotIn("Usage:", res2.stderr.decode())
        self.assertEqual(self._exec_calls()[-1][-5:], ["dev-container", "get", name, "--stdout", "../escape.txt"])
        with open(existing_dst, "rb") as handle:
            self.assertEqual(handle.read(), b"keep-me")
        self.assertEqual(os.listdir(self.outside_dir), ["existing.txt"])

    def test_global_cli_get_requires_project_and_allows_help_outside(self):
        refused = self._run_thin("get", "slot-1", "a.png", os.path.join(self.outside_dir, "a.png"), cwd=self.elsewhere, script=GLOBAL)
        self.assertNotEqual(refused.returncode, 0)
        err = refused.stderr.decode()
        self.assertIn("project.env", err)
        self.assertIn("dev-container adopt", err)
        self.assertEqual(self._exec_calls(), [])

        if os.path.exists(self.docker_log):
            os.remove(self.docker_log)
        help_res = self._run_thin("get", "--help", cwd=self.elsewhere, script=GLOBAL)
        self.assertEqual(len(self._exec_calls()), 1)
        self.assertEqual(self._exec_calls()[0][-2:], ["get", "--help"])
        self.assertEqual(help_res.returncode, 0, help_res.stderr.decode())

        if os.path.exists(self.docker_log):
            os.remove(self.docker_log)
        acq = self.harness.run("acquire", "--for", "glob-get", "--as", "Agent [gg1]")
        self.assertEqual(acq.returncode, 0, acq.stderr.decode())
        name = self.harness.acquired_slot_name(acq)
        with open(os.path.join(self.harness.slot_dir(name), "ok.png"), "wb") as handle:
            handle.write(PNG_SAMPLE)
        nested = os.path.join(self.harness.fixture_repo, "sub")
        os.makedirs(nested, exist_ok=True)
        out_png = os.path.join(self.outside_dir, "global.png")
        ok = self._run_thin("get", name, "ok.png", out_png, cwd=nested, script=GLOBAL)
        self.assertEqual(ok.returncode, 0, ok.stderr.decode())
        with open(out_png, "rb") as handle:
            self.assertEqual(handle.read(), PNG_SAMPLE)


if __name__ == "__main__":
    unittest.main()
