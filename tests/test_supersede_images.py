import os
import subprocess
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# build_slot_image against a shell `docker` that holds one image tag whose id
# the "build" moves along IDS (space separated, one step per build). Every
# `rmi` is written to the log; BUSY names an id a container still uses and
# RMI_FAIL makes `rmi` fail. Nothing here touches a real docker.
SCRIPT = r"""
set -u
source "$REPO/lib/images.sh"
die() { echo "die: $*" >&2; exit 1; }
STATE=$(mktemp -d)
LOG=$STATE/log
: >"$LOG"
set -- $IDS
echo "$1" >"$STATE/id"
shift
STEPS=("$@")
docker() {
  case "$1 $2" in
    "image ls") echo slot-app:latest ;;
    "image inspect")
      if [[ "$*" == *Labels* ]]; then echo H; else cat "$STATE/id"; fi ;;
    "ps -aq") [[ " $* " == *"ancestor=${BUSY:-none}"* ]] && echo abc123 ;;
    "ps -a") [[ " $* " == *"ancestor=${BUSY:-none}"* ]] && echo abc123 ;;
    rmi*) echo "rmi $2" >>"$LOG"; [[ -z "${RMI_FAIL:-}" ]] ;;
  esac
}
slot_build_hash() { echo H; }
slot_compose() { echo "${STEPS[$STEP]}" >"$STATE/id"; STEP=$((STEP + 1)); }
drop_unused_stale_images() { :; }
STEP=0
for _ in "${STEPS[@]}"; do
  build_slot_image "" slot || { echo "BUILD_FAILED"; break; }
done
cat "$LOG"
"""


class SupersededImages(unittest.TestCase):
    def run_script(self, ids, **extra):
        return subprocess.run(
            ["bash", "-c", SCRIPT], env={**os.environ, "REPO": REPO_ROOT, "IDS": ids, **extra},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
        )

    def rmis(self, result):
        return [line for line in result.stdout.splitlines() if line.startswith("rmi ")]

    def test_two_consecutive_rebuilds_remove_each_superseded_id_once(self):
        result = self.run_script("sha256:A sha256:B sha256:C")
        self.assertEqual(self.rmis(result), ["rmi sha256:A", "rmi sha256:B"])
        self.assertNotIn("rmi sha256:C", result.stdout)

    def test_an_old_image_a_container_uses_is_never_removed(self):
        result = self.run_script("sha256:A sha256:B", BUSY="sha256:A")
        self.assertEqual(self.rmis(result), [])
        self.assertNotIn("BUILD_FAILED", result.stdout)

    def test_a_failing_rmi_is_logged_and_the_build_still_succeeds(self):
        result = self.run_script("sha256:A sha256:B", RMI_FAIL="1")
        self.assertNotIn("BUILD_FAILED", result.stdout)
        self.assertEqual(self.rmis(result), ["rmi sha256:A"])
        self.assertIn("sha256:A", result.stderr)

    def test_an_unchanged_id_is_not_removed(self):
        result = self.run_script("sha256:A sha256:A")
        self.assertEqual(self.rmis(result), [])


if __name__ == "__main__":
    unittest.main()
