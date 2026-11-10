import os
import unittest

from dockerkit import FIXTURE_DIR, RealEngineCase, docker, needs_docker, write

COMPOSE = """name: {project}
services:
  app:
    image: {fixture}
    volumes:
      - ..:/workspace
      - ../../shared/hello.txt:/hello.txt:ro
    configs:
      - source: note
        target: /note.txt
  built:
    build: ./ctx
configs:
  note:
    file: ./note.txt
"""


@needs_docker
class ComposePathsResolveThroughTheEngine(RealEngineCase):
    """The engine's compose talks to the host's daemon, which reads bind sources
    as host paths: they must be the host path the caller sees."""

    def setUp(self):
        super().setUp()
        self.project = "dctp" + self.uid
        self.fixture = f"dct{self.uid}-fixture:local"
        self.addCleanup(docker, "rmi", "-f", self.fixture, self.project + "-built", check=False)
        built = docker("build", "-q", "-t", self.fixture, "--build-arg", "BASE=" + self.image, FIXTURE_DIR, check=False)
        self.assertEqual(built.returncode, 0, built.stderr)
        self.checkout = os.path.join(self.projects, "proj")
        self.compose_dir = os.path.join(self.checkout, ".devcontainer")
        write(os.path.join(self.compose_dir, "compose.yml"), COMPOSE.format(project=self.project, fixture=self.fixture))
        write(os.path.join(self.compose_dir, "note.txt"), "a config\n")
        write(os.path.join(self.compose_dir, "ctx", "Dockerfile"), "FROM " + self.fixture + "\nCOPY marker.txt /marker.txt\n")
        write(os.path.join(self.compose_dir, "ctx", "marker.txt"), "built here\n")
        write(os.path.join(self.projects, "shared", "hello.txt"), "shared file\n")
        self.addCleanup(self.down)
        up = self.inside("docker", "compose", "up", "-d", "--build", workdir=self.compose_dir, check=False)
        self.assertEqual(up.returncode, 0, up.stderr)

    def down(self):
        self.inside("docker", "compose", "down", "-v", "--remove-orphans", workdir=self.compose_dir, check=False)

    def container(self, service):
        ids = self.inside("docker", "compose", "ps", "-q", service, workdir=self.compose_dir).stdout.split()
        self.assertEqual(len(ids), 1, service)
        return ids[0]

    def mounts(self, service):
        return {m["Destination"]: m for m in self.inspect(self.container(service))["Mounts"]}

    def test_a_relative_bind_has_the_host_path_as_its_source(self):
        mount = self.mounts("app")["/workspace"]
        self.assertEqual((mount["Type"], mount["Source"]), ("bind", self.checkout))
        listing = docker("exec", "-u", "node", self.container("app"), "ls", "/workspace/.devcontainer").stdout
        self.assertIn("compose.yml", listing)

    def test_a_file_shared_from_outside_the_project_is_visible(self):
        mount = self.mounts("app")["/hello.txt"]
        self.assertEqual(mount["Source"], os.path.join(self.projects, "shared", "hello.txt"))
        said = docker("exec", "-u", "node", self.container("app"), "cat", "/hello.txt").stdout
        self.assertEqual(said.strip(), "shared file")

    def test_a_configs_file_entry_resolves_to_the_host_path(self):
        said = docker("exec", "-u", "node", self.container("app"), "cat", "/note.txt").stdout
        self.assertEqual(said.strip(), "a config")
        mount = self.mounts("app")["/note.txt"]
        self.assertEqual(mount["Source"], os.path.join(self.compose_dir, "note.txt"))

    def test_a_build_context_is_read_from_the_checkout(self):
        said = docker("exec", "-u", "node", self.container("built"), "cat", "/marker.txt").stdout
        self.assertEqual(said.strip(), "built here")

    def test_the_fixture_image_has_node_git_and_tar(self):
        said = docker("exec", "-u", "node", self.container("app"), "sh", "-c", "id -un && git --version && tar --version").stdout
        self.assertTrue(said.startswith("node\n"), said)


if __name__ == "__main__":
    unittest.main()
