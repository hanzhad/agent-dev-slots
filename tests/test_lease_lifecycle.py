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


class LeaseVerbs(DevContainerTestCase):
    ENV_WITH_IDENTITY = "GIT_USER_NAME=demo-agent\nGIT_USER_EMAIL=demo-agent@example.com\n"

    def test_acquire_writes_a_lease_and_release_removes_it(self):
        self.harness.build_fixture_repo(with_parent_env=self.ENV_WITH_IDENTITY)

        result = self.harness.run_acquire()
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        name = self.harness.acquired_slot_name(result)
        self.assertTrue(os.path.isfile(self.harness.lease_path(name)), result.stdout.decode())

        result = self.harness.run("release", name, "--for", "test")
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertFalse(os.path.isfile(self.harness.lease_path(name)))
        self.assertFalse(os.path.isdir(self.harness.slot_dir(name)), "release left the slot directory")

    def test_acquire_sets_ssh_public_key_in_env_and_release_removes_the_key_with_the_slot(self):
        self.harness.build_fixture_repo(with_parent_env=self.ENV_WITH_IDENTITY)

        result = self.harness.run_acquire()
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        name = self.harness.acquired_slot_name(result)
        key = self.harness.read_env(name)["SSH_PUBLIC_KEY"]
        self.assertTrue(key.startswith("ssh-ed25519 "), key)
        with open(self.harness.lease_path(name), encoding="utf-8") as handle:
            key_path = json.load(handle)["key"]
        self.assertTrue(os.path.isfile(key_path))

        result = self.harness.run("release", name, "--for", "test")
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertFalse(os.path.exists(key_path), "the private key of the lease outlived the release")
        self.assertFalse(os.path.exists(key_path + ".pub"), "the public key of the lease outlived the release")
        self.assertFalse(os.path.isdir(self.harness.slot_dir(name)), "release left the slot directory")


    def test_acquire_as_records_the_owner_in_the_lease_and_in_list(self):
        self.harness.build_fixture_repo(with_parent_env=self.ENV_WITH_IDENTITY)
        owner = "Sesión de compilación — vérification [a1b2c3]"

        result = self.harness.run_acquire(owner=owner)

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        name = self.harness.acquired_slot_name(result)
        with open(self.harness.lease_path(name), encoding="utf-8") as handle:
            lease = json.load(handle)
        self.assertEqual(lease["owner"], owner)
        listing = self.harness.run("list", "--brief")
        self.assertEqual(listing.returncode, 0, listing.stderr.decode())
        row = [line for line in listing.stdout.decode().splitlines() if line.startswith(name)]
        self.assertTrue(row, listing.stdout.decode())
        self.assertIn(owner, row[0], row[0])

    def test_acquire_without_as_records_a_dash_for_owner(self):
        self.harness.build_fixture_repo(with_parent_env=self.ENV_WITH_IDENTITY)

        result = self.harness.run_acquire()

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        name = self.harness.acquired_slot_name(result)
        with open(self.harness.lease_path(name), encoding="utf-8") as handle:
            lease = json.load(handle)
        self.assertEqual(lease["owner"], "-")
        listing = self.harness.run("list", "--brief")
        self.assertEqual(listing.returncode, 0, listing.stderr.decode())
        self.assertEqual(self._row(listing.stdout.decode(), name)[4], "-")

    def test_list_reads_a_lease_written_before_owner_existed_as_a_dash(self):
        self.harness.build_fixture_repo(with_parent_env=self.ENV_WITH_IDENTITY)
        self.harness.make_bare_slot(
            "legacy",
            env_content=self.ENV_WITH_IDENTITY,
            ports_content="NOVNC_PORT=39234\nSSH_PORT=39224\nVITE_DEV_PORT=39244\n",
        )
        path = self.harness.lease_path("legacy")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "holder": "task-5",
                    "project": PROJECT_PREFIX + "-legacy",
                    "since": int(time.time()),
                    "ttl_seconds": 14400,
                    "key": "/dev/null",
                    "ssh_port": 39224,
                },
                handle,
            )

        result = self.harness.run("list", "--brief")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(self._row(result.stdout.decode(), "legacy")[4], "-")

    @staticmethod
    def _row(stdout, slot_name):
        for line in stdout.splitlines():
            fields = line.split()
            if fields and fields[0] == slot_name:
                return fields
        raise AssertionError(f"no '{slot_name}' row in:\n{stdout}")


