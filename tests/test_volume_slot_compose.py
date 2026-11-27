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
if [[ -n "${STRICT_DOCKER_ENV_LOG:-}" ]]; then
  printf '%s\tBUILDKIT=%s\tATTEST=%s\tGH=%s\tGITHUB=%s\n' \
    "$1 ${2:-}" \
    "${DOCKER_BUILDKIT-UNSET}" \
    "${BUILDX_NO_DEFAULT_ATTESTATIONS-UNSET}" \
    "${GH_TOKEN-UNSET}" \
    "${GITHUB_TOKEN-UNSET}" >> "$STRICT_DOCKER_ENV_LOG"
fi

if [[ "${1:-}" == buildx && "${2:-}" == version ]]; then
  [[ -z "${STRICT_DOCKER_NO_BUILDX:-}" ]] || exit 1
  exit 0
fi

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
    [[ -z "${STRICT_DOCKER_STOPPED:-}" ]] && echo "app"
    exit 0
  elif [[ "$verb_seen" == port ]]; then
    echo "42001"
    exit 0
  elif [[ "$verb_seen" == up || "$verb_seen" == down || "$verb_seen" == stop || "$verb_seen" == rm || "$verb_seen" == build ]]; then
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

    def test_slot_compose_sets_buildkit_only_for_build_and_up_and_memoizes_scrubbed_probe(self):
        env_log = os.path.join(self.harness.tmp, "strict_docker_env.log")
        slot_id = f"{PROJECT_PREFIX}-slot-1"
        script = f"""
        unset DOCKER_BUILDKIT BUILDX_NO_DEFAULT_ATTESTATIONS
        export GH_TOKEN=secret-gh GITHUB_TOKEN=secret-github
        source "{ENGINE}"
        slot_compose "{slot_id}" "{slot_id}" config --format json >/dev/null
        slot_compose "{slot_id}" "{slot_id}" ps -q >/dev/null
        slot_compose "{slot_id}" "{slot_id}" exec -T app true
        slot_compose "{slot_id}" "{slot_id}" build
        slot_compose "{slot_id}" "{slot_id}" up -d
        """
        proc = self._call_bash(script, STRICT_DOCKER_ENV_LOG=env_log)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        with open(env_log, encoding="utf-8") as f:
            lines = [l.strip() for l in f if l.strip()]

        buildx_probes = [l for l in lines if l.startswith("buildx version\t")]
        self.assertEqual(len(buildx_probes), 1, f"expected 1 memoized buildx probe, got: {lines}")
        self.assertIn("GH=UNSET\tGITHUB=UNSET", buildx_probes[0])

        compose_lines = [l for l in lines if l.startswith("compose ")]
        non_build_lines = compose_lines[:4]
        build_up_lines = compose_lines[4:]
        for line in non_build_lines:
            self.assertIn("BUILDKIT=UNSET", line, line)
        for line in build_up_lines:
            self.assertIn("BUILDKIT=1\tATTEST=1", line, line)

    def test_slot_compose_buildkit_fallback_without_buildx_and_respects_explicit_zero(self):
        env_log = os.path.join(self.harness.tmp, "strict_docker_env.log")
        slot_id = f"{PROJECT_PREFIX}-slot-1"
        script_nobx = f"""
        unset DOCKER_BUILDKIT BUILDX_NO_DEFAULT_ATTESTATIONS
        source "{ENGINE}"
        slot_compose "{slot_id}" "{slot_id}" build
        """
        proc = self._call_bash(script_nobx, STRICT_DOCKER_ENV_LOG=env_log, STRICT_DOCKER_NO_BUILDX="1")
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        with open(env_log, encoding="utf-8") as f:
            lines = [l.strip() for l in f if l.strip()]
        compose_build = [l for l in lines if l.startswith("compose ")][-1]
        self.assertIn("BUILDKIT=UNSET\tATTEST=1", compose_build)

        script_zero = f"""
        export DOCKER_BUILDKIT=0
        unset BUILDX_NO_DEFAULT_ATTESTATIONS
        source "{ENGINE}"
        slot_compose "{slot_id}" "{slot_id}" up -d
        """
        proc_zero = self._call_bash(script_zero, STRICT_DOCKER_ENV_LOG=env_log)
        self.assertEqual(proc_zero.returncode, 0, proc_zero.stderr.decode())
        with open(env_log, encoding="utf-8") as f:
            lines = [l.strip() for l in f if l.strip()]
        compose_up = [l for l in lines if l.startswith("compose ")][-1]
        self.assertIn("BUILDKIT=0\tATTEST=1", compose_up)

    def test_slot_helper_run_and_online_pass_uid_and_gid(self):
        slot_id = f"{PROJECT_PREFIX}-slot-1"
        script = f"""
        source "{ENGINE}"
        slot_helper_run "{slot_id}" true
        slot_helper_run_online "{slot_id}" true
        CONTAINER_UID=1001 CONTAINER_GID=1002 slot_helper_run "{slot_id}" true
        CONTAINER_UID=1001 CONTAINER_GID=1002 slot_helper_run_online "{slot_id}" true
        """
        proc = self._call_bash(script)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        with open(self.strict_calls_log, encoding="utf-8") as f:
            runs = [l.strip() for l in f if l.strip().startswith("run ")]
        self.assertEqual(len(runs), 4, runs)
        self.assertIn("--user 1000:1000", runs[0])
        self.assertIn("--user 1000:1000", runs[1])
        self.assertIn("--user 1001:1002", runs[2])
        self.assertIn("--user 1001:1002", runs[3])

    def test_volume_only_git_identity_and_holder_helpers_use_slot_exec_stream_not_compose_run(self):
        slot_id = f"{PROJECT_PREFIX}-slot-1"
        script = f"""
        unset DOCKER_STUB_STATE
        source "{ENGINE}"
        mkdir -p "$STATE/leases" "$STATE/env"
        printf 'GIT_USER_NAME=Bot\\nGIT_USER_EMAIL=bot@example.com\\n' > "$STATE/env/{slot_id}.env"
        echo '{{"holder":"task-65","owner":"Agent [a]","since":1700000000}}' > "$STATE/leases/{slot_id}.json"
        apply_checkout_git_identity "{slot_id}"
        write_holder_file "" "task-65" "Agent [a]" "{slot_id}" "1700000000"
        clear_holder_file "{slot_id}"
        """
        proc = self._call_bash(script, STRICT_DOCKER_STOPPED="1")
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        with open(self.strict_calls_log, encoding="utf-8") as f:
            calls = [l.strip() for l in f if l.strip()]
        compose_runs = [c for c in calls if c.startswith("compose ") and " run " in f" {c} "]
        self.assertEqual(compose_runs, [], f"unexpected compose run on stopped volume slot: {compose_runs}")
        helper_runs = [c for c in calls if c.startswith("run ")]
        self.assertGreaterEqual(len(helper_runs), 3, calls)
        self.assertTrue(any('safe.directory="*"' in c for c in helper_runs), helper_runs)


if __name__ == "__main__":
    unittest.main()
