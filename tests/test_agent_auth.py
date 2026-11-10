import base64
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from harness import (
    DOCKER_STUB,
    DEFAULT_PORT_NAMES,
    DevContainerHarness,
    DevContainerTestCase,
    PROJECT_PREFIX,
    REPO_ROOT,
    SCRIPT_REL,
    project_env_text,
    slot_port,
)

TOK = "ghs_SENTINEL_9f3a_new"
OLD = "ghs_SENTINEL_9f3a_old"
OTHER_TOK = "ghs_SENTINEL_9f3a_other"
OWNER = "ghp_SENTINEL_9f3a_owner"
MARK = "SENTINEL_9f3a"
JWT = re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")
APP_ID = "1234567"
INSTALLATION = "7654321"
SLUG = "demo-agent"
BOT_ID = "424242"
REPOS = ["demo", "demo.docs"]
BOT_NAME = SLUG + "[bot]"
BOT_EMAIL = f"{BOT_ID}+{SLUG}[bot]@users.noreply.github.com"
APP_NAMES = [
    "FORGE_APP_ID",
    "FORGE_APP_INSTALLATION_ID",
    "FORGE_APP_SLUG",
    "FORGE_APP_BOT_ID",
    "FORGE_APP_KEY_FILE",
    "FORGE_APP_REPOS",
]
ENV_FILE_REL = ".devcontainer/.env"
LIVE_NAMES = ["SEARCH_API_KEY", "LOOKUP_API_KEY", "VERIFY_API_KEY", "MODEL_API_KEY"]
MADE_UP = "ZZ_MADE_UP_KEY_7"
LIVE_VALUES = {
    "SEARCH_API_KEY": "sentinel-search-4c1e",
    "LOOKUP_API_KEY": "sentinel-lookup-77ab",
    "VERIFY_API_KEY": "sentinel-verify-91d0",
    "MODEL_API_KEY": "sentinel-model-3e8f",
    MADE_UP: "sentinel-madeup-5a52",
}
ALL_LIVE = list(LIVE_VALUES)

KEY_FILE = None
_KEY_DIR = None
_KEY_SOURCE_DIR = None
_KEY_SOURCE = None


def setUpModule():
    global _KEY_SOURCE_DIR, _KEY_SOURCE
    _KEY_SOURCE_DIR = tempfile.mkdtemp(prefix="agent-auth-key-")
    _KEY_SOURCE = os.path.join(_KEY_SOURCE_DIR, "app.pem")
    subprocess.run(
        ["openssl", "genrsa", "-out", _KEY_SOURCE, "2048"],
        check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )


def tearDownModule():
    shutil.rmtree(_KEY_SOURCE_DIR, ignore_errors=True)


def place_key(harness):
    # The engine container sees the projects root, not the rest of the host, so the
    # key lives under it, as it does for a real project.
    global KEY_FILE, _KEY_DIR
    _KEY_DIR = os.path.join(harness.tmp, "keys")
    os.makedirs(_KEY_DIR)
    KEY_FILE = os.path.join(_KEY_DIR, "app.pem")
    shutil.copyfile(_KEY_SOURCE, KEY_FILE)
    os.chmod(KEY_FILE, 0o600)


DOCKER_WRAPPER = r"""#!/usr/bin/env bash
set -u
fake="${AUTH_FAKE:?}"
base="$(dirname "$0")/docker-base"
args=("$@")
verb="" envfile="" pdir="" svc="" passed=""
names=() cmd=()
if [[ "${1:-}" != compose ]]; then
  verb="${1:-}"
else
  i=1
  while (( i < ${#args[@]} )); do
    case "${args[i]}" in
      up|down|stop|port|ps|exec|run|ls|config) verb="${args[i]}"; break ;;
      --env-file) envfile="${args[i+1]:-}"; i=$((i+1)) ;;
      --project-directory) pdir="${args[i+1]:-}"; i=$((i+1)) ;;
      -f|-p) i=$((i+1)) ;;
    esac
    i=$((i+1))
  done
  if [[ "$verb" == exec || "$verb" == run ]]; then
    j=$((i+1))
    while (( j < ${#args[@]} )); do
      a="${args[j]}"
      case "$a" in
        -e) names+=("${args[j+1]}"); j=$((j+2)) ;;
        -u|--user|-w|--workdir|-v|--volume|--entrypoint) j=$((j+2)) ;;
        -*) j=$((j+1)) ;;
        *) svc="$a"; cmd=("${args[@]:j+1}"); break ;;
      esac
    done
  fi
fi
envargs=()
for n in ${names[@]+"${names[@]}"}; do
  case "$n" in
    *=*) k="${n%%=*}"; v="${n#*=}"; st=ARG ;;
    *) k="$n"; if [ -n "${!k+x}" ]; then v="${!k}"; st=SET; else v=""; st=UNSET; fi ;;
  esac
  passed+="$k:$st:$(printf %s "$v" | base64 -w0);"
  [ "$st" = UNSET ] || envargs+=("$k=$v")
done
printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$verb" "$*" "${GH_TOKEN-UNSET}" "${COMPOSE_PROFILES-}" "$passed" "$svc" "$pdir" >> "$fake/docker-calls.tsv"
if [[ "$verb" == up && -f "$envfile" ]]; then
  { echo UP; cat "$envfile"; } >> "$fake/up-env.log"
fi
if [[ "$verb" == run && "${cmd[*]-}" != *dev-container-clone* ]]; then
  if [[ -n "${WORKSPACE_DIR:-}" ]]; then
    if [[ -d "$WORKSPACE_DIR" ]]; then
      cd "$WORKSPACE_DIR"
    elif [[ -n "${SLOT_WORKSPACE_VOLUME:-}" && -n "${DOCKER_STUB_STATE:-}" && -d "$DOCKER_STUB_STATE/volumes/$SLOT_WORKSPACE_VOLUME" ]]; then
      cd "$DOCKER_STUB_STATE/volumes/$SLOT_WORKSPACE_VOLUME"
    fi
  fi
  exec env -i PATH="$PATH" HOME="${HOME:-/}" REAL_GIT_BIN="${REAL_GIT_BIN:-}" ${envargs[@]+"${envargs[@]}"} "${cmd[@]}"
fi
exec "$base" "$@"
"""

CURL_STUB = r"""#!/usr/bin/env bash
set -uo pipefail
fake="${AUTH_FAKE:?}"
printf '%s\n' "$*" >> "$fake/curl-argv.log"
cat > "$(mktemp "$fake/curl-stdin.XXXXXX")"
if [ -f "$fake/curl-hold" ]; then
  echo "$$" >> "$fake/curl-inside"
  while [ -f "$fake/curl-hold" ]; do "$AUTH_REAL_SLEEP" 0.02; done
fi
[ -f "$fake/curl-delay" ] && "$AUTH_REAL_SLEEP" "$(cat "$fake/curl-delay")"
mode=$(cat "$fake/curl-mode" 2>/dev/null || echo ok)
tok=$(cat "$fake/curl-token" 2>/dev/null || true)
exp=$(cat "$fake/curl-expires" 2>/dev/null || true)
case "$mode" in
  ok) printf '{"token":"%s","expires_at":"%s"}\n' "$tok" "$exp" ;;
  http) echo 'curl: (22) The requested URL returned error: 401' >&2; exit 22 ;;
  notoken) printf '{"message":"Bad credentials"}\n' ;;
  emptytoken) printf '{"token":"","expires_at":"%s"}\n' "$exp" ;;
  noexpiry) printf '{"token":"%s"}\n' "$tok" ;;
  badexpiry) printf '{"token":"%s","expires_at":"not a date"}\n' "$tok" ;;
  garbage) echo 'not json at all' ;;
  empty) : ;;
esac
exit 0
"""


def app_lines(**over):
    values = {
        "FORGE_APP_ID": APP_ID,
        "FORGE_APP_INSTALLATION_ID": INSTALLATION,
        "FORGE_APP_SLUG": SLUG,
        "FORGE_APP_BOT_ID": BOT_ID,
        "FORGE_APP_KEY_FILE": KEY_FILE,
        "FORGE_APP_REPOS": " ".join(REPOS),
    }
    values.update(over)
    return [f"{k}={v}" for k, v in values.items() if v is not None]


def text(res):
    return res.stdout.decode() + res.stderr.decode()


def read_if(path):
    if not os.path.isfile(path):
        return ""
    with open(path, encoding="utf-8", errors="replace") as handle:
        return handle.read()


