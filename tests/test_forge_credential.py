import os
import shutil
import stat
import subprocess
import tempfile
import unittest

from harness import DevContainerTestCase, project_env_text

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB = os.path.join(REPO_ROOT, "lib", "forge-credential.sh")

TOKEN = "ghp_SENTINEL_host_token"
APP_TOKEN = "ghs_SENTINEL_host_app"
OWNER_SECRET = "SENTINEL_owner_ambient_helper"
ASKPASS_SECRET = "SENTINEL_ambient_askpass"
GITHUB_URL = "https://github.com/example/demo.git"

# What the engine provides around the library: FORGE_AUTH from project.env and the
# lookups lib/agent-auth.sh owns. Defined after the library is sourced, as the engine does.
PREAMBLE_NONE = """
FORGE_AUTH=
SLOT_PROJECT_PREFIX=demo
"""
PREAMBLE_TOKEN = """
FORGE_AUTH=token
SLOT_PROJECT_PREFIX=demo
agent_auth_file() { echo /project/.devcontainer/.env; }
auth_value() { [ "$1" = GH_TOKEN ] && printf '%s\n' '@TOKEN@'; return 0; }
auth_names_missing() { [ -n "$(auth_value GH_TOKEN)" ] || printf GH_TOKEN; }
""".replace("@TOKEN@", TOKEN)
PREAMBLE_TOKEN_MISSING = PREAMBLE_TOKEN.replace(TOKEN, "")
PREAMBLE_APP = """
FORGE_AUTH=app
SLOT_PROJECT_PREFIX=demo
agent_auth_file() { echo /project/.devcontainer/.env; }
agent_installation_token() { AGENT_TOKEN='@TOKEN@'; }
""".replace("@TOKEN@", APP_TOKEN)
PREAMBLE_APP_FAILS = """
FORGE_AUTH=app
SLOT_PROJECT_PREFIX=demo
agent_auth_file() { echo /project/.devcontainer/.env; }
agent_installation_token() { echo "could not get a GitHub App installation token: nope" >&2; exit 1; }
"""

RECORDER = """#!/bin/sh
{ for a in "$@"; do printf '%s\n' "$a"; done; } >> "$FAKE_LOG.argv"
env >> "$FAKE_LOG.env"
exit 0
"""


def both(result):
    return result.stdout + result.stderr


class HostCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="forge-cred-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.home)
        os.makedirs(self.bin)
        self.log = os.path.join(self.tmp, "fake")
        self.repo = os.path.join(self.tmp, "github-checkout")
        for args in (["init", "-q", self.repo], ["-C", self.repo, "remote", "add", "origin", GITHUB_URL]):
            subprocess.run(["git", *args], check=True, env=self.env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def env(self, **extra):
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("GIT_", "GH_", "SSH_ASKPASS", "FORGE_"))}
        env.update(
            HOME=self.home, GIT_CONFIG_NOSYSTEM="1",
            GIT_CONFIG_GLOBAL=os.path.join(self.home, ".gitconfig"),
        )
        env.update(extra)
        return env

    def sh(self, preamble, body, **extra):
        return subprocess.run(
            ["bash", "-c", 'set -u; . "$1"; ' + preamble + body, "bash", LIB],
            env=self.env(**extra), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True, timeout=60,
        )

    def fake(self, name):
        path = os.path.join(self.bin, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(RECORDER)
        os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
        return dict(PATH=self.bin + os.pathsep + os.environ["PATH"], FAKE_LOG=self.log)

    def recorded(self, kind):
        path = f"{self.log}.{kind}"
        if not os.path.exists(path):
            return ""
        with open(path, encoding="utf-8") as handle:
            return handle.read()

    def fill(self, preamble, protocol="https", host="github.com", **extra):
        body = f'printf "protocol={protocol}\nhost={host}\n\n" | forge_git -C "{self.repo}" credential fill'
        return self.sh(preamble, body, **extra)


class Supply(HostCase):
    def test_token_mode_answers_for_https_github(self):
        r = self.fill(PREAMBLE_TOKEN)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("password=" + TOKEN, r.stdout)
        self.assertRegex(r.stdout, r"(?m)^username=.+")

    def test_app_mode_answers_with_the_installation_token(self):
        r = self.fill(PREAMBLE_APP)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("password=" + APP_TOKEN, r.stdout)

    def test_other_hosts_and_schemes_get_nothing(self):
        for protocol, host in [
            ("https", "example.com"), ("http", "github.com"),
            ("https", "github.com.example.test"), ("https", "notgithub.com"),
            ("https", "gist.github.com"), ("ssh", "github.com"),
        ]:
            with self.subTest(protocol=protocol, host=host):
                r = self.fill(PREAMBLE_TOKEN, protocol, host)
                self.assertNotIn(TOKEN, both(r))
                self.assertNotEqual(r.returncode, 0)

    def test_the_owners_ambient_credential_is_never_used(self):
        askpass = os.path.join(self.tmp, "askpass")
        with open(askpass, "w", encoding="utf-8") as handle:
            handle.write(f"#!/bin/sh\necho {ASKPASS_SECRET}\n")
        os.chmod(askpass, 0o755)
        with open(os.path.join(self.home, ".gitconfig"), "w", encoding="utf-8") as handle:
            handle.write('[credential]\n\thelper = "!f() { echo username=owner; echo password='
                         + OWNER_SECRET + '; }; f"\n')
        for preamble, expect in [(PREAMBLE_NONE, None), (PREAMBLE_TOKEN, TOKEN)]:
            with self.subTest(configured=expect is not None):
                r = self.fill(preamble, GIT_ASKPASS=askpass)
                self.assertNotIn(OWNER_SECRET, both(r))
                self.assertNotIn(ASKPASS_SECRET, both(r))
                if expect:
                    self.assertIn("password=" + expect, r.stdout)
                else:
                    self.assertNotEqual(r.returncode, 0)


class Isolation(HostCase):
    def test_a_prompt_cannot_happen(self):
        extra = self.fake("git")
        for preamble in (PREAMBLE_NONE, PREAMBLE_TOKEN):
            r = self.sh(preamble, "forge_git -C /nowhere fetch --quiet " + GITHUB_URL,
                        GIT_ASKPASS="/ambient/askpass", **extra)
            self.assertEqual(r.returncode, 0, r.stderr)
        env_dump = self.recorded("env")
        self.assertEqual(env_dump.count("GIT_TERMINAL_PROMPT=0"), 2, env_dump)
        self.assertNotIn("/ambient/askpass", env_dump)

    def test_the_token_is_in_the_environment_and_never_in_arguments(self):
        extra = self.fake("git")
        r = self.sh(PREAMBLE_TOKEN, "forge_git -C /nowhere push --quiet " + GITHUB_URL + " main", **extra)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(TOKEN, self.recorded("env"))
        self.assertNotIn(TOKEN, self.recorded("argv"))
        self.assertNotIn(TOKEN, both(r))

    def test_a_call_that_is_not_for_github_never_loads_the_credential(self):
        extra = self.fake("git")
        mark = os.path.join(self.tmp, "minted")
        preamble = PREAMBLE_APP.replace("AGENT_TOKEN=", "touch " + mark + "; AGENT_TOKEN=")
        r = self.sh(preamble, "forge_git -C /nowhere fetch /elsewhere/remote.git", **extra)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(os.path.exists(mark))
        self.assertNotIn(APP_TOKEN, self.recorded("env"))
        self.assertIn("GIT_TERMINAL_PROMPT=0", self.recorded("env"))

    def test_without_a_credential_no_secret_is_passed(self):
        extra = self.fake("git")
        r = self.sh(PREAMBLE_NONE, "forge_git -C /nowhere fetch " + GITHUB_URL, **extra)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("FORGE_HOST_SECRET", self.recorded("env"))

    def _hooked_repo(self):
        remote = os.path.join(self.tmp, "remote.git")
        work = os.path.join(self.tmp, "work")
        marker = os.path.join(self.tmp, "hook-ran")
        git = lambda *a: subprocess.run(["git", *a], check=True, env=self.env(),
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        git("init", "-q", "--bare", remote)
        git("init", "-q", work)
        hook = os.path.join(work, ".git", "hooks", "pre-push")
        with open(hook, "w", encoding="utf-8") as handle:
            handle.write(f"#!/bin/sh\ntouch {marker}\n")
        os.chmod(hook, 0o755)
        git("-C", work, "-c", "user.name=t", "-c", "user.email=t@example.com",
            "commit", "-q", "--allow-empty", "-m", "x")
        git("-C", work, "remote", "add", "origin", remote)
        git("-C", work, "config", f"url.{remote}.insteadOf", GITHUB_URL)
        return work, marker

    def test_a_slots_hooks_do_not_run_with_the_token_on_the_host(self):
        work, marker = self._hooked_repo()
        r = self.sh(PREAMBLE_TOKEN, f'forge_git -C "{work}" push --quiet {GITHUB_URL} HEAD:refs/heads/x')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(os.path.exists(marker))

    def test_purely_local_work_is_unchanged_without_a_credential(self):
        work, marker = self._hooked_repo()
        r = self.sh(PREAMBLE_NONE, f'forge_git -C "{work}" push --quiet origin HEAD:refs/heads/x')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.exists(marker))


class Messages(HostCase):
    def test_no_credential_is_one_line_with_the_fix(self):
        r = self.sh(PREAMBLE_NONE, "forge_host_credential_load; echo rc=$?; forge_credential_hint")
        self.assertIn("rc=1", r.stdout)
        hint = both(r).replace("rc=1", "").strip()
        self.assertEqual(len(hint.splitlines()), 1, hint)
        for word in ("FORGE_AUTH", "FORGE_AUTH_FILE", "project.env"):
            self.assertIn(word, hint)

    def test_a_credential_file_missing_its_value_names_the_name_not_a_value(self):
        r = self.sh(PREAMBLE_TOKEN_MISSING, "forge_host_credential_load; echo rc=$?")
        self.assertIn("rc=2", r.stdout)
        message = r.stderr.strip()
        self.assertEqual(len(message.splitlines()), 1, message)
        self.assertIn("GH_TOKEN", message)
        self.assertIn("/project/.devcontainer/.env", message)

    def test_an_app_that_cannot_mint_stops_with_the_reason_and_no_value(self):
        r = self.sh(PREAMBLE_APP_FAILS, "forge_host_credential_load; echo rc=$?")
        self.assertIn("rc=2", r.stdout)
        self.assertIn("installation token", r.stderr)

    def test_a_loaded_secret_is_not_exported_to_unrelated_commands(self):
        r = self.sh(PREAMBLE_TOKEN, "forge_host_credential_load; env")
        self.assertNotIn(TOKEN, r.stdout)


class HintAfterSubshell(HostCase):
    def test_the_hint_prints_when_the_failing_call_ran_in_a_command_substitution(self):
        extra = {}
        body = (
            f'out=$(forge_git -C "{self.repo}" rev-parse --verify --quiet nothing 2>&1); '
            f'forge_hint_for "{self.repo}"'
        )
        r = self.sh(PREAMBLE_NONE, body, **extra)
        hints = [l for l in both(r).splitlines() if "FORGE_AUTH" in l]
        self.assertEqual(len(hints), 1, both(r))

    def test_it_prints_once_and_only_for_a_github_origin(self):
        extra = {}
        r = self.sh(PREAMBLE_NONE, f'forge_hint_for "{self.repo}"; forge_hint_for "{self.repo}"', **extra)
        self.assertEqual(len([l for l in both(r).splitlines() if "FORGE_AUTH" in l]), 1, both(r))
        r = self.sh(PREAMBLE_NONE, 'forge_hint_for /nowhere')
        self.assertEqual(both(r), "")

    def test_a_project_with_a_credential_gets_no_hint(self):
        r = self.sh(PREAMBLE_TOKEN, f'forge_hint_for "{self.repo}"')
        self.assertEqual(both(r), "")


class Gh(HostCase):
    def test_the_token_goes_to_gh_in_the_environment_only(self):
        extra = self.fake("gh")
        r = self.sh(PREAMBLE_TOKEN, "forge_gh pr list --repo example/demo", **extra)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("GH_TOKEN=" + TOKEN, self.recorded("env"))
        self.assertIn("GH_PROMPT_DISABLED=1", self.recorded("env"))
        self.assertNotIn(TOKEN, self.recorded("argv"))

    def test_the_owners_ambient_token_is_replaced_when_a_credential_exists(self):
        extra = self.fake("gh")
        self.sh(PREAMBLE_TOKEN, "forge_gh api user", GH_TOKEN="owner-ambient", **extra)
        self.assertNotIn("owner-ambient", self.recorded("env"))

    def test_gh_does_not_run_when_the_configured_credential_is_unobtainable(self):
        extra = self.fake("gh")
        r = self.sh(PREAMBLE_APP_FAILS, "forge_gh api user", **extra)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.recorded("argv"), "")


