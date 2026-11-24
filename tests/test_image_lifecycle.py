import os
import re
import subprocess
import unittest

from harness import DevContainerTestCase, project_env_text
from test_doctor import IDENTITY, MOUNT, output_of, read, stub_config

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# A shell `docker` that knows three images of one slot: the one this build made,
# a leftover of another service with an old hash, and a leftover that a container
# still uses. Every `image rm` is written to the log.
STALE_SCRIPT = r"""
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

# build_slot_image against a shell `docker` that holds one image tag whose id
# the "build" moves along IDS (space separated, one step per build). Every
# `rmi` is written to the log; BUSY names an id a container still uses and
# RMI_FAIL makes `rmi` fail. Nothing here touches a real docker.
SUPERSEDE_SCRIPT = r"""
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

# The slot image is labelled with slot_build_hash and judged stale by comparing that
# label with a fresh hash. A scratch repository stands in for the main checkout; its
# compose dir is untracked (excluded the way adopt excludes /.devcontainer/), as the
# generated Dockerfile and entrypoint are. `docker` is a stub holding one image whose
# label is whatever was stored last.
HASH_PRELUDE = r"""
set -eu
source "$REPO/lib/images.sh"
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null
repo_root=$(mktemp -d)
COMPOSE_DIR=${COMPOSE_DIR-.devcontainer}
git -C "$repo_root" init -q -b main
git -C "$repo_root" -c user.name=t -c user.email=t@example.com commit -q --allow-empty -m init
echo '/.devcontainer/' >> "$repo_root/.git/info/exclude"
cd="$repo_root/${COMPOSE_DIR:-.}"
mkdir -p "$cd/ctx"
printf 'FROM scratch\n' > "$cd/Dockerfile"
printf '#!/bin/sh\n' > "$cd/entrypoint.sh"
printf 'PORT=1\n' > "$cd/.env"
printf 'TOKEN=a\n' > "$cd/agent.env"
LABEL=$(mktemp)
docker() {
  case "$1 $2" in
    "image ls") echo slot-app:latest ;;
    "image inspect") cat "$LABEL" ;;
  esac
}
fresh() { SLOT_BUILD_HASH_VALUE=""; slot_build_hash; }
build() { fresh > "$LABEL"; }
stale() { if slot_image_is_stale slot "$(fresh)"; then echo stale; else echo current; fi; }
"""


def bash(body, **env):
    return subprocess.run(
        ["bash", "-c", HASH_PRELUDE + body], env={**os.environ, "REPO": REPO_ROOT, **env},
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
    )


def digest(text):
    return subprocess.run(
        ["bash", "-c", 'source "$REPO/lib/images.sh"; compose_dir_digest'],
        input=text, env={**os.environ, "REPO": REPO_ROOT},
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
    ).stdout.strip()


