import json
import os
import re
import shutil
import subprocess
import time
import unittest

from harness import (
    DevContainerTestCase,
    PROJECT_PREFIX,
    project_env_text,
)

MAIN_ENV = "GIT_USER_NAME=demo-agent\nGIT_USER_EMAIL=demo-agent@example.com\n"


class VolumeCreationAndLabels(DevContainerTestCase):
    def test_acquire_creates_named_volume_for_slot(self):
        self.harness.build_fixture_repo(with_parent_env=MAIN_ENV)
        result = self.harness.run("acquire", "--for", "test-holder")
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        name = self.harness.acquired_slot_name(result)
        expected_vol = f"{PROJECT_PREFIX}-{name}-workspace"
        self.assertEqual(self.harness.volume_names(), [expected_vol])
        self.assertTrue(self.harness.volume_exists(name))

    def test_volume_carries_mandatory_ownership_labels(self):
        self.harness.build_fixture_repo(with_parent_env=MAIN_ENV)
        result = self.harness.run("acquire", "--for", "test-holder")
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        name = self.harness.acquired_slot_name(result)
        labels = self.harness.volume_labels(name)
        sid = f"{PROJECT_PREFIX}-{name}"
        self.assertEqual(labels.get("dev-container.sid"), sid)
        self.assertEqual(labels.get("dev-container.project"), PROJECT_PREFIX)
        self.assertEqual(labels.get("dev-container.origin"), "https://example.invalid/fixture.git")
        self.assertEqual(labels.get("dev-container.repo-root"), self.harness.fixture_repo)

    def test_acquire_leaves_no_host_directory_under_slots_dir(self):
        self.harness.build_fixture_repo(with_parent_env=MAIN_ENV)
        result = self.harness.run("acquire", "--for", "test-holder")
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        slots_dir = self.harness.slots_dir
        if os.path.isdir(slots_dir):
            self.assertEqual(os.listdir(slots_dir), [], "acquire created a directory on the host under slots_dir")


class VolumeOverlay(DevContainerTestCase):
    def test_slot_volume_overlay_is_generated_under_state(self):
        self.harness.build_fixture_repo(with_parent_env=MAIN_ENV)
        result = self.harness.run("acquire", "--for", "test-holder")
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        overlay_path = os.path.join(self.harness.state_dir, "compose", "slot-volume.yml")
        self.assertTrue(os.path.isfile(overlay_path), "slot-volume.yml was not created under $STATE/compose")
        with open(overlay_path, encoding="utf-8") as handle:
            content = handle.read()
        self.assertIn("slot-workspace:", content)
        self.assertIn("external: true", content)
        self.assertIn("${SLOT_WORKSPACE_VOLUME}", content)

    def test_compose_args_includes_slot_volume_overlay_and_sets_workspace_dir(self):
        self.harness.build_fixture_repo(with_parent_env=MAIN_ENV)
        result = self.harness.run("acquire", "--for", "test-holder")
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        name = self.harness.acquired_slot_name(result)
        calls = self.harness.compose_calls()
        up_calls = [c for c in calls if c["verb"] == "up"]
        self.assertTrue(up_calls, "no compose up calls recorded")
        for call in up_calls:
            self.assertEqual(call["workspace"], "slot-workspace")
            self.assertTrue(any("slot-volume.yml" in f for f in call["files"]), f"slot-volume.yml not in compose files: {call['files']}")


