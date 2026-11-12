import os
import shutil
import stat
import subprocess
import tempfile
import unittest

from harness import DevContainerTestCase, PROJECT_PREFIX, REPO_ROOT, project_env_text

NL = chr(10)
IDENTITY = "GIT_USER_NAME=demo-agent" + NL + "GIT_USER_EMAIL=demo-agent@example.com" + NL
HASH_LABEL = "dev-container.build-hash"

# Fake docker in front of the harness stub. It models images, which the
# harness stub does not: a record per compose project under images/, a label
# file beside it, and an event log. `compose down --rmi local` leaves the
# image, as compose does for an image that carries its own tag; only an
# explicit image removal deletes it. A build records the hash it is given as
# `build-hash=<value>` on its command line or in a *BUILD_HASH variable.
IMAGE_STUB = """#!/usr/bin/env bash
state="${DOCKER_STUB_STATE:?}"
img="$state/images"
mkdir -p "$img"
events="$state/image-events.log"
next="${IMAGE_STUB_NEXT:?}"
proj_of() {
  if [[ "$1" =~ ^(demo-[a-z0-9-]+)-app(:.*)?$ ]]; then printf '%s' "${BASH_REMATCH[1]}"; return; fi
  printf '%s' "$1" | grep -oE 'demo-[a-z]+-[0-9]+' | head -1
}
drop() { find "$img" -maxdepth 1 -name "$1" -delete; }
do_build() {
  local p="$1"; shift
  if [[ -f "$state/$p.fail-build" ]]; then
    echo "build failed" >&2
    echo "build-failed $p" >> "$events"
    return 1
  fi
  local extra=""
  [[ " $* " == *" --no-cache "* ]] && extra=" no-cache"
  echo "build $p$extra" >> "$events"
  local h
  h=$(printf '%s' "$*" | grep -oE 'build-hash=[^ ]+' | head -1 | cut -d= -f2)
  [[ -n "$h" ]] || h=$(env | grep -iE 'build_hash=' | head -1 | cut -d= -f2)
  if [[ -f "$state/$p.build-noop" ]]; then [[ -f "$img/$p.present" ]] || : > "$img/$p.present"; return 0; fi
  : > "$img/$p.present"
  if [[ -n "$h" ]]; then printf '%s' "$h" > "$img/$p.label"; else drop "$p.label"; fi
}
first="${1:-}"
sub=""
case "$first" in
  image) sub="${2:-}" ;;
  inspect) sub=inspect ;;
  rmi) sub=rm ;;
  images) sub=ls ;;
esac
case "$first:$sub" in
  compose:*)
    verb="" project="${COMPOSE_PROJECT_NAME:-}" prev=""
    for a in "$@"; do
      [[ "$prev" == -p ]] && project="$a"
      if [[ -z "$verb" ]]; then
        case "$a" in build|up|down) verb="$a" ;; esac
      fi
      prev="$a"
    done
    case "$verb" in
      build) do_build "$project" "$@" || exit 1; exit 0 ;;
      up)
        if [[ " $* " == *" --build "* ]]; then
          do_build "$project" "$@" || exit 1
        fi
        echo "up $project" >> "$events"
        ;;
      down) [[ " $* " == *" --rmi "* ]] && echo "down-rmi $project" >> "$events" ;;
    esac
    ;;
  build:*)
    p=$(proj_of "$*")
    [[ -n "$p" ]] && { do_build "$p" "$@" || exit 1; exit 0; }
    ;;
  *:inspect)
    p=""
    for a in "$@"; do [[ -n "$(proj_of "$a")" ]] && p=$(proj_of "$a"); done
    [[ -n "$p" && -f "$img/$p.present" ]] || { echo "no such image" >&2; exit 1; }
    label=""
    [[ -f "$img/$p.label" ]] && label=$(cat "$img/$p.label")
    if [[ " $* " == *" --format "* || " $* " == *" -f "* ]]; then
      printf '%s\n' "$label"
    else
      printf '[{"Config":{"Labels":{"dev-container.build-hash":"%s"}}}]\n' "$label"
    fi
    exit 0
    ;;
  *:rm)
    for a in "$@"; do
      p=$(proj_of "$a")
      [[ -n "$p" ]] && { drop "$p.present"; drop "$p.label"; echo "rm $p" >> "$events"; }
    done
    exit 0
    ;;
  *:ls)
    want=""
    for a in "$@"; do
      case "$a" in label=dev-container.compose-project=*) want="${a#label=dev-container.compose-project=}" ;; esac
    done
    for f in "$img"/*.present; do
      [[ -f "$f" ]] || continue
      name=$(basename "$f" .present)
      [[ -z "$want" || "$name" == "$want" ]] && echo "$name-app:latest"
    done
    exit 0
    ;;
esac
exec "$next" "$@"
"""