class StaleImages(unittest.TestCase):
    def run_script(self, **extra):
        return subprocess.run(
            ["bash", "-c", STALE_SCRIPT], env={**os.environ, "REPO": REPO_ROOT, **extra},
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


class SupersededImages(unittest.TestCase):
    def run_script(self, ids, **extra):
        return subprocess.run(
            ["bash", "-c", SUPERSEDE_SCRIPT], env={**os.environ, "REPO": REPO_ROOT, "IDS": ids, **extra},
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


class BuildsUseTheCacheUnlessAskedNot(DevContainerTestCase):
    def setUp(self):
        super().setUp()
        h = self.harness
        stub_config(self, {"app": {"build": {"context": "/ctx"}, "volumes": [MOUNT]}})
        h.build_fixture_repo(with_parent_env=IDENTITY, project_env=project_env_text())
        with open(os.path.join(h.main_devcontainer(), "docker-compose.yml"), "w", encoding="utf-8") as handle:
            handle.write("services:\n  app:\n    build: .\n")
        subprocess.run(["git", "-C", h.fixture_repo, "commit", "-q", "-am", "build section"], check=True)
        subprocess.run(["git", "-C", h.fixture_repo, "branch", "-M", "master"], check=True)

    def build_lines(self):
        if not os.path.isfile(self.harness.docker_log):
            return []
        return [
            line for line in read(self.harness.docker_log).splitlines()
            if re.search(r"(^ARGS: |\s)build(\s|$)", line)
        ]

    def acquire(self, *flags):
        before = len(self.build_lines())
        result = self.harness.run("acquire", "--for", "task-27", *flags)
        self.assertEqual(result.returncode, 0, output_of(result))
        return self.harness.acquired_slot_name(result), self.build_lines()[before:]

    def assert_cached(self, lines):
        self.assertTrue(lines, "no image was built")
        self.assertFalse([line for line in lines if "--no-cache" in line], lines)

    def assert_from_scratch(self, lines):
        self.assertTrue(lines, "no image was built")
        self.assertTrue(all("--no-cache" in line for line in lines), lines)

    def test_a_plain_acquire_builds_with_the_cache(self):
        self.assert_cached(self.acquire()[1])

    def test_acquire_refresh_builds_from_scratch(self):
        self.assert_from_scratch(self.acquire("--refresh")[1])

    def test_install_refresh_builds_from_scratch_and_plain_install_does_not(self):
        name, _ = self.acquire()
        before = len(self.build_lines())
        self.assertEqual(self.harness.run("install", name).returncode, 0)
        self.assert_cached(self.build_lines()[before:])
        before = len(self.build_lines())
        result = self.harness.run("install", name, "--refresh")
        self.assertEqual(result.returncode, 0, output_of(result))
        self.assert_from_scratch(self.build_lines()[before:])

    def test_the_flag_is_not_remembered_by_the_next_acquire(self):
        self.acquire("--refresh")
        self.assert_cached(self.acquire()[1])

    def test_restart_refresh_rebuilds_an_image_that_is_not_stale(self):
        name, _ = self.acquire()
        before = len(self.build_lines())
        self.assertEqual(self.harness.run("restart", name).returncode, 0)
        self.assertEqual(self.build_lines()[before:], [])
        result = self.harness.run("restart", name, "--refresh")
        self.assertEqual(result.returncode, 0, output_of(result))
        self.assert_from_scratch(self.build_lines()[before:])

    def test_the_usage_text_names_the_flag(self):
        text = output_of(self.harness.run("acquire", "--help"))
        self.assertIn("--refresh", text)
        for verb in ("install", "restart"):
            self.assertIn("--refresh", output_of(self.harness.run(verb, "--help")))


class ComposeDirDigest(unittest.TestCase):
    LINES = "Dockerfile aaa\nentrypoint.sh bbb\n"

    def test_it_is_one_hex_sha(self):
        self.assertRegex(digest(self.LINES), r"^[0-9a-f]{40,64}$")

    def test_the_same_files_give_the_same_sha(self):
        self.assertEqual(digest(self.LINES), digest(self.LINES))

    def test_a_changed_blob_changes_it(self):
        self.assertNotEqual(digest(self.LINES), digest("Dockerfile aaa\nentrypoint.sh ccc\n"))

    def test_a_renamed_file_changes_it(self):
        self.assertNotEqual(digest(self.LINES), digest("Dockerfile aaa\nstart.sh bbb\n"))

    def test_an_added_file_changes_it(self):
        self.assertNotEqual(digest(self.LINES), digest(self.LINES + "extra.sh ddd\n"))

    def test_no_files_still_give_a_sha(self):
        self.assertRegex(digest(""), r"^[0-9a-f]{40,64}$")


class BuildHash(unittest.TestCase):
    def stale_after(self, change, **env):
        out = bash("build\n" + change + "\nstale\n", **env)
        self.assertEqual(out.returncode, 0, out.stderr)
        return out.stdout.strip()

    def test_nothing_changed_is_current(self):
        self.assertEqual(self.stale_after(":"), "current")

    def test_the_hash_is_hex_for_the_overlay_label(self):
        out = bash("fresh")
        self.assertRegex(out.stdout.strip(), r"^[0-9a-f]+$")

    def test_a_new_commit_is_stale(self):
        change = "git -C $repo_root -c user.name=t -c user.email=t@e commit -q --allow-empty -m two"
        self.assertEqual(self.stale_after(change), "stale")

    def test_an_edited_untracked_entrypoint_is_stale(self):
        self.assertEqual(self.stale_after("echo exec >> $cd/entrypoint.sh"), "stale")

    def test_an_edited_untracked_dockerfile_is_stale(self):
        self.assertEqual(self.stale_after("echo 'RUN true' >> $cd/Dockerfile"), "stale")

    def test_a_file_in_a_subdirectory_counts(self):
        self.assertEqual(self.stale_after("echo x > $cd/ctx/marker.txt"), "stale")

    def test_a_new_file_is_stale(self):
        self.assertEqual(self.stale_after("echo x > $cd/install.sh"), "stale")

    def test_a_deleted_file_is_stale(self):
        self.assertEqual(self.stale_after("rm $cd/entrypoint.sh"), "stale")

    def test_editing_dotenv_is_not(self):
        self.assertEqual(self.stale_after("echo A=1 >> $cd/.env"), "current")

    def test_editing_agent_env_is_not(self):
        self.assertEqual(self.stale_after("echo B=2 >> $cd/agent.env"), "current")

    def test_a_new_dotenv_variant_is_not(self):
        self.assertEqual(self.stale_after("echo A=1 > $cd/.env.local"), "current")

    def test_a_secrets_overlay_is_not(self):
        self.assertEqual(self.stale_after("echo 'x: 1' > $cd/app.secrets.yml; echo 'x: 1' > $cd/app.secrets.yaml"), "current")

    def test_secret_and_credential_files_are_not(self):
        change = "echo k > $cd/credentials.json; echo k > $cd/id_ed25519; echo k > $cd/server.pem; echo k > $cd/db-secret.txt"
        self.assertEqual(self.stale_after(change), "current")

    def test_the_slot_holder_marker_is_not(self):
        self.assertEqual(self.stale_after("echo me > $cd/.slot-holder"), "current")

    def test_a_compose_dir_at_the_root_hashes_its_own_files_only(self):
        env = {"COMPOSE_DIR": "."}
        self.assertEqual(self.stale_after("mkdir -p $cd/src/deep; echo x > $cd/src/deep/a.js", **env), "current")
        self.assertEqual(self.stale_after("echo RUN >> $cd/Dockerfile", **env), "stale")

    def test_a_missing_compose_dir_still_gives_a_hash(self):
        out = bash("rm -rf $cd; fresh")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertRegex(out.stdout.strip(), r"^[0-9a-f]+$")


if __name__ == "__main__":
    unittest.main()