class VolumeDeleteGuard(DevContainerTestCase):
    def _call_teardown(self, sid):
        h = self.harness
        env = h._env(None)
        env["DEV_CONTAINER_LIB"] = "1"
        body = (
            '. "$0"; '
            'if teardown_dir "" "$1"; then echo "RC=0"; else echo "RC=$?"; fi; '
            'echo REACHED'
        )
        result = subprocess.run(
            ["bash", "-c", body, h.script, sid],
            cwd=h.tmp, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60,
        )
        return result.stdout.decode()

    def test_teardown_removes_volume_with_matching_labels(self):
        self.harness.build_fixture_repo()
        sid = f"{PROJECT_PREFIX}-slot-1"
        vol = f"{sid}-workspace"
        self.harness.write_volume_labels(vol, {
            "dev-container.sid": sid,
            "dev-container.project": PROJECT_PREFIX,
            "dev-container.origin": "https://example.invalid/fixture.git",
            "dev-container.repo-root": self.harness.fixture_repo,
        })
        self.assertTrue(self.harness.volume_exists("slot-1"))
        output = self._call_teardown(sid)
        self.assertIn("RC=0", output)
        self.assertFalse(self.harness.volume_exists("slot-1"))

    def test_teardown_refuses_volume_with_different_project_label(self):
        self.harness.build_fixture_repo()
        sid = f"{PROJECT_PREFIX}-slot-1"
        vol = f"{sid}-workspace"
        self.harness.write_volume_labels(vol, {
            "dev-container.sid": sid,
            "dev-container.project": "foreign-project",
            "dev-container.origin": "https://example.invalid/fixture.git",
            "dev-container.repo-root": self.harness.fixture_repo,
        })
        output = self._call_teardown(sid)
        self.assertIn("RC=1", output)
        self.assertTrue(self.harness.volume_exists("slot-1"), "foreign project volume was deleted")

    def test_teardown_refuses_volume_with_different_sid_label(self):
        self.harness.build_fixture_repo()
        sid = f"{PROJECT_PREFIX}-slot-1"
        vol = f"{sid}-workspace"
        self.harness.write_volume_labels(vol, {
            "dev-container.sid": f"{PROJECT_PREFIX}-slot-99",
            "dev-container.project": PROJECT_PREFIX,
            "dev-container.origin": "https://example.invalid/fixture.git",
            "dev-container.repo-root": self.harness.fixture_repo,
        })
        output = self._call_teardown(sid)
        self.assertIn("RC=1", output)
        self.assertTrue(self.harness.volume_exists("slot-1"), "wrong sid volume was deleted")

    def test_teardown_refuses_volume_with_different_origin(self):
        self.harness.build_fixture_repo()
        sid = f"{PROJECT_PREFIX}-slot-1"
        vol = f"{sid}-workspace"
        self.harness.write_volume_labels(vol, {
            "dev-container.sid": sid,
            "dev-container.project": PROJECT_PREFIX,
            "dev-container.origin": "https://example.invalid/other-origin.git",
            "dev-container.repo-root": self.harness.fixture_repo,
        })
        output = self._call_teardown(sid)
        self.assertIn("RC=1", output)
        self.assertTrue(self.harness.volume_exists("slot-1"), "foreign origin volume was deleted")


