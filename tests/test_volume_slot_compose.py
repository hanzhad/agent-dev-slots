import os
import subprocess
import unittest

from harness import (
    DevContainerTestCase,
    PROJECT_PREFIX,
    REPO_ROOT,
    SCRIPT_REL,
)

ENGINE = os.path.join(REPO_ROOT, SCRIPT_REL)

STRICT_DOCKER_STUB = r"""#!/usr/bin/env bash
set -u

calls_log="${STRICT_DOCKER_CALLS_LOG:-/dev/null}"
echo "$@" >> "$calls_log"

if [[ "${1:-}" == compose ]]; then
  skip_next=0
  verb_seen=""
  for arg in "${@:2}"; do
    if (( skip_next )); then
      skip_next=0
      continue
    fi
    case "$arg" in
      -f|--file|--project-directory|--env-file|-p|--project-name|--profile)
        skip_next=1
        continue
        ;;
      -*)
        continue
        ;;
      config|exec|ps|up|down|run|build|stop|rm|ls|port|version)
        verb_seen="$arg"
        break
        ;;
      *)
        echo "unknown docker compose command: $arg" >&2
        exit 1
        ;;
    esac
  done

  if [[ "$verb_seen" == config ]]; then
    jq -nc '{services: {app: {volumes: [{type: "bind", target: "/workspace"}]}}}'
    exit 0
  elif [[ "$verb_seen" == exec ]]; then
    exit 0
  elif [[ "$verb_seen" == ps ]]; then
    echo "app"
    exit 0
  elif [[ "$verb_seen" == port ]]; then
    echo "42001"
    exit 0
  elif [[ "$verb_seen" == up || "$verb_seen" == down || "$verb_seen" == stop || "$verb_seen" == rm ]]; then
    exit 0
  fi
elif [[ "${1:-}" == volume ]]; then
  exit 0
fi

exit 0
"""


