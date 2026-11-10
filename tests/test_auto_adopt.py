import json
import os
import re
import subprocess
import unittest

_this_dir = os.path.dirname(os.path.abspath(__file__))
_repo_dir = os.path.abspath(os.path.join(_this_dir, ".."))
if os.path.exists(os.path.join(_repo_dir, "bin", "dev-container")):
    os.chdir(_repo_dir)

from harness import (
    DevContainerTestCase,
    REPO_ROOT,
    SCRIPT_REL,
    engine_source_lines,
    slot_port,
)

ENGINE = os.path.join(REPO_ROOT, SCRIPT_REL)
THIN = os.path.join(REPO_ROOT, "scripts", "dev-container-thin")


def output_of(result):
    return result.stdout.decode() + result.stderr.decode()


def read_text(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def write_text(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


class AutoAdoptTestCase(DevContainerTestCase):
    def setUp(self):
        super().setUp()
        self.harness.build_fixture_repo()

    def make_checkout(self, name="fresh-app", branch="main", with_origin=True, lock_file=None):
        path = os.path.join(self.harness.tmp, name)
        os.makedirs(path, exist_ok=True)
        write_text(os.path.join(path, "README.md"), f"{name}\n")
        if lock_file:
            write_text(os.path.join(path, lock_file), "# lockfile placeholder\n")
        git = lambda *args: subprocess.run(
            [self.harness.real_git_bin, "-C", path, *args],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        git("init", "-q")
        git("config", "user.email", "fixture@example.com")
        git("config", "user.name", "fixture")
        git("checkout", "-q", "-b", branch)
        git("add", "-A")
        git("commit", "-q", "-m", "initial")
        if with_origin:
            git("remote", "add", "origin", f"https://example.invalid/{name}.git")
        return path

    def engine_run(self, *args, caller_pwd=None, **extra_env):
        overrides = dict(extra_env)
        if caller_pwd is not None:
            overrides["DEV_CONTAINER_CALLER_PWD"] = caller_pwd
        return subprocess.run(
            ["bash", ENGINE, *args],
            cwd=self.harness.tmp,
            env=self.harness._env(overrides),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
        )

    def acquire(self, checkout, holder="worker", *args, **extra_env):
        return self.engine_run("acquire", "--for", holder, *args, caller_pwd=checkout, **extra_env)

    def adopt(self, checkout, *args, via_arg=False, **extra_env):
        if via_arg:
            return self.engine_run(
                "adopt", checkout, *args, caller_pwd=self.harness.fixture_repo, **extra_env
            )
        return self.engine_run("adopt", *args, caller_pwd=checkout, **extra_env)

    def doctor(self, checkout, **extra_env):
        return self.engine_run("doctor", caller_pwd=checkout, **extra_env)

    def git_status(self, checkout):
        result = subprocess.run(
            [self.harness.real_git_bin, "-C", checkout, "status", "--porcelain"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
        return result.stdout.decode().strip()

    def lease_files(self):
        leases_dir = os.path.join(self.harness.state_dir, "leases")
        if not os.path.isdir(leases_dir):
            return []
        return sorted(os.listdir(leases_dir))

    def test_first_acquire_detects_node_pnpm_and_connects_with_clean_git_status(self):
        checkout = self.make_checkout(name="pnpm-service", lock_file="pnpm-lock.yaml")
        self.assertFalse(os.path.exists(os.path.join(checkout, ".devcontainer")))
        self.assertFalse(os.path.exists(os.path.join(checkout, "project.env")))

        result = self.acquire(checkout, holder="worker")

        self.assertEqual(result.returncode, 0, output_of(result))
        out = output_of(result)
        self.assertRegex(out, r"(?i)connect|adopt|stack|pnpm")

        compose_path = os.path.join(checkout, ".devcontainer", "docker-compose.yml")
        self.assertTrue(os.path.isfile(compose_path), ".devcontainer/docker-compose.yml was not written")
        project_env_path = os.path.join(checkout, "project.env")
        self.assertTrue(os.path.isfile(project_env_path), "project.env was not written")
        self.assertFalse(os.path.lexists(os.path.join(checkout, "bin", "dev-container")),
                         "auto-connect writes no launcher: the global dev-container command is the launcher")

        self.assertEqual(self.git_status(checkout), "")
        for rel in ("project.env", "bin/dev-container", ".devcontainer/docker-compose.yml"):
            check = subprocess.run(
                [self.harness.real_git_bin, "-C", checkout, "check-ignore", "-q", rel],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertEqual(check.returncode, 0, f"{rel} is not ignored by git in {checkout}")

        leases = self.lease_files()
        self.assertEqual(len(leases), 1, f"expected exactly 1 lease, got {leases}")
        with open(os.path.join(self.harness.state_dir, "leases", leases[0]), encoding="utf-8") as handle:
            lease_data = json.load(handle)
        self.assertEqual(lease_data.get("holder"), "worker")

        slot_dir = self.harness.slot_dir(lease_data.get("project", ""))
        self.assertTrue(
            os.path.exists(os.path.join(slot_dir, "project.env"))
            or os.path.exists(os.path.join(slot_dir, ".devcontainer")),
            f"slot {slot_dir} does not contain connected files",
        )

        doc = self.doctor(checkout)
        self.assertEqual(doc.returncode, 0, output_of(doc))

    def test_first_acquire_detects_node_npm_and_connects(self):
        checkout = self.make_checkout(name="npm-service", lock_file="package-lock.json")
        result = self.acquire(checkout, holder="worker")
        self.assertEqual(result.returncode, 0, output_of(result))
        self.assertTrue(os.path.isfile(os.path.join(checkout, "project.env")))
        self.assertTrue(os.path.isfile(os.path.join(checkout, ".devcontainer", "docker-compose.yml")))
        self.assertEqual(self.git_status(checkout), "")
        self.assertEqual(len(self.lease_files()), 1)

    def test_first_acquire_detects_node_yarn_and_connects(self):
        checkout = self.make_checkout(name="yarn-service", lock_file="yarn.lock")
        result = self.acquire(checkout, holder="worker")
        self.assertEqual(result.returncode, 0, output_of(result))
        self.assertTrue(os.path.isfile(os.path.join(checkout, "project.env")))
        self.assertTrue(os.path.isfile(os.path.join(checkout, ".devcontainer", "docker-compose.yml")))
        self.assertEqual(self.git_status(checkout), "")
        self.assertEqual(len(self.lease_files()), 1)

    def test_first_acquire_detects_python_requirements_and_connects(self):
        checkout = self.make_checkout(name="py-req-service", lock_file="requirements.txt")
        result = self.acquire(checkout, holder="worker")
        self.assertEqual(result.returncode, 0, output_of(result))
        self.assertTrue(os.path.isfile(os.path.join(checkout, "project.env")))
        self.assertTrue(os.path.isfile(os.path.join(checkout, ".devcontainer", "docker-compose.yml")))
        self.assertEqual(self.git_status(checkout), "")
        self.assertEqual(len(self.lease_files()), 1)

    def test_first_acquire_detects_python_pyproject_and_connects(self):
        checkout = self.make_checkout(name="py-project-service", lock_file="pyproject.toml")
        result = self.acquire(checkout, holder="worker")
        self.assertEqual(result.returncode, 0, output_of(result))
        self.assertTrue(os.path.isfile(os.path.join(checkout, "project.env")))
        self.assertTrue(os.path.isfile(os.path.join(checkout, ".devcontainer", "docker-compose.yml")))
        self.assertEqual(self.git_status(checkout), "")
        self.assertEqual(len(self.lease_files()), 1)

    def test_no_central_list_of_repositories_in_state_or_engine_source(self):
        repo_name = "ephemeral-service"
        checkout = self.make_checkout(name=repo_name, lock_file="pnpm-lock.yaml")
        result = self.acquire(checkout, holder="worker")
        self.assertEqual(result.returncode, 0, output_of(result))

        allowed_dirs = {
            "leases",
            "ports",
            "env",
            "keys",
            "activity",
            "seen",
            "delivered",
            "closed",
            "parked",
            "rescue",
            "overlays",
            "lock",
            "boot-id",
            "compose",
            "scratch",
            "schedule",
            "limits.json",
            "tokens",
        }
        for entry in os.listdir(self.harness.state_dir):
            self.assertIn(entry, allowed_dirs, f"unexpected central entry in state: {entry}")

        for root, dirs, files in os.walk(self.harness.state_dir):
            for file in files:
                self.assertNotIn(
                    file,
                    ("repositories.json", "repos.json", "projects.json", "registry.txt", "repositories.txt"),
                )

        lines = engine_source_lines()
        for line in lines:
            self.assertNotIn(repo_name, line)

    def test_boot_id_stored_in_canonical_state_path_across_verbs(self):
        checkout = self.make_checkout(name="boot-id-service", lock_file="pnpm-lock.yaml")
        res = self.acquire(checkout, holder="worker")
        self.assertEqual(res.returncode, 0, output_of(res))

        canonical_boot_id = os.path.join(self.harness.state_dir, "boot-id")
        self.assertTrue(
            os.path.isfile(canonical_boot_id),
            f"boot-id was not written to canonical path: {canonical_boot_id}",
        )

        lock_dir = os.path.join(self.harness.state_dir, "lock")
        os.makedirs(lock_dir, exist_ok=True)
        list_res = self.engine_run("list", caller_pwd=checkout)
        self.assertTrue(os.path.isdir(lock_dir), "host-wide lock was unexpectedly cleared by list")
        self.assertNotIn("clearing the host-wide lock", output_of(list_res))

    def test_acquire_auto_connects_repo_with_existing_compose_and_no_project_env(self):
        compose_content = "services:\n  app:\n    image: node:22-bookworm\n"

        checkout_a = self.make_checkout(name="devcontainer-existing")
        write_text(os.path.join(checkout_a, ".devcontainer", "docker-compose.yml"), compose_content)
        subprocess.run(
            [self.harness.real_git_bin, "-C", checkout_a, "add", "-A"],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        subprocess.run(
            [self.harness.real_git_bin, "-C", checkout_a, "commit", "-q", "-m", "add devcontainer"],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )

        res_a = self.acquire(checkout_a, holder="worker")
        self.assertEqual(res_a.returncode, 0, output_of(res_a))
        project_env_a = os.path.join(checkout_a, "project.env")
        self.assertTrue(os.path.isfile(project_env_a), "project.env was not written for existing devcontainer")
        content_a = read_text(project_env_a)
        self.assertIn("PROJECT=devcontainer-existing", content_a)

        leases_a = [f for f in self.lease_files() if "devcontainer-existing" in f]
        self.assertTrue(leases_a, f"expected lease with prefix devcontainer-existing, got {self.lease_files()}")
        self.assertFalse(any(f.startswith("demo-") for f in self.lease_files()))

        self.engine_run("release", "slot-1", "--for", "worker", caller_pwd=checkout_a)
        checkout_b = self.make_checkout(name="root-compose-existing", lock_file="pnpm-lock.yaml")
        write_text(os.path.join(checkout_b, "docker-compose.yml"), compose_content)
        subprocess.run(
            [self.harness.real_git_bin, "-C", checkout_b, "add", "-A"],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        subprocess.run(
            [self.harness.real_git_bin, "-C", checkout_b, "commit", "-q", "-m", "add root compose"],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )

        res_b = self.acquire(checkout_b, holder="worker")
        self.assertEqual(res_b.returncode, 0, output_of(res_b))
        self.assertFalse(
            os.path.exists(os.path.join(checkout_b, ".devcontainer", "docker-compose.yml")),
            ".devcontainer/docker-compose.yml should not be written when root compose exists",
        )
        project_env_b = os.path.join(checkout_b, "project.env")
        self.assertTrue(os.path.isfile(project_env_b), "project.env was not written for root compose")
        content_b = read_text(project_env_b)
        self.assertIn("COMPOSE_DIR=.", content_b)
        self.assertIn("PROJECT=root-compose-existing", content_b)

    def test_slot_clone_anchors_git_exclude_path_at_workspace_root(self):
        checkout = self.make_checkout(name="custom-workdir-app")
        compose_content = "services:\n  app:\n    image: node:22-bookworm\n    working_dir: /\n"
        write_text(os.path.join(checkout, ".devcontainer", "docker-compose.yml"), compose_content)
        subprocess.run(
            [self.harness.real_git_bin, "-C", checkout, "add", "-A"],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        subprocess.run(
            [self.harness.real_git_bin, "-C", checkout, "commit", "-q", "-m", "add devcontainer with custom working_dir"],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )

        res = self.acquire(checkout, holder="worker")
        self.assertEqual(res.returncode, 0, output_of(res))

        leases = [f for f in self.lease_files() if "custom-workdir-app" in f]
        self.assertTrue(leases, f"no lease found for custom-workdir-app: {self.lease_files()}")
        with open(os.path.join(self.harness.state_dir, "leases", leases[0]), encoding="utf-8") as handle:
            lease_data = json.load(handle)

        slot_dir = self.harness.slot_dir(lease_data.get("project", ""))
        self.assertTrue(os.path.isdir(slot_dir), f"slot dir not found: {slot_dir}")
        self.assertEqual(self.git_status(slot_dir), "")

        for rel in ("project.env", "bin/dev-container"):  # a tracked .devcontainer/ keeps its own git rules
            check = subprocess.run(
                [self.harness.real_git_bin, "-C", slot_dir, "check-ignore", "--no-index", "-q", rel],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertEqual(check.returncode, 0, f"{rel} is not ignored in slot {slot_dir}")

        lines = engine_source_lines()
        exclude_calls = [line for line in lines if "rev-parse" in line and "info/exclude" in line]
        self.assertTrue(exclude_calls, "no rev-parse info/exclude call found in engine source")
        for line in exclude_calls:
            self.assertTrue(
                "--path-format=absolute" in line or "$ws/" in line or '"$ws/' in line,
                f"rev-parse info/exclude is not anchored to workspace root: {line}",
            )

    def test_unrecognised_stack_refuses_with_next_command_and_no_generic_usage(self):
        checkout = self.make_checkout(name="unrecognised-app")
        self.assertFalse(os.path.exists(os.path.join(checkout, ".devcontainer")))
        self.assertFalse(os.path.exists(os.path.join(checkout, "project.env")))

        result = self.acquire(checkout, holder="worker")

        self.assertNotEqual(result.returncode, 0, "acquire should refuse on unrecognised stack")
        out = output_of(result)
        self.assertNotIn("Usage:", out, "refusal dumped generic usage")
        self.assertRegex(out, r"(?i)unrecogni[sz]ed|unknown.*stack|stack")
        self.assertRegex(out, r"(?i)adopt|template|\.devcontainer")

        self.assertFalse(os.path.exists(os.path.join(checkout, "project.env")))
        self.assertEqual(len(self.lease_files()), 0)

    def test_acquire_runs_doctor_itself_and_prints_next_command_when_doctor_fails(self):
        checkout = self.make_checkout(name="doctor-fail-service", lock_file="pnpm-lock.yaml")
        busy_port = slot_port(1, 0)
        write_text(os.path.join(self.harness.docker_state, "busy.ports"), f"SSH_PORT={busy_port}\n")

        result = self.acquire(checkout, holder="worker")

        self.assertNotEqual(result.returncode, 0, "acquire should fail when doctor fails")
        out = output_of(result)
        self.assertNotIn("Usage:", out)
        self.assertRegex(out, r"(?i)doctor")

    def test_explicit_adopt_uses_same_code_path_and_preserves_compatibility(self):
        checkout = self.make_checkout(name="explicit-adopt-service", lock_file="pnpm-lock.yaml")

        result = self.adopt(checkout)

        self.assertEqual(result.returncode, 0, output_of(result))
        self.assertTrue(os.path.isfile(os.path.join(checkout, "project.env")))
        self.assertTrue(os.path.isfile(os.path.join(checkout, ".devcontainer", "docker-compose.yml")))
        self.assertFalse(os.path.lexists(os.path.join(checkout, "bin", "dev-container")))
        self.assertEqual(self.git_status(checkout), "")

        doc = self.doctor(checkout)
        self.assertEqual(doc.returncode, 0, output_of(doc))

        res_acq = self.acquire(checkout, holder="worker")
        self.assertEqual(res_acq.returncode, 0, output_of(res_acq))


    def test_stack_compose_includes_ssh_port_for_node_and_python(self):
        for stack_name, lock_file in (("node-stack", "package-lock.json"), ("python-stack", "requirements.txt")):
            with self.subTest(stack=stack_name):
                checkout = self.make_checkout(name=stack_name, lock_file=lock_file)
                result = self.adopt(checkout)
                self.assertEqual(result.returncode, 0, output_of(result))
                compose_path = os.path.join(checkout, ".devcontainer", "docker-compose.yml")
                self.assertTrue(os.path.isfile(compose_path))
                text = read_text(compose_path)
                self.assertIn("SSH_PORT", text)
                self.assertIn("127.0.0.1:${SSH_PORT:-2222}:22", text)
                project_env = read_text(os.path.join(checkout, "project.env"))
                self.assertIn("SLOT_PORTS=SSH_PORT", project_env)

    def test_adopt_writes_compose_dir_dotenv_even_if_root_dotenv_exists(self):
        checkout = self.make_checkout(name="root-dotenv-service", lock_file="pnpm-lock.yaml")
        write_text(os.path.join(checkout, ".env"), "APP_SECRET=topsecret\n")

        result = self.adopt(checkout)

        self.assertEqual(result.returncode, 0, output_of(result))
        target_env = os.path.join(checkout, ".devcontainer", ".env")
        self.assertTrue(os.path.isfile(target_env), f"{target_env} should exist even when root .env exists")
        content = read_text(target_env)
        self.assertIn("GIT_USER_NAME=", content)
        self.assertIn("GIT_USER_EMAIL=", content)
        root_dotenv = read_text(os.path.join(checkout, ".env"))
        self.assertIn("APP_SECRET=topsecret", root_dotenv)


if __name__ == "__main__":
    unittest.main()
