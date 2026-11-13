import json
import os
import shutil
import subprocess
import tempfile
import unittest

from harness import REPO_ROOT, PROJECT_PREFIX, DevContainerTestCase

IDENTITY = "GIT_USER_NAME=identity-agent\nGIT_USER_EMAIL=identity-agent@example.com\n"
SIGNING_LIB = os.path.join(REPO_ROOT, "lib", "signing.sh")
KINDS = ("tokens", "signing", "rescue")


class MigrateCase(DevContainerTestCase):
    def setUp(self):
        super().setUp()
        self.harness.build_fixture_repo(with_parent_env=IDENTITY)
        self.state = self.harness.state_dir
        self.label = PROJECT_PREFIX
        self.key = self.harness.project_key

    def path(self, *parts):
        return os.path.join(self.state, *parts)

    def write(self, rel, text):
        full = self.path(rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as handle:
            handle.write(text)

    def read(self, rel):
        with open(self.path(rel), encoding="utf-8") as handle:
            return handle.read()

    def legacy(self, label=None):
        """The state a label-named project has today, for LABEL."""
        label = label or self.label
        self.write(f"tokens/{label}", f"token-of-{label}")
        self.write(f"tokens/{label}.extra", f"extra-of-{label}")
        self.write(f"signing/{label}/container_signing", f"private-of-{label}")
        self.write(f"signing/{label}/container_signing.pub", f"public-of-{label}")
        self.write(f"rescue/{label}/note.txt", f"rescue-of-{label}")

    def snapshot(self):
        seen = {}
        for kind in KINDS:
            for base, dirs, files in os.walk(self.path(kind)):
                for name in files:
                    full = os.path.join(base, name)
                    with open(full, encoding="utf-8") as handle:
                        seen[os.path.relpath(full, self.state)] = handle.read()
                if not files and not dirs:
                    seen[os.path.relpath(base, self.state) + "/"] = ""
        return seen

    def lease(self, name="slot-1"):
        self.write(
            f"leases/{self.label}-{name}.json",
            json.dumps({"holder": "t", "owner": "-", "project": f"{self.label}-{name}", "since": 1, "key": "/dev/null"}),
        )

    def parked(self, name="slot-1"):
        self.write(
            f"parked/{self.label}-{name}.json",
            json.dumps({"holder": "t", "since": 1, "reason": "uncommitted:1"}),
        )

    def migrate(self, *args):
        result = self.harness.run("migrate-identity", *args)
        return result, (result.stdout + result.stderr).decode()

    def assertRefused(self, result, text, word):
        self.assertNotEqual(result.returncode, 0, text)
        self.assertNotIn("Usage:", text)  # an unknown command prints the usage, which is no refusal
        self.assertIn(word, text.lower())


class MigrateTest(MigrateCase):
    def test_moves_tokens_signing_and_rescue_to_the_key(self):
        self.legacy()

        result, text = self.migrate()

        self.assertEqual(result.returncode, 0, text)
        self.assertEqual(self.read(f"tokens/{self.key}"), f"token-of-{self.label}")
        self.assertEqual(self.read(f"tokens/{self.key}.extra"), f"extra-of-{self.label}")
        self.assertEqual(self.read(f"signing/{self.key}/container_signing"), f"private-of-{self.label}")
        self.assertEqual(self.read(f"signing/{self.key}/container_signing.pub"), f"public-of-{self.label}")
        self.assertEqual(self.read(f"rescue/{self.key}/note.txt"), f"rescue-of-{self.label}")
        self.assertFalse(os.path.exists(self.path("tokens", self.label)))
        self.assertFalse(os.path.exists(self.path("tokens", self.label + ".extra")))
        self.assertFalse(os.path.exists(self.path("signing", self.label)))
        self.assertFalse(os.path.exists(self.path("rescue", self.label)))

    def test_a_project_with_another_label_is_untouched(self):
        self.legacy()
        self.legacy("other")
        before = {k: v for k, v in self.snapshot().items() if "other" in k}

        result, text = self.migrate()

        self.assertEqual(result.returncode, 0, text)
        self.assertEqual({k: v for k, v in self.snapshot().items() if "other" in k}, before)
        self.assertTrue(os.path.isdir(self.path("signing", "other")))
        self.assertTrue(os.path.isdir(self.path("rescue", "other")))

    def test_dry_run_lists_the_moves_and_changes_nothing(self):
        self.legacy()
        before = self.snapshot()

        result, text = self.migrate("--dry-run")

        self.assertEqual(result.returncode, 0, text)
        self.assertEqual(self.snapshot(), before)
        self.assertIn(self.key, text)
        for kind in KINDS:
            self.assertIn(kind, text)

    def test_a_second_run_moves_nothing_and_says_so(self):
        self.legacy()
        first, text = self.migrate()
        self.assertEqual(first.returncode, 0, text)
        after_first = self.snapshot()

        second, text = self.migrate()

        self.assertEqual(second.returncode, 0, text)
        self.assertIn("nothing to migrate", text.lower())
        self.assertEqual(self.snapshot(), after_first)

    def test_a_fresh_project_has_nothing_to_migrate(self):
        result, text = self.migrate()

        self.assertEqual(result.returncode, 0, text)
        self.assertIn("nothing to migrate", text.lower())


class MigrateGuardTest(MigrateCase):
    def test_refuses_while_a_lease_is_live_and_moves_nothing(self):
        self.legacy()
        self.lease()
        before = self.snapshot()

        result, text = self.migrate()

        self.assertRefused(result, text, "lease")
        self.assertEqual(self.snapshot(), before)

    def test_a_live_lease_also_stops_a_dry_run_from_claiming_success(self):
        self.legacy()
        self.lease()

        result, text = self.migrate("--dry-run")

        self.assertRefused(result, text, "lease")

    def test_parked_slots_do_not_block_the_migration(self):
        self.legacy()
        self.parked()

        result, text = self.migrate()

        self.assertEqual(result.returncode, 0, text)
        self.assertTrue(os.path.isdir(self.path("signing", self.key)))
        self.assertFalse(os.path.exists(self.path("signing", self.label)))

    def test_never_overwrites_an_existing_destination(self):
        self.legacy()
        self.write(f"tokens/{self.key}", "already-here")
        before = self.snapshot()

        result, text = self.migrate()

        self.assertRefused(result, text, "exist")
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.read(f"tokens/{self.key}"), "already-here")

    def test_never_overwrites_an_existing_destination_directory(self):
        self.legacy()
        self.write(f"rescue/{self.key}/keep.txt", "already-here")
        before = self.snapshot()

        result, text = self.migrate()

        self.assertRefused(result, text, "exist")
        self.assertEqual(self.snapshot(), before)