def git(directory, *args):
    subprocess.run(
        ["git", "-C", directory, "-c", "user.email=fixture@example.com", "-c", "user.name=fixture",
         "-c", "commit.gpgsign=false", *args],
        check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )


class ImageHarnessMixin:
    """Puts the image-aware docker in front of the harness stub for every run."""

    def use_images(self):
        h = self.harness
        image_bin = os.path.join(h.tmp, "image-bin")
        os.makedirs(image_bin)
        path = os.path.join(image_bin, "docker")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(IMAGE_STUB)
        os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
        self.extra_env = {
            "PATH": image_bin + os.pathsep + h.stub_bin + os.pathsep + os.environ["PATH"],
            "IMAGE_STUB_NEXT": os.path.join(h.stub_bin, "docker"),
        }

    def engine(self, *args):
        return self.harness.run(*args, port_overrides=self.extra_env)

    def image_dir(self):
        return os.path.join(self.harness.docker_state, "images")

    def plant_image(self, project, label=None):
        os.makedirs(self.image_dir(), exist_ok=True)
        with open(os.path.join(self.image_dir(), project + ".present"), "w", encoding="utf-8"):
            pass
        if label is not None:
            with open(os.path.join(self.image_dir(), project + ".label"), "w", encoding="utf-8") as handle:
                handle.write(label)

    def image_present(self, project):
        return os.path.isfile(os.path.join(self.image_dir(), project + ".present"))

    def image_label(self, project):
        path = os.path.join(self.image_dir(), project + ".label")
        if not os.path.isfile(path):
            return ""
        with open(path, encoding="utf-8") as handle:
            return handle.read()

    def events(self):
        path = os.path.join(self.harness.docker_state, "image-events.log")
        if not os.path.isfile(path):
            return []
        with open(path, encoding="utf-8") as handle:
            return [line.strip() for line in handle if line.strip()]

    def mark(self, project, name, content=""):
        with open(os.path.join(self.harness.docker_state, project + "." + name), "w", encoding="utf-8") as handle:
            handle.write(content)

    def running(self, project):
        return os.path.isfile(os.path.join(self.harness.docker_state, project + ".running"))


class DeleteGuardBase(DevContainerTestCase):
    """`remove_slot_dir <path>` is the only way the engine deletes a slot.

    Sourced with DEV_CONTAINER_LIB=1 the script defines its functions and does
    not dispatch a verb.
    """

    project_env = None

    def setUp(self):
        super().setUp()
        self.harness.build_fixture_repo(project_env=project_env_text(**(self.project_env or {})))
        self.slots = self.harness.slots_dir
        os.makedirs(self.slots)

    def call(self, path):
        h = self.harness
        env = h._env(None)
        env["DEV_CONTAINER_LIB"] = "1"
        body = (
            '. "$0"; '
            'if remove_slot_dir "$1"; then echo "RC=0"; else echo "RC=$?"; fi; '
            "echo REACHED"
        )
        result = subprocess.run(
            ["bash", "-c", body, h.script, path],
            cwd=h.tmp, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60,
        )
        return result.stdout.decode()

    def write_marker(self, slot, compose_dir=".devcontainer"):
        directory = os.path.join(self.harness.slot_dir(slot), compose_dir)
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, ".slot-holder"), "w", encoding="utf-8") as handle:
            handle.write("{}" + NL)

    def drop_git(self, slot):
        shutil.rmtree(os.path.join(self.harness.slot_dir(slot), ".git"))

    def assertRefused(self, output, survivor=None):
        self.assertIn("RC=1", output)
        self.assertIn("REACHED", output)
        reason = [l for l in output.splitlines() if not l.startswith(("RC=", "REACHED"))]
        self.assertTrue(any(l.strip() for l in reason), "a refusal prints its reason: " + output)
        if survivor is not None:
            self.assertTrue(os.path.lexists(survivor), "the path was deleted: " + survivor)

    def assertRemoved(self, output, slot):
        self.assertIn("RC=0", output)
        self.assertFalse(os.path.exists(self.harness.slot_dir(slot)))


