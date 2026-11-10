"""adopt leaves no launcher in the project: the global `dev-container` command
(scripts/dev-container-global) is the launcher. It says how to install the
command when it is not on PATH, and may seed .devcontainer/start.sh, a project-
owned script that installs the command if it is missing and then runs it."""

import os
import subprocess

from test_adopt import AdoptTestCase, output_of, read_text, write_text

CLONE = "/opt/example-engine"
FACTS = {"DEV_CONTAINER_CLI_STATE": "off-path", "DEV_CONTAINER_CLI_CLONE": CLONE}
NL = chr(10)


class AdoptLeavesNoLauncher(AdoptTestCase):
    def adopted(self, name="launcher-app", **env):
        checkout = self.make_checkout(name=name)
        self.add_devcontainer_compose(checkout)
        result = self.adopt(checkout, **env)
        self.assertEqual(result.returncode, 0, output_of(result))
        return checkout, result

    def launcher(self, checkout):
        return os.path.join(checkout, "bin", "dev-container")

    def start_script(self, checkout):
        return os.path.join(checkout, ".devcontainer", "start.sh")

    def test_a_fresh_adopt_writes_nothing_under_bin(self):
        checkout, _ = self.adopted()
        self.assertFalse(os.path.lexists(os.path.join(checkout, "bin")))

    def test_an_untracked_launcher_that_is_already_there_is_left_byte_identical(self):
        checkout = self.make_checkout(name="old-stub-app")
        self.add_devcontainer_compose(checkout)
        old = "#!/bin/sh" + NL + "echo old stub" + NL
        write_text(self.launcher(checkout), old)
        os.chmod(self.launcher(checkout), 0o755)
        result = self.adopt(checkout)
        self.assertEqual(result.returncode, 0, output_of(result))
        self.assertEqual(read_text(self.launcher(checkout)), old)
        self.assertTrue(os.access(self.launcher(checkout), os.X_OK))

    def test_not_on_path_it_prints_the_one_install_command_with_the_clone(self):
        _, result = self.adopted(**FACTS)
        self.assertIn(f"{CLONE}/scripts/install-cli.sh", output_of(result))

    def test_shadowed_by_another_command_it_prints_the_install_command_too(self):
        _, result = self.adopted(DEV_CONTAINER_CLI_STATE="shadowed", DEV_CONTAINER_CLI_CLONE=CLONE)
        self.assertIn(f"{CLONE}/scripts/install-cli.sh", output_of(result))

    def test_run_some_other_way_it_still_says_how_to_install_it(self):
        _, result = self.adopted()
        self.assertIn("scripts/install-cli.sh", output_of(result))

    def test_on_path_it_prints_no_install_hint(self):
        _, result = self.adopted(
            DEV_CONTAINER_CLI_STATE="path", DEV_CONTAINER_CLI_CLONE=CLONE,
            DEV_CONTAINER_CLI_FOUND="/u/bin/dev-container")
        self.assertNotIn("install-cli", output_of(result))

    def test_through_the_windows_shim_it_prints_no_install_hint(self):
        _, result = self.adopted(DEV_CONTAINER_CLI_STATE="windows-shim", DEV_CONTAINER_CLI_CLONE=CLONE)
        self.assertNotIn("install-cli", output_of(result))

    def test_it_seeds_a_start_script_that_names_the_clone(self):
        checkout, _ = self.adopted(**FACTS)
        path = self.start_script(checkout)
        self.assertTrue(os.path.isfile(path))
        self.assertTrue(os.access(path, os.X_OK))
        text = read_text(path)
        self.assertEqual(text.splitlines()[0], "#!/bin/sh")
        self.assertIn(f"{CLONE}/scripts/install-cli.sh", text)
        self.assertIn('exec dev-container "$@"', text)
        for bashism in ("[[ ", "<<<", "function ", "=(", "local "):
            self.assertNotIn(bashism, text)

    def test_without_a_known_clone_it_seeds_no_start_script(self):
        checkout, _ = self.adopted()
        self.assertFalse(os.path.exists(self.start_script(checkout)))

    def test_a_start_script_the_project_already_has_is_never_touched(self):
        checkout = self.make_checkout(name="own-start-app")
        self.add_devcontainer_compose(checkout)
        mine = "#!/bin/sh" + NL + "echo mine" + NL
        write_text(self.start_script(checkout), mine)
        result = self.adopt(checkout, **FACTS)
        self.assertEqual(result.returncode, 0, output_of(result))
        self.assertEqual(read_text(self.start_script(checkout)), mine)

    def test_a_second_adopt_leaves_the_seeded_script_as_it_was(self):
        checkout, _ = self.adopted(**FACTS)
        write_text(self.start_script(checkout), read_text(self.start_script(checkout)) + "# edited" + NL)
        before = read_text(self.start_script(checkout))
        again = self.adopt(checkout, **FACTS)
        self.assertEqual(again.returncode, 0, output_of(again))
        self.assertEqual(read_text(self.start_script(checkout)), before)

    def test_the_seeded_script_stays_out_of_git_status(self):
        checkout, _ = self.adopted(**FACTS)
        status = subprocess.run(
            [self.harness.real_git_bin, "-C", checkout, "status", "--porcelain"],
            stdout=subprocess.PIPE, check=True).stdout.decode()
        self.assertEqual(status, "")


