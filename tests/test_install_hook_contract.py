"""What the project's install hook (install.sh in the compose directory) sees.

The contract: the hook runs on the host, before the image build, and without the
engine's overlays. It gets WORKSPACE_DIR and COMPOSE_PROJECT_NAME (and
PNPM_STORE_VOLUME when set) and nothing the engine adds for the slot: no forge
token, no credential helper, no signing key. In a volume slot WORKSPACE_DIR is
the volume's compose name, not a path. A hook that fails stops the command.
"""
import os
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


if __name__ == "__main__":
    unittest.main()