class DeleteGuard(DeleteGuardBase):
    def test_a_real_slot_with_the_marker_is_removed(self):
        self.harness.make_bare_slot("slot-1")
        self.write_marker("slot-1")
        self.assertRemoved(self.call(self.harness.slot_dir("slot-1")), "slot-1")

    def test_a_marker_with_no_checkout_beside_it_is_enough(self):
        self.harness.make_bare_slot("slot-1")
        self.write_marker("slot-1")
        self.drop_git("slot-1")
        self.assertRemoved(self.call(self.harness.slot_dir("slot-1")), "slot-1")

    def test_a_checkout_verified_as_ours_needs_no_marker(self):
        self.harness.make_bare_slot("slot-1")
        self.assertRemoved(self.call(self.harness.slot_dir("slot-1")), "slot-1")

    def test_a_checkout_of_another_origin_is_kept_even_with_the_marker(self):
        self.harness.make_bare_slot("slot-1")
        self.write_marker("slot-1")
        git(self.harness.slot_dir("slot-1"), "remote", "set-url", "origin", "https://example.invalid/other.git")
        self.assertRefused(self.call(self.harness.slot_dir("slot-1")), self.harness.slot_dir("slot-1"))

    def test_a_bare_directory_with_no_marker_and_no_checkout_is_kept(self):
        stray = os.path.join(self.slots, "stray")
        os.makedirs(stray)
        self.assertRefused(self.call(stray), stray)

    def test_a_sibling_whose_name_starts_like_the_slots_root_is_refused(self):
        evil = os.path.join(self.harness.tmp, "dev-containers-evil", "x")
        os.makedirs(os.path.join(evil, ".devcontainer"))
        with open(os.path.join(evil, ".devcontainer", ".slot-holder"), "w", encoding="utf-8"):
            pass
        self.assertRefused(self.call(evil), evil)

    def test_an_empty_path_is_refused(self):
        self.assertRefused(self.call(""))

    def test_a_symlink_to_a_slot_directory_is_refused_and_the_target_kept(self):
        self.harness.make_bare_slot("slot-1")
        self.write_marker("slot-1")
        link = os.path.join(self.slots, "link")
        os.symlink(self.harness.slot_dir("slot-1"), link)
        self.assertRefused(self.call(link), link)
        self.assertTrue(os.path.isdir(self.harness.slot_dir("slot-1")))

    def test_a_root_level_path_is_refused(self):
        self.assertRefused(self.call("/"))
        self.assertRefused(self.call("/dev-container-test-absent"))

    def test_a_path_with_a_dotdot_component_is_refused(self):
        self.harness.make_bare_slot("slot-1")
        self.harness.make_bare_slot("slot-2")
        self.write_marker("slot-2")
        path = os.path.join(self.slots, "slot-1", "..", "slot-2")
        self.assertRefused(self.call(path), self.harness.slot_dir("slot-2"))

    def test_a_path_with_a_dot_component_is_refused(self):
        self.harness.make_bare_slot("slot-1")
        self.write_marker("slot-1")
        path = os.path.join(self.slots, ".", "slot-1")
        self.assertRefused(self.call(path), self.harness.slot_dir("slot-1"))

    def test_a_relative_path_is_refused(self):
        self.harness.make_bare_slot("slot-1")
        self.write_marker("slot-1")
        self.assertRefused(self.call("dev-containers/slot-1"), self.harness.slot_dir("slot-1"))

    def test_a_directory_below_a_slot_and_the_slots_root_itself_are_refused(self):
        self.harness.make_bare_slot("slot-1")
        inner = os.path.join(self.harness.slot_dir("slot-1"), "docs")
        os.makedirs(inner, exist_ok=True)
        self.assertRefused(self.call(inner), inner)
        self.assertRefused(self.call(self.slots), self.slots)

    def test_a_file_is_refused(self):
        path = os.path.join(self.slots, "afile")
        with open(path, "w", encoding="utf-8"):
            pass
        self.assertRefused(self.call(path), path)

    def test_release_does_not_delete_a_directory_that_is_neither_marked_nor_ours(self):
        self.harness.make_bare_slot("slot-1")
        git(self.harness.slot_dir("slot-1"), "remote", "set-url", "origin", "https://example.invalid/other.git")
        self.harness.run("release", "slot-1", "--for", "nobody")
        self.assertTrue(os.path.isdir(self.harness.slot_dir("slot-1")))


class DeleteGuardComposeDir(DeleteGuardBase):
    project_env = {"COMPOSE_DIR": "compose"}

    def setUp(self):
        super().setUp()
        git(self.harness.fixture_repo, "mv", ".devcontainer", "compose")
        git(self.harness.fixture_repo, "commit", "-q", "-m", "move")

    def test_the_marker_is_read_relative_to_the_projects_compose_directory(self):
        self.harness.make_bare_slot("slot-1")
        self.harness.make_bare_slot("slot-2")
        for slot in ("slot-1", "slot-2"):
            self.drop_git(slot)
        self.write_marker("slot-1", "compose")
        self.write_marker("slot-2", ".devcontainer")

        self.assertRemoved(self.call(self.harness.slot_dir("slot-1")), "slot-1")
        self.assertRefused(self.call(self.harness.slot_dir("slot-2")), self.harness.slot_dir("slot-2"))


