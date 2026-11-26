"""Real-Docker end-to-end tests for multi-repo catalog, acquire --repo, live attach,
and provisioned system git identity (#49).
"""

import http.server
import json
import os
import socketserver
import subprocess
import threading
import time
import urllib.parse

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


class _ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


def _make_git_http_handler(repos_root):
    class GitHTTPHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self._handle()

        def do_POST(self):
            self._handle()

        def log_message(self, format, *args):
            pass

        def _handle(self):
            parsed = urllib.parse.urlsplit(self.path)
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length > 0 else b""
            env = dict(os.environ)
            env.update(
                {
                    "GIT_PROJECT_ROOT": repos_root,
                    "GIT_HTTP_EXPORT_ALL": "1",
                    "PATH_INFO": parsed.path,
                    "QUERY_STRING": parsed.query,
                    "REQUEST_METHOD": self.command,
                    "CONTENT_TYPE": self.headers.get("Content-Type", ""),
                    "CONTENT_LENGTH": str(len(body)),
                    "REMOTE_USER": "git",
                    "SERVER_PROTOCOL": "HTTP/1.1",
                }
            )
            proc = subprocess.run(
                ["git", "http-backend"],
                input=body,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=30,
            )
            raw = proc.stdout
            sep = b"\r\n\r\n" if b"\r\n\r\n" in raw else b"\n\n"
            hdr_bytes, _, payload = raw.partition(sep)
            status = 200
            headers = []
            for line in hdr_bytes.decode("iso-8859-1").splitlines():
                if ":" not in line:
                    continue
                k, v = line.split(":", 1)
                if k.strip().lower() == "status":
                    status = int(v.strip().split()[0])
                else:
                    headers.append((k.strip(), v.strip()))
            self.send_response(status)
            for k, v in headers:
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    return GitHTTPHandler


