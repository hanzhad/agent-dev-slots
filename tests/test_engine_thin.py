import os
import pty
import subprocess
import unittest

from engine_stub import FakeDockerCase, THIN, option_values

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


if __name__ == "__main__":
    unittest.main()