class MigrateSpacedStateTest(MigrateCase):
    """A state directory whose path holds spaces must not cut the plan's paths."""

    def setUp(self):
        super().setUp()
        self.state = os.path.join(self.harness.tmp, "state dir with space", "dev-container-state")
        os.makedirs(self.state)
        self.harness.state_dir = self.state

    def test_moves_everything_under_a_path_with_a_space(self):
        self.legacy()

        result, text = self.migrate()

        self.assertEqual(result.returncode, 0, text)
        self.assertEqual(self.read(f"tokens/{self.key}"), f"token-of-{self.label}")
        self.assertEqual(self.read(f"tokens/{self.key}.extra"), f"extra-of-{self.label}")
        self.assertEqual(self.read(f"signing/{self.key}/container_signing"), f"private-of-{self.label}")
        self.assertEqual(self.read(f"rescue/{self.key}/note.txt"), f"rescue-of-{self.label}")
        self.assertFalse(os.path.exists(self.path("tokens", self.label)))
        self.assertFalse(os.path.exists(self.path("signing", self.label)))
        self.assertFalse(os.path.exists(self.path("rescue", self.label)))

    def test_dry_run_prints_the_full_paths_and_changes_nothing(self):
        self.legacy()
        before = self.snapshot()

        result, text = self.migrate("--dry-run")

        self.assertEqual(result.returncode, 0, text)
        self.assertEqual(self.snapshot(), before)
        self.assertIn(f"{self.path('tokens', self.label)} -> {self.path('tokens', self.key)}", text)
        self.assertIn(f"{self.path('signing', self.label)} -> {self.path('signing', self.key)}", text)
        self.assertIn(f"{self.path('rescue', self.label)} -> {self.path('rescue', self.key)}", text)

    def test_still_refuses_an_existing_destination_under_such_a_path(self):
        self.legacy()
        self.write(f"signing/{self.key}/keep", "already-here")
        before = self.snapshot()

        result, text = self.migrate()

        self.assertRefused(result, text, "exist")
        self.assertIn(self.path("signing", self.key), text)
        self.assertEqual(self.snapshot(), before)


class EnsureSigningKeyGuardTest(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="migrate-signing-state-")
        self.addCleanup(shutil.rmtree, self.state, True)
        self.key = "0a1b2c3d"
        self.label = "demo"

    def ensure(self):
        script = ". \"$0\"; ensure_signing_key \"$1\" \"$2\""
        return subprocess.run(
            ["bash", "-c", script, SIGNING_LIB, self.state, self.label],
            env=dict(os.environ, PROJECT_KEY=self.key, PROJECT_LABEL=self.label),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
        )

    def test_refuses_while_the_legacy_directory_exists_and_the_key_one_does_not(self):
        legacy = os.path.join(self.state, "signing", self.label)
        os.makedirs(legacy)
        with open(os.path.join(legacy, "container_signing"), "w") as handle:
            handle.write("private")

        result = self.ensure()

        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertIn("migrate-identity", result.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.state, "signing", self.key)))

    def test_a_fresh_project_with_no_legacy_directory_is_not_refused(self):
        result = self.ensure()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("migrate-identity", result.stderr)

    def test_a_migrated_project_is_not_refused(self):
        os.makedirs(os.path.join(self.state, "signing", self.key))

        result = self.ensure()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("migrate-identity", result.stderr)


if __name__ == "__main__":
    unittest.main()