def write(path, content, mode=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(content)
    if mode is not None:
        os.chmod(path, mode)


def env_value(path, key):
    hits = [l for l in read_if(path).split("\n") if l.startswith(key + "=")]
    return hits[-1][len(key) + 1:] if hits else None


def count_key(path, key):
    return len([l for l in read_if(path).split("\n") if l.startswith(key + "=")])


class AuthTestCase(DevContainerTestCase):

    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("GH_TOKEN", None)
        self.fake = os.path.join(self.harness.tmp, "auth-fake")
        os.makedirs(self.fake)
        os.environ["AUTH_FAKE"] = self.fake
        os.environ["AUTH_REAL_SLEEP"] = shutil.which("sleep")
        place_key(self.harness)
        self.install_stubs(self.harness)
        self.set_mint(TOK, 3600)

    def install_stubs(self, harness):
        harness._write_stub("docker-base", DOCKER_STUB)
        harness._write_stub("docker", DOCKER_WRAPPER)
        harness._write_stub("curl", CURL_STUB)

    def use_loser_succeeds_mkdir(self):
        real = shutil.which("mkdir")
        self.harness._write_stub(
            "mkdir",
            "#!/usr/bin/env bash\n"
            f'case "${{1-}}" in -*) exec "{real}" "$@" ;; esac\n'
            f'[ "$#" -eq 1 ] || exec "{real}" "$@"\n'
            f'"{real}" "$1" 2>/dev/null\n'
            '[ -d "$1" ]\n',
        )

    def use_quick_sleep(self):
        self.harness._write_stub(
            "sleep", f'#!/usr/bin/env bash\nexec "{shutil.which("sleep")}" 0.01\n'
        )

    def default_env(self, mode):
        if mode == "app":
            return app_lines()
        identity = ["GIT_USER_NAME=demo-agent", "GIT_USER_EMAIL=demo-agent@example.com"]
        if mode == "token":
            return ["GH_TOKEN=" + TOK] + identity
        return ["GH_TOKEN=" + OWNER] + identity

    def project(self, mode="app", env_lines=None, harness=None, **keys):
        harness = harness or self.harness
        values = {}
        if mode is not None:
            values["FORGE_AUTH"] = mode
            values["FORGE_AUTH_FILE"] = ENV_FILE_REL
        values.update(keys)
        lines = self.default_env(mode) if env_lines is None else env_lines
        harness.build_fixture_repo(
            with_parent_env="".join(l + "\n" for l in lines),
            project_env=project_env_text(**values),
        )

    def owner_env(self, lines, rel=ENV_FILE_REL, harness=None):
        harness = harness or self.harness
        write(os.path.join(harness.fixture_repo, rel), "".join(l + "\n" for l in lines), mode=0o600)

    def acquire(self):
        result = self.harness.run_acquire()
        self.assertEqual(result.returncode, 0, text(result))
        self.name = self.harness.acquired_slot_name(result)
        self.sid = f"{PROJECT_PREFIX}-{self.name}"
        self.clear_calls()
        return self.name

    def clear_calls(self):
        for name in os.listdir(self.fake):
            if name.startswith(("docker-calls", "up-env", "curl-argv", "curl-stdin")):
                os.remove(os.path.join(self.fake, name))
        if os.path.isfile(self.harness.docker_log):
            os.remove(self.harness.docker_log)

    def run_slot(self, cmd=("true",), live=(), name=None, extra=None, harness=None):
        harness = harness or self.harness
        flags = [a for n in live for a in ("--live", n)]
        return harness.run(
            "run", name or self.name, *flags, "--", *cmd, port_overrides=extra
        )

    def calls(self):
        out = []
        for line in read_if(os.path.join(self.fake, "docker-calls.tsv")).split("\n"):
            if not line:
                continue
            verb, argv, gh, profiles, passed, svc, pdir = line.split("\t")
            names = {}
            for item in filter(None, passed.split(";")):
                key, state, value = item.split(":", 2)
                names[key] = (state, base64.b64decode(value).decode())
            out.append(
                {"verb": verb, "argv": argv, "gh": gh, "profiles": profiles,
                 "passed": names, "svc": svc, "pdir": pdir}
            )
        return out

    def execs(self):
        return [c for c in self.calls() if c["verb"] == "exec"]

    def live_runs(self):
        return [c for c in self.calls() if c["verb"] == "run"]

    def exec_tokens(self):
        return [c["gh"] for c in self.execs()]

    def docker_argv(self):
        return "\n".join(c["argv"] for c in self.calls())

    def curl_argv(self):
        return [l for l in read_if(os.path.join(self.fake, "curl-argv.log")).split("\n") if l]

    def curl_stdins(self):
        return [
            read_if(os.path.join(self.fake, n))
            for n in sorted(os.listdir(self.fake)) if n.startswith("curl-stdin.")
        ]

    def up_envs(self):
        snaps = []
        for line in read_if(os.path.join(self.fake, "up-env.log")).split("\n"):
            if line == "UP":
                snaps.append([])
            elif snaps and line:
                snaps[-1].append(line)
        return snaps

    def set_mint(self, token, expires_in, mode="ok"):
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + expires_in))
        write(os.path.join(self.fake, "curl-token"), token)
        write(os.path.join(self.fake, "curl-expires"), stamp)
        write(os.path.join(self.fake, "curl-mode"), mode)

    def token_dir(self):
        return os.path.join(self.harness.state_dir, "tokens")

    def cache_file(self, project=PROJECT_PREFIX):
        return os.path.join(self.token_dir(), project)

    def lock_path(self, project=PROJECT_PREFIX):
        return os.path.join(self.token_dir(), project + ".lock")

    def token_entries(self):
        directory = self.token_dir()
        return sorted(os.listdir(directory)) if os.path.isdir(directory) else []

    def write_cache(self, expires_in, token, project=PROJECT_PREFIX):
        write(self.cache_file(project), f"{int(time.time()) + expires_in}\n{token}\n", 0o600)

    def read_cache(self, project=PROJECT_PREFIX):
        lines = read_if(self.cache_file(project)).split("\n")
        self.assertGreaterEqual(len(lines), 2, "no expiry and token lines in the cache")
        return int(lines[0]), lines[1]

    def slot_env(self, name=None):
        return self.harness.env_path(name or self.name)

    def expect_no_leak(self, res, *tokens, allow=()):
        streams = {
            "stdout": res.stdout.decode(),
            "stderr": res.stderr.decode(),
            "docker argv": self.docker_argv(),
            "curl argv": "\n".join(self.curl_argv()),
        }
        for what, body in streams.items():
            self.assertNotIn(MARK, body, what)
            self.assertIsNone(JWT.search(body), what)
            for token in tokens:
                self.assertNotIn(token, body, what)
        for root, _, files in os.walk(self.harness.state_dir):
            for name in files:
                path = os.path.join(root, name)
                if path == self.cache_file() or path in allow:
                    continue
                self.assertNotIn(MARK, read_if(path), path)


class GithubAppRun(AuthTestCase):
    MODE = "app"

    def setUp(self):
        super().setUp()
        self.project(self.MODE)
        self.acquire()

    def start_run(self, tag, name=None):
        log = os.path.join(self.harness.tmp, f"run-{tag}.log")
        proc = self.harness.spawn("run", name or self.name, "--", "true", log=log)
        return proc, log

    def finish(self, started):
        proc, log = started
        return proc.wait(timeout=120), read_if(log)

    def hold_mint(self):
        write(os.path.join(self.fake, "curl-hold"), "")
        started = self.start_run("holder")
        inside = os.path.join(self.fake, "curl-inside")
        deadline = time.monotonic() + 30
        while not os.path.exists(inside):
            self.assertLess(time.monotonic(), deadline, "the first run never reached curl")
            time.sleep(0.02)
        return started

    def release_mint(self):
        os.remove(os.path.join(self.fake, "curl-hold"))


class TheInstallationTokenCache(GithubAppRun):

    def test_reuses_a_cached_token_with_more_than_30_minutes_left_and_never_calls_curl(self):
        self.write_cache(45 * 60, OLD)

        res = self.run_slot()

        self.assertEqual(res.returncode, 0, text(res))
        self.assertEqual(self.curl_argv(), [])
        self.assertEqual(self.exec_tokens(), [OLD])
        self.assertEqual(self.read_cache()[1], OLD)

    def test_mints_exactly_once_with_no_cache_and_caches_the_token_0600_under_umask_022(self):
        self.addCleanup(os.umask, os.umask(0o022))

        res = self.run_slot()

        self.assertEqual(res.returncode, 0, text(res))
        self.assertEqual(len(self.curl_argv()), 1)
        self.assertEqual(self.exec_tokens(), [TOK])
        self.assertEqual(stat.S_IMODE(os.stat(self.cache_file()).st_mode), 0o600)
        expires, token = self.read_cache()
        self.assertEqual(token, TOK)
        self.assertGreater(expires, time.time() + 3500)
        self.assertLessEqual(expires, time.time() + 3600)
        self.assertEqual(self.token_entries(), ["demo"])

    def test_mints_once_more_and_replaces_the_cache_when_under_30_minutes_are_left(self):
        self.write_cache(20 * 60, OLD)

        res = self.run_slot()

        self.assertEqual(res.returncode, 0, text(res))
        self.assertEqual(len(self.curl_argv()), 1)
        self.assertEqual(self.exec_tokens(), [TOK])
        self.assertEqual(self.read_cache()[1], TOK)
        self.assertNotIn(OLD, read_if(self.cache_file()))
        self.assertEqual(stat.S_IMODE(os.stat(self.cache_file()).st_mode), 0o600)

    def test_mints_when_the_cached_token_has_already_expired(self):
        self.write_cache(-10, OLD)

        res = self.run_slot()

        self.assertEqual(res.returncode, 0, text(res))
        self.assertEqual(self.exec_tokens(), [TOK])

    def test_the_line_is_at_30_minutes_29_mints_and_31_does_not(self):
        self.write_cache(29 * 60, OLD)
        self.assertEqual(self.run_slot().returncode, 0)
        self.assertEqual(self.exec_tokens(), [TOK])

        self.write_cache(31 * 60, OLD)
        self.assertEqual(self.run_slot().returncode, 0)

        self.assertEqual(self.exec_tokens()[-1:], [OLD])
        self.assertEqual(len(self.curl_argv()), 1)

    def test_the_second_run_is_served_from_the_cache_the_first_one_wrote(self):
        self.assertEqual(self.run_slot().returncode, 0)
        self.assertEqual(self.run_slot().returncode, 0)

        self.assertEqual(len(self.curl_argv()), 1)
        self.assertEqual(self.exec_tokens(), [TOK, TOK])

    def test_a_damaged_cache_is_a_missing_cache(self):
        now = int(time.time())
        cases = {
            "empty": "",
            "garbage": "not a cache\n",
            "an expiry that is not a number": f"soon\n{OLD}\n",
            "an expiry with no token": f"{now + 7200}\n",
            "an empty token": f"{now + 7200}\n\n",
        }
        for why, body in cases.items():
            with self.subTest(why):
                self.clear_calls()
                write(self.cache_file(), body, 0o600)

                res = self.run_slot()

                self.assertEqual(res.returncode, 0, text(res))
                self.assertEqual(len(self.curl_argv()), 1)
                self.assertEqual(self.exec_tokens(), [TOK])
                self.assertEqual(self.read_cache()[1], TOK)

    def test_two_runs_at_once_mint_once_and_the_second_uses_the_first_ones_token(self):
        write(os.path.join(self.fake, "curl-delay"), "1")
        first = self.start_run("a")
        second = self.start_run("b")

        (code_a, log_a), (code_b, log_b) = self.finish(first), self.finish(second)

        self.assertEqual(code_a, 0, log_a)
        self.assertEqual(code_b, 0, log_b)
        self.assertEqual(len(self.curl_argv()), 1)
        self.assertEqual(self.exec_tokens(), [TOK, TOK])
        self.assertEqual(self.token_entries(), ["demo"])


def parse_request(body):
    out = {"headers": []}
    for line in body.split("\n"):
        match = re.match(r'^([a-z-]+)\s*=\s*(".*")\s*$', line)
        if not match:
            continue
        value = json.loads(match.group(2))
        if match.group(1) == "url":
            out["url"] = value
        elif match.group(1) == "header":
            out["headers"].append(value)
        elif match.group(1) == "data":
            out["data"] = value
    return out


