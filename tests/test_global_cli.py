"""The one `dev-container` command on PATH (scripts/dev-container-global) and the
installer that puts it there (scripts/install-cli.sh). A project carries no
launcher: the command finds the project from wherever it is run and forwards to
the engine the way scripts/dev-container-thin does."""

import os
import subprocess
import unittest

from engine_stub import FAKE_DOCKER, FakeDockerCase, THIN, option_values
from harness import REPO_ROOT

GLOBAL = os.path.join(REPO_ROOT, "scripts", "dev-container-global")
INSTALL = os.path.join(REPO_ROOT, "scripts", "install-cli.sh")
CLONE = os.path.realpath(REPO_ROOT)

WSLPATH = r"""#!/bin/sh
# Maps C:\work\<rest> to $FAKE_LINUX_ROOT/<rest>, as wslpath -u would for a mount.
[ "${1:-}" = -u ] && shift
[ -z "${FAKE_WSLPATH_FAIL:-}" ] || exit 1
case $1 in
  'C:\work'*)
    rest=$(printf '%s' "${1#'C:\work'}" | tr '\134' '/')
    printf '%s%s\n' "$FAKE_LINUX_ROOT" "$rest" ;;
  *) printf '/mnt/other/%s\n' "$(printf '%s' "$1" | tr '\134' '/')" ;;
esac
"""

NOT_A_PROJECT = "no project.env"


