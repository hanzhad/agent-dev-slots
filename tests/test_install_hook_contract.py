"""What the project's install hook (install.sh in the compose directory) sees.

The contract: the hook runs on the host, before the image build, and without the
engine's overlays. It gets WORKSPACE_DIR and COMPOSE_PROJECT_NAME (and
PNPM_STORE_VOLUME when set) and nothing the engine adds for the slot: no forge
token, no credential helper, no signing key. In a volume slot WORKSPACE_DIR is
the volume's compose name, not a path. A hook that fails stops the command.
"""
import os
import subprocess
import unittest
from unittest import mock

from harness import DevContainerTestCase, PROJECT_PREFIX, project_env_text

SECRET = "ghp_fixture_secret_value"
ENV = (
    "GIT_USER_NAME=demo-agent\nGIT_USER_EMAIL=demo-agent@example.com\n"
    f"GH_TOKEN={SECRET}\n"
)

# Records what it was given, one line per fact, into the docker stub's state
# directory; the real install.sh is never run.
RECORDING_HOOK = r"""#!/usr/bin/env bash
set -u
state_dir="${DOCKER_STUB_STATE:?}"
project="${COMPOSE_PROJECT_NAME:?}"
out="$state_dir/$project.hook-seen"
{
  echo "script=$0"
  echo "cwd=$(pwd)"
  echo "WORKSPACE_DIR=${WORKSPACE_DIR-UNSET}"
  echo "COMPOSE_PROJECT_NAME=$COMPOSE_PROJECT_NAME"
  echo "GH_TOKEN=${GH_TOKEN-UNSET}"
  echo "SLOT_GH_TOKEN=${SLOT_GH_TOKEN-UNSET}"
  echo "SLOT_WORKSPACE_VOLUME=${SLOT_WORKSPACE_VOLUME-UNSET}"
  echo "SLOT_ENV_FILE=${SLOT_ENV_FILE-UNSET}"
  if [[ -e "$state_dir/$project.image-hash" ]]; then echo "image_built=yes"; else echo "image_built=no"; fi
  echo "SECRET_IN_ENV=$(env | grep -v "^SECRET_PROBE=" | grep -c -F "$SECRET_PROBE" || true)"
} > "$out"
"""

FAILING_HOOK = "#!/usr/bin/env bash\necho 'hook: boom' >&2\nexit 1\n"


class InstallHookInAVolumeSlot(DevContainerTestCase):

    def _acquire_with_hook(self, hook=RECORDING_HOOK, env=None):
        self.harness.build_fixture_repo(
            with_parent_env=ENV,
            project_env=project_env_text(FORGE_AUTH="token", FORGE_AUTH_FILE=".devcontainer/.env"),
        )
        path = os.path.join(self.harness.fixture_repo, ".devcontainer", "install.sh")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(hook)
        with mock.patch.dict(os.environ, {"SECRET_PROBE": SECRET}):
            os.environ.pop("GH_TOKEN", None)
            os.environ.update(env or {})
            return self.harness.run_acquire()

    def _seen(self, result):
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        name = self.harness.acquired_slot_name(result)
        path = os.path.join(self.harness.docker_state, f"{PROJECT_PREFIX}-{name}.hook-seen")
        self.assertTrue(os.path.isfile(path), "the install hook did not run")
        seen = {}
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                key, _, value = line.rstrip("\n").partition("=")
                seen[key] = value
        return name, seen

    def test_workspace_dir_is_the_volume_name_not_a_path(self):
        _, seen = self._seen(self._acquire_with_hook())
        self.assertEqual(seen["WORKSPACE_DIR"], "slot-workspace")

    def test_compose_project_name_is_the_slots_project(self):
        name, seen = self._seen(self._acquire_with_hook())
        self.assertEqual(seen["COMPOSE_PROJECT_NAME"], f"{PROJECT_PREFIX}-{name}")

    def test_the_hook_is_the_one_in_the_compose_directory(self):
        _, seen = self._seen(self._acquire_with_hook())
        expected = os.path.join(self.harness.fixture_repo, ".devcontainer", "install.sh")
        self.assertEqual(os.path.realpath(seen["script"]), os.path.realpath(expected))

    def test_the_hook_runs_before_the_image_is_built(self):
        name, seen = self._seen(self._acquire_with_hook())
        self.assertEqual(seen["image_built"], "no")
        built = os.path.join(self.harness.docker_state, f"{PROJECT_PREFIX}-{name}.image-hash")
        self.assertTrue(os.path.isfile(built), "acquire never built the image")

    def test_the_engines_overlay_variables_are_not_set(self):
        _, seen = self._seen(self._acquire_with_hook())
        self.assertEqual(seen["SLOT_WORKSPACE_VOLUME"], "UNSET")
        self.assertEqual(seen["SLOT_ENV_FILE"], "UNSET")
        self.assertEqual(seen["SLOT_GH_TOKEN"], "UNSET")

    def test_the_slots_forge_token_is_not_in_the_hook_environment(self):
        _, seen = self._seen(self._acquire_with_hook())
        self.assertEqual(seen["GH_TOKEN"], "UNSET")
        self.assertEqual(seen["SECRET_IN_ENV"], "0")

    def test_the_callers_own_forge_token_is_not_passed_to_the_hook(self):
        _, seen = self._seen(self._acquire_with_hook(env={"GH_TOKEN": "ghp_callers_own_token"}))
        self.assertEqual(seen["GH_TOKEN"], "UNSET")

    def test_a_failing_hook_stops_acquire_and_no_image_is_built(self):
        result = self._acquire_with_hook(hook=FAILING_HOOK)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("installing dependencies", result.stderr.decode())
        self.assertIn("hook: boom", result.stderr.decode())
        images = [f for f in os.listdir(self.harness.docker_state) if f.endswith(".image-hash")]
        self.assertEqual(images, [])


