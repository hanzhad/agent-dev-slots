import json
import os
import subprocess
import sys
import unittest

if os.path.dirname(__file__) not in sys.path:
    sys.path.insert(0, os.path.dirname(__file__))

from harness import (
    DevContainerTestCase,
    PROJECT_PREFIX,
    REPO_ROOT,
    wait_until,
)

MAIN_ENV = (
    "GIT_USER_NAME=demo-agent\n"
    "GIT_USER_EMAIL=demo-agent@example.com\n"
)
ACTIVITY_SH = os.path.join(REPO_ROOT, "lib", "activity.sh")


class TestLeaseEffectiveIdleLimitPureCore(unittest.TestCase):
    """Pure Core unit tests for lease_effective_idle_limit in lib/activity.sh."""

    def test_lease_effective_idle_limit_table(self):
        probe = subprocess.run(
            ["bash", "-c", '. "$1" && declare -F lease_effective_idle_limit >/dev/null', "test", ACTIVITY_SH],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=15,
        )
        self.assertEqual(
            probe.returncode,
            0,
            f"lease_effective_idle_limit is not defined in {ACTIVITY_SH}: {probe.stderr.decode()}",
        )

        cases = [
            ("7200", "900", "900"),
            ("300", "900", "300"),
            ("900", "900", "900"),
            ("7200", "1", "1"),
            ("7200", "7199", "7199"),
            ("7200", "7201", "7200"),
            ("1", "1", "1"),
            ("1", "900", "1"),
            ("7200", "0", "7200"),
            ("7200", "-1", "7200"),
            ("7200", "-900", "7200"),
            ("7200", "", "7200"),
            ("7200", "null", "7200"),
            ("7200", "abc", "7200"),
            ("7200", "900.5", "7200"),
            ("7200", "900s", "7200"),
        ]
        for machine_limit, lease_limit, expected in cases:
            with self.subTest(machine_limit=machine_limit, lease_limit=lease_limit):
                res = subprocess.run(
                    [
                        "bash",
                        "-c",
                        '. "$1" && lease_effective_idle_limit "$2" "$3"',
                        "test",
                        ACTIVITY_SH,
                        machine_limit,
                        lease_limit,
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=15,
                )
                self.assertEqual(res.returncode, 0, res.stderr.decode())
                self.assertEqual(res.stdout.decode().strip(), expected)


class _EphemeralTestCase(DevContainerTestCase):

    def setUp(self):
        super().setUp()
        self.rescue_dir = os.path.join(self.harness.state_dir, "rescue")

    def _setup_repo_with_remote(self):
        self.harness.build_fixture_repo(with_parent_env=MAIN_ENV)
        self.remote = os.path.join(self.harness.tmp, "remote.git")
        git = self.harness.real_git_bin
        subprocess.run(
            [git, "init", "--bare", "-q", self.remote],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        subprocess.run(
            [git, "-C", self.remote, "symbolic-ref", "HEAD", "refs/heads/master"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        subprocess.run(
            [git, "-C", self.harness.fixture_repo, "remote", "set-url", "origin", self.remote],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        subprocess.run(
            [git, "-C", self.harness.fixture_repo, "branch", "-M", "master"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        subprocess.run(
            [git, "-C", self.harness.fixture_repo, "push", "-q", "origin", "master"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def _write_limits(self, **limits):
        path = os.path.join(self.harness.state_dir, "limits.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(limits, handle)

    def _read_lease(self, name):
        path = self.harness.lease_path(name)
        self.assertTrue(os.path.isfile(path), f"lease file missing: {path}")
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)

    def _token_path(self, name):
        return os.path.join(self.harness.state_dir, "lease-tokens", f"{PROJECT_PREFIX}-{name}.token")

    def _rescue_files(self):
        found = []
        if not os.path.isdir(self.rescue_dir):
            return found
        for root, _dirs, files in os.walk(self.rescue_dir):
            for fname in files:
                found.append(os.path.join(root, fname))
        return sorted(found)

    def _status_json(self):
        res = self.harness.run("status", "--json")
        self.assertEqual(res.returncode, 0, res.stderr.decode())
        return json.loads(res.stdout.decode())

    def _docker_log(self):
        if not os.path.isfile(self.harness.docker_log):
            return ""
        with open(self.harness.docker_log, encoding="utf-8") as handle:
            return handle.read()

    def _git_slot(self, slot_dir, *args):
        return subprocess.run(
            [
                self.harness.real_git_bin,
                "-C",
                slot_dir,
                "-c",
                "commit.gpgsign=false",
                "-c",
                "user.email=fixture@example.com",
                "-c",
                "user.name=fixture",
                *args,
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def _make_slot_dirty(self, name):
        slot_dir = self.harness.slot_dir(name)
        self._git_slot(slot_dir, "checkout", "-q", "-b", "unpushed-wip")
        with open(os.path.join(slot_dir, "branch_work.txt"), "w", encoding="utf-8") as handle:
            handle.write("unpushed commit data\n")
        self._git_slot(slot_dir, "add", "branch_work.txt")
        self._git_slot(slot_dir, "commit", "-q", "-m", "unpushed wip")

        with open(os.path.join(slot_dir, "untracked_artifact.txt"), "w", encoding="utf-8") as handle:
            handle.write("untracked artifact\n")
        with open(os.path.join(slot_dir, ".gitignore"), "a", encoding="utf-8") as handle:
            handle.write("temp_dirty_line\n")

    def _lease_is_live(self, name):
        env = self.harness._env(None)
        env["DEV_CONTAINER_LIB"] = "1"
        res = subprocess.run(
            [
                "bash",
                "-c",
                '. "$0"; idle_limit_load; lease_is_live "$1"',
                self.harness.script,
                self.harness.lease_path(name),
            ],
            cwd=self.harness.tmp,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )
        return res.returncode == 0

    def _machine_in_use(self):
        env = self.harness._env(None)
        env["DEV_CONTAINER_LIB"] = "1"
        res = subprocess.run(
            [
                "bash",
                "-c",
                '. "$0"; idle_limit_load; machine_in_use',
                self.harness.script,
            ],
            cwd=self.harness.tmp,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )
        self.assertEqual(res.returncode, 0, res.stderr.decode())
        return int(res.stdout.decode().strip())


class TestAcquireEphemeral(_EphemeralTestCase):

    def test_acquire_ephemeral_writes_reservation_and_lease_json_and_exposes_status_json(self):
        self.harness.build_fixture_repo(with_parent_env=MAIN_ENV)
        self.harness.set_now(1700000000)

        self.harness.hold_compose_up("slot-1")
        self.addCleanup(self.harness.let_compose_up_go, "slot-1")

        log = os.path.join(self.harness.tmp, "eph-acq.log")
        proc = self.harness.spawn(
            "acquire",
            "--ephemeral",
            "--for",
            "eph-holder",
            "--as",
            "Agent [eph1]",
            log=log,
        )

        def cleanup_proc():
            self.harness.let_compose_up_go("slot-1")
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=30)

        self.addCleanup(cleanup_proc)

        def compose_up_started():
            if proc.poll() is not None and not os.path.isfile(
                os.path.join(self.harness.docker_state, f"{PROJECT_PREFIX}-slot-1.up-held")
            ):
                out = ""
                if os.path.isfile(log):
                    with open(log, encoding="utf-8") as handle:
                        out = handle.read()
                raise AssertionError(f"acquire --ephemeral exited early (rc={proc.returncode}): {out}")
            return os.path.isfile(
                os.path.join(self.harness.docker_state, f"{PROJECT_PREFIX}-slot-1.up-held")
            )

        wait_until(
            compose_up_started,
            timeout=30,
            interval=0.02,
            message="compose up for demo-slot-1 never started",
        )

        res_data = self._read_lease("slot-1")
        self.assertIs(res_data.get("ephemeral"), True, res_data)
        self.assertEqual(res_data.get("idle_limit"), 900, res_data)

        self.harness.let_compose_up_go("slot-1")
        rc = proc.wait(timeout=30)
        with open(log, encoding="utf-8") as handle:
            acq_out = handle.read()
        self.assertEqual(rc, 0, acq_out)

        lease = self._read_lease("slot-1")
        self.assertEqual(lease.get("holder"), "eph-holder")
        self.assertIs(lease.get("ephemeral"), True, lease)
        self.assertEqual(lease.get("idle_limit"), 900, lease)

        norm_res = self.harness.run("acquire", "--for", "norm-holder", "--as", "Agent [norm1]")
        self.assertEqual(norm_res.returncode, 0, norm_res.stderr.decode())
        norm_name = self.harness.acquired_slot_name(norm_res)
        norm_lease = self._read_lease(norm_name)
        self.assertFalse(norm_lease.get("ephemeral"), norm_lease)

        status = self._status_json()
        by_slot = {entry["slot"]: entry for entry in status.get("slots", [])}
        eph_entry = by_slot.get(f"{PROJECT_PREFIX}-slot-1")
        norm_entry = by_slot.get(f"{PROJECT_PREFIX}-{norm_name}")
        self.assertIsNotNone(eph_entry, status)
        self.assertIs(eph_entry.get("ephemeral"), True, eph_entry)
        self.assertEqual(eph_entry.get("idle_limit"), 900, eph_entry)
        self.assertIsNotNone(norm_entry, status)
        self.assertFalse(norm_entry.get("ephemeral"), norm_entry)

    def test_ephemeral_lease_expires_at_900_and_901_while_live_at_899(self):
        self.harness.build_fixture_repo(with_parent_env=MAIN_ENV)
        self._write_limits(machine_max=1, idle_limit=7200)
        since = 1700000000
        self.harness.set_now(since)

        acq = self.harness.run("acquire", "--ephemeral", "--for", "eph-ttl", "--as", "Agent [u1]")
        self.assertEqual(acq.returncode, 0, acq.stderr.decode())
        name = self.harness.acquired_slot_name(acq)

        self.harness.set_now(since + 899)
        self.assertTrue(self._lease_is_live(name))
        self.assertEqual(self._machine_in_use(), 1)
        self.assertEqual(len(self._status_json().get("slots", [])), 1)
        blocked = self.harness.run("acquire", "--for", "next-1", "--as", "Agent [u2]")
        self.assertEqual(blocked.returncode, 2, blocked.stderr.decode())

        self.harness.set_now(since + 900)
        self.assertFalse(self._lease_is_live(name))
        self.assertEqual(self._machine_in_use(), 0)
        self.assertEqual(self._status_json().get("slots", []), [])

        self.harness.set_now(since + 901)
        self.assertFalse(self._lease_is_live(name))
        self.assertEqual(self._machine_in_use(), 0)
        self.assertEqual(self._status_json().get("slots", []), [])
        next_acq = self.harness.run("acquire", "--for", "next-2", "--as", "Agent [u2]")
        self.assertEqual(next_acq.returncode, 0, next_acq.stderr.decode())

    def test_ephemeral_lease_cannot_extend_past_shorter_machine_idle_limit(self):
        self.harness.build_fixture_repo(with_parent_env=MAIN_ENV)
        self._write_limits(machine_max=1, idle_limit=300)
        since = 1700000000
        self.harness.set_now(since)

        acq = self.harness.run("acquire", "--ephemeral", "--for", "eph-short", "--as", "Agent [u1]")
        self.assertEqual(acq.returncode, 0, acq.stderr.decode())
        name = self.harness.acquired_slot_name(acq)

        self.harness.set_now(since + 299)
        self.assertTrue(self._lease_is_live(name))
        self.assertEqual(self._machine_in_use(), 1)
        blocked = self.harness.run("acquire", "--for", "other", "--as", "Agent [u2]")
        self.assertEqual(blocked.returncode, 2, blocked.stderr.decode())

        self.harness.set_now(since + 300)
        self.assertFalse(self._lease_is_live(name))
        self.assertEqual(self._machine_in_use(), 0)
        allowed = self.harness.run("acquire", "--for", "other", "--as", "Agent [u2]")
        self.assertEqual(allowed.returncode, 0, allowed.stderr.decode())

    def test_ephemeral_activity_extends_by_900s_not_machine_7200s(self):
        self.harness.build_fixture_repo(with_parent_env=MAIN_ENV)
        self._write_limits(machine_max=1, idle_limit=7200)
        since = 1700000000
        self.harness.set_now(since)

        acq = self.harness.run("acquire", "--ephemeral", "--for", "eph-act", "--as", "Agent [u1]")
        self.assertEqual(acq.returncode, 0, acq.stderr.decode())
        name = self.harness.acquired_slot_name(acq)

        self.harness.set_now(since + 500)
        ran = self.harness.run("run", name, "--", "true")
        self.assertEqual(ran.returncode, 0, ran.stderr.decode())

        self.harness.set_now(since + 901)
        self.assertTrue(self._lease_is_live(name))
        self.assertEqual(self._machine_in_use(), 1)

        self.harness.set_now(since + 1400)
        self.assertFalse(self._lease_is_live(name))
        self.assertEqual(self._machine_in_use(), 0)

    def test_ephemeral_expiry_leaves_concurrent_non_ephemeral_slot_live(self):
        self.harness.build_fixture_repo(with_parent_env=MAIN_ENV)
        self._write_limits(machine_max=2, idle_limit=7200)
        since = 1700000000
        self.harness.set_now(since)

        keep = self.harness.run("acquire", "--for", "keep-holder", "--as", "Agent [u1]")
        self.assertEqual(keep.returncode, 0, keep.stderr.decode())
        keep_name = self.harness.acquired_slot_name(keep)

        drop = self.harness.run("acquire", "--ephemeral", "--for", "drop-holder", "--as", "Agent [u2]")
        self.assertEqual(drop.returncode, 0, drop.stderr.decode())
        drop_name = self.harness.acquired_slot_name(drop)

        self.harness.set_now(since + 901)
        slots = self._status_json().get("slots", [])
        self.assertEqual([s["slot"] for s in slots], [f"{PROJECT_PREFIX}-{keep_name}"])

        reaped = self.harness.run("reap")
        self.assertEqual(reaped.returncode, 0, reaped.stderr.decode())
        self.assertFalse(self.harness.volume_exists(drop_name))
        self.assertFalse(os.path.exists(self.harness.lease_path(drop_name)))
        self.assertTrue(self.harness.volume_exists(keep_name))
        self.assertTrue(os.path.isfile(self.harness.lease_path(keep_name)))
        self.assertEqual(self._rescue_files(), [])


class TestReleaseDiscardAndEphemeralTeardown(_EphemeralTestCase):

    def test_release_discard_refuses_wrong_holder_and_missing_for_without_removing_slot(self):
        self._setup_repo_with_remote()
        acq = self.harness.run("acquire", "--for", "real-holder", "--as", "Agent [u1]")
        self.assertEqual(acq.returncode, 0, acq.stderr.decode())
        name = self.harness.acquired_slot_name(acq)
        self._make_slot_dirty(name)

        lease = self._read_lease(name)
        key_path = lease["key"]
        tok_path = self._token_path(name)
        self.assertTrue(os.path.isfile(key_path))
        self.assertTrue(os.path.isfile(tok_path))

        if os.path.exists(self.harness.docker_log):
            os.remove(self.harness.docker_log)

        wrong = self.harness.run("release", name, "--for", "wrong-holder", "--discard")
        wrong_err = wrong.stderr.decode()
        self.assertNotEqual(wrong.returncode, 0, wrong.stdout.decode() + wrong_err)
        self.assertNotIn("unknown flag", wrong_err)
        self.assertIn("real-holder", wrong_err)

        missing_for = self.harness.run("release", name, "--discard")
        missing_err = missing_for.stderr.decode()
        self.assertNotEqual(missing_for.returncode, 0, missing_for.stdout.decode() + missing_err)
        self.assertNotIn("unknown flag", missing_err)

        self.assertTrue(self.harness.volume_exists(name))
        self.assertTrue(os.path.isfile(self.harness.lease_path(name)))
        self.assertTrue(os.path.isfile(tok_path))
        self.assertTrue(os.path.isfile(key_path))
        self.assertTrue(os.path.isfile(key_path + ".pub"))
        self.assertNotIn(" down ", self._docker_log())

    def test_release_discard_on_dirty_non_ephemeral_slot_tears_down_with_zero_rescue(self):
        self._setup_repo_with_remote()
        acq = self.harness.run("acquire", "--for", "dirty-holder", "--as", "Agent [u1]")
        self.assertEqual(acq.returncode, 0, acq.stderr.decode())
        name = self.harness.acquired_slot_name(acq)
        self._make_slot_dirty(name)
        key_path = self._read_lease(name)["key"]
        tok_path = self._token_path(name)

        plain_rel = self.harness.run("release", name, "--for", "dirty-holder")
        self.assertNotEqual(plain_rel.returncode, 0, plain_rel.stdout.decode() + plain_rel.stderr.decode())
        self.assertTrue(self.harness.volume_exists(name))

        if os.path.exists(self.harness.docker_log):
            os.remove(self.harness.docker_log)

        rel = self.harness.run("release", name, "--for", "dirty-holder", "--discard")
        self.assertEqual(rel.returncode, 0, rel.stdout.decode() + rel.stderr.decode())

        self.assertFalse(self.harness.volume_exists(name))
        self.assertFalse(os.path.exists(self.harness.lease_path(name)))
        self.assertFalse(os.path.exists(tok_path))
        self.assertFalse(os.path.exists(key_path))
        self.assertFalse(os.path.exists(key_path + ".pub"))
        self.assertEqual(self._rescue_files(), [])
        down_lines = [ln for ln in self._docker_log().splitlines() if " down " in ln]
        self.assertTrue(any("-v" in ln for ln in down_lines), down_lines)

        reacq = self.harness.run("acquire", "--for", "reused", "--as", "Agent [u1]")
        self.assertEqual(reacq.returncode, 0, reacq.stderr.decode())
        self.assertEqual(self.harness.acquired_slot_name(reacq), name)

    def test_release_ephemeral_without_discard_flag_bypasses_dirty_checks_with_zero_rescue(self):
        self._setup_repo_with_remote()
        acq = self.harness.run("acquire", "--ephemeral", "--for", "eph-rel", "--as", "Agent [u1]")
        self.assertEqual(acq.returncode, 0, acq.stderr.decode())
        name = self.harness.acquired_slot_name(acq)
        self._make_slot_dirty(name)
        key_path = self._read_lease(name)["key"]
        tok_path = self._token_path(name)

        wrong = self.harness.run("release", name, "--for", "other-holder")
        self.assertNotEqual(wrong.returncode, 0, wrong.stderr.decode())
        self.assertTrue(self.harness.volume_exists(name))

        rel = self.harness.run("release", name, "--for", "eph-rel")
        self.assertEqual(rel.returncode, 0, rel.stdout.decode() + rel.stderr.decode())
        self.assertFalse(self.harness.volume_exists(name))
        self.assertFalse(os.path.exists(self.harness.lease_path(name)))
        self.assertFalse(os.path.exists(tok_path))
        self.assertFalse(os.path.exists(key_path))
        self.assertFalse(os.path.exists(key_path + ".pub"))
        self.assertEqual(self._rescue_files(), [])

    def test_reap_and_clean_on_expired_dirty_ephemeral_slot_write_zero_rescue(self):
        self._setup_repo_with_remote()
        since = 1700000000
        self.harness.set_now(since)

        acq1 = self.harness.run("acquire", "--ephemeral", "--for", "eph-reap", "--as", "Agent [u1]")
        self.assertEqual(acq1.returncode, 0, acq1.stderr.decode())
        name1 = self.harness.acquired_slot_name(acq1)
        self._make_slot_dirty(name1)
        key1 = self._read_lease(name1)["key"]
        tok1 = self._token_path(name1)

        self.harness.set_now(since + 901)
        self.assertFalse(self._lease_is_live(name1))
        self.assertEqual(self._machine_in_use(), 0)

        reap_res = self.harness.run("reap")
        self.assertEqual(reap_res.returncode, 0, reap_res.stdout.decode() + reap_res.stderr.decode())
        self.assertFalse(self.harness.volume_exists(name1))
        self.assertFalse(os.path.exists(self.harness.lease_path(name1)))
        self.assertFalse(os.path.exists(tok1))
        self.assertFalse(os.path.exists(key1))
        self.assertFalse(os.path.exists(key1 + ".pub"))
        self.assertEqual(self._rescue_files(), [])

        self.harness.set_now(since + 1000)
        acq2 = self.harness.run("acquire", "--ephemeral", "--for", "eph-clean", "--as", "Agent [u1]")
        self.assertEqual(acq2.returncode, 0, acq2.stderr.decode())
        name2 = self.harness.acquired_slot_name(acq2)
        self.assertEqual(name2, name1)
        self._make_slot_dirty(name2)

        self.harness.set_now(since + 1901)
        clean_res = self.harness.run("clean")
        self.assertEqual(clean_res.returncode, 0, clean_res.stderr.decode())
        self.assertFalse(self.harness.volume_exists(name2))
        self.assertEqual(self._rescue_files(), [])

        self.harness.set_now(since + 2000)
        norm = self.harness.run("acquire", "--for", "norm-reap", "--as", "Agent [u2]")
        self.assertEqual(norm.returncode, 0, norm.stderr.decode())
        norm_name = self.harness.acquired_slot_name(norm)
        self._make_slot_dirty(norm_name)
        self.harness.set_now(since + 2000 + 7201)
        reap_norm = self.harness.run("reap")
        self.assertEqual(reap_norm.returncode, 0, reap_norm.stderr.decode())
        self.assertFalse(self.harness.volume_exists(norm_name))
        self.assertTrue(len(self._rescue_files()) > 0, "non-ephemeral dirty slot should still be rescued")

    def test_archive_and_clean_slot_directly_discards_ephemeral_lease_without_rescue(self):
        self._setup_repo_with_remote()
        since = 1700000000
        self.harness.set_now(since)
        acq = self.harness.run("acquire", "--for", "direct-eph", "--as", "Agent [u1]")
        self.assertEqual(acq.returncode, 0, acq.stderr.decode())
        name = self.harness.acquired_slot_name(acq)
        self._make_slot_dirty(name)

        lease_path = self.harness.lease_path(name)
        lease = self._read_lease(name)
        lease["ephemeral"] = True
        lease["idle_limit"] = 900
        with open(lease_path, "w", encoding="utf-8") as handle:
            json.dump(lease, handle)

        self.harness.set_now(since + 901)
        env = self.harness._env(None)
        env["DEV_CONTAINER_LIB"] = "1"
        sid = f"{PROJECT_PREFIX}-{name}"
        res = subprocess.run(
            ["bash", "-c", '. "$0"; archive_and_clean_slot "" "$1" "$2"', self.harness.script, sid, name],
            cwd=self.harness.tmp,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
        )
        self.assertEqual(res.returncode, 0, res.stderr.decode())
        self.assertFalse(self.harness.volume_exists(name))
        self.assertFalse(os.path.exists(lease_path))
        self.assertFalse(os.path.exists(self._token_path(name)))
        self.assertFalse(os.path.exists(lease["key"]))
        self.assertEqual(self._rescue_files(), [])


if __name__ == "__main__":
    unittest.main()