class ProjectsCase(FakeDockerCase):
    def setUp(self):
        super().setUp()
        self.project = os.path.join(self.projects, "demo")
        self.nested = os.path.join(self.project, "src", "deep")
        os.makedirs(self.nested)
        with open(os.path.join(self.project, "project.env"), "w", encoding="utf-8") as handle:
            handle.write("SLOT_PROJECT_PREFIX=demo\n")
        self.elsewhere = os.path.join(self.projects, "not-a-project")
        os.makedirs(self.elsewhere)
        self.bindir = os.path.join(self.tmp, "bin")
        self._stub("wslpath", WSLPATH)
        # The fake docker also records the CLI facts it was started with: they go to the
        # engine by name, so the values live in docker's own environment.
        self._stub("docker", FAKE_DOCKER.replace(
            "state=${FAKE_DOCKER_STATE:-running}",
            'env | grep "^DEV_CONTAINER_CLI_" >> "${FAKE_DOCKER_LOG}.env" || true' + chr(10) + 'state=${FAKE_DOCKER_STATE:-running}', 1))
        self.cli_env_log = self.docker_log + ".env"

    def command(self, *args, cwd, state="running", via=GLOBAL, **extra):
        env = self.env(FAKE_DOCKER_STATE=state, FAKE_LINUX_ROOT=self.projects, **extra)
        return subprocess.run(
            [via, *args], cwd=cwd, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60,
        )

    def exec_call(self):
        calls = self.calls_of("exec")
        self.assertEqual(len(calls), 1, self.calls())
        return calls[0]

    def passed(self):
        return option_values(self.exec_call(), "-e", "--env")

    def caller_pwd(self):
        values = [e for e in self.passed() if e.startswith("DEV_CONTAINER_CALLER_PWD=")]
        self.assertEqual(len(values), 1, self.passed())
        return values[0].split("=", 1)[1]

    def install_into(self, directory=None, **extra):
        directory = directory or self.bindir
        result = subprocess.run(
            ["/bin/sh", INSTALL, "--dir", directory], env=self.env(**extra),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        return os.path.join(directory, "dev-container")


class GlobalCommand(ProjectsCase):
    def test_from_the_project_root_it_forwards_the_arguments_as_written(self):
        result = self.command("acquire", "--for", "x y", "--as", "", cwd=self.project)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(self.exec_call()[-6:], ["dev-container", "acquire", "--for", "x y", "--as", ""])
        self.assertEqual(self.caller_pwd(), self.project)

    def test_from_a_nested_directory_it_finds_the_project_and_passes_that_directory(self):
        result = self.command("list", cwd=self.nested)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(self.caller_pwd(), self.nested)

    def test_outside_any_project_a_project_verb_is_refused_with_the_way_forward(self):
        result = self.command("run", "slot-1", "--", "ls", cwd=self.elsewhere)
        text = result.stderr.decode()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("project.env", text)
        self.assertIn(self.elsewhere, text)
        self.assertIn("dev-container adopt", text)
        self.assertEqual(self.calls_of("exec"), [])

    def test_verbs_that_name_no_project_pass_through_outside_one(self):
        for args in (("adopt",), ("status", "--json"), ("doctor",), ("migrate-names", "--dry-run"),
                     ("acquire", "--for", "x"), ("reap", "--all"), ("run", "--help"), ("--help",)):
            with self.subTest(args=args):
                if os.path.exists(self.docker_log):
                    os.remove(self.docker_log)
                result = self.command(*args, cwd=self.elsewhere)
                self.assertEqual(result.returncode, 0, result.stderr.decode())
                self.assertEqual(self.caller_pwd(), self.elsewhere)

    def test_the_engines_exit_code_is_the_commands(self):
        self.assertEqual(self.command("list", cwd=self.nested, FAKE_EXEC_RC="7").returncode, 7)

    def test_the_engine_is_told_the_command_is_called_dev_container(self):
        self.command("list", cwd=self.nested)
        self.assertIn("DEV_CONTAINER_CALLER=dev-container", self.passed())

    def test_a_stopped_engine_is_refused_by_the_thin_with_the_command_that_starts_it(self):
        result = self.command("list", cwd=self.nested, state="stopped")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("docker start dev-container", result.stderr.decode())

    def test_it_needs_docker_and_sh_only(self):
        self.command("list", cwd=self.nested)
        self.command("run", "slot-1", "--", "ls", cwd=self.elsewhere)
        self.assertEqual(self.forbidden_calls(), [])

    def test_through_the_installed_link_it_finds_its_engine_clone_and_works_the_same(self):
        link = self.install_into()
        result = self.command("list", cwd=self.nested, via=link)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(self.caller_pwd(), self.nested)
        self.assertIn("DEV_CONTAINER_CALLER=dev-container", self.passed())

    def test_it_is_a_posix_sh_script(self):
        self.assertTrue(os.path.isfile(GLOBAL), "scripts/dev-container-global does not exist")
        with open(GLOBAL, encoding="utf-8") as handle:
            text = handle.read()
        self.assertEqual(text.splitlines()[0], "#!/bin/sh")
        for bashism in ("[[ ", "<<<", "function ", "=(", "readlink -f", "local "):
            self.assertNotIn(bashism, text)
        self.assertTrue(os.access(GLOBAL, os.X_OK), "scripts/dev-container-global is not executable")


class WindowsShim(ProjectsCase):
    """The shim hands the Windows directory over in DEV_CONTAINER_WINDOWS_CWD; the
    command maps it with wslpath when it is inside a project."""

    def windows(self, *args, win, cwd, **extra):
        return self.command(*args, cwd=cwd, DEV_CONTAINER_WINDOWS_CWD=win, **extra)

    def test_a_windows_directory_inside_a_project_is_mapped_and_used(self):
        result = self.windows("list", win=r"C:\work\demo\src\deep", cwd=self.elsewhere)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(self.caller_pwd(), self.nested)

    def test_a_windows_directory_outside_any_project_leaves_the_working_directory_alone(self):
        result = self.windows("list", win=r"C:\work\not-a-project", cwd=self.nested)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(self.caller_pwd(), self.nested)

    def test_a_directory_that_does_not_exist_after_mapping_is_ignored(self):
        result = self.windows("list", win=r"C:\work\demo\gone", cwd=self.nested)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(self.caller_pwd(), self.nested)

    def test_a_wslpath_that_fails_is_ignored(self):
        result = self.windows("list", win=r"C:\work\demo", cwd=self.nested, FAKE_WSLPATH_FAIL="1")
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(self.caller_pwd(), self.nested)

    def test_with_no_project_either_way_the_refusal_names_the_windows_directory(self):
        result = self.windows("run", "slot-1", "--", "ls", win=r"C:\work\not-a-project", cwd=self.elsewhere)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(r"C:\work\not-a-project", result.stderr.decode())
        self.assertEqual(self.calls_of("exec"), [])

    def test_the_shim_script_hands_the_directory_over_and_calls_the_linux_command(self):
        with open(os.path.join(REPO_ROOT, "scripts", "install-cli.ps1"), encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("DEV_CONTAINER_WINDOWS_CWD", text)
        self.assertIn("WSLENV", text)
        self.assertIn("wsl.exe", text)
        self.assertIn("dev-container-global", text)


class CliFactsForDoctor(ProjectsCase):
    """The command tells `doctor` whether it is on PATH and which clone it runs from."""

    def facts(self, **extra):
        result = self.command("doctor", cwd=self.nested, **extra)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        with open(self.cli_env_log, encoding="utf-8") as handle:
            return dict(line.rstrip().split("=", 1) for line in handle if "=" in line)

    def on_path(self, *front):
        return os.pathsep.join([*front, self.bindir, self.fake_bin, "/usr/bin", "/bin"])

    def test_on_path_it_names_the_command_it_found_and_the_clone_behind_it(self):
        link = self.install_into()
        facts = self.facts(PATH=self.on_path(), via=link)
        self.assertEqual(facts["DEV_CONTAINER_CLI_STATE"], "path")
        self.assertEqual(facts["DEV_CONTAINER_CLI_FOUND"], link)
        self.assertEqual(facts["DEV_CONTAINER_CLI_CLONE"], CLONE)

    def test_run_by_its_path_while_not_on_path_it_says_so_and_still_names_the_clone(self):
        facts = self.facts()
        self.assertEqual(facts["DEV_CONTAINER_CLI_STATE"], "off-path")
        self.assertEqual(facts["DEV_CONTAINER_CLI_CLONE"], CLONE)

    def test_another_dev_container_earlier_on_path_is_reported_as_shadowing(self):
        link = self.install_into()
        other = os.path.join(self.tmp, "other")
        os.makedirs(other)
        self._stub_in(other, "dev-container", "#!/bin/sh\nexit 0\n")
        facts = self.facts(PATH=self.on_path(other), via=link)
        self.assertEqual(facts["DEV_CONTAINER_CLI_STATE"], "shadowed")
        self.assertEqual(facts["DEV_CONTAINER_CLI_FOUND"], os.path.join(other, "dev-container"))

    def test_through_the_windows_shim_the_state_is_the_shim(self):
        facts = self.facts(DEV_CONTAINER_WINDOWS_CWD=r"C:\work\demo")
        self.assertEqual(facts["DEV_CONTAINER_CLI_STATE"], "windows-shim")
        self.assertEqual(facts["DEV_CONTAINER_CLI_CLONE"], CLONE)

    def test_adopt_gets_the_facts_too_so_it_can_say_how_to_install_the_command(self):
        result = self.command("adopt", cwd=self.elsewhere)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        with open(self.cli_env_log, encoding="utf-8") as handle:
            facts = dict(line.rstrip().split("=", 1) for line in handle if "=" in line)
        self.assertEqual(facts["DEV_CONTAINER_CLI_STATE"], "off-path")
        self.assertEqual(facts["DEV_CONTAINER_CLI_CLONE"], CLONE)

    def test_other_verbs_carry_no_such_facts(self):
        self.command("list", cwd=self.nested)
        self.assertFalse([e for e in self.passed() if e.startswith("DEV_CONTAINER_CLI_")], self.passed())

    def _stub_in(self, directory, name, body):
        path = os.path.join(directory, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(body)
        os.chmod(path, 0o755)


class ThinForwardsWhatTheCommandSets(FakeDockerCase):
    def setUp(self):
        super().setUp()
        self.cwd = os.path.join(self.projects, "demo")
        os.makedirs(self.cwd)

    def thin(self, **extra):
        env = self.env(PWD=self.cwd, **extra)
        result = self.sh(THIN, "doctor", cwd=self.cwd, env=env)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        return option_values(self.calls_of("exec")[0], "-e", "--env")

    def test_a_caller_name_set_by_the_command_is_the_one_the_engine_gets(self):
        self.assertIn("DEV_CONTAINER_CALLER=dev-container", self.thin(DEV_CONTAINER_CALLER="dev-container"))

    def test_the_cli_facts_are_passed_by_name_when_set_and_not_invented_when_not(self):
        passed = self.thin(DEV_CONTAINER_CLI_STATE="path", DEV_CONTAINER_CLI_CLONE="/x")
        self.assertIn("DEV_CONTAINER_CLI_STATE", passed)
        self.assertIn("DEV_CONTAINER_CLI_CLONE", passed)
        self.assertNotIn("DEV_CONTAINER_CLI_FOUND", passed)
        os.remove(self.docker_log)
        self.assertFalse([e for e in self.thin() if e.startswith("DEV_CONTAINER_CLI_")])


class InstallCli(ProjectsCase):
    def install(self, *args, **extra):
        return subprocess.run(
            ["/bin/sh", INSTALL, *args], env=self.env(**extra), cwd=self.tmp,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60,
        )

    def said(self, result):
        return result.stdout.decode() + result.stderr.decode()

    def target(self):
        return os.path.join(CLONE, "scripts", "dev-container-global")

    def test_a_fresh_install_links_the_command_to_the_global_script_of_this_clone(self):
        result = self.install("--dir", self.bindir)
        self.assertEqual(result.returncode, 0, self.said(result))
        link = os.path.join(self.bindir, "dev-container")
        self.assertTrue(os.path.islink(link))
        self.assertEqual(os.path.realpath(link), self.target())

    def test_with_no_directory_given_it_uses_local_bin_in_the_home_and_creates_it(self):
        result = self.install()
        self.assertEqual(result.returncode, 0, self.said(result))
        link = os.path.join(self.home, ".local", "bin", "dev-container")
        self.assertEqual(os.path.realpath(link), self.target())

    def test_a_second_run_changes_nothing_and_says_so(self):
        self.install("--dir", self.bindir)
        link = os.path.join(self.bindir, "dev-container")
        before = os.lstat(link).st_ino
        result = self.install("--dir", self.bindir)
        self.assertEqual(result.returncode, 0, self.said(result))
        self.assertIn("already", self.said(result))
        self.assertEqual(os.lstat(link).st_ino, before)

    def test_a_link_to_another_clone_is_repointed_and_the_old_target_is_named(self):
        os.makedirs(self.bindir)
        link = os.path.join(self.bindir, "dev-container")
        os.symlink("/somewhere/else/scripts/dev-container-global", link)
        result = self.install("--dir", self.bindir)
        self.assertEqual(result.returncode, 0, self.said(result))
        self.assertIn("/somewhere/else/scripts/dev-container-global", self.said(result))
        self.assertEqual(os.path.realpath(link), self.target())

    def test_a_file_that_is_not_ours_is_refused_and_left_as_it_was(self):
        os.makedirs(self.bindir)
        path = os.path.join(self.bindir, "dev-container")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("#!/bin/sh\necho a project stub\n")
        result = self.install("--dir", self.bindir)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(path, self.said(result))
        self.assertFalse(os.path.islink(path))
        with open(path, encoding="utf-8") as handle:
            self.assertIn("a project stub", handle.read())

    def test_a_directory_not_on_path_gets_the_exact_line_to_add(self):
        result = self.install("--dir", self.bindir)
        self.assertIn(f'export PATH="{self.bindir}:$PATH"', self.said(result))

    def test_the_default_directory_is_shown_with_home_not_its_expansion(self):
        result = self.install()
        self.assertIn('export PATH="$HOME/.local/bin:$PATH"', self.said(result))

    def test_a_directory_already_on_path_gets_no_hint(self):
        path = os.pathsep.join([self.bindir, self.fake_bin, "/usr/bin", "/bin"])
        result = self.install("--dir", self.bindir, PATH=path)
        self.assertEqual(result.returncode, 0, self.said(result))
        self.assertNotIn("export PATH", self.said(result))

    def test_another_dev_container_earlier_on_path_is_called_out(self):
        other = os.path.join(self.tmp, "other")
        os.makedirs(other)
        stub = os.path.join(other, "dev-container")
        with open(stub, "w", encoding="utf-8") as handle:
            handle.write("#!/bin/sh\nexit 0\n")
        os.chmod(stub, 0o755)
        path = os.pathsep.join([other, self.bindir, self.fake_bin, "/usr/bin", "/bin"])
        result = self.install("--dir", self.bindir, PATH=path)
        self.assertEqual(result.returncode, 0, self.said(result))
        self.assertIn(stub, self.said(result))
        self.assertRegex(self.said(result), r"(?i)shadow|first|earlier")

    def test_shell_profiles_are_never_touched(self):
        names = (".profile", ".bashrc", ".zshrc", ".bash_profile", ".zprofile")
        for name in names:
            with open(os.path.join(self.home, name), "w", encoding="utf-8") as handle:
                handle.write("# mine\n")
        self.install("--dir", self.bindir)
        self.install()
        for name in names:
            with open(os.path.join(self.home, name), encoding="utf-8") as handle:
                self.assertEqual(handle.read(), "# mine\n", name)
        self.assertEqual(sorted(os.listdir(self.home)), sorted(names + (".local",)))

    def test_it_needs_sh_only_and_no_root(self):
        self.install("--dir", self.bindir)
        self.assertEqual(self.forbidden_calls(), [])

    def test_an_unknown_option_is_refused_with_the_usage(self):
        result = self.install("--bogus")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Usage", self.said(result))

    def test_uninstall_removes_our_link_and_only_ours(self):
        self.install("--dir", self.bindir)
        result = self.install("--uninstall", "--dir", self.bindir)
        self.assertEqual(result.returncode, 0, self.said(result))
        self.assertFalse(os.path.lexists(os.path.join(self.bindir, "dev-container")))
        path = os.path.join(self.bindir, "dev-container")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("not ours\n")
        result = self.install("--uninstall", "--dir", self.bindir)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(os.path.isfile(path))

    def test_it_is_a_posix_sh_script(self):
        self.assertTrue(os.path.isfile(INSTALL), "scripts/install-cli.sh does not exist")
        with open(INSTALL, encoding="utf-8") as handle:
            text = handle.read()
        self.assertEqual(text.splitlines()[0], "#!/bin/sh")
        for bashism in ("[[ ", "<<<", "function ", "=(", "readlink -f", "local "):
            self.assertNotIn(bashism, text)
        self.assertTrue(os.access(INSTALL, os.X_OK), "scripts/install-cli.sh is not executable")


if __name__ == "__main__":
    unittest.main()