class ReleaseRefusesWithoutTheMatchingHolder(DevContainerTestCase):
    ENV_WITH_IDENTITY = LeaseVerbs.ENV_WITH_IDENTITY

    def _acquire(self, holder, owner=None):
        self.harness.build_fixture_repo(with_parent_env=self.ENV_WITH_IDENTITY)
        result = self.harness.run_acquire(holder=holder, owner=owner)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        return self.harness.acquired_slot_name(result)

    def _lease_snapshot(self, name):
        with open(self.harness.lease_path(name), encoding="utf-8") as handle:
            return json.load(handle)

    def test_release_with_no_for_refuses_and_touches_nothing(self):
        name = self._acquire(holder="task-6-holder", owner="Session A [aaaa]")
        before = self._lease_snapshot(name)
        key_path = before["key"]
        self.assertTrue(os.path.exists(key_path), "fixture's own key is missing before the test even runs")
        pub_key_before = self.harness.read_env(name)["SSH_PUBLIC_KEY"]

        result = self.harness.run("release", name)

        self.assertNotEqual(result.returncode, 0, result.stdout.decode())
        stderr = result.stderr.decode()
        self.assertIn("task-6-holder", stderr)
        self.assertIn("Session A [aaaa]", stderr)
        self.assertIn("evict", stderr)
        self.assertTrue(os.path.isfile(self.harness.lease_path(name)), "the lease was removed by a refused release")
        self.assertEqual(self._lease_snapshot(name), before, "the lease was rewritten by a refused release")
        self.assertTrue(os.path.exists(key_path), "the holder's own private key was deleted by a refused release")
        self.assertEqual(
            self.harness.read_env(name)["SSH_PUBLIC_KEY"], pub_key_before,
            "authorized_keys was blanked by a refused release",
        )

    def test_release_with_the_wrong_for_refuses_and_touches_nothing(self):
        name = self._acquire(holder="task-6-holder", owner="Session A [aaaa]")
        before = self._lease_snapshot(name)
        key_path = before["key"]
        pub_key_before = self.harness.read_env(name)["SSH_PUBLIC_KEY"]

        result = self.harness.run("release", name, "--for", "somebody-elses-issue")

        self.assertNotEqual(result.returncode, 0, result.stdout.decode())
        stderr = result.stderr.decode()
        self.assertIn("task-6-holder", stderr)
        self.assertIn("Session A [aaaa]", stderr)
        self.assertIn("somebody-elses-issue", stderr)
        self.assertIn("evict", stderr)
        self.assertTrue(os.path.isfile(self.harness.lease_path(name)))
        self.assertEqual(self._lease_snapshot(name), before)
        self.assertTrue(os.path.exists(key_path))
        self.assertEqual(self.harness.read_env(name)["SSH_PUBLIC_KEY"], pub_key_before)

    def test_release_with_no_lease_refuses_and_touches_nothing(self):
        self.harness.build_fixture_repo(with_parent_env=self.ENV_WITH_IDENTITY)
        self.harness.make_bare_slot("probe", env_content=self.ENV_WITH_IDENTITY)

        result = self.harness.run("release", "probe", "--for", "anyone")

        self.assertNotEqual(result.returncode, 0, result.stdout.decode())
        self.assertIn("no lease", result.stderr.decode())
        self.assertFalse(os.path.isfile(self.harness.lease_path("probe")))

    def test_release_with_the_matching_for_releases_as_before(self):
        name = self._acquire(holder="task-6-holder")

        result = self.harness.run("release", name, "--for", "task-6-holder")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertFalse(os.path.isfile(self.harness.lease_path(name)))
        self.assertFalse(os.path.isdir(self.harness.slot_dir(name)), "release left the slot directory")


