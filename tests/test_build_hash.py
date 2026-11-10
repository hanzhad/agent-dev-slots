import os
import subprocess
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The slot image is labelled with slot_build_hash and judged stale by comparing that
# label with a fresh hash. A scratch repository stands in for the main checkout; its
# compose dir is untracked (excluded the way adopt excludes /.devcontainer/), as the
# generated Dockerfile and entrypoint are. `docker` is a stub holding one image whose
# label is whatever was stored last.
PRELUDE = r"""
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
        ["bash", "-c", PRELUDE + body], env={**os.environ, "REPO": REPO_ROOT, **env},
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
    )


def digest(text):
    return subprocess.run(
        ["bash", "-c", 'source "$REPO/lib/images.sh"; compose_dir_digest'],
        input=text, env={**os.environ, "REPO": REPO_ROOT},
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
    ).stdout.strip()


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
