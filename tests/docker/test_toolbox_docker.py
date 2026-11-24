import json
import os
import subprocess
import time
import unittest

from dockerkit import RealEngineCase, docker, needs_docker, write

FOUR_GIB = 4 * 1024 ** 3
ONE_GIB = 1024 ** 3

COMPOSE_YML = r"""services:
  app:
    build: ./ctx
    user: node
    working_dir: /workspace
    command:
      - sh
      - -c
      - |
        cat <<'EOF' > /tmp/respond.sh
        #!/bin/sh
        cr=$$(printf '\r')
        while IFS= read -r l; do
          case "$$l" in
            ""|"$$cr") break ;;
          esac
        done
        printf 'HTTP/1.1 200 OK\r\nContent-Length: 12\r\nConnection: close\r\n\r\napp-http-ok\n'
        EOF
        chmod +x /tmp/respond.sh
        exec nc -lk -p 8080 -e /tmp/respond.sh
    volumes:
      - ${WORKSPACE_DIR:-..}:/workspace
    networks:
      - inside
    environment:
      HTTPS_PROXY: http://gateway:8888

networks:
  inside:
    internal: true
"""


@needs_docker
class TestToolboxDocker(RealEngineCase):
    """Real Docker verification of image/toolbox, internal network proxy bypass, and run --toolbox."""

    def setUp(self):
        super().setUp()
        self.fixture = self.ensure_fixture_image()
        self.toolbox_image = f"dct{self.uid}-toolbox:local"
        self.addCleanup(docker, "rmi", "-f", self.toolbox_image, check=False)

    def _in_engine(self, checkout, *args, check=False):
        return self.inside(
            "env",
            f"DEV_CONTAINER_TOOLBOX_IMAGE={self.toolbox_image}",
            f"DEV_CONTAINER_CALLER_PWD={checkout}",
            "dev-container",
            *args,
            workdir=checkout,
            check=check,
        )

    def _cleanup_slot(self, compose_dir, sid):
        self.inside("docker", "compose", "-p", sid, "down", "-v", "--remove-orphans", workdir=compose_dir, check=False)
        docker("volume", "rm", "-f", f"{sid}-workspace", check=False)
        imgs = docker("image", "ls", "--filter", f"label=dev-container.compose-project={sid}", "-q", check=False).stdout.split()
        if imgs:
            docker("rmi", "-f", *imgs, check=False)

    def test_toolbox_image_build_internal_network_and_run_toolbox_end_to_end(self):
        bundled = self.inside("test", "-f", "/opt/dev-container/toolbox/Dockerfile", check=False)
        self.assertEqual(bundled.returncode, 0, "image/Dockerfile did not bundle /opt/dev-container/toolbox/Dockerfile")

        checkout = os.path.join(self.projects, "tbproj")
        compose_dir = os.path.join(checkout, ".devcontainer")
        proj_prefix = f"dct{self.uid}tb"
        sid = f"{proj_prefix}-slot-1"
        self.addCleanup(self._cleanup_slot, compose_dir, sid)

        write(
            os.path.join(checkout, "project.env"),
            "\n".join([
                f"PROJECT={proj_prefix}",
                "DEFAULT_BRANCH=main",
                "SLOT_PORTS=SSH_PORT",
                "COMPOSE_FILES=docker-compose.yml",
                "FORGE_AUTH=token",
                "FORGE_AUTH_FILE=.devcontainer/.env",
                "",
            ]),
        )
        write(
            os.path.join(compose_dir, ".env"),
            "GIT_USER_NAME=demo-agent\nGIT_USER_EMAIL=demo-agent@example.com\nGH_TOKEN=tok_docker_tb_e2e\n",
            mode=0o600,
        )
        write(os.path.join(compose_dir, "docker-compose.yml"), COMPOSE_YML)
        write(os.path.join(compose_dir, "ctx", "Dockerfile"), f"FROM {self.fixture}\n")

        origin = os.path.join(self.tmp, "tbproj-origin.git")
        for args in (
            ["init", "-q", "--bare", origin],
            ["-C", checkout, "init", "-q", "-b", "main"],
            ["-C", checkout, "add", "project.env", ".devcontainer/docker-compose.yml", ".devcontainer/ctx/Dockerfile"],
            ["-C", checkout, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "init"],
            ["-C", checkout, "remote", "add", "origin", origin],
        ):
            subprocess.run(["git", *args], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        vol = f"{sid}-workspace"
        docker(
            "volume", "create",
            "--label", f"dev-container.sid={sid}",
            "--label", f"dev-container.project={proj_prefix}",
            "--label", f"dev-container.origin={origin}",
            "--label", f"dev-container.repo-root={checkout}",
            vol,
        )
        seeded = docker(
            "run", "--rm", "--user", "root", "-v", f"{vol}:/workspace", self.fixture,
            "sh", "-c", "chown 1000:1000 /workspace && printf 'from-slot\\n' > /workspace/seed.txt && chown 1000:1000 /workspace/seed.txt",
            check=False,
        )
        self.assertEqual(seeded.returncode, 0, seeded.stderr)

        write(os.path.join(self.state, "ports", f"{sid}.ports"), "SSH_PORT=41901\n")
        write(
            os.path.join(self.state, "env", f"{sid}.env"),
            f"COMPOSE_PROJECT_NAME={sid}\nSSH_PORT=41901\nGIT_USER_NAME=demo-agent\nGIT_USER_EMAIL=demo-agent@example.com\nGH_TOKEN=tok_docker_tb_e2e\n",
        )
        write(
            os.path.join(self.state, "leases", f"{sid}.json"),
            json.dumps({
                "holder": "tb-e2e",
                "owner": "E2E [tb]",
                "project": sid,
                "since": int(time.time()),
                "key": "/dev/null",
                "ssh_port": 41901,
                "repo_root": checkout,
            }),
        )

        # Before --toolbox: run --toolbox refuses cleanly.
        before = self._in_engine(checkout, "run", "slot-1", "--toolbox", "--", "true")
        self.assertNotEqual(before.returncode, 0)

        # up --toolbox lazily builds self.toolbox_image and starts app + toolbox.
        up_res = self._in_engine(checkout, "up", "slot-1", "--toolbox")
        self.assertEqual(up_res.returncode, 0, up_res.stdout + up_res.stderr)

        # Re-running ensure_toolbox_image when already present succeeds cleanly.
        ensure_again = self.inside(
            "bash", "-c",
            'export DEV_CONTAINER_CALLER_PWD="$1" DEV_CONTAINER_LIB=1 DEV_CONTAINER_TOOLBOX_IMAGE="$2" && '
            'source /usr/local/bin/dev-container && ensure_toolbox_image',
            "bash", checkout, self.toolbox_image,
            workdir=checkout, check=False,
        )
        self.assertEqual(ensure_again.returncode, 0, ensure_again.stdout + ensure_again.stderr)

        # container_service_of resolves uniquely to app even with toolbox active.
        svc_res = self.inside(
            "bash", "-c",
            'export DEV_CONTAINER_CALLER_PWD="$1" DEV_CONTAINER_LIB=1 && '
            'source /usr/local/bin/dev-container && container_service_of "" "$2"',
            "bash", checkout, sid,
            workdir=checkout, check=False,
        )
        self.assertEqual(svc_res.returncode, 0, svc_res.stdout + svc_res.stderr)
        self.assertEqual(svc_res.stdout.strip(), "app")

        # Inspect toolbox container config & labels.
        tb_cids = docker(
            "ps", "-q",
            "--filter", f"label=com.docker.compose.project={sid}",
            "--filter", "label=dev-container.role=toolbox",
        ).stdout.split()
        self.assertEqual(len(tb_cids), 1, tb_cids)
        tb_info = self.inspect(tb_cids[0])
        self.assertEqual(tb_info["HostConfig"]["Memory"], FOUR_GIB)
        self.assertEqual(tb_info["HostConfig"]["ShmSize"], ONE_GIB)
        self.assertIs(tb_info["HostConfig"]["Init"], True)

        # Execute inside toolbox: uid 1000, /workspace shared, GH_TOKEN unset,
        # tools + Playwright Chromium present, and internal network curl bypasses HTTPS_PROXY.
        probe_script = (
            'set -eu; '
            'test "$(id -u)" = 1000; '
            'test "$(pwd)" = /workspace; '
            'test -z "${GH_TOKEN+x}"; '
            'test "$PLAYWRIGHT_BROWSERS_PATH" = /ms-playwright; '
            'test -d /ms-playwright && [ -n "$(ls -A /ms-playwright)" ]; '
            'python3 --version >/dev/null; '
            'jq --version >/dev/null; '
            'curl --version >/dev/null; '
            'sqlite3 --version >/dev/null; '
            'psql --version >/dev/null; '
            'if python3 -c "import playwright" 2>/dev/null; then '
            'python3 -c "import os; from playwright.sync_api import sync_playwright; '
            'p = sync_playwright().start(); exe = p.chromium.executable_path; p.stop(); '
            'assert os.path.isfile(exe), exe"; '
            'else playwright --version >/dev/null; fi; '
            'find /ms-playwright -maxdepth 4 -type f -perm -100 '
            '\\( -name chrome -o -name headless_shell -o -name chromium \\) | grep -q .; '
            'printf "from-toolbox\\n" > /workspace/from_toolbox.txt; '
            'case ",$NO_PROXY," in *,app,*) ;; *) echo "NO_PROXY=$NO_PROXY" >&2; exit 1 ;; esac; '
            'case ",$no_proxy," in *,app,*) ;; *) echo "no_proxy=$no_proxy" >&2; exit 1 ;; esac; '
            'body=$(curl -fsS --retry 10 --retry-delay 1 --retry-all-errors http://app:8080/health); '
            'test "$body" = "app-http-ok"'
        )
        probe = self._in_engine(checkout, "run", "slot-1", "--toolbox", "--", "sh", "-c", probe_script)
        self.assertEqual(probe.returncode, 0, probe.stdout + probe.stderr)

        # Verify shared /workspace and GH_TOKEN isolation on app vs toolbox.
        app_cids = docker(
            "ps", "-q",
            "--filter", f"label=com.docker.compose.project={sid}",
            "--filter", "label=com.docker.compose.service=app",
        ).stdout.split()
        self.assertEqual(len(app_cids), 1, app_cids)
        app_seen = docker("exec", app_cids[0], "cat", "/workspace/from_toolbox.txt")
        self.assertEqual(app_seen.stdout.strip(), "from-toolbox")
        app_tok = docker("exec", app_cids[0], "sh", "-c", 'printf "%s" "${GH_TOKEN-UNSET}"')
        self.assertEqual(app_tok.stdout.strip(), "tok_docker_tb_e2e")

        # Exit code propagates cleanly.
        rc_probe = self._in_engine(checkout, "run", "slot-1", "--toolbox", "--", "sh", "-c", "exit 42")
        self.assertEqual(rc_probe.returncode, 42, rc_probe.stdout + rc_probe.stderr)

        # Teardown via down removes toolbox marker and containers.
        down_res = self._in_engine(checkout, "down", "slot-1", "--force")
        self.assertEqual(down_res.returncode, 0, down_res.stdout + down_res.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.state, "toolbox", sid)))


if __name__ == "__main__":
    unittest.main()
