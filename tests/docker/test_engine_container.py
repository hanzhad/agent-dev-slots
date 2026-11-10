import json
import os
import re
import subprocess
import time

from dockerkit import ENGINE, REPO_ROOT, THIN, RealEngineCase, docker, needs_docker, write

SOCKET = "/var/run/docker.sock"
ALLOW_LIST = os.path.join(REPO_ROOT, "image", "tools.txt")
DOCKERFILE = os.path.join(REPO_ROOT, "image", "Dockerfile")
MARK = "mark"


def listed_tools():
    with open(ALLOW_LIST, encoding="utf-8") as handle:
        lines = [line.split("#")[0].strip() for line in handle.read().splitlines()]
    return {line for line in lines if line}


def base_image():
    with open(DOCKERFILE, encoding="utf-8") as handle:
        return re.search(r"(?m)^FROM\s+(\S+)", handle.read()).group(1)


@needs_docker
class ListedTools(RealEngineCase):
    def test_the_packages_added_to_the_base_are_exactly_the_allow_list(self):
        world = set(self.inside("cat", "/etc/apk/world").stdout.split())
        base = set(docker("run", "--rm", "--entrypoint", "cat", base_image(), "/etc/apk/world").stdout.split())
        self.assertEqual(world - base, listed_tools())

    def test_every_listed_package_is_installed(self):
        installed = set(self.inside("apk", "info").stdout.split())
        self.assertEqual(listed_tools() - installed, set())

    def test_the_tools_the_engine_calls_are_there_and_work(self):
        for tool in ("bash", "jq", "git", "gh", "docker", "ssh-keygen", "openssl", "curl", "tar", "tini", "realpath"):
            self.assertEqual(self.inside("sh", "-c", "command -v " + tool, check=False).returncode, 0, tool)
        self.assertEqual(self.inside("docker", "compose", "version", check=False).returncode, 0)
        self.assertEqual(self.inside("realpath", "-m", "/a/../b").stdout.strip(), "/b")

    def test_the_engine_is_on_the_path_and_prints_its_usage(self):
        result = self.inside("dev-container", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Usage:", result.stderr)


def put(path, text):
    return ["sh", "-c", 'printf "%s\n" "$1" > "$2"', "sh", text, path]


@needs_docker
class EngineContainer(RealEngineCase):
    def test_it_runs_as_the_host_user_with_a_passwd_entry(self):
        self.assertEqual(self.inside("id", "-u").stdout.strip(), str(os.getuid()))
        named = self.inside("id", "-un", check=False)
        self.assertEqual(named.returncode, 0, named.stderr)
        key = os.path.join(self.home, "k")
        made = self.inside("ssh-keygen", "-q", "-N", "", "-t", "ed25519", "-f", key, check=False)
        self.assertEqual(made.returncode, 0, made.stderr)

    def test_home_is_a_tmpfs_at_the_host_path_and_holds_no_ssh_directory(self):
        self.assertEqual(self.inside("sh", "-c", "echo $HOME").stdout.strip(), self.home)
        mounts = self.inside("cat", "/proc/mounts").stdout
        self.assertRegex(mounts, "(?m)^\S+ " + re.escape(self.home) + " tmpfs ")
        self.assertEqual(self.inside("test", "-e", self.home + "/.ssh", check=False).returncode, 1)

    def test_run_dev_container_is_a_private_noexec_tmpfs(self):
        self.assertEqual(self.inside("stat", "-c", "%a", "/run/dev-container").stdout.strip(), "700")
        mounts = [m for m in self.inside("cat", "/proc/mounts").stdout.splitlines() if " /run/dev-container " in m]
        self.assertEqual(len(mounts), 1, mounts)
        self.assertIn("noexec", mounts[0])

    def test_state_the_projects_root_and_machine_limits_are_the_same_paths_inside(self):
        self.inside(*put(os.path.join(self.state, MARK), "from-engine"))
        self.inside(*put(os.path.join(self.projects, MARK), "from-engine"))
        self.assertTrue(os.path.isfile(os.path.join(self.state, MARK)))
        self.assertEqual(os.stat(os.path.join(self.projects, MARK)).st_uid, os.getuid())
        self.assertEqual(self.inside("cat", self.limits + "/memory-floor-mb").stdout.strip(), "2048")
        refused = self.inside(*put(self.limits + "/memory-floor-mb", "x"), check=False)
        self.assertNotEqual(refused.returncode, 0, "machine-limits is writable from the engine")

    def test_state_survives_removing_the_container(self):
        self.inside(*put(os.path.join(self.state, MARK), "kept"))
        docker("rm", "-f", self.name)
        self.install()
        self.assertEqual(self.inside("cat", os.path.join(self.state, MARK)).stdout.strip(), "kept")
        with open(os.path.join(self.state, MARK), encoding="utf-8") as handle:
            self.assertEqual(handle.read().strip(), "kept")

    def test_uninstall_removes_the_container_and_leaves_state(self):
        self.inside(*put(os.path.join(self.state, MARK), "kept"))
        result = self.script(ENGINE, "uninstall")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotEqual(docker("inspect", self.name, check=False).returncode, 0)
        self.assertTrue(os.path.isfile(os.path.join(self.state, MARK)))

    def test_the_docker_socket_is_the_only_privilege(self):
        info = self.inspect()
        host = info["HostConfig"]
        self.assertFalse(host["Privileged"])
        self.assertNotEqual(host["NetworkMode"], "host")
        self.assertIn(host["PidMode"], ("", "private"))
        self.assertNotEqual(host["IpcMode"], "host")
        self.assertFalse(host.get("CapAdd"))
        self.assertFalse(host.get("Devices"))
        self.assertFalse(host.get("SecurityOpt"))
        self.assertEqual(host["RestartPolicy"]["Name"], "unless-stopped")
        self.assertEqual(info["Config"]["User"].split(":")[0], str(os.getuid()))
        sources = {m["Source"] for m in info["Mounts"] if m["Type"] == "bind"}
        allowed = {self.state, self.projects, self.limits, SOCKET, os.path.join(self.state, "passwd")}
        self.assertEqual(sources - allowed, set())
        self.assertIn(SOCKET, sources)
        self.assertIn(self.home, host["Tmpfs"])


@needs_docker
class ThinAgainstARealEngine(RealEngineCase):
    def thin_stdin(self, *args, cwd, text):
        return subprocess.run(
            ["/bin/sh", THIN, *args],
            cwd=cwd,
            env=self.env(PWD=cwd),
            input=text,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=900,
        )

    def checkout(self, parent, name):
        path = os.path.join(parent, name)
        os.makedirs(path)
        origin = os.path.join(self.tmp, name + "-origin.git")
        commit = ["-C", path, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "--allow-empty", "-m", "first"]
        for args in (
            ["init", "-q", "--bare", origin],
            ["-C", path, "init", "-q", "-b", "main"],
            commit,
            ["-C", path, "remote", "add", "origin", origin],
        ):
            subprocess.run(["git", *args], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return path

    def test_a_stopped_engine_is_refused_with_a_command_that_works(self):
        docker("stop", self.name)
        result = self.thin("list", cwd=self.tmp)
        self.assertNotEqual(result.returncode, 0)
        wanted = "docker start " + self.name
        printed = [line for line in result.stderr.splitlines() if wanted in line]
        self.assertTrue(printed, result.stderr)
        command = printed[0][printed[0].index("docker start"):].strip()
        started = subprocess.run(command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.assertEqual(started.returncode, 0, started.stderr)
        again = self.thin(cwd=self.tmp)
        self.assertIn("Usage:", again.stderr)

    def test_the_usage_the_engine_prints_names_the_thin(self):
        result = self.thin(cwd=self.tmp)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("dev-container-thin", result.stderr)

    def test_a_checkout_inside_the_projects_root_is_found_from_the_passed_directory(self):
        path = self.checkout(self.projects, "inside")
        sub = os.path.join(path, "nested")
        os.makedirs(sub)
        result = self.thin("list", "--brief", cwd=sub)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_a_checkout_outside_the_projects_root_is_refused_with_the_install_command(self):
        path = self.checkout(os.path.join(self.tmp, "elsewhere"), "outside")
        result = self.thin("list", "--brief", cwd=path)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("engine install --projects-root", result.stderr)

    def test_unlock_stores_the_token_on_tmpfs_unlocks_check_and_a_restart_locks_it_again(self):
        token = "forge-read-SENTINEL_11_CONTAINER"
        before = self.thin("check", cwd=self.tmp)
        self.assertIn("locked", (before.stdout + before.stderr).lower())

        unlocked = self.thin_stdin("unlock", cwd=self.tmp, text=token + "\n")
        self.assertEqual(unlocked.returncode, 0, unlocked.stderr)
        self.assertNotIn(token, unlocked.stdout + unlocked.stderr)

        entries = self.inside(
            "sh", "-c", 'for f in /run/dev-container/*; do [ -f "$f" ] && printf "%s:%s:%s\n" "$f" "$(stat -c %a "$f")" "$(cat "$f")"; done'
        ).stdout.splitlines()
        self.assertEqual(len(entries), 1, entries)
        _, mode, stored = entries[0].split(":", 2)
        self.assertEqual(mode, "600")
        self.assertEqual(stored.strip(), token)

        for root, _, names in os.walk(self.state):
            for name in names:
                with open(os.path.join(root, name), encoding="utf-8", errors="replace") as handle:
                    self.assertNotIn(token, handle.read())

        checked = self.thin("check", cwd=self.tmp)
        self.assertEqual(checked.returncode, 0, checked.stderr)
        self.assertNotIn("locked", (checked.stdout + checked.stderr).lower())

        docker("restart", self.name)
        after_entries = self.inside(
            "sh", "-c", 'for f in /run/dev-container/*; do [ -e "$f" ] && echo "$f"; done; true'
        ).stdout.splitlines()
        self.assertEqual(after_entries, [])
        after = self.thin("check", cwd=self.tmp)
        self.assertIn("locked", (after.stdout + after.stderr).lower())


@needs_docker
class ScheduleInTheEngineContainer(RealEngineCase):
    checkout = ThinAgainstARealEngine.checkout

    def env(self, **extra):
        base = super().env(
            DEV_CONTAINER_SCHEDULE_FAST="10",
            DEV_CONTAINER_SCHEDULE_SLOW="3600",
            DEV_CONTAINER_SCHEDULE_TICK="1",
        )
        base.update(extra)
        return base

    def stamp_path(self, job):
        return os.path.join(self.state, "schedule", job + ".last")

    def read_stamp(self, job):
        path = self.stamp_path(job)
        if not os.path.isfile(path):
            return 0
        with open(path, encoding="utf-8") as handle:
            text = handle.read().strip()
        return int(text) if text.isdigit() else 0

    def wait_for_stamp(self, job, at_least=1, timeout=20):
        deadline = time.monotonic() + timeout
        while self.read_stamp(job) < at_least:
            if time.monotonic() > deadline:
                raise AssertionError(f"{job}.last never reached {at_least}")
            time.sleep(0.1)
        return self.read_stamp(job)

    def read_boot_id(self):
        path = os.path.join(self.state, "boot-id")
        if not os.path.isfile(path):
            return ""
        with open(path, encoding="utf-8") as handle:
            return handle.read().strip()

    def wait_for_boot_id(self, different_from="", timeout=20):
        deadline = time.monotonic() + timeout
        while True:
            current = self.read_boot_id()
            if current and current != different_from:
                return current
            if time.monotonic() > deadline:
                raise AssertionError(f"boot-id stayed {current!r}")
            time.sleep(0.1)

    def test_stopping_past_several_intervals_and_starting_again_runs_one_makeup_pass(self):
        self.wait_for_stamp("fast")
        cmdline = self.inside("sh", "-c", "tr '\\0' ' ' </proc/1/cmdline").stdout
        self.assertIn("tini", cmdline)

        docker("stop", self.name)
        write(self.stamp_path("fast"), f"{int(time.time()) - 30}\n")
        before = int(time.time())
        docker("start", self.name)

        makeup = self.wait_for_stamp("fast", at_least=before)
        time.sleep(2.5)
        self.assertEqual(self.read_stamp("fast"), makeup)

    def test_restarting_the_container_clears_a_stale_lock_and_drops_an_old_boot_reservation(self):
        boot_before = self.wait_for_boot_id()
        path = self.checkout(self.projects, "inside")
        lock = os.path.join(self.state, "lock")
        write(lock, "")
        reservation = os.path.join(self.state, "leases", "demo-slot-1.json")
        write(
            reservation,
            json.dumps(
                {
                    "holder": "ghost",
                    "project": "demo-slot-1",
                    "since": int(time.time()),
                    "key": "",
                    "ssh_port": 0,
                    "owner": "-",
                    "setup": "creating",
                    "pid": 1,
                    "boot_id": boot_before,
                }
            ),
        )
        probe = 'DEV_CONTAINER_LIB=1 DEV_CONTAINER_CALLER_PWD="$1" . /usr/local/bin/dev-container && live_lease_ids'
        before = self.inside("bash", "-c", probe, "sh", path)
        self.assertEqual(before.stdout.split(), ["demo-slot-1"])

        docker("restart", self.name)
        self.wait_for_boot_id(different_from=boot_before)

        self.assertFalse(os.path.exists(lock), "stale lock survived container restart")
        after = self.inside(
            "bash", "-c", probe + " && sweep_dead_reservations", "sh", path
        )
        self.assertEqual(after.stdout.split(), [])
        self.assertFalse(os.path.exists(reservation), "old-boot reservation survived sweep")

    def test_thin_commands_run_in_the_same_container_as_the_running_schedule(self):
        self.wait_for_stamp("fast")
        schedule_boot = self.wait_for_boot_id()
        ns_before = self.inside("readlink", "/proc/1/ns/pid").stdout.strip()
        os.remove(os.path.join(self.state, "boot-id"))

        path = self.checkout(self.projects, "inside")
        result = self.thin("list", "--brief", cwd=path)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.read_boot_id(), schedule_boot)
        self.assertEqual(self.inside("readlink", "/proc/1/ns/pid").stdout.strip(), ns_before)


if __name__ == "__main__":
    import unittest

    unittest.main()