NET_GIT = """#!/usr/bin/env bash
# Stands in front of the harness git: a fetch or push in a checkout whose origin is
# github.com is recorded (never its secret) and refused, as an unauthenticated one is.
real="$STUB_GIT"
dir="."; prev=""
for a in "$@"; do
  [ "$prev" = -C ] && dir="$a"
  prev="$a"
done
for a in "$@"; do
  case "$a" in
    fetch|push)
      url=$("$real" -C "$dir" remote get-url origin 2>/dev/null)
      case "$url" in
        https://github.com/*)
          {
            printf 'CALL %s\n' "$*"
            printf 'PROMPT=%s\n' "${GIT_TERMINAL_PROMPT-unset}"
            if [ -n "${FORGE_HOST_SECRET-}" ] && [ "$FORGE_HOST_SECRET" = "$EXPECT_SECRET" ]; then
              echo SECRET=present
            else
              echo SECRET=absent
            fi
          } >> "$NET_LOG"
          echo "fatal: could not read Username for 'https://github.com': terminal prompts disabled" >&2
          exit 128 ;;
      esac
      break ;;
  esac
done
exec "$real" "$@"
"""


class HostSideEviction(DevContainerTestCase):
    ENV = "GIT_USER_NAME=demo-agent\nGIT_USER_EMAIL=a@example.com\n"
    SECRET = "ghp_SENTINEL_evict_secret"

    def setUp(self):
        super().setUp()
        h = self.harness
        self.net_log = os.path.join(h.tmp, "net.log")
        wrap = os.path.join(h.tmp, "net-bin")
        os.makedirs(wrap)
        with open(os.path.join(wrap, "git"), "w", encoding="utf-8") as handle:
            handle.write(NET_GIT)
        os.chmod(os.path.join(wrap, "git"), 0o755)
        self.env_extra = dict(
            PATH=wrap + os.pathsep + h.stub_bin + os.pathsep + os.environ["PATH"],
            STUB_GIT=os.path.join(h.stub_bin, "git"),
            NET_LOG=self.net_log,
            EXPECT_SECRET=self.SECRET,
        )
        h._write_stub("gh", "#!/usr/bin/env bash\nexit 1\n")

    def git(self, cwd, *args):
        return subprocess.run(
            [self.harness.real_git_bin, "-C", cwd, "-c", "commit.gpgsign=false",
             "-c", "user.name=fixture", "-c", "user.email=fixture@example.com", *args],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )

    def project(self, credentialed):
        if credentialed:
            self.harness.build_fixture_repo(
                with_parent_env=f"GH_TOKEN={self.SECRET}\n",
                project_env=project_env_text(FORGE_AUTH="token", FORGE_AUTH_FILE=".devcontainer/.env"),
            )
        else:
            self.harness.build_fixture_repo()

    def slot_on_github(self, name, held_branch=None):
        h = self.harness
        remote = os.path.join(h.tmp, "seed-remote.git")
        subprocess.run(["git", "init", "--bare", "-q", remote], check=True)
        subprocess.run([h.real_git_bin, "-C", remote, "symbolic-ref", "HEAD", "refs/heads/master"], check=True)
        h.make_bare_slot(name, env_content=self.ENV, local_identity=("demo-agent", "a@example.com"))
        slot = h.slot_dir(name)
        self.git(slot, "remote", "set-url", "origin", remote)
        with open(os.path.join(slot, ".gitignore"), "w", encoding="utf-8") as handle:
            handle.write(".devcontainer/.env\n.devcontainer/.slot-holder\ndocs/\n")
        self.git(slot, "add", ".gitignore", ".devcontainer/docker-compose.yml")
        self.git(slot, "commit", "-q", "-m", "base")
        self.git(slot, "branch", "-M", "master")
        self.git(slot, "push", "-q", "origin", "master")
        self.git(slot, "fetch", "-q", "origin")
        if held_branch:
            self.git(slot, "checkout", "-q", "-b", held_branch)
            with open(os.path.join(slot, "held.txt"), "w", encoding="utf-8") as handle:
                handle.write("held\n")
            self.git(slot, "add", "held.txt")
            self.git(slot, "commit", "-q", "-m", "held work")
        self.git(slot, "remote", "set-url", "origin", GITHUB_URL)
        self.git(h.fixture_repo, "remote", "set-url", "origin", GITHUB_URL)
        return slot

    def net(self):
        if not os.path.exists(self.net_log):
            return ""
        with open(self.net_log, encoding="utf-8") as handle:
            return handle.read()

    def evict(self, name):
        return self.harness.run("evict", name, "--yes", port_overrides=self.env_extra)

    def test_evicting_a_clean_slot_never_prompts_and_a_failed_fetch_does_not_stop_it(self):
        self.project(credentialed=False)
        slot = self.slot_on_github("clean")
        result = self.evict("clean")
        out = result.stdout.decode() + result.stderr.decode()
        self.assertEqual(result.returncode, 0, out)
        calls = self.net().count("CALL ")
        self.assertGreater(calls, 0, "no host-side fetch was attempted")
        self.assertEqual(self.net().count("PROMPT=0"), calls, self.net())
        self.assertIn("could not fetch", out)
        self.assertEqual(len([l for l in out.splitlines() if "FORGE_AUTH" in l]), 1, out)
        self.assertTrue(os.path.isfile(os.path.join(slot, ".gitignore")))

    def test_with_a_credential_the_fetch_carries_it_in_the_environment_only(self):
        self.project(credentialed=True)
        self.slot_on_github("clean")
        result = self.evict("clean")
        out = result.stdout.decode() + result.stderr.decode()
        self.assertEqual(result.returncode, 0, out)
        self.assertIn("SECRET=present", self.net())
        self.assertNotIn("SECRET=absent", self.net())
        self.assertNotIn(self.SECRET, self.net())
        self.assertNotIn(self.SECRET, out)

    def test_the_rescue_push_uses_it_too_and_leaks_nothing(self):
        self.project(credentialed=True)
        self.slot_on_github("held", held_branch="work-in-progress")
        result = self.evict("held")
        out = result.stdout.decode() + result.stderr.decode()
        pushes = [l for l in self.net().splitlines() if l.startswith("CALL ") and " push " in l]
        self.assertTrue(pushes, self.net() + out)
        self.assertNotIn("SECRET=absent", self.net())
        self.assertNotIn(self.SECRET, out)
        for root, _dirs, files in os.walk(self.harness.state_dir):
            for f in files:
                with open(os.path.join(root, f), "rb") as handle:
                    self.assertNotIn(self.SECRET.encode(), handle.read(), os.path.join(root, f))


if __name__ == "__main__":
    unittest.main()