class VolumeSlotSafety(DevContainerTestCase):
    def test_slot_is_ours_returns_true_for_our_volume(self):
        self.harness.build_fixture_repo()
        sid = f"{PROJECT_PREFIX}-slot-1"
        vol = f"{sid}-workspace"
        self.harness.write_volume_labels(vol, {
            "dev-container.sid": sid,
            "dev-container.project": PROJECT_PREFIX,
            "dev-container.origin": "https://example.invalid/fixture.git",
        })
        h = self.harness
        env = h._env(None)
        env["DEV_CONTAINER_LIB"] = "1"
        res = subprocess.run(
            ["bash", "-c", '. "$0"; slot_is_ours "$1"', h.script, sid],
            cwd=h.tmp, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self.assertEqual(res.returncode, 0)

    def test_slot_is_ours_returns_false_for_foreign_volume(self):
        self.harness.build_fixture_repo()
        sid = f"{PROJECT_PREFIX}-slot-1"
        vol = f"{sid}-workspace"
        self.harness.write_volume_labels(vol, {
            "dev-container.sid": sid,
            "dev-container.project": "other-project",
            "dev-container.origin": "https://example.invalid/fixture.git",
        })
        h = self.harness
        env = h._env(None)
        env["DEV_CONTAINER_LIB"] = "1"
        res = subprocess.run(
            ["bash", "-c", '. "$0"; slot_is_ours "$1"', h.script, sid],
            cwd=h.tmp, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self.assertNotEqual(res.returncode, 0)

    def test_default_slot_name_skips_slot_whose_volume_already_exists(self):
        self.harness.build_fixture_repo()
        sid1 = f"{PROJECT_PREFIX}-slot-1"
        vol1 = f"{sid1}-workspace"
        self.harness.write_volume_labels(vol1, {
            "dev-container.sid": sid1,
            "dev-container.project": PROJECT_PREFIX,
        })
        h = self.harness
        env = h._env(None)
        env["DEV_CONTAINER_LIB"] = "1"
        res = subprocess.run(
            ["bash", "-c", '. "$0"; default_slot_name', h.script],
            cwd=h.tmp, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self.assertEqual(res.returncode, 0)
        self.assertEqual(res.stdout.decode().strip(), "slot-2")


class PutStreamsFilesIntoASlot(DevContainerTestCase):
    def _src(self):
        src = os.path.join(self.harness.tmp, "put-src")
        os.makedirs(os.path.join(src, "nested"), exist_ok=True)
        with open(os.path.join(src, "top.txt"), "w", encoding="utf-8") as handle:
            handle.write("top\n")
        with open(os.path.join(src, "nested", "deep.txt"), "w", encoding="utf-8") as handle:
            handle.write("deep\n")
        return src

    def test_put_copies_a_file_to_the_destination_in_the_slot(self):
        self.harness.build_fixture_repo(with_parent_env=MAIN_ENV)
        acq = self.harness.run("acquire", "--for", "test-put")
        self.assertEqual(acq.returncode, 0, acq.stderr.decode())
        name = self.harness.acquired_slot_name(acq)
        src = os.path.join(self._src(), "top.txt")

        result = self.harness.run("put", name, src, ".local/sub/copied.txt")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        slot_dir = self.harness.slot_dir(name)
        with open(os.path.join(slot_dir, ".local", "sub", "copied.txt"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "top\n")

    def test_put_copies_a_directory_with_its_contents(self):
        self.harness.build_fixture_repo(with_parent_env=MAIN_ENV)
        acq = self.harness.run("acquire", "--for", "test-put")
        self.assertEqual(acq.returncode, 0, acq.stderr.decode())
        name = self.harness.acquired_slot_name(acq)

        result = self.harness.run("put", name, self._src(), ".local/tree")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        slot_dir = self.harness.slot_dir(name)
        with open(os.path.join(slot_dir, ".local", "tree", "top.txt"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "top\n")
        with open(os.path.join(slot_dir, ".local", "tree", "nested", "deep.txt"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "deep\n")

    def test_put_refuses_a_destination_that_climbs_out_before_anything_runs(self):
        self.harness.build_fixture_repo(with_parent_env=MAIN_ENV)
        acq = self.harness.run("acquire", "--for", "test-put")
        self.assertEqual(acq.returncode, 0, acq.stderr.decode())
        name = self.harness.acquired_slot_name(acq)
        src = os.path.join(self._src(), "top.txt")

        result = self.harness.run("put", name, src, "../escaped.txt")

        self.assertNotEqual(result.returncode, 0, result.stdout.decode())
        self.assertIn("..", result.stderr.decode())

    def test_put_refuses_absolute_destination(self):
        self.harness.build_fixture_repo(with_parent_env=MAIN_ENV)
        acq = self.harness.run("acquire", "--for", "test-put")
        self.assertEqual(acq.returncode, 0, acq.stderr.decode())
        name = self.harness.acquired_slot_name(acq)
        src = os.path.join(self._src(), "top.txt")

        result = self.harness.run("put", name, src, "/workspace/abs.txt")

        self.assertNotEqual(result.returncode, 0, result.stdout.decode())
        self.assertIn("relative", result.stderr.decode())

    def test_put_refuses_a_source_that_does_not_exist(self):
        self.harness.build_fixture_repo(with_parent_env=MAIN_ENV)
        acq = self.harness.run("acquire", "--for", "test-put")
        self.assertEqual(acq.returncode, 0, acq.stderr.decode())
        name = self.harness.acquired_slot_name(acq)

        result = self.harness.run("put", name, os.path.join(self.harness.tmp, "nonexistent"), "x.txt")

        self.assertNotEqual(result.returncode, 0, result.stdout.decode())
        self.assertIn("no such file", result.stderr.decode())

    def test_put_refuses_an_unleased_slot(self):
        self.harness.build_fixture_repo(with_parent_env=MAIN_ENV)
        acq = self.harness.run("acquire", "--for", "test-put")
        name = self.harness.acquired_slot_name(acq)
        os.remove(self.harness.lease_path(name))
        src = os.path.join(self._src(), "top.txt")

        result = self.harness.run("put", name, src, "test.txt")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not leased", result.stderr.decode())

    def test_put_via_stdin_stream(self):
        self.harness.build_fixture_repo(with_parent_env=MAIN_ENV)
        acq = self.harness.run("acquire", "--for", "test-put")
        name = self.harness.acquired_slot_name(acq)

        result = self.harness.run("put", name, "--stdin", ".local/stdin.txt", input=b"streamed-content\n")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        slot_dir = self.harness.slot_dir(name)
        with open(os.path.join(slot_dir, ".local", "stdin.txt"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "streamed-content\n")

    def test_put_into_stopped_slot_via_helper(self):
        self.harness.build_fixture_repo(with_parent_env=MAIN_ENV)
        acq = self.harness.run("acquire", "--for", "test-put")
        name = self.harness.acquired_slot_name(acq)
        self.harness.mark_stopped(name)
        src = os.path.join(self._src(), "top.txt")

        result = self.harness.run("put", name, src, ".local/stopped.txt")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        slot_dir = self.harness.slot_dir(name)
        with open(os.path.join(slot_dir, ".local", "stopped.txt"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "top\n")


class SlotCopyFilesAndOverlays(DevContainerTestCase):
    def test_acquire_copies_slot_copy_files_into_volume(self):
        project_env = project_env_text(SLOT_COPY_FILES="config/custom.json extra.txt")
        self.harness.build_fixture_repo(with_parent_env=MAIN_ENV, project_env=project_env)
        cfg_dir = os.path.join(self.harness.fixture_repo, "config")
        os.makedirs(cfg_dir, exist_ok=True)
        with open(os.path.join(cfg_dir, "custom.json"), "w", encoding="utf-8") as f:
            f.write('{"custom": true}\n')
        with open(os.path.join(self.harness.fixture_repo, "extra.txt"), "w", encoding="utf-8") as f:
            f.write("extra-data\n")

        acq = self.harness.run("acquire", "--for", "test-holder")
        self.assertEqual(acq.returncode, 0, acq.stderr.decode())
        name = self.harness.acquired_slot_name(acq)
        slot_dir = self.harness.slot_dir(name)

        with open(os.path.join(slot_dir, "config", "custom.json"), encoding="utf-8") as f:
            self.assertEqual(f.read(), '{"custom": true}\n')
        with open(os.path.join(slot_dir, "extra.txt"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "extra-data\n")

    def test_up_with_secrets_puts_overlay_files_into_slot(self):
        self.harness.build_fixture_repo(with_parent_env=MAIN_ENV)
        with open(os.path.join(self.harness.main_devcontainer(), "docker-compose.secrets.yml"), "w", encoding="utf-8") as f:
            f.write("services: {}\n")
        secret_file = os.path.join(self.harness.fixture_repo, ".env.test.local")
        with open(secret_file, "w", encoding="utf-8") as f:
            f.write("API_SECRET=supersecret\n")

        acq = self.harness.run("acquire", "--for", "task-secrets")
        self.assertEqual(acq.returncode, 0, acq.stderr.decode())
        name = self.harness.acquired_slot_name(acq)

        result = self.harness.run("up", name, "--secrets", "--for", "task-secrets")
        self.assertEqual(result.returncode, 0, result.stderr.decode())

        slot_dir = self.harness.slot_dir(name)
        dest = os.path.join(slot_dir, ".env.test.local")
        self.assertTrue(os.path.isfile(dest), "overlay file was not put into slot")
        with open(dest, encoding="utf-8") as f:
            self.assertEqual(f.read(), "API_SECRET=supersecret\n")


class AbandonedAndForeignVolumeReap(DevContainerTestCase):
    def test_reap_pass_3_removes_stopped_unleased_volume_slot_and_frees_number(self):
        # Criterion 6: A labelled volume with no live lease and no folder is treated as abandoned.
        # Stopped, unleased, labelled volume with no folder is removed and its number comes back into the pool.
        self.harness.build_fixture_repo(with_parent_env=MAIN_ENV)
        acq = self.harness.run("acquire", "--for", "test-holder")
        self.assertEqual(acq.returncode, 0, acq.stderr.decode())
        name = self.harness.acquired_slot_name(acq)
        self.assertEqual(name, "slot-1")
        self.assertTrue(self.harness.volume_exists("slot-1"))

        # Stop slot container, release/remove lease, ensure no folder exists on host
        self.harness.mark_stopped("slot-1")
        lease_path = self.harness.lease_path("slot-1")
        if os.path.exists(lease_path):
            os.remove(lease_path)
        self.assertFalse(os.path.isdir(os.path.join(self.harness.slots_dir, "slot-1")))

        # Run reap
        reap_res = self.harness.run("reap")
        self.assertEqual(reap_res.returncode, 0, reap_res.stderr.decode())

        # Volume must be removed
        self.assertFalse(self.harness.volume_exists("slot-1"), "abandoned volume was not removed by reap pass 3")

        # Slot number comes back into the pool
        acq2 = self.harness.run("acquire", "--for", "test-holder-2")
        self.assertEqual(acq2.returncode, 0, acq2.stderr.decode())
        self.assertEqual(self.harness.acquired_slot_name(acq2), "slot-1", "slot-1 was not reused after reap")

    def test_reap_leaves_foreign_project_volume_untouched(self):
        # Criterion 7: A reap for one project leaves a volume labelled for another project in place.
        self.harness.build_fixture_repo(with_parent_env=MAIN_ENV)
        foreign_vol = "foreign-slot-1-workspace"
        foreign_labels = {
            "dev-container.sid": "foreign-slot-1",
            "dev-container.project": "foreign",
            "dev-container.origin": "https://example.invalid/foreign.git",
            "dev-container.repo-root": "/some/foreign/repo",
        }
        self.harness.write_volume_labels(foreign_vol, foreign_labels)

        # Also create our project's abandoned volume
        acq = self.harness.run("acquire", "--for", "test-holder")
        name = self.harness.acquired_slot_name(acq)
        self.harness.mark_stopped(name)
        os.remove(self.harness.lease_path(name))

        # Run reap for our project
        reap_res = self.harness.run("reap")
        self.assertEqual(reap_res.returncode, 0, reap_res.stderr.decode())

        # Our abandoned volume is removed, foreign volume is untouched
        self.assertFalse(self.harness.volume_exists(name))
        self.assertTrue(self.harness.volume_exists(foreign_vol), "foreign volume was removed or modified by reap")


class RescueArchiveTarContents(DevContainerTestCase):
    def _expire_lease(self, name, age_seconds=100000):
        path = self.harness.lease_path(name)
        self.assertTrue(os.path.isfile(path), f"lease file missing: {path}")
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        data["since"] = int(time.time()) - age_seconds
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(data, handle)

        prefix = f"{PROJECT_PREFIX}-{name}"
        for kind in ("run", "cpu"):
            stamp = os.path.join(self.harness.state_dir, "activity", f"{prefix}.{kind}")
            if os.path.exists(stamp):
                os.remove(stamp)

    def test_rescue_tar_keeps_local_dir_and_excludes_node_modules(self):
        # Criterion 8: After a slot's volume is wiped, the rescue archive holds its .local/ files and no dependency directory.
        self.harness.build_fixture_repo(with_parent_env=MAIN_ENV)
        acq = self.harness.run("acquire", "--for", "test-holder")
        self.assertEqual(acq.returncode, 0, acq.stderr.decode())
        name = self.harness.acquired_slot_name(acq)
        slot_dir = self.harness.slot_dir(name)

        # Setup .gitignore in slot
        with open(os.path.join(slot_dir, ".gitignore"), "a", encoding="utf-8") as f:
            f.write(".local/\nnode_modules/\n")

        # Create files inside .local/
        local_dir = os.path.join(slot_dir, ".local")
        os.makedirs(local_dir, exist_ok=True)
        storage_file = os.path.join(local_dir, "node-chrome-storage.json")
        with open(storage_file, "w", encoding="utf-8") as f:
            f.write('{"auth_cookie": "secret123"}\n')

        notes_file = os.path.join(local_dir, "notes.txt")
        with open(notes_file, "w", encoding="utf-8") as f:
            f.write("local notes\n")

        # Create dependency directory node_modules
        nm_dir = os.path.join(slot_dir, "node_modules", "some-dep")
        os.makedirs(nm_dir, exist_ok=True)
        with open(os.path.join(nm_dir, "index.js"), "w", encoding="utf-8") as f:
            f.write("module.exports = {};\n")

        # Expire lease and clean
        self._expire_lease(name)

        clean_res = self.harness.run("clean")
        self.assertEqual(clean_res.returncode, 0, clean_res.stderr.decode())

        # Volume wiped
        self.assertFalse(self.harness.volume_exists(name))

        # Rescue archive check
        archive_dir = os.path.join(self.harness.state_dir, "rescue", self.harness.project_key, name)
        self.assertTrue(os.path.isdir(archive_dir), f"archive dir missing: {archive_dir}")

        tar_path = os.path.join(archive_dir, "new-files.tar")
        self.assertTrue(os.path.isfile(tar_path), "new-files.tar missing")

        tar_list = subprocess.run(
            ["tar", "-tf", tar_path],
            check=True,
            stdout=subprocess.PIPE,
        ).stdout.decode().splitlines()

        self.assertIn(".local/node-chrome-storage.json", tar_list)
        self.assertIn(".local/notes.txt", tar_list)
        for item in tar_list:
            self.assertFalse("node_modules" in item, f"dependency directory leaked into rescue tar: {item}")


class VolumeSlotEvict(DevContainerTestCase):
    def test_evict_stopped_volume_slot_removes_volume_and_env_and_pushes_nothing(self):
        # Criterion 9: Evict on a stopped slot starts nothing, pushes nothing, archives its work and removes the slot.
        self.harness.build_fixture_repo(with_parent_env=MAIN_ENV)
        acq = self.harness.run("acquire", "--for", "test-evict")
        self.assertEqual(acq.returncode, 0, acq.stderr.decode())
        name = self.harness.acquired_slot_name(acq)
        sid = f"{PROJECT_PREFIX}-{name}"

        env_file = os.path.join(self.harness.state_dir, "env", f"{sid}.env")
        self.assertTrue(os.path.isfile(env_file), f"env file missing: {env_file}")

        # Put work into slot
        slot_dir = self.harness.slot_dir(name)
        work_file = os.path.join(slot_dir, "uncommitted.txt")
        with open(work_file, "w", encoding="utf-8") as f:
            f.write("important unpushed work\n")

        # Stop slot
        self.harness.mark_stopped(name)

        # Record remote git branches before evict
        refs_before = subprocess.run(
            ["git", "-C", self.harness.fixture_repo, "for-each-ref", "--format=%(refname)"],
            stdout=subprocess.PIPE, check=True,
        ).stdout.decode()

        # Evict
        evict_res = self.harness.run("evict", name, "--yes", "--force")
        self.assertEqual(evict_res.returncode, 0, evict_res.stderr.decode())

        # Volume removed
        self.assertFalse(self.harness.volume_exists(name), "slot volume was not removed by evict")

        # Slot env removed
        self.assertFalse(os.path.exists(env_file), "slot env file was not removed by evict")

        # Lease removed
        self.assertFalse(os.path.exists(self.harness.lease_path(name)), "slot lease was not removed by evict")

        # Archive created with work
        archive_dir = os.path.join(self.harness.state_dir, "rescue", self.harness.project_key, name)
        self.assertTrue(os.path.isdir(archive_dir), f"archive dir missing: {archive_dir}")
        tar_path = os.path.join(archive_dir, "new-files.tar")
        self.assertTrue(os.path.isfile(tar_path), "new-files.tar missing from archive")
        tar_list = subprocess.run(
            ["tar", "-tf", tar_path],
            check=True,
            stdout=subprocess.PIPE,
        ).stdout.decode()
        self.assertIn("uncommitted.txt", tar_list)

        # Pushes nothing to remote (no rescue branch pushed)
        refs_after = subprocess.run(
            ["git", "-C", self.harness.fixture_repo, "for-each-ref", "--format=%(refname)"],
            stdout=subprocess.PIPE, check=True,
        ).stdout.decode()
        self.assertEqual(refs_before, refs_after, "evict pushed branches to remote")
        self.assertNotIn("rescue", refs_after)


class CanonicalZeroSlotNamingAndLabels(DevContainerTestCase):
    def test_acquire_uses_0_slot_N_project_format_compose_name_volume_label_and_lease_fields(self):
        self.harness.build_fixture_repo(with_parent_env=MAIN_ENV)
        result = self.harness.run("acquire", "--for", "test-holder")
        self.assertEqual(result.returncode, 0, result.stderr.decode())

        match = re.search(r'"id": "([^"]+)"', result.stdout.decode())
        self.assertIsNotNone(match, result.stdout.decode())
        sid = match.group(1)
        self.assertEqual(sid, f"0-slot-1-{PROJECT_PREFIX}")

        expected_vol = f"{sid}-workspace"
        self.assertIn(expected_vol, self.harness.volume_names())

        labels = self.harness.volume_labels(expected_vol)
        self.assertEqual(labels.get("dev-container.sid"), sid)
        self.assertEqual(labels.get("dev-container.project"), PROJECT_PREFIX)
        self.assertEqual(labels.get("dev-container.slot-num"), "1")
        self.assertEqual(labels.get("dev-container.repo-root"), os.path.realpath(self.harness.fixture_repo))
        self.assertEqual(labels.get("dev-container.origin"), "https://example.invalid/fixture.git")

        env_path = os.path.join(self.harness.state_dir, "env", f"{sid}.env")
        self.assertTrue(os.path.isfile(env_path), f"missing env file {env_path}")
        with open(env_path, encoding="utf-8") as handle:
            env_text = handle.read()
        self.assertIn(f"COMPOSE_PROJECT_NAME={sid}", env_text)

        with open(self.harness.docker_log, encoding="utf-8") as handle:
            self.assertIn(f"COMPOSE_PROJECT_NAME={sid}", handle.read())

        lease_path = os.path.join(self.harness.state_dir, "leases", f"{sid}.json")
        self.assertTrue(os.path.isfile(lease_path), f"missing lease file {lease_path}")
        with open(lease_path, encoding="utf-8") as handle:
            lease = json.load(handle)
        self.assertEqual(lease.get("project"), sid)
        self.assertEqual(lease.get("project_label"), PROJECT_PREFIX)
        self.assertIs(type(lease.get("slot_num")), int)
        self.assertEqual(lease.get("slot_num"), 1)


class LegacySlotTransition(DevContainerTestCase):
    def _plant_legacy_slot(self, slot_num=2, holder="legacy-worker"):
        name = f"slot-{slot_num}"
        sid = f"{PROJECT_PREFIX}-{name}"
        vol = f"{sid}-workspace"
        vol_dir = os.path.join(self.harness.volumes_dir, vol)
        os.makedirs(os.path.join(vol_dir, ".devcontainer"), exist_ok=True)
        with open(
            os.path.join(vol_dir, ".devcontainer", "docker-compose.yml"),
            "w",
            encoding="utf-8",
        ) as handle:
            handle.write("# fixture placeholder\n")
        os.makedirs(os.path.join(vol_dir, "docs", "repo", ".git"), exist_ok=True)
        subprocess.run(
            [self.harness.real_git_bin, "-C", vol_dir, "init", "-q"],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        subprocess.run(
            [self.harness.real_git_bin, "-C", vol_dir, "remote", "add", "origin", "https://example.invalid/fixture.git"],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self.harness.write_volume_labels(
            vol,
            {
                "dev-container.sid": sid,
                "dev-container.project": PROJECT_PREFIX,
                "dev-container.repo-root": os.path.realpath(self.harness.fixture_repo),
                "dev-container.origin": "https://example.invalid/fixture.git",
            },
        )
        ports_text = "SSH_PORT=41032\nNOVNC_PORT=41033\nVITE_DEV_PORT=41034\n"
        ports_path = os.path.join(self.harness.state_dir, "ports", f"{sid}.ports")
        os.makedirs(os.path.dirname(ports_path), exist_ok=True)
        with open(ports_path, "w", encoding="utf-8") as handle:
            handle.write(ports_text)
        env_path = os.path.join(self.harness.state_dir, "env", f"{sid}.env")
        os.makedirs(os.path.dirname(env_path), exist_ok=True)
        with open(env_path, "w", encoding="utf-8") as handle:
            handle.write(f"COMPOSE_PROJECT_NAME={sid}\n{MAIN_ENV}{ports_text}")
        lease_path = os.path.join(self.harness.state_dir, "leases", f"{sid}.json")
        os.makedirs(os.path.dirname(lease_path), exist_ok=True)
        with open(lease_path, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "holder": holder,
                    "owner": "-",
                    "project": sid,
                    "repo_root": os.path.realpath(self.harness.fixture_repo),
                    "since": self.harness.now_epoch() - 30,
                    "key": "/dev/null",
                    "ssh_port": 41032,
                },
                handle,
            )
        with open(os.path.join(self.harness.docker_state, f"{sid}.running"), "w", encoding="utf-8"):
            pass
        return name, sid, lease_path

    def test_legacy_slot_remains_readable_runnable_and_releasable_alongside_new_acquire(self):
        self.harness.build_fixture_repo(with_parent_env=MAIN_ENV)
        name, legacy_sid, legacy_lease = self._plant_legacy_slot(slot_num=2, holder="legacy-worker")

        listed = self.harness.run("list")
        self.assertEqual(listed.returncode, 0, listed.stderr.decode())
        self.assertIn(name, listed.stdout.decode())

        ran = self.harness.run("run", name, "--", "echo", "legacy-ok")
        self.assertEqual(ran.returncode, 0, ran.stderr.decode())
        self.assertEqual(ran.stdout.decode().strip(), "legacy-ok")

        acquired = self.harness.run("acquire", "--for", "new-worker")
        self.assertEqual(acquired.returncode, 0, acquired.stderr.decode())
        match = re.search(r'"id": "([^"]+)"', acquired.stdout.decode())
        self.assertIsNotNone(match, acquired.stdout.decode())
        new_sid = match.group(1)
        self.assertEqual(new_sid, f"0-slot-1-{PROJECT_PREFIX}")

        released = self.harness.run("release", name, "--for", "legacy-worker")
        self.assertEqual(released.returncode, 0, released.stderr.decode())
        self.assertFalse(os.path.exists(legacy_lease))
        self.assertTrue(os.path.exists(os.path.join(self.harness.state_dir, "leases", f"{new_sid}.json")))


if __name__ == "__main__":
    unittest.main()