@needs_docker
class TestMultiRepoDocker(RealEngineCase):
    """End-to-end checks on real Docker volumes and running containers without stubs."""

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

        self.remotes_dir = os.path.join(self.tmp, "remotes")
        os.makedirs(self.remotes_dir, exist_ok=True)
        self.httpd = _ThreadingHTTPServer(("0.0.0.0", 0), _make_git_http_handler(self.remotes_dir))
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.httpd.shutdown)
        self.addCleanup(self.httpd.server_close)

        gw_res = docker(
            "network", "inspect", "bridge", "--format", "{{(index .IPAM.Config 0).Gateway}}", check=False
        )
        self.bridge_gw = gw_res.stdout.strip() or "172.17.0.1"

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

    def _make_remote(self, name, *, commits=("c1", "c2"), extra_branch=None):
        bare = os.path.join(self.remotes_dir, f"{name}.git")
        work = os.path.join(self.tmp, f"work-{name}")
        subprocess.run(["git", "init", "-q", "--bare", bare], check=True)
        subprocess.run(["git", "-C", bare, "symbolic-ref", "HEAD", "refs/heads/main"], check=True)
        subprocess.run(["git", "clone", "-q", bare, work], check=True)
        self._git(work, "checkout", "-q", "-B", "main")
        shas = []
        for idx, msg in enumerate(commits):
            write(os.path.join(work, f"{name}-{idx}.txt"), f"{msg}\n")
            self._git(work, "add", f"{name}-{idx}.txt")
            self._git(work, "commit", "-q", "-m", msg)
            shas.append(self._git(work, "rev-parse", "HEAD").stdout.strip())
        self._git(work, "push", "-q", "origin", "main")
        if extra_branch:
            self._git(work, "checkout", "-q", "-b", extra_branch)
            write(os.path.join(work, "branch.txt"), f"{extra_branch}\n")
            self._git(work, "add", "branch.txt")
            self._git(work, "commit", "-q", "-m", extra_branch)
            self._git(work, "push", "-q", "origin", extra_branch)
        return f"http://{self.bridge_gw}:{self.port}/{name}.git", shas

    def test_acquire_attach_provision_and_release_multi_repo_volume_slot(self):
        specs_url, specs_shas = self._make_remote("specs", commits=("s1", "s2"))
        tools_url, _ = self._make_remote("tools", commits=("t1",), extra_branch="v2")
        pub_url, _ = self._make_remote("pub", commits=("p1", "p2"))

        primary_bare = os.path.join(self.remotes_dir, "primary.git")
        subprocess.run(["git", "init", "-q", "--bare", primary_bare], check=True)
        subprocess.run(["git", "-C", primary_bare, "symbolic-ref", "HEAD", "refs/heads/main"], check=True)
        primary_url = f"http://{self.bridge_gw}:{self.port}/primary.git"

        checkout = os.path.join(self.projects, "mrproj")
        compose_dir = os.path.join(checkout, ".devcontainer")
        proj_prefix = f"dct{self.uid}mr"
        sid = f"0-slot-1-{proj_prefix}"
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
                "SLOT_REPOS_FILE=.devcontainer/repos.json",
                "",
            ]),
        )
        write(
            os.path.join(compose_dir, ".env"),
            "GIT_USER_NAME=demo-agent\nGIT_USER_EMAIL=demo-agent@example.com\nGH_TOKEN=tok_docker_mr_e2e\n",
            mode=0o600,
        )
        write(os.path.join(compose_dir, "docker-compose.yml"), COMPOSE_YML)
        # Provide a dummy sshd process name and writable authorized_keys.d for acquire without network apk add.
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
        write(
            os.path.join(compose_dir, "repos.json"),
            json.dumps({
                "version": 1,
                "repos": [
                    {
                        "alias": "specs",
                        "path": "deps/specs",
                        "url": specs_url,
                        "mode": "ro",
                        "attach": "default",
                        "pin_file": ".github/specs-pin",
                    },
                    {
                        "alias": "tools",
                        "path": "extras/tools",
                        "url": tools_url,
                        "mode": "rw",
                        "attach": "optional",
                        "branch": "v2",
                    },
                    {
                        "alias": "pub",
                        "path": "extras/pub",
                        "url": pub_url,
                        "mode": "ro",
                        "attach": "optional",
                        "auth": "none",
                        "depth": 1,
                    },
                ],
            }),
        )
        write(os.path.join(checkout, ".github", "specs-pin"), specs_shas[0] + "\n")
        write(os.path.join(checkout, "README.md"), "primary\n")

        self._git(checkout, "init", "-q", "-b", "main")
        self._git(checkout, "add", "project.env", ".devcontainer/docker-compose.yml", ".devcontainer/ctx/Dockerfile", ".devcontainer/repos.json", ".github/specs-pin", "README.md")
        self._git(checkout, "commit", "-q", "-m", "init")
        self._git(checkout, "remote", "add", "origin", primary_bare)
        self._git(checkout, "push", "-q", "origin", "main")
        self._git(checkout, "fetch", "-q", "origin")
        self._git(checkout, "remote", "set-url", "origin", primary_url)

        acq = self._in_engine(checkout, "acquire", "--for", "mr-e2e", "--as", "E2E [mr]")
        self.assertEqual(acq.returncode, 0, acq.stdout + acq.stderr)

        cids_before = docker(
            "ps", "-q",
            "--filter", f"label=com.docker.compose.project={sid}",
            "--filter", "label=com.docker.compose.service=app",
        ).stdout.split()
        self.assertEqual(len(cids_before), 1, cids_before)
        cid = cids_before[0]

        # Inspect inside real running container: default pinned ro repo present, optional absent, system git identity set
        self.assertEqual(
            docker("exec", cid, "git", "-C", "/workspace/deps/specs", "rev-parse", "HEAD").stdout.strip(),
            specs_shas[0],
        )
        self.assertEqual(
            docker("exec", cid, "test", "!", "-e", "/workspace/extras/tools", check=False).returncode,
            0,
        )
        self.assertEqual(
            docker("exec", cid, "git", "-C", "/workspace", "status", "--porcelain").stdout.strip(),
            "",
        )
        self.assertEqual(
            docker("exec", cid, "git", "config", "--system", "user.name").stdout.strip(),
            "demo-agent",
        )
        self.assertEqual(
            docker("exec", cid, "git", "config", "--system", "tag.gpgsign").stdout.strip(),
            "true",
        )
        self.assertEqual(
            docker("exec", cid, "git", "-C", "/workspace/deps/specs", "config", "--local", "remote.origin.pushurl").stdout.strip(),
            "read-only://slot-repo-is-read-only--specs",
        )
        ro_commit = docker(
            "exec", "-u", "node", cid,
            "sh", "-c", "printf 'x\\n' > /workspace/deps/specs/x.txt && git -C /workspace/deps/specs add x.txt && git -C /workspace/deps/specs -c commit.gpgsign=false commit -m try",
            check=False,
        )
        self.assertNotEqual(ro_commit.returncode, 0)

        # Live attach with zero container restart
        att = self._in_engine(checkout, "attach", "slot-1", "--repo", "tools,pub", "--for", "mr-e2e")
        self.assertEqual(att.returncode, 0, att.stdout + att.stderr)
        cids_after = docker(
            "ps", "-q",
            "--filter", f"label=com.docker.compose.project={sid}",
            "--filter", "label=com.docker.compose.service=app",
        ).stdout.split()
        self.assertEqual(cids_after, [cid], "attach restarted the slot container")

        self.assertEqual(
            docker("exec", cid, "git", "-C", "/workspace/extras/tools", "symbolic-ref", "--short", "HEAD").stdout.strip(),
            "v2",
        )
        self.assertEqual(
            docker("exec", cid, "git", "-C", "/workspace/extras/pub", "rev-parse", "--is-shallow-repository").stdout.strip(),
            "true",
        )
        self.assertEqual(
            docker("exec", cid, "git", "-C", "/workspace", "status", "--porcelain").stdout.strip(),
            "",
        )
        rw_commit = docker(
            "exec", "-u", "node", cid,
            "sh", "-c", "printf 'ok\\n' > /workspace/extras/tools/ok.txt && git -C /workspace/extras/tools add ok.txt && git -C /workspace/extras/tools -c commit.gpgsign=false commit -m ok",
            check=False,
        )
        self.assertEqual(rw_commit.returncode, 0, rw_commit.stdout + rw_commit.stderr)

        st = self._in_engine(checkout, "status", "--json")
        self.assertEqual(st.returncode, 0, st.stdout + st.stderr)
        slots = json.loads(st.stdout)["slots"]
        self.assertEqual(len(slots), 1)
        aliases = {r["alias"] for r in slots[0]["repos"]}
        self.assertEqual(aliases, {"specs", "tools", "pub"})

        rel = self._in_engine(checkout, "release", "slot-1", "--for", "mr-e2e", "--discard")
        self.assertEqual(rel.returncode, 0, rel.stdout + rel.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.state, "repos", f"{sid}.json")))


if __name__ == "__main__":
    unittest.main()
