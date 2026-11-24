import json
import os
import re
import shutil
import subprocess
import time
import unittest

from harness import (
    DOCKER_STUB,
    DOCS_REPO_URL,
    DevContainerHarness,
    DevContainerTestCase,
    ListVerdictTestCase,
    PROJECT_PREFIX,
    REPO_ROOT,
    LIB_REL,
    engine_source_lines,
    SCRIPT_REL,
    DEFAULT_PORT_NAMES,
    OWNER_PORTS,
    TEST_SSH_PORT,
    project_env_text,
    slot_port,
    wait_until,
)

ENV_WITH_IDENTITY = "GIT_USER_NAME=demo-agent\nGIT_USER_EMAIL=demo-agent@example.com\n"
IDENTITY_ENV = ENV_WITH_IDENTITY


class EvictSurvivesARefusedRescuePush(DevContainerTestCase):

    ENV = "GIT_USER_NAME=demo-agent\nGIT_USER_EMAIL=demo-agent@example.com\n"

    def setUp(self):
        super().setUp()
        self.remote = os.path.join(self.harness.tmp, "refusing-remote.git")
        subprocess.run(
            ["git", "init", "--bare", "-q", self.remote],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        hook = os.path.join(self.remote, "hooks", "pre-receive")
        with open(hook, "w", encoding="utf-8") as handle:
            handle.write(
                "#!/usr/bin/env bash\n"
                "while read -r _ _ ref; do\n"
                '  case "$ref" in\n'
                "    *aaa-refused*)\n"
                '      echo "- This ref is protected here." >&2\n'
                "      exit 1\n"
                "      ;;\n"
                "  esac\n"
                "done\n"
                "exit 0\n"
            )
        os.chmod(hook, 0o755)
        self.harness._write_stub("gh", "#!/usr/bin/env bash\nexit 1\n")

    def _held_branch(self, slot, branch, filename):
        with open(os.path.join(slot, filename), "w", encoding="utf-8") as handle:
            handle.write(branch + "\n")
        git = lambda *args: subprocess.run(
            [self.harness.real_git_bin, "-C", slot, "-c", "commit.gpgsign=false", *args],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        git("checkout", "-q", "-b", branch)
        git("add", filename)
        git("commit", "-q", "-m", "held work on " + branch)

    def _point_fixture_at_remote(self):
        subprocess.run(
            ["git", "-C", self.harness.fixture_repo, "remote", "set-url", "origin", self.remote],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )

    def _seed_master(self, slot, git):
        with open(os.path.join(slot, ".gitignore"), "w", encoding="utf-8") as handle:
            handle.write(".devcontainer/.env\n.devcontainer/.slot-holder\ndocs/\n")
        with open(os.path.join(slot, "README.md"), "w", encoding="utf-8") as handle:
            handle.write("slot fixture\n")
        git("add", "README.md", ".gitignore", ".devcontainer/docker-compose.yml")
        git("commit", "-q", "-m", "base")
        git("branch", "-M", "master")
        git("push", "-q", "origin", "master")
        git("fetch", "-q", "origin")

    def test_a_refused_push_costs_one_branch_not_the_whole_eviction(self):
        self.harness.build_fixture_repo()
        self._point_fixture_at_remote()
        self.harness.make_bare_slot("refused", env_content=self.ENV)
        slot = self.harness.slot_dir("refused")
        subprocess.run(
            [self.harness.real_git_bin, "-C", slot, "remote", "set-url", "origin", self.remote],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        subprocess.run(
            [self.harness.real_git_bin, "-C", slot, "config", "--local", "user.email", "fixture@example.com"],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        subprocess.run(
            [self.harness.real_git_bin, "-C", slot, "config", "--local", "user.name", "fixture"],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        git = lambda *args: subprocess.run(
            [self.harness.real_git_bin, "-C", slot, "-c", "commit.gpgsign=false", *args],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self._seed_master(slot, git)

        self._held_branch(slot, "aaa-refused", "refused.txt")
        self._held_branch(slot, "zzz-keeps-going", "kept.txt")

        result = self.harness.run("evict", "refused", "--yes")

        stderr = result.stderr.decode()
        self.assertEqual(result.returncode, 0, stderr)
        self.assertIn("aaa-refused", stderr)
        self.assertIn("the remote refused", stderr)

        rescued = subprocess.run(
            [self.harness.real_git_bin, "-C", self.remote, "for-each-ref", "--format=%(refname:short)", "refs/heads/"],
            stdout=subprocess.PIPE, check=True,
        ).stdout.decode().split()
        self.assertTrue(
            any(ref.endswith("/zzz-keeps-going") for ref in rescued),
            "the branch after the refused one was never pushed: " + str(rescued),
        )
        self.assertFalse(
            any(ref.endswith("/aaa-refused") for ref in rescued),
            "the remote was supposed to refuse this one: " + str(rescued),
        )

        bundles = os.listdir(os.path.join(self.harness.state_dir, "rescue"))
        self.assertTrue(
            any(name.startswith(PROJECT_PREFIX + "-refused") for name in bundles),
            "no bundle was written for a slot whose push was refused: " + str(bundles),
        )
        self.assertFalse(
            os.path.exists(os.path.join(slot, "kept.txt")),
            "the slot was never reset — eviction stopped at the refused push",
        )

    def test_uncommitted_work_survives_a_slot_configured_to_sign_with_the_container_key(self):
        self.harness.build_fixture_repo()
        self._point_fixture_at_remote()
        self.harness.make_bare_slot("signing", env_content=self.ENV)
        slot = self.harness.slot_dir("signing")
        git = lambda *args: subprocess.run(
            [self.harness.real_git_bin, "-C", slot, "-c", "commit.gpgsign=false", *args],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        git("remote", "set-url", "origin", self.remote)
        git("config", "--local", "user.email", "fixture@example.com")
        git("config", "--local", "user.name", "fixture")
        self._seed_master(slot, git)

        git("config", "--local", "gpg.format", "ssh")
        git("config", "--local", "user.signingkey", os.path.join(self.harness.tmp, "no-such-key.pub"))
        git("config", "--local", "commit.gpgsign", "true")

        with open(os.path.join(slot, "unsaved.txt"), "w", encoding="utf-8") as handle:
            handle.write("an hour of work nobody committed\n")

        result = self.harness.run("evict", "signing", "--yes")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertIn(
            "unsaved.txt",
            self.harness.bundle_head_file_list("signing"),
            "the rescue commit never happened and the reset deleted the work it was meant to save",
        )

    def test_a_signature_refusal_stops_the_next_unsigned_branch_being_offered(self):
        self.harness.build_fixture_repo()
        self._point_fixture_at_remote()
        self.harness.make_bare_slot("signatures", env_content=self.ENV)
        slot = self.harness.slot_dir("signatures")
        git = lambda *args: subprocess.run(
            [self.harness.real_git_bin, "-C", slot, "-c", "commit.gpgsign=false", *args],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        git("remote", "set-url", "origin", self.remote)
        git("config", "--local", "user.email", "fixture@example.com")
        git("config", "--local", "user.name", "fixture")
        self._seed_master(slot, git)

        with open(os.path.join(self.remote, "hooks", "pre-receive"), "w", encoding="utf-8") as handle:
            handle.write(
                "#!/usr/bin/env bash\n"
                'echo "- Commits must have verified signatures." >&2\n'
                "exit 1\n"
            )
        os.chmod(os.path.join(self.remote, "hooks", "pre-receive"), 0o755)

        for branch, filename in (("aaa-first", "one.txt"), ("bbb-second", "two.txt"), ("ccc-third", "three.txt")):
            self._held_branch(slot, branch, filename)

        result = self.harness.run("evict", "signatures", "--yes")

        stdout = result.stdout.decode()
        stderr = result.stderr.decode()
        self.assertEqual(result.returncode, 0, stderr)
        self.assertIn("rescue/demo-signatures/", stdout)
        self.assertEqual(
            stdout.count("▸ pushing"), 1,
            "a branch was offered after the remote had already named its rule:\n" + stdout,
        )
        self.assertIn("skipping 'bbb-second'", stdout)
        self.assertIn("skipping 'ccc-third'", stdout)
        self.assertIn("⚠ refused: aaa-first — Commits must have verified signatures.", stderr)
        self.assertNotIn("GH013", stderr)
        logs = [
            name for name in os.listdir(os.path.join(self.harness.state_dir, "rescue"))
            if name.endswith("-push.log")
        ]
        self.assertTrue(logs, "the refused push's own output was kept nowhere")
        with open(os.path.join(self.harness.state_dir, "rescue", logs[0]), encoding="utf-8") as handle:
            self.assertIn("aaa-first", handle.read())

    def test_the_rescue_commit_is_not_judged_by_the_repositorys_own_hooks(self):
        self.harness.build_fixture_repo()
        self._point_fixture_at_remote()
        self.harness.make_bare_slot("hooked", env_content=self.ENV)
        slot = self.harness.slot_dir("hooked")
        git = lambda *args: subprocess.run(
            [self.harness.real_git_bin, "-C", slot, "-c", "commit.gpgsign=false", *args],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        git("remote", "set-url", "origin", self.remote)
        git("config", "--local", "user.email", "fixture@example.com")
        git("config", "--local", "user.name", "fixture")
        self._seed_master(slot, git)

        hooks = os.path.join(slot, ".git", "hooks")
        with open(os.path.join(hooks, "commit-msg"), "w", encoding="utf-8") as handle:
            handle.write(
                "#!/usr/bin/env bash\n"
                'case "$(head -1 "$1")" in\n'
                "  feat*|fix*|chore*|ci*|docs*|test*) exit 0 ;;\n"
                '  *) echo "Commit type not allowed" >&2; exit 1 ;;\n'
                "esac\n"
            )
        with open(os.path.join(hooks, "pre-commit"), "w", encoding="utf-8") as handle:
            handle.write(
                "#!/usr/bin/env bash\n"
                'printf "ran\\n" > "$(git rev-parse --show-toplevel)/../pre-commit-ran"\n'
                "exit 0\n"
            )
        for name in ("commit-msg", "pre-commit"):
            os.chmod(os.path.join(hooks, name), 0o755)

        with open(os.path.join(slot, "unsaved.txt"), "w", encoding="utf-8") as handle:
            handle.write("work the hooks would have judged\n")

        result = self.harness.run("evict", "hooked", "--yes")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        sha = self.harness.bundle_head_sha("hooked")
        message = subprocess.run(
            [self.harness.real_git_bin, "-C", slot, "log", "-1", "--format=%s", sha],
            stdout=subprocess.PIPE, check=True,
        ).stdout.decode()
        self.assertIn(
            "unsaved.txt", self.harness.bundle_head_file_list("hooked"),
            "the rescue commit was refused by the hook again",
        )
        self.assertIn("chore(slot): rescue", message)
        self.assertFalse(
            os.path.exists(os.path.join(self.harness.slots_dir, "pre-commit-ran")),
            "a pre-commit hook ran over a rescue snapshot and could have rewritten it",
        )


class EvictLooksAtTheLease(DevContainerTestCase):

    def _slot_with_work(self, name):
        self.harness.build_fixture_repo()
        self.harness.make_bare_slot(name, env_content="GIT_USER_NAME=demo-agent\nGIT_USER_EMAIL=a@example.com\n")
        slot = self.harness.slot_dir(name)
        git = lambda *args: subprocess.run(
            [self.harness.real_git_bin, "-C", slot, "-c", "commit.gpgsign=false", *args],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        git("config", "--local", "user.email", "fixture@example.com")
        git("config", "--local", "user.name", "fixture")
        remote = os.path.join(self.harness.tmp, name + "-remote.git")
        subprocess.run(
            ["git", "init", "--bare", "-q", remote],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        git("remote", "set-url", "origin", remote)
        subprocess.run(
            ["git", "-C", self.harness.fixture_repo, "remote", "set-url", "origin", remote],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        with open(os.path.join(slot, ".gitignore"), "w", encoding="utf-8") as handle:
            handle.write(".devcontainer/.env\n.devcontainer/.slot-holder\ndocs/\n")
        with open(os.path.join(slot, "README.md"), "w", encoding="utf-8") as handle:
            handle.write("slot fixture\n")
        git("add", "README.md", ".gitignore", ".devcontainer/docker-compose.yml")
        git("commit", "-q", "-m", "base")
        git("branch", "-M", "master")
        git("push", "-q", "origin", "master")
        git("fetch", "-q", "origin")
        with open(os.path.join(slot, "unsaved.txt"), "w", encoding="utf-8") as handle:
            handle.write("unsaved work somebody is mid-way through\n")
        return slot

    def _write_lease(self, name, holder, age_seconds=60, ttl=14400):
        path = self.harness.lease_path(name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "holder": holder,
                    "project": PROJECT_PREFIX + "-" + name,
                    "since": int(time.time()) - age_seconds,
                    "ttl_seconds": ttl,
                    "key": "/dev/null",
                    "ssh_port": 39229,
                },
                handle,
            )
        return path

    def test_a_live_lease_stops_the_eviction_and_the_work_stays(self):
        slot = self._slot_with_work("busy")
        self._write_lease("busy", "task-15")

        result = self.harness.run("evict", "busy", "--yes")

        stderr = result.stderr.decode()
        self.assertNotEqual(result.returncode, 0, result.stdout.decode())
        self.assertIn("refusing to evict", stderr)
        self.assertIn("task-15", stderr)
        self.assertIn("--force", stderr)
        self.assertTrue(
            os.path.exists(os.path.join(slot, "unsaved.txt")),
            "the slot was reset despite a live lease",
        )
        self.assertTrue(os.path.exists(self.harness.lease_path("busy")), "the lease was removed anyway")

    def test_force_evicts_the_same_slot(self):
        slot = self._slot_with_work("forced")
        self._write_lease("forced", "task-15")

        result = self.harness.run("evict", "forced", "--yes", "--force")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertFalse(
            os.path.exists(os.path.join(slot, "unsaved.txt")),
            "--force did not actually evict",
        )
        self.assertIn(
            "unsaved.txt",
            self.harness.bundle_head_file_list("forced"),
            "the work was discarded rather than rescued",
        )

    def test_an_expired_lease_still_evicts_without_force(self):
        slot = self._slot_with_work("stale")
        self._write_lease("stale", "task-16", age_seconds=18000, ttl=14400)

        result = self.harness.run("evict", "stale", "--yes")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertFalse(os.path.exists(os.path.join(slot, "unsaved.txt")))
        self.assertFalse(os.path.exists(self.harness.lease_path("stale")))


class EvictSignsTheRescueCommitWhereTheKeyIs(DevContainerTestCase):

    def _slot(self, name):
        self.harness.build_fixture_repo()
        self.harness.make_bare_slot(name, env_content="GIT_USER_NAME=demo-agent\nGIT_USER_EMAIL=a@example.com\n")
        slot = self.harness.slot_dir(name)
        compose_file = os.path.join(self.harness.main_devcontainer(), "docker-compose.yml")
        with open(compose_file, "a", encoding="utf-8") as handle:
            handle.write("    volumes:\n      - ..:/workspace:cached\n")
        git = lambda *args: subprocess.run(
            [self.harness.real_git_bin, "-C", slot, "-c", "commit.gpgsign=false", *args],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        git("config", "--local", "user.email", "fixture@example.com")
        git("config", "--local", "user.name", "fixture")
        remote = os.path.join(self.harness.tmp, name + "-remote.git")
        subprocess.run(
            ["git", "init", "--bare", "-q", remote],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        git("remote", "set-url", "origin", remote)
        subprocess.run(
            ["git", "-C", self.harness.fixture_repo, "remote", "set-url", "origin", remote],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        with open(os.path.join(slot, ".gitignore"), "w", encoding="utf-8") as handle:
            handle.write(".devcontainer/.env\n.devcontainer/.slot-holder\ndocs/\n")
        with open(os.path.join(slot, "README.md"), "w", encoding="utf-8") as handle:
            handle.write("slot fixture\n")
        git("add", "README.md", ".gitignore", ".devcontainer/docker-compose.yml")
        git("commit", "-q", "-m", "base")
        git("branch", "-M", "master")
        git("push", "-q", "origin", "master")
        git("fetch", "-q", "origin")
        return slot, remote

    def test_the_commit_is_made_through_the_container_when_it_is_running(self):
        slot, _remote = self._slot("signed")
        self.harness.mark_running("signed")
        with open(os.path.join(slot, "unsaved.txt"), "w", encoding="utf-8") as handle:
            handle.write("an hour of work nobody committed\n")

        result = self.harness.run("evict", "signed", "--yes")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        with open(self.harness.docker_log, encoding="utf-8") as handle:
            log = handle.read()
        exec_lines = [
            line for line in log.splitlines()
            if " exec " in line and "-u node" in line and "git commit" in line
        ]
        self.assertTrue(
            exec_lines,
            "the rescue commit was never made through the container as node:\n" + log,
        )
        self.assertNotIn("commit.gpgsign=false", exec_lines[-1], exec_lines[-1])

    def test_bundle_only_when_the_container_is_not_running(self):
        slot, remote = self._slot("unsigned")
        with open(os.path.join(slot, "unsaved.txt"), "w", encoding="utf-8") as handle:
            handle.write("an hour of work nobody committed\n")

        result = self.harness.run("evict", "unsigned", "--yes")

        stdout = result.stdout.decode()
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertIn(
            "bundle only (container not running, so the commit is unsigned)", stdout
        )
        self.assertNotIn("▸ pushing refs/heads/rescue", stdout, "an unsigned commit was offered to the remote")

        rescued = subprocess.run(
            [self.harness.real_git_bin, "-C", remote, "for-each-ref", "--format=%(refname:short)", "refs/heads/"],
            stdout=subprocess.PIPE, check=True,
        ).stdout.decode().split()
        self.assertFalse(
            [ref for ref in rescued if ref.startswith("rescue/")],
            "the unsigned WIP commit reached the remote anyway: " + str(rescued),
        )
        self.assertIn(
            "unsaved.txt",
            self.harness.bundle_head_file_list("unsigned"),
            "the unsigned commit is not even in the bundle",
        )


class EvictTwiceInOneDayWritesDistinctBundles(DevContainerTestCase):

    def _slot(self, name):
        self.harness.build_fixture_repo()
        self.harness.make_bare_slot(name, env_content="GIT_USER_NAME=demo-agent\nGIT_USER_EMAIL=a@example.com\n")
        slot = self.harness.slot_dir(name)
        git = lambda *args: subprocess.run(
            [self.harness.real_git_bin, "-C", slot, "-c", "commit.gpgsign=false", *args],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        git("config", "--local", "user.email", "fixture@example.com")
        git("config", "--local", "user.name", "fixture")
        remote = os.path.join(self.harness.tmp, name + "-remote.git")
        subprocess.run(
            ["git", "init", "--bare", "-q", remote],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        git("remote", "set-url", "origin", remote)
        subprocess.run(
            ["git", "-C", self.harness.fixture_repo, "remote", "set-url", "origin", remote],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        with open(os.path.join(slot, ".gitignore"), "w", encoding="utf-8") as handle:
            handle.write(".devcontainer/.env\n.devcontainer/.slot-holder\ndocs/\n")
        with open(os.path.join(slot, "README.md"), "w", encoding="utf-8") as handle:
            handle.write("slot fixture\n")
        git("add", "README.md", ".gitignore")
        git("commit", "-q", "-m", "base")
        git("branch", "-M", "master")
        git("push", "-q", "origin", "master")
        git("fetch", "-q", "origin")
        return slot

    def test_two_evicts_leave_two_bundle_files(self):
        slot = self._slot("twice")
        with open(os.path.join(slot, "first.txt"), "w", encoding="utf-8") as handle:
            handle.write("first evict's own held work\n")
        result = self.harness.run("evict", "twice", "--yes")
        self.assertEqual(result.returncode, 0, result.stderr.decode())

        with open(os.path.join(slot, "second.txt"), "w", encoding="utf-8") as handle:
            handle.write("second evict's own held work, minutes later\n")
        result = self.harness.run("evict", "twice", "--yes")
        self.assertEqual(result.returncode, 0, result.stderr.decode())

        project = f"{PROJECT_PREFIX}-twice"
        bundle_dir = os.path.join(self.harness.state_dir, "rescue")
        bundles = sorted(
            f for f in os.listdir(bundle_dir)
            if f.startswith(project + "-") and f.endswith(".bundle")
        )
        self.assertEqual(
            len(bundles), 2,
            f"two evicts on the same day should leave two distinct bundles, found: {bundles}",
        )
        self.assertNotEqual(bundles[0], bundles[1])


class EvictRefusesADirectoryThatIsNotThisProjectsSlot(DevContainerTestCase):

    def _foreign_slot(self, name):
        self.harness.build_fixture_repo()
        self.harness.make_bare_slot(name, env_content="GIT_USER_NAME=demo-agent\nGIT_USER_EMAIL=a@example.com\n")
        slot = self.harness.slot_dir(name)
        git = lambda *args: subprocess.run(
            [self.harness.real_git_bin, "-C", slot, "-c", "commit.gpgsign=false", *args],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        git("config", "--local", "user.email", "fixture@example.com")
        git("config", "--local", "user.name", "fixture")
        remote = os.path.join(self.harness.tmp, name + "-other-project-remote.git")
        subprocess.run(
            ["git", "init", "--bare", "-q", remote],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        git("remote", "set-url", "origin", remote)
        with open(os.path.join(slot, "README.md"), "w", encoding="utf-8") as handle:
            handle.write("another project's own fixture\n")
        git("add", "README.md")
        git("commit", "-q", "-m", "base")
        git("branch", "-M", "master")
        git("push", "-q", "origin", "master")
        git("fetch", "-q", "origin")
        git("checkout", "-q", "-b", "topic/other-projects-work")
        with open(os.path.join(slot, "unsaved.txt"), "w", encoding="utf-8") as handle:
            handle.write("somebody else's uncommitted work\n")
        return slot, remote

    def test_refuses_before_pushing_resetting_or_cleaning_the_foreign_directory(self):
        slot, remote = self._foreign_slot("shared-name")

        result = self.harness.run("evict", "shared-name", "--yes", "--force")

        stderr = result.stderr.decode()
        self.assertNotEqual(result.returncode, 0, result.stdout.decode())
        self.assertIn(slot, stderr)
        self.assertIn(remote, stderr)

        branches = subprocess.run(
            [self.harness.real_git_bin, "-C", remote, "for-each-ref", "--format=%(refname)", "refs/heads/"],
            stdout=subprocess.PIPE, check=True,
        ).stdout.decode()
        self.assertNotIn(
            "topic/other-projects-work", branches,
            "the foreign checkout's branch was pushed to its own remote",
        )

        self.assertTrue(
            os.path.exists(os.path.join(slot, "unsaved.txt")),
            "the foreign checkout's uncommitted work was discarded",
        )
        current_branch = subprocess.run(
            [self.harness.real_git_bin, "-C", slot, "branch", "--show-current"],
            stdout=subprocess.PIPE, check=True,
        ).stdout.decode().strip()
        self.assertEqual(
            current_branch, "topic/other-projects-work",
            "the foreign checkout was reset off the branch it was on",
        )

    def test_force_does_not_override_the_refusal(self):
        slot, remote = self._foreign_slot("force-does-not-help")

        without_force = self.harness.run("evict", "force-does-not-help", "--yes")
        with_force = self.harness.run("evict", "force-does-not-help", "--yes", "--force")

        self.assertNotEqual(without_force.returncode, 0, without_force.stdout.decode())
        self.assertNotEqual(with_force.returncode, 0, with_force.stdout.decode())
        self.assertIn(slot, with_force.stderr.decode())
        self.assertTrue(
            os.path.exists(os.path.join(slot, "unsaved.txt")),
            "--force pushed the ownership check aside and evicted the foreign slot anyway",
        )


class DocsRepoRefusesCommitsAndPushes(DevContainerTestCase):

    ENV_WITH_IDENTITY = "GIT_USER_NAME=demo-agent\nGIT_USER_EMAIL=demo-agent@example.com\n"
    REFUSAL = "docs/repo is read-only"
    PUSHURL = "read-only://docs-repo-is-read-only--clone-docs-fix"

    def setUp(self):
        super().setUp()
        self.harness._write_stub("gh", "#!/usr/bin/env bash\necho '[]'\n")

    def _acquire(self, holder="docs-guard"):
        self.harness.build_fixture_repo(with_parent_env=self.ENV_WITH_IDENTITY)
        result = self.harness.run_acquire(holder=holder)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        name = self.harness.acquired_slot_name(result)
        slot = self.harness.slot_dir(name)
        docs = os.path.join(slot, "docs", "repo")
        self.assertTrue(os.path.isdir(os.path.join(docs, ".git")), "no real docs/repo clone — fixture is broken")
        return name, slot, docs

    def _docs_git(self, docs, *args):
        return subprocess.run(
            [
                self.harness.real_git_bin, "-C", docs,
                "-c", "commit.gpgsign=false",
                "-c", "user.email=fixture@example.com",
                "-c", "user.name=fixture",
                *args,
            ],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )

    def _stub_branches(self):
        return subprocess.run(
            [self.harness.real_git_bin, "-C", self.harness.docs_repo_stub,
             "for-each-ref", "--format=%(refname:short)", "refs/heads/"],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        ).stdout.decode().split()

    def test_commit_is_refused_with_where_a_docs_fix_goes(self):
        name, slot, docs = self._acquire()
        head = self._docs_git(docs, "rev-parse", "HEAD").stdout

        result = self._docs_git(docs, "commit", "--allow-empty", "-m", "x")

        self.assertNotEqual(result.returncode, 0, result.stdout.decode())
        stderr = result.stderr.decode()
        self.assertIn(self.REFUSAL, stderr)
        self.assertIn("docs/fix", stderr)
        self.assertIn("into docs/fix", stderr)
        self.assertEqual(self._docs_git(docs, "rev-parse", "HEAD").stdout, head)

    def test_commit_is_still_refused_where_the_container_sees_the_clone(self):
        name, slot, docs = self._acquire()
        hooks_path = self._docs_git(docs, "config", "--local", "--get", "core.hooksPath").stdout.decode().strip()
        self.assertFalse(os.path.isabs(hooks_path), hooks_path)

        elsewhere = os.path.join(self.harness.tmp, "container-view", "docs-repo")
        os.makedirs(os.path.dirname(elsewhere))
        os.rename(docs, elsewhere)

        result = self._docs_git(elsewhere, "commit", "--allow-empty", "-m", "x")

        self.assertNotEqual(result.returncode, 0, result.stdout.decode())
        self.assertIn(self.REFUSAL, result.stderr.decode())

    def test_push_is_refused_by_the_hook_and_without_it_by_the_pushurl(self):
        name, slot, docs = self._acquire()
        subprocess.run(
            [self.harness.real_git_bin, "-C", self.harness.docs_repo_stub,
             "config", "receive.denyCurrentBranch", "updateInstead"],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        before = self._stub_branches()

        hooked = self._docs_git(docs, "push", self.harness.docs_repo_stub, "HEAD:refs/heads/docs/topic-branch")
        self.assertNotEqual(hooked.returncode, 0)
        self.assertIn(self.REFUSAL, hooked.stderr.decode())

        bypassed = self._docs_git(docs, "push", "--no-verify", "origin", "HEAD:refs/heads/docs/topic-branch")
        self.assertNotEqual(bypassed.returncode, 0)
        self.assertIn("read-only", bypassed.stderr.decode())

        self.assertEqual(self._stub_branches(), before)
        self.assertEqual(
            self._docs_git(docs, "config", "remote.origin.pushurl").stdout.decode().strip(),
            self.PUSHURL,
        )

    def test_fetch_and_a_detached_checkout_of_a_new_pin_still_work(self):
        name, slot, docs = self._acquire()
        stub = self.harness.docs_repo_stub
        subprocess.run(
            [self.harness.real_git_bin, "-C", stub,
             "-c", "user.name=fixture", "-c", "user.email=fixture@example.com",
             "commit", "-q", "--allow-empty", "-m", "a newer pin"],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        pin = subprocess.run(
            [self.harness.real_git_bin, "-C", stub, "rev-parse", "HEAD"],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        ).stdout.decode().strip()

        fetched = self._docs_git(docs, "fetch", "--quiet", "--depth", "1", "origin", pin)
        self.assertEqual(fetched.returncode, 0, fetched.stderr.decode())
        checked_out = self._docs_git(docs, "checkout", "--quiet", "--detach", pin)
        self.assertEqual(checked_out.returncode, 0, checked_out.stderr.decode())
        self.assertEqual(self._docs_git(docs, "rev-parse", "HEAD").stdout.decode().strip(), pin)


class ListStaysCheapAndNeverSilent(ListVerdictTestCase):

    def _gh_calls(self):
        if not os.path.isfile(self._gh_log):
            return ""
        with open(self._gh_log, encoding="utf-8") as handle:
            return handle.read()

    def _undelivered_branch(self, slot, branch="wip"):
        git = self._git(slot)
        git("branch", branch)
        git("checkout", "-q", branch)
        with open(os.path.join(slot, "notes.md"), "w", encoding="utf-8") as handle:
            handle.write("never pushed anywhere\n")
        git("add", "notes.md")
        git("commit", "-q", "-m", "wip")
        git("checkout", "-q", "--detach", "origin/master")


    def test_fast_says_in_its_own_header_what_it_did_not_check(self):
        self._ready_slot("quick")

        fast = self.harness.run("list", "--fast")

        self.assertEqual(fast.returncode, 0, fast.stderr.decode())
        header = fast.stdout.decode().splitlines()[0]
        self.assertIn("--fast", header)
        self.assertIn("no delivery check", header)


    def test_a_listing_with_nothing_to_list_is_never_zero_bytes(self):
        for args in (("list",), ("list", "--brief"), ("list", "--fast")):
            with self.subTest(args=args):
                result = self.harness.run(*args)

                stdout = result.stdout.decode()
                self.assertEqual(result.returncode, 0, result.stderr.decode())
                self.assertNotEqual(stdout.strip(), "", "zero bytes and exit 0")
                self.assertTrue(stdout.startswith("SLOT "), stdout)
                self.assertIn("nothing to list", stdout)


class ReapFastForwardsTheMainCheckout(ListVerdictTestCase):

    def setUp(self):
        super().setUp()
        self.remote = self._shared_remote()
        self.main = self.harness.fixture_repo
        git = self._git(self.main)
        git("branch", "-M", "master")
        git("push", "-q", "origin", "master")
        git("fetch", "-q", "origin")
        self._ready_slot("free")

    def _head(self):
        return self._git(self.main)("rev-parse", "HEAD").stdout.decode().strip()

    def _advance_origin(self, name="upstream"):
        other = os.path.join(self.harness.tmp, "other-" + name)
        subprocess.run(
            [self.harness.real_git_bin, "clone", "-q", self.remote, other],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        git = self._git(other)
        with open(os.path.join(other, name + ".txt"), "w", encoding="utf-8") as handle:
            handle.write(name + "\n")
        git("add", "-A")
        git("commit", "-q", "-m", name)
        git("push", "-q", "origin", "master")
        return git("rev-parse", "HEAD").stdout.decode().strip()

    def _main_lines(self, result):
        out = result.stdout.decode() + result.stderr.decode()
        return [line for line in out.splitlines() if "main checkout" in line]

    def _assert_reap_ran_on(self, result):
        self.assertEqual(result.returncode, 0, result.stderr.decode())

    def test_1_behind_origin_it_fast_forwards_and_logs_one_line(self):
        old = self._head()
        new = self._advance_origin()

        result = self.harness.run("reap")

        self._assert_reap_ran_on(result)
        self.assertEqual(self._head(), new)
        lines = self._main_lines(result)
        self.assertEqual(len(lines), 1, lines)
        self.assertIn(f"{old[:7]}..{new[:7]}", lines[0])

    def test_2_a_dirty_checkout_is_refused_and_reap_still_runs(self):
        old = self._head()
        self._advance_origin()
        with open(os.path.join(self.main, ".gitignore"), "a", encoding="utf-8") as handle:
            handle.write("uncommitted\n")

        result = self.harness.run("reap")

        self._assert_reap_ran_on(result)
        self.assertEqual(self._head(), old)
        lines = self._main_lines(result)
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("uncommitted changes", lines[0])

    def test_3_untracked_files_alone_do_not_block_it(self):
        new = self._advance_origin()
        with open(os.path.join(self.main, "scratch.txt"), "w", encoding="utf-8") as handle:
            handle.write("untracked\n")

        result = self.harness.run("reap")

        self._assert_reap_ran_on(result)
        self.assertEqual(self._head(), new)

    def test_4_on_another_branch_it_is_refused(self):
        self._git(self.main)("checkout", "-q", "-b", "elsewhere")
        old = self._head()
        self._advance_origin()

        result = self.harness.run("reap")

        self._assert_reap_ran_on(result)
        self.assertEqual(self._head(), old)
        lines = self._main_lines(result)
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("'elsewhere', not master", lines[0])

    def test_4b_on_a_detached_head_it_is_refused(self):
        self._git(self.main)("checkout", "-q", "--detach", "HEAD")
        old = self._head()
        self._advance_origin()

        result = self.harness.run("reap")

        self._assert_reap_ran_on(result)
        self.assertEqual(self._head(), old)
        lines = self._main_lines(result)
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("detached HEAD", lines[0])
        self.assertTrue(lines[0].startswith("⚠"), lines[0])

    def test_5_diverged_from_origin_it_is_refused(self):
        self._advance_origin()
        git = self._git(self.main)
        with open(os.path.join(self.main, "local.txt"), "w", encoding="utf-8") as handle:
            handle.write("local only\n")
        git("add", "-A")
        git("commit", "-q", "-m", "local only")
        old = self._head()

        result = self.harness.run("reap")

        self._assert_reap_ran_on(result)
        self.assertEqual(self._head(), old)
        lines = self._main_lines(result)
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("diverged", lines[0])

    def test_6_up_to_date_it_says_nothing(self):
        old = self._head()

        result = self.harness.run("reap")

        self._assert_reap_ran_on(result)
        self.assertEqual(self._head(), old)
        self.assertEqual(self._main_lines(result), [])

    def test_7_a_failed_fetch_is_refused_and_reap_still_runs(self):
        old = self._head()
        os.rename(self.remote, self.remote + ".gone")
        self.addCleanup(os.rename, self.remote + ".gone", self.remote)

        result = self.harness.run("reap")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(self._head(), old)
        lines = self._main_lines(result)
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("fetch from origin failed", lines[0])


class DefaultBranchComesFromProjectEnv(ListVerdictTestCase):

    def setUp(self):
        super().setUp()
        self.remote = self._shared_remote()
        self.main = self.harness.fixture_repo
        with open(os.path.join(self.main, "project.env"), "w", encoding="utf-8") as handle:
            handle.write(project_env_text(DEFAULT_BRANCH="trunk"))
        git = self._git(self.main)
        git("add", "project.env")
        git("commit", "-q", "-m", "connect")
        git("branch", "-M", "trunk")
        git("push", "-q", "origin", "trunk")
        git("push", "-q", "origin", "trunk:master")
        git("fetch", "-q", "origin")

    def test_reap_fast_forwards_a_main_checkout_that_is_on_the_configured_branch(self):
        self._ready_slot("free")
        other = os.path.join(self.harness.tmp, "other")
        subprocess.run(
            [self.harness.real_git_bin, "clone", "-q", self.remote, other],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        git = self._git(other)
        with open(os.path.join(other, "upstream.txt"), "w", encoding="utf-8") as handle:
            handle.write("upstream\n")
        git("add", "-A")
        git("commit", "-q", "-m", "upstream")
        git("push", "-q", "origin", "HEAD:trunk")
        new = git("rev-parse", "HEAD").stdout.decode().strip()

        result = self.harness.run("reap")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(self._git(self.main)("rev-parse", "HEAD").stdout.decode().strip(), new)

    def test_an_unproven_branch_is_measured_against_the_configured_branch(self):
        slot = self._ready_slot("held")
        git = self._git(slot)
        git("checkout", "-q", "-b", "wip")
        with open(os.path.join(slot, "wip.txt"), "w", encoding="utf-8") as handle:
            handle.write("wip\n")
        git("add", "wip.txt")
        git("commit", "-q", "-m", "wip")
        self._write_lease("held", holder="h")

        # Only a forced release (evict) still warns by git cherry.
        result = self.harness.run("evict", "held", "--yes", "--force")

        stderr = result.stderr.decode()
        self.assertIn("wip: not proved to be in origin/trunk", stderr)
        self.assertNotIn("origin/master", stderr)

    def test_reap_keeps_a_delivered_configured_branch_and_prunes_one_named_after_the_old_default(self):
        slot = self._ready_slot("free")
        git = self._git(slot)
        git("branch", "-f", "trunk", "origin/trunk")
        git("branch", "-f", "master", "origin/trunk")

        result = self.harness.run("reap")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        heads = git("for-each-ref", "--format=%(refname:short)", "refs/heads/").stdout.decode().split()
        self.assertIn("trunk", heads)
        self.assertNotIn("master", heads)


class SlotChecksStatMinimal(ListVerdictTestCase):

    def _check_stat(self, repo):
        return subprocess.run(
            [self.harness.real_git_bin, "--git-dir", os.path.join(repo, ".git"),
             "config", "--local", "core.checkStat"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        ).stdout.decode().strip()

    def _ready_slot_with_docs_clone(self, name, index=0):
        slot = self._ready_slot(name, index=index)
        docs = os.path.join(slot, "docs", "repo")
        shutil.rmtree(docs)
        subprocess.run(
            [self.harness.real_git_bin, "clone", "-q", self.harness.docs_repo_stub, docs],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        return slot

    def test_a_fresh_slot_gets_check_stat_minimal_in_both_clones(self):
        with open(
            os.path.join(self.harness.fixture_repo, ".devcontainer", ".env"), "w", encoding="utf-8"
        ) as handle:
            handle.write(self.ENV)

        result = self.harness.run_acquire()

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        slot = self.harness.slot_dir(self.harness.acquired_slot_name(result))
        self.assertEqual(self._check_stat(slot), "minimal")
        self.assertEqual(self._check_stat(os.path.join(slot, "docs", "repo")), "minimal")


    def test_evict_sets_it_in_both_clones(self):
        self._shared_remote()
        slot = self._ready_slot_with_docs_clone("evictee", index=1)

        result = self.harness.run("evict", "evictee", "--yes")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(self._check_stat(slot), "minimal")
        self.assertEqual(self._check_stat(os.path.join(slot, "docs", "repo")), "minimal")


class ReleaseRemovesTheSlotsEnvAndComposesFromMain(ListVerdictTestCase):
    def setUp(self):
        super().setUp()
        self.slot = self._ready_slot("rel")
        self.harness.mark_running("rel")
        key = os.path.join(self.harness.tmp, "rel-fixture-key")
        for path in (key, key + ".pub"):
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("key" + chr(10))
        lease = self.harness.lease_path("rel")
        os.makedirs(os.path.dirname(lease), exist_ok=True)
        with open(lease, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "holder": "test",
                    "project": PROJECT_PREFIX + "-rel",
                    "since": int(time.time()),
                    "ttl_seconds": 14400,
                    "key": key,
                    "ssh_port": TEST_SSH_PORT,
                },
                handle,
            )

    def test_release_removes_the_slots_env_file_with_the_slot(self):
        self.assertTrue(os.path.isfile(self.harness.env_path("rel")))

        result = self.harness.run("release", "rel", "--for", "test")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertFalse(os.path.isdir(self.slot), "the slot directory survived: " + result.stdout.decode())
        self.assertFalse(os.path.exists(self.harness.env_path("rel")), "release left the slot's env file")

    def test_release_takes_the_project_down_from_the_main_checkout(self):
        result = self.harness.run("release", "rel", "--for", "test")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        calls = [c for c in self.harness.compose_calls() if c["verb"] == "down"]
        self.assertTrue(calls, "release never took the project down")
        for call in calls:
            self.assertEqual(os.path.realpath(call["project_dir"]), os.path.realpath(self.harness.main_devcontainer()))
            self.assertEqual(os.path.realpath(call["workspace"]), os.path.realpath(self.slot))
            self.assertEqual(os.path.realpath(call["env_file"]), os.path.realpath(self.harness.env_path("rel")))


class ReapNeverDeletesADirectoryOutsideSlotsDir(DevContainerTestCase):
    ENV = "GIT_USER_NAME=demo-agent\nGIT_USER_EMAIL=demo-agent@example.com\n"
    OTHER = "other-demo-slot-1"

    LABEL_AWARE_DOCKER = """#!/usr/bin/env bash
if [[ "${1:-}" == ps ]]; then
  want=""
  case "$*" in
    *dev-container.workspace*) want=workspace ;;
    *working_dir*) want=workdir ;;
  esac
  if [[ -n "$want" ]]; then
    for arg in "$@"; do
      case "$arg" in
        label=com.docker.compose.project=*)
          p="${arg#label=com.docker.compose.project=}"
          [[ -f "$DOCKER_STUB_STATE/$p.$want" ]] && cat "$DOCKER_STUB_STATE/$p.$want"
          ;;
      esac
    done
    exit 0
  fi
fi
exec "$(dirname "$0")/docker-real" "$@"
"""

    def setUp(self):
        super().setUp()
        self.harness.build_fixture_repo(with_parent_env=self.ENV)
        self.harness._write_stub("docker-real", DOCKER_STUB)
        self.harness._write_stub("docker", self.LABEL_AWARE_DOCKER)

    def _git(self, *args):
        subprocess.run(
            [self.harness.real_git_bin, *args],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )

    def _foreign_repo(self, name):
        path = os.path.join(self.harness.tmp, name)
        self._git("clone", "-q", self.harness.fixture_repo, path)
        self._git("-C", path, "remote", "set-url", "origin", "/nonexistent/other.git")
        return path

    def _running_project(self, project, workspace=None, working_dir=None):
        state = self.harness.docker_state
        with open(os.path.join(state, f"{project}.running"), "w", encoding="utf-8"):
            pass
        for suffix, value in (("workspace", workspace), ("workdir", working_dir)):
            if value is not None:
                with open(os.path.join(state, f"{project}.{suffix}"), "w", encoding="utf-8") as handle:
                    handle.write(value + "\n")

    def _down_calls(self, project):
        with open(self.harness.docker_log, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
        return [
            line for line in lines
            if line.startswith("ARGS:") and f"-p {project} " in line
            and re.search(r"\bdown\b", line.split("|")[0])
        ]

    def _reap(self):
        result = self.harness.run("reap")
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        return result.stdout.decode()

    def _assert_untouched(self, path, project, out):
        self.assertTrue(os.path.isdir(path), f"reap removed {path}:\n{out}")
        self.assertTrue(os.path.isdir(os.path.join(path, ".git")), f"reap damaged {path}:\n{out}")
        self.assertEqual(self._down_calls(project), [], f"reap took {project} down:\n{out}")

    def test_a_workspace_label_outside_slots_dir_is_not_removed(self):
        other = self._foreign_repo("other-demo-slot-1")
        self._running_project(self.OTHER, workspace=other)

        out = self._reap()

        self._assert_untouched(other, self.OTHER, out)

    def test_a_working_dir_label_outside_slots_dir_is_not_removed(self):
        other = self._foreign_repo("other-demo-slot-1")
        self._running_project(self.OTHER, working_dir=os.path.join(other, ".devcontainer"))

        out = self._reap()

        self._assert_untouched(other, self.OTHER, out)

    def test_the_main_checkout_named_by_a_workspace_label_is_not_removed(self):
        main = self.harness.fixture_repo
        self._running_project(self.OTHER, workspace=main)

        out = self._reap()

        self._assert_untouched(main, self.OTHER, out)

    def test_the_main_checkout_named_by_a_working_dir_label_is_not_removed(self):
        main = self.harness.fixture_repo
        self._running_project(self.OTHER, working_dir=os.path.join(main, ".devcontainer"))

        out = self._reap()

        self._assert_untouched(main, self.OTHER, out)

    def test_a_project_outside_slots_dir_is_not_taken_down_with_its_volumes(self):
        other = self._foreign_repo("other-demo-slot-1")
        self._running_project(self.OTHER, workspace=other)

        out = self._reap()

        downs = self._down_calls(self.OTHER)
        self.assertEqual(
            [line for line in downs if re.search(r"(^|\s)(-v|--volumes)\b", line.split("|")[0])],
            [], f"reap ran down -v on another project:\n{out}",
        )
        self.assertNotIn("reaping orphan compose project", out)

    def test_an_orphan_slot_under_slots_dir_with_a_foreign_origin_is_kept(self):
        result = self.harness.run_acquire(holder="first")
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        name = self.harness.acquired_slot_name(result)
        project = f"{PROJECT_PREFIX}-{name}"
        slot = self.harness.slot_dir(name)
        self.harness.free_slot(name)
        self._git("-C", slot, "remote", "set-url", "origin", "/nonexistent/other.git")
        with open(os.path.join(self.harness.docker_state, f"{project}.workspace"), "w", encoding="utf-8") as handle:
            handle.write(slot + "\n")

        result = self.harness.run("reap")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertIn("refusing to remove", result.stderr.decode())
        self.assertTrue(os.path.isdir(slot), "reap removed a directory that is not this project's checkout")
        self.assertEqual(self._down_calls(project), [], "reap took the project down although it kept the directory")


if __name__ == "__main__":
    unittest.main()
