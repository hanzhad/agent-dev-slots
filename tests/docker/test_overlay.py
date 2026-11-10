import json
import os
import re
import shutil
import subprocess
import unittest

from dockerkit import FIXTURE_DIR, REPO_ROOT, RealEngineCase, docker, needs_docker, write

FIXTURES = os.path.join(REPO_ROOT, "tests", "fixtures")
FOUR_GIB = 4 * 1024 ** 3


def read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def env_map(text):
    values = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        values[key.strip()] = val.strip().strip('"').strip("'")
    return values


@needs_docker
class OverlayBuildsOnFixtureProjects(RealEngineCase):
    """The generated slot overlay builds and starts on each fixture project's compose files."""

    def setUp(self):
        super().setUp()
        self.fixture = f"dct{self.uid}-fixture:local"
        self.addCleanup(docker, "rmi", "-f", self.fixture, check=False)
        built = docker("build", "-q", "-t", self.fixture, "--build-arg", "BASE=" + self.image, FIXTURE_DIR, check=False)
        self.assertEqual(built.returncode, 0, built.stderr)

    def fixture_projects(self):
        return sorted(
            d for d in os.listdir(FIXTURES) if os.path.isfile(os.path.join(FIXTURES, d, "project.env"))
        )

    def prepare_checkout(self, fixture_name, idx):
        src = os.path.join(FIXTURES, fixture_name)
        checkout = os.path.join(self.projects, fixture_name)
        shutil.copytree(src, checkout)
        pe = env_map(read(os.path.join(checkout, "project.env")))
        proj_prefix = f"dct{self.uid}{idx}"
        pe_text = re.sub(r"(?m)^PROJECT=.*$", "PROJECT=" + proj_prefix, read(os.path.join(checkout, "project.env")))
        write(os.path.join(checkout, "project.env"), pe_text)

        compose_dir = os.path.join(checkout, pe.get("COMPOSE_DIR", ".devcontainer"))
        first_file = os.path.join(compose_dir, pe["COMPOSE_FILES"].split()[0])
        compose_text = re.sub(
            r"(?m)^(\s*)image:\s*\S+",
            r"\1build: ./ctx",
            read(first_file),
        )
        write(first_file, compose_text)
        write(
            os.path.join(compose_dir, "ctx", "Dockerfile"),
            "FROM " + self.fixture + "\nCOPY marker.txt /marker.txt\n",
        )
        write(os.path.join(compose_dir, "ctx", "marker.txt"), fixture_name + "\n")

        branch = pe.get("DEFAULT_BRANCH", "master")
        origin = os.path.join(self.tmp, fixture_name + "-origin.git")
        for args in (
            ["init", "-q", "--bare", origin],
            ["-C", checkout, "init", "-q", "-b", branch],
            ["-C", checkout, "add", "."],
            ["-C", checkout, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "init"],
            ["-C", checkout, "remote", "add", "origin", origin],
        ):
            subprocess.run(["git", *args], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        head = subprocess.run(
            ["git", "-C", checkout, "rev-parse", "HEAD"], check=True, stdout=subprocess.PIPE, text=True
        ).stdout.strip()

        slot_dir = os.path.join(self.projects, "dev-containers", f"slot-{idx}")
        os.makedirs(slot_dir, exist_ok=True)
        for copy_rel in pe.get("SLOT_COPY_FILES", "").split():
            write(os.path.join(checkout, copy_rel), "{}\n")
        for mount_rel in pe.get("SLOT_MOUNT_DIRS", "").split():
            os.makedirs(os.path.join(checkout, mount_rel), exist_ok=True)
            os.makedirs(os.path.join(slot_dir, mount_rel), exist_ok=True)

        sid = f"{proj_prefix}-slot-{idx}"
        ports = pe["SLOT_PORTS"].split()
        port_lines = "".join(f"{name}={41000 + idx * 20 + i}\n" for i, name in enumerate(ports))
        write(os.path.join(self.state, "ports", f"{sid}.ports"), port_lines)
        write(
            os.path.join(self.state, "env", f"{sid}.env"),
            f"COMPOSE_PROJECT_NAME={sid}\n" + port_lines,
        )
        return checkout, compose_dir, slot_dir, sid, head

    def cleanup_project(self, compose_dir, sid):
        self.inside(
            "docker", "compose", "-p", sid, "down", "-v", "--remove-orphans",
            workdir=compose_dir, check=False,
        )
        imgs = docker(
            "image", "ls", "--filter", f"label=dev-container.compose-project={sid}",
            "-q", check=False,
        ).stdout.split()
        if imgs:
            docker("rmi", "-f", *imgs, check=False)

    def test_the_overlay_builds_and_sets_the_ceiling_and_labels_on_each_fixture_project(self):
        for idx, name in enumerate(self.fixture_projects(), start=1):
            with self.subTest(project=name):
                checkout, compose_dir, slot_dir, sid, head = self.prepare_checkout(name, idx)
                self.addCleanup(self.cleanup_project, compose_dir, sid)
                ran = self.inside(
                    "bash", "-c",
                    "export DEV_CONTAINER_CALLER_PWD=\"$1\" DEV_CONTAINER_LIB=1 && "
                    "source /usr/local/bin/dev-container && "
                    "build_slot_image \"$2\" \"$3\" && "
                    "slot_compose \"$2\" \"$3\" up -d && "
                    "echo \"BUILD_HASH=$(slot_build_hash)\"",
                    "bash", checkout, slot_dir, sid,
                    workdir=checkout, check=False,
                )
                self.assertEqual(ran.returncode, 0, ran.stdout + ran.stderr)
                imgs = docker(
                    "image", "ls", "--filter", f"label=dev-container.compose-project={sid}",
                    "--format", "{{.Repository}}:{{.Tag}}",
                ).stdout.split()
                self.assertTrue(imgs, f"no labelled image for {sid}")
                labels = self.inspect(imgs[0])["Config"]["Labels"]
                self.assertEqual(labels.get("dev-container.compose-project"), sid)
                built = re.search(r"^BUILD_HASH=([0-9a-f]+)$", ran.stdout, re.M)
                self.assertTrue(built, ran.stdout)
                self.assertEqual(labels.get("dev-container.build-hash"), built.group(1))
                self.assertEqual(labels.get("org.opencontainers.image.revision"), head)
                cids = docker(
                    "ps", "-q", "--filter", f"label=com.docker.compose.project={sid}",
                ).stdout.split()
                self.assertEqual(len(cids), 1, cids)
                info = self.inspect(cids[0])
                self.assertEqual(info["HostConfig"]["Memory"], FOUR_GIB)


if __name__ == "__main__":
    unittest.main()
