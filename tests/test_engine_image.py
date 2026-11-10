import os
import re
import unittest

from harness import REPO_ROOT

DOCKERFILE = os.path.join(REPO_ROOT, "image", "Dockerfile")
TOOLS = os.path.join(REPO_ROOT, "image", "tools.txt")

# What the engine needs at run time, by the package that carries it.
REQUIRED = {
    "bash", "coreutils", "git", "jq", "docker-cli", "docker-cli-compose",
    "openssh-keygen", "openssl", "curl", "tar", "tini", "github-cli",
}


def read_lines(path):
    assert os.path.isfile(path), f"{path} does not exist"
    with open(path, encoding="utf-8") as handle:
        return handle.read().splitlines()


def tools():
    lines = [line.split("#")[0].strip() for line in read_lines(TOOLS)]
    return [line for line in lines if line]


class EngineImageDefinition(unittest.TestCase):
    """The image is a Dockerfile and one allow-list of packages; no docker needed to check them."""

    def test_the_base_is_alpine_pinned_by_digest(self):
        froms = [line for line in read_lines(DOCKERFILE) if line.upper().startswith("FROM ")]
        self.assertEqual(len(froms), 1, froms)
        self.assertRegex(froms[0], r"^FROM\s+alpine(:[\w.]+)?@sha256:[0-9a-f]{64}(\s|$)")

    def test_tini_is_the_entrypoint(self):
        entry = [line for line in read_lines(DOCKERFILE) if line.upper().startswith("ENTRYPOINT")]
        self.assertEqual(len(entry), 1, entry)
        self.assertIn("tini", entry[0])

    def test_the_default_command_runs_the_schedule(self):
        cmds = [line for line in read_lines(DOCKERFILE) if line.upper().startswith("CMD")]
        self.assertEqual(len(cmds), 1, cmds)
        self.assertIn("dev-container", cmds[0])
        self.assertIn("schedule", cmds[0])
        self.assertIn("run", cmds[0])

    def test_bin_and_lib_are_copied_in(self):
        text = "\n".join(read_lines(DOCKERFILE))
        self.assertRegex(text, r"(?m)^COPY\s+(--\S+\s+)*bin/?\s+\S+")
        self.assertRegex(text, r"(?m)^COPY\s+(--\S+\s+)*lib/?\s+\S+")

    def test_packages_come_from_the_allow_list_file(self):
        self.assertIn("tools.txt", "\n".join(read_lines(DOCKERFILE)))

    def test_the_allow_list_holds_every_tool_the_engine_needs(self):
        self.assertEqual(REQUIRED - set(tools()), set())

    def test_the_allow_list_has_no_duplicates(self):
        listed = tools()
        self.assertEqual(len(listed), len(set(listed)), listed)


if __name__ == "__main__":
    unittest.main()
