"""Real-Docker tests for volume-slot dirty checks and rescue archiving without DOCKER_STUB_STATE (#44)."""

import hashlib
import json
import os
import shutil
import subprocess
import time
import unittest

from dockerkit import RealEngineCase, docker, needs_docker, write

COMPOSE_YML = """services:
  app:
    build: ./ctx
    user: node
    working_dir: /workspace
    volumes:
      - ${WORKSPACE_DIR:-..}:/workspace
    ports:
      - "127.0.0.1:${SSH_PORT}:22"
"""


@needs_docker
class TestVolumeLifecycle(RealEngineCase):
    """End-to-end checks on real Docker volumes with zero host slot directory and no DOCKER_STUB_STATE."""

    def env(self, **extra):
        base = super().env(
            DEV_CONTAINER_SCHEDULE_FAST="3600",
            DEV_CONTAINER_SCHEDULE_SLOW="3600",
            DEV_CONTAINER_SCHEDULE_TICK="3600",
        )
        base.update(extra)
        return base

    def setUp(self):
        super().setUp()
        self.fixture = self.ensure_fixture_image()

    def _in_engine(self, checkout, *args, check=False):
        return self.inside(
            "env",
            f"DEV_CONTAINER_ENGINE_IMAGE={self.image}",
            f"DEV_CONTAINER_CALLER_PWD={checkout}",
            "dev-container",
            *args,
            workdir=checkout,
            check=check,
        )

    def _cleanup_slot(self, compose_dir, sid):
        self.inside(
            "docker", "compose", "-p", sid, "down", "-v", "--remove-orphans", workdir=compose_dir, check=False
        )
        docker("volume", "rm", "-f", f"{sid}-workspace", check=False)
        imgs = docker(
            "image", "ls", "--filter", f"label=dev-container.compose-project={sid}", "-q", check=False
        ).stdout.split()
        if imgs:
            docker("rmi", "-f", *imgs, check=False)

    def _git(self, repo, *args):
        return subprocess.run(
            [
                "git",
                "-C",
                repo,
                "-c",
                "commit.gpgsign=false",
                "-c",
                "user.name=demo-agent",
                "-c",
                "user.email=demo-agent@example.com",
                *args,
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def _create_project(self, label="vlproj"):
        checkout = os.path.join(self.projects, label)
        compose_dir = os.path.join(checkout, ".devcontainer")
        origin = os.path.join(self.tmp, f"{label}-origin.git")
        proj_prefix = f"dct{self.uid}vl"

        subprocess.run(["git", "init", "-q", "--bare", origin], check=True)
        subprocess.run(["git", "-C", origin, "symbolic-ref", "HEAD", "refs/heads/main"], check=True)

        write(
            os.path.join(checkout, "project.env"),
            "\n".join([
                f"PROJECT={proj_prefix}",
                "DEFAULT_BRANCH=main",
                "SLOT_PORTS=SSH_PORT",
                "COMPOSE_FILES=docker-compose.yml",
                "",
            ]),
        )
        write(os.path.join(checkout, ".gitignore"), ".local/\nnode_modules/\nignored.log\n")
        write(os.path.join(checkout, "README.md"), "base readme\n")
        write(os.path.join(compose_dir, "docker-compose.yml"), COMPOSE_YML)
        write(os.path.join(compose_dir, "ctx", "Dockerfile"), f"FROM {self.fixture}\n")

        self._git(checkout, "init", "-q", "-b", "main")
        self._git(
            checkout,
            "add",
            "project.env",
            ".gitignore",
            "README.md",
            ".devcontainer/docker-compose.yml",
            ".devcontainer/ctx/Dockerfile",
        )
        self._git(checkout, "commit", "-q", "-m", "init")
        self._git(checkout, "remote", "add", "origin", origin)
        self._git(checkout, "push", "-q", "origin", "main")
        self._git(checkout, "fetch", "-q", "origin")

        key = hashlib.sha256(os.path.realpath(checkout).encode()).hexdigest()[:8]
        return checkout, compose_dir, origin, proj_prefix, key

    def _seed_volume_slot(
        self,
        checkout,
        origin,
        proj_prefix,
        slot_num,
        *,
        holder=None,
        ephemeral=False,
        since=None,
        setup_fn=None,
        running=True,
    ):
        compose_dir = os.path.join(checkout, ".devcontainer")
        sid = f"{proj_prefix}-slot-{slot_num}"
        vol = f"{sid}-workspace"
        self.addCleanup(self._cleanup_slot, compose_dir, sid)

        seed = os.path.join(self.tmp, f"seed-{sid}")
        subprocess.run(["git", "clone", "-q", origin, seed], check=True)
        self._git(seed, "config", "--local", "user.name", "demo-agent")
        self._git(seed, "config", "--local", "user.email", "demo-agent@example.com")
        exclude_path = os.path.join(seed, ".git", "info", "exclude")
        os.makedirs(os.path.dirname(exclude_path), exist_ok=True)
        with open(exclude_path, "a", encoding="utf-8") as handle:
            handle.write("/.devcontainer/\n/project.env\n/.slot-holder\ndocs/repo\ndocs/fix\n")

        if setup_fn is not None:
            setup_fn(seed)

        docker(
            "volume",
            "create",
            "--label",
            f"dev-container.sid={sid}",
            "--label",
            f"dev-container.project={proj_prefix}",
            "--label",
            f"dev-container.origin={origin}",
            "--label",
            f"dev-container.repo-root={checkout}",
            vol,
        )
        seeded = docker(
            "run",
            "--rm",
            "--user",
            "root",
            "-v",
            f"{vol}:/workspace",
            "-v",
            f"{seed}:/seed:ro",
            self.fixture,
            "sh",
            "-c",
            "cp -a /seed/. /workspace/ && chown -R 1000:1000 /workspace",
            check=False,
        )
        self.assertEqual(seeded.returncode, 0, seeded.stderr)

        ssh_port = 42000 + slot_num * 16
        write(os.path.join(self.state, "ports", f"{sid}.ports"), f"SSH_PORT={ssh_port}\n")
        write(
            os.path.join(self.state, "env", f"{sid}.env"),
            f"COMPOSE_PROJECT_NAME={sid}\nSSH_PORT={ssh_port}\nGIT_USER_NAME=demo-agent\nGIT_USER_EMAIL=demo-agent@example.com\n",
        )
        if holder is not None:
            lease_obj = {
                "holder": holder,
                "owner": "Docker [vl]",
                "project": sid,
                "since": int(since if since is not None else time.time()),
                "key": "/dev/null",
                "ssh_port": ssh_port,
                "repo_root": checkout,
            }
            if ephemeral:
                lease_obj["ephemeral"] = True
                lease_obj["idle_limit"] = 900
            write(os.path.join(self.state, "leases", f"{sid}.json"), json.dumps(lease_obj))

        built = self.inside(
            "bash",
            "-c",
            'export DEV_CONTAINER_CALLER_PWD="$1" DEV_CONTAINER_LIB=1 && '
            'source /usr/local/bin/dev-container && build_slot_image "" "$2"',
            "bash",
            checkout,
            sid,
            workdir=checkout,
            check=False,
        )
        self.assertEqual(built.returncode, 0, built.stdout + built.stderr)

        if running:
            up = self._in_engine(checkout, "up", f"slot-{slot_num}")
            self.assertEqual(up.returncode, 0, up.stdout + up.stderr)

        self.assertFalse(os.path.exists(os.path.join(self.projects, "dev-containers", f"slot-{slot_num}")))
        return sid, vol

    def _volume_exists(self, vol):
        return docker("volume", "inspect", vol, check=False).returncode == 0

    def test_release_refuses_dirty_and_allows_discard_and_clean_real_volume_slot(self):
        checkout, _cdir, origin, proj_prefix, key = self._create_project("vlrel")

        def make_dirty(seed):
            self._git(seed, "checkout", "-q", "-b", "wip-work")
            write(os.path.join(seed, "branch.txt"), "unpushed commit\n")
            self._git(seed, "add", "branch.txt")
            self._git(seed, "commit", "-q", "-m", "wip commit")
            write(os.path.join(seed, "README.md"), "dirty tracked edit\n")
            write(os.path.join(seed, "untracked.txt"), "untracked work\n")

        sid1, vol1 = self._seed_volume_slot(
            checkout, origin, proj_prefix, 1, holder="holder-1", setup_fn=make_dirty, running=True
        )
        refused = self._in_engine(checkout, "release", "slot-1", "--for", "holder-1")
        combined = refused.stdout + refused.stderr
        self.assertNotEqual(refused.returncode, 0, combined)
        self.assertIn("wip-work", combined)
        self.assertIn("uncommitted:", combined)
        self.assertTrue(self._volume_exists(vol1), "dirty volume was deleted on plain release")
        self.assertTrue(os.path.isfile(os.path.join(self.state, "leases", f"{sid1}.json")))

        discarded = self._in_engine(checkout, "release", "slot-1", "--for", "holder-1", "--discard")
        self.assertEqual(discarded.returncode, 0, discarded.stdout + discarded.stderr)
        self.assertFalse(self._volume_exists(vol1))
        self.assertFalse(os.path.exists(os.path.join(self.state, "leases", f"{sid1}.json")))
        self.assertFalse(os.path.exists(os.path.join(self.state, "rescue", key, "slot-1")))

        sid2, vol2 = self._seed_volume_slot(
            checkout, origin, proj_prefix, 2, holder="holder-2", running=False
        )
        clean_rel = self._in_engine(checkout, "release", "slot-2", "--for", "holder-2")
        self.assertEqual(clean_rel.returncode, 0, clean_rel.stdout + clean_rel.stderr)
        self.assertFalse(self._volume_exists(vol2))
        self.assertFalse(os.path.exists(os.path.join(self.state, "leases", f"{sid2}.json")))

    def test_down_and_list_real_volume_slots_without_stub_state(self):
        checkout, _cdir, origin, proj_prefix, _key = self._create_project("vldown")

        def make_dirty(seed):
            self._git(seed, "checkout", "-q", "-b", "down-wip")
            write(os.path.join(seed, "c.txt"), "unpushed\n")
            self._git(seed, "add", "c.txt")
            self._git(seed, "commit", "-q", "-m", "unpushed")
            write(os.path.join(seed, "untracked.txt"), "untracked\n")

        _sid1, vol1 = self._seed_volume_slot(
            checkout, origin, proj_prefix, 1, holder=None, setup_fn=make_dirty, running=False
        )
        _sid2, vol2 = self._seed_volume_slot(
            checkout, origin, proj_prefix, 2, holder=None, running=False
        )

        listed = self._in_engine(checkout, "list")
        self.assertEqual(listed.returncode, 0, listed.stdout + listed.stderr)
        rows = {line.split()[0]: line for line in listed.stdout.splitlines() if line.split()}
        self.assertIn("slot-1", rows, listed.stdout)
        self.assertIn("uncommitted:1", rows["slot-1"])
        self.assertIn("dirty:", rows["slot-1"])
        self.assertIn("slot-2", rows, listed.stdout)
        self.assertIn("clean", rows["slot-2"])
        self.assertIn("acquirable", rows["slot-2"])
        self.assertNotIn("unbuilt", rows["slot-2"])

        down_refused = self._in_engine(checkout, "down", "slot-1")
        combined = down_refused.stdout + down_refused.stderr
        self.assertNotEqual(down_refused.returncode, 0, combined)
        self.assertIn("down-wip", combined)
        self.assertIn("untracked.txt", combined)
        self.assertTrue(self._volume_exists(vol1), "down deleted dirty unleased volume")

        down_forced = self._in_engine(checkout, "down", "slot-1", "--force")
        self.assertEqual(down_forced.returncode, 0, down_forced.stdout + down_forced.stderr)
        self.assertFalse(self._volume_exists(vol1))

        down_clean = self._in_engine(checkout, "down", "slot-2")
        self.assertEqual(down_clean.returncode, 0, down_clean.stdout + down_clean.stderr)
        self.assertFalse(self._volume_exists(vol2))

    def test_evict_and_reap_real_dirty_volume_slots_write_recoverable_rescue_artifacts(self):
        checkout, _cdir, origin, proj_prefix, key = self._create_project("vlarch")

        def make_rescue_work(seed):
            self._git(seed, "checkout", "-q", "-b", "feat-rescue")
            write(os.path.join(seed, "commit_work.txt"), "unpushed docker volume work\n")
            self._git(seed, "add", "commit_work.txt")
            self._git(seed, "commit", "-q", "-m", "rescue commit")
            write(os.path.join(seed, "README.md"), "modified in volume\n")
            write(os.path.join(seed, "untracked.txt"), "untracked in volume\n")
            write(os.path.join(seed, ".local", "state.json"), '{"saved":true}\n')
            write(os.path.join(seed, "ignored.log"), "ignored log\n")
            write(os.path.join(seed, "node_modules", "pkg", "index.js"), "module.exports = {};\n")

        sid1, vol1 = self._seed_volume_slot(
            checkout, origin, proj_prefix, 1, holder="evict-h", setup_fn=make_rescue_work, running=False
        )
        evicted = self._in_engine(checkout, "evict", "slot-1", "--yes", "--force")
        self.assertEqual(evicted.returncode, 0, evicted.stdout + evicted.stderr)
        self.assertFalse(self._volume_exists(vol1))
        self.assertFalse(os.path.exists(os.path.join(self.state, "leases", f"{sid1}.json")))

        arch1 = os.path.join(self.state, "rescue", key, "slot-1")
        for fname in ("commits.bundle", "changes.patch", "new-files.tar", "meta.json"):
            self.assertTrue(os.path.isfile(os.path.join(arch1, fname)), f"missing {fname} in {arch1}")

        recover = os.path.join(self.tmp, "recover-docker")
        subprocess.run(["git", "clone", "-q", origin, recover], check=True)
        self._git(recover, "fetch", os.path.join(arch1, "commits.bundle"), "refs/heads/*:refs/heads/*")
        self._git(recover, "checkout", "-q", "feat-rescue")
        with open(os.path.join(recover, "commit_work.txt"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "unpushed docker volume work\n")
        self._git(recover, "apply", "--whitespace=nowarn", os.path.join(arch1, "changes.patch"))
        with open(os.path.join(recover, "README.md"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "modified in volume\n")
        subprocess.run(["tar", "-xf", os.path.join(arch1, "new-files.tar"), "-C", recover], check=True)
        with open(os.path.join(recover, "untracked.txt"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "untracked in volume\n")
        with open(os.path.join(recover, ".local", "state.json"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), '{"saved":true}\n')
        self.assertFalse(os.path.exists(os.path.join(recover, "node_modules")))
        self.assertFalse(os.path.exists(os.path.join(recover, "ignored.log")))

        with open(os.path.join(arch1, "meta.json"), encoding="utf-8") as handle:
            meta = json.load(handle)
        self.assertIn("feat-rescue", meta["branches"])
        self.assertIn("ignored.log", meta["ignored"])
        self.assertFalse(any("node_modules" in item for item in meta["ignored"]))

        # Reap: expired non-ephemeral running slot -> rescued; expired ephemeral slot -> zero rescue
        old_since = int(time.time()) - 10000
        sid2, vol2 = self._seed_volume_slot(
            checkout,
            origin,
            proj_prefix,
            2,
            holder="reap-norm",
            since=old_since,
            setup_fn=lambda s: write(os.path.join(s, "reap_work.txt"), "keep via reap\n"),
            running=True,
        )
        sid3, vol3 = self._seed_volume_slot(
            checkout,
            origin,
            proj_prefix,
            3,
            holder="reap-eph",
            ephemeral=True,
            since=old_since,
            setup_fn=lambda s: write(os.path.join(s, "eph_work.txt"), "discard via reap\n"),
            running=False,
        )

        reaped = self._in_engine(checkout, "reap")
        self.assertEqual(reaped.returncode, 0, reaped.stdout + reaped.stderr)
        self.assertFalse(self._volume_exists(vol2))
        self.assertFalse(self._volume_exists(vol3))
        self.assertFalse(os.path.exists(os.path.join(self.state, "leases", f"{sid2}.json")))
        self.assertFalse(os.path.exists(os.path.join(self.state, "leases", f"{sid3}.json")))
        self.assertTrue(os.path.isfile(os.path.join(self.state, "rescue", key, "slot-2", "new-files.tar")))
        self.assertFalse(os.path.exists(os.path.join(self.state, "rescue", key, "slot-3")))

    def test_reap_and_evict_non_git_real_volume_slot_removes_without_parking(self):
        checkout, compose_dir, origin, proj_prefix, key = self._create_project("vlnogit")
        old_since = int(time.time()) - 10000

        def make_bare_vol(slot_num, *, holder=None, half_provisioned=False):
            sid = f"{proj_prefix}-slot-{slot_num}"
            vol = f"{sid}-workspace"
            self.addCleanup(self._cleanup_slot, compose_dir, sid)
            created = docker(
                "volume",
                "create",
                "--label",
                f"dev-container.sid={sid}",
                "--label",
                f"dev-container.project={proj_prefix}",
                "--label",
                f"dev-container.origin={origin}",
                "--label",
                f"dev-container.repo-root={checkout}",
                vol,
                check=False,
            )
            self.assertEqual(created.returncode, 0, created.stderr)
            if half_provisioned:
                seeded = docker(
                    "run",
                    "--rm",
                    "--user",
                    "root",
                    "-v",
                    f"{vol}:/workspace",
                    self.fixture,
                    "sh",
                    "-c",
                    "mkdir -p /workspace/.devcontainer && chown -R 1000:1000 /workspace",
                    check=False,
                )
                self.assertEqual(seeded.returncode, 0, seeded.stderr)
            if holder is not None:
                write(
                    os.path.join(self.state, "leases", f"{sid}.json"),
                    json.dumps({
                        "holder": holder,
                        "owner": "Docker [vl]",
                        "project": sid,
                        "since": old_since,
                        "key": "/dev/null",
                        "ssh_port": 42000 + slot_num * 16,
                        "repo_root": checkout,
                    }),
                )
            return sid, vol

        sid1, vol1 = make_bare_vol(1, holder="reap-nogit", half_provisioned=True)
        sid2, vol2 = make_bare_vol(2, holder=None, half_provisioned=False)

        evicted = self._in_engine(checkout, "evict", "slot-2", "--yes", "--force")
        self.assertEqual(evicted.returncode, 0, evicted.stdout + evicted.stderr)
        self.assertFalse(self._volume_exists(vol2))
        self.assertFalse(os.path.exists(os.path.join(self.state, "parked", f"{sid2}.json")))
        self.assertFalse(os.path.exists(os.path.join(self.state, "rescue", key, "slot-2")))

        reaped = self._in_engine(checkout, "reap")
        self.assertEqual(reaped.returncode, 0, reaped.stdout + reaped.stderr)
        self.assertFalse(self._volume_exists(vol1))
        self.assertFalse(os.path.exists(os.path.join(self.state, "leases", f"{sid1}.json")))
        self.assertFalse(os.path.exists(os.path.join(self.state, "parked", f"{sid1}.json")))
        self.assertFalse(os.path.exists(os.path.join(self.state, "rescue", key, "slot-1")))

    def test_non_git_real_volume_slot_release_down_park_and_list_acquirable(self):
        checkout, _cdir, origin, proj_prefix, key = self._create_project("vlnogitgate")

        def drop_git(seed):
            shutil.rmtree(os.path.join(seed, ".git"))

        def corrupt_git(seed):
            shutil.rmtree(os.path.join(seed, ".git"))
            os.makedirs(os.path.join(seed, ".git"))

        # 1. Leased non-git vol:* slot -> release --for removes volume and lease
        sid1, vol1 = self._seed_volume_slot(
            checkout, origin, proj_prefix, 1, holder="nogit-rel", setup_fn=drop_git, running=True
        )
        released = self._in_engine(checkout, "release", "slot-1", "--for", "nogit-rel")
        self.assertEqual(released.returncode, 0, released.stdout + released.stderr)
        self.assertFalse(self._volume_exists(vol1))
        self.assertFalse(os.path.exists(os.path.join(self.state, "leases", f"{sid1}.json")))
        self.assertFalse(os.path.exists(os.path.join(self.state, "rescue", key, "slot-1")))

        # 2. Unleased non-git vol:* slot -> list shows clean + acquirable, down removes volume
        _sid2, vol2 = self._seed_volume_slot(
            checkout, origin, proj_prefix, 2, holder=None, setup_fn=drop_git, running=False
        )
        listed = self._in_engine(checkout, "list")
        self.assertEqual(listed.returncode, 0, listed.stdout + listed.stderr)
        rows = {line.split()[0]: line for line in listed.stdout.splitlines() if line.split()}
        self.assertIn("slot-2", rows, listed.stdout)
        self.assertIn("clean", rows["slot-2"])
        self.assertIn("acquirable", rows["slot-2"])
        self.assertNotIn("unreadable", rows["slot-2"])

        down_clean = self._in_engine(checkout, "down", "slot-2")
        self.assertEqual(down_clean.returncode, 0, down_clean.stdout + down_clean.stderr)
        self.assertFalse(self._volume_exists(vol2))

        # 3. Leased non-git vol:* slot -> park releases instead of parking
        sid3, vol3 = self._seed_volume_slot(
            checkout, origin, proj_prefix, 3, holder="nogit-park", setup_fn=drop_git, running=False
        )
        parked = self._in_engine(checkout, "park", "slot-3")
        self.assertEqual(parked.returncode, 0, parked.stdout + parked.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.state, "leases", f"{sid3}.json")))
        self.assertFalse(os.path.exists(os.path.join(self.state, "parked", f"{sid3}.json")))
        down3 = self._in_engine(checkout, "down", "slot-3")
        self.assertEqual(down3.returncode, 0, down3.stdout + down3.stderr)
        self.assertFalse(self._volume_exists(vol3))

        # 4. Corrupt .git on real vol:* slot -> blocks release and reports unreadable
        sid4, vol4 = self._seed_volume_slot(
            checkout, origin, proj_prefix, 4, holder="corrupt-rel", setup_fn=corrupt_git, running=False
        )
        refused = self._in_engine(checkout, "release", "slot-4", "--for", "corrupt-rel")
        self.assertNotEqual(refused.returncode, 0, refused.stdout + refused.stderr)
        self.assertIn("unreadable", refused.stderr)
        self.assertTrue(self._volume_exists(vol4))
        self.assertTrue(os.path.isfile(os.path.join(self.state, "leases", f"{sid4}.json")))

    def test_acquire_builds_once_and_restart_after_new_commit_leaves_no_dangling_images(self):
        checkout, compose_dir, origin, proj_prefix, _key = self._create_project("vldangle")
        sid = f"0-slot-1-{proj_prefix}"
        self.addCleanup(self._cleanup_slot, compose_dir, sid)

        write(
            os.path.join(checkout, "project.env"),
            "\n".join([
                f"PROJECT={proj_prefix}",
                "DEFAULT_BRANCH=main",
                "SLOT_PORTS=SSH_PORT",
                "COMPOSE_FILES=docker-compose.yml",
                "SLOT_GIT_NAME=demo-agent",
                "SLOT_GIT_EMAIL=demo-agent@example.com",
                "",
            ]),
        )
        write(
            os.path.join(compose_dir, "ctx", "Dockerfile"),
            "\n".join([
                f"FROM {self.fixture}",
                "USER root",
                "RUN cp /bin/bash /usr/local/bin/sshd && mkdir -p /etc/ssh/authorized_keys.d && chown node /etc/ssh/authorized_keys.d",
                "USER node",
                'CMD ["/usr/local/bin/sshd", "-c", "while :; do sleep 3600; done"]',
                "",
            ]),
        )
        self._git(checkout, "add", "project.env", ".devcontainer/ctx/Dockerfile")
        self._git(checkout, "commit", "-q", "-m", "configure acquire git identity and sshd")
        self._git(checkout, "push", "-q", "origin", "main")

        acquired = self._in_engine(checkout, "acquire", "--for", "test-dangle")
        self.assertEqual(acquired.returncode, 0, acquired.stdout + acquired.stderr)
        self.assertIn("slot-1", acquired.stdout)

        tagged = docker(
            "image", "ls", "--filter", "dangling=false",
            "--filter", f"label=dev-container.compose-project={sid}", "-q",
        ).stdout.split()
        self.assertTrue(tagged, f"acquire built no tagged image for {sid}")
        dangling_after_acquire = docker(
            "image", "ls", "--filter", "dangling=true",
            "--filter", f"label=dev-container.compose-project={sid}", "-q",
        ).stdout.split()
        self.assertEqual(dangling_after_acquire, [], f"acquire left dangling images: {dangling_after_acquire}")

        write(os.path.join(checkout, "README.md"), "updated readme for rebuild\n")
        self._git(checkout, "add", "README.md")
        self._git(checkout, "commit", "-q", "-m", "trigger rebuild")
        self._git(checkout, "push", "-q", "origin", "main")

        restarted = self._in_engine(checkout, "restart", "slot-1")
        self.assertEqual(restarted.returncode, 0, restarted.stdout + restarted.stderr)

        dangling_after_restart = docker(
            "image", "ls", "--filter", "dangling=true",
            "--filter", f"label=dev-container.compose-project={sid}", "-q",
        ).stdout.split()
        self.assertEqual(
            dangling_after_restart, [],
            f"restart left dangling images behind: {dangling_after_restart}",
        )


if __name__ == "__main__":
    unittest.main()
