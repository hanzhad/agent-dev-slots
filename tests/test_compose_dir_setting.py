import os
import re
import subprocess
import unittest

from harness import DevContainerTestCase, REPO_ROOT, project_env_text

OTHER = "stack"
LITERAL = ".devcontainer"

# A line of lib/ or bin/ may spell the literal only as one of these. Everything else
# is a real path use and must come from COMPOSE_DIR instead.
ALLOWED = [
    # a comment
    re.compile(r"^\s*#"),
    # the default of the setting itself, or of the credential file inside it
    re.compile(r"COMPOSE_DIR:-\.devcontainer"),
    re.compile(r"FORGE_AUTH_FILE:-\.devcontainer"),
    re.compile(r"compose_dir=\"?\.devcontainer\"?(\s|$)", re.IGNORECASE),
    re.compile(r"credential=\"\.devcontainer/agent\.env\""),
    # a message for the person at the terminal: said by echo, die, a problem
    # report, the usage text, or the continuation line of one of those
    re.compile(r"^\s*echo\s"),
    re.compile(r"die\s+\""),
    re.compile(r"preflight_problem\s+\""),
    re.compile(r"^\s+\"[^\"]* [^\"]*\"\s*\?$"),
    re.compile(r"^\s+\$CALLER\s"),
]


def output_of(result):
    return result.stdout.decode() + result.stderr.decode()


def write_text(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


class NoLiteralComposeDirInTheEngine(unittest.TestCase):
    def test_every_use_of_the_literal_is_a_default_or_a_message(self):
        offenders = []
        sources = [os.path.join(REPO_ROOT, "bin", "dev-container")]
        lib = os.path.join(REPO_ROOT, "lib")
        sources += [os.path.join(lib, n) for n in sorted(os.listdir(lib)) if n.endswith(".sh")]
        for path in sources:
            with open(path, encoding="utf-8") as handle:
                for number, line in enumerate(handle, 1):
                    if LITERAL not in line:
                        continue
                    if any(rule.search(line) for rule in ALLOWED):
                        continue
                    rel = os.path.relpath(path, REPO_ROOT)
                    offenders.append(f"{rel}:{number}: {line.strip()}")
        self.assertEqual([], offenders, "path uses of the literal " + LITERAL)


class ComposeDirIsHonoured(DevContainerTestCase):
    def setUp(self):
        super().setUp()
        h = self.harness
        h.build_fixture_repo(project_env=project_env_text(
            COMPOSE_DIR=OTHER, SLOT_GIT_NAME="demo-agent", SLOT_GIT_EMAIL="demo-agent@example.com"
        ))
        git = lambda *args: subprocess.run(
            ["git", "-C", h.fixture_repo, *args],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        git("mv", LITERAL, OTHER)
        git("commit", "-q", "-m", "move the compose dir")

    def assert_no_literal_dir(self):
        found = []
        for root, dirs, _files in os.walk(self.harness.tmp):
            if ".git" in dirs:
                dirs.remove(".git")
            found += [os.path.join(root, d) for d in dirs if d == LITERAL]
        self.assertEqual([], found, f"a {LITERAL} directory appeared")

    def test_doctor_finds_the_compose_file_in_the_configured_dir(self):
        result = self.harness.run("doctor")
        text = output_of(result)
        self.assertIn(f"{OTHER}/docker-compose.yml", text)
        self.assertNotIn(LITERAL, text)
        self.assert_no_literal_dir()

    def test_acquire_and_the_image_build_use_the_configured_dir(self):
        h = self.harness
        result = h.run_acquire()
        self.assertEqual(0, result.returncode, output_of(result))
        name = h.acquired_slot_name(result)
        built = h.run("install", name)
        self.assertEqual(0, built.returncode, output_of(built))
        calls = h.compose_calls()
        # the stand-in logs a verb it does not know, such as build, as an empty one
        self.assertIn("", [c["verb"] for c in calls], "no image was built")
        for call in calls:
            where = " ".join([call["project_dir"], *call["files"]])
            self.assertNotIn(LITERAL, where)
        self.assertTrue(
            any(f"/{OTHER}" in c["project_dir"] or any(f"/{OTHER}/" in f for f in c["files"])
                for c in calls),
            calls,
        )
        self.assert_no_literal_dir()

    def test_adopt_of_a_checkout_with_the_other_dir_writes_nothing_to_the_literal_one(self):
        h = self.harness
        checkout = os.path.join(h.tmp, "alpha-app")
        write_text(os.path.join(checkout, "README.md"), "alpha\n")
        write_text(os.path.join(checkout, OTHER, "docker-compose.yml"), "services: {}\n")
        git = lambda *args: subprocess.run(
            [h.real_git_bin, "-C", checkout, *args],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        git("init", "-q")
        git("config", "user.email", "fixture@example.com")
        git("config", "user.name", "fixture")
        git("checkout", "-q", "-b", "main")
        git("add", "-A")
        git("commit", "-q", "-m", "initial")
        git("remote", "add", "origin", "https://example.invalid/alpha-app.git")
        env = h._env({"DEV_CONTAINER_CALLER_PWD": checkout})
        result = subprocess.run(
            ["bash", h.script, "adopt"], cwd=h.tmp, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60,
        )
        self.assertEqual(0, result.returncode, output_of(result))
        self.assertFalse(os.path.exists(os.path.join(checkout, LITERAL)), output_of(result))
        self.assertTrue(os.path.isfile(os.path.join(checkout, OTHER, "README.md")))


if __name__ == "__main__":
    unittest.main()