class SlotHolderFile(DevContainerTestCase):
    ENV_WITH_IDENTITY = LeaseVerbs.ENV_WITH_IDENTITY

    def _slot_with_real_remote(self, name):
        self.harness.build_fixture_repo(with_parent_env=self.ENV_WITH_IDENTITY)
        self.harness.make_bare_slot(name, env_content=self.ENV_WITH_IDENTITY)
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
        with open(os.path.join(slot, "README.md"), "w", encoding="utf-8") as handle:
            handle.write("slot fixture\n")
        with open(os.path.join(slot, ".gitignore"), "w", encoding="utf-8") as handle:
            handle.write(".devcontainer/.env\n.devcontainer/.slot-holder\ndocs/\n")
        git("add", "README.md", ".gitignore")
        git("commit", "-q", "-m", "base")
        git("branch", "-M", "master")
        git("push", "-q", "origin", "master")
        git("fetch", "-q", "origin")
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
                    "owner": "-",
                },
                handle,
            )

    def _write_holder_stub(self, name, holder):
        path = self.harness.holder_path(name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(
                f"holder={holder}\nowner=-\nslot_id={PROJECT_PREFIX}-{name}\n"
                "since_epoch=0\nsince_iso=1970-01-01T00:00:00Z\n"
            )

    def test_acquire_writes_the_holder_file_matching_the_lease(self):
        self.harness.build_fixture_repo(with_parent_env=self.ENV_WITH_IDENTITY)

        result = self.harness.run_acquire(holder="task-7", owner="Session A [aaaa]")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        name = self.harness.acquired_slot_name(result)
        self.assertTrue(os.path.isfile(self.harness.holder_path(name)))
        with open(self.harness.lease_path(name), encoding="utf-8") as handle:
            lease = json.load(handle)
        holder = self.harness.read_holder(name)
        self.assertEqual(holder["holder"], "task-7")
        self.assertEqual(holder["owner"], "Session A [aaaa]")
        self.assertEqual(holder["slot_id"], f"{PROJECT_PREFIX}-{name}")
        self.assertEqual(int(holder["since_epoch"]), lease["since"])
        self.assertNotIn("expiry_epoch", holder)
        self.assertNotIn("ttl_seconds", lease)

    def test_release_removes_the_holder_file(self):
        self.harness.build_fixture_repo(with_parent_env=self.ENV_WITH_IDENTITY)
        result = self.harness.run_acquire()
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        name = self.harness.acquired_slot_name(result)
        self.assertTrue(os.path.isfile(self.harness.holder_path(name)))

        release = self.harness.run("release", name, "--for", "test")

        self.assertEqual(release.returncode, 0, release.stderr.decode())
        self.assertFalse(os.path.isfile(self.harness.holder_path(name)))
        self.assertFalse(os.path.isdir(self.harness.slot_dir(name)), "the holder file went but the slot stayed")

    def test_evict_removes_the_holder_file(self):
        name = "evicted"
        self._slot_with_real_remote(name)
        self._write_lease(name, "task-7")
        self._write_holder_stub(name, "task-7")

        result = self.harness.run("evict", name, "--yes", "--force")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertFalse(os.path.isfile(self.harness.holder_path(name)))


    def test_acquire_on_a_checkout_predating_the_gitignore_line_still_reuse_cleans(self):
        self.harness.build_fixture_repo(with_parent_env=self.ENV_WITH_IDENTITY)
        gitignore_path = os.path.join(self.harness.fixture_repo, ".gitignore")
        with open(gitignore_path, encoding="utf-8") as handle:
            content = handle.read()
        self.assertIn(".slot-holder", content, "fixture's own .gitignore is missing the line this test removes")
        stale = content.replace(".slot-holder\n", "")
        self.assertNotIn(".slot-holder", stale)
        with open(gitignore_path, "w", encoding="utf-8") as handle:
            handle.write(stale)
        subprocess.run(
            ["git", "-C", self.harness.fixture_repo, "commit", "-q", "-am", "earlier .gitignore"],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )

        result = self.harness.run_acquire(holder="task-7-old-checkout")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        name = self.harness.acquired_slot_name(result)
        slot = self.harness.slot_dir(name)
        status = subprocess.run(
            ["git", "-C", slot, "status", "--porcelain"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
        ).stdout.decode()
        self.assertEqual(
            status, "", "the holder file reads as untracked work on a stale-.gitignore checkout"
        )

        release = self.harness.run("release", name, "--for", "task-7-old-checkout")

        self.assertEqual(release.returncode, 0, release.stderr.decode())


class ReleaseStopsTheSlotsContainers(DevContainerTestCase):
    ENV_WITH_IDENTITY = LeaseVerbs.ENV_WITH_IDENTITY
    _slot_with_real_remote = SlotHolderFile._slot_with_real_remote
    _write_lease = SlotHolderFile._write_lease

    def _running(self, name):
        return os.path.isfile(
            os.path.join(self.harness.docker_state, f"{PROJECT_PREFIX}-{name}.running")
        )

    def _docker_log(self):
        with open(self.harness.docker_log, encoding="utf-8") as handle:
            return handle.read()

    def _acquire(self):
        acquired = self.harness.run_acquire()
        self.assertEqual(acquired.returncode, 0, acquired.stderr.decode())
        name = self.harness.acquired_slot_name(acquired)
        self.assertTrue(self._running(name))
        return name

    def test_release_takes_the_compose_project_down_with_its_volumes_and_removes_the_slot(self):
        self.harness.build_fixture_repo(with_parent_env=self.ENV_WITH_IDENTITY)
        name = self._acquire()

        result = self.harness.run("release", name, "--for", "test")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertFalse(self._running(name), result.stdout.decode())
        self.assertRegex(self._docker_log(), rf"-p {PROJECT_PREFIX}-{name} .*down -v")
        self.assertFalse(os.path.isdir(self.harness.slot_dir(name)))
        self.assertFalse(os.path.isfile(self.harness.lease_path(name)))
        self.assertIn("removed", result.stdout.decode())

    def test_evict_stops_them(self):
        name = "evicted"
        slot = self._slot_with_real_remote(name)
        for args in (
            ["add", ".devcontainer/docker-compose.yml"],
            ["commit", "-q", "-m", "compose file"],
            ["push", "-q", "origin", "master"],
        ):
            subprocess.run(
                [self.harness.real_git_bin, "-C", slot, "-c", "commit.gpgsign=false", *args],
                check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
        self._write_lease(name, "task-8")
        self.harness.mark_running(name)

        result = self.harness.run("evict", name, "--yes", "--force")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertFalse(self._running(name), result.stdout.decode())
        self.assertTrue(os.path.isdir(self.harness.slot_dir(name)))
        self.assertFalse(os.path.isfile(self.harness.lease_path(name)))


class ForRefusesADuration(DevContainerTestCase):
    ENV_WITH_IDENTITY = LeaseVerbs.ENV_WITH_IDENTITY

    @staticmethod
    def _lease(path):
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)

    def test_acquire_for_4h_refuses_and_writes_no_lease(self):
        self.harness.build_fixture_repo()

        result = self.harness.run_acquire(holder="4h")

        self.assertNotEqual(result.returncode, 0)
        stderr = result.stderr.decode()
        self.assertIn("no term", stderr)
        self.assertIn("4h", stderr)
        self.assertFalse(os.path.exists(self.harness.slot_dir("slot-1")))
        self.assertFalse(os.path.isfile(self.harness.lease_path("slot-1")))


    def test_acquire_for_values_that_are_not_a_duration_are_accepted(self):
        self.harness.build_fixture_repo(with_parent_env=self.ENV_WITH_IDENTITY)
        for holder in ("task-1-details-demo", "manual", "-", "4hours", "h4"):
            with self.subTest(holder=holder):
                result = self.harness.run_acquire(holder=holder)
                self.assertEqual(result.returncode, 0, result.stderr.decode())
                self.assertNotIn("no term", result.stderr.decode())
                name = self.harness.acquired_slot_name(result)
                self.assertEqual(self._lease(self.harness.lease_path(name))["holder"], holder)



    def test_acquire_for_with_whitespace_refuses_and_writes_no_lease(self):
        self.harness.build_fixture_repo()

        result = self.harness.run_acquire(holder="task a b")

        self.assertNotEqual(result.returncode, 0)
        stderr = result.stderr.decode()
        self.assertIn("whitespace", stderr)
        self.assertFalse(os.path.exists(self.harness.slot_dir("slot-1")))
        self.assertFalse(os.path.isfile(self.harness.lease_path("slot-1")))


class ParkAVerbForGivingBackADirtySlot(DevContainerTestCase):
    ENV_WITH_IDENTITY = LeaseVerbs.ENV_WITH_IDENTITY

    @staticmethod
    def _dirty(slot_dir):
        with open(os.path.join(slot_dir, "scratch.txt"), "w", encoding="utf-8") as handle:
            handle.write("uncommitted, never staged\n")

    def test_park_removes_the_lease_and_writes_a_marker(self):
        self.harness.build_fixture_repo(with_parent_env=self.ENV_WITH_IDENTITY)
        result = self.harness.run_acquire(holder="task-11")
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        name = self.harness.acquired_slot_name(result)
        self._dirty(self.harness.slot_dir(name))

        park = self.harness.run("park", name)

        self.assertEqual(park.returncode, 0, park.stderr.decode())
        self.assertFalse(os.path.isfile(self.harness.lease_path(name)), "lease survived park")
        marker_path = self.harness.parked_path(name)
        self.assertTrue(os.path.isfile(marker_path), park.stdout.decode())
        with open(marker_path, encoding="utf-8") as handle:
            marker = json.load(handle)
        self.assertEqual(marker["project"], f"{PROJECT_PREFIX}-{name}")
        self.assertEqual(marker["holder"], "task-11")
        self.assertIn("uncommitted", marker["reason"])
        self.assertIsInstance(marker["since"], int)

    def test_park_blanks_the_ssh_key_same_as_release(self):
        self.harness.build_fixture_repo(with_parent_env=self.ENV_WITH_IDENTITY)
        result = self.harness.run_acquire()
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        name = self.harness.acquired_slot_name(result)
        self._dirty(self.harness.slot_dir(name))

        park = self.harness.run("park", name)

        self.assertEqual(park.returncode, 0, park.stderr.decode())
        self.assertEqual(self.harness.read_env(name)["SSH_PUBLIC_KEY"], "")

    def test_park_on_a_clean_slot_releases_instead_and_says_so(self):
        self.harness.build_fixture_repo(with_parent_env=self.ENV_WITH_IDENTITY)
        result = self.harness.run_acquire()
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        name = self.harness.acquired_slot_name(result)

        park = self.harness.run("park", name)

        self.assertEqual(park.returncode, 0, park.stderr.decode())
        self.assertFalse(os.path.isfile(self.harness.lease_path(name)))
        self.assertFalse(os.path.isfile(self.harness.parked_path(name)), "clean slot got a park marker")
        stdout = park.stdout.decode()
        self.assertIn("clean", stdout)
        self.assertIn("release", stdout)

    def test_park_refuses_a_slot_with_no_lease(self):
        self.harness.build_fixture_repo(with_parent_env=self.ENV_WITH_IDENTITY)
        self.harness.make_bare_slot("probe", env_content=self.ENV_WITH_IDENTITY)

        park = self.harness.run("park", "probe")

        self.assertNotEqual(park.returncode, 0, park.stdout.decode())
        self.assertIn("no lease", park.stderr.decode())
        self.assertFalse(os.path.isfile(self.harness.parked_path("probe")))

    def test_park_refuses_a_slot_that_is_not_this_projects_own(self):
        self.harness.build_fixture_repo(with_parent_env=self.ENV_WITH_IDENTITY)
        self.harness.make_bare_slot("probe", env_content=self.ENV_WITH_IDENTITY)
        subprocess.run(
            [
                "git", "-C", self.harness.slot_dir("probe"), "remote", "set-url", "origin",
                "https://example.invalid/somebody-elses.git",
            ],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )

        park = self.harness.run("park", "probe")

        self.assertNotEqual(park.returncode, 0, park.stdout.decode())
        self.assertIn("not this project's own slot", park.stderr.decode())
        self.assertFalse(os.path.isfile(self.harness.parked_path("probe")))


    def test_park_clear_removes_the_marker_without_cleaning_the_slot(self):
        self.harness.build_fixture_repo(with_parent_env=self.ENV_WITH_IDENTITY)
        result = self.harness.run_acquire()
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        name = self.harness.acquired_slot_name(result)
        slot_dir = self.harness.slot_dir(name)
        self._dirty(slot_dir)
        park = self.harness.run("park", name)
        self.assertEqual(park.returncode, 0, park.stderr.decode())
        self.assertTrue(os.path.isfile(self.harness.parked_path(name)))

        clear = self.harness.run("park", name, "--clear")

        self.assertEqual(clear.returncode, 0, clear.stderr.decode())
        self.assertFalse(os.path.isfile(self.harness.parked_path(name)))
        self.assertTrue(os.path.isfile(os.path.join(slot_dir, "scratch.txt")))
        self.assertFalse(
            os.path.isfile(self.harness.lease_path(name)), "clearing a park must not re-lease the slot"
        )

    def test_park_clear_refuses_when_nothing_is_parked(self):
        self.harness.build_fixture_repo(with_parent_env=self.ENV_WITH_IDENTITY)
        self.harness.make_bare_slot("probe", env_content=self.ENV_WITH_IDENTITY)

        clear = self.harness.run("park", "probe", "--clear")

        self.assertNotEqual(clear.returncode, 0, clear.stdout.decode())
        self.assertIn("nothing to clear", clear.stderr.decode())

    def test_list_shows_parked_as_its_own_state_with_the_reason(self):
        self.harness.build_fixture_repo(with_parent_env=self.ENV_WITH_IDENTITY)
        result = self.harness.run_acquire(holder="task-11")
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        name = self.harness.acquired_slot_name(result)
        self._dirty(self.harness.slot_dir(name))
        park = self.harness.run("park", name)
        self.assertEqual(park.returncode, 0, park.stderr.decode())

        listing = self.harness.run("list")

        self.assertEqual(listing.returncode, 0, listing.stderr.decode())
        row = [line for line in listing.stdout.decode().splitlines() if line.startswith(name)]
        self.assertTrue(row, listing.stdout.decode())
        fields = row[0].split()
        self.assertEqual(fields[2], "parked")
        self.assertNotIn("free", fields)
        self.assertIn("task-11", row[0])
        self.assertIn("uncommitted", row[0])


class IdleLeaseIsListedAsIdle(DevContainerTestCase):

    ENV = "GIT_USER_NAME=demo-agent\nGIT_USER_EMAIL=demo-agent@example.com\n"
    PORTS = "NOVNC_PORT=39236\nSSH_PORT=39226\nVITE_DEV_PORT=39246\n"

    def setUp(self):
        super().setUp()
        self.now = 1700000000
        self.harness.set_now(self.now)

    def _write_lease(self, name, age_seconds):
        path = self.harness.lease_path(name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "holder": "task-12",
                    "project": PROJECT_PREFIX + "-" + name,
                    "since": self.harness.now_epoch() - age_seconds,
                    "key": "/dev/null",
                    "ssh_port": 39226,
                },
                handle,
            )
        return path

    def _make_slot(self, name, local_identity=("demo-agent", "demo-agent@example.com")):
        self.harness.build_fixture_repo()
        self.harness.make_bare_slot(
            name,
            env_content=self.ENV,
            ports_content=self.PORTS,
            local_identity=local_identity,
        )

    def _make_clean_slot(self, name):
        self._make_slot(name)
        slot = self.harness.slot_dir(name)
        git = lambda *args: subprocess.run(
            [self.harness.real_git_bin, "-C", slot, "-c", "commit.gpgsign=false", *args],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        remote = os.path.join(self.harness.tmp, name + "-remote.git")
        subprocess.run(
            ["git", "init", "--bare", "-q", remote],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        git("add", "-A")
        git("commit", "-q", "-m", "fixture")
        git("branch", "-M", "master")
        git("push", "-q", remote, "master:refs/heads/master")
        git("fetch", "-q", remote, "+refs/heads/master:refs/remotes/origin/master")
        return slot

    def test_idle_past_the_limit_is_idle_whatever_runs_in_it(self):
        self._make_clean_slot("held-open")
        self.harness.mark_running("held-open")
        self.harness.set_container_procs("held-open", ["sshd", "sshd", "playwright"])
        self._write_lease("held-open", age_seconds=18000)

        result = self.harness.run("list")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        row = self._row(result.stdout.decode(), "held-open")
        self.assertEqual(row[10], "clean", "fixture is not actually clean — test proves nothing")
        self.assertEqual(row[2], "idle")

    def test_idle_and_dirty_is_idle(self):
        self._make_slot("left-dirty")
        self._write_lease("left-dirty", age_seconds=18000)

        result = self.harness.run("list")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(self._row(result.stdout.decode(), "left-dirty")[2], "idle")

    def test_a_live_lease_on_a_busy_slot_is_leased(self):
        self._make_clean_slot("working")
        self.harness.mark_running("working")
        self.harness.set_container_procs("working", ["sshd", "sshd"])
        self._write_lease("working", age_seconds=60)

        result = self.harness.run("list")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(self._row(result.stdout.decode(), "working")[2], "leased")

    def test_a_lease_inside_the_limit_that_nothing_uses_is_leased(self):
        self._make_clean_slot("quiet")
        self._write_lease("quiet", age_seconds=3600)

        result = self.harness.run("list", "--brief")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        row = self._row(result.stdout.decode(), "quiet")
        self.assertEqual(row[2], "leased")
        self.assertEqual(row[10], "-")

    def test_acquired_slot_transitions_from_leased_to_idle_at_exact_limit_via_dev_container_now(self):
        self.harness.build_fixture_repo(with_parent_env=self.ENV)
        acquired = self.harness.run_acquire(holder="clock-test")
        self.assertEqual(acquired.returncode, 0, acquired.stderr.decode())
        name = self.harness.acquired_slot_name(acquired)

        self.harness.set_now(self.now + 7199)
        before = self.harness.run("list", "--brief")
        self.assertEqual(before.returncode, 0, before.stderr.decode())
        self.assertEqual(self._row(before.stdout.decode(), name)[2], "leased")

        self.harness.set_now(self.now + 7200)
        at_limit = self.harness.run("list", "--brief")
        self.assertEqual(at_limit.returncode, 0, at_limit.stderr.decode())
        self.assertEqual(self._row(at_limit.stdout.decode(), name)[2], "idle")

    @staticmethod
    def _row(stdout, slot_name):
        for line in stdout.splitlines():
            fields = line.split()
            if fields and fields[0] == slot_name:
                return fields
        raise AssertionError(f"no '{slot_name}' row in:\n{stdout}")


class ReleaseAcceptsTheAcquireShapedSlotFlag(DevContainerTestCase):
    ENV_WITH_IDENTITY = LeaseVerbs.ENV_WITH_IDENTITY

    def test_release_accepts_slot_flag_same_as_acquire(self):
        self.harness.build_fixture_repo(with_parent_env=self.ENV_WITH_IDENTITY)
        acquired = self.harness.run_acquire()
        self.assertEqual(acquired.returncode, 0, acquired.stderr.decode())
        name = self.harness.acquired_slot_name(acquired)

        result = self.harness.run("release", "--slot", name, "--for", "test")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertFalse(os.path.isfile(self.harness.lease_path(name)), result.stdout.decode())

    def test_release_accepts_the_prefixed_id_acquire_itself_prints(self):
        self.harness.build_fixture_repo(with_parent_env=self.ENV_WITH_IDENTITY)
        acquired = self.harness.run_acquire()
        self.assertEqual(acquired.returncode, 0, acquired.stderr.decode())
        name = self.harness.acquired_slot_name(acquired)
        prefixed = f"{PROJECT_PREFIX}-{name}"

        result = self.harness.run("release", prefixed, "--for", "test")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertFalse(os.path.isfile(self.harness.lease_path(name)), result.stdout.decode())


class NowEpochClockInjection(DevContainerTestCase):
    ENV_WITH_IDENTITY = "GIT_USER_NAME=demo-agent\nGIT_USER_EMAIL=demo-agent@example.com\n"

    def _run_lib(self, body, dev_container_now=None):
        env = self.harness._env(None)
        env["DEV_CONTAINER_LIB"] = "1"
        env.pop("DEV_CONTAINER_NOW", None)
        if dev_container_now is not None:
            env["DEV_CONTAINER_NOW"] = dev_container_now
        return subprocess.run(
            ["bash", "-c", '. "$0"; ' + body, self.harness.script],
            cwd=self.harness.tmp,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )

    def test_now_epoch_respects_integer_dev_container_now_and_falls_back_otherwise(self):
        self.harness.build_fixture_repo(with_parent_env=self.ENV_WITH_IDENTITY)

        for valid in ("0", "1", "1700000000", "1799999999", "2147483648"):
            with self.subTest(valid=valid):
                res = self._run_lib("now_epoch", dev_container_now=valid)
                self.assertEqual(res.returncode, 0, res.stderr.decode())
                self.assertEqual(res.stdout.decode().strip(), valid)

        for invalid in (
            None,
            "",
            "abc",
            "1700000000.5",
            "1700000000s",
            " 1700000000",
            "1700000000 ",
            "+1700000000",
            "-",
            "1700000000;echo_injected",
        ):
            with self.subTest(invalid=repr(invalid)):
                before = int(time.time())
                res = self._run_lib("now_epoch", dev_container_now=invalid)
                after = int(time.time())
                self.assertEqual(res.returncode, 0, res.stderr.decode())
                out = res.stdout.decode().strip()
                self.assertRegex(out, r"^[0-9]+$")
                self.assertNotIn("injected", out)
                self.assertGreaterEqual(int(out), before - 1)
                self.assertLessEqual(int(out), after + 1)

    def test_acquire_writes_lease_and_holder_timestamps_from_dev_container_now(self):
        self.harness.build_fixture_repo(with_parent_env=self.ENV_WITH_IDENTITY)
        self.harness.set_now(1700000000)

        acquired = self.harness.run_acquire(holder="clock-holder", owner="Agent [clk]")
        self.assertEqual(acquired.returncode, 0, acquired.stderr.decode())
        name = self.harness.acquired_slot_name(acquired)

        with open(self.harness.lease_path(name), encoding="utf-8") as handle:
            lease = json.load(handle)
        holder = self.harness.read_holder(name)
        self.assertEqual(lease["since"], 1700000000)
        self.assertEqual(int(holder["since_epoch"]), 1700000000)
        self.assertEqual(holder["since_iso"], "2023-11-14T22:13:20Z")

    def test_wait_deadline_check_uses_dev_container_now(self):
        self.harness.build_fixture_repo(with_parent_env=self.ENV_WITH_IDENTITY)

        before_deadline = self._run_lib(
            'WAIT_DEADLINE=1700000005; WAIT_TIMEOUT="5s"; wait_deadline_check',
            dev_container_now="1700000004",
        )
        self.assertEqual(before_deadline.returncode, 0, before_deadline.stderr.decode())

        at_deadline = self._run_lib(
            'WAIT_DEADLINE=1700000005; WAIT_TIMEOUT="5s"; wait_deadline_check',
            dev_container_now="1700000005",
        )
        self.assertEqual(at_deadline.returncode, 3, at_deadline.stderr.decode())
        self.assertIn("no slot became free within 5s", at_deadline.stderr.decode())

    def test_list_seen_stamp_and_evict_stale_use_dev_container_now(self):
        self.harness.build_fixture_repo(with_parent_env=self.ENV_WITH_IDENTITY)
        t_seen = 1700000000
        self.harness.set_now(t_seen)

        acquired = self.harness.run_acquire(holder="seen-holder", owner="Agent [seen]")
        self.assertEqual(acquired.returncode, 0, acquired.stderr.decode())
        name = self.harness.acquired_slot_name(acquired)
        sid = f"{PROJECT_PREFIX}-{name}"

        self.harness.mark_running(name)
        self.harness.set_container_procs(name, ["sshd", "sshd"])

        listed = self.harness.run("list", "--brief")
        self.assertEqual(listed.returncode, 0, listed.stderr.decode())

        seen_file = os.path.join(self.harness.state_dir, "seen", sid)
        self.assertTrue(os.path.isfile(seen_file), f"missing seen stamp at {seen_file}")
        with open(seen_file, encoding="utf-8") as handle:
            self.assertEqual(handle.read().strip(), str(t_seen))

        self.harness.set_container_procs(name, ["sshd"])

        self.harness.set_now(t_seen + 6 * 86400)
        before_cutoff = self.harness.run("evict", "--stale")
        self.assertEqual(before_cutoff.returncode, 0, before_cutoff.stderr.decode())
        self.assertTrue(
            os.path.isfile(self.harness.lease_path(name)),
            "evict --stale evicted slot before 7 days",
        )

        self.harness.set_now(t_seen + 8 * 86400)
        past_cutoff = self.harness.run("evict", "--stale")
        self.assertEqual(past_cutoff.returncode, 0, past_cutoff.stderr.decode())
        self.assertFalse(
            os.path.isfile(self.harness.lease_path(name)),
            "evict --stale did not evict slot after 8 days under DEV_CONTAINER_NOW",
        )

    def test_list_seen_stamp_with_future_dev_container_now_is_not_evicted_before_stale_cutoff(self):
        self.harness.build_fixture_repo(with_parent_env=self.ENV_WITH_IDENTITY)
        t_seen = 2000000000
        self.harness.set_now(t_seen)

        acquired = self.harness.run_acquire(holder="future-holder", owner="Agent [fut]")
        self.assertEqual(acquired.returncode, 0, acquired.stderr.decode())
        name = self.harness.acquired_slot_name(acquired)
        sid = f"{PROJECT_PREFIX}-{name}"

        self.harness.mark_running(name)
        self.harness.set_container_procs(name, ["sshd", "sshd"])

        listed = self.harness.run("list", "--brief")
        self.assertEqual(listed.returncode, 0, listed.stderr.decode())

        seen_file = os.path.join(self.harness.state_dir, "seen", sid)
        with open(seen_file, encoding="utf-8") as handle:
            self.assertEqual(handle.read().strip(), str(t_seen))

        self.harness.set_container_procs(name, ["sshd"])

        self.harness.set_now(t_seen + 6 * 86400)
        before_cutoff = self.harness.run("evict", "--stale")
        self.assertEqual(before_cutoff.returncode, 0, before_cutoff.stderr.decode())
        self.assertTrue(
            os.path.isfile(self.harness.lease_path(name)),
            "evict --stale prematurely evicted slot seen at future DEV_CONTAINER_NOW",
        )

        self.harness.set_now(t_seen + 8 * 86400)
        past_cutoff = self.harness.run("evict", "--stale")
        self.assertEqual(past_cutoff.returncode, 0, past_cutoff.stderr.decode())
        self.assertFalse(
            os.path.isfile(self.harness.lease_path(name)),
            "evict --stale did not evict slot after 8 days from future DEV_CONTAINER_NOW",
        )

    def test_stat_mtime_or_now_respects_injected_clock(self):
        self.harness.build_fixture_repo(with_parent_env=self.ENV_WITH_IDENTITY)
        target = os.path.join(self.harness.tmp, "mtime-target")
        os.makedirs(target, exist_ok=True)
        os.utime(target, (1600000000, 1600000000))

        for injected in ("1700000000", "2000000000"):
            with self.subTest(injected=injected):
                res = self._run_lib(
                    f'stat_mtime_or_now "{target}"',
                    dev_container_now=injected,
                )
                self.assertEqual(res.returncode, 0, res.stderr.decode())
                self.assertEqual(res.stdout.decode().strip(), injected)

        real = self._run_lib(f'stat_mtime_or_now "{target}"')
        self.assertEqual(real.returncode, 0, real.stderr.decode())
        self.assertEqual(real.stdout.decode().strip(), "1600000000")


class LeaseStateSignature(DevContainerTestCase):
    ENV_WITH_IDENTITY = "GIT_USER_NAME=demo-agent\nGIT_USER_EMAIL=demo-agent@example.com\n"

    def _signature(self):
        env = self.harness._env(None)
        env["DEV_CONTAINER_LIB"] = "1"
        res = subprocess.run(
            ["bash", "-c", '. "$0"; lease_state_signature', self.harness.script],
            cwd=self.harness.tmp,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )
        self.assertEqual(res.returncode, 0, res.stderr.decode())
        return res.stdout.decode()

    @staticmethod
    def _write_pinned(path, text, mtime):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.utime(path, (mtime, mtime))

    def test_lease_state_signature_ignores_mtime_changes_when_contents_unchanged(self):
        self.harness.build_fixture_repo(with_parent_env=self.ENV_WITH_IDENTITY)
        limits = os.path.join(self.harness.state_dir, "limits.json")
        lease = self.harness.lease_path("slot-1")
        parked = self.harness.parked_path("slot-2")
        self._write_pinned(limits, '{"machine_max": 1}', 1700000000.0)
        self._write_pinned(lease, '{"holder": "a", "since": 1700000000}', 1700000000.0)
        self._write_pinned(parked, '{"holder": "b", "reason": "dirty"}', 1700000000.0)

        sig_before = self._signature()

        for path in (limits, lease, parked):
            os.utime(path, (1700000010.0, 1700000010.0))

        sig_after = self._signature()
        self.assertEqual(sig_before, sig_after)

    def test_lease_state_signature_changes_when_limits_or_lease_contents_change_at_same_mtime(self):
        self.harness.build_fixture_repo(with_parent_env=self.ENV_WITH_IDENTITY)
        pinned = 1700000000.0
        limits = os.path.join(self.harness.state_dir, "limits.json")
        lease = self.harness.lease_path("slot-1")
        parked = self.harness.parked_path("slot-2")
        limits_a = '{"machine_max": 0}'
        limits_b = '{"machine_max": 1}'
        lease_a = '{"holder": "alpha", "since": 1700000000}'
        lease_b = '{"holder": "bravo", "since": 1700000000}'
        parked_a = '{"holder": "alpha", "reason": "dirty"}'
        parked_b = '{"holder": "bravo", "reason": "dirty"}'
        self.assertEqual(len(limits_a), len(limits_b))
        self.assertEqual(len(lease_a), len(lease_b))
        self.assertEqual(len(parked_a), len(parked_b))

        self._write_pinned(limits, limits_a, pinned)
        self._write_pinned(lease, lease_a, pinned)
        self._write_pinned(parked, parked_a, pinned)
        sig_base = self._signature()

        self._write_pinned(limits, limits_b, pinned)
        sig_limits = self._signature()
        self.assertNotEqual(sig_limits, sig_base)

        self._write_pinned(limits, limits_a, pinned)
        self._write_pinned(lease, lease_b, pinned)
        sig_lease = self._signature()
        self.assertNotEqual(sig_lease, sig_base)

        self._write_pinned(lease, lease_a, pinned)
        self._write_pinned(parked, parked_b, pinned)
        sig_parked = self._signature()
        self.assertNotEqual(sig_parked, sig_base)

        os.remove(limits)
        sig_no_limits = self._signature()
        self.assertNotEqual(sig_no_limits, sig_base)


if __name__ == "__main__":
    unittest.main()