class StartScript(AdoptTestCase):
    """The seeded .devcontainer/start.sh, run for real with a fake clone and a fake command."""

    def setUp(self):
        super().setUp()
        self.clone = os.path.join(self.harness.tmp, "clone")
        self.bindir = os.path.join(self.harness.tmp, "bin-on-path")
        self.log = os.path.join(self.harness.tmp, "start.log")
        os.makedirs(self.bindir)
        self.fake_command = "#!/bin/sh" + NL + 'echo "ran $*" >> "' + self.log + '"' + NL
        checkout = self.make_checkout(name="start-app")
        self.add_devcontainer_compose(checkout)
        result = self.adopt(checkout, DEV_CONTAINER_CLI_STATE="off-path", DEV_CONTAINER_CLI_CLONE=self.clone)
        self.assertEqual(result.returncode, 0, output_of(result))
        self.script = os.path.join(checkout, ".devcontainer", "start.sh")

    def install_fake_installer(self, body):
        path = os.path.join(self.clone, "scripts", "install-cli.sh")
        write_text(path, "#!/bin/sh" + NL + body)
        os.chmod(path, 0o755)

    def start(self, *args):
        env = {"PATH": self.bindir + os.pathsep + "/usr/bin" + os.pathsep + "/bin", "HOME": self.harness.tmp}
        return subprocess.run(
            ["/bin/sh", self.script, *args], env=env, cwd=self.harness.tmp,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)

    def test_with_the_command_on_path_it_just_runs_it_with_the_arguments(self):
        command = os.path.join(self.bindir, "dev-container")
        write_text(command, self.fake_command)
        os.chmod(command, 0o755)
        self.install_fake_installer('echo installer-ran >> "' + self.log + '"' + NL)
        result = self.start("list", "--brief")
        self.assertEqual(result.returncode, 0, output_of(result))
        self.assertEqual(read_text(self.log).splitlines(), ["ran list --brief"])

    def test_without_the_command_it_runs_the_installer_first_and_then_the_command(self):
        spare = os.path.join(self.harness.tmp, "fake-command")
        write_text(spare, self.fake_command)
        os.chmod(spare, 0o755)
        self.install_fake_installer(
            'echo installer-ran >> "' + self.log + '"' + NL
            + 'cp "' + spare + '" "' + self.bindir + '/dev-container"' + NL)
        result = self.start("acquire", "--for", "x")
        self.assertEqual(result.returncode, 0, output_of(result))
        self.assertEqual(read_text(self.log).splitlines(), ["installer-ran", "ran acquire --for x"])

    def test_a_failing_installer_stops_it_before_the_command_and_says_so(self):
        self.install_fake_installer("exit 3" + NL)
        result = self.start("list")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(os.path.exists(self.log))
        self.assertIn("install", output_of(result))
