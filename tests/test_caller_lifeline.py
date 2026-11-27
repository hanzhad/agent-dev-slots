import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import unittest

if os.path.dirname(__file__) not in sys.path:
    sys.path.insert(0, os.path.dirname(__file__))

from harness import (
    DOCKER_STUB,
    DevContainerTestCase,
    LIB_REL,
    REPO_ROOT,
    project_env_text,
    wait_until,
)

CALLER_SH = os.path.join(REPO_ROOT, LIB_REL, "caller.sh")
IDENTITY_ENV = "GIT_USER_NAME=demo-agent\nGIT_USER_EMAIL=demo-agent@example.com\n"


def _read_text(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _pid_is_gone(pid):
    stat_path = f"/proc/{pid}/stat"
    if not os.path.exists(stat_path):
        return True
    try:
        with open(stat_path, encoding="utf-8") as handle:
            raw = handle.read()
    except OSError:
        return True
    idx = raw.rfind(") ")
    if idx != -1 and raw[idx + 2 :].startswith("Z"):
        return True
    return False


class PureCallerHelpers(unittest.TestCase):
    """Pure Core helpers in lib/caller.sh run in-memory without Docker."""

    def _require_caller_sh(self):
        self.assertTrue(os.path.isfile(CALLER_SH), f"missing {CALLER_SH}")

    def test_caller_lifeline_requested_matches_only_exact_stdin(self):
        self._require_caller_sh()
        cases = [
            ("stdin", 0),
            ("", 1),
            ("0", 1),
            ("1", 1),
            ("true", 1),
            ("STDIN", 1),
            ("stdin ", 1),
            (" stdin", 1),
            ("pipe", 1),
            ("tty", 1),
        ]
        for val, expected in cases:
            with self.subTest(val=val):
                res = subprocess.run(
                    [
                        "bash",
                        "-c",
                        'set -euo pipefail; source "$1"; if caller_lifeline_requested "$2"; then exit 0; else exit 1; fi',
                        "bash",
                        CALLER_SH,
                        val,
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=15,
                )
                self.assertEqual(res.returncode, expected, f"val={val!r}: {res.stderr.decode()}")

    def test_caller_stale_launcher_warning_matrix(self):
        self._require_caller_sh()
        repo = "/projects/demo"
        cases = [
            (f"{repo}/bin/dev-container", "", "1", repo, True),
            ("bin/dev-container", "", "1", repo, True),
            ("./bin/dev-container", "", "1", repo, True),
            ("/usr/local/bin/dev-container", "", "1", repo, False),
            ("/opt/dev-container/bin/dev-container", "", "1", repo, False),
            ("/home/user/.local/bin/dev-container", "", "1", repo, False),
            ("dev-container", "", "1", repo, False),
            ("/opt/engine/scripts/dev-container-thin", "", "1", repo, False),
            ("/projects/other/bin/dev-container", "", "1", repo, False),
            ("./bin/dev-container", "stdin", "1", repo, False),
            ("./bin/dev-container", "other", "1", repo, False),
            ("./bin/dev-container", "", "0", repo, False),
            ("./bin/dev-container", "", "", repo, False),
        ]
        for caller_path, lifeline_env, in_container, repo_root, should_warn in cases:
            with self.subTest(
                caller_path=caller_path,
                lifeline_env=lifeline_env,
                in_container=in_container,
            ):
                env = dict(os.environ)
                env["HOME"] = "/home/user"
                res = subprocess.run(
                    [
                        "bash",
                        "-c",
                        'set -euo pipefail; source "$1"; caller_stale_launcher_warning "$2" "$3" "$4" "$5"',
                        "bash",
                        CALLER_SH,
                        caller_path,
                        lifeline_env,
                        in_container,
                        repo_root,
                    ],
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=15,
                )
                self.assertEqual(res.returncode, 0, res.stderr.decode())
                lines = [ln for ln in res.stdout.decode().splitlines() if ln.strip()]
                if should_warn:
                    self.assertEqual(len(lines), 1, f"expected one-line warning, got: {lines!r}")
                else:
                    self.assertEqual(lines, [], f"unexpected warning for {caller_path!r}: {lines!r}")

    def test_descendant_pids_of_parses_proc_stat_with_spaces_and_parens_without_procps(self):
        self._require_caller_sh()
        with open(CALLER_SH, encoding="utf-8") as handle:
            code_lines = [
                ln.strip()
                for ln in handle.read().splitlines()
                if not ln.lstrip().startswith("#")
            ]
        offenders = [ln for ln in code_lines if re.search(r"\bps\s+-e", ln)]
        self.assertEqual(offenders, [], f"lib/caller.sh must not use non-BusyBox ps -e: {offenders}")

        tmp = tempfile.mkdtemp(prefix="caller-proc-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        real_sleep = shutil.which("sleep")
        self.assertTrue(real_sleep)

        mid_bin = os.path.join(tmp, "mid ) one")
        leaf_bin = os.path.join(tmp, "leaf (two) )")
        os.symlink(real_sleep, mid_bin)
        os.symlink(real_sleep, leaf_bin)

        no_ps_bin = os.path.join(tmp, "no-ps-bin")
        os.makedirs(no_ps_bin)
        ps_stub = os.path.join(no_ps_bin, "ps")
        with open(ps_stub, "w", encoding="utf-8") as handle:
            handle.write("#!/bin/sh\necho 'ps must not be called when /proc exists' >&2\nexit 99\n")
        os.chmod(ps_stub, 0o755)

        sibling = subprocess.Popen([real_sleep, "60"])
        self.addCleanup(lambda: sibling.poll() is None and sibling.kill() or sibling.wait(timeout=10))

        mid_pid_file = os.path.join(tmp, "mid.pid")
        leaf_pid_file = os.path.join(tmp, "leaf.pid")
        root_holder = subprocess.Popen(
            [
                "bash",
                "-c",
                'bash -c \'"$1" 60 & echo $! > "$3"; exec "$2" 60\' bash "$1" "$2" "$3" & echo $! > "$4"; wait',
                "bash",
                leaf_bin,
                mid_bin,
                leaf_pid_file,
                mid_pid_file,
            ],
            start_new_session=True,
        )

        def cleanup_root_holder():
            if root_holder.poll() is None:
                try:
                    os.killpg(root_holder.pid, signal.SIGKILL)
                except OSError:
                    root_holder.kill()
            for p_file in (leaf_pid_file, mid_pid_file):
                if os.path.isfile(p_file):
                    try:
                        pid = int(_read_text(p_file).strip())
                        os.kill(pid, signal.SIGKILL)
                    except (OSError, ValueError):
                        pass
            root_holder.wait(timeout=10)

        self.addCleanup(cleanup_root_holder)

        wait_until(
            lambda: os.path.isfile(mid_pid_file) and os.path.isfile(leaf_pid_file),
            timeout=5.0,
            interval=0.02,
            message="descendant pid files were not written",
        )
        mid_pid = int(_read_text(mid_pid_file).strip())
        leaf_pid = int(_read_text(leaf_pid_file).strip())

        if os.path.isdir("/proc"):
            wait_until(
                lambda: (
                    os.path.isfile(f"/proc/{mid_pid}/stat")
                    and ") " in _read_text(f"/proc/{mid_pid}/stat")
                    and "mid ) one" in _read_text(f"/proc/{mid_pid}/stat")
                ),
                timeout=5.0,
                interval=0.02,
                message="mid process did not exec weird comm name",
            )

        env = dict(os.environ)
        if os.path.isdir("/proc"):
            env["PATH"] = no_ps_bin + os.pathsep + env.get("PATH", "")

        res = subprocess.run(
            [
                "bash",
                "-c",
                'set -euo pipefail; source "$1"; descendant_pids_of "$2"',
                "bash",
                CALLER_SH,
                str(root_holder.pid),
            ],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=15,
        )
        self.assertEqual(res.returncode, 0, res.stderr.decode())
        found = {int(tok) for tok in res.stdout.decode().split() if tok.strip()}
        self.assertIn(mid_pid, found)
        self.assertIn(leaf_pid, found)
        self.assertNotIn(root_holder.pid, found)
        self.assertNotIn(sibling.pid, found)

        leaf_res = subprocess.run(
            [
                "bash",
                "-c",
                'set -euo pipefail; source "$1"; descendant_pids_of "$2"',
                "bash",
                CALLER_SH,
                str(leaf_pid),
            ],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=15,
        )
        self.assertEqual(leaf_res.returncode, 0, leaf_res.stderr.decode())
        self.assertEqual(leaf_res.stdout.decode().split(), [])

        missing_res = subprocess.run(
            [
                "bash",
                "-c",
                'set -euo pipefail; source "$1"; descendant_pids_of 99999999',
                "bash",
                CALLER_SH,
            ],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=15,
        )
        self.assertEqual(missing_res.returncode, 0, missing_res.stderr.decode())
        self.assertEqual(missing_res.stdout.decode().split(), [])


class StaleLauncherWarningCLI(DevContainerTestCase):
    """Outdated per-project ./bin/dev-container invocations inside /.dockerenv warn on stderr."""

    def setUp(self):
        super().setUp()
        self.harness.build_fixture_repo(
            with_parent_env=IDENTITY_ENV,
            project_env=project_env_text(SLOT_REPOS_FILE=".devcontainer/repos.json"),
        )
        repos_file = os.path.join(self.harness.fixture_repo, ".devcontainer", "repos.json")
        with open(repos_file, "w", encoding="utf-8") as handle:
            json.dump({"version": 1, "repos": []}, handle)
        origin = os.path.join(self.harness.tmp, "warn-origin.git")
        git = lambda *args: subprocess.run(
            [
                self.harness.real_git_bin,
                "-C",
                self.harness.fixture_repo,
                "-c",
                "commit.gpgsign=false",
                "-c",
                "user.email=fixture@example.com",
                "-c",
                "user.name=fixture",
                *args,
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        subprocess.run(
            [self.harness.real_git_bin, "init", "--bare", "-q", origin],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        git("add", ".devcontainer/repos.json")
        git("commit", "-q", "-m", "add empty repos catalog")
        git("remote", "set-url", "origin", origin)
        git("push", "-q", "origin", "master")
        self.dockerenv = os.path.join(self.harness.tmp, "pretend-dockerenv")
        with open(self.dockerenv, "w", encoding="utf-8"):
            pass

    def _run_with_caller(self, caller, *, in_container=True, lifeline=None):
        overrides = {
            "DEV_CONTAINER_CALLER": caller,
            "DEV_CONTAINER_DOCKERENV_PATH": (
                self.dockerenv if in_container else os.path.join(self.harness.tmp, "absent-dockerenv")
            ),
        }
        r_fd = w_fd = None
        if lifeline is not None:
            overrides["DEV_CONTAINER_CALLER_LIFELINE"] = lifeline
            r_fd, w_fd = os.pipe()
        env = self.harness._env(overrides)
        env.pop("DOCS_REPO_URL", None)
        if lifeline is None:
            env.pop("DEV_CONTAINER_CALLER_LIFELINE", None)
        try:
            return subprocess.run(
                ["bash", self.harness.script, "acquire", "--for", "warn-probe"],
                cwd=self.harness.tmp,
                env=env,
                stdin=r_fd if r_fd is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=60,
            )
        finally:
            for fd in (r_fd, w_fd):
                if fd is not None:
                    try:
                        os.close(fd)
                    except OSError:
                        pass

    def test_outdated_project_launcher_inside_dockerenv_warns_without_false_positives(self):
        for stale_caller in (
            "./bin/dev-container",
            "bin/dev-container",
            os.path.join(self.harness.fixture_repo, "bin", "dev-container"),
        ):
            with self.subTest(stale_caller=stale_caller):
                res = self._run_with_caller(stale_caller, in_container=True, lifeline=None)
                self.assertEqual(res.returncode, 0, res.stderr.decode())
                stderr = res.stderr.decode()
                self.assertTrue(
                    stderr.strip(),
                    f"expected stderr warning for stale caller {stale_caller!r}, got empty stderr",
                )

        for clean_caller, in_container, lifeline in (
            ("/usr/local/bin/dev-container", True, None),
            ("/opt/dev-container/bin/dev-container", True, None),
            (os.path.join(self.harness.home, ".local", "bin", "dev-container"), True, None),
            ("./bin/dev-container", True, "stdin"),
            ("./bin/dev-container", False, None),
        ):
            with self.subTest(clean_caller=clean_caller, in_container=in_container, lifeline=lifeline):
                res = self._run_with_caller(clean_caller, in_container=in_container, lifeline=lifeline)
                self.assertEqual(res.returncode, 0, res.stderr.decode())
                self.assertEqual(res.stderr.decode().strip(), "")


class CallerLifelineLifecycle(DevContainerTestCase):
    """Caller disconnect aborts and rolls back uncommitted slots across every stage."""

    def setUp(self):
        super().setUp()
        self.harness.build_fixture_repo(with_parent_env=IDENTITY_ENV)

    def _spawn_with_lifeline(self, *args, overrides=None, log=None):
        r_fd, w_fd = os.pipe()
        merged = {"DEV_CONTAINER_CALLER_LIFELINE": "stdin"}
        if overrides:
            merged.update(overrides)
        env = self.harness._env(merged)
        if log is not None:
            out_fh = open(log, "w", encoding="utf-8")
            stdout_arg = out_fh
            stderr_arg = subprocess.STDOUT
        else:
            out_fh = None
            stdout_arg = subprocess.PIPE
            stderr_arg = subprocess.PIPE
        proc = subprocess.Popen(
            ["bash", self.harness.script, *args],
            cwd=self.harness.tmp,
            env=env,
            stdin=r_fd,
            stdout=stdout_arg,
            stderr=stderr_arg,
            start_new_session=True,
        )
        os.close(r_fd)
        if out_fh is not None:
            out_fh.close()

        closed = {"done": False}

        def close_writer():
            if not closed["done"]:
                closed["done"] = True
                try:
                    os.close(w_fd)
                except OSError:
                    pass

        def cleanup():
            close_writer()
            for name in os.listdir(self.harness.docker_state):
                if name.endswith(".hold-up") or name.startswith("hold-"):
                    try:
                        os.remove(os.path.join(self.harness.docker_state, name))
                    except OSError:
                        pass
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                if proc.poll() is None:
                    proc.kill()
            proc.communicate(timeout=20)

        self.addCleanup(cleanup)
        return proc, close_writer

    def _write_limits(self, payload):
        path = os.path.join(self.harness.state_dir, "limits.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)

    def _scratch_files(self):
        scratch = os.path.join(self.harness.state_dir, "scratch")
        if not os.path.isdir(scratch):
            return []
        return sorted(n for n in os.listdir(scratch) if n not in ("spec", "facts"))

    def test_early_refusal_exits_2_3_4_and_1_close_stdout_stderr_without_hanging_live_caller(self):
        self._write_limits({"machine_max": 0, "default": 1})

        cases = [
            (("acquire",), 1),
            (("acquire", "--for", "h-cap", "--as", "Caller [c1]"), 2),
            (("acquire", "--for", "h-to", "--as", "Caller [c1]", "--wait", "--timeout", "1s"), 3),
            (("acquire", "--for", "h-noref"), 4),
        ]
        for args, expected_rc in cases:
            with self.subTest(args=args, expected_rc=expected_rc):
                proc, _close_writer = self._spawn_with_lifeline(*args)
                # Keep the caller pipe open: stdout/stderr must still hit EOF without hanging.
                captured = {}

                def drain():
                    out, err = proc.communicate()
                    captured["out"] = out
                    captured["err"] = err

                reader = threading.Thread(target=drain, daemon=True)
                reader.start()
                reader.join(timeout=4.0)
                self.assertFalse(
                    reader.is_alive(),
                    f"lifeline watcher leaked stdout/stderr fd and hung caller on {args}",
                )
                self.assertEqual(proc.returncode, expected_rc, captured.get("err", b"").decode())
                self.assertFalse(os.path.exists(os.path.join(self.harness.state_dir, "lock")))
                self.assertEqual(self._scratch_files(), [])

        # Contrast with an already-disconnected caller on an available pool: must abort 143, not lease.
        self._write_limits({"machine_max": 1, "default": 1})
        dead_proc, close_dead = self._spawn_with_lifeline(
            "acquire", "--for", "dead-at-start", "--as", "Caller [c1]"
        )
        close_dead()
        dead_proc.communicate(timeout=30)
        self.assertEqual(dead_proc.returncode, 143)
        self.assertFalse(os.path.exists(self.harness.lease_path("slot-1")))
        self.assertEqual(self._scratch_files(), [])

    def test_disconnect_and_signals_while_queued_on_wait_abort_143_without_leaking_lock(self):
        self._write_limits({"machine_max": 1})
        first = self.harness.run("acquire", "--for", "holder-1", "--as", "Caller [c1]")
        self.assertEqual(first.returncode, 0, first.stderr.decode())

        for mode in ("eof", signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            with self.subTest(mode=mode):
                log = os.path.join(self.harness.tmp, f"wait-{mode}.log")
                proc, close_writer = self._spawn_with_lifeline(
                    "acquire",
                    "--for",
                    "waiter",
                    "--as",
                    "Caller [c1]",
                    "--wait",
                    overrides={"DEV_CONTAINER_WAIT_INTERVAL": "10"},
                    log=log,
                )
                wait_until(
                    lambda: os.path.isfile(log) and "1/1" in _read_text(log),
                    timeout=5.0,
                    interval=0.02,
                    message="waiter never entered queue sleep",
                )
                if mode == "eof":
                    close_writer()
                else:
                    proc.send_signal(mode)

                wait_until(
                    lambda: proc.poll() is not None,
                    timeout=2.5,
                    interval=0.02,
                    message=f"queued acquire did not abort promptly on {mode}",
                )
                self.assertEqual(proc.returncode, 143)
                self.assertFalse(os.path.exists(os.path.join(self.harness.state_dir, "lock")))
                self.assertTrue(os.path.isfile(self.harness.lease_path("slot-1")))
                self.assertFalse(os.path.exists(self.harness.lease_path("slot-2")))
                self.assertEqual(self._scratch_files(), [])

    def test_disconnect_while_waiting_in_lock_acquire_aborts_143_and_preserves_foreign_lock(self):
        lock_path = os.path.join(self.harness.state_dir, "lock")
        with open(lock_path, "w", encoding="utf-8"):
            pass
        self.addCleanup(lambda: os.path.exists(lock_path) and os.remove(lock_path))

        sleep_marker = os.path.join(self.harness.tmp, "lock-sleeping")
        real_sleep = shutil.which("sleep")
        self.harness._write_stub(
            "sleep",
            f'#!/usr/bin/env bash\n: > "{sleep_marker}"\nexec "{real_sleep}" "$@"\n',
        )

        proc, close_writer = self._spawn_with_lifeline(
            "acquire",
            "--for",
            "lock-waiter",
            "--wait",
            overrides={"DEV_CONTAINER_WAIT_INTERVAL": "10"},
        )
        wait_until(
            lambda: os.path.isfile(sleep_marker),
            timeout=5.0,
            interval=0.02,
            message="lock_acquire never entered caller_sleep",
        )
        close_writer()

        wait_until(
            lambda: proc.poll() is not None,
            timeout=2.5,
            interval=0.02,
            message="lock_acquire did not abort promptly on caller disconnect",
        )
        self.assertEqual(proc.returncode, 143)
        self.assertTrue(
            os.path.isfile(lock_path),
            "aborting waiter deleted a lock held by another process",
        )

    def test_disconnect_during_ensure_toolbox_image_kills_build_child_and_exits_143(self):
        tb_dir = os.path.join(self.harness.fixture_repo, "image", "toolbox")
        os.makedirs(tb_dir, exist_ok=True)
        with open(os.path.join(tb_dir, "Dockerfile"), "w", encoding="utf-8") as handle:
            handle.write("FROM scratch\n")

        held_file = os.path.join(self.harness.docker_state, "tb-build-held")
        pid_file = os.path.join(self.harness.docker_state, "tb-build.pid")
        self.harness._write_stub("docker-real", DOCKER_STUB)
        self.harness._write_stub(
            "docker",
            "#!/usr/bin/env bash\n"
            'if [[ "${1:-}" == image && "${2:-}" == inspect && "$*" == *toolbox* ]]; then exit 1; fi\n'
            'if [[ "${1:-}" == build && "$*" == *toolbox* ]]; then\n'
            f'  sleep 60 & echo $! > "{pid_file}"\n'
            f'  : > "{held_file}"\n'
            "  wait\n"
            "  exit 0\n"
            "fi\n"
            'exec "$(dirname "$0")/docker-real" "$@"\n',
        )

        proc, close_writer = self._spawn_with_lifeline("acquire", "--for", "tb-caller", "--toolbox")
        wait_until(
            lambda: os.path.isfile(held_file) and os.path.isfile(pid_file),
            timeout=5.0,
            interval=0.02,
            message="ensure_toolbox_image never started docker build",
        )
        build_sleep_pid = int(_read_text(pid_file).strip())
        self.addCleanup(
            lambda: not _pid_is_gone(build_sleep_pid) and os.kill(build_sleep_pid, signal.SIGKILL)
        )

        close_writer()
        wait_until(
            lambda: proc.poll() is not None,
            timeout=3.0,
            interval=0.02,
            message="acquire --toolbox did not abort during ensure_toolbox_image",
        )
        self.assertEqual(proc.returncode, 143)
        wait_until(
            lambda: _pid_is_gone(build_sleep_pid),
            timeout=2.0,
            interval=0.02,
            message="toolbox build descendant survived caller disconnect",
        )
        self.assertFalse(os.path.exists(self.harness.lease_path("slot-1")))
        self.assertFalse(os.path.exists(os.path.join(self.harness.state_dir, "lock")))

    def test_disconnect_mid_create_slot_kills_descendants_rolls_back_all_state_and_allows_immediate_slot_1_reacquire(self):
        self._write_limits({"default": 1})

        hold_install = os.path.join(self.harness.docker_state, "hold-install")
        with open(hold_install, "w", encoding="utf-8"):
            pass
        ready_file = os.path.join(self.harness.docker_state, "install-ready")
        mid_pid_file = os.path.join(self.harness.docker_state, "install-mid.pid")
        leaf_pid_file = os.path.join(self.harness.docker_state, "install-leaf.pid")
        stdin_file = os.path.join(self.harness.docker_state, "install-stdin.txt")
        rm_log = os.path.join(self.harness.docker_state, "docker-rm.log")

        installer = os.path.join(self.harness.fixture_repo, ".devcontainer", "install.sh")
        with open(installer, "w", encoding="utf-8") as handle:
            handle.write(
                "#!/usr/bin/env bash\n"
                "set -euo pipefail\n"
                f'if [[ -f "{hold_install}" ]]; then\n'
                f'  readlink /proc/$$/fd/0 > "{stdin_file}" 2>/dev/null || true\n'
                f'  bash -c \'sleep 60 & echo $! > "{leaf_pid_file}"; wait\' & echo $! > "{mid_pid_file}"\n'
                f'  : > "{ready_file}"\n'
                "  wait\n"
                "fi\n"
            )
        os.chmod(installer, 0o755)

        self.harness._write_stub("docker-real", DOCKER_STUB)
        self.harness._write_stub(
            "docker",
            "#!/usr/bin/env bash\n"
            'if [[ "${1:-}" == ps && "$*" == *-q* && "$*" == *volume=* ]]; then\n'
            "  echo orphan-vol-cid\n"
            "  exit 0\n"
            "fi\n"
            'if [[ "${1:-}" == rm ]]; then\n'
            f'  printf "%s\\n" "$*" >> "{rm_log}"\n'
            "  exit 0\n"
            "fi\n"
            'exec "$(dirname "$0")/docker-real" "$@"\n',
        )

        proc, close_writer = self._spawn_with_lifeline(
            "acquire",
            "--for",
            "first-caller",
            "--as",
            "Caller [s1]",
            "--allow-host",
            "api.example.com",
            "--toolbox",
        )
        wait_until(
            lambda: (
                os.path.isfile(ready_file)
                and os.path.isfile(mid_pid_file)
                and os.path.isfile(leaf_pid_file)
            ),
            timeout=15.0,
            interval=0.02,
            message="create_slot never reached install.sh",
        )
        mid_pid = int(_read_text(mid_pid_file).strip())
        leaf_pid = int(_read_text(leaf_pid_file).strip())
        for pid in (mid_pid, leaf_pid):
            self.addCleanup(lambda p=pid: not _pid_is_gone(p) and os.kill(p, signal.SIGKILL))

        if os.path.isfile(stdin_file):
            stdin_target = _read_text(stdin_file).strip()
            if stdin_target:
                self.assertEqual(stdin_target, "/dev/null", "create_slot child inherited lifeline fd 0")

        sid = self.harness.slot_project_of("slot-1")
        self.assertEqual(sid, "0-slot-1-demo")
        self.assertTrue(os.path.isfile(self.harness.lease_path("slot-1")))
        self.assertTrue(self.harness.volume_exists("slot-1"))
        self.assertTrue(os.path.isfile(self.harness.ports_path("slot-1")))
        self.assertTrue(os.path.isfile(self.harness.env_path("slot-1")))

        close_writer()
        wait_until(
            lambda: proc.poll() is not None,
            timeout=4.0,
            interval=0.02,
            message="acquire did not abort promptly mid-create_slot",
        )
        self.assertEqual(proc.returncode, 143)

        for pid in (mid_pid, leaf_pid):
            wait_until(
                lambda p=pid: _pid_is_gone(p),
                timeout=2.0,
                interval=0.02,
                message=f"create_slot descendant {pid} survived abort",
            )

        self.assertTrue(os.path.isfile(rm_log), "abort_acquire_slot did not remove volume-holding container")
        with open(rm_log, encoding="utf-8") as handle:
            self.assertIn("orphan-vol-cid", handle.read())

        with open(self.harness.docker_log, encoding="utf-8") as handle:
            docker_lines = handle.read().splitlines()
        down_leases = [ln for ln in docker_lines if ln.startswith(f"DOWN-LEASES {sid}:")]
        self.assertTrue(down_leases, f"compose down did not run during abort:\n{docker_lines}")
        self.assertIn(
            f"{sid}.json",
            down_leases[-1],
            "RESERVED_LEASE was dropped before compose down finished",
        )

        self.assertFalse(self.harness.volume_exists("slot-1"))
        self.assertFalse(os.path.exists(self.harness.ports_path("slot-1")))
        self.assertFalse(os.path.exists(self.harness.env_path("slot-1")))
        self.assertFalse(os.path.exists(self.harness.overlay_path("slot-1")))
        self.assertFalse(os.path.exists(self.harness.lease_token_path("slot-1")))
        self.assertFalse(os.path.exists(self.harness.lease_path("slot-1")))
        self.assertFalse(os.path.exists(os.path.join(self.harness.state_dir, "toolbox", sid)))
        self.assertFalse(os.path.exists(os.path.join(self.harness.state_dir, "allow-hosts", f"{sid}.hosts")))
        self.assertFalse(os.path.exists(os.path.join(self.harness.state_dir, "lock")))
        self.assertEqual(self._scratch_files(), [])

        os.remove(hold_install)
        second_proc, _close_second = self._spawn_with_lifeline(
            "acquire", "--for", "second-caller", "--as", "Caller [s1]"
        )
        second_out, second_err = second_proc.communicate(timeout=60)
        self.assertEqual(second_proc.returncode, 0, second_err.decode())
        self.assertEqual(
            self.harness.acquired_slot_name(
                type("Res", (), {"stdout": second_out})()
            ),
            "slot-1",
        )
        with open(self.harness.lease_path("slot-1"), encoding="utf-8") as handle:
            lease_data = json.load(handle)
        self.assertEqual(lease_data["holder"], "second-caller")
        self.assertTrue(
            lease_data.get("repos"),
            f"ACQUIRE_STATE_REPOS_JSON was lost across caller_run_interruptible subshell: {lease_data}",
        )

    def test_disconnect_in_wait_for_sshd_and_at_commit_barrier_rolls_back_running_stack_keys_and_token(self):
        # Stage A: disconnect while wait_for_sshd is polling/sleeping.
        hold_sshd = os.path.join(self.harness.docker_state, "hold-sshd")
        sshd_probed = os.path.join(self.harness.docker_state, "sshd-probed")
        with open(hold_sshd, "w", encoding="utf-8"):
            pass

        hold_key = os.path.join(self.harness.docker_state, "hold-key")
        key_reached = os.path.join(self.harness.docker_state, "key-reached")
        key_release = os.path.join(self.harness.docker_state, "key-release")

        self.harness._write_stub("docker-real", DOCKER_STUB)
        self.harness._write_stub(
            "docker",
            "#!/usr/bin/env bash\n"
            f'if [[ "$*" == *"pgrep sshd"* && -f "{hold_sshd}" ]]; then\n'
            f'  : > "{sshd_probed}"\n'
            "  exit 1\n"
            "fi\n"
            f'if [[ "$*" == *"authorized_keys.d/node"* && -f "{hold_key}" ]]; then\n'
            "  cat >/dev/null\n"
            f'  : > "{key_reached}"\n'
            f'  while [[ ! -f "{key_release}" ]]; do sleep 0.02; done\n'
            "  exit 0\n"
            "fi\n"
            'exec "$(dirname "$0")/docker-real" "$@"\n',
        )

        proc_a, close_a = self._spawn_with_lifeline("acquire", "--for", "sshd-caller")
        wait_until(
            lambda: os.path.isfile(sshd_probed),
            timeout=15.0,
            interval=0.02,
            message="finish_acquire never reached wait_for_sshd",
        )
        sid = self.harness.slot_project_of("slot-1")
        keys_dir = os.path.join(self.harness.state_dir, "keys")
        self.assertTrue(os.path.isfile(self.harness.running_path("slot-1")))
        self.assertTrue([f for f in os.listdir(keys_dir) if f.startswith(f"{sid}-")])

        close_a()
        wait_until(
            lambda: proc_a.poll() is not None,
            timeout=3.0,
            interval=0.02,
            message="wait_for_sshd did not abort promptly on caller disconnect",
        )
        self.assertEqual(proc_a.returncode, 143)
        self.assertFalse(os.path.exists(self.harness.running_path("slot-1")))
        self.assertFalse(self.harness.volume_exists("slot-1"))
        self.assertFalse(os.path.exists(self.harness.lease_path("slot-1")))
        self.assertFalse(os.path.exists(self.harness.ports_path("slot-1")))
        self.assertFalse(os.path.exists(self.harness.env_path("slot-1")))
        self.assertEqual([f for f in os.listdir(keys_dir) if f.startswith(f"{sid}-")], [])

        os.remove(hold_sshd)

        # Stage B: disconnect right before write_lease commit barrier (after key install + token write).
        with open(hold_key, "w", encoding="utf-8"):
            pass
        proc_b, close_b = self._spawn_with_lifeline("acquire", "--for", "barrier-caller")
        wait_until(
            lambda: os.path.isfile(key_reached),
            timeout=15.0,
            interval=0.02,
            message="finish_acquire never reached install_lease_key",
        )
        close_b()
        with open(key_release, "w", encoding="utf-8"):
            pass

        wait_until(
            lambda: proc_b.poll() is not None,
            timeout=4.0,
            interval=0.02,
            message="finish_acquire did not complete or abort at commit barrier",
        )
        self.assertEqual(proc_b.returncode, 143, "commit barrier minted lease to dead caller")
        self.assertFalse(os.path.exists(self.harness.lease_path("slot-1")))
        self.assertFalse(os.path.exists(self.harness.lease_token_path("slot-1")))
        self.assertFalse(os.path.exists(self.harness.running_path("slot-1")))
        self.assertFalse(self.harness.volume_exists("slot-1"))
        self.assertEqual([f for f in os.listdir(keys_dir) if f.startswith(f"{sid}-")], [])

    def test_resume_succeeds_under_set_u_with_lifeline_and_aborts_cleanly_on_disconnect(self):
        acquired = self.harness.run_acquire("resume-seed")
        self.assertEqual(acquired.returncode, 0, acquired.stderr.decode())
        name = self.harness.acquired_slot_name(acquired)
        slot_dir = self.harness.slot_dir(name)

        git = lambda *gargs: subprocess.run(
            [
                self.harness.real_git_bin,
                "-C",
                slot_dir,
                "-c",
                "commit.gpgsign=false",
                "-c",
                "user.email=fixture@example.com",
                "-c",
                "user.name=fixture",
                *gargs,
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        git("checkout", "-b", "feat-lifeline-resume")
        with open(os.path.join(slot_dir, "work.txt"), "w", encoding="utf-8") as handle:
            handle.write("committed resume payload\n")
        git("add", "work.txt")
        git("commit", "-m", "resume work")
        with open(os.path.join(slot_dir, "extra.txt"), "w", encoding="utf-8") as handle:
            handle.write("untracked payload\n")

        lease_path = self.harness.lease_path(name)
        with open(lease_path, encoding="utf-8") as handle:
            lease_obj = json.load(handle)
        lease_obj["since"] = 1
        with open(lease_path, "w", encoding="utf-8") as handle:
            json.dump(lease_obj, handle)

        cleaned = self.harness.run("clean")
        self.assertEqual(cleaned.returncode, 0, cleaned.stderr.decode())

        res_proc, _close_res = self._spawn_with_lifeline("resume", "feat-lifeline-resume")
        res_out, res_err = res_proc.communicate(timeout=60)
        self.assertEqual(res_proc.returncode, 0, res_err.decode())
        self.assertNotIn("unbound variable", res_err.decode())
        restored_name = self.harness.acquired_slot_name(
            type("Res", (), {"stdout": res_out})()
        )
        restored_dir = self.harness.slot_dir(restored_name)
        with open(os.path.join(restored_dir, "work.txt"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "committed resume payload\n")
        with open(os.path.join(restored_dir, "extra.txt"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "untracked payload\n")

        # Disconnecting mid-resume while allocating from a remote branch aborts 143 and rolls back.
        remote_git = lambda *rargs: subprocess.run(
            [
                self.harness.real_git_bin,
                "-C",
                self.harness.fixture_repo,
                "-c",
                "commit.gpgsign=false",
                "-c",
                "user.email=fixture@example.com",
                "-c",
                "user.name=fixture",
                *rargs,
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        remote_git("branch", "remote-lifeline-branch")
        self.harness.hold_compose_up("slot-2")
        abort_res, close_abort_res = self._spawn_with_lifeline("resume", "remote-lifeline-branch")
        self.harness.wait_until_compose_up_held("slot-2")
        close_abort_res()
        wait_until(
            lambda: abort_res.poll() is not None,
            timeout=3.0,
            interval=0.02,
            message="resume did not abort promptly on caller disconnect",
        )
        self.assertEqual(abort_res.returncode, 143)
        self.assertFalse(os.path.exists(self.harness.lease_path("slot-2")))
        self.assertFalse(self.harness.volume_exists("slot-2"))

    def test_normal_create_slot_failures_preserve_pre_up_git_dir_and_clean_half_started_compose_up(self):
        # 1. Pre-compose normal failure (missing GIT_USER_NAME) preserves slot_dir/.git.
        env_file = os.path.join(self.harness.fixture_repo, ".devcontainer", ".env")
        os.remove(env_file)
        proc_ident, _close_ident = self._spawn_with_lifeline("acquire", "--for", "no-ident")
        _out1, err1 = proc_ident.communicate(timeout=60)
        self.assertEqual(proc_ident.returncode, 1, err1.decode())
        self.assertIn("GIT_USER_NAME", err1.decode())
        self.assertTrue(os.path.isdir(os.path.join(self.harness.slot_dir("slot-1"), ".git")))
        self.assertFalse(os.path.isfile(self.harness.lease_path("slot-1")))

        # 2. Normal compose up -d failure runs compose down --remove-orphans before die.
        with open(env_file, "w", encoding="utf-8") as handle:
            handle.write(IDENTITY_ENV)
        os.chmod(env_file, 0o600)
        sid2 = self.harness.slot_project_of("slot-2")
        with open(os.path.join(self.harness.docker_state, f"{sid2}.fail-up"), "w", encoding="utf-8"):
            pass

        proc_up, _close_up = self._spawn_with_lifeline("acquire", "--for", "up-fail")
        _out2, err2 = proc_up.communicate(timeout=60)
        self.assertEqual(proc_up.returncode, 1, err2.decode())
        with open(self.harness.docker_log, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
        down_orphans = [
            ln
            for ln in lines
            if f"-p {sid2} " in ln or f"COMPOSE_PROJECT_NAME={sid2} " in ln
            if " down " in ln and "--remove-orphans" in ln
        ]
        self.assertTrue(
            down_orphans,
            f"failed compose up -d did not run compose down --remove-orphans for {sid2}:\n"
            + "\n".join(lines),
        )

        # 3. Usage text clarifies --timeout scope vs client disconnect rollback.
        help_res = self.harness.run("acquire", "--help")
        self.assertEqual(help_res.returncode, 0, help_res.stderr.decode())
        help_text = help_res.stdout.decode().lower()
        self.assertIn("reservation", help_text)
        self.assertTrue("disconnect" in help_text and "roll" in help_text, help_res.stdout.decode())


if __name__ == "__main__":
    unittest.main()
