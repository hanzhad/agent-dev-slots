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


class GitIdentity(DevContainerTestCase):
    def test_acquire_sets_the_checkouts_local_identity_from_its_own_env(self):
        self.harness.build_fixture_repo(
            with_parent_env="GIT_USER_NAME=demo-agent\nGIT_USER_EMAIL=demo-agent@example.com\n"
        )

        result = self.harness.run_acquire()

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        name = self.harness.acquired_slot_name(result)
        self.assertEqual(self.harness.checkout_identity(name), "demo-agent")
        email = subprocess.run(
            ["git", "-C", self.harness.slot_dir(name), "config", "--local", "user.email"],
            stdout=subprocess.PIPE,
        ).stdout.decode().strip()
        self.assertEqual(email, "demo-agent@example.com")

    def test_acquire_syncs_the_checkout_to_whatever_env_says_even_the_owners_name(self):
        self.harness.build_fixture_repo(
            with_parent_env="GIT_USER_NAME=demo-owner\nGIT_USER_EMAIL=demo-owner@example.com\n"
        )

        result = self.harness.run_acquire()

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        name = self.harness.acquired_slot_name(result)
        self.assertEqual(self.harness.checkout_identity(name), "demo-owner")

    def test_acquire_refuses_before_the_docs_clone_when_env_names_no_git_user_name(self):
        self.harness.build_fixture_repo()

        result = self.harness.run_acquire()

        self.assertNotEqual(result.returncode, 0)
        stderr = result.stderr.decode()
        self.assertIn("GIT_USER_NAME", stderr)
        self.assertIn("which would commit (and push) as the owner", stderr)
        slot_dir = self.harness.slot_dir("slot-1")
        self.assertTrue(os.path.isdir(os.path.join(slot_dir, ".git")))
        self.assertFalse(os.path.exists(os.path.join(slot_dir, "docs")))
        self.assertFalse(os.path.exists(os.path.join(slot_dir, ".local", "archive")))
        self.assertFalse(os.path.isfile(self.harness.lease_path("slot-1")))

    def test_restart_repairs_a_checkout_whose_local_identity_has_drifted(self):
        self.harness.build_fixture_repo()
        self.harness.make_bare_slot(
            "probe",
            env_content="GIT_USER_NAME=demo-agent\nGIT_USER_EMAIL=demo-agent@example.com\n",
            ports_content="NOVNC_PORT=39230\nSSH_PORT=39220\nVITE_DEV_PORT=39240\n",
            local_identity=("demo-owner", "demo-owner@example.com"),
        )
        self.assertEqual(self.harness.checkout_identity("probe"), "demo-owner")

        result = self.harness.run("restart", "probe")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(self.harness.checkout_identity("probe"), "demo-agent")

    def test_restart_repairs_a_checkout_with_no_local_identity_at_all(self):
        self.harness.build_fixture_repo()
        self.harness.make_bare_slot(
            "probe",
            env_content="GIT_USER_NAME=demo-agent\nGIT_USER_EMAIL=demo-agent@example.com\n",
            ports_content="NOVNC_PORT=39230\nSSH_PORT=39220\nVITE_DEV_PORT=39240\n",
        )
        self.assertEqual(self.harness.checkout_identity("probe"), "")

        result = self.harness.run("restart", "probe")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(self.harness.checkout_identity("probe"), "demo-agent")

    def test_restart_refuses_when_env_names_no_git_user_name(self):
        self.harness.build_fixture_repo()
        self.harness.make_bare_slot(
            "probe",
            env_content="GIT_USER_EMAIL=demo-agent@example.com\n",
            ports_content="NOVNC_PORT=39230\nSSH_PORT=39220\nVITE_DEV_PORT=39240\n",
            local_identity=("demo-owner", "demo-owner@example.com"),
        )

        result = self.harness.run("restart", "probe")

        self.assertNotEqual(result.returncode, 0)
        stderr = result.stderr.decode()
        self.assertIn("GIT_USER_NAME", stderr)
        self.assertIn("which would commit (and push) as the owner", stderr)
        self.assertEqual(self.harness.checkout_identity("probe"), "demo-owner")


    @staticmethod
    def _local_config(dir_, key):
        result = subprocess.run(
            ["git", "-C", dir_, "config", "--local", "--get", key],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        return result.stdout.decode().strip()

    def test_list_shows_each_slots_actual_checkout_identity(self):
        self.harness.build_fixture_repo()
        self.harness.make_bare_slot(
            "synced",
            env_content="GIT_USER_NAME=demo-agent\nGIT_USER_EMAIL=demo-agent@example.com\n",
            ports_content="NOVNC_PORT=39230\nSSH_PORT=39220\nVITE_DEV_PORT=39240\n",
            local_identity=("demo-agent", "demo-agent@example.com"),
        )
        self.harness.make_bare_slot(
            "drifted",
            env_content="GIT_USER_NAME=demo-agent\nGIT_USER_EMAIL=demo-agent@example.com\n",
            ports_content="NOVNC_PORT=39231\nSSH_PORT=39221\nVITE_DEV_PORT=39241\n",
            local_identity=("demo-owner", "demo-owner@example.com"),
        )

        result = self.harness.run("list")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        stdout = result.stdout.decode()
        self.assertEqual(self._row(stdout, "synced")[9], "demo-agent")
        self.assertEqual(self._row(stdout, "drifted")[9], "demo-owner")

    def test_list_brief_skips_the_dirty_column(self):
        self.harness.build_fixture_repo()
        self.harness.make_bare_slot(
            "quiet",
            env_content="GIT_USER_NAME=demo-agent\nGIT_USER_EMAIL=demo-agent@example.com\n",
            ports_content="NOVNC_PORT=39232\nSSH_PORT=39222\nVITE_DEV_PORT=39242\n",
            local_identity=("demo-agent", "demo-agent@example.com"),
        )

        full = self.harness.run("list")
        brief = self.harness.run("list", "--brief")

        self.assertEqual(brief.returncode, 0, brief.stderr.decode())
        self.assertNotEqual(self._row(full.stdout.decode(), "quiet")[10], "-")
        self.assertEqual(self._row(brief.stdout.decode(), "quiet")[10], "-")
        self.assertIn("--brief", brief.stdout.decode().splitlines()[0])

    def test_list_rejects_an_unknown_argument(self):
        self.harness.build_fixture_repo()

        result = self.harness.run("list", "--everything")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--everything", result.stderr.decode())

    @staticmethod
    def _row(stdout, slot_name):
        for line in stdout.splitlines():
            fields = line.split()
            if fields and fields[0] == slot_name:
                return fields
        raise AssertionError(f"no '{slot_name}' row in:\n{stdout}")


class SlotSigningIdentity(DevContainerTestCase):

    ENV = "GIT_USER_NAME=demo-agent\nGIT_USER_EMAIL=demo-agent@example.com\n"
    CONTAINER_KEY = "/home/node/.ssh/container_signing.pub"

    def _config(self, name, key):
        result = subprocess.run(
            ["git", "-C", self.harness.slot_dir(name), "config", "--local", key],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        return result.stdout.decode().strip()

    def test_restart_points_the_checkout_at_the_container_signing_key(self):
        self.harness.build_fixture_repo()
        self.harness.make_bare_slot(
            "signed",
            env_content=self.ENV,
            ports_content="NOVNC_PORT=39238\nSSH_PORT=39228\nVITE_DEV_PORT=39248\n",
            local_identity=("demo-agent", "demo-agent@example.com"),
        )

        result = self.harness.run("restart", "signed")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(self._config("signed", "user.signingkey"), self.CONTAINER_KEY)
        self.assertEqual(self._config("signed", "gpg.format"), "ssh")
        self.assertEqual(self._config("signed", "commit.gpgsign"), "true")

    @unittest.skipIf(
        os.path.exists("/home/node/.ssh/container_signing.pub"),
        "the container signing key is present here — a signed commit is expected",
    )
    def test_a_host_side_commit_in_the_slot_fails_instead_of_being_signed(self):
        self.harness.build_fixture_repo()
        self.harness.make_bare_slot(
            "signed",
            env_content=self.ENV,
            ports_content="NOVNC_PORT=39239\nSSH_PORT=39229\nVITE_DEV_PORT=39249\n",
            local_identity=("demo-agent", "demo-agent@example.com"),
        )
        self.harness.run("restart", "signed")

        slot = self.harness.slot_dir("signed")
        with open(os.path.join(slot, "host-authored.txt"), "w", encoding="utf-8") as handle:
            handle.write("written on the host\n")
        subprocess.run(
            [self.harness.real_git_bin, "-C", slot, "add", "host-authored.txt"],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        commit = subprocess.run(
            [self.harness.real_git_bin, "-C", slot, "commit", "-m", "from the host"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )

        self.assertNotEqual(commit.returncode, 0, "a host-side commit was allowed to sign")
        self.assertIn("container_signing", commit.stderr.decode())


class SlotsTreeResolution(DevContainerTestCase):

    def _worktree(self, relative):
        self.harness.build_fixture_repo()
        repo = self.harness.fixture_repo
        path = os.path.join(repo, relative)
        subprocess.run(
            [self.harness.real_git_bin, "-C", repo, "worktree", "add", "-q", "-b", "wt", path],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        script = os.path.join(path, SCRIPT_REL)
        os.makedirs(os.path.dirname(script), exist_ok=True)
        shutil.copyfile(os.path.join(repo, SCRIPT_REL), script)
        shutil.copytree(os.path.join(repo, LIB_REL), os.path.join(path, LIB_REL), dirs_exist_ok=True)
        return script

    def _run_from(self, script, caller_pwd, env_extra=None):
        env = dict(os.environ)
        env["PATH"] = self.harness.stub_bin + os.pathsep + env.get("PATH", "")
        env["DOCKER_STUB_LOG"] = self.harness.docker_log
        env["DOCKER_STUB_STATE"] = self.harness.docker_state
        env["REAL_GIT_BIN"] = self.harness.real_git_bin
        env["DOCS_REPO_STUB_PATH"] = self.harness.docs_repo_stub
        env["DOCS_REPO_URL"] = self.harness.docs_repo_stub
        env["DEV_CONTAINER_HOME"] = self.harness.state_dir
        env["DEV_CONTAINER_PROJECTS_ROOT"] = self.harness.tmp
        env["DEV_CONTAINER_CALLER_PWD"] = caller_pwd
        env.update(env_extra or {})
        return subprocess.run(
            ["bash", script, "list", "--brief"],
            cwd=self.harness.tmp,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=120,
        )

    def test_a_worktree_outside_claude_uses_the_main_checkouts_slots_tree(self):
        script = self._worktree(os.path.join("..", "sibling-worktrees", "wt"))
        self.harness.make_bare_slot(
            "shared",
            env_content="GIT_USER_NAME=demo-agent\nGIT_USER_EMAIL=demo-agent@example.com\n",
            ports_content="NOVNC_PORT=39233\nSSH_PORT=39223\nVITE_DEV_PORT=39243\n",
            local_identity=("demo-agent", "demo-agent@example.com"),
        )

        result = self._run_from(script, os.path.dirname(os.path.dirname(script)))

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertIn("shared", result.stdout.decode())

    def test_a_claude_worktree_as_the_caller_uses_the_main_checkouts_slots_tree(self):
        # The engine no longer lives in the worktree, so there is no refusal to
        # make by name: the directory it is given resolves to the main checkout.
        script = self._worktree(os.path.join(".claude", "worktrees", "some-task"))
        self.harness.make_bare_slot(
            "shared",
            env_content="GIT_USER_NAME=demo-agent\nGIT_USER_EMAIL=demo-agent@example.com\n",
            ports_content="NOVNC_PORT=39234\nSSH_PORT=39224\nVITE_DEV_PORT=39244\n",
            local_identity=("demo-agent", "demo-agent@example.com"),
        )

        result = self._run_from(script, os.path.dirname(os.path.dirname(script)))

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertIn("shared", result.stdout.decode())


class HalfBuiltSlot(DevContainerTestCase):

    ENV = "GIT_USER_NAME=demo-agent\nGIT_USER_EMAIL=demo-agent@example.com\n"
    PORTS = "NOVNC_PORT=39235\nSSH_PORT=39225\nVITE_DEV_PORT=39245\n"


    def test_list_calls_it_unbuilt_rather_than_free(self):
        self.harness.build_fixture_repo()
        self.harness.make_bare_slot(
            "halfway",
            env_content=self.ENV,
            local_identity=("demo-agent", "demo-agent@example.com"),
            built=False,
        )

        result = self.harness.run("list")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(self._row(result.stdout.decode(), "halfway")[2], "unbuilt")

    def test_a_slot_with_no_docs_repo_at_all_is_not_called_docs_drift(self):
        self.harness.build_fixture_repo()
        self.harness.make_bare_slot(
            "halfway",
            env_content=self.ENV,
            local_identity=("demo-agent", "demo-agent@example.com"),
            built=False,
        )

        result = self.harness.run("list")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertNotEqual(self._row(result.stdout.decode(), "halfway")[2], "docs-drift")

    def test_a_slot_that_has_everything_is_not_called_unbuilt(self):
        self.harness.build_fixture_repo()
        self.harness.make_bare_slot(
            "complete",
            env_content=self.ENV,
            ports_content=self.PORTS,
            local_identity=("demo-agent", "demo-agent@example.com"),
        )

        result = self.harness.run("list")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertNotEqual(self._row(result.stdout.decode(), "complete")[2], "unbuilt")

    @staticmethod
    def _row(stdout, slot_name):
        for line in stdout.splitlines():
            fields = line.split()
            if fields and fields[0] == slot_name:
                return fields
        raise AssertionError(f"no '{slot_name}' row in:\n{stdout}")


class LocalIsLinkedNotCopied(DevContainerTestCase):

    ENV = "GIT_USER_NAME=demo-agent\nGIT_USER_EMAIL=demo-agent@example.com\n"

    def _write(self, rel, content="x\n"):
        path = os.path.join(self.harness.fixture_repo, ".local", rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(content)

    def _stub_copying_rsync(self):
        self.rsync_log = os.path.join(self.harness.tmp, "rsync-calls.log")
        self.harness._write_stub(
            "rsync",
            "#!/bin/sh\n"
            'echo "$@" >> "%s"\n' % self.rsync_log
            + 'for a in "$@"; do src="$dst"; dst="$a"; done\n'
            + 'mkdir -p "$dst" && cp -a "$src". "$dst"\n',
        )

    def _acquire(self, **kwargs):
        self.harness.build_fixture_repo(with_parent_env=self.ENV)
        self._stub_copying_rsync()
        self._write("handoff-demo/x")
        self._write("db-backup-x/y")
        self._write("db-snapshots/z.sql.gz")
        self._write("notes/n.md")
        result = self.harness.run_acquire(**kwargs)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        name = self.harness.acquired_slot_name(result)
        return name, os.path.join(self.harness.slot_dir(name), ".local")

    def test_handoff_directory_is_an_absolute_symlink_to_the_main_checkout(self):
        name, slot_local = self._acquire()

        link = os.path.join(slot_local, "handoff-demo")
        self.assertTrue(os.path.islink(link))
        target = os.readlink(link)
        self.assertTrue(os.path.isabs(target), target)
        main = os.path.join(self.harness.fixture_repo, ".local", "handoff-demo")
        self.assertIn(target, (main, os.path.realpath(main)))

    def test_no_db_backup_db_snapshots_or_other_local_content_is_in_the_slot(self):
        name, slot_local = self._acquire()

        self.assertFalse(os.path.lexists(os.path.join(slot_local, "db-backup-x")))
        self.assertFalse(os.path.lexists(os.path.join(slot_local, "db-snapshots")))
        self.assertFalse(os.path.lexists(os.path.join(slot_local, "notes")))

    def test_acquire_does_not_invoke_rsync(self):
        self._acquire()

        self.assertFalse(os.path.exists(self.rsync_log), "acquire called rsync")


class HostGitNeverTouchesALeasedSlotsLocks(DevContainerTestCase):

    ENV = "GIT_USER_NAME=demo-agent\nGIT_USER_EMAIL=demo-agent@example.com\n"
    PORTS = "NOVNC_PORT=39236\nSSH_PORT=39226\nVITE_DEV_PORT=39246\n"

    def _write_lease(self, name, ttl=14400):
        path = self.harness.lease_path(name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "holder": "task-13",
                    "project": PROJECT_PREFIX + "-" + name,
                    "since": int(time.time()),
                    "ttl_seconds": ttl,
                    "key": "/dev/null",
                    "ssh_port": 39226,
                },
                handle,
            )

    def _make_slot(self, name):
        if not os.path.isdir(self.harness.fixture_repo):
            self.harness.build_fixture_repo()
        self.harness.make_bare_slot(
            name,
            env_content=self.ENV,
            ports_content=self.PORTS,
            local_identity=("demo-agent", "demo-agent@example.com"),
        )

    def test_list_does_not_fetch_a_leased_slot_but_still_fetches_a_free_one(self):
        self._make_slot("leased")
        self._write_lease("leased")
        self._make_slot("free")

        result = self.harness.run("list")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        fetch_calls = [c for c in self.harness.git_calls() if " fetch " in f" {c} "]
        leased_dir = self.harness.slot_dir("leased")
        free_dir = self.harness.slot_dir("free")
        self.assertFalse(
            any(leased_dir in c for c in fetch_calls),
            f"list fetched inside the leased slot: {fetch_calls}",
        )
        self.assertTrue(
            any(free_dir in c for c in fetch_calls),
            f"list never fetched the free slot at all — this test proves nothing: {fetch_calls}",
        )

    def test_host_side_status_and_stash_reads_carry_no_optional_locks(self):
        self._make_slot("quiet")

        result = self.harness.run("list")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        slot = self.harness.slot_dir("quiet")
        reads = [
            c for c in self.harness.git_calls()
            if slot in c and (" status " in f" {c} " or " stash " in f" {c} ")
        ]
        self.assertTrue(reads, "list never read this slot's status/stash at all — this test proves nothing")
        for call in reads:
            self.assertTrue(
                call.startswith("GIT_OPTIONAL_LOCKS=0 "),
                f"a host-side read ran without GIT_OPTIONAL_LOCKS=0: {call}",
            )


class AcquireLockScope(DevContainerTestCase):
    ENV_WITH_IDENTITY = "GIT_USER_NAME=demo-agent\nGIT_USER_EMAIL=demo-agent@example.com\n"

    def _reap_on_failure(self, proc):
        def stop():
            for held in os.listdir(self.harness.docker_state):
                if held.endswith(".hold-up"):
                    os.remove(os.path.join(self.harness.docker_state, held))
            if proc.poll() is None:
                proc.kill()
            proc.communicate(timeout=30)
        self.addCleanup(stop)

    def _lease(self, name):
        with open(self.harness.lease_path(name), encoding="utf-8") as handle:
            return json.load(handle)

    def test_wait_outlasts_a_lock_held_past_30s_and_plain_acquire_names_it_busy(self):
        self.harness.build_fixture_repo(with_parent_env=self.ENV_WITH_IDENTITY)
        lock = os.path.join(self.harness.state_dir, "lock")
        os.mkdir(lock)
        self.addCleanup(lambda: os.path.isdir(lock) and os.rmdir(lock))
        self.harness._write_stub(
            "sleep", f'#!/usr/bin/env bash\nexec "{shutil.which("sleep")}" 0.01\n'
        )

        wait_log = os.path.join(self.harness.tmp, "wait-lock.log")
        waiting = self.harness.spawn("acquire", "--for", "waiter", "--wait", log=wait_log)
        self._reap_on_failure(waiting)
        plain = self.harness.spawn("acquire", "--for", "plain")
        self._reap_on_failure(plain)

        _, plain_err = plain.communicate(timeout=90)
        self.assertNotEqual(plain.returncode, 0)
        self.assertIn(lock, plain_err.decode())
        self.assertIn("busy", plain_err.decode())

        def read_wait_log():
            if not os.path.isfile(wait_log):
                return ""
            with open(wait_log, encoding="utf-8") as handle:
                return handle.read()

        wait_until(
            lambda: "still waiting (--wait)" in read_wait_log(),
            timeout=15.0,
            interval=0.02,
            message=lambda: "acquire --wait never logged waiting past 30s:\n" + read_wait_log(),
        )
        self.assertIsNone(waiting.poll(), "acquire --wait gave up on a busy lock")
        os.rmdir(lock)

        self.assertEqual(waiting.wait(timeout=90), 0, read_wait_log())
        self.assertIn("leased to waiter", read_wait_log())

    def test_a_second_acquire_is_not_held_up_by_a_slot_being_created(self):
        self.harness.build_fixture_repo(with_parent_env=self.ENV_WITH_IDENTITY)
        self.harness.hold_compose_up("slot-1")
        first = self.harness.spawn("acquire", "--for", "first")
        self._reap_on_failure(first)
        self.harness.wait_until_compose_up_held("slot-1")

        started = time.monotonic()
        second = self.harness.run_acquire(holder="second")
        elapsed = time.monotonic() - started

        self.assertEqual(second.returncode, 0, second.stderr.decode())
        self.assertLess(elapsed, 10, "the second acquire waited on the first one's creation")
        self.assertNotEqual(self.harness.acquired_slot_name(second), "slot-1")

        self.harness.let_compose_up_go("slot-1")
        out, err = first.communicate(timeout=90)
        self.assertEqual(first.returncode, 0, err.decode())
        self.assertIn("leased to first", out.decode())
        self.assertEqual(self._lease("slot-1")["holder"], "first")

    def test_a_failed_creation_leaves_no_reservation_behind(self):
        self.harness.build_fixture_repo(with_parent_env=self.ENV_WITH_IDENTITY)
        self.harness.hold_compose_up("slot-1", then_fail=True)
        first = self.harness.spawn("acquire", "--for", "first")
        self._reap_on_failure(first)
        self.harness.wait_until_compose_up_held("slot-1")
        self.assertEqual(self._lease("slot-1")["holder"], "first")

        self.harness.let_compose_up_go("slot-1")
        first.communicate(timeout=90)

        self.assertNotEqual(first.returncode, 0)
        self.assertFalse(os.path.exists(self.harness.lease_path("slot-1")))
        self.assertFalse(os.path.isdir(os.path.join(self.harness.state_dir, "lock")))

    def test_a_reservation_whose_acquire_was_killed_is_dropped_by_the_next_acquire(self):
        self.harness.build_fixture_repo(with_parent_env=self.ENV_WITH_IDENTITY)
        self.harness.hold_compose_up("slot-1")
        first = self.harness.spawn("acquire", "--for", "first")
        self._reap_on_failure(first)
        self.harness.wait_until_compose_up_held("slot-1")
        self.assertTrue(os.path.isfile(self.harness.lease_path("slot-1")))

        first.kill()
        self.harness.let_compose_up_go("slot-1")
        first.communicate(timeout=30)

        second = self.harness.run_acquire(holder="second")

        self.assertEqual(second.returncode, 0, second.stderr.decode())
        self.assertFalse(os.path.exists(self.harness.lease_path("slot-1")))
        self.assertIn("reservation", second.stderr.decode())


class AcquireWaitsForAFullPool(DevContainerTestCase):
    ENV_WITH_IDENTITY = "GIT_USER_NAME=demo-agent\nGIT_USER_EMAIL=demo-agent@example.com\n"
    OWNER = "Agent [aaa]"

    def setUp(self):
        super().setUp()
        self.harness.build_fixture_repo(with_parent_env=self.ENV_WITH_IDENTITY)
        with open(os.path.join(self.harness.state_dir, "limits.json"), "w", encoding="utf-8") as handle:
            json.dump({"machine_max": 1}, handle)
        taken = self.harness.run("acquire", "--for", "holder", "--as", self.OWNER)
        self.assertEqual(taken.returncode, 0, taken.stderr.decode())
        self.assertEqual(self.harness.acquired_slot_name(taken), "slot-1")

    def _spawn_waiter(self, holder):
        log = os.path.join(self.harness.tmp, f"{holder}.log")
        proc = self.harness.spawn(
            "acquire", "--for", holder, "--as", self.OWNER, "--wait", log=log
        )

        def stop():
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=30)
        self.addCleanup(stop)
        return proc, log

    @staticmethod
    def _read(log):
        with open(log, encoding="utf-8") as handle:
            return handle.read()

    def _wait_for_text(self, log, text, timeout=90):
        wait_until(
            lambda: os.path.isfile(log) and text in self._read(log),
            timeout=timeout,
            interval=0.02,
            message=lambda: f"never printed {text!r}:\n{self._read(log) if os.path.isfile(log) else ''}",
        )

    def test_plain_acquire_on_a_full_pool_says_how_to_wait_for_one(self):
        result = self.harness.run("acquire", "--for", "late", "--as", self.OWNER)

        self.assertEqual(result.returncode, 2, result.stderr.decode())
        self.assertIn(
            "run 'acquire --wait --for late' (e.g. in the background) to wait for one",
            result.stderr.decode(),
        )

    def test_wait_with_a_timeout_gives_up_when_nothing_frees(self):
        started = time.monotonic()
        result = self.harness.run(
            "acquire", "--for", "late", "--as", self.OWNER, "--wait", "--timeout", "5s"
        )
        elapsed = time.monotonic() - started

        self.assertEqual(result.returncode, 3, result.stderr.decode())
        self.assertIn("no slot became free within 5s", result.stderr.decode())
        self.assertLess(elapsed, 30)
        self.assertFalse(os.path.exists(self.harness.lease_path("slot-2")))

    def test_a_freed_slot_wakes_the_waiters_and_exactly_one_takes_it(self):
        a, a_log = self._spawn_waiter("waiter-a")
        b, b_log = self._spawn_waiter("waiter-b")
        self._wait_for_text(a_log, "1/1")
        self._wait_for_text(b_log, "1/1")
        self.assertIn("limits.json", self._read(a_log))

        released = self.harness.run("release", "slot-1", "--for", "holder")
        self.assertEqual(released.returncode, 0, released.stderr.decode())

        wait_until(
            lambda: a.poll() is not None or b.poll() is not None,
            timeout=20.0,
            interval=0.02,
            message="neither waiter took the freed slot",
        )

        winner, winner_log, loser, loser_log = (
            (a, a_log, b, b_log) if a.poll() is not None else (b, b_log, a, a_log)
        )
        self.assertEqual(winner.returncode, 0, self._read(winner_log))
        holder = "waiter-a" if winner is a else "waiter-b"
        other_holder = "waiter-b" if winner is a else "waiter-a"
        self.assertIn(f"leased to {holder}", self._read(winner_log))
        with open(self.harness.lease_path("slot-1"), encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["holder"], holder)

        self.assertIsNone(loser.poll(), "the second waiter stopped instead of waiting")
        second_release = self.harness.run("release", "slot-1", "--for", holder)
        self.assertEqual(second_release.returncode, 0, second_release.stderr.decode())
        self.assertEqual(loser.wait(timeout=90), 0, self._read(loser_log))
        with open(self.harness.lease_path("slot-1"), encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["holder"], other_holder)


class AcquireAlwaysBuildsAFreshSlot(DevContainerTestCase):
    ENV = "GIT_USER_NAME=demo-agent\nGIT_USER_EMAIL=demo-agent@example.com\n"
    OWNER = "Agent [aaa]"

    def _plant_idle_stamp(self, name):
        stamp_dir = os.path.join(self.harness.state_dir, "idle")
        os.makedirs(stamp_dir, exist_ok=True)
        path = os.path.join(stamp_dir, f"{PROJECT_PREFIX}-{name}")
        open(path, "w", encoding="utf-8").close()
        return path

    def _leave_a_slot_behind(self):
        self.harness.build_fixture_repo(with_parent_env=self.ENV)
        first = self.harness.run_acquire(holder="first")
        self.assertEqual(first.returncode, 0, first.stderr.decode())
        name = self.harness.acquired_slot_name(first)
        self.harness.free_slot(name, stopped=True)
        self._plant_idle_stamp(name)
        return name

    def _stub_ssh_keygen(self):
        log = os.path.join(self.harness.tmp, "ssh-keygen.log")
        real = shutil.which("ssh-keygen")
        self.assertTrue(real, "no real ssh-keygen on PATH")
        self.harness._write_stub(
            "ssh-keygen",
            "#!/usr/bin/env bash\n"
            'if [[ "$1" == "-R" ]]; then\n'
            f'  printf "%s\n" "$*" >> "{log}"\n'
            "  exit 0\n"
            "fi\n"
            f'exec "{real}" "$@"\n',
        )
        return log

    def test_a_stale_directory_is_not_reused_and_a_free_index_is_built(self):
        stale = self._leave_a_slot_behind()

        result = self.harness.run_acquire(holder="second")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        fresh = self.harness.acquired_slot_name(result)
        self.assertNotEqual(fresh, stale, "acquire handed out the stale directory again")
        self.assertEqual(fresh, "slot-2")
        self.assertTrue(os.path.isdir(self.harness.slot_dir(stale)), "the stale directory was touched")
        self.assertFalse(os.path.isfile(self.harness.lease_path(stale)), "the stale directory was leased")

    def test_at_the_cap_a_stale_directory_is_refused_not_reused(self):
        stale = self._leave_a_slot_behind()
        with open(os.path.join(self.harness.state_dir, "limits.json"), "w", encoding="utf-8") as handle:
            json.dump({"machine_max": 1}, handle)
        foreign = os.path.join(self.harness.state_dir, "leases", "alpha-slot-7.json")
        with open(foreign, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "holder": "someone-else",
                    "project": "alpha-slot-7",
                    "since": int(time.time()) - 60,
                    "ttl_seconds": 14400,
                    "key": "/dev/null",
                    "ssh_port": 39229,
                    "owner": "-",
                },
                handle,
            )

        result = self.harness.run("acquire", "--for", "second", "--as", self.OWNER)

        self.assertNotEqual(result.returncode, 0, result.stdout.decode())
        self.assertIn("1/1", result.stderr.decode())
        self.assertFalse(os.path.isfile(self.harness.lease_path(stale)), "the stale directory was leased")
        self.assertFalse(os.path.isdir(self.harness.slot_dir("slot-2")), "a slot was built past the cap")

    def test_slot_is_rejected_as_an_unknown_option(self):
        stale = self._leave_a_slot_behind()

        result = self.harness.run("acquire", "--for", "second", "--slot", stale)

        self.assertNotEqual(result.returncode, 0, result.stdout.decode())
        self.assertIn("--slot", result.stderr.decode())
        self.assertFalse(os.path.isfile(self.harness.lease_path(stale)), "--slot leased the named directory")

    def test_acquire_does_not_touch_known_hosts_inside_the_engine(self):
        log = self._stub_ssh_keygen()
        self.harness.build_fixture_repo(with_parent_env=self.ENV)

        result = self.harness.run_acquire(holder="first")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        calls = open(log, encoding="utf-8").read().splitlines() if os.path.isfile(log) else []
        self.assertEqual(calls, [], "acquire ran ssh-keygen -R in the engine, whose known_hosts is not the caller's")

    def test_a_fresh_slot_prints_the_host_key_step_for_its_ssh_port(self):
        self.harness.build_fixture_repo(with_parent_env=self.ENV)

        result = self.harness.run_acquire(holder="first")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        name = self.harness.acquired_slot_name(result)
        port = self.harness.read_env(name)["SSH_PORT"]
        text = (result.stdout + result.stderr).decode()
        self.assertIn(f"ssh-keygen -R '[localhost]:{port}'", text)

    def test_nothing_creates_the_idle_state_directory(self):
        self.harness.build_fixture_repo(with_parent_env=self.ENV)
        first = self.harness.run_acquire(holder="first")
        self.assertEqual(first.returncode, 0, first.stderr.decode())
        name = self.harness.acquired_slot_name(first)

        for args in (["list"], ["release", name, "--for", "first"], ["reap"], ["list"]):
            result = self.harness.run(*args)
            self.assertEqual(result.returncode, 0, f"{args}: {result.stderr.decode()}")

        self.assertFalse(
            os.path.exists(os.path.join(self.harness.state_dir, "idle")),
            "a verb created $STATE/idle",
        )

    def test_reap_leaves_a_planted_idle_stamp_alone(self):
        self.harness.build_fixture_repo(with_parent_env=self.ENV)
        self.harness.make_bare_slot("slot-1", env_content=self.ENV, local_identity=("demo-agent", "a@example.com"))
        stamp = self._plant_idle_stamp("slot-1")

        result = self.harness.run("reap")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertTrue(os.path.isfile(stamp), "reap removed a stamp it should not know about")

    def test_the_script_has_no_code_naming_the_idle_state_directory(self):
        self.harness.build_fixture_repo()
        code = [line for line in engine_source_lines(self.harness.fixture_repo) if not line.lstrip().startswith("#")]
        offenders = [line.strip() for line in code if "STATE/idle" in line or "idle_stamp" in line]
        self.assertEqual(offenders, [], "code still reads or writes $STATE/idle")

    def test_no_comment_names_the_acquire_slot_flag_or_scan_steps(self):
        self.harness.build_fixture_repo()
        stale = re.compile(
            r"acquire.{0,20}--slot"
            r"|try_acquire_once step"
            r"|by `?--slot`? \(which refuses"
        )
        offenders = [
            f"{number}: {line.strip()}"
            for number, line in enumerate(engine_source_lines(self.harness.fixture_repo), 1)
            if line.lstrip().startswith("#") and stale.search(line)
        ]
        self.assertEqual(offenders, [], "comments describe an acquire --slot / scan step that no longer exists")


class AFreshSlotGetsAFreshImage(DevContainerTestCase):
    ENV = "GIT_USER_NAME=demo-agent\nGIT_USER_EMAIL=demo-agent@example.com\n"

    ORPHAN_AWARE_DOCKER = """#!/usr/bin/env bash
if [[ "${1:-}" == ps && "$*" == *working_dir* ]]; then
  for arg in "$@"; do
    case "$arg" in
      label=com.docker.compose.project=*)
        p="${arg#label=com.docker.compose.project=}"
        [[ -f "$DOCKER_STUB_STATE/$p.workdir" ]] && cat "$DOCKER_STUB_STATE/$p.workdir"
        ;;
    esac
  done
  exit 0
fi
exec "$(dirname "$0")/docker-real" "$@"
"""

    LABELLED_IMAGE_DOCKER = """#!/usr/bin/env bash
if [[ "${1:-}" == image && "${2:-}" == ls ]]; then
  if [[ " $* " == *"dangling=true"* ]]; then
    exit 0
  fi
  echo "$(cat "$DOCKER_STUB_STATE/image.project")-app:latest"
  exit 0
fi
if [[ "${1:-}" == image && "${2:-}" == inspect ]]; then
  cat "$DOCKER_STUB_STATE/image.hash"
  exit 0
fi
exec "$(dirname "$0")/docker-real" "$@"
"""

    def _calls(self, project):
        with open(self.harness.docker_log, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
        return [
            line for line in lines
            if line.startswith("ARGS:")
            and (f"-p {project} " in line or f"COMPOSE_PROJECT_NAME={project} " in line)
        ]

    @staticmethod
    def _is_up(line):
        return re.search(r"\bup\b", line.split("|")[0]) is not None

    @staticmethod
    def _builds(line):
        args = line.split("|")[0]
        return "--build" in args or re.search(r"\bcompose\b.*\bbuild\b", args) is not None

    def _assert_built_before_the_stack_ran(self, project):
        calls = self._calls(project)
        ups = [i for i, line in enumerate(calls) if self._is_up(line)]
        self.assertTrue(ups, "no compose up ran:\n" + "\n".join(calls))
        built = [i for i, line in enumerate(calls) if self._builds(line)]
        ok = bool(built) and built[0] <= ups[0]
        self.assertTrue(ok, "the image was not built before the stack ran:\n" + "\n".join(calls))

    def _assert_image_removed(self, project):
        calls = self._calls(project)
        with open(self.harness.docker_log, encoding="utf-8") as handle:
            everything = handle.read().splitlines()
        down_rmi = [
            line for line in calls
            if re.search(r"\bdown\b", line.split("|")[0])
            and re.search(r"--rmi[ =]local", line.split("|")[0])
        ]
        image_rm = [
            line for line in everything
            if re.search(rf"\b(image rm|rmi)\b.*\b{re.escape(project)}-app\b", line.split("|")[0])
        ]
        self.assertTrue(
            down_rmi or image_rm,
            f"teardown left {project}-app behind:\n" + "\n".join(everything),
        )

    def _acquire(self):
        self.harness.build_fixture_repo(with_parent_env=self.ENV)
        result = self.harness.run_acquire(holder="first")
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        return self.harness.acquired_slot_name(result)

    def test_acquire_builds_the_image_before_the_stack_runs(self):
        name = self._acquire()

        self._assert_built_before_the_stack_ran(self.harness.slot_project_of(name))

    def test_release_removes_the_slots_image_with_its_containers(self):
        name = self._acquire()

        result = self.harness.run("release", name, "--for", "first")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self._assert_image_removed(self.harness.slot_project_of(name))

    def test_down_removes_the_slots_image_with_its_containers(self):
        name = self._acquire()
        self.harness.free_slot(name, stopped=True)

        result = self.harness.run("down", name)

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self._assert_image_removed(self.harness.slot_project_of(name))

    def test_reap_keeps_the_directory_of_an_orphan_with_a_foreign_origin(self):
        name = self._acquire()
        project = self.harness.slot_project_of(name)
        slot = self.harness.slot_dir(name)
        self.harness.free_slot(name)
        subprocess.run(
            [self.harness.real_git_bin, "-C", slot, "remote", "set-url", "origin", "/nonexistent/other.git"],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self.harness._write_stub("docker-real", DOCKER_STUB)
        self.harness._write_stub("docker", self.ORPHAN_AWARE_DOCKER)
        with open(os.path.join(self.harness.docker_state, f"{project}.workdir"), "w", encoding="utf-8") as handle:
            handle.write(os.path.join(slot, ".devcontainer") + "\n")

        result = self.harness.run("reap")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertIn("refusing to remove", result.stderr.decode())
        self.assertTrue(os.path.isdir(slot), "reap removed a directory that is not this project's checkout")

    def test_restart_of_a_leased_slot_does_not_build(self):
        name = self._acquire()
        project = self.harness.slot_project_of(name)
        # the image carries the hash the engine itself computes: the commit plus the compose dir
        head = subprocess.run(
            ["bash", "-c",
             'source "$1/lib/images.sh"; repo_root=$1 COMPOSE_DIR=.devcontainer; slot_build_hash',
             "bash", self.harness.fixture_repo],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        ).stdout.decode().strip()
        for fname, content in (("image.project", project), ("image.hash", head)):
            with open(os.path.join(self.harness.docker_state, fname), "w", encoding="utf-8") as handle:
                handle.write(content + chr(10))
        self.harness._write_stub("docker-real", DOCKER_STUB)
        self.harness._write_stub("docker", self.LABELLED_IMAGE_DOCKER)
        before = len(self._calls(project))

        result = self.harness.run("restart", name)

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        after = self._calls(project)[before:]
        self.assertTrue(any(self._is_up(line) for line in after), "restart never ran compose up")
        self.assertEqual([line for line in after if self._builds(line)], [], "restart built an image")


class SlotMountDirs(DevContainerTestCase):
    DIRS = ".local/shared mounts/data"

    def _acquire(self):
        self.harness.build_fixture_repo(
            with_parent_env=IDENTITY_ENV,
            project_env=project_env_text(SLOT_MOUNT_DIRS=self.DIRS),
        )
        result = self.harness.run_acquire()
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        return self.harness.acquired_slot_name(result)

    def test_each_directory_exists_in_the_checkout_and_in_the_slot_after_acquire(self):
        name = self._acquire()

        for root in (self.harness.fixture_repo, self.harness.slot_dir(name)):
            for rel in self.DIRS.split():
                self.assertTrue(os.path.isdir(os.path.join(root, rel)), f"{rel} missing under {root}")

    def test_release_succeeds_with_the_directories_present(self):
        name = self._acquire()

        result = self.harness.run("release", name, "--for", "test")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertFalse(os.path.isdir(self.harness.slot_dir(name)), "release left the slot directory")
        self.assertTrue(os.path.isdir(os.path.join(self.harness.fixture_repo, ".local", "shared")))

    def test_restart_creates_the_directories_a_slot_lacks(self):
        self.harness.build_fixture_repo(project_env=project_env_text(SLOT_MOUNT_DIRS=self.DIRS))
        self.harness.make_bare_slot(
            "slot-1",
            env_content=IDENTITY_ENV,
            ports_content="SSH_PORT=39220\nNOVNC_PORT=39230\nVITE_DEV_PORT=39240\n",
        )

        result = self.harness.run("restart", "slot-1")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        for root in (self.harness.fixture_repo, self.harness.slot_dir("slot-1")):
            for rel in self.DIRS.split():
                self.assertTrue(os.path.isdir(os.path.join(root, rel)), f"{rel} missing under {root}")


class ProjectNeutralSteps(DevContainerTestCase):

    def _volume_lines(self):
        with open(self.harness.docker_log, encoding="utf-8") as handle:
            return [line for line in handle.read().splitlines() if "volume create" in line]

    def _up_files(self):
        return [c for c in self.harness.compose_calls() if c["verb"] == "up"][-1]["files"]

    def test_a_store_volume_is_created_only_when_the_project_names_one(self):
        self.harness.build_fixture_repo(
            with_parent_env=IDENTITY_ENV, project_env=project_env_text(PNPM_STORE_VOLUME="store-x")
        )
        self.assertEqual(self.harness.run_acquire().returncode, 0)
        self.assertTrue(any("store-x" in line for line in self._volume_lines()), self._volume_lines())

    def test_no_store_volume_is_created_when_the_project_names_none(self):
        self.harness.build_fixture_repo(with_parent_env=IDENTITY_ENV, project_env=project_env_text())
        self.assertEqual(self.harness.run_acquire().returncode, 0)
        self.assertFalse(any("pnpm" in line for line in self._volume_lines()), self._volume_lines())

    def test_the_container_service_is_the_one_the_compose_config_mounts_the_checkout_in(self):
        self.harness.build_fixture_repo(with_parent_env=IDENTITY_ENV, project_env=project_env_text())

        result = self.harness.run(
            "acquire", "--for", "test", port_overrides={"DOCKER_STUB_SERVICE": "workspace"}
        )

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        with open(self.harness.docker_log, encoding="utf-8") as handle:
            execs = [line for line in handle.read().splitlines() if " exec " in line]
        self.assertTrue(execs)
        for line in execs:
            self.assertIn(" exec -T workspace ", line)

    def test_no_service_mounting_the_checkout_refuses_acquire_and_says_so(self):
        self.harness.build_fixture_repo(with_parent_env=IDENTITY_ENV, project_env=project_env_text())

        result = self.harness.run(
            "acquire", "--for", "test", port_overrides={"DOCKER_STUB_SERVICE": "none"}
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no service", result.stderr.decode())

    def test_two_services_mounting_the_checkout_refuse_acquire_and_name_both(self):
        self.harness.build_fixture_repo(with_parent_env=IDENTITY_ENV, project_env=project_env_text())

        result = self.harness.run(
            "acquire", "--for", "test", port_overrides={"DOCKER_STUB_SERVICE": "app,worker"}
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("app, worker", result.stderr.decode())

    def test_release_refuses_when_two_services_mount_the_checkout_rather_than_keep_the_key(self):
        self.harness.build_fixture_repo(with_parent_env=IDENTITY_ENV, project_env=project_env_text())
        acquired = self.harness.run_acquire()
        self.assertEqual(acquired.returncode, 0, acquired.stderr.decode())
        name = self.harness.acquired_slot_name(acquired)

        result = self.harness.run(
            "release", name, "--for", "test", port_overrides={"DOCKER_STUB_SERVICE": "app,worker"}
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("app, worker", result.stderr.decode())

    def _without_install_script(self, **values):
        self.harness.build_fixture_repo(
            with_parent_env=IDENTITY_ENV, project_env=project_env_text(**values)
        )
        os.remove(os.path.join(self.harness.fixture_repo, ".devcontainer", "install.sh"))

    def test_a_store_volume_with_no_install_script_is_refused(self):
        self._without_install_script(PNPM_STORE_VOLUME="store-x")

        result = self.harness.run_acquire()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("PNPM_STORE_VOLUME", result.stderr.decode())

    def test_no_install_script_and_no_store_volume_skips_the_install_and_says_so(self):
        self._without_install_script()

        result = self.harness.run_acquire()

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertIn("install is skipped", result.stdout.decode())

    def test_an_overlay_file_named_like_an_echo_flag_still_joins_the_stack(self):
        self.harness.build_fixture_repo(
            with_parent_env=IDENTITY_ENV + "SHARED_DIR=/somewhere\n",
            project_env=project_env_text(SLOT_OVERLAYS="SHARED_DIR=-n", SLOT_ENV_KEYS="SHARED_DIR"),
        )

        self.assertEqual(self.harness.run_acquire().returncode, 0)

        self.assertTrue(any(f.endswith("/-n") for f in self._up_files()), self._up_files())

    def test_an_overlay_file_joins_the_stack_when_its_variable_is_set_in_the_slots_env(self):
        self.harness.build_fixture_repo(
            with_parent_env=IDENTITY_ENV + "SHARED_DIR=/somewhere\n",
            project_env=project_env_text(SLOT_OVERLAYS="SHARED_DIR=docker-compose.shared.yml", SLOT_ENV_KEYS="SHARED_DIR"),
        )

        self.assertEqual(self.harness.run_acquire().returncode, 0)

        self.assertTrue(any(f.endswith("docker-compose.shared.yml") for f in self._up_files()), self._up_files())

    def test_an_overlay_file_stays_out_when_its_variable_is_not_set(self):
        self.harness.build_fixture_repo(
            with_parent_env=IDENTITY_ENV,
            project_env=project_env_text(SLOT_OVERLAYS="SHARED_DIR=docker-compose.shared.yml"),
        )

        self.assertEqual(self.harness.run_acquire().returncode, 0)

        self.assertFalse(any(f.endswith("docker-compose.shared.yml") for f in self._up_files()), self._up_files())


if __name__ == "__main__":
    unittest.main()
