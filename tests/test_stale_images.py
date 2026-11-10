import os
import subprocess
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# A shell `docker` that knows three images of one slot: the one this build made,
# a leftover of another service with an old hash, and a leftover that a container
# still uses. Every `image rm` is written to the log.
SCRIPT = r"""
set -u
source "$REPO/lib/images.sh"
discover_overlay_services() { OVERLAY_BUILDS="${BUILDS-app}"; }
LOG=$(mktemp)
docker() {
  case "$1 $2" in
    "image ls") printf '%s\n' slot-app:latest slot-workspace:latest slot-busy:latest slot-other:latest ;;
    "image inspect")
      case "${@: -1}" in
        slot-app:latest) echo NEW ;;
        slot-workspace:latest|slot-busy:latest|slot-other:latest) echo OLD ;;
      esac ;;
    "image rm") echo "rm $3" >>"$LOG" ;;
    "ps -aq") [[ " $* " == *"ancestor=slot-busy:latest"* ]] && echo abc123 ;;
  esac
  return 0
}
drop_unused_stale_images slot NEW
cat "$LOG"
"""


class StaleImages(unittest.TestCase):
    def run_script(self, **extra):
        return subprocess.run(
            ["bash", "-c", SCRIPT], env={**os.environ, "REPO": REPO_ROOT, **extra},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
        )

    def test_a_stale_image_nobody_uses_is_removed(self):
        out = self.run_script().stdout
        self.assertIn("rm slot-workspace:latest", out)

    def test_the_current_image_is_kept(self):
        self.assertNotIn("slot-app:latest", self.run_script().stdout)

    def test_a_stale_image_of_a_service_that_is_built_is_kept(self):
        # a build that failed to label its own image must still fail the check
        out = self.run_script(BUILDS="app workspace").stdout
        self.assertNotIn("slot-workspace:latest", out)

    def test_nothing_is_removed_when_the_built_services_are_unknown(self):
        self.assertEqual(self.run_script(BUILDS="").stdout.strip(), "")

    def test_a_stale_image_a_container_uses_is_kept(self):
        self.assertNotIn("slot-busy:latest", self.run_script().stdout)


if __name__ == "__main__":
    unittest.main()
