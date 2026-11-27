import os
import pty
import shutil
import subprocess
import unittest

from engine_stub import FAKE_DOCKER, FakeDockerCase, THIN, option_values

SESSION_VAR = "CLAUDE_CODE_HOST_SESSION_ID"


class ThinCommand(FakeDockerCase):
    """`scripts/dev-container-thin` runs the engine inside its container."""

    def setUp(self):
        super().setUp()
        self.cwd = os.path.join(self.projects, "some-project", "sub")
        os.makedirs(self.cwd)

    def thin(self, *args, state="running", stdin=subprocess.DEVNULL, **extra):
        env = self.env(FAKE_DOCKER_STATE=state, PWD=self.cwd, **extra)
        return self.sh(THIN, *args, cwd=self.cwd, env=env, stdin=stdin)

    def exec_call(self):
        calls = self.calls_of("exec")
        self.assertEqual(len(calls), 1, self.calls())
        return calls[0]

    def env_of(self, call):
        return option_values(call, "-e", "--env")

    def test_a_running_engine_gets_the_arguments_as_they_were_written(self):
        result = self.thin("acquire", "--for", "x y", "--as", "")
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        call = self.exec_call()
        self.assertEqual(call[-6:], ["dev-container", "acquire", "--for", "x y", "--as", ""])

    def test_the_engines_exit_code_is_the_thins(self):
        self.assertEqual(self.thin("list", FAKE_EXEC_RC="7").returncode, 7)

    def test_the_callers_working_directory_and_name_are_passed(self):
        self.thin("list")
        passed = self.env_of(self.exec_call())
        self.assertIn(f"DEV_CONTAINER_CALLER_PWD={self.cwd}", passed)
        callers = [e for e in passed if e.startswith("DEV_CONTAINER_CALLER=")]
        self.assertEqual(len(callers), 1, passed)
        self.assertTrue(callers[0].endswith("dev-container-thin"), callers)

    def test_the_session_variable_is_passed_when_set_and_not_invented_when_not(self):
        self.thin("list", **{SESSION_VAR: "sess-1"})
        passed = self.env_of(self.exec_call())
        self.assertTrue({SESSION_VAR, f"{SESSION_VAR}=sess-1"} & set(passed), passed)
        os.remove(self.docker_log)
        self.thin("list")
        passed = self.env_of(self.exec_call())
        self.assertFalse([e for e in passed if e.startswith(SESSION_VAR + "=")], passed)

    def test_without_a_terminal_it_keeps_stdin_open_and_allocates_no_tty(self):
        self.thin("list")
        call = self.exec_call()
        self.assertTrue({"-i", "--interactive"} & set(call), call)
        self.assertFalse({"-t", "-it", "-ti", "--tty"} & set(call), call)

    def test_with_a_terminal_it_allocates_a_tty(self):
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        try:
            self.thin("list", stdin=slave)
        finally:
            os.close(slave)
        self.assertTrue({"-t", "-it", "-ti", "--tty"} & set(self.exec_call()))

    def test_a_stopped_engine_is_refused_with_the_command_that_starts_it(self):
        result = self.thin("list", state="stopped")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("docker start dev-container", result.stderr.decode())
        self.assertEqual(self.calls_of("exec"), [])

    def test_a_missing_engine_is_refused_with_the_install_command(self):
        result = self.thin("list", state="missing")
        self.assertNotEqual(result.returncode, 0)
        text = result.stderr.decode()
        self.assertIn("engine install", text)
        self.assertIn("--projects-root", text)
        self.assertEqual(self.calls_of("exec"), [])

    def test_the_container_name_can_be_chosen(self):
        self.thin("list", DEV_CONTAINER_ENGINE_NAME="other-engine")
        self.assertIn("other-engine", self.exec_call())
        refused = self.thin("list", state="stopped", DEV_CONTAINER_ENGINE_NAME="other-engine")
        self.assertIn("docker start other-engine", refused.stderr.decode())

    def test_the_thin_needs_docker_and_sh_only(self):
        self.thin("list")
        self.thin("list", state="stopped")
        self.assertEqual(self.forbidden_calls(), [])
        with open(THIN, encoding="utf-8") as handle:
            text = handle.read()
        self.assertEqual(text.splitlines()[0], "#!/bin/sh")
        for bashism in ("[[ ", "<<<", "function ", "=("):
            self.assertNotIn(bashism, text)
        self.assertTrue(os.access(THIN, os.X_OK), "the thin is not executable")

    def test_thin_put_streams_file_via_stdin(self):
        src = os.path.join(self.cwd, "file.txt")
        with open(src, "w", encoding="utf-8") as f:
            f.write("content\n")
        self.thin("put", "slot-1", src, "file.txt")
        call = self.exec_call()
        self.assertEqual(call[-5:], ["dev-container", "put", "slot-1", "--stdin", "file.txt"])

    def test_thin_put_streams_dir_via_stdin_tar(self):
        src = os.path.join(self.cwd, "dir")
        os.makedirs(src)
        self.thin("put", "slot-1", src, "dir")
        call = self.exec_call()
        self.assertEqual(call[-5:], ["dev-container", "put", "slot-1", "--stdin-tar", "dir"])

    def test_thin_put_with_stdin_flag(self):
        self.thin("put", "slot-1", "--stdin", "stream.txt")
        call = self.exec_call()
        self.assertEqual(call[-5:], ["dev-container", "put", "slot-1", "--stdin", "stream.txt"])

    def _install_stdout_docker_stub(self):
        self._stub(
            "docker",
            FAKE_DOCKER.replace(
                '  exec) exit "${FAKE_EXEC_RC:-0}" ;;',
                '  exec) [ -n "${FAKE_STDOUT_FILE:-}" ] && [ "${FAKE_EXEC_RC:-0}" = "0" ] && cat "$FAKE_STDOUT_FILE"; exit "${FAKE_EXEC_RC:-0}" ;;',
                1,
            ),
        )

    def test_thin_get_stdout_disables_tty_and_preserves_binary_png_bytes(self):
        self._install_stdout_docker_stub()
        png_bytes = b"\x89PNG\r\n\x1a\n\x00\x01\x02\xff\xfe\r\n" + bytes(range(256))
        payload = os.path.join(self.tmp, "payload.png")
        with open(payload, "wb") as handle:
            handle.write(png_bytes)

        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        try:
            result = self.thin("get", "slot-1", "--stdout", "shot.png", stdin=slave, FAKE_STDOUT_FILE=payload)
        finally:
            os.close(slave)

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        call = self.exec_call()
        self.assertFalse({"-t", "-it", "-ti", "--tty"} & set(call), call)
        self.assertEqual(call[-5:], ["dev-container", "get", "slot-1", "--stdout", "shot.png"])
        self.assertEqual(result.stdout, png_bytes)
        self.assertEqual(self.forbidden_calls(), [])

        os.remove(self.docker_log)
        capped = self.thin("get", "slot-1", "--stdout", "shot.png", "--max-bytes", "64", FAKE_STDOUT_FILE=payload)
        self.assertEqual(capped.returncode, 0, capped.stderr.decode())
        self.assertEqual(
            self.exec_call()[-7:],
            ["dev-container", "get", "slot-1", "--stdout", "shot.png", "--max-bytes", "64"],
        )

    def test_thin_get_to_host_dst_writes_atomically_and_cleans_tmp_on_failure(self):
        self._install_stdout_docker_stub()
        png_bytes = b"\x89PNG\r\n\x1a\n\x00\x01\x02\xff\xfe\r\n" + bytes(range(256))
        payload = os.path.join(self.tmp, "payload.png")
        with open(payload, "wb") as handle:
            handle.write(png_bytes)

        outside = os.path.join(self.tmp, "outside")
        os.makedirs(outside)
        dst = os.path.join(outside, "shot.png")

        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        try:
            result = self.thin("get", "slot-1", "shot.png", dst, stdin=slave, FAKE_STDOUT_FILE=payload)
        finally:
            os.close(slave)

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        call = self.exec_call()
        self.assertFalse({"-t", "-it", "-ti", "--tty"} & set(call), call)
        self.assertEqual(call[-5:], ["dev-container", "get", "slot-1", "--stdout", "shot.png"])
        with open(dst, "rb") as handle:
            self.assertEqual(handle.read(), png_bytes)
        self.assertEqual(os.listdir(outside), ["shot.png"])
        self.assertEqual(self.forbidden_calls(), [])

        fail_dst = os.path.join(outside, "failed.png")
        os.remove(self.docker_log)
        failed = self.thin("get", "slot-1", "shot.png", fail_dst, FAKE_EXEC_RC="1", FAKE_STDOUT_FILE=payload)
        self.assertNotEqual(failed.returncode, 0)
        self.assertFalse(os.path.exists(fail_dst))
        self.assertEqual(os.listdir(outside), ["shot.png"])

        keep_dst = os.path.join(outside, "keep.png")
        with open(keep_dst, "wb") as handle:
            handle.write(b"original")
        os.remove(self.docker_log)
        kept = self.thin("get", "slot-1", "shot.png", keep_dst, FAKE_EXEC_RC="1", FAKE_STDOUT_FILE=payload)
        self.assertNotEqual(kept.returncode, 0)
        with open(keep_dst, "rb") as handle:
            self.assertEqual(handle.read(), b"original")
        self.assertEqual(sorted(os.listdir(outside)), ["keep.png", "shot.png"])

    def test_thin_expose_runs_expose_plan_without_tty_or_forbidden_tools_and_uses_no_at_syntax(self):
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        try:
            result = self.thin("expose", "slot-1", "3000", stdin=slave)
        finally:
            os.close(slave)

        self.assertNotEqual(result.returncode, 0)
        call = self.exec_call()
        self.assertFalse({"-t", "-it", "-ti", "--tty"} & set(call), call)
        self.assertEqual(call[-4:], ["dev-container", "expose-plan", "slot-1", "3000"])
        self.assertEqual(self.forbidden_calls(), [])
        with open(THIN, encoding="utf-8") as handle:
            text = handle.read()
        self.assertNotIn("node" + "@" + "127.0.0.1", text)

    def _install_lifeline_probe_stubs(self, tmpdir, *, fail_first_mkdir=False):
        real_mkfifo = shutil.which("mkfifo")
        self.assertTrue(real_mkfifo, "mkfifo is required on PATH")
        mkfifo_log = os.path.join(self.tmp, "mkfifo.log")
        self._stub(
            "mkfifo",
            "#!/bin/sh\n"
            "last=\n"
            "for a in \"$@\"; do last=$a; done\n"
            "dir=$(dirname \"$last\")\n"
            "mode=$(stat -c '%a' \"$dir\" 2>/dev/null || stat -f '%Lp' \"$dir\" 2>/dev/null || echo unknown)\n"
            f"printf '%s|%s\\n' \"$dir\" \"$mode\" >> \"{mkfifo_log}\"\n"
            f"exec \"{real_mkfifo}\" \"$@\"\n",
        )
        if fail_first_mkdir:
            real_mkdir = shutil.which("mkdir")
            self.assertTrue(real_mkdir, "mkdir is required on PATH")
            mkdir_once = os.path.join(self.tmp, "mkdir-failed-once")
            self._stub(
                "mkdir",
                "#!/bin/sh\n"
                "case \"$*\" in\n"
                "  *dev-container-lifeline*)\n"
                f"    if [ ! -e \"{mkdir_once}\" ]; then\n"
                f"      : > \"{mkdir_once}\"\n"
                "      echo 'mkdir: File exists' >&2\n"
                "      exit 1\n"
                "    fi ;;\n"
                "esac\n"
                f"exec \"{real_mkdir}\" \"$@\"\n",
            )
        probe_out = os.path.join(self.tmp, "exec-stdin-probe.txt")
        probe_snippet = (
            "  exec)\n"
            "    is_pipe=0\n"
            "    [ -p /dev/fd/0 ] && is_pipe=1\n"
            f"    tmp_left=$(ls -A \"{tmpdir}\" 2>/dev/null | tr '\\n' ',')\n"
            "    fd_stat=\n"
            "    fd_flags=\n"
            "    if [ -e /proc/self/fdinfo/0 ]; then\n"
            "      fd_stat=$(stat -L -c '%F:%a:%h' /proc/self/fd/0 2>/dev/null || true)\n"
            "      fd_flags=$(awk '/^flags:/ {print $2}' /proc/self/fdinfo/0 2>/dev/null || true)\n"
            "    fi\n"
            f"    printf 'is_pipe=%s\\ntmp_left=%s\\nfd_stat=%s\\nfd_flags=%s\\n' \"$is_pipe\" \"$tmp_left\" \"$fd_stat\" \"$fd_flags\" > \"{probe_out}\"\n"
            "    exit \"${FAKE_EXEC_RC:-0}\" ;;"
        )
        self._stub(
            "docker",
            FAKE_DOCKER.replace('  exec) exit "${FAKE_EXEC_RC:-0}" ;;', probe_snippet, 1),
        )
        return mkfifo_log, probe_out

    @staticmethod
    def _read_kv(path):
        data = {}
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as handle:
                for line in handle:
                    if "=" in line:
                        k, _, v = line.rstrip("\n").partition("=")
                        data[k] = v
        return data

    def test_lifeline_commands_attach_unlinked_rdwr_fifo_in_0700_tmpdir_disable_tty_and_pass_env(self):
        tmpdir = os.path.join(self.tmp, "sticky-tmp")
        os.makedirs(tmpdir, mode=0o1777)
        mkfifo_log, probe_out = self._install_lifeline_probe_stubs(tmpdir)

        cases = [
            (("acquire", "--for", "holder"), {}),
            (("acquire", "--for", "holder", "--", "--help"), {}),
            (("acquire", "--for", "holder"), {SESSION_VAR: "sess-lifeline"}),
            (("resume", "slot-1"), {}),
            (("resume", "feat-branch", "--", "--help"), {}),
            (("up",), {}),
        ]
        for args, extra_env in cases:
            for use_pty in (False, True):
                with self.subTest(args=args, extra_env=extra_env, use_pty=use_pty):
                    for log_path in (self.docker_log, mkfifo_log, probe_out):
                        if os.path.exists(log_path):
                            os.remove(log_path)
                    if use_pty:
                        master, slave = pty.openpty()
                        self.addCleanup(os.close, master)
                        try:
                            result = self.thin(*args, stdin=slave, TMPDIR=tmpdir, **extra_env)
                        finally:
                            os.close(slave)
                    else:
                        result = self.thin(*args, stdin=subprocess.DEVNULL, TMPDIR=tmpdir, **extra_env)

                    self.assertEqual(result.returncode, 0, result.stderr.decode())
                    call = self.exec_call()
                    self.assertTrue({"-i", "--interactive"} & set(call), call)
                    self.assertFalse({"-t", "-it", "-ti", "--tty"} & set(call), call)
                    passed = self.env_of(call)
                    self.assertIn("DEV_CONTAINER_CALLER_LIFELINE=stdin", passed)
                    if SESSION_VAR in extra_env:
                        self.assertTrue(
                            {SESSION_VAR, f"{SESSION_VAR}={extra_env[SESSION_VAR]}"} & set(passed),
                            passed,
                        )

                    self.assertTrue(os.path.isfile(mkfifo_log), "mkfifo was not called")
                    with open(mkfifo_log, encoding="utf-8") as handle:
                        mk_lines = [ln.strip() for ln in handle if ln.strip()]
                    self.assertEqual(len(mk_lines), 1, mk_lines)
                    fifo_dir, dir_mode = mk_lines[0].split("|", 1)
                    self.assertTrue(
                        fifo_dir.startswith(tmpdir + os.sep + "dev-container-lifeline."),
                        fifo_dir,
                    )
                    self.assertEqual(dir_mode, "700")

                    probe = self._read_kv(probe_out)
                    self.assertEqual(probe.get("is_pipe"), "1", probe)
                    self.assertEqual(probe.get("tmp_left"), "", "FIFO or temp dir was not removed before exec")
                    if probe.get("fd_stat"):
                        self.assertEqual(probe["fd_stat"], "fifo:600:0", probe)
                    if probe.get("fd_flags"):
                        acc_mode = int(probe["fd_flags"], 8) & os.O_ACCMODE
                        self.assertEqual(acc_mode, os.O_RDWR, probe)

                    self.assertEqual(os.listdir(tmpdir), [])
                    self.assertEqual(self.forbidden_calls(), [])

        # Retry across candidate directory suffixes when the first mkdir collides.
        mkfifo_log2, probe_out2 = self._install_lifeline_probe_stubs(tmpdir, fail_first_mkdir=True)
        for log_path in (self.docker_log, mkfifo_log2, probe_out2):
            if os.path.exists(log_path):
                os.remove(log_path)
        retried = self.thin("acquire", "--for", "holder", TMPDIR=tmpdir)
        self.assertEqual(retried.returncode, 0, retried.stderr.decode())
        self.assertIn("DEV_CONTAINER_CALLER_LIFELINE=stdin", self.env_of(self.exec_call()))
        with open(mkfifo_log2, encoding="utf-8") as handle:
            retry_dir, retry_mode = handle.read().strip().split("|", 1)
        self.assertTrue(retry_dir.endswith(".1"), retry_dir)
        self.assertEqual(retry_mode, "700")
        self.assertEqual(self._read_kv(probe_out2).get("tmp_left"), "")
        self.assertEqual(os.listdir(tmpdir), [])
        self.assertEqual(self.forbidden_calls(), [])

        # Adjacent negative and boundary commands must NOT attach lifeline or suppress PTY.
        mkfifo_log, _probe_out = self._install_lifeline_probe_stubs(tmpdir)
        non_lifeline_exec_cases = [
            ("acquire", "--help"),
            ("acquire", "--for", "holder", "--help"),
            ("resume", "--help"),
            ("resume", "slot-1", "--help"),
            ("up", "slot-1"),
            ("up", "--help"),
            ("up", "slot-1", "--toolbox"),
            ("list",),
            ("release", "slot-1", "--for", "holder"),
            ("run", "slot-1", "--", "true"),
            ("doctor",),
            (),
        ]
        for args in non_lifeline_exec_cases:
            with self.subTest(args=args):
                for log_path in (self.docker_log, mkfifo_log):
                    if os.path.exists(log_path):
                        os.remove(log_path)
                result = self.thin(*args, TMPDIR=tmpdir)
                self.assertEqual(result.returncode, 0, result.stderr.decode())
                passed = self.env_of(self.exec_call())
                self.assertFalse(
                    [e for e in passed if e.startswith("DEV_CONTAINER_CALLER_LIFELINE")],
                    passed,
                )
                self.assertFalse(os.path.exists(mkfifo_log), f"mkfifo ran for {args}")
                self.assertEqual(os.listdir(tmpdir), [])
                self.assertEqual(self.forbidden_calls(), [])

        for tty_args in (("acquire", "--help"), ("resume", "--help"), ("up", "slot-1")):
            with self.subTest(tty_args=tty_args):
                os.remove(self.docker_log)
                master, slave = pty.openpty()
                self.addCleanup(os.close, master)
                try:
                    self.thin(*tty_args, stdin=slave, TMPDIR=tmpdir)
                finally:
                    os.close(slave)
                call = self.exec_call()
                self.assertTrue({"-t", "-it", "-ti", "--tty"} & set(call), call)
                self.assertNotIn("DEV_CONTAINER_CALLER_LIFELINE=stdin", self.env_of(call))


if __name__ == "__main__":
    unittest.main()