class VolumeSlotComposeTestCase(DevContainerTestCase):
    def setUp(self):
        super().setUp()
        self.harness.build_fixture_repo()
        self.strict_calls_log = os.path.join(self.harness.tmp, "strict_docker_calls.log")
        self.harness._write_stub("docker", STRICT_DOCKER_STUB)

    def _call_bash(self, script, **extra_env):
        env = self.harness._env(extra_env)
        env["DEV_CONTAINER_LIB"] = "1"
        env["STRICT_DOCKER_CALLS_LOG"] = self.strict_calls_log
        return subprocess.run(
            ["bash", "-c", script],
            cwd=self.harness.tmp,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
        )

    def _compose_calls(self):
        if not os.path.exists(self.strict_calls_log):
            return []
        with open(self.strict_calls_log, encoding="utf-8") as f:
            lines = [line.strip() for line in f if line.strip()]
        return [line for line in lines if line.startswith("compose ")]

    def test_finish_acquire_for_volume_slot_runs_real_slot_compose_without_slot_id_subcommand(self):
        slot_id = f"{PROJECT_PREFIX}-slot-1"
        script = f"""
        source "{ENGINE}"
        mkdir -p "$STATE/ports" "$STATE/keys" "$STATE/leases" "$STATE/env"
        echo "SSH_PORT=42001" > "$STATE/ports/{slot_id}.ports"
        finish_acquire "" "{slot_id}" "worker" "owner [ref]"
        """
        proc = self._call_bash(script)
        err = proc.stderr.decode()
        self.assertNotIn("jq: parse error", err)
        self.assertNotIn(
            "no service in its compose config mounts the checkout at /workspace",
            err,
        )
        self.assertEqual(
            proc.returncode,
            0,
            f"finish_acquire failed with rc={proc.returncode}:\nstdout: {proc.stdout.decode()}\nstderr: {err}",
        )

        compose_calls = self._compose_calls()
        self.assertTrue(compose_calls, "no docker compose calls recorded")
        config_calls = [c for c in compose_calls if "config" in c.split()]
        self.assertTrue(config_calls, "docker compose config was never called")
        for call in compose_calls:
            tokens = call.split()
            for i, token in enumerate(tokens):
                if token == slot_id:
                    self.assertIn(
                        tokens[i - 1],
                        ("-p", "--project-name"),
                        f"slot ID {slot_id} passed as subcommand or positional arg in: {call}",
                    )

    def test_slot_compose_with_two_equal_slot_id_arguments_does_not_pass_slot_id_to_docker_compose(self):
        slot_id = f"{PROJECT_PREFIX}-slot-1"
        script = f"""
        source "{ENGINE}"
        container_service_of "{slot_id}" "{slot_id}"
        slot_compose "{slot_id}" "{slot_id}" config --format json
        """
        proc = self._call_bash(script)
        out = proc.stdout.decode()
        err = proc.stderr.decode()
        self.assertEqual(
            proc.returncode,
            0,
            f"commands failed with rc={proc.returncode}:\nstdout: {out}\nstderr: {err}",
        )
        lines = out.strip().splitlines()
        first_line = lines[0] if lines else ""
        self.assertEqual(first_line, "app", f"container_service_of output was: {out}")

        compose_calls = self._compose_calls()
        self.assertTrue(compose_calls, "no docker compose calls recorded")
        for call in compose_calls:
            tokens = call.split()
            for i, token in enumerate(tokens):
                if token == slot_id:
                    self.assertIn(
                        tokens[i - 1],
                        ("-p", "--project-name"),
                        f"slot ID {slot_id} passed as subcommand or positional arg in: {call}",
                    )

    def test_adopt_starter_template_and_readme_cover_project_requirements(self):
        checkout = os.path.join(self.harness.tmp, "bare-app")
        os.makedirs(checkout)
        subprocess.run([self.harness.real_git_bin, "-C", checkout, "init", "-q"], check=True)
        subprocess.run([self.harness.real_git_bin, "-C", checkout, "checkout", "-q", "-b", "main"], check=True)

        res = subprocess.run(
            ["bash", ENGINE, "adopt", "--template"],
            cwd=self.harness.tmp,
            env=self.harness._env({"DEV_CONTAINER_CALLER_PWD": checkout}),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
        )
        self.assertEqual(res.returncode, 0, f"adopt --template failed:\n{res.stderr.decode()}")
        compose_path = os.path.join(checkout, ".devcontainer", "docker-compose.yml")
        self.assertTrue(os.path.isfile(compose_path), "missing starter docker-compose.yml")
        with open(compose_path, encoding="utf-8") as f:
            compose_text = f.read()

        self.assertIn(
            "app:",
            compose_text,
            "starter compose file must define service 'app:' for container_service_of compatibility",
        )
        self.assertNotIn(
            "workspace:",
            compose_text,
            "starter compose service should be named 'app:', not 'workspace:'",
        )
        with open(os.path.join(checkout, ".devcontainer", "engine.Dockerfile"), encoding="utf-8") as f:
            dockerfile_text = f.read()
        self.assertRegex(
            dockerfile_text,
            r"(?m)^FROM\s+node:",
            "starter image must be based on a node: image, which has the unprivileged node user",
        )
        self.assertIn("openssh-server", dockerfile_text, "the engine reaches a slot over ssh")
        self.assertTrue(
            os.access(os.path.join(checkout, ".devcontainer", "engine-entrypoint.sh"), os.X_OK),
            "starter entrypoint.sh must be executable",
        )

        readme_path = os.path.join(REPO_ROOT, "README.md")
        self.assertTrue(os.path.isfile(readme_path), "missing README.md")
        with open(readme_path, encoding="utf-8") as f:
            readme = f.read()

        self.assertRegex(
            readme,
            r"(?i)compose\s+service.*named\s+`?app`?|service\s+named\s+`?app`?",
            "README.md must document required compose service named app",
        )
        self.assertRegex(
            readme,
            r"(?i)node.*(?:uid\s*)?1000|1000.*node|unprivileged.*node",
            "README.md must document required unprivileged user node (uid 1000)",
        )
        self.assertIn(
            "GIT_USER_NAME",
            readme,
            "README.md must document agent git identity env var GIT_USER_NAME",
        )
        self.assertIn(
            "GIT_USER_EMAIL",
            readme,
            "README.md must document agent git identity env var GIT_USER_EMAIL",
        )


if __name__ == "__main__":
    unittest.main()
