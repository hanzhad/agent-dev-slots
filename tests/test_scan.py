"""Tests for scripts/scan.py.

Every planted value is assembled at run time from parts, so this file holds
nothing the scan would flag.
"""
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCAN = os.path.join(ROOT, "scripts", "scan.py")


def run_scan(*args, env=None):
    full_env = dict(os.environ)
    full_env.pop("SCAN_DENY_FILE", None)
    if env:
        full_env.update(env)
    proc = subprocess.run(
        [sys.executable, SCAN, *args],
        capture_output=True,
        text=True,
        env=full_env,
    )
    return proc.returncode, proc.stdout + proc.stderr


def fake_token():
    return "gh" + "p_" + "a1B2" * 9


def home_path(name="alice"):
    return "/ho" + "me/" + name + "/project"


def private_issue():
    return "acme" + "/" + "widget" + "#" + "7"


def other_email():
    return "bob" + "@" + "corp" + ".io"


class TreeCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = self._tmp.name

    def write(self, name, text):
        path = os.path.join(self.dir, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    def scan(self, **kw):
        return run_scan("--tree", self.dir, **kw)

    def assert_clean(self, text):
        self.write("f.txt", text)
        code, out = self.scan()
        self.assertEqual(code, 0, out)
        self.assertIn("0", out.strip().splitlines()[-1])

    def assert_finding(self, text, category):
        self.write("f.txt", text)
        code, out = self.scan()
        self.assertEqual(code, 1, out)
        self.assertIn("f.txt:1: " + category, out)


class TestCategories(TreeCase):
    def test_token_fails_then_passes_when_removed(self):
        self.assert_finding("key = " + fake_token() + "\n", "token")
        self.assert_clean("key = removed\n")

    def test_other_token_kinds(self):
        samples = [
            "github_" + "pat_" + "A" * 24,
            "sk-" + "ant-" + "x" * 30,
            "AK" + "IA" + "A" * 16,
            "xo" + "xb-" + "1234567890-abc",
            "-----BEGIN " + "RSA PRIVATE KEY-----",
        ]
        for sample in samples:
            with self.subTest(sample=sample[:6]):
                self.assert_finding(sample + "\n", "token")

    def test_token_value_never_printed(self):
        self.write("f.txt", fake_token() + "\n")
        _, out = self.scan()
        self.assertNotIn(fake_token(), out)
        self.assertNotIn("a1B2a1B2", out)

    def test_home_path_fails_then_passes_when_removed(self):
        self.assert_finding("cd " + home_path() + "\n", "home path")
        self.assert_clean("cd somewhere\n")

    def test_home_path_variants(self):
        for text in [
            "/Us" + "ers/" + "alice/x",
            "C:" + "\\Us" + "ers\\" + "alice",
            "C:" + "/Us" + "ers/" + "alice",
        ]:
            with self.subTest(text=text[:5]):
                self.assert_finding(text + "\n", "home path")

    def test_generic_home_names_pass(self):
        for name in ["node", "user", "runner"]:
            self.assert_clean("/ho" + "me/" + name + "/x\n")
            self.assert_clean("/Us" + "ers/" + name + "/x\n")
            self.assert_clean("C:" + "\\Us" + "ers\\" + name + "\\x\n")

    def test_email_fails_and_allowed_pass(self):
        self.assert_finding("by " + other_email() + "\n", "email")
        allowed = [
            "1234+someone@users.noreply.github.com",
            "noreply@github.com",
            "noreply@anthropic.com",
            "a@example.com",
            "a@example.org",
            "a@example.net",
        ]
        for addr in allowed:
            with self.subTest(addr=addr):
                self.assert_clean(addr + "\n")

    def test_issue_reference_fails_then_passes_when_removed(self):
        self.assert_finding("see " + private_issue() + "\n", "issue reference")
        self.assert_clean("see nothing\n")

    def test_own_repository_reference_passes(self):
        self.assert_clean("see hanzhad/agent-dev-slots#12\n")
        self.assert_clean(
            "https://github.com/hanzhad/agent-dev-slots/issues/12\n"
        )

    def test_foreign_issue_url_fails(self):
        url = "https://github" + ".com/" + "acme/widget" + "/pull/" + "5"
        self.assert_finding(url + "\n", "issue reference")

    def test_bare_issue_numbers(self):
        self.assert_clean("fixes #12\n")
        self.assert_finding("fixes #" + "500\n", "issue reference")
        self.assert_clean("color: #" + "123456;\n")
        self.assert_clean("color: #" + "ff0000;\n")
        self.assert_clean("page.html#" + "500\n")

    def test_adr_and_docs_references(self):
        self.assert_finding("per " + "ADR" + "-" + "12\n", "issue reference")
        self.assert_finding("see " + "docs/" + "00" + "15 here\n", "issue reference")

    def test_git_dir_and_pycache_skipped(self):
        os.mkdir(os.path.join(self.dir, ".git"))
        with open(os.path.join(self.dir, ".git", "x"), "w") as fh:
            fh.write(fake_token())
        os.mkdir(os.path.join(self.dir, "__pycache__"))
        with open(os.path.join(self.dir, "__pycache__", "m.pyc"), "wb") as fh:
            fh.write(bytes([0, 1, 2]))
        code, out = self.scan()
        self.assertEqual(code, 0, out)

    def test_binary_file_is_a_finding_without_reading_it(self):
        with open(os.path.join(self.dir, "b.bin"), "wb") as fh:
            fh.write(bytes([0]) + fake_token().encode())
        code, out = self.scan()
        self.assertEqual(code, 1, out)
        self.assertIn("b.bin:0: binary file", out)
        self.assertNotIn("token", out)
        self.assertNotIn(fake_token(), out)

    def test_single_file(self):
        path = self.write("one.txt", fake_token())
        code, out = run_scan("--tree", path)
        self.assertEqual(code, 1, out)


class TestDenyList(TreeCase):
    def test_deny_term_fails_without_echo(self):
        term = "Zeta" + "Quux" + "Project"
        deny = self.write("deny.txt", "other" + "thing\n" + term + "\n")
        os.mkdir(os.path.join(self.dir, "src"))
        self.write(os.path.join("src", "a.txt"), "mentions " + term.lower() + "\n")
        code, out = run_scan(
            "--tree", os.path.join(self.dir, "src"), "--deny-file", deny
        )
        self.assertEqual(code, 1, out)
        self.assertIn("deny-list entry 2", out)
        self.assertNotIn(term.lower(), out.lower())

    def test_deny_file_from_environment(self):
        term = "Zeta" + "Quux"
        deny = self.write("deny.txt", term + "\n")
        os.mkdir(os.path.join(self.dir, "src"))
        self.write(os.path.join("src", "a.txt"), term + "\n")
        code, out = run_scan(
            "--tree", os.path.join(self.dir, "src"), env={"SCAN_DENY_FILE": deny}
        )
        self.assertEqual(code, 1, out)
        self.assertIn("deny-list entry 1", out)

    def test_deny_term_matches_across_separators(self):
        deny = self.write("deny.txt", "zeta" + "-" + "quux" + "\n")
        os.mkdir(os.path.join(self.dir, "src"))
        variants = [
            "zeta" + "_" + "quux",
            "zeta" + "." + "quux",
            "zeta" + " " + "quux",
            "zeta" + "quux",
            "Zeta" + "Quux",
            "ZETA" + "_" + "QUUX" + "_" + "HOME",
            "run_" + "zeta" + "quux" + "_now",
        ]
        for index, variant in enumerate(variants):
            self.write(os.path.join("src", "a.txt"), "x = " + variant + "\n")
            code, out = run_scan(
                "--tree", os.path.join(self.dir, "src"), "--deny-file", deny
            )
            self.assertEqual(code, 1, (index, out))
            self.assertIn("deny-list entry 1", out)
            self.assertNotIn("quux", out.lower())

    def test_deny_term_in_file_name_across_separators(self):
        deny = self.write("deny.txt", "zeta" + "-" + "quux" + "\n")
        os.mkdir(os.path.join(self.dir, "src"))
        self.write(os.path.join("src", "zeta" + "_" + "quux.txt"), "harmless\n")
        code, out = run_scan(
            "--tree", os.path.join(self.dir, "src"), "--deny-file", deny
        )
        self.assertEqual(code, 1, out)
        self.assertIn("deny-list entry 1", out)

    def test_deny_term_split_across_words_passes(self):
        deny = self.write("deny.txt", "zeta" + "-" + "quux" + "\n")
        os.mkdir(os.path.join(self.dir, "src"))
        self.write(os.path.join("src", "a.txt"), "zeta and quux apart\n")
        code, out = run_scan(
            "--tree", os.path.join(self.dir, "src"), "--deny-file", deny
        )
        self.assertEqual(code, 0, out)

    def test_deny_term_absent_passes(self):
        deny = self.write("deny.txt", "Zeta" + "Quux\n")
        os.mkdir(os.path.join(self.dir, "src"))
        self.write(os.path.join("src", "a.txt"), "harmless\n")
        code, out = run_scan(
            "--tree", os.path.join(self.dir, "src"), "--deny-file", deny
        )
        self.assertEqual(code, 0, out)


class TestSelf(unittest.TestCase):
    def test_scan_passes_on_itself_and_its_tests(self):
        for path in (SCAN, os.path.abspath(__file__)):
            code, out = run_scan("--tree", path)
            self.assertEqual(code, 0, out)


GIT_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
}