class SlotImages(ImageHarnessMixin, DevContainerTestCase):
    def setUp(self):
        super().setUp()
        self.harness.build_fixture_repo(with_parent_env=IDENTITY)
        self.use_images()
        self.project = PROJECT_PREFIX + "-slot-1"

    def test_a_leftover_image_is_removed_or_bypassed_before_a_fresh_slot_builds(self):
        self.plant_image(self.project, label="old")
        result = self.engine("acquire", "--for", "test")
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        events = self.events()
        builds = [i for i, e in enumerate(events) if e.startswith("build " + self.project)]
        removed = [i for i, e in enumerate(events) if e == "rm " + self.project]
        self.assertTrue(builds, events)
        fresh = any(" no-cache" in events[i] for i in builds) or (removed and removed[0] < builds[0])
        self.assertTrue(fresh, "the old image fed the build: " + str(events))

    def test_release_leaves_no_image_of_the_slot(self):
        result = self.engine("acquire", "--for", "test")
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertTrue(self.image_present(self.project), "acquire built no image")
        result = self.engine("release", "slot-1", "--for", "test")
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertFalse(self.image_present(self.project))

    def test_release_keeps_the_images_of_a_slot_whose_name_extends_its_own(self):
        result = self.engine("acquire", "--for", "test")
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        longer = self.project + "-x"
        self.plant_image(longer, label="old")
        result = self.engine("release", "slot-1", "--for", "test")
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertFalse(self.image_present(self.project))
        self.assertTrue(self.image_present(longer), "release removed the image of slot-1-x")

    def test_acquire_ignores_the_images_of_a_slot_whose_name_extends_its_own(self):
        self.plant_image(self.project + "-x", label="other")
        result = self.engine("acquire", "--for", "test")
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertTrue(self.image_present(self.project + "-x"))
        self.assertEqual(self.image_label(self.project + "-x"), "other")

    def test_acquire_does_not_start_a_slot_from_an_image_with_a_different_hash(self):
        self.plant_image(self.project, label="stale")
        self.mark(self.project, "build-noop")
        result = self.engine("acquire", "--for", "test")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.running(self.project))
        self.assertFalse(os.path.isfile(self.harness.lease_path("slot-1")))

    def test_acquire_does_not_start_a_slot_from_an_image_with_no_hash_label(self):
        self.plant_image(self.project)
        self.mark(self.project, "build-noop")
        result = self.engine("acquire", "--for", "test")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.running(self.project))
        self.assertFalse(os.path.isfile(self.harness.lease_path("slot-1")))

    def test_acquire_starts_a_slot_whose_build_carries_the_hash(self):
        result = self.engine("acquire", "--for", "test")
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertTrue(self.running(self.project))
        self.assertNotEqual(self.image_label(self.project), "", HASH_LABEL + " was not passed to the build")

    def test_a_failed_build_stops_acquire_with_nothing_started(self):
        self.mark(self.project, "fail-build")
        result = self.engine("acquire", "--for", "test")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.running(self.project))
        self.assertFalse(os.path.isfile(self.harness.lease_path("slot-1")))
        self.assertNotIn("up " + self.project, self.events())


class RestartInstall(ImageHarnessMixin, DevContainerTestCase):
    def setUp(self):
        super().setUp()
        self.harness.build_fixture_repo(project_env=project_env_text())
        self.harness.make_bare_slot(
            "slot-1",
            env_content=IDENTITY,
            ports_content="SSH_PORT=39220" + NL + "NOVNC_PORT=39230" + NL + "VITE_DEV_PORT=39240" + NL,
        )
        self.use_images()
        self.project = PROJECT_PREFIX + "-slot-1"

    def install_calls(self):
        path = os.path.join(self.harness.docker_state, self.project + ".install-calls")
        if not os.path.isfile(path):
            return []
        with open(path, encoding="utf-8") as handle:
            return [l for l in handle if l.strip()]

    def builds(self):
        return [e for e in self.events() if e.startswith("build " + self.project)]

    def test_restart_installs_after_it_builds_a_missing_image(self):
        result = self.engine("restart", "slot-1")
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertTrue(self.builds(), self.events())
        self.assertEqual(len(self.install_calls()), 1)

    def test_restart_installs_after_it_rebuilds_a_stale_image(self):
        self.plant_image(self.project, label="stale")
        result = self.engine("restart", "slot-1")
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertTrue(self.builds(), self.events())
        self.assertEqual(len(self.install_calls()), 1)

    def test_restart_reports_failure_when_the_install_fails(self):
        self.mark(self.project, "install-fail-from", "1")
        result = self.engine("restart", "slot-1")
        self.assertTrue(self.builds(), self.events())
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("install", (result.stderr + result.stdout).decode().lower())


if __name__ == "__main__":
    unittest.main()
