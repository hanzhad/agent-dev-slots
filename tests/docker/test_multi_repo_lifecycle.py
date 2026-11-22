"""Real-Docker end-to-end tests for multi-repo facts, dirty/delivery checks,
multi-repo volume rescue archiving, and slot-fs ro guard + diff subdir (#50).
"""

import hashlib
import http.server
import json
import os
import socketserver
import subprocess
import threading
import urllib.parse

from dockerkit import FIXTURE_DIR, RealEngineCase, docker, needs_docker, write

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
class TestMultiRepoLifecycleDocker(RealEngineCase):
    """End-to-end real-Docker volume checks for #50 without stubs."""

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
        self.fixture = f"dct{self.uid}-fixture:local"
        self.addCleanup(docker, "rmi", "-f", self.fixture, check=False)
        built = docker(
            "build", "-q", "-t", self.fixture, "--build-arg", "BASE=" + self.image, FIXTURE_DIR, check=False
        )
        self.assertEqual(built.returncode, 0, built.stderr)

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

    def _in_engine_fs(self, checkout, op, slot, payload):
        raw = json.dumps(payload)
        return subprocess.run(
            [
                "docker", "exec", "-i", "-w", checkout, self.name,
                "env",
                f"DEV_CONTAINER_ENGINE_IMAGE={self.image}",
                f"DEV_CONTAINER_CALLER_PWD={checkout}",
                "dev-container", "fs", op, slot, "--stdin-json",
            ],
            input=raw,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=120,
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
        return f"http://{self.bridge_gw}:{self.port}/{name}.git", bare, shas

    def test_multi_repo_volume_fs_dirty_checks_and_rescue_archive_e2e(self):
        specs_url, _specs_bare, specs_shas = self._make_remote("specs", commits=("s1", "s2"))
        notes_url, _notes_bare, _ = self._make_remote("notes", commits=("n1",))
        tools_url, tools_bare, _ = self._make_remote("tools", commits=("t1",), extra_branch="v2")

        primary_bare = os.path.join(self.remotes_dir, "primary.git")
        subprocess.run(["git", "init", "-q", "--bare", primary_bare], check=True)
        subprocess.run(["git", "-C", primary_bare, "symbolic-ref", "HEAD", "refs/heads/main"], check=True)
        primary_url = f"http://{self.bridge_gw}:{self.port}/primary.git"

        checkout = os.path.join(self.projects, "mr50")
        compose_dir = os.path.join(checkout, ".devcontainer")
        proj_prefix = f"dct{self.uid}m50"
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
                "SLOT_REPOS_FILE=config/repos.json",
                "",
            ]),
        )
        write(
            os.path.join(compose_dir, ".env"),
            "GIT_USER_NAME=demo-agent\nGIT_USER_EMAIL=demo-agent@example.com\nGH_TOKEN=tok_docker_m50\n",
            mode=0o600,
        )
        write(os.path.join(compose_dir, "docker-compose.yml"), COMPOSE_YML)
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
            os.path.join(checkout, "config", "repos.json"),
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
                        "depth": 1,
                    },
                    {
                        "alias": "notes",
                        "path": "deps/notes",
                        "url": notes_url,
                        "mode": "rw",
                        "attach": "default",
                    },
                    {
                        "alias": "tools",
                        "path": "extras/tools",
                        "url": tools_url,
                        "mode": "rw",
                        "attach": "optional",
                        "branch": "v2",
                    },
                ],
            }),
        )
        write(os.path.join(checkout, ".github", "specs-pin"), specs_shas[0] + "\n")
        write(os.path.join(checkout, "src", "app.txt"), "root\n")

        self._git(checkout, "init", "-q", "-b", "main")
        self._git(
            checkout,
            "add",
            "project.env",
            ".devcontainer/docker-compose.yml",
            ".devcontainer/ctx/Dockerfile",
            "config/repos.json",
            ".github/specs-pin",
            "src/app.txt",
        )
        self._git(checkout, "commit", "-q", "-m", "init")
        self._git(checkout, "remote", "add", "origin", primary_bare)
        self._git(checkout, "push", "-q", "origin", "main")
        self._git(checkout, "fetch", "-q", "origin")
        self._git(checkout, "remote", "set-url", "origin", primary_url)

        acq = self._in_engine(checkout, "acquire", "--for", "m50-e2e", "--as", "E2E [m50]")
        self.assertEqual(acq.returncode, 0, acq.stdout + acq.stderr)
        att = self._in_engine(checkout, "attach", "slot-1", "--repo", "tools", "--for", "m50-e2e")
        self.assertEqual(att.returncode, 0, att.stdout + att.stderr)

        tok_file = os.path.join(self.state, "lease-tokens", f"{sid}.token")
        with open(tok_file, encoding="utf-8") as handle:
            tok = handle.read().strip()

        # 1. slot-fs ro guard & SLOT_REPOS_FILE guard & nested rw editing & subdir diff on real volume
        ro_write = self._in_engine_fs(
            checkout, "write", "slot-1",
            {"lease_token": tok, "path": "deps/specs/specs-0.txt", "content": "blocked\n"},
        )
        self.assertNotEqual(ro_write.returncode, 0, ro_write.stdout + ro_write.stderr)
        cfg_write = self._in_engine_fs(
            checkout, "write", "slot-1",
            {"lease_token": tok, "path": "config/repos.json", "content": "{}"},
        )
        self.assertNotEqual(cfg_write.returncode, 0, cfg_write.stdout + cfg_write.stderr)

        cfg_read = self._in_engine_fs(checkout, "read", "slot-1", {"path": "config/repos.json"})
        self.assertEqual(cfg_read.returncode, 0, cfg_read.stdout + cfg_read.stderr)
        self.assertIs(json.loads(cfg_read.stdout)["ok"], True)

        ro_read = self._in_engine_fs(checkout, "read", "slot-1", {"path": "deps/specs/specs-0.txt"})
        self.assertEqual(ro_read.returncode, 0, ro_read.stdout + ro_read.stderr)
        self.assertIs(json.loads(ro_read.stdout)["ok"], True)

        cids = docker(
            "ps", "-q",
            "--filter", f"label=com.docker.compose.project={sid}",
            "--filter", "label=com.docker.compose.service=app",
        ).stdout.split()
        self.assertEqual(len(cids), 1)
        cid = cids[0]

        # Make tracked edit & unpushed commit & untracked & .local inside /workspace/extras/tools
        docker(
            "exec", "-u", "node", cid, "sh", "-c",
            "cd /workspace/extras/tools && "
            "printf 'ignored.log\\n.local/\\nnode_modules/\\n' > .gitignore && "
            "git add .gitignore && git -c commit.gpgsign=false commit -q -m gitignore && "
            "git checkout -q -b tools-e2e && "
            "printf 'commit-work\\n' > cwork.txt && git add cwork.txt && "
            "git -c commit.gpgsign=false commit -q -m 'e2e tool commit' && "
            "printf 'tracked-edit\\n' > tools-0.txt && "
            "printf 'untracked-edit\\n' > new.txt && "
            "mkdir -p .local && printf '{\"k\":1}\\n' > .local/k.json && "
            "printf 'ignored\\n' > ignored.log",
        )

        diff_alias = self._in_engine_fs(checkout, "diff", "slot-1", {"subdir": "tools"})
        self.assertEqual(diff_alias.returncode, 0, diff_alias.stdout + diff_alias.stderr)
        diff_data = json.loads(diff_alias.stdout)
        self.assertIs(diff_data["ok"], True)
        self.assertIn("tools-0.txt", diff_data["diff"])
        self.assertIn("+tracked-edit", diff_data["diff"])

        bad_subdir = self._in_engine_fs(checkout, "diff", "slot-1", {"subdir": "src"})
        self.assertNotEqual(bad_subdir.returncode, 0, bad_subdir.stdout + bad_subdir.stderr)

        # 2. Dirty checks across sub-repo
        listed = self._in_engine(checkout, "list")
        self.assertEqual(listed.returncode, 0, listed.stdout + listed.stderr)
        self.assertIn("tools:", listed.stdout)

        rel = self._in_engine(checkout, "release", "slot-1", "--for", "m50-e2e")
        self.assertNotEqual(rel.returncode, 0, rel.stdout + rel.stderr)
        self.assertIn("tools:", rel.stderr)

        # 3. Evict archives dirty tools under repos/tools/ while omitting clean specs & notes
        evict = self._in_engine(checkout, "evict", "slot-1", "--yes", "--force")
        self.assertEqual(evict.returncode, 0, evict.stdout + evict.stderr)
        self.assertEqual(docker("volume", "inspect", f"{sid}-workspace", check=False).returncode != 0, True)

        key = hashlib.sha256(os.path.realpath(checkout).encode()).hexdigest()[:8]
        arch = os.path.join(self.state, "rescue", key, "slot-1")
        self.assertFalse(os.path.exists(os.path.join(arch, "repos", "specs")))
        self.assertFalse(os.path.exists(os.path.join(arch, "repos", "notes")))
        tools_arch = os.path.join(arch, "repos", "tools")
        for fname in ("commits.bundle", "changes.patch", "new-files.tar"):
            self.assertTrue(os.path.isfile(os.path.join(tools_arch, fname)), fname)

        with open(os.path.join(arch, "meta.json"), encoding="utf-8") as handle:
            meta = json.load(handle)
        self.assertEqual([r["alias"] for r in meta["repos"]], ["tools"])

        recover = os.path.join(self.tmp, "recover-e2e")
        subprocess.run(["git", "clone", "-q", tools_bare, recover], check=True)
        self._git(recover, "fetch", os.path.join(tools_arch, "commits.bundle"), "refs/heads/*:refs/heads/*")
        self._git(recover, "checkout", "-q", "tools-e2e")
        with open(os.path.join(recover, "cwork.txt"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "commit-work\n")
        self._git(recover, "apply", "--whitespace=nowarn", os.path.join(tools_arch, "changes.patch"))
        with open(os.path.join(recover, "tools-0.txt"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "tracked-edit\n")
        subprocess.run(["tar", "-xf", os.path.join(tools_arch, "new-files.tar"), "-C", recover], check=True)
        with open(os.path.join(recover, "new.txt"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "untracked-edit\n")
        with open(os.path.join(recover, ".local", "k.json"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), '{"k":1}\n')


if __name__ == "__main__":
    unittest.main()