class TestHistory(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = self._tmp.name
        self.git("init", "-q")

    def git(self, *args, email="1+t@users.noreply.github.com", message=None):
        env = dict(os.environ, **GIT_ENV)
        env.update(
            GIT_AUTHOR_NAME="t",
            GIT_COMMITTER_NAME="t",
            GIT_AUTHOR_EMAIL=email,
            GIT_COMMITTER_EMAIL=email,
        )
        proc = subprocess.run(
            ["git", "-C", self.repo, "-c", "commit.gpgsign=false", *args],
            capture_output=True,
            text=True,
            env=env,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout.strip()

    def commit(self, text=None, message="plain message", email=None):
        if text is not None:
            with open(os.path.join(self.repo, "f.txt"), "w") as fh:
                fh.write(text)
            self.git("add", "f.txt")
        kw = {"email": email} if email else {}
        self.git("commit", "-q", "--allow-empty", "-m", message, **kw)

    def _run(self, *extra):
        proc = subprocess.run(
            [sys.executable, SCAN, "--history", *extra],
            capture_output=True,
            text=True,
            cwd=self.repo,
        )
        return proc.returncode, proc.stdout + proc.stderr

    def test_clean_history_passes(self):
        self.commit("hello\n")
        code, out = self._run()
        self.assertEqual(code, 0, out)

    def test_finds_message(self):
        self.commit("hello\n")
        self.commit(message="note " + private_issue())
        code, out = self._run()
        self.assertEqual(code, 1, out)
        self.assertIn("issue reference", out)

    def test_finds_line_removed_later(self):
        self.commit("key = " + fake_token() + "\n")
        self.commit("key = gone\n")
        code, out = self._run()
        self.assertEqual(code, 1, out)
        self.assertIn("token", out)
        self.assertNotIn(fake_token(), out)

    def test_finds_root_commit(self):
        self.commit("cd " + home_path() + "\n")
        self.commit("fine\n")
        code, out = self._run()
        self.assertEqual(code, 1, out)
        self.assertIn("home path", out)

    def test_finds_commit_email(self):
        self.commit("hello\n", email=other_email())
        code, out = self._run()
        self.assertEqual(code, 1, out)
        self.assertIn("email", out)
        self.assertNotIn(other_email(), out)

    def test_finds_trailer_email(self):
        self.commit(message="x\n\nCo-Authored-By: B <" + other_email() + ">")
        code, out = self._run()
        self.assertEqual(code, 1, out)

    def test_range_limits_commits(self):
        self.commit("key = " + fake_token() + "\n")
        self.commit("key = gone\n")
        base = self.git("rev-parse", "HEAD")
        self.commit("more\n")
        code, out = self._run("--range", base + "..HEAD")
        self.assertEqual(code, 0, out)

    def test_deny_term_in_history_not_echoed(self):
        term = "Zeta" + "Quux"
        self.commit(message="about " + term)
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as fh:
            fh.write(term + "\n")
        self.addCleanup(os.unlink, fh.name)
        code, out = self._run("--deny-file", fh.name)
        self.assertEqual(code, 1, out)
        self.assertIn("deny-list entry 1", out)
        self.assertNotIn(term.lower(), out.lower())


class TestFileNames(TreeCase):
    def test_email_in_file_name(self):
        name = other_email() + ".txt"
        self.write(name, "harmless\n")
        code, out = self.scan()
        self.assertEqual(code, 1, out)
        self.assertIn("email", out)
        self.assertNotIn(name, out)

    def test_home_path_in_directory_names(self):
        os.makedirs(os.path.join(self.dir, "ho" + "me", "alice"))
        self.write(os.path.join("ho" + "me", "alice", "x.txt"), "harmless\n")
        code, out = self.scan()
        self.assertEqual(code, 1, out)
        self.assertIn("home path", out)
        self.assertNotIn("alice", out)

    def test_deny_term_in_file_name(self):
        term = "Zeta" + "Quux"
        deny = self.write("deny.txt", term + "\n")
        os.mkdir(os.path.join(self.dir, "src"))
        self.write(os.path.join("src", term.lower() + ".txt"), "harmless\n")
        code, out = run_scan(
            "--tree", os.path.join(self.dir, "src"), "--deny-file", deny
        )
        self.assertEqual(code, 1, out)
        self.assertIn("deny-list entry 1", out)
        self.assertNotIn(term.lower(), out.lower())

    def test_plain_file_names_pass(self):
        self.write("notes-1.txt", "harmless\n")
        code, out = self.scan()
        self.assertEqual(code, 0, out)


class TestHistoryNamesAndMetadata(TestHistory):
    def deny_file(self, term):
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as fh:
            fh.write(term + "\n")
        self.addCleanup(os.unlink, fh.name)
        return fh.name

    def add_file(self, name):
        with open(os.path.join(self.repo, name), "w") as fh:
            fh.write("harmless\n")
        self.git("add", name)
        self.git("commit", "-q", "-m", "add a file")

    def test_added_binary_file_is_a_finding(self):
        with open(os.path.join(self.repo, "b.bin"), "wb") as fh:
            fh.write(bytes([0, 1, 2, 3]))
        self.git("add", "b.bin")
        self.git("commit", "-q", "-m", "add a binary")
        self.commit("fine" + chr(10))
        code, out = self._run()
        self.assertEqual(code, 1, out)
        self.assertIn("[path]:0: binary file", out)

    def test_added_file_name_with_email(self):
        name = other_email() + ".txt"
        self.add_file(name)
        code, out = self._run()
        self.assertEqual(code, 1, out)
        self.assertIn("email", out)
        self.assertNotIn(name, out)

    def test_added_file_name_with_deny_term(self):
        term = "Zeta" + "Quux"
        self.add_file(term + ".txt")
        code, out = self._run("--deny-file", self.deny_file(term))
        self.assertEqual(code, 1, out)
        self.assertIn("deny-list entry 1", out)
        self.assertNotIn(term.lower(), out.lower())

    def test_allowed_email_metadata_exempt_from_deny(self):
        term = "users" + ".noreply"
        self.commit(
            "hello\n",
            message="x\n\nCo-Authored-By: B <b@users.noreply.github.com>",
        )
        code, out = self._run("--deny-file", self.deny_file(term))
        self.assertEqual(code, 0, out)

    def test_denied_term_still_found_in_message_and_diff(self):
        term = "users" + ".noreply"
        self.commit(message="mentions " + term)
        code, out = self._run("--deny-file", self.deny_file(term))
        self.assertEqual(code, 1, out)
        self.assertIn("[message]", out)
        self.commit("see " + term + "\n")
        code, out = self._run("--deny-file", self.deny_file(term))
        self.assertIn("[diff]", out)


if __name__ == "__main__":
    unittest.main()
