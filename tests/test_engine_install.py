import os
import unittest

from engine_stub import ENGINE, FakeDockerCase, option_values

SOCKET = "/var/run/docker.sock"


class EngineInstall(FakeDockerCase):
    """`scripts/engine install` starts the engine container; it needs docker and sh."""

    def setUp(self):
        super().setUp()
        self.state = os.path.join(self.home, ".dev-containers")
        self.limits = os.path.join(self.home, ".config", "machine-limits")
        os.makedirs(self.limits)

    def install(self, *args, env=None):
        args = args or ("--projects-root", self.projects)
        return self.sh(ENGINE, "install", *args, env=env)

    def started(self):
        runs = [c for c in self.calls_of("run") if "--rm" not in c]
        self.assertEqual(len(runs), 1, self.calls())
        return runs[0]

    def installed(self, **extra):
        result = self.install(env=self.env(**extra) if extra else None)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        return self.started()

    def volumes(self, call):
        return option_values(call, "-v", "--volume")

    def test_the_container_is_named_dev_container_and_restarts_unless_stopped(self):
        call = self.installed()
        self.assertIn("-d", call)
        self.assertEqual(option_values(call, "--name"), ["dev-container"])
        self.assertEqual(option_values(call, "--restart"), ["unless-stopped"])

    def test_the_image_is_built_locally_and_the_container_runs_that_image(self):
        call = self.installed()
        builds = self.calls_of("build")
        self.assertEqual(len(builds), 1, self.calls())
        tags = option_values(builds[0], "-t", "--tag")
        self.assertEqual(len(tags), 1, builds[0])
        self.assertIn(tags[0], call)
        self.assertNotIn("pull", [c[0] for c in self.calls()])

    def test_state_the_projects_root_and_machine_limits_sit_at_identical_paths(self):
        volumes = self.volumes(self.installed())
        self.assertIn(f"{self.state}:{self.state}", volumes)
        self.assertIn(f"{self.projects}:{self.projects}", volumes)
        self.assertIn(f"{self.limits}:{self.limits}:ro", volumes)

    def test_a_state_directory_from_the_environment_is_mirrored_too(self):
        elsewhere = os.path.join(self.tmp, "elsewhere-state")
        call = self.installed(DEV_CONTAINER_HOME=elsewhere)
        self.assertIn(f"{elsewhere}:{elsewhere}", self.volumes(call))

    def test_the_projects_root_is_passed_to_the_engine(self):
        call = self.installed()
        self.assertIn(f"DEV_CONTAINER_PROJECTS_ROOT={self.projects}", option_values(call, "-e", "--env"))

    def test_schedule_intervals_are_passed_when_set(self):
        call = self.installed(
            DEV_CONTAINER_SCHEDULE_FAST="6",
            DEV_CONTAINER_SCHEDULE_SLOW="120",
            DEV_CONTAINER_SCHEDULE_TICK="1",
        )
        passed = option_values(call, "-e", "--env")
        self.assertIn("DEV_CONTAINER_SCHEDULE_FAST=6", passed)
        self.assertIn("DEV_CONTAINER_SCHEDULE_SLOW=120", passed)
        self.assertIn("DEV_CONTAINER_SCHEDULE_TICK=1", passed)

    def test_home_is_a_tmpfs_owned_by_the_host_user_at_the_host_path(self):
        call = self.installed()
        homes = [t for t in option_values(call, "--tmpfs") if t.split(":")[0] == self.home]
        self.assertEqual(len(homes), 1, option_values(call, "--tmpfs"))
        self.assertIn(f"uid={os.getuid()}", homes[0])
        self.assertIn(f"HOME={self.home}", option_values(call, "-e", "--env"))
        self.assertNotIn(f"{self.home}:{self.home}", self.volumes(call), "the whole home is mirrored")

    def test_run_dev_container_is_a_private_noexec_tmpfs(self):
        call = self.installed()
        runs = [t for t in option_values(call, "--tmpfs") if t.split(":")[0] == "/run/dev-container"]
        self.assertEqual(len(runs), 1, option_values(call, "--tmpfs"))
        self.assertIn("mode=0700", runs[0])
        self.assertIn("noexec", runs[0])

    def test_the_container_runs_as_the_host_uid_with_the_sockets_group(self):
        call = self.installed(FAKE_DOCKER_GID="4242")
        users = option_values(call, "-u", "--user")
        self.assertEqual(len(users), 1, call)
        self.assertEqual(users[0].split(":")[0], str(os.getuid()))
        self.assertEqual(option_values(call, "--group-add"), ["4242"])

    def test_a_passwd_file_in_state_names_the_host_user_and_is_bound_over_etc_passwd(self):
        call = self.installed()
        passwd = os.path.join(self.state, "passwd")
        self.assertIn(f"{passwd}:/etc/passwd:ro", self.volumes(call))
        with open(passwd, encoding="utf-8") as handle:
            rows = [line.rstrip("\n").split(":") for line in handle if line.strip()]
        mine = [row for row in rows if row[2] == str(os.getuid())]
        self.assertEqual(len(mine), 1, rows)
        self.assertTrue(mine[0][0])
        self.assertEqual(mine[0][5], self.home)

    def test_the_socket_is_the_only_privilege(self):
        call = self.installed()
        for flag in ("--privileged", "--cap-add", "--device", "--pid", "--ipc", "--userns", "--security-opt"):
            self.assertFalse([w for w in call if w == flag or w.startswith(flag + "=")], f"{flag} in {call}")
        networks = option_values(call, "--network", "--net")
        self.assertNotIn("host", networks)
        allowed = {
            self.state, self.projects, self.limits, SOCKET, os.path.join(self.state, "passwd"),
        }
        sources = {v.split(":")[0] for v in self.volumes(call)}
        self.assertEqual(sources - allowed, set(), self.volumes(call))
        self.assertIn(SOCKET, sources)

    def test_a_projects_root_that_does_not_exist_is_refused_before_anything_starts(self):
        result = self.install("--projects-root", os.path.join(self.tmp, "no-such-dir"))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual([c for c in self.calls_of("run") if "--rm" not in c], [])
        self.assertIn("projects-root", (result.stdout + result.stderr).decode())

    def test_install_needs_docker_and_sh_only(self):
        self.installed()
        self.assertEqual(self.forbidden_calls(), [])

    def test_it_is_a_posix_sh_script(self):
        self.assertTrue(os.path.isfile(ENGINE), "scripts/engine does not exist")
        with open(ENGINE, encoding="utf-8") as handle:
            text = handle.read()
        self.assertEqual(text.splitlines()[0], "#!/bin/sh")
        for bashism in ("[[ ", "<<<", "function ", "=("):
            self.assertNotIn(bashism, text)
        self.assertTrue(os.access(ENGINE, os.X_OK), "scripts/engine is not executable")


class EngineUninstall(FakeDockerCase):
    def test_uninstall_removes_the_container_and_leaves_state(self):
        state = os.path.join(self.home, ".dev-containers")
        os.makedirs(os.path.join(state, "leases"))
        marker = os.path.join(state, "leases", "keep.json")
        with open(marker, "w", encoding="utf-8") as handle:
            handle.write("{}")
        result = self.sh(ENGINE, "uninstall")
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        removed = [c for c in self.calls() if c[0] in ("rm", "stop", "container") and "dev-container" in c]
        self.assertTrue(removed, self.calls())
        self.assertTrue(os.path.isfile(marker), "uninstall deleted state")
        self.assertEqual(self.forbidden_calls(), [])

    def test_uninstall_does_not_remove_volumes_or_images(self):
        self.sh(ENGINE, "uninstall")
        self.assertTrue(self.calls(), "uninstall ran no docker command")
        for call in self.calls():
            self.assertNotIn(call[0], ("volume", "rmi", "image"), call)
            self.assertNotIn("-v", call, call)


if __name__ == "__main__":
    unittest.main()