class InstallHookAbsent(DevContainerTestCase):
    """What each entry point that reaches the hook does when install.sh is missing."""

    def _slot_without_hook(self):
        h = self.harness
        h.build_fixture_repo(with_parent_env=ENV.replace(SECRET, "x"), project_env=project_env_text())
        os.remove(os.path.join(h.fixture_repo, ".devcontainer", "install.sh"))
        for args in (("branch", "-M", "master"), ("add", "-A"), ("commit", "-q", "-m", "no hook")):
            subprocess.run(["git", "-C", h.fixture_repo, *args], check=True, stdout=subprocess.PIPE)
        result = h.run_acquire()
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        return h.acquired_slot_name(result)

    def _text(self, result):
        return (result.stdout + result.stderr).decode()

    def test_install_command_refuses_a_missing_hook_without_a_store_volume(self):
        name = self._slot_without_hook()
        result = self.harness.run("install", name)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("this project has no install script", self._text(result))

    def test_install_command_refuses_a_missing_hook_with_a_store_volume(self):
        name = self._slot_without_hook()
        result = self.harness.run("install", name, port_overrides={"PNPM_STORE_VOLUME": "store-x"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("this project has no install script", self._text(result))

    def test_acquire_says_the_install_is_skipped(self):
        h = self.harness
        h.build_fixture_repo(with_parent_env=ENV.replace(SECRET, "x"), project_env=project_env_text())
        os.remove(os.path.join(h.fixture_repo, ".devcontainer", "install.sh"))
        result = h.run_acquire()
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertIn("install is skipped", result.stdout.decode())

    def test_acquire_refuses_a_missing_hook_with_a_store_volume(self):
        h = self.harness
        h.build_fixture_repo(
            with_parent_env=ENV.replace(SECRET, "x"), project_env=project_env_text(PNPM_STORE_VOLUME="store-x")
        )
        os.remove(os.path.join(h.fixture_repo, ".devcontainer", "install.sh"))
        result = h.run_acquire()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("PNPM_STORE_VOLUME", result.stderr.decode())

    def test_resync_reruns_the_hook_when_node_modules_drifted(self):
        h = self.harness
        h.build_fixture_repo(with_parent_env=ENV.replace(SECRET, "x"), project_env=project_env_text())
        acquired = h.run_acquire()
        self.assertEqual(acquired.returncode, 0, acquired.stderr.decode())
        name = h.acquired_slot_name(acquired)
        calls = os.path.join(h.docker_state, f"{PROJECT_PREFIX}-{name}.install-calls")

        def count():
            with open(calls, encoding="utf-8") as handle:
                return len(handle.read().splitlines())

        self.assertEqual(h.run("release", name, "--for", "test").returncode, 0)
        reused = h.run("acquire", "--for", "test", port_overrides={"PNPM_STORE_VOLUME": "store-x"})
        self.assertEqual(reused.returncode, 0, reused.stderr.decode())
        without_drift = count()
        self.assertIn("already matches pnpm-lock.yaml", reused.stdout.decode())
        self.assertEqual(h.run("release", name, "--for", "test").returncode, 0)
        with open(os.path.join(h.docker_state, f"{PROJECT_PREFIX}-{name}.lockfile-drift"), "w"):
            pass
        drifted = h.run("acquire", "--for", "test", port_overrides={"PNPM_STORE_VOLUME": "store-x"})
        self.assertEqual(drifted.returncode, 0, drifted.stderr.decode())
        self.assertIn("node_modules resynced", drifted.stdout.decode())
        self.assertEqual(count(), without_drift + 2)

    def test_readme_hook_section_names_every_entry_point_and_the_install_command_refusal(self):
        with open(os.path.join(os.path.dirname(__file__), "..", "README.md"), encoding="utf-8") as handle:
            readme = handle.read()
        section = readme.split("## The install hook", 1)[1].split("\n## ", 1)[0]
        self.assertIn("node_modules", section)
        self.assertIn("pnpm-lock.yaml", section)
        self.assertIn("this project has no install script", section)


if __name__ == "__main__":
    unittest.main()