def verify_jwt(jwt, scratch):
    def decode(part):
        return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))

    head, payload, signature = jwt.split(".")
    public = os.path.join(scratch, "app-public.pem")
    subprocess.run(
        ["openssl", "rsa", "-in", KEY_FILE, "-pubout", "-out", public],
        check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    signed = os.path.join(scratch, "jwt-signed")
    sig = os.path.join(scratch, "jwt-signature")
    with open(signed, "wb") as handle:
        handle.write(f"{head}.{payload}".encode())
    with open(sig, "wb") as handle:
        handle.write(decode(signature))
    ok = subprocess.run(
        ["openssl", "dgst", "-sha256", "-verify", public, "-signature", sig, signed],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ).returncode == 0
    return ok, json.loads(decode(head)), json.loads(decode(payload))


class WhatIsSentAndNeverShown(GithubAppRun):

    def minted(self):
        res = self.run_slot()
        self.assertEqual(res.returncode, 0, text(res))
        stdins = self.curl_stdins()
        self.assertEqual(len(stdins), 1)
        return res, parse_request(stdins[0])

    def test_posts_to_the_installation_endpoint_with_the_github_headers_and_a_bearer_jwt(self):
        _, req = self.minted()

        self.assertEqual(
            req["url"], f"https://api.github.com/app/installations/{INSTALLATION}/access_tokens"
        )
        self.assertIn("Accept: application/vnd.github+json", req["headers"])
        self.assertIn("X-GitHub-Api-Version: 2022-11-28", req["headers"])
        bearer = [h for h in req["headers"] if h.startswith("Authorization: Bearer ")]
        self.assertEqual(len(bearer), 1)

    def test_signs_a_real_rs256_jwt_with_the_key_file(self):
        _, req = self.minted()
        auth = next(h for h in req["headers"] if h.startswith("Authorization: Bearer "))
        jwt = auth[len("Authorization: Bearer "):]
        self.assertRegex(jwt, "^" + JWT.pattern + "$")

        ok, header, claims = verify_jwt(jwt, self.harness.tmp)

        self.assertTrue(ok, "the signature does not verify against the key")
        self.assertEqual(header, {"alg": "RS256", "typ": "JWT"})
        self.assertEqual(str(claims["iss"]), APP_ID)
        self.assertLessEqual(claims["iat"], time.time() - 55)
        self.assertGreaterEqual(claims["iat"], time.time() - 120)
        self.assertEqual(claims["exp"] - claims["iat"], 600)
        self.assertEqual(sorted(claims), ["exp", "iat", "iss"])

    def test_asks_for_exactly_the_configured_repositories_and_the_agreed_permissions(self):
        _, req = self.minted()

        body = json.loads(req["data"])

        self.assertEqual(body["repositories"], REPOS)
        self.assertEqual(
            body["permissions"],
            {
                "contents": "write",
                "pull_requests": "write",
                "issues": "write",
                "actions": "read",
                "checks": "read",
                "statuses": "read",
                "metadata": "read",
            },
        )
        self.assertEqual(sorted(body), ["permissions", "repositories"])
        self.assertNotRegex(req["data"], "(?i)workflows")

    def test_takes_the_repositories_from_the_env_file_split_on_spaces_last_line_wins(self):
        self.owner_env(
            app_lines(FORGE_APP_REPOS="ignored") + ["FORGE_APP_REPOS=  alpha    beta.docs  gamma "]
        )

        _, req = self.minted()

        self.assertEqual(json.loads(req["data"])["repositories"], ["alpha", "beta.docs", "gamma"])

    def test_keeps_the_jwt_and_the_token_off_every_argv_and_out_of_every_stream_and_state_file(self):
        res, _ = self.minted()

        self.expect_no_leak(res)
        self.assertEqual(self.exec_tokens(), [TOK])
        self.assertNotIn("Bearer", "\n".join(self.curl_argv()))
        self.assertNotIn("Authorization", "\n".join(self.curl_argv()))

    def test_keeps_them_out_of_the_output_of_a_cached_run_too(self):
        self.write_cache(3000, OLD)

        res = self.run_slot()

        self.assertEqual(res.returncode, 0, text(res))
        self.expect_no_leak(res, OLD)

    def test_prints_nothing_of_its_own_on_success(self):
        res = self.run_slot()

        self.assertEqual(res.returncode, 0, text(res))
        self.assertEqual(res.stdout.decode(), "")
        self.assertEqual(res.stderr.decode(), "")


class AFailedMintChangesNothing(GithubAppRun):
    MODES = ["http", "notoken", "emptytoken", "noexpiry", "badexpiry", "garbage", "empty"]

    def test_each_failure_is_nonzero_keeps_the_old_cache_and_execs_nothing(self):
        for mode in self.MODES:
            with self.subTest(mode):
                self.clear_calls()
                self.write_cache(10 * 60, OLD)
                before = read_if(self.cache_file())
                self.set_mint(TOK, 3600, mode)

                res = self.run_slot()

                self.assertNotEqual(res.returncode, 0)
                self.assertEqual(read_if(self.cache_file()), before)
                self.assertEqual(stat.S_IMODE(os.stat(self.cache_file()).st_mode), 0o600)
                self.assertEqual(self.token_entries(), ["demo"])
                self.assertEqual(self.execs(), [])
                self.assertRegex(res.stderr.decode(), "(?i)installation token|App")
                self.expect_no_leak(res, OLD)

    def test_each_failure_with_no_cache_before_leaves_no_cache_file(self):
        for mode in self.MODES:
            with self.subTest(mode):
                self.clear_calls()
                self.set_mint(TOK, 3600, mode)

                res = self.run_slot()

                self.assertNotEqual(res.returncode, 0)
                self.assertFalse(os.path.exists(self.cache_file()))
                self.assertEqual(self.token_entries(), [])
                self.assertEqual(self.execs(), [])

    def test_a_later_run_after_a_failure_still_works_the_lock_was_released(self):
        self.set_mint(TOK, 3600, "http")
        self.assertNotEqual(self.run_slot().returncode, 0)
        self.set_mint(TOK, 3600, "ok")

        res = self.run_slot()

        self.assertEqual(res.returncode, 0, text(res))
        self.assertEqual(self.exec_tokens(), [TOK])

    def test_a_key_file_that_is_not_a_key_fails_the_mint_without_calling_github(self):
        bad = os.path.join(self.harness.tmp, "bad.pem")
        write(bad, "this is not a private key\n")
        self.owner_env(app_lines(FORGE_APP_KEY_FILE=bad))
        self.write_cache(10 * 60, OLD)
        before = read_if(self.cache_file())

        res = self.run_slot()

        self.assertNotEqual(res.returncode, 0)
        self.assertEqual(self.curl_argv(), [])
        self.assertEqual(read_if(self.cache_file()), before)
        self.assertEqual(self.execs(), [])
        self.assertNotIn("this is not a private key", text(res))


class MissingAppConfigIsRefusedOnRun(GithubAppRun):
    VALUES = [APP_ID, INSTALLATION, BOT_ID, " ".join(REPOS)]

    def expect_refused(self, res, named):
        err = res.stderr.decode()
        self.assertNotEqual(res.returncode, 0)
        for name in named:
            self.assertIn(name, err)
        for name in APP_NAMES:
            if name not in named:
                self.assertNotIn(name, err)
        self.assertIn(ENV_FILE_REL, err)
        for value in [*self.VALUES, KEY_FILE, _KEY_DIR]:
            self.assertNotIn(value, text(res))
        self.assertEqual(self.curl_argv(), [])
        self.assertEqual(self.execs(), [])
        self.assertFalse(os.path.exists(self.cache_file()))

    def test_each_missing_name_is_named_alone(self):
        for name in APP_NAMES:
            with self.subTest(name):
                self.clear_calls()
                self.owner_env(app_lines(**{name: None}))
                self.expect_refused(self.run_slot(), [name])

    def test_several_missing_are_all_named_in_one_refusal(self):
        names = ["FORGE_APP_ID", "FORGE_APP_KEY_FILE", "FORGE_APP_INSTALLATION_ID"]
        self.owner_env(app_lines(**{n: None for n in names}))

        self.expect_refused(self.run_slot(), names)

    def test_an_empty_value_or_a_commented_out_line_counts_as_missing(self):
        self.owner_env(
            app_lines(FORGE_APP_ID=None, FORGE_APP_REPOS=None)
            + ["FORGE_APP_ID=", "# FORGE_APP_REPOS=demo"]
        )

        self.expect_refused(self.run_slot(), ["FORGE_APP_ID", "FORGE_APP_REPOS"])

    def test_a_later_empty_line_overrides_an_earlier_value(self):
        self.owner_env(app_lines() + ["FORGE_APP_INSTALLATION_ID="])

        self.expect_refused(self.run_slot(), ["FORGE_APP_INSTALLATION_ID"])

    def test_no_env_file_at_all_names_all_six(self):
        os.remove(os.path.join(self.harness.fixture_repo, ENV_FILE_REL))

        self.expect_refused(self.run_slot(), APP_NAMES)

    def test_a_key_file_that_does_not_exist_names_the_key_name_not_its_path(self):
        self.owner_env(app_lines(FORGE_APP_KEY_FILE=os.path.join(_KEY_DIR, "missing.pem")))

        res = self.run_slot()

        self.assertNotEqual(res.returncode, 0)
        self.assertIn("FORGE_APP_KEY_FILE", res.stderr.decode())
        self.assertNotIn(_KEY_DIR, text(res))
        self.assertEqual(self.curl_argv(), [])
        self.assertEqual(self.execs(), [])

    @unittest.skipIf(os.geteuid() == 0, "root reads any file")
    def test_a_key_file_that_cannot_be_read_names_the_key_name_and_mints_nothing(self):
        locked = os.path.join(self.harness.tmp, "locked.pem")
        shutil.copyfile(KEY_FILE, locked)
        os.chmod(locked, 0o000)
        self.owner_env(app_lines(FORGE_APP_KEY_FILE=locked))

        res = self.run_slot()

        self.assertNotEqual(res.returncode, 0)
        self.assertIn("FORGE_APP_KEY_FILE", res.stderr.decode())
        self.assertEqual(self.curl_argv(), [])
        self.assertEqual(self.execs(), [])

    def test_a_directory_as_the_key_file_is_not_a_key_file(self):
        self.owner_env(app_lines(FORGE_APP_KEY_FILE=_KEY_DIR))

        res = self.run_slot()

        self.assertNotEqual(res.returncode, 0)
        self.assertIn("FORGE_APP_KEY_FILE", res.stderr.decode())
        self.assertEqual(self.execs(), [])


class TheTokenTravelsInTheEnvironmentOfTheOneExec(GithubAppRun):

    def test_execs_with_the_token_name_only_in_argv_and_the_token_in_that_calls_environment(self):
        res = self.run_slot()

        self.assertEqual(res.returncode, 0, text(res))
        execs = self.execs()
        self.assertEqual(len(execs), 1)
        call = execs[0]
        self.assertEqual(call["gh"], TOK)
        self.assertRegex(call["argv"], r"\bexec\b.*-e GH_TOKEN ")
        self.assertNotIn("GH_TOKEN=", call["argv"])
        self.assertNotIn(TOK, call["argv"])
        self.assertRegex(call["argv"], "-u node")
        self.assertTrue(call["argv"].endswith(" app true"), call["argv"])
        self.assertEqual(call["passed"], {"GH_TOKEN": ("SET", TOK)})

    def test_no_other_docker_call_sees_a_token_it_is_scoped_to_that_command(self):
        res = self.run_slot()

        self.assertEqual(res.returncode, 0, text(res))
        others = [c for c in self.calls() if c["verb"] != "exec"]
        self.assertTrue(others)
        for call in others:
            self.assertEqual(call["gh"], "UNSET", call["argv"])

    def test_replaces_a_token_of_the_callers_environment_with_the_installation_token(self):
        res = self.run_slot(extra={"GH_TOKEN": OWNER})

        self.assertEqual(res.returncode, 0, text(res))
        self.assertEqual(self.exec_tokens(), [TOK])
        self.assertNotIn(OWNER, self.docker_argv())
        self.assertNotIn(OWNER, text(res))
        self.assertNotIn(OWNER, read_if(self.cache_file()))

    def test_passes_the_command_through_untouched_hostile_arguments_included(self):
        arg = 'a b;$(id)`x`"y"'

        res = self.run_slot(cmd=("echo", arg))

        self.assertEqual(res.returncode, 0, text(res))
        self.assertIn(arg, self.execs()[0]["argv"])

    def test_exits_with_the_exit_code_of_the_command(self):
        res = self.run_slot(cmd=("sh", "-c", "exit 7"))

        self.assertEqual(res.returncode, 7, text(res))

    def test_does_not_mint_or_run_anything_for_a_slot_nobody_holds(self):
        os.remove(self.harness.lease_path(self.name))

        res = self.run_slot()

        self.assertNotEqual(res.returncode, 0)
        self.assertEqual(self.curl_argv(), [])
        self.assertEqual(self.execs(), [])

    def test_does_not_mint_for_a_slot_with_no_running_containers(self):
        self.harness.mark_stopped(self.name)

        res = self.run_slot()

        self.assertNotEqual(res.returncode, 0)
        self.assertIn("restart", res.stderr.decode())
        self.assertEqual(self.execs(), [])
        self.assertEqual(self.curl_argv(), [])

    def test_a_failed_mint_stops_before_the_exec_with_exit_status_1(self):
        self.set_mint(TOK, 3600, "http")

        res = self.run_slot()

        self.assertEqual(res.returncode, 1, text(res))
        self.assertEqual(self.execs(), [])
        self.expect_no_leak(res)


class TheInstallationTokenLock(GithubAppRun):

    def test_two_runs_at_once_mint_once_also_when_mkdir_lets_the_loser_of_a_race_succeed(self):
        self.use_loser_succeeds_mkdir()
        write(os.path.join(self.fake, "curl-delay"), "1")
        first = self.start_run("a")
        second = self.start_run("b")

        (code_a, log_a), (code_b, log_b) = self.finish(first), self.finish(second)

        self.assertEqual(code_a, 0, log_a)
        self.assertEqual(code_b, 0, log_b)
        self.assertEqual(len(self.curl_argv()), 1)
        self.assertEqual(self.exec_tokens(), [TOK, TOK])
        self.assertEqual(self.token_entries(), ["demo"])

    def test_a_held_lock_blocks_a_second_run_which_uses_the_first_token_once_released(self):
        holder = self.hold_mint()
        waiter = self.start_run("waiter")

        time.sleep(1.5)

        self.assertIsNone(waiter[0].poll(), "the second run did not wait for the lock")
        self.assertEqual(len(self.curl_argv()), 1)
        self.release_mint()
        (code_a, log_a), (code_b, log_b) = self.finish(holder), self.finish(waiter)
        self.assertEqual(code_a, 0, log_a)
        self.assertEqual(code_b, 0, log_b)
        self.assertEqual(len(self.curl_argv()), 1)
        self.assertEqual(self.exec_tokens(), [TOK, TOK])
        self.assertEqual(self.token_entries(), ["demo"])

    def test_a_run_that_cannot_get_the_lock_in_150_attempts_refuses_busy_and_never_mints(self):
        self.use_quick_sleep()
        holder = self.hold_mint()

        res = self.run_slot()

        self.assertEqual(res.returncode, 1, text(res))
        self.assertRegex(
            res.stderr.decode(),
            r"the installation-token lock at .*tokens/demo\.lock was busy for 30s",
        )
        self.assertEqual(len(self.curl_argv()), 1)
        self.assertTrue(os.path.exists(self.lock_path()), "the holder's lock is gone")
        self.release_mint()
        code, log = self.finish(holder)
        self.assertEqual(code, 0, log)
        self.assertEqual(self.exec_tokens(), [TOK])
        self.assertEqual(self.token_entries(), ["demo"])


class SlotIdentityCase(AuthTestCase):

    def git_local(self, name, key):
        return subprocess.run(
            ["git", "-C", self.harness.slot_dir(name), "config", "--local", key],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        ).stdout.decode().strip()

    def expect_bot_identity(self, name):
        env = self.slot_env(f"{name}")
        self.assertEqual(env_value(env, "GIT_USER_NAME"), BOT_NAME)
        self.assertEqual(env_value(env, "GIT_USER_EMAIL"), BOT_EMAIL)
        self.assertEqual(count_key(env, "GIT_USER_NAME"), 1)
        self.assertEqual(count_key(env, "GIT_USER_EMAIL"), 1)
        self.assertEqual(self.git_local(name, "user.name"), BOT_NAME)
        self.assertEqual(self.git_local(name, "user.email"), BOT_EMAIL)
        self.assertEqual(self.git_local(name, "gpg.format"), "ssh")
        self.assertRegex(self.git_local(name, "user.signingkey"), r"container_signing\.pub$")
        self.assertEqual(self.git_local(name, "commit.gpgsign"), "true")

    def no_docker_call(self):
        self.assertEqual(read_if(self.harness.docker_log), "")
        self.assertEqual(self.calls(), [])

    def no_slot_directory(self):
        slots = self.harness.slots_dir
        self.assertEqual(os.listdir(slots) if os.path.isdir(slots) else [], [])


class AcquireGivesANewSlotTheAppBotIdentity(SlotIdentityCase):

    def acquired(self, lines=None):
        self.project(env_lines=lines)
        return self.acquire()

    def test_writes_the_bot_name_and_noreply_email_to_the_slot_env_and_the_checkout_config(self):
        name = self.acquired()

        self.expect_bot_identity(name)

    def test_writes_no_token_line_though_the_owner_file_holds_one(self):
        name = self.acquired(app_lines() + ["GH_TOKEN=" + OWNER])

        env = self.slot_env(name)
        self.assertEqual(count_key(env, "GH_TOKEN"), 0)
        self.assertNotIn(MARK, read_if(env))
        self.expect_bot_identity(name)

    def test_has_the_bot_email_and_no_token_line_at_the_moment_the_containers_are_brought_up(self):
        self.project(env_lines=app_lines() + ["GH_TOKEN=" + OWNER])
        acquired = self.harness.run_acquire()
        self.assertEqual(acquired.returncode, 0, text(acquired))

        snapshots = self.up_envs()

        self.assertTrue(snapshots)
        for lines in snapshots:
            self.assertFalse([l for l in lines if l.startswith("GH_TOKEN=")], lines)
            self.assertIn("GIT_USER_EMAIL=" + BOT_EMAIL, lines)

    def test_takes_the_last_line_of_a_key_given_twice_and_ignores_a_commented_out_one(self):
        name = self.acquired(
            ["# FORGE_APP_SLUG=commented-agent", "FORGE_APP_SLUG=first-agent"]
            + app_lines()
            + ["FORGE_APP_BOT_ID=1", "FORGE_APP_BOT_ID=" + BOT_ID]
        )

        self.expect_bot_identity(name)

    def test_follows_the_slug_and_bot_id_of_the_env_file(self):
        name = self.acquired(app_lines(FORGE_APP_SLUG="other-app", FORGE_APP_BOT_ID="42"))

        env = self.slot_env(name)
        self.assertEqual(env_value(env, "GIT_USER_NAME"), "other-app[bot]")
        self.assertEqual(
            self.git_local(name, "user.email"), "42+other-app[bot]@users.noreply.github.com"
        )

    def test_a_missing_app_name_refuses_before_docker_and_before_cloning_naming_only_it(self):
        self.project()
        for name in APP_NAMES:
            with self.subTest(name):
                self.owner_env(app_lines(**{name: None}))

                res = self.harness.run_acquire()

                self.assertNotEqual(res.returncode, 0)
                err = res.stderr.decode()
                self.assertIn(name, err)
                for other in APP_NAMES:
                    if other != name:
                        self.assertNotIn(other, err)
                self.assertIn(ENV_FILE_REL, err)
                self.no_docker_call()
                self.no_slot_directory()

    def test_several_missing_are_all_named_in_the_same_refusal(self):
        self.project()
        self.owner_env(
            app_lines(FORGE_APP_ID=None, FORGE_APP_KEY_FILE=None, FORGE_APP_REPOS=None)
        )

        res = self.harness.run_acquire()

        self.assertNotEqual(res.returncode, 0)
        err = res.stderr.decode()
        for name in ("FORGE_APP_ID", "FORGE_APP_KEY_FILE", "FORGE_APP_REPOS"):
            self.assertIn(name, err)
        self.assertNotIn("FORGE_APP_SLUG", err)
        self.no_slot_directory()

    def test_an_empty_value_or_a_commented_out_line_counts_as_missing(self):
        self.project()
        self.owner_env(
            app_lines(FORGE_APP_SLUG=None, FORGE_APP_BOT_ID=None)
            + ["FORGE_APP_SLUG=", "# FORGE_APP_BOT_ID=" + BOT_ID]
        )

        res = self.harness.run_acquire()

        self.assertNotEqual(res.returncode, 0)
        self.assertIn("FORGE_APP_SLUG", res.stderr.decode())
        self.assertIn("FORGE_APP_BOT_ID", res.stderr.decode())
        self.no_slot_directory()

    def test_no_env_file_at_all_refuses_naming_the_app_names(self):
        self.project()
        os.remove(os.path.join(self.harness.fixture_repo, ENV_FILE_REL))

        res = self.harness.run_acquire()

        self.assertNotEqual(res.returncode, 0)
        self.assertIn("FORGE_APP_ID", res.stderr.decode())
        self.no_docker_call()
        self.no_slot_directory()

    def test_prints_no_value_of_the_env_file_when_it_refuses(self):
        self.project(env_lines=app_lines(FORGE_APP_REPOS=None))

        res = self.harness.run_acquire()

        self.assertNotEqual(res.returncode, 0)
        self.assertIn("FORGE_APP_REPOS", res.stderr.decode())
        for value in (APP_ID, INSTALLATION, BOT_ID, KEY_FILE):
            self.assertNotIn(value, text(res))


class RestartMovesASlotToTheAppBotIdentity(SlotIdentityCase):
    OLD_NAME = "old-agent"
    OLD_EMAIL = "old-agent@example.com"

    def setUp(self):
        super().setUp()
        self.project()

    def old_slot(self, extra=(), name="probe", identity=None):
        who, email = identity or (self.OLD_NAME, self.OLD_EMAIL)
        ports = "".join(
            f"{port}={slot_port(1, index)}\n" for index, port in enumerate(DEFAULT_PORT_NAMES)
        )
        env = f"GIT_USER_NAME={who}\nGIT_USER_EMAIL={email}\n"
        env += "".join(line + "\n" for line in extra) + "KEEP_ME=1\n"
        self.harness.make_bare_slot(
            name, env_content=env, ports_content=ports, local_identity=(who, email)
        )
        return name

    def restart(self, name="probe"):
        return self.harness.run("restart", name)

    def test_moves_a_slot_off_container_env_token_gone_bot_name_and_email_signing_intact(self):
        name = self.old_slot(["GH_TOKEN=" + OLD])

        res = self.restart(name)

        self.assertEqual(res.returncode, 0, text(res))
        env = self.slot_env(name)
        self.assertEqual(count_key(env, "GH_TOKEN"), 0)
        self.assertNotIn(MARK, read_if(env))
        self.expect_bot_identity(name)
        self.assertNotIn(MARK, text(res))

    def test_leaves_every_other_line_as_it_was(self):
        name = self.old_slot(["GH_TOKEN=" + OLD])
        ports = self.harness.read_ports(name)

        res = self.restart(name)

        self.assertEqual(res.returncode, 0, text(res))
        env = self.slot_env(name)
        self.assertEqual(env_value(env, "KEEP_ME"), "1")
        for port, value in ports.items():
            self.assertEqual(env_value(env, port), value)

    def test_removes_every_token_line_an_empty_one_a_duplicate_and_one_with_hostile_characters(self):
        name = self.old_slot(["GH_TOKEN=", "GH_TOKEN=a&b|c/d$HOME`id`", "GH_TOKEN=" + OLD])

        res = self.restart(name)

        self.assertEqual(res.returncode, 0, text(res))
        env = self.slot_env(name)
        self.assertEqual(count_key(env, "GH_TOKEN"), 0)
        self.assertNotIn(MARK, read_if(env))

    def test_does_not_touch_a_key_that_merely_starts_with_gh_token(self):
        name = self.old_slot(["GH_TOKEN_FILE=/x", "GH_TOKEN=" + OLD])

        res = self.restart(name)

        self.assertEqual(res.returncode, 0, text(res))
        env = self.slot_env(name)
        self.assertEqual(env_value(env, "GH_TOKEN_FILE"), "/x")
        self.assertEqual(count_key(env, "GH_TOKEN"), 0)

    def test_has_no_token_line_and_the_bot_email_at_the_moment_the_containers_are_brought_up(self):
        name = self.old_slot(["GH_TOKEN=" + OLD])

        res = self.restart(name)

        self.assertEqual(res.returncode, 0, text(res))
        snapshots = self.up_envs()
        self.assertTrue(snapshots)
        for lines in snapshots:
            self.assertFalse([l for l in lines if l.startswith("GH_TOKEN=")], lines)
            self.assertIn("GIT_USER_EMAIL=" + BOT_EMAIL, lines)

    def test_is_idempotent_a_second_restart_changes_nothing(self):
        name = self.old_slot(["GH_TOKEN=" + OLD])
        self.assertEqual(self.restart(name).returncode, 0)
        once = read_if(self.slot_env(name))

        res = self.restart(name)

        self.assertEqual(res.returncode, 0, text(res))
        self.assertEqual(read_if(self.slot_env(name)), once)
        self.expect_bot_identity(name)

    def test_a_slot_already_on_the_bot_identity_stays_as_it_is(self):
        name = self.old_slot(identity=(BOT_NAME, BOT_EMAIL))

        res = self.restart(name)

        self.assertEqual(res.returncode, 0, text(res))
        self.expect_bot_identity(name)
        self.assertEqual(count_key(self.slot_env(name), "GH_TOKEN"), 0)

    def test_is_refused_with_env_and_git_config_unchanged_when_the_app_settings_are_missing(self):
        self.owner_env(app_lines(FORGE_APP_BOT_ID=None, FORGE_APP_SLUG=None))
        name = self.old_slot(["GH_TOKEN=" + OLD])
        before = read_if(self.slot_env(name))

        res = self.restart(name)

        self.assertNotEqual(res.returncode, 0)
        err = res.stderr.decode()
        self.assertIn("FORGE_APP_BOT_ID", err)
        self.assertIn("FORGE_APP_SLUG", err)
        self.assertIn(ENV_FILE_REL, err)
        self.assertEqual(read_if(self.slot_env(name)), before)
        self.assertEqual(self.git_local(name, "user.email"), self.OLD_EMAIL)


class ContainerEnvCopiesTheTokenIntoTheSlotEnv(AuthTestCase):
    IDENTITY = ["GIT_USER_NAME=demo-agent", "GIT_USER_EMAIL=demo-agent@example.com"]

    def test_takes_the_value_from_the_projects_own_file_not_another(self):
        self.project(
            "token",
            env_lines=self.IDENTITY + ["GH_TOKEN=" + OWNER],
            FORGE_AUTH_FILE="secrets/agent.env",
        )
        self.owner_env(["GH_TOKEN=" + TOK], rel="secrets/agent.env")

        name = self.acquire()

        env = self.slot_env(name)
        self.assertEqual(env_value(env, "GH_TOKEN"), TOK)
        self.assertEqual(count_key(env, "GH_TOKEN"), 1)

    def test_the_slot_env_is_0600_under_umask_022_after_acquire_and_after_restart(self):
        self.addCleanup(os.umask, os.umask(0o022))
        self.project("token")
        name = self.acquire()
        self.assertEqual(stat.S_IMODE(os.stat(self.slot_env(name)).st_mode), 0o600)

        res = self.harness.run("restart", name)

        self.assertEqual(res.returncode, 0, text(res))
        self.assertEqual(stat.S_IMODE(os.stat(self.slot_env(name)).st_mode), 0o600)

    def test_restart_rewrites_the_line_from_the_file_as_it_is_now(self):
        self.project("token")
        name = self.acquire()
        self.owner_env(["GH_TOKEN=" + OTHER_TOK] + self.IDENTITY)

        res = self.harness.run("restart", name)

        self.assertEqual(res.returncode, 0, text(res))
        env = self.slot_env(name)
        self.assertEqual(env_value(env, "GH_TOKEN"), OTHER_TOK)
        self.assertEqual(count_key(env, "GH_TOKEN"), 1)

    def test_the_token_is_gone_from_the_machine_after_release(self):
        self.project("token")
        name = self.acquire()
        self.assertIn(TOK, read_if(self.slot_env(name)))

        res = self.harness.run("release", name, "--for", "test")

        self.assertEqual(res.returncode, 0, text(res))
        self.assertFalse(os.path.exists(self.slot_env(name)))
        for root, _, files in os.walk(self.harness.state_dir):
            for file in files:
                self.assertNotIn(TOK, read_if(os.path.join(root, file)), file)

    def test_the_token_is_in_the_env_file_handed_to_compose_up(self):
        self.project("token")
        acquired = self.harness.run_acquire()
        self.assertEqual(acquired.returncode, 0, text(acquired))

        snapshots = self.up_envs()

        self.assertTrue(snapshots)
        for lines in snapshots:
            self.assertIn("GH_TOKEN=" + TOK, lines)

    def test_the_token_is_not_written_into_the_checkout(self):
        self.project("token")
        name = self.acquire()

        for root, _, files in os.walk(self.harness.slot_dir(name)):
            for file in files:
                self.assertNotIn(TOK, read_if(os.path.join(root, file)), os.path.join(root, file))

    def test_a_missing_blank_or_commented_token_refuses_before_docker_and_clone_naming_it(self):
        self.project("token")
        cases = {
            "absent": self.IDENTITY,
            "blank": self.IDENTITY + ["GH_TOKEN="],
            "commented out": self.IDENTITY + ["# GH_TOKEN=" + TOK],
            "blanked by a later line": ["GH_TOKEN=" + TOK] + self.IDENTITY + ["GH_TOKEN="],
        }
        for why, lines in cases.items():
            with self.subTest(why):
                self.owner_env(lines)

                res = self.harness.run_acquire()

                self.assertNotEqual(res.returncode, 0)
                err = res.stderr.decode()
                self.assertIn("GH_TOKEN", err)
                self.assertIn(ENV_FILE_REL, err)
                self.assertNotIn(MARK, text(res))
                self.assertEqual(read_if(self.harness.docker_log), "")
                slots = self.harness.slots_dir
                self.assertEqual(os.listdir(slots) if os.path.isdir(slots) else [], [])

    def test_the_last_line_wins_when_the_token_is_given_twice(self):
        self.project("token", env_lines=self.IDENTITY + ["GH_TOKEN=" + OLD, "GH_TOKEN=" + TOK])

        name = self.acquire()

        self.assertEqual(env_value(self.slot_env(name), "GH_TOKEN"), TOK)

    def test_a_restart_with_the_token_gone_is_refused_and_leaves_the_slot_env_as_it_was(self):
        self.project("token")
        name = self.acquire()
        before = read_if(self.slot_env(name))
        self.owner_env(self.IDENTITY)

        res = self.harness.run("restart", name)

        self.assertNotEqual(res.returncode, 0)
        self.assertIn("GH_TOKEN", res.stderr.decode())
        self.assertEqual(read_if(self.slot_env(name)), before)

    def test_run_passes_no_token_name_and_never_calls_curl(self):
        self.project("token")
        name = self.acquire()

        res = self.run_slot()

        self.assertEqual(res.returncode, 0, text(res))
        execs = self.execs()
        self.assertEqual(len(execs), 1)
        self.assertNotIn("GH_TOKEN", execs[0]["argv"])
        self.assertEqual(execs[0]["passed"], {})
        self.assertEqual(execs[0]["gh"], "UNSET")
        self.assertEqual(self.curl_argv(), [])
        self.assertFalse(os.path.exists(self.token_dir()))

    def test_the_value_is_in_no_output_argument_or_log_of_acquire_restart_or_run(self):
        self.project("token")
        acquired = self.harness.run_acquire()
        self.assertEqual(acquired.returncode, 0, text(acquired))
        name = self.harness.acquired_slot_name(acquired)
        self.sid = f"{PROJECT_PREFIX}-{name}"
        restarted = self.harness.run("restart", name)
        ran = self.harness.run("run", name, "--", "true")

        for res in (acquired, restarted, ran):
            self.assertNotIn(TOK, text(res))
        self.assertNotIn(TOK, self.docker_argv())
        self.assertNotIn(TOK, read_if(self.harness.docker_log))
        self.assertNotIn(TOK, "\n".join(self.curl_argv()))


class WithNoForgeAuthTheSlotHoldsNoToken(AuthTestCase):

    def setUp(self):
        super().setUp()
        self.project(None)

    def test_the_slot_env_carries_no_token_though_the_owners_file_holds_one(self):
        name = self.acquire()

        env = self.slot_env(name)
        self.assertEqual(count_key(env, "GH_TOKEN"), 0)
        self.assertNotIn(MARK, read_if(env))

    def test_run_is_as_before_no_mint_no_token_name_nothing_in_the_exec_environment(self):
        name = self.acquire()

        res = self.run_slot()

        self.assertEqual(res.returncode, 0, text(res))
        execs = self.execs()
        self.assertEqual(len(execs), 1)
        self.assertEqual(execs[0]["gh"], "UNSET")
        self.assertEqual(execs[0]["passed"], {})
        self.assertEqual(self.curl_argv(), [])
        self.assertFalse(os.path.exists(self.token_dir()))


class LiveCase(AuthTestCase):
    PROJECT_KEYS = {"LIVE_ENV_FILE": ".env", "LIVE_SERVICE": "live"}

    def setUp(self):
        super().setUp()
        self.project("app", **self.PROJECT_KEYS)
        self.live_env([f"{k}={v}" for k, v in LIVE_VALUES.items()])
        self.acquire()
        self.slot_env_before = read_if(self.slot_env())

    def live_env(self, lines):
        write(os.path.join(self.harness.fixture_repo, ".env"), "".join(l + "\n" for l in lines))

    def live(self, names=(), extra=None, cmd=("true",)):
        return self.run_slot(cmd, live=names, extra=extra)

    def handed(self):
        runs = self.live_runs()
        self.assertTrue(runs, "no compose run was made")
        return {k: v for k, (state, v) in runs[-1]["passed"].items() if state != "UNSET"}

    def control_runs(self):
        res = self.live(["LOOKUP_API_KEY"])
        self.assertEqual(res.returncode, 0, "a plain --live request must work: " + text(res))
        return len(self.live_runs())

    def expect_no_value_anywhere(self, res, values):
        skip = (
            os.path.join(self.harness.fixture_repo, ".env"),
            self.fake,
            os.path.join(self.harness.fixture_repo, ".git"),
        )
        files = []
        for root, dirs, names in os.walk(self.harness.tmp):
            dirs[:] = [d for d in dirs if os.path.join(root, d) not in skip]
            files += [
                os.path.join(root, n) for n in names if os.path.join(root, n) not in skip
            ]
        for value in values:
            self.assertNotIn(value, res.stdout.decode(), "stdout")
            self.assertNotIn(value, res.stderr.decode(), "stderr")
            self.assertNotIn(value, self.docker_argv(), "docker argv")
            for path in files:
                with open(path, "rb") as handle:
                    self.assertNotIn(value.encode(), handle.read(), path)


class AnOrdinaryRunHoldsNoKey(LiveCase):

    def test_hands_none_of_the_keys_to_an_exec_without_live_though_the_owner_file_holds_all(self):
        res = self.live()

        self.assertEqual(res.returncode, 0, text(res))
        passed = self.execs()[-1]["passed"]
        for key in ALL_LIVE:
            self.assertNotIn(key, passed)

    def test_does_not_pass_on_a_key_it_inherited_from_its_caller_when_none_is_asked_for(self):
        res = self.live(extra={"SEARCH_API_KEY": LIVE_VALUES["SEARCH_API_KEY"]})

        self.assertEqual(res.returncode, 0, text(res))
        self.assertNotIn("SEARCH_API_KEY", self.execs()[-1]["passed"])
        self.assertNotIn(LIVE_VALUES["SEARCH_API_KEY"], self.docker_argv())

    def test_does_not_copy_any_key_into_the_slot_env_with_or_without_a_live_run(self):
        self.live()
        for key in ALL_LIVE:
            self.assertEqual(count_key(self.slot_env(), key), 0, key)

        res = self.live(["SEARCH_API_KEY"])

        self.assertEqual(res.returncode, 0, text(res))
        self.assertEqual(read_if(self.slot_env()), self.slot_env_before)


class LiveHandsOnlyTheNamedKeysFromTheOwnersFile(LiveCase):

    def test_hands_the_one_named_key_and_no_other_though_the_file_holds_more(self):
        res = self.live(["SEARCH_API_KEY"])

        self.assertEqual(res.returncode, 0, text(res))
        self.assertEqual(self.handed(), {"SEARCH_API_KEY": LIVE_VALUES["SEARCH_API_KEY"]})

    def test_hands_several_named_keys_and_only_those(self):
        res = self.live(["LOOKUP_API_KEY", "VERIFY_API_KEY"])

        self.assertEqual(res.returncode, 0, text(res))
        self.assertEqual(
            self.handed(),
            {k: LIVE_VALUES[k] for k in ("LOOKUP_API_KEY", "VERIFY_API_KEY")},
        )

    def test_handles_all_four_names(self):
        res = self.live(LIVE_NAMES)

        self.assertEqual(res.returncode, 0, text(res))
        self.assertEqual(self.handed(), {k: LIVE_VALUES[k] for k in LIVE_NAMES})

    def test_is_generic_a_name_no_code_knows_is_handed_on_like_the_others(self):
        res = self.live([MADE_UP])

        self.assertEqual(res.returncode, 0, text(res))
        self.assertEqual(self.handed(), {MADE_UP: LIVE_VALUES[MADE_UP]})

    def test_takes_the_key_from_the_owners_file_not_from_the_caller_environment(self):
        self.live_env(["SEARCH_API_KEY=from-owner-file"])

        res = self.live(["SEARCH_API_KEY"], extra={"SEARCH_API_KEY": "from-caller-env"})

        self.assertEqual(res.returncode, 0, text(res))
        self.assertEqual(self.handed(), {"SEARCH_API_KEY": "from-owner-file"})

    def test_does_not_take_a_key_from_the_slot_env_held_only_there_it_counts_as_absent(self):
        before = self.control_runs()
        self.live_env(["LOOKUP_API_KEY=" + LIVE_VALUES["LOOKUP_API_KEY"]])
        with open(self.slot_env(), "a", encoding="utf-8") as handle:
            handle.write("SEARCH_API_KEY=from-slot-env\n")

        res = self.live(["SEARCH_API_KEY"])

        self.assertNotEqual(res.returncode, 0)
        self.assertEqual(len(self.live_runs()), before)
        self.assertNotIn("from-slot-env", text(res))

    def test_a_name_that_is_a_prefix_of_another_key_gets_its_own_value_not_the_longer_ones(self):
        self.live_env(["SEARCH_API_KEY=exact-value", "SEARCH_API_KEY_OLD=longer-value"])

        res = self.live(["SEARCH_API_KEY"])

        self.assertEqual(res.returncode, 0, text(res))
        self.assertEqual(self.handed(), {"SEARCH_API_KEY": "exact-value"})
        self.assertNotIn("longer-value", self.docker_argv() + text(res))

    def test_a_name_that_is_only_a_prefix_of_a_key_in_the_file_is_absent(self):
        before = self.control_runs()
        self.live_env(["LOOKUP_API_KEY=" + LIVE_VALUES["LOOKUP_API_KEY"], "SEARCH_API_KEY_OLD=longer-value"])

        res = self.live(["SEARCH_API_KEY"])

        self.assertNotEqual(res.returncode, 0)
        self.assertEqual(len(self.live_runs()), before)

    def test_a_key_named_twice_is_handed_once_intact_and_the_run_is_not_refused(self):
        res = self.live(["LOOKUP_API_KEY", "LOOKUP_API_KEY"])

        self.assertEqual(res.returncode, 0, text(res))
        self.assertEqual(len(self.live_runs()), 1)
        self.assertEqual(self.handed(), {"LOOKUP_API_KEY": LIVE_VALUES["LOOKUP_API_KEY"]})
        self.assertEqual(self.live_runs()[0]["argv"].count("-e LOOKUP_API_KEY"), 1)

    def test_an_unnamed_key_beside_the_named_one_in_the_file_stays_out(self):
        res = self.live(["MODEL_API_KEY"])

        self.assertEqual(res.returncode, 0, text(res))
        self.assertEqual(self.handed(), {"MODEL_API_KEY": LIVE_VALUES["MODEL_API_KEY"]})


class LiveRefusesAKeyItCannotHonour(LiveCase):

    def test_a_named_key_absent_from_the_file_refuses_the_whole_run_naming_it_without_a_value(self):
        before = self.control_runs()
        self.live_env(["LOOKUP_API_KEY=" + LIVE_VALUES["LOOKUP_API_KEY"]] * 2)

        res = self.live(["SEARCH_API_KEY", "LOOKUP_API_KEY"])

        self.assertNotEqual(res.returncode, 0)
        self.assertEqual(len(self.live_runs()), before)
        self.assertIn("SEARCH_API_KEY", res.stderr.decode())
        self.expect_no_value_anywhere(res, [LIVE_VALUES["LOOKUP_API_KEY"]])

    def test_a_key_present_only_as_a_commented_out_line_counts_as_absent(self):
        before = self.control_runs()
        self.live_env(["LOOKUP_API_KEY=" + LIVE_VALUES["LOOKUP_API_KEY"], "# SEARCH_API_KEY=commented-out-value"])

        res = self.live(["SEARCH_API_KEY"])

        self.assertNotEqual(res.returncode, 0)
        self.assertEqual(len(self.live_runs()), before)
        self.assertNotIn("commented-out-value", text(res))

    def test_a_commented_out_line_before_the_live_one_does_not_shadow_it(self):
        self.live_env(
            ["# SEARCH_API_KEY=commented-out-value", "  # SEARCH_API_KEY=indented-comment", "SEARCH_API_KEY=live-value"]
        )

        res = self.live(["SEARCH_API_KEY"])

        self.assertEqual(res.returncode, 0, text(res))
        self.assertEqual(self.handed(), {"SEARCH_API_KEY": "live-value"})

    def test_an_empty_value_counts_as_absent(self):
        before = self.control_runs()
        self.live_env(["LOOKUP_API_KEY=" + LIVE_VALUES["LOOKUP_API_KEY"], "SEARCH_API_KEY="])

        res = self.live(["SEARCH_API_KEY"])

        self.assertNotEqual(res.returncode, 0)
        self.assertEqual(len(self.live_runs()), before)

    def test_a_request_that_is_not_a_variable_name_is_refused_and_nothing_runs(self):
        before = self.control_runs()
        for bad in ("SEARCH_API_KEY=x", "NOT A NAME", "", "-x", "$(id)"):
            with self.subTest(bad):
                res = self.live([bad])

                self.assertNotEqual(res.returncode, 0)
        self.assertEqual(len(self.live_runs()), before)

    def test_live_with_no_name_before_the_double_dash_is_refused(self):
        before = self.control_runs()

        res = self.harness.run("run", self.name, "--live", "--", "true")

        self.assertNotEqual(res.returncode, 0)
        self.assertEqual(len(self.live_runs()), before)


class LiveValuesArriveIntactAndAreNeverEvaluated(LiveCase):
    BACKSLASH = chr(92)
    BACKTICK = chr(96)
    QUOTE = chr(39)

    def awkward(self):
        return {
            "spaces": "two words  and   more",
            "an equals sign": "abc=def==ghi=",
            "shell metacharacters": "a;b|c&d>e<f*g?h[i]{j}~k#l",
            "a command substitution": "$(touch PWNED_FILE)",
            "backticks": self.BACKTICK + "touch PWNED_FILE" + self.BACKTICK,
            "a variable reference": "$" + "{HOME}-$HOME-$PWNED_VAR",
            "quotes and a backslash": "it" + self.QUOTE + "s " + chr(34) + "q" + chr(34) + " " + self.BACKSLASH + "n end",
            "a trailing space": "value-with-trailing-space ",
        }

    def test_carries_every_awkward_value_byte_for_byte_and_evaluates_nothing(self):
        for what, value in self.awkward().items():
            with self.subTest(what):
                self.live_env(["SEARCH_API_KEY=" + value])

                res = self.live(["SEARCH_API_KEY"])

                self.assertEqual(res.returncode, 0, text(res))
                self.assertEqual(self.handed(), {"SEARCH_API_KEY": value})
                for directory in (
                    self.harness.tmp, self.harness.fixture_repo,
                    self.harness.slot_dir(self.name), REPO_ROOT,
                ):
                    self.assertFalse(os.path.exists(os.path.join(directory, "PWNED_FILE")), directory)
                self.assertNotIn("PWNED_VAR", self.live_runs()[-1]["passed"])
                self.assertNotIn(value, self.docker_argv())

    def test_a_value_holding_the_name_of_another_key_does_not_pull_that_key_in(self):
        self.live_env(["SEARCH_API_KEY=LOOKUP_API_KEY", "LOOKUP_API_KEY=" + LIVE_VALUES["LOOKUP_API_KEY"]])

        res = self.live(["SEARCH_API_KEY"])

        self.assertEqual(res.returncode, 0, text(res))
        self.assertEqual(self.handed(), {"SEARCH_API_KEY": "LOOKUP_API_KEY"})


class ALiveKeyReachesOnlyTheRunItWasAskedFor(LiveCase):

    def git_status(self, directory):
        return subprocess.run(
            [self.harness.real_git_bin, "-C", directory, "status", "--porcelain", "--untracked-files=no"],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        ).stdout.decode()

    def test_lands_in_no_stream_argv_state_file_or_slot_file_and_the_slot_env_is_unchanged(self):
        res = self.live(["SEARCH_API_KEY", "LOOKUP_API_KEY"])

        self.assertEqual(res.returncode, 0, text(res))
        self.assertEqual(self.handed()["SEARCH_API_KEY"], LIVE_VALUES["SEARCH_API_KEY"])
        self.expect_no_value_anywhere(
            res, [LIVE_VALUES["SEARCH_API_KEY"], LIVE_VALUES["LOOKUP_API_KEY"]]
        )
        self.assertEqual(read_if(self.slot_env()), self.slot_env_before)

    def test_leaves_the_owner_file_as_it_was_and_no_tracked_file_changes(self):
        owner = os.path.join(self.harness.fixture_repo, ".env")
        before = read_if(owner)

        res = self.live(["SEARCH_API_KEY"])

        self.assertEqual(res.returncode, 0, text(res))
        self.assertEqual(read_if(owner), before)
        self.assertEqual(self.git_status(self.harness.fixture_repo), "")
        self.assertEqual(self.git_status(self.harness.slot_dir(self.name)), "")

    def test_when_the_command_fails_the_exit_code_comes_through_and_no_value_is_printed(self):
        res = self.live(["SEARCH_API_KEY", "VERIFY_API_KEY"], cmd=("sh", "-c", "exit 3"))

        self.assertEqual(res.returncode, 3, text(res))
        self.assertEqual(self.handed()["SEARCH_API_KEY"], LIVE_VALUES["SEARCH_API_KEY"])
        self.expect_no_value_anywhere(
            res, [LIVE_VALUES["SEARCH_API_KEY"], LIVE_VALUES["VERIFY_API_KEY"]]
        )

    def test_names_may_be_printed_values_never_whatever_the_outcome(self):
        for code in ("0", "1"):
            with self.subTest(code):
                res = self.live(LIVE_NAMES, cmd=("sh", "-c", "exit " + code))

                self.assertEqual(res.returncode, int(code), text(res))
                self.expect_no_value_anywhere(res, [LIVE_VALUES[k] for k in LIVE_NAMES])

    def test_an_ordinary_run_after_a_live_run_in_the_same_slot_holds_no_key(self):
        self.assertEqual(self.live(["SEARCH_API_KEY", "LOOKUP_API_KEY"]).returncode, 0)

        res = self.live()

        self.assertEqual(res.returncode, 0, text(res))
        passed = self.execs()[-1]["passed"]
        for key in ALL_LIVE:
            self.assertNotIn(key, passed)

    def test_a_live_run_after_a_live_run_hands_only_what_the_second_names(self):
        self.live(["SEARCH_API_KEY", "LOOKUP_API_KEY"])

        res = self.live(["VERIFY_API_KEY"])

        self.assertEqual(res.returncode, 0, text(res))
        self.assertEqual(self.handed(), {"VERIFY_API_KEY": LIVE_VALUES["VERIFY_API_KEY"]})

    def test_passes_the_key_by_name_to_a_one_shot_service_never_name_equals_value(self):
        res = self.live(["SEARCH_API_KEY"])

        self.assertEqual(res.returncode, 0, text(res))
        call = self.live_runs()[-1]
        self.assertRegex(call["argv"], r"\brun\b.*--rm.*-e SEARCH_API_KEY\b.*\blive\b")
        self.assertNotIn("SEARCH_API_KEY=", call["argv"])
        self.assertEqual(call["svc"], "live")
        self.assertEqual(call["profiles"], "live")
        self.assertIn("-u node", call["argv"])
        self.assertEqual(self.execs(), [])


class LiveRunsHoldNoForgeToken(LiveCase):

    def test_mints_nothing_and_passes_no_token_even_with_an_expired_cache(self):
        self.write_cache(-10, OLD)
        before = read_if(self.cache_file())

        res = self.live(["SEARCH_API_KEY"])

        self.assertEqual(res.returncode, 0, text(res))
        self.assertEqual(self.curl_argv(), [])
        self.assertEqual(read_if(self.cache_file()), before)
        for call in self.calls():
            self.assertEqual(call["gh"], "UNSET", call["argv"])
        self.assertNotIn("GH_TOKEN", self.live_runs()[-1]["passed"])
        self.assertNotIn("GH_TOKEN", self.live_runs()[-1]["argv"])

    def test_composes_from_the_main_checkout_with_the_slot_env_file(self):
        res = self.live(["SEARCH_API_KEY"])

        self.assertEqual(res.returncode, 0, text(res))
        call = self.live_runs()[-1]
        self.assertEqual(
            os.path.realpath(call["pdir"]), os.path.realpath(self.harness.main_devcontainer())
        )
        self.assertIn(self.slot_env(), call["argv"])


class LiveNeedsItsSettings(AuthTestCase):

    def refused(self, keys):
        self.project("app", **keys)
        write(
            os.path.join(self.harness.fixture_repo, ".env"),
            "LOOKUP_API_KEY=" + LIVE_VALUES["LOOKUP_API_KEY"] + "\n",
        )
        self.acquire()

        res = self.run_slot(("sh", "-c", "echo should-not-run"), live=["LOOKUP_API_KEY"])

        self.assertNotEqual(res.returncode, 0)
        self.assertNotIn("should-not-run", res.stdout.decode())
        self.assertEqual(self.execs(), [])
        self.assertEqual(self.live_runs(), [])
        self.assertEqual(self.curl_argv(), [])
        return res.stderr.decode()

    def test_without_a_live_service_it_refuses_before_anything_runs(self):
        err = self.refused({"LIVE_ENV_FILE": ".env"})

        self.assertIn("LIVE_SERVICE", err)

    def test_without_a_live_env_file_it_refuses_before_anything_runs(self):
        err = self.refused({"LIVE_SERVICE": "live"})

        self.assertIn("LIVE_ENV_FILE", err)


class TheTokenCacheBelongsToTheProject(AuthTestCase):

    def setUp(self):
        super().setUp()
        self.project()
        self.acquire()
        self.other = DevContainerHarness()
        self.addCleanup(self.other.cleanup)
        self.other.state_dir = self.harness.state_dir
        self.install_stubs(self.other)
        other_key = os.path.join(self.other.tmp, "keys", "app.pem")
        os.makedirs(os.path.dirname(other_key))
        shutil.copyfile(KEY_FILE, other_key)
        os.chmod(other_key, 0o600)
        self.project(
            harness=self.other, env_lines=app_lines(FORGE_APP_KEY_FILE=other_key), PROJECT="other"
        )
        subprocess.run(
            [self.other.real_git_bin, "-C", self.other.fixture_repo, "remote", "set-url",
             "origin", "https://example.invalid/other.git"],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        acquired = self.other.run("acquire", "--for", "test")
        self.assertEqual(acquired.returncode, 0, text(acquired))
        found = re.search("other-(slot-[0-9]+)", acquired.stdout.decode())
        self.assertTrue(found, acquired.stdout.decode())
        self.other_name = found.group(1)
        self.clear_calls()

    def run_other(self):
        return self.run_slot(name=self.other_name, harness=self.other)

    def test_another_project_mints_its_own_token_and_the_first_projects_file_stays_byte_identical(self):
        self.write_cache(3000, OLD)
        before = read_if(self.cache_file())
        self.set_mint(OTHER_TOK, 3600)

        res = self.run_other()

        self.assertEqual(res.returncode, 0, text(res))
        self.assertEqual(len(self.curl_argv()), 1)
        self.assertEqual(self.exec_tokens(), [OTHER_TOK])
        self.assertEqual(self.read_cache("other")[1], OTHER_TOK)
        self.assertEqual(read_if(self.cache_file()), before)
        self.assertEqual(stat.S_IMODE(os.stat(self.cache_file("other")).st_mode), 0o600)
        self.assertEqual(self.token_entries(), ["demo", "other"])

    def test_a_failed_mint_refuses_and_never_falls_back_to_the_first_projects_cache(self):
        self.write_cache(3000, OLD)
        before = read_if(self.cache_file())
        self.set_mint(OTHER_TOK, 3600, "http")

        res = self.run_other()

        self.assertNotEqual(res.returncode, 0)
        self.assertEqual(self.execs(), [])
        self.assertEqual(read_if(self.cache_file()), before)
        self.assertFalse(os.path.exists(self.cache_file("other")))
        self.assertNotIn(OLD, text(res) + self.docker_argv())

    def test_each_project_keeps_using_its_own_cache(self):
        self.write_cache(3000, OLD)
        self.set_mint(OTHER_TOK, 3600)
        self.assertEqual(self.run_other().returncode, 0)

        res = self.run_slot()

        self.assertEqual(res.returncode, 0, text(res))
        self.assertEqual(self.exec_tokens(), [OTHER_TOK, OLD])
        self.assertEqual(len(self.curl_argv()), 1)

    def test_a_run_from_one_project_on_the_others_slot_refuses_before_any_mint(self):
        os.makedirs(self.harness.slots_dir, exist_ok=True)
        os.symlink(self.other.slot_dir(self.other_name), self.harness.slot_dir("probe-b"))

        res = self.run_slot(name="probe-b")

        self.assertNotEqual(res.returncode, 0)
        self.assertIn("not this project", res.stderr.decode())
        self.assertEqual(self.curl_argv(), [])
        self.assertEqual(self.execs(), [])
        self.assertEqual(self.token_entries(), [])


class ProjectReaderCase(unittest.TestCase):
    REQUIRED = {
        "PROJECT": "demo",
        "DEFAULT_BRANCH": "main",
        "SLOT_PORTS": "SSH_PORT",
        "COMPOSE_FILES": "docker-compose.yml",
    }

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="agent-auth-reader-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def read(self, **extra):
        values = dict(self.REQUIRED, **extra)
        directory = tempfile.mkdtemp(dir=self.tmp)
        write(os.path.join(directory, "project.env"), "".join(f"{k}={v}\n" for k, v in values.items()))
        result = subprocess.run(
            [os.path.join(REPO_ROOT, SCRIPT_REL), "project", directory],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=dict(os.environ, DEV_CONTAINER_HOME=os.path.join(self.tmp, "state")),
            cwd=self.tmp, timeout=30,
        )
        pairs = dict(l.split("=", 1) for l in result.stdout.decode().split("\n") if "=" in l)
        return result.returncode, pairs, result.stderr.decode()


class TheProjectFileNamesItsAuth(ProjectReaderCase):

    def test_reads_the_four_keys_as_they_stand(self):
        code, got, err = self.read(
            FORGE_AUTH="app", FORGE_AUTH_FILE=".devcontainer/.env",
            LIVE_ENV_FILE=".env", LIVE_SERVICE="live",
        )

        self.assertEqual(code, 0, err)
        self.assertEqual(got["FORGE_AUTH"], "app")
        self.assertEqual(got["FORGE_AUTH_FILE"], ".devcontainer/.env")
        self.assertEqual(got["LIVE_ENV_FILE"], ".env")
        self.assertEqual(got["LIVE_SERVICE"], "live")

    def test_both_values_of_forge_auth_are_accepted(self):
        for mode in ("token", "app"):
            with self.subTest(mode):
                code, got, err = self.read(FORGE_AUTH=mode, FORGE_AUTH_FILE=".env")

                self.assertEqual(code, 0, err)
                self.assertEqual(got["FORGE_AUTH"], mode)

    def test_an_unknown_value_is_refused_naming_the_allowed_values(self):
        for value in ("TOKEN", "GITHUB-APP", "github_app", "none", "gate"):
            with self.subTest(value):
                code, _, err = self.read(FORGE_AUTH=value, FORGE_AUTH_FILE=".env")

                self.assertNotEqual(code, 0)
                self.assertIn("FORGE_AUTH", err)
                self.assertIn("token, app", err)

    def test_forge_auth_without_a_file_is_refused_naming_it(self):
        code, _, err = self.read(FORGE_AUTH="app")

        self.assertNotEqual(code, 0)
        self.assertIn("FORGE_AUTH_FILE", err)

    def test_the_two_new_paths_must_be_relative_and_stay_inside_the_checkout(self):
        for key in ("FORGE_AUTH_FILE", "LIVE_ENV_FILE"):
            for entry in ("/etc/agent.env", "..", "../agent.env", "a/../b"):
                with self.subTest(key=key, entry=entry):
                    values = {"FORGE_AUTH": "app", "FORGE_AUTH_FILE": ".env", key: entry}
                    code, _, err = self.read(**values)

                    self.assertNotEqual(code, 0)
                    self.assertIn(key, err)
                    self.assertIn(entry, err)

    def test_a_token_name_is_refused_in_the_slot_env_keys_and_the_slot_env_set(self):
        for key, value in (
            ("SLOT_ENV_KEYS", "LOG_LEVEL GH_TOKEN"),
            ("SLOT_ENV_SET", "GH_TOKEN=x"),
            ("SLOT_ENV_SET", "LOG_LEVEL=info GH_TOKEN=x"),
        ):
            with self.subTest(key=key, value=value):
                code, _, err = self.read(**{key: value})

                self.assertNotEqual(code, 0)
                self.assertIn("GH_TOKEN", err)
                self.assertIn(key, err)

    def test_a_project_name_that_could_climb_out_of_the_token_directory_is_refused(self):
        for name in ("a/b", "..", "../x", "a b", "-x", "Up", "a.b", "x/"):
            with self.subTest(name):
                code, _, err = self.read(PROJECT=name)

                self.assertNotEqual(code, 0)
                self.assertIn("PROJECT", err)

    def test_an_ordinary_project_name_is_still_accepted(self):
        for name in ("demo", "my_proj-2", "9lives"):
            with self.subTest(name):
                code, got, err = self.read(PROJECT=name)

                self.assertEqual(code, 0, err)
                self.assertEqual(got["PROJECT"], name)


def legacy_app_lines(**over):
    return [l.replace("FORGE_APP_", "AGENT_APP_", 1) for l in app_lines(**over)]


APP_RENAMES = [(l.split("=", 1)[0].replace("FORGE_", "AGENT_", 1), l.split("=", 1)[0]) for l in app_lines()]


class TheOldNamesAreRefused(ProjectReaderCase):

    def test_an_old_key_is_refused_by_name_with_the_command_that_renames_it(self):
        for old, new, value in (
            ("AGENT_AUTH", "FORGE_AUTH", "app"),
            ("AGENT_AUTH_ENV_FILE", "FORGE_AUTH_FILE", ".env"),
        ):
            with self.subTest(old):
                code, got, err = self.read(**{old: value})

                self.assertNotEqual(code, 0)
                self.assertIn(f"{old} renamed to {new}; run", err)
                self.assertIn("migrate-names", err)
                self.assertEqual(got, {})

    def test_an_old_mode_is_refused_by_name_under_either_key(self):
        for key in ("FORGE_AUTH", "AGENT_AUTH"):
            for old, new in (("container-env", "token"), ("github-app", "app")):
                with self.subTest(key=key, mode=old):
                    code, _, err = self.read(**{key: old, "FORGE_AUTH_FILE": ".env"})

                    self.assertNotEqual(code, 0)
                    self.assertIn(f"{old} renamed to {new}; run", err)
                    self.assertIn("migrate-names", err)

    def test_an_old_key_is_refused_even_beside_the_new_one(self):
        code, _, err = self.read(FORGE_AUTH="app", AGENT_AUTH="app", FORGE_AUTH_FILE=".env")

        self.assertNotEqual(code, 0)
        self.assertIn("AGENT_AUTH renamed to FORGE_AUTH; run", err)

    def test_the_new_names_read_as_they_stand_with_no_notice(self):
        for mode in ("token", "app"):
            with self.subTest(mode):
                code, got, err = self.read(FORGE_AUTH=mode, FORGE_AUTH_FILE=".env")

                self.assertEqual(code, 0, err)
                self.assertEqual(got["FORGE_AUTH"], mode)
                self.assertEqual(got["FORGE_AUTH_FILE"], ".env")
                self.assertNotIn("deprecated", err)
                self.assertEqual(err, "")


class TheOldNamesAreRefusedAtAcquire(AuthTestCase):

    def test_an_old_name_in_project_env_refuses_acquire_and_starts_nothing(self):
        self.project(None, env_lines=self.default_env("token"), AGENT_AUTH="container-env", AGENT_AUTH_ENV_FILE=ENV_FILE_REL)

        res = self.harness.run_acquire()

        self.assertNotEqual(res.returncode, 0)
        self.assertIn("AGENT_AUTH renamed to FORGE_AUTH", text(res))
        self.assertIn("container-env renamed to token", text(res))
        self.assertIn("; run", text(res))
        self.assertEqual(self.calls(), [])

    def test_the_old_app_names_in_the_credential_file_are_refused_by_name(self):
        self.project("app", env_lines=legacy_app_lines())

        res = self.harness.run_acquire()

        err = res.stderr.decode()
        self.assertNotEqual(res.returncode, 0)
        for old, new in APP_RENAMES:
            self.assertIn(f"{old} renamed to {new}", err)
        self.assertIn("; run", err)
        self.assertIn("migrate-names", err)
        self.assertNotIn(MARK, text(res))
        self.assertEqual(self.calls(), [])

    def test_an_old_app_name_is_refused_even_when_the_new_one_is_there_too(self):
        self.project("app", env_lines=app_lines() + legacy_app_lines(FORGE_APP_SLUG="old-app"))

        res = self.harness.run_acquire()

        self.assertNotEqual(res.returncode, 0)
        self.assertIn("AGENT_APP_SLUG renamed to FORGE_APP_SLUG", res.stderr.decode())
        self.assertIn("; run", res.stderr.decode())

    def test_files_that_migrate_names_rewrote_acquire_and_mint(self):
        self.project(None, env_lines=legacy_app_lines(), AGENT_AUTH="github-app", AGENT_AUTH_ENV_FILE=ENV_FILE_REL)
        moved = subprocess.run(
            ["bash", os.path.join(REPO_ROOT, SCRIPT_REL), "migrate-names"],
            cwd=self.harness.tmp, env=self.harness._env({}),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60,
        )
        self.assertEqual(moved.returncode, 0, text(moved))

        res = self.harness.run_acquire()
        self.assertEqual(res.returncode, 0, text(res))
        self.name = self.harness.acquired_slot_name(res)
        self.clear_calls()
        run = self.run_slot()

        self.assertEqual(run.returncode, 0, text(run))
        self.assertEqual(self.exec_tokens(), [TOK])
        self.assertNotIn("deprecated", text(res) + text(run))

    def test_the_new_app_names_acquire_and_mint_with_no_notice(self):
        self.project("app")

        res = self.harness.run_acquire()
        self.name = self.harness.acquired_slot_name(res)
        self.clear_calls()
        run = self.run_slot()

        self.assertEqual(res.returncode, 0, text(res))
        self.assertEqual(run.returncode, 0, text(run))
        self.assertNotIn("deprecated", text(res) + text(run))


if __name__ == "__main__":
    unittest.main()
