import os
import shutil
import stat
import subprocess
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB = os.path.join(REPO_ROOT, "lib", "signing.sh")


def sh(body, *args, umask=None):
    """Run `body` with lib/signing.sh sourced; $1.. are `args`."""
    prefix = f"umask {umask}; " if umask else ""
    return subprocess.run(
        ["bash", "-c", prefix + '. "$0"; ' + body, LIB, *args],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
    )


def mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


class SigningCase(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="signing-state-")
        self.addCleanup(shutil.rmtree, self.state, True)

    def key(self, project="demo"):
        return os.path.join(self.state, "signing", project, "container_signing")

    def ensure(self, project="demo", **kw):
        return sh('ensure_signing_key "$1" "$2"', self.state, project, **kw)


class KeyPath(SigningCase):
    def test_the_path_is_under_the_state_dir_per_project(self):
        r = sh('signing_key_path "$1" "$2"', "/s", "demo")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout.strip(), "/s/signing/demo/container_signing")

    def test_a_project_name_that_could_leave_the_directory_is_rejected(self):
        for name in ("a/b", "/abs", "..", "a..b", "../x", ".", ""):
            with self.subTest(name=name):
                r = sh('signing_key_path "$1" "$2"', "/s", name)
                self.assertNotEqual(r.returncode, 0)
                self.assertEqual(r.stdout, "")
                self.assertNotEqual(r.stderr.strip(), "")


class RegistrationHint(SigningCase):
    def hint(self, mode_name, account="example-agent", pub="/s/signing/demo/container_signing.pub"):
        return sh('signing_registration_hint "$1" "$2" "$3"', mode_name, account, pub)

    def test_token_mode_gives_the_exact_gh_command_for_the_account(self):
        r = self.hint("container-env")
        self.assertEqual(r.returncode, 0)
        self.assertIn("gh ssh-key add /s/signing/demo/container_signing.pub --type signing --title", r.stdout)
        self.assertIn("example-agent", r.stdout)

    def test_app_mode_says_a_bot_cannot_register_keys_and_gives_no_command(self):
        r = self.hint("github-app", account="my-bot[bot]")
        self.assertEqual(r.returncode, 0)
        self.assertNotIn("gh ssh-key add", r.stdout)
        self.assertIn("cannot register", r.stdout)
        self.assertIn("API", r.stdout)
        self.assertIn("Verified", r.stdout)

    def test_an_unknown_mode_is_an_error(self):
        r = self.hint("carrier-pigeon")
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "")


class EnsureKey(SigningCase):
    def test_creates_an_ed25519_pair_and_prints_the_private_path(self):
        r = self.ensure()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), self.key())
        self.assertTrue(os.path.isfile(self.key()))
        info = subprocess.run(["ssh-keygen", "-l", "-f", self.key() + ".pub"],
                              stdout=subprocess.PIPE, universal_newlines=True)
        self.assertIn("ED25519", info.stdout)

    def test_the_key_has_no_passphrase(self):
        self.ensure()
        r = subprocess.run(["ssh-keygen", "-y", "-P", "", "-f", self.key()],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(self.key() + ".pub", encoding="utf-8") as handle:
            self.assertEqual(r.stdout.split()[:2], handle.read().split()[:2])

    def test_modes_are_private_whatever_the_umask(self):
        self.ensure(umask="000")
        self.assertEqual(mode(self.key()), 0o600)
        self.assertEqual(mode(os.path.dirname(self.key())), 0o700)

    def test_a_second_call_never_regenerates(self):
        self.ensure()
        with open(self.key(), "rb") as a, open(self.key() + ".pub", "rb") as b:
            before = (a.read(), b.read())
        r = self.ensure()
        self.assertEqual(r.stdout.strip(), self.key())
        with open(self.key(), "rb") as a, open(self.key() + ".pub", "rb") as b:
            self.assertEqual(before, (a.read(), b.read()))

    def test_a_missing_public_half_is_rebuilt_from_the_private_one(self):
        self.ensure()
        with open(self.key() + ".pub", encoding="utf-8") as handle:
            before = handle.read().split()[:2]
        os.remove(self.key() + ".pub")
        self.assertEqual(self.ensure().returncode, 0)
        with open(self.key() + ".pub", encoding="utf-8") as handle:
            self.assertEqual(handle.read().split()[:2], before)

    def test_a_public_half_without_its_private_one_is_refused_not_replaced(self):
        os.makedirs(os.path.dirname(self.key()))
        with open(self.key() + ".pub", "w", encoding="utf-8") as handle:
            handle.write("ssh-ed25519 AAAA orphan\n")
        r = self.ensure()
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(os.path.exists(self.key()))
        self.assertNotEqual(r.stderr.strip(), "")

    def test_projects_get_their_own_keys(self):
        self.ensure("one")
        self.ensure("two")
        with open(self.key("one") + ".pub", encoding="utf-8") as a, open(self.key("two") + ".pub", encoding="utf-8") as b:
            self.assertNotEqual(a.read().split()[1], b.read().split()[1])

    def test_a_bad_project_name_creates_nothing(self):
        r = self.ensure("../escape")
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "")
        self.assertEqual(os.listdir(self.state), [])


if __name__ == "__main__":
    unittest.main()
