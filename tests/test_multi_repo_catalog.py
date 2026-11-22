"""Tests for declarative SLOT_REPOS_FILE catalog, DOCS_REPO_URL compatibility,
project --json, acquire --repo, attach, multi-repo git identity/guards,
and FORGE_AUTH=app preflight/doctor validation (#49).
"""

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from harness import (
    DOCKER_STUB,
    GIT_STUB,
    PROJECT_PREFIX,
    REPO_ROOT,
    SCRIPT_REL,
    DevContainerTestCase,
    project_env_text,
)

ENGINE = os.path.join(REPO_ROOT, SCRIPT_REL)
FIXTURES = os.path.join(REPO_ROOT, "tests", "fixtures")
PLACEHOLDER_DOCS_URL = "https://github.com/example/demo.docs.git"
REQUIRED_ENV = {
    "PROJECT": "demo",
    "DEFAULT_BRANCH": "main",
    "SLOT_PORTS": "SSH_PORT",
    "COMPOSE_FILES": "docker-compose.yml",
}
IDENTITY_ENV = (
    "GIT_USER_NAME=demo-agent\n"
    "GIT_USER_EMAIL=demo-agent@example.com\n"
)

_KEY_DIR = None
KEY_FILE = None


def setUpModule():
    global _KEY_DIR, KEY_FILE
    _KEY_DIR = tempfile.mkdtemp(prefix="multi-repo-key-")
    KEY_FILE = os.path.join(_KEY_DIR, "app.pem")
    subprocess.run(
        ["openssl", "genrsa", "-out", KEY_FILE, "2048"],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def tearDownModule():
    shutil.rmtree(_KEY_DIR, ignore_errors=True)


def output_of(result):
    return result.stdout.decode(errors="replace") + result.stderr.decode(errors="replace")


DOCKER_SPY_WRAPPER = r"""#!/usr/bin/env bash
set -u
base="$(dirname "$0")/docker-base"
run_log="${TEST_DOCKER_RUN_LOG:-}"
compose_log="${TEST_COMPOSE_VERBS_LOG:-}"
forbid_exec="${TEST_FORBID_DOCKER_EXEC:-0}"

if [[ "${1:-}" == compose ]]; then
  verb=""
  for arg in "${@:2}"; do
    case "$arg" in
      up|down|stop|restart|exec|run|ps|config|build|ls) verb="$arg"; break ;;
    esac
  done
  if [[ -n "$compose_log" && -n "$verb" ]]; then
    printf '%s\n' "$verb" >> "$compose_log"
  fi
  if [[ "$forbid_exec" == "1" && ( "$verb" == "exec" || "$verb" == "run" ) ]]; then
    echo "forbidden docker compose $verb during host-only read" >&2
    exit 99
  fi
elif [[ "${1:-}" == run ]]; then
  if [[ "$forbid_exec" == "1" ]]; then
    echo "forbidden docker run during host-only read" >&2
    exit 99
  fi
  if [[ -n "$run_log" ]]; then
    has_secret_flag=0
    net_none=0
    prev=""
    for arg in "$@"; do
      case "$arg" in
        --env=FORGE_HOST_SECRET|--env=FORGE_HOST_SECRET=*) has_secret_flag=1 ;;
        FORGE_HOST_SECRET|FORGE_HOST_SECRET=*)
          [[ "$prev" == "-e" || "$prev" == "--env" ]] && has_secret_flag=1
          ;;
        --network=none) net_none=1 ;;
        none) [[ "$prev" == "--network" ]] && net_none=1 ;;
      esac
      prev="$arg"
    done
    secret_val_set=0
    [[ -n "${FORGE_HOST_SECRET:-}" ]] && secret_val_set=1
    stdin_tmp=$(mktemp)
    cat > "$stdin_tmp"
    jq -nc \
      --argjson argv "$(printf '%s\0' "$@" | jq -Rs 'split("\u0000")[:-1]')" \
      --argjson has_secret_flag "$has_secret_flag" \
      --argjson secret_val_set "$secret_val_set" \
      --argjson network_none "$net_none" \
      --rawfile stdin "$stdin_tmp" \
      '{argv: $argv, has_secret_flag: ($has_secret_flag == 1), secret_val_set: ($secret_val_set == 1), network_none: ($network_none == 1), stdin: $stdin}' >> "$run_log"
    "$base" "$@" < "$stdin_tmp"
    rc=$?
    rm -f "$stdin_tmp"
    exit "$rc"
  fi
elif [[ "${1:-}" == exec ]]; then
  if [[ "$forbid_exec" == "1" ]]; then
    echo "forbidden docker exec during host-only read" >&2
    exit 99
  fi
fi

exec "$base" "$@"
"""


GIT_URL_WRAPPER = r"""#!/usr/bin/env bash
set -u
real_git="${REAL_GIT_BIN:?REAL_GIT_BIN must be set}"
declare -a c_args=()
if [[ -n "${TEST_GIT_URL_MAP:-}" && -f "${TEST_GIT_URL_MAP:-}" ]]; then
  while IFS=$'\t' read -r remote_url local_path; do
    [[ -n "$remote_url" && -n "$local_path" ]] || continue
    c_args+=(-c "url.${local_path}.insteadOf=${remote_url}")
  done < "$TEST_GIT_URL_MAP"
fi

if [[ "${1:-}" == clone ]]; then
  args=()
  for arg in "$@"; do
    if [[ "$arg" == "https://github.com/example/demo.docs.git" && -n "${DOCS_REPO_STUB_PATH:-}" ]]; then
      args+=("$DOCS_REPO_STUB_PATH")
    else
      args+=("$arg")
    fi
  done
  exec "$real_git" "${c_args[@]}" "${args[@]}"
fi

if [[ "${TEST_GIT_REJECT_SHA_FETCH:-0}" == "1" ]]; then
  is_fetch=0
  for arg in "$@"; do
    [[ "$arg" == "fetch" ]] && is_fetch=1
    if (( is_fetch )) && [[ "$arg" =~ ^[0-9a-f]{40}$ ]]; then
      echo "error: Server does not allow request for unadvertised object $arg" >&2
      exit 128
    fi
  done
fi

if [[ -n "${GIT_STUB_CALL_LOG:-}" ]]; then
  printf 'GIT_OPTIONAL_LOCKS=%s %s\n' "${GIT_OPTIONAL_LOCKS:-}" "$*" >> "$GIT_STUB_CALL_LOG"
fi
exec "$real_git" "${c_args[@]}" "$@"
"""


CURL_SPY_STUB = r"""#!/usr/bin/env bash
set -euo pipefail
log="${TEST_CURL_LOG:-}"
if [[ -n "$log" ]]; then
  printf '%s\n' "$*" >> "$log"
fi
cat >/dev/null
if [[ "${TEST_CURL_FAIL:-0}" == "1" ]]; then
  echo "curl stub: network mint disabled for test" >&2
  exit 22
fi
printf '{"token":"%s","expires_at":"%s"}\n' "${TEST_MINT_TOKEN:-ghs_SENTINEL_mr}" "${TEST_MINT_EXPIRES:-2036-01-01T00:00:00Z}"
"""


class CatalogUnitCase(unittest.TestCase):
    """Pure Core / CLI tests for SLOT_REPOS_FILE, slot_repos_catalog_json, and project --json."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="multi-repo-unit-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.state_dir = os.path.join(self.tmp, "state")

    def make_project(self, env_values=None, repos_obj=None, repos_rel=".devcontainer/repos.json", raw_repos=None):
        root = tempfile.mkdtemp(dir=self.tmp)
        vals = dict(REQUIRED_ENV)
        if env_values:
            vals.update(env_values)
        with open(os.path.join(root, "project.env"), "w", encoding="utf-8") as handle:
            handle.write("".join(f"{k}={v}\n" for k, v in vals.items()))
        if repos_obj is not None or raw_repos is not None:
            target = os.path.join(root, repos_rel)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "w", encoding="utf-8") as handle:
                if raw_repos is not None:
                    handle.write(raw_repos)
                else:
                    json.dump(repos_obj, handle)
        return root

    def run_project(self, directory, *extra_args, extra_env=None):
        env = dict(os.environ, DEV_CONTAINER_HOME=self.state_dir)
        env.pop("DOCS_REPO_URL", None)
        env.pop("DOCS_PIN_FILE", None)
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            ["bash", ENGINE, "project", directory, *extra_args],
            cwd=self.tmp,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )

    def test_lib_repos_defines_slot_repos_catalog_json(self):
        env = dict(os.environ, DEV_CONTAINER_LIB="1", DEV_CONTAINER_HOME=self.state_dir)
        res = subprocess.run(
            ["bash", "-c", '. "$1"; type slot_repos_catalog_json', "test", ENGINE],
            cwd=self.tmp,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=15,
        )
        self.assertEqual(res.returncode, 0, output_of(res))
        self.assertIn("slot_repos_catalog_json", res.stdout.decode())

    def test_project_accepts_and_prints_slot_repos_file(self):
        root = self.make_project(
            {"SLOT_REPOS_FILE": ".devcontainer/repos.json"},
            repos_obj={"version": 1, "repos": []},
        )
        res = self.run_project(root)
        self.assertEqual(res.returncode, 0, output_of(res))
        pairs = dict(line.split("=", 1) for line in res.stdout.decode().splitlines() if "=" in line)
        self.assertEqual(pairs.get("SLOT_REPOS_FILE"), ".devcontainer/repos.json")

    def test_project_rejects_invalid_slot_repos_file_paths(self):
        for bad in ("/etc/repos.json", "../repos.json", "..", "a/../b"):
            with self.subTest(path=bad):
                root = self.make_project({"SLOT_REPOS_FILE": bad})
                res = self.run_project(root)
                err = res.stderr.decode()
                self.assertNotEqual(res.returncode, 0, output_of(res))
                self.assertNotIn("Usage:", err)
                self.assertNotIn("unknown key", err)
                self.assertIn("SLOT_REPOS_FILE", err)

    def test_project_rejects_missing_or_malformed_slot_repos_file(self):
        missing_root = self.make_project({"SLOT_REPOS_FILE": ".devcontainer/missing.json"})
        res_missing = self.run_project(missing_root)
        err_missing = res_missing.stderr.decode()
        self.assertNotEqual(res_missing.returncode, 0, output_of(res_missing))
        self.assertNotIn("Usage:", err_missing)
        self.assertNotIn("unknown key", err_missing)
        self.assertRegex(err_missing, r"SLOT_REPOS_FILE|missing\.json")

        bad_json_root = self.make_project(
            {"SLOT_REPOS_FILE": ".devcontainer/repos.json"},
            raw_repos="{not valid json",
        )
        res_bad = self.run_project(bad_json_root)
        err_bad = res_bad.stderr.decode()
        self.assertNotEqual(res_bad.returncode, 0, output_of(res_bad))
        self.assertNotIn("Usage:", err_bad)
        self.assertNotIn("unknown key", err_bad)

    def test_project_json_outputs_empty_catalog_without_touching_state(self):
        root = self.make_project()
        res = self.run_project(root, "--json")
        self.assertEqual(res.returncode, 0, output_of(res))
        data = json.loads(res.stdout.decode())
        canon = os.path.realpath(root)
        expected_key = hashlib.sha256(canon.encode()).hexdigest()[:8]
        self.assertEqual(data["version"], 1)
        self.assertEqual(data["project"], "demo")
        self.assertEqual(data["project_key"], expected_key)
        self.assertEqual(data["root"], canon)
        self.assertEqual(data["default_branch"], "main")
        self.assertEqual(data["repos"], [])
        self.assertFalse(os.path.exists(self.state_dir), "project --json must not touch $STATE")

    def test_project_json_normalizes_defaults_and_valid_variations(self):
        catalog = {
            "version": 1,
            "repos": [
                {
                    "alias": "specs",
                    "path": "docs",
                    "url": "https://github.com/acme/specs.git",
                    "mode": "ro",
                    "attach": "default",
                    "pin_file": ".github/specs-pin",
                    "rw_alias": "specs-rw_2",
                },
                {
                    "alias": "specs-rw_2",
                    "path": "docs-extra",
                    "url": "https://github.com/acme/specs/",
                    "mode": "rw",
                    "attach": "optional",
                    "branch": "release/1.0",
                },
                {
                    "alias": "pub_0",
                    "path": "vendor/a",
                    "url": "https://github.com/other/public-lib.git",
                    "mode": "ro",
                    "attach": "optional",
                    "depth": 3,
                    "auth": "none",
                },
                {
                    "alias": "sibling",
                    "path": "vendor/ab",
                    "url": "https://github.com/acme/sibling.git",
                    "mode": "rw",
                    "attach": "default",
                    "depth": "full",
                },
            ],
        }
        root = self.make_project({"SLOT_REPOS_FILE": ".devcontainer/repos.json"}, repos_obj=catalog)
        res = self.run_project(root, "--json")
        self.assertEqual(res.returncode, 0, output_of(res))
        data = json.loads(res.stdout.decode())
        by_alias = {item["alias"]: item for item in data["repos"]}
        self.assertEqual(set(by_alias), {"specs", "specs-rw_2", "pub_0", "sibling"})
        self.assertEqual(by_alias["specs"]["depth"], 1)
        self.assertEqual(by_alias["specs"]["auth"], "project")
        self.assertEqual(by_alias["specs"]["pin_file"], ".github/specs-pin")
        self.assertEqual(by_alias["specs"]["rw_alias"], "specs-rw_2")
        self.assertEqual(by_alias["specs-rw_2"]["depth"], "full")
        self.assertEqual(by_alias["specs-rw_2"]["auth"], "project")
        self.assertEqual(by_alias["specs-rw_2"]["branch"], "release/1.0")
        self.assertEqual(by_alias["pub_0"]["depth"], 3)
        self.assertEqual(by_alias["pub_0"]["auth"], "none")
        self.assertEqual(by_alias["sibling"]["depth"], "full")

    def test_project_json_synthesizes_legacy_docs_repo_url_and_custom_pin(self):
        root_default_pin = self.make_project({"DOCS_REPO_URL": "https://github.com/acme/demo.docs.git"})
        res1 = self.run_project(root_default_pin, "--json")
        self.assertEqual(res1.returncode, 0, output_of(res1))
        repos1 = {r["alias"]: r for r in json.loads(res1.stdout.decode())["repos"]}
        self.assertEqual(set(repos1), {"docs", "docs-fix"})
        self.assertEqual(
            {
                "alias": repos1["docs"]["alias"],
                "path": repos1["docs"]["path"],
                "url": repos1["docs"]["url"],
                "mode": repos1["docs"]["mode"],
                "attach": repos1["docs"]["attach"],
                "pin_file": repos1["docs"]["pin_file"],
                "depth": repos1["docs"]["depth"],
                "auth": repos1["docs"]["auth"],
                "rw_alias": repos1["docs"]["rw_alias"],
                "legacy_docs": repos1["docs"]["legacy_docs"],
            },
            {
                "alias": "docs",
                "path": "docs/repo",
                "url": "https://github.com/acme/demo.docs.git",
                "mode": "ro",
                "attach": "default",
                "pin_file": ".github/docs-pin",
                "depth": 1,
                "auth": "project",
                "rw_alias": "docs-fix",
                "legacy_docs": True,
            },
        )
        self.assertEqual(
            {
                "alias": repos1["docs-fix"]["alias"],
                "path": repos1["docs-fix"]["path"],
                "url": repos1["docs-fix"]["url"],
                "mode": repos1["docs-fix"]["mode"],
                "attach": repos1["docs-fix"]["attach"],
                "depth": repos1["docs-fix"]["depth"],
                "auth": repos1["docs-fix"]["auth"],
                "legacy_docs": repos1["docs-fix"]["legacy_docs"],
            },
            {
                "alias": "docs-fix",
                "path": "docs/fix",
                "url": "https://github.com/acme/demo.docs.git",
                "mode": "rw",
                "attach": "optional",
                "depth": "full",
                "auth": "project",
                "legacy_docs": True,
            },
        )

        # Fixture demo-project has DOCS_REPO_URL + DOCS_PIN_FILE=docs/.pin
        res_demo = self.run_project(os.path.join(FIXTURES, "demo-project"), "--json")
        self.assertEqual(res_demo.returncode, 0, output_of(res_demo))
        demo_repos = {r["alias"]: r for r in json.loads(res_demo.stdout.decode())["repos"]}
        self.assertEqual(demo_repos["docs"]["pin_file"], "docs/.pin")
        self.assertTrue(demo_repos["docs"]["legacy_docs"])

    def test_project_json_placeholder_docs_url_normalizes_to_empty_repos(self):
        root = self.make_project({"DOCS_REPO_URL": PLACEHOLDER_DOCS_URL})
        res = self.run_project(root, "--json")
        self.assertEqual(res.returncode, 0, output_of(res))
        self.assertEqual(json.loads(res.stdout.decode())["repos"], [])

    def test_simultaneous_slot_repos_file_and_docs_repo_url_fails_fast(self):
        root = self.make_project(
            {
                "SLOT_REPOS_FILE": ".devcontainer/repos.json",
                "DOCS_REPO_URL": "https://github.com/acme/demo.docs.git",
            },
            repos_obj={"version": 1, "repos": []},
        )
        for args in ((), ("--json",)):
            with self.subTest(args=args):
                res = self.run_project(root, *args)
                err = res.stderr.decode()
                self.assertNotEqual(res.returncode, 0, output_of(res))
                self.assertNotIn("Usage:", err)
                self.assertNotIn("unknown key", err)
                self.assertIn("SLOT_REPOS_FILE", err)
                self.assertIn("DOCS_REPO_URL", err)

        # Placeholder DOCS_REPO_URL does NOT conflict with SLOT_REPOS_FILE
        ok_root = self.make_project(
            {
                "SLOT_REPOS_FILE": ".devcontainer/repos.json",
                "DOCS_REPO_URL": PLACEHOLDER_DOCS_URL,
            },
            repos_obj={"version": 1, "repos": []},
        )
        ok_res = self.run_project(ok_root, "--json")
        self.assertEqual(ok_res.returncode, 0, output_of(ok_res))

    def test_catalog_schema_validation_rejects_invalid_entries(self):
        base_entry = {
            "alias": "specs",
            "path": "vendor/specs",
            "url": "https://github.com/acme/specs.git",
            "mode": "ro",
            "attach": "default",
        }
        cases = [
            ("missing_version", {"repos": []}, {}, r"version"),
            ("wrong_version", {"version": 2, "repos": []}, {}, r"version"),
            ("string_version", {"version": "1", "repos": []}, {}, r"version"),
            ("missing_repos", {"version": 1}, {}, r"repos"),
            ("repos_not_array", {"version": 1, "repos": {}}, {}, r"repos"),
            ("non_object_entry", {"version": 1, "repos": ["specs"]}, {}, r"repos|object"),
            ("missing_alias", {"version": 1, "repos": [{k: v for k, v in base_entry.items() if k != "alias"}]}, {}, r"alias"),
            ("missing_path", {"version": 1, "repos": [{k: v for k, v in base_entry.items() if k != "path"}]}, {}, r"path"),
            ("missing_url", {"version": 1, "repos": [{k: v for k, v in base_entry.items() if k != "url"}]}, {}, r"url"),
            ("missing_mode", {"version": 1, "repos": [{k: v for k, v in base_entry.items() if k != "mode"}]}, {}, r"mode"),
            ("missing_attach", {"version": 1, "repos": [{k: v for k, v in base_entry.items() if k != "attach"}]}, {}, r"attach"),
            ("alias_uppercase", {"version": 1, "repos": [dict(base_entry, alias="Bad")]}, {}, r"alias|Bad"),
            ("alias_leading_dash", {"version": 1, "repos": [dict(base_entry, alias="-bad")]}, {}, r"alias|-bad"),
            ("alias_leading_underscore", {"version": 1, "repos": [dict(base_entry, alias="_bad")]}, {}, r"alias|_bad"),
            ("alias_dot", {"version": 1, "repos": [dict(base_entry, alias="a.b")]}, {}, r"alias|a\.b"),
            ("alias_reserved_root", {"version": 1, "repos": [dict(base_entry, alias="root")]}, {}, r"alias|root"),
            ("alias_reserved_dot", {"version": 1, "repos": [dict(base_entry, alias=".")]}, {}, r"alias|\."),
            ("alias_reserved_all", {"version": 1, "repos": [dict(base_entry, alias="all")]}, {}, r"alias|all"),
            ("alias_duplicate", {"version": 1, "repos": [base_entry, dict(base_entry, path="vendor/other")]}, {}, r"alias|specs|duplicate|unique"),
            ("path_leading_slash", {"version": 1, "repos": [dict(base_entry, path="/vendor/specs")]}, {}, r"path|/vendor/specs"),
            ("path_trailing_slash", {"version": 1, "repos": [dict(base_entry, path="vendor/specs/")]}, {}, r"path|vendor/specs/"),
            ("path_dot", {"version": 1, "repos": [dict(base_entry, path=".")]}, {}, r"path"),
            ("path_dotdot", {"version": 1, "repos": [dict(base_entry, path="../escape")]}, {}, r"path|\.\."),
            ("path_inner_dotdot", {"version": 1, "repos": [dict(base_entry, path="a/../b")]}, {}, r"path|\.\."),
            ("path_inner_dot", {"version": 1, "repos": [dict(base_entry, path="a/./b")]}, {}, r"path"),
            ("path_whitespace", {"version": 1, "repos": [dict(base_entry, path="vendor specs")]}, {}, r"path"),
            ("path_control", {"version": 1, "repos": [dict(base_entry, path="vendor\tspecs")]}, {}, r"path"),
            ("path_git", {"version": 1, "repos": [dict(base_entry, path=".git")]}, {}, r"\.git|path"),
            ("path_inside_git", {"version": 1, "repos": [dict(base_entry, path=".git/hooks")]}, {}, r"\.git|path"),
            ("path_default_compose_dir", {"version": 1, "repos": [dict(base_entry, path=".devcontainer")]}, {}, r"\.devcontainer|COMPOSE_DIR|path"),
            ("path_custom_compose_dir", {"version": 1, "repos": [dict(base_entry, path="deploy/compose")]}, {"COMPOSE_DIR": "deploy/compose"}, r"deploy/compose|COMPOSE_DIR|path"),
            ("path_inside_compose_dir", {"version": 1, "repos": [dict(base_entry, path="deploy/compose/sub")]}, {"COMPOSE_DIR": "deploy/compose"}, r"deploy/compose|COMPOSE_DIR|path"),
            ("path_equal", {"version": 1, "repos": [base_entry, dict(base_entry, alias="other")]}, {}, r"path|vendor/specs"),
            ("path_ancestor_then_child", {"version": 1, "repos": [dict(base_entry, path="docs"), dict(base_entry, alias="other", path="docs/repo")]}, {}, r"path|docs"),
            ("path_child_then_ancestor", {"version": 1, "repos": [dict(base_entry, path="a/b/c"), dict(base_entry, alias="other", path="a/b")]}, {}, r"path|a/b"),
            ("url_empty", {"version": 1, "repos": [dict(base_entry, url="")]}, {}, r"url"),
            ("url_whitespace", {"version": 1, "repos": [dict(base_entry, url="https://github.com/acme/a b.git")]}, {}, r"url"),
            ("url_control", {"version": 1, "repos": [dict(base_entry, url="https://github.com/acme/a\nb.git")]}, {}, r"url"),
            ("url_leading_dash", {"version": 1, "repos": [dict(base_entry, url="--upload-pack=x")]}, {}, r"url|--upload-pack"),
            ("mode_invalid", {"version": 1, "repos": [dict(base_entry, mode="read")]}, {}, r"mode|read"),
            ("attach_invalid", {"version": 1, "repos": [dict(base_entry, attach="always")]}, {}, r"attach|always"),
            ("branch_invalid", {"version": 1, "repos": [dict(base_entry, branch="--upload-pack=x")]}, {}, r"branch"),
            ("branch_dotdot", {"version": 1, "repos": [dict(base_entry, branch="a..b")]}, {}, r"branch"),
            ("branch_and_pin_file", {"version": 1, "repos": [dict(base_entry, branch="main", pin_file=".github/pin")]}, {}, r"branch|pin_file"),
            ("pin_file_on_rw", {"version": 1, "repos": [dict(base_entry, mode="rw", pin_file=".github/pin")]}, {}, r"pin_file|rw"),
            ("pin_file_invalid_path", {"version": 1, "repos": [dict(base_entry, pin_file="../pin")]}, {}, r"pin_file"),
            ("depth_zero", {"version": 1, "repos": [dict(base_entry, depth=0)]}, {}, r"depth"),
            ("depth_negative", {"version": 1, "repos": [dict(base_entry, depth=-2)]}, {}, r"depth"),
            ("depth_bad_string", {"version": 1, "repos": [dict(base_entry, depth="shallow")]}, {}, r"depth"),
            ("depth_bool", {"version": 1, "repos": [dict(base_entry, depth=True)]}, {}, r"depth"),
            ("auth_none_on_rw", {"version": 1, "repos": [dict(base_entry, mode="rw", auth="none")]}, {}, r"auth|none|rw"),
            ("auth_invalid", {"version": 1, "repos": [dict(base_entry, auth="token")]}, {}, r"auth|token"),
            ("rw_alias_unknown", {"version": 1, "repos": [dict(base_entry, rw_alias="missing")]}, {}, r"rw_alias|missing"),
            ("rw_alias_points_to_ro", {"version": 1, "repos": [dict(base_entry, rw_alias="other"), dict(base_entry, alias="other", path="vendor/other")]}, {}, r"rw_alias|other|ro|rw"),
            ("rw_alias_url_mismatch", {"version": 1, "repos": [dict(base_entry, rw_alias="other"), dict(base_entry, alias="other", path="vendor/other", mode="rw", url="https://github.com/acme/different.git")]}, {}, r"rw_alias|other|url"),
        ]
        for label, catalog, extra_env, err_rx in cases:
            with self.subTest(case=label):
                env_vals = {"SLOT_REPOS_FILE": ".devcontainer/repos.json"}
                env_vals.update(extra_env)
                root = self.make_project(env_vals, repos_obj=catalog)
                for args in ((), ("--json",)):
                    res = self.run_project(root, *args)
                    err = res.stderr.decode(errors="replace")
                    self.assertNotEqual(res.returncode, 0, f"{label} ({args}) unexpectedly passed:\n{output_of(res)}")
                    self.assertNotIn("Usage:", err, f"{label} ({args}): {err}")
                    self.assertNotIn("unknown key", err, f"{label} ({args}): {err}")
                    self.assertRegex(err, f"(?i){err_rx}", f"{label} ({args}): {err}")


class MultiRepoIntegrationBase(DevContainerTestCase):
    """Harness-backed integration helpers with local bare repos and spy wrappers (zero network)."""

    def setUp(self):
        super().setUp()
        self.docker_run_log = os.path.join(self.harness.tmp, "docker-runs.jsonl")
        self.compose_verbs_log = os.path.join(self.harness.tmp, "compose-verbs.log")
        self.curl_log = os.path.join(self.harness.tmp, "curl.log")
        self.url_map_file = os.path.join(self.harness.tmp, "git-url-map.tsv")
        self.url_map = {}

        self.harness._write_stub("docker-base", DOCKER_STUB)
        self.harness._write_stub("docker", DOCKER_SPY_WRAPPER)
        self.harness._write_stub("git-base", GIT_STUB)
        self.harness._write_stub("git", GIT_URL_WRAPPER)
        self.harness._write_stub("curl", CURL_SPY_STUB)

    def real_git(self, repo, *args):
        return subprocess.run(
            [
                self.harness.real_git_bin,
                "-C",
                repo,
                "-c",
                "commit.gpgsign=false",
                "-c",
                "user.name=fixture",
                "-c",
                "user.email=fixture@example.com",
                *args,
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ).stdout.decode().strip()

    def make_bare_repo(self, name, *, commits=("first", "second"), extra_branch=None, github_url=None):
        bare = os.path.join(self.harness.tmp, "remotes", f"{name}.git")
        work = os.path.join(self.harness.tmp, "work", name)
        os.makedirs(bare, exist_ok=True)
        subprocess.run([self.harness.real_git_bin, "init", "-q", "--bare", bare], check=True)
        subprocess.run([self.harness.real_git_bin, "-C", bare, "symbolic-ref", "HEAD", "refs/heads/main"], check=True)
        subprocess.run([self.harness.real_git_bin, "clone", "-q", bare, work], check=True)
        self.real_git(work, "checkout", "-q", "-B", "main")
        shas = []
        for idx, msg in enumerate(commits):
            fname = f"{name}-{idx}.txt"
            with open(os.path.join(work, fname), "w", encoding="utf-8") as handle:
                handle.write(f"{msg}\n")
            self.real_git(work, "add", fname)
            self.real_git(work, "commit", "-q", "-m", msg)
            shas.append(self.real_git(work, "rev-parse", "HEAD"))
        self.real_git(work, "push", "-q", "origin", "main")
        branch_sha = None
        if extra_branch:
            self.real_git(work, "checkout", "-q", "-b", extra_branch)
            with open(os.path.join(work, "branch-only.txt"), "w", encoding="utf-8") as handle:
                handle.write(f"on {extra_branch}\n")
            self.real_git(work, "add", "branch-only.txt")
            self.real_git(work, "commit", "-q", "-m", f"branch {extra_branch}")
            branch_sha = self.real_git(work, "rev-parse", "HEAD")
            self.real_git(work, "push", "-q", "origin", extra_branch)
        if github_url:
            self.map_url(github_url, bare)
            return github_url, {"bare": bare, "shas": shas, "branch_sha": branch_sha}
        return bare, {"bare": bare, "shas": shas, "branch_sha": branch_sha}

    def map_url(self, remote_url, local_bare):
        self.url_map[remote_url] = local_bare
        with open(self.url_map_file, "w", encoding="utf-8") as handle:
            for k, v in self.url_map.items():
                handle.write(f"{k}\t{v}\n")

    def build_project(
        self,
        *,
        repos_catalog=None,
        project_env_extra=None,
        parent_env=IDENTITY_ENV,
        origin_url=None,
        pin_files=None,
        tracked_files=None,
    ):
        extra = dict(project_env_extra or {})
        if repos_catalog is not None:
            extra.setdefault("SLOT_REPOS_FILE", ".devcontainer/repos.json")
        self.harness.build_fixture_repo(
            with_parent_env=parent_env,
            project_env=project_env_text(DEFAULT_BRANCH="main", **extra),
        )
        repo = self.harness.fixture_repo
        self.real_git(repo, "branch", "-M", "main")
        if repos_catalog is not None:
            repos_rel = extra["SLOT_REPOS_FILE"]
            repos_path = os.path.join(repo, repos_rel)
            os.makedirs(os.path.dirname(repos_path), exist_ok=True)
            with open(repos_path, "w", encoding="utf-8") as handle:
                json.dump(repos_catalog, handle)
            self.real_git(repo, "add", repos_rel)
        if pin_files:
            for rel, content in pin_files.items():
                full = os.path.join(repo, rel)
                os.makedirs(os.path.dirname(full), exist_ok=True)
                with open(full, "w", encoding="utf-8") as handle:
                    handle.write(content)
                self.real_git(repo, "add", rel)
        if tracked_files:
            for rel, content in tracked_files.items():
                full = os.path.join(repo, rel)
                os.makedirs(os.path.dirname(full), exist_ok=True)
                with open(full, "w", encoding="utf-8") as handle:
                    handle.write(content)
                self.real_git(repo, "add", rel)
        if repos_catalog is not None or pin_files or tracked_files:
            self.real_git(repo, "commit", "-q", "-m", "catalog setup")

        bare_origin = os.path.join(self.harness.tmp, "primary-origin.git")
        subprocess.run([self.harness.real_git_bin, "init", "-q", "--bare", bare_origin], check=True)
        subprocess.run([self.harness.real_git_bin, "-C", bare_origin, "symbolic-ref", "HEAD", "refs/heads/main"], check=True)
        self.real_git(repo, "remote", "set-url", "origin", bare_origin)
        self.real_git(repo, "push", "-q", "origin", "main")
        self.real_git(repo, "fetch", "-q", "origin")
        if origin_url:
            self.map_url(origin_url, bare_origin)
            self.real_git(repo, "remote", "set-url", "origin", origin_url)

    def run_engine(self, *args, unset_docs=True, extra_env=None):
        overrides = {
            "TEST_DOCKER_RUN_LOG": self.docker_run_log,
            "TEST_COMPOSE_VERBS_LOG": self.compose_verbs_log,
            "TEST_CURL_LOG": self.curl_log,
            "TEST_GIT_URL_MAP": self.url_map_file,
        }
        if extra_env:
            overrides.update(extra_env)
        real_env = self.harness._env
        with mock.patch.object(
            self.harness,
            "_env",
            side_effect=lambda port_overrides=None: self._patched_env(
                real_env(port_overrides), overrides, unset_docs=unset_docs
            ),
        ):
            return self.harness.run(*args)

    @staticmethod
    def _patched_env(env, overrides, *, unset_docs):
        if unset_docs:
            env.pop("DOCS_REPO_URL", None)
        env.update(overrides)
        return env

    def docker_runs(self):
        if not os.path.isfile(self.docker_run_log):
            return []
        with open(self.docker_run_log, encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]

    def compose_verbs(self):
        if not os.path.isfile(self.compose_verbs_log):
            return []
        with open(self.compose_verbs_log, encoding="utf-8") as handle:
            return [line.strip() for line in handle if line.strip()]

    def curl_calls(self):
        if not os.path.isfile(self.curl_log):
            return []
        with open(self.curl_log, encoding="utf-8") as handle:
            return [line.strip() for line in handle if line.strip()]

    def clear_logs(self):
        for p in (self.docker_run_log, self.compose_verbs_log, self.curl_log):
            if os.path.exists(p):
                os.remove(p)

    def repos_state_path(self, name):
        return os.path.join(self.harness.state_dir, "repos", f"{PROJECT_PREFIX}-{name}.json")


class TestAcquireAndAttachCatalogRepos(MultiRepoIntegrationBase):
    """acquire --repo / --all-repos, attach, exclude handling, and tracked-path fail-closed checks."""

    def _standard_catalog(self):
        specs_url, specs_info = self.make_bare_repo("specs", commits=("s1", "s2", "s3"))
        notes_url, notes_info = self.make_bare_repo("notes", commits=("n1", "n2"))
        tools_url, tools_info = self.make_bare_repo("tools", commits=("t1", "t2"), extra_branch="v2")
        pub_url, pub_info = self.make_bare_repo("pub", commits=("p1", "p2"))
        catalog = {
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
        }
        return catalog, {"specs": specs_info, "notes": notes_info, "tools": tools_info, "pub": pub_info}

    def test_acquire_rejects_unknown_or_invalid_repo_aliases_before_locking(self):
        catalog, _ = self._standard_catalog()
        self.build_project(repos_catalog=catalog)
        bad_invocations = [
            ["--repo", "nonexistent"],
            ["--repo", "root"],
            ["--repo", "all"],
            ["--repo", "tools,nonexistent"],
            ["--repo", ""],
            ["--repo", "tools,,pub"],
        ]
        for flags in bad_invocations:
            with self.subTest(flags=flags):
                res = self.run_engine("acquire", "--for", "worker", *flags)
                err = res.stderr.decode(errors="replace")
                self.assertNotEqual(res.returncode, 0, output_of(res))
                self.assertNotIn("Usage:", err)
                self.assertNotIn("unknown flag", err)
                self.assertNotIn("unknown key", err)
                self.assertRegex(err, r"(?i)repo|alias|nonexistent|root|all")
                self.assertFalse(os.path.exists(os.path.join(self.harness.state_dir, "lock")))
                leases_dir = os.path.join(self.harness.state_dir, "leases")
                self.assertEqual(os.listdir(leases_dir) if os.path.isdir(leases_dir) else [], [])
                self.assertEqual(self.harness.volume_names(), [])

    def test_acquire_with_repo_on_empty_catalog_rejects_before_locking(self):
        self.build_project(repos_catalog={"version": 1, "repos": []})
        res_empty = self.run_engine("acquire", "--for", "worker", "--repo", "foo")
        err_empty = res_empty.stderr.decode(errors="replace")
        self.assertNotEqual(res_empty.returncode, 0, output_of(res_empty))
        self.assertNotIn("Usage:", err_empty)
        self.assertNotIn("unknown flag", err_empty)
        self.assertIn("foo", err_empty)
        self.assertEqual(self.harness.volume_names(), [])

    def test_acquire_default_clones_defaults_only_pins_ro_and_excludes_all_catalog_paths(self):
        catalog, info = self._standard_catalog()
        pinned_sha = info["specs"]["shas"][0]
        self.build_project(
            repos_catalog=catalog,
            pin_files={".github/specs-pin": f"# pinned specs\n{pinned_sha}\n"},
        )
        res = self.run_engine("acquire", "--for", "worker")
        self.assertEqual(res.returncode, 0, output_of(res))
        name = self.harness.acquired_slot_name(res)
        slot_dir = self.harness.slot_dir(name)

        specs_dir = os.path.join(slot_dir, "deps", "specs")
        notes_dir = os.path.join(slot_dir, "deps", "notes")
        self.assertTrue(os.path.isdir(os.path.join(specs_dir, ".git")))
        self.assertEqual(self.real_git(specs_dir, "rev-parse", "HEAD"), pinned_sha)
        self.assertNotEqual(pinned_sha, info["specs"]["shas"][-1])
        self.assertTrue(os.path.isdir(os.path.join(notes_dir, ".git")))
        self.assertEqual(self.real_git(notes_dir, "rev-parse", "--is-shallow-repository"), "false")

        self.assertFalse(os.path.exists(os.path.join(slot_dir, "extras", "tools")))
        self.assertFalse(os.path.exists(os.path.join(slot_dir, "extras", "pub")))

        exclude_file = os.path.join(slot_dir, ".git", "info", "exclude")
        with open(exclude_file, encoding="utf-8") as handle:
            excludes = handle.read().splitlines()
        for rel in ("deps/specs", "deps/notes", "extras/tools", "extras/pub"):
            self.assertTrue(
                any(line.strip("/") == rel for line in excludes),
                f"{rel} missing from .git/info/exclude: {excludes}",
            )
        os.makedirs(os.path.join(slot_dir, "extras", "tools"), exist_ok=True)
        with open(os.path.join(slot_dir, "extras", "tools", "probe.txt"), "w", encoding="utf-8") as handle:
            handle.write("probe\n")
        self.assertEqual(self.real_git(slot_dir, "status", "--porcelain"), "")

    def test_acquire_repo_flags_and_all_repos_and_default_idempotency(self):
        catalog, info = self._standard_catalog()
        pinned_sha = info["specs"]["shas"][0]
        self.build_project(
            repos_catalog=catalog,
            pin_files={".github/specs-pin": f"{pinned_sha}\n"},
        )
        res1 = self.run_engine(
            "acquire",
            "--for",
            "worker-a",
            "--repo",
            "specs,tools",
            "--repo",
            "tools",
        )
        self.assertEqual(res1.returncode, 0, output_of(res1))
        slot1 = self.harness.acquired_slot_name(res1)
        slot1_dir = self.harness.slot_dir(slot1)
        self.assertTrue(os.path.isdir(os.path.join(slot1_dir, "deps", "specs", ".git")))
        self.assertTrue(os.path.isdir(os.path.join(slot1_dir, "deps", "notes", ".git")))
        tools_dir = os.path.join(slot1_dir, "extras", "tools")
        self.assertTrue(os.path.isdir(os.path.join(tools_dir, ".git")))
        self.assertEqual(self.real_git(tools_dir, "symbolic-ref", "--short", "HEAD"), "v2")
        self.assertEqual(self.real_git(tools_dir, "rev-parse", "HEAD"), info["tools"]["branch_sha"])
        self.assertFalse(os.path.exists(os.path.join(slot1_dir, "extras", "pub")))

        res2 = self.run_engine("acquire", "--for", "worker-b", "--all-repos")
        self.assertEqual(res2.returncode, 0, output_of(res2))
        slot2 = self.harness.acquired_slot_name(res2)
        slot2_dir = self.harness.slot_dir(slot2)
        for rel in ("deps/specs", "deps/notes", "extras/tools", "extras/pub"):
            self.assertTrue(os.path.isdir(os.path.join(slot2_dir, rel, ".git")), rel)
        self.assertEqual(
            self.real_git(os.path.join(slot2_dir, "extras", "pub"), "rev-parse", "--is-shallow-repository"),
            "true",
        )

    def test_create_slot_fails_closed_if_catalog_path_is_tracked_in_primary_repo(self):
        catalog, _ = self._standard_catalog()
        self.build_project(
            repos_catalog=catalog,
            tracked_files={"extras/tools/README.md": "already tracked\n"},
        )
        res = self.run_engine("acquire", "--for", "worker")
        err = output_of(res)
        self.assertNotEqual(res.returncode, 0, err)
        self.assertIn("extras/tools", err)
        self.assertEqual(self.harness.volume_names(), [], "failed acquire left a slot volume")
        leases_dir = os.path.join(self.harness.state_dir, "leases")
        self.assertEqual(os.listdir(leases_dir) if os.path.isdir(leases_dir) else [], [])

    def test_attach_clones_optional_repo_into_live_slot_without_restart_and_is_idempotent(self):
        catalog, info = self._standard_catalog()
        pinned_sha = info["specs"]["shas"][0]
        self.build_project(
            repos_catalog=catalog,
            pin_files={".github/specs-pin": f"{pinned_sha}\n"},
        )
        acq = self.run_engine("acquire", "--for", "worker")
        self.assertEqual(acq.returncode, 0, output_of(acq))
        name = self.harness.acquired_slot_name(acq)
        slot_dir = self.harness.slot_dir(name)

        for bad_args, needle in (
            (["attach", name, "--repo", "unknown"], r"unknown|alias"),
            (["attach", name], r"--repo"),
            (["attach", "slot-99", "--repo", "tools"], r"slot|no slot"),
            (["attach", name, "--repo", "tools", "--for", "other-holder"], r"other-holder|worker|holder"),
        ):
            with self.subTest(bad=bad_args):
                bad = self.run_engine(*bad_args)
                self.assertNotEqual(bad.returncode, 0, output_of(bad))
                self.assertNotIn("Usage:", bad.stderr.decode(errors="replace"))
                self.assertRegex(output_of(bad), f"(?i){needle}")
                self.assertFalse(os.path.exists(os.path.join(slot_dir, "extras", "tools")))

        self.clear_logs()
        att = self.run_engine("attach", name, "--repo", "tools", "--repo", "pub", "--for", "worker")
        self.assertEqual(att.returncode, 0, output_of(att))
        verbs = self.compose_verbs()
        for forbidden in ("up", "down", "stop", "restart"):
            self.assertNotIn(forbidden, verbs, f"attach triggered container {forbidden}: {verbs}")
        self.assertTrue(self.docker_runs(), "attach did not invoke slot_helper_run_online")

        tools_dir = os.path.join(slot_dir, "extras", "tools")
        pub_dir = os.path.join(slot_dir, "extras", "pub")
        self.assertTrue(os.path.isdir(os.path.join(tools_dir, ".git")))
        self.assertTrue(os.path.isdir(os.path.join(pub_dir, ".git")))
        self.assertEqual(self.real_git(tools_dir, "symbolic-ref", "--short", "HEAD"), "v2")
        self.assertEqual(
            self.real_git(pub_dir, "config", "--local", "remote.origin.pushurl"),
            "read-only://slot-repo-is-read-only--pub",
        )

        att_again = self.run_engine("attach", name, "--repo", "tools,specs")
        self.assertEqual(att_again.returncode, 0, output_of(att_again))
        with open(self.repos_state_path(name), encoding="utf-8") as handle:
            recorded = json.load(handle)
        aliases = [entry["alias"] for entry in recorded]
        self.assertEqual(len(aliases), len(set(aliases)), f"duplicate aliases in state: {recorded}")
        self.assertEqual(set(aliases), {"specs", "notes", "tools", "pub"})

        self.harness.free_slot(name)
        unleased = self.run_engine("attach", name, "--repo", "tools")
        self.assertNotEqual(unleased.returncode, 0, output_of(unleased))
        self.assertNotIn("Usage:", unleased.stderr.decode(errors="replace"))

    def test_acquire_pinned_shallow_repo_falls_back_when_server_rejects_unadvertised_sha(self):
        specs_url, specs_info = self.make_bare_repo("specs", commits=("s1", "s2", "s3"))
        alt_url, alt_info = self.make_bare_repo("alt", commits=("a1", "a2"), extra_branch="side")
        pinned_main = specs_info["shas"][0]
        pinned_branch = alt_info["branch_sha"]
        catalog = {
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
                    "alias": "alt",
                    "path": "deps/alt",
                    "url": alt_url,
                    "mode": "ro",
                    "attach": "default",
                    "pin_file": ".github/alt-pin",
                    "auth": "none",
                    "depth": 1,
                },
            ],
        }
        self.build_project(
            repos_catalog=catalog,
            pin_files={
                ".github/specs-pin": f"{pinned_main}\n",
                ".github/alt-pin": f"{pinned_branch}\n",
            },
        )
        res = self.run_engine("acquire", "--for", "worker", extra_env={"TEST_GIT_REJECT_SHA_FETCH": "1"})
        out = output_of(res)
        self.assertEqual(res.returncode, 0, out)
        self.assertNotIn("unadvertised object", out)
        self.assertNotIn("could not fetch pin", out)
        name = self.harness.acquired_slot_name(res)
        slot_dir = self.harness.slot_dir(name)
        self.assertEqual(self.real_git(os.path.join(slot_dir, "deps", "specs"), "rev-parse", "HEAD"), pinned_main)
        self.assertEqual(self.real_git(os.path.join(slot_dir, "deps", "alt"), "rev-parse", "HEAD"), pinned_branch)

        # Also exercise host-side directory clone (`clone_repos_in_dir`) & `sync_docs_repo_to_pin`
        dir_clone = os.path.join(self.harness.tmp, "dir-slot")
        subprocess.run([self.harness.real_git_bin, "clone", "-q", self.harness.fixture_repo, dir_clone], check=True)
        self.real_git(dir_clone, "remote", "set-url", "origin", self.real_git(self.harness.fixture_repo, "remote", "get-url", "origin"))
        self.real_git(dir_clone, "fetch", "-q", "origin")
        env = self.harness._env(None)
        env.update(
            {
                "DEV_CONTAINER_LIB": "1",
                "DEV_CONTAINER_CALLER_PWD": self.harness.fixture_repo,
                "TEST_GIT_REJECT_SHA_FETCH": "1",
                "TEST_GIT_URL_MAP": self.url_map_file,
            }
        )
        env.pop("DOCS_REPO_URL", None)
        lib_res = subprocess.run(
            [
                "bash",
                "-c",
                '. "$1"; clone_repos_in_dir "$2" "$3"',
                "test",
                os.path.join(self.harness.fixture_repo, SCRIPT_REL),
                dir_clone,
                json.dumps(catalog["repos"]),
            ],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )
        lib_out = output_of(lib_res)
        self.assertEqual(lib_res.returncode, 0, lib_out)
        self.assertNotIn("unadvertised object", lib_out)
        self.assertNotIn("could not fetch pin", lib_out)
        self.assertEqual(self.real_git(os.path.join(dir_clone, "deps", "specs"), "rev-parse", "HEAD"), pinned_main)
        self.assertEqual(self.real_git(os.path.join(dir_clone, "deps", "alt"), "rev-parse", "HEAD"), pinned_branch)

        docs_clone = os.path.join(dir_clone, "docs", "repo")
        os.makedirs(os.path.dirname(docs_clone), exist_ok=True)
        subprocess.run(
            [self.harness.real_git_bin, "clone", "-q", "--depth", "1", "file://" + specs_info["bare"], docs_clone],
            check=True,
        )
        self.assertEqual(self.real_git(docs_clone, "rev-parse", "HEAD"), specs_info["shas"][-1])
        sync_env = dict(env, DOCS_PIN_FILE=".github/specs-pin")
        sync_res = subprocess.run(
            [
                "bash",
                "-c",
                '. "$1"; sync_docs_repo_to_pin "$2"',
                "test",
                os.path.join(self.harness.fixture_repo, SCRIPT_REL),
                dir_clone,
            ],
            env=sync_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )
        sync_out = output_of(sync_res)
        self.assertEqual(sync_res.returncode, 0, sync_out)
        self.assertNotIn("unadvertised object", sync_out)
        self.assertEqual(self.real_git(docs_clone, "rev-parse", "HEAD"), pinned_main)

    def test_acquire_after_release_clones_newly_requested_optional_repo(self):
        catalog, info = self._standard_catalog()
        pinned_sha = info["specs"]["shas"][0]
        self.build_project(
            repos_catalog=catalog,
            pin_files={".github/specs-pin": f"{pinned_sha}\n"},
        )
        acq1 = self.run_engine("acquire", "--for", "worker-1")
        self.assertEqual(acq1.returncode, 0, output_of(acq1))
        name1 = self.harness.acquired_slot_name(acq1)
        rel = self.run_engine("release", name1, "--for", "worker-1")
        self.assertEqual(rel.returncode, 0, output_of(rel))

        acq2 = self.run_engine("acquire", "--for", "worker-2", "--repo", "tools")
        self.assertEqual(acq2.returncode, 0, output_of(acq2))
        name2 = self.harness.acquired_slot_name(acq2)
        slot_dir = self.harness.slot_dir(name2)
        self.assertTrue(os.path.isdir(os.path.join(slot_dir, "extras", "tools", ".git")))
        self.assertEqual(self.real_git(os.path.join(slot_dir, "extras", "tools"), "symbolic-ref", "--short", "HEAD"), "v2")
        with open(self.harness.lease_path(name2), encoding="utf-8") as handle:
            aliases = {r["alias"] for r in json.load(handle).get("repos", [])}
        self.assertEqual(aliases, {"specs", "notes", "tools"})


class TestBatchedHelperCloningAndCredentials(MultiRepoIntegrationBase):
    """Batching slot_helper_run_online by auth mode and omitting FORGE_HOST_SECRET for auth=none."""

    def _app_cred_env(self):
        return (
            "FORGE_APP_ID=1234567\n"
            "FORGE_APP_INSTALLATION_ID=7654321\n"
            "FORGE_APP_SLUG=demo-agent\n"
            "FORGE_APP_BOT_ID=424242\n"
            f"FORGE_APP_KEY_FILE={KEY_FILE}\n"
            "FORGE_APP_REPOS=primary priv1 priv2 optpriv\n"
        )

    def _place_key_and_env(self, parent_env):
        key = os.path.join(self.harness.tmp, "keys", "app.pem")
        os.makedirs(os.path.dirname(key), exist_ok=True)
        shutil.copyfile(KEY_FILE, key)
        os.chmod(key, 0o600)
        return parent_env.replace(KEY_FILE, key)

    def test_acquire_batches_helper_clones_by_auth_mode_and_omits_secret_for_auth_none(self):
        priv1, _ = self.make_bare_repo("priv1", github_url="https://github.com/acme/priv1.git")
        priv2, _ = self.make_bare_repo("priv2", github_url="https://github.com/acme/priv2.git")
        pub1, _ = self.make_bare_repo("pub1", github_url="https://github.com/other/pub1.git")
        pub2, _ = self.make_bare_repo("pub2", github_url="https://github.com/other/pub2.git")
        catalog = {
            "version": 1,
            "repos": [
                {"alias": "priv1", "path": "r/priv1", "url": priv1, "mode": "ro", "attach": "default", "auth": "project"},
                {"alias": "priv2", "path": "r/priv2", "url": priv2, "mode": "rw", "attach": "default", "auth": "project"},
                {"alias": "pub1", "path": "r/pub1", "url": pub1, "mode": "ro", "attach": "default", "auth": "none"},
                {"alias": "pub2", "path": "r/pub2", "url": pub2, "mode": "ro", "attach": "default", "auth": "none"},
            ],
        }
        self.build_project(
            repos_catalog=catalog,
            project_env_extra={"FORGE_AUTH": "token", "FORGE_AUTH_FILE": ".devcontainer/.env"},
            parent_env=IDENTITY_ENV + "GH_TOKEN=ghs_SENTINEL_mr\n",
            origin_url="https://github.com/acme/primary.git",
        )
        res = self.run_engine("acquire", "--for", "batch")
        self.assertEqual(res.returncode, 0, output_of(res))
        name = self.harness.acquired_slot_name(res)
        slot_dir = self.harness.slot_dir(name)
        for rel in ("r/priv1", "r/priv2", "r/pub1", "r/pub2"):
            self.assertTrue(os.path.isdir(os.path.join(slot_dir, rel, ".git")), rel)

        runs = [r for r in self.docker_runs() if not r["network_none"]]
        self.assertEqual(len(runs), 2, f"expected 2 batched online helper runs (project + none), got {len(runs)}: {runs}")
        secret_runs = [r for r in runs if r["has_secret_flag"]]
        public_runs = [r for r in runs if not r["has_secret_flag"]]
        self.assertEqual(len(secret_runs), 1, runs)
        self.assertEqual(len(public_runs), 1, runs)
        self.assertFalse(public_runs[0]["secret_val_set"])

    def test_acquire_with_only_auth_none_repos_in_app_mode_never_mints_token_or_passes_secret(self):
        pub1, _ = self.make_bare_repo("pub1", github_url="https://github.com/other/pub1.git")
        pub2, _ = self.make_bare_repo("pub2", github_url="https://github.com/other/pub2.git")
        optpriv, _ = self.make_bare_repo("optpriv", github_url="https://github.com/acme/optpriv.git")
        catalog = {
            "version": 1,
            "repos": [
                {"alias": "pub1", "path": "p/pub1", "url": pub1, "mode": "ro", "attach": "default", "auth": "none"},
                {"alias": "pub2", "path": "p/pub2", "url": pub2, "mode": "ro", "attach": "default", "auth": "none"},
                {"alias": "optpriv", "path": "p/optpriv", "url": optpriv, "mode": "rw", "attach": "optional", "auth": "project"},
            ],
        }
        parent = self._place_key_and_env(self._app_cred_env())
        self.build_project(
            repos_catalog=catalog,
            project_env_extra={"FORGE_AUTH": "app", "FORGE_AUTH_FILE": ".devcontainer/.env"},
            parent_env=parent,
            origin_url="https://github.com/acme/primary.git",
        )
        res = self.run_engine("acquire", "--for", "pub-only", extra_env={"TEST_CURL_FAIL": "1"})
        self.assertEqual(res.returncode, 0, output_of(res))
        self.assertEqual(self.curl_calls(), [], "App token was minted despite cloning only auth=none repos")
        runs = [r for r in self.docker_runs() if not r["network_none"]]
        self.assertEqual(len(runs), 1, runs)
        self.assertFalse(runs[0]["has_secret_flag"])
        self.assertFalse(runs[0]["secret_val_set"])

    def test_attach_batches_same_auth_and_omits_secret_for_auth_none(self):
        optpub1, _ = self.make_bare_repo("optpub1", github_url="https://github.com/other/optpub1.git")
        optpub2, _ = self.make_bare_repo("optpub2", github_url="https://github.com/other/optpub2.git")
        catalog = {
            "version": 1,
            "repos": [
                {"alias": "optpub1", "path": "p/one", "url": optpub1, "mode": "ro", "attach": "optional", "auth": "none"},
                {"alias": "optpub2", "path": "p/two", "url": optpub2, "mode": "ro", "attach": "optional", "auth": "none"},
            ],
        }
        parent = self._place_key_and_env(self._app_cred_env())
        self.build_project(
            repos_catalog=catalog,
            project_env_extra={"FORGE_AUTH": "app", "FORGE_AUTH_FILE": ".devcontainer/.env"},
            parent_env=parent,
            origin_url="https://github.com/acme/primary.git",
        )
        acq = self.run_engine("acquire", "--for", "att-batch")
        self.assertEqual(acq.returncode, 0, output_of(acq))
        name = self.harness.acquired_slot_name(acq)

        self.clear_logs()
        att = self.run_engine(
            "attach", name, "--repo", "optpub1,optpub2", "--for", "att-batch",
            extra_env={"TEST_CURL_FAIL": "1"},
        )
        self.assertEqual(att.returncode, 0, output_of(att))
        self.assertEqual(self.curl_calls(), [])
        runs = [r for r in self.docker_runs() if not r["network_none"]]
        self.assertEqual(len(runs), 1, runs)
        self.assertFalse(runs[0]["has_secret_flag"])
        self.assertFalse(runs[0]["secret_val_set"])


class TestHostRepoStateLeaseAndStatusJson(MultiRepoIntegrationBase):
    """$STATE/repos/$sid.json, $STATE/leases/$sid.json, status --json (zero docker exec), teardown cleanup."""

    def test_acquire_attach_status_and_release_lifecycle(self):
        specs_url, _ = self.make_bare_repo("specs", github_url="https://github.com/acme/specs.git")
        tools_url, _ = self.make_bare_repo("tools", github_url="https://github.com/acme/tools.git")
        catalog = {
            "version": 1,
            "repos": [
                {"alias": "specs", "path": "deps/specs", "url": specs_url, "mode": "ro", "attach": "default"},
                {"alias": "tools", "path": "extras/tools", "url": tools_url, "mode": "rw", "attach": "optional"},
            ],
        }
        self.build_project(repos_catalog=catalog, origin_url="https://github.com/acme/app.git")
        acq = self.run_engine("acquire", "--for", "state-test")
        self.assertEqual(acq.returncode, 0, output_of(acq))
        name = self.harness.acquired_slot_name(acq)

        expected_specs = {
            "alias": "specs",
            "path": "deps/specs",
            "slug": "acme/specs",
            "mode": "ro",
            "attach": "default",
        }
        expected_tools = {
            "alias": "tools",
            "path": "extras/tools",
            "slug": "acme/tools",
            "mode": "rw",
            "attach": "optional",
        }

        repos_file = self.repos_state_path(name)
        self.assertTrue(os.path.isfile(repos_file), f"missing {repos_file}")
        with open(repos_file, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle), [expected_specs])

        with open(self.harness.lease_path(name), encoding="utf-8") as handle:
            lease_data = json.load(handle)
        self.assertEqual(lease_data.get("repos"), [expected_specs])

        st1 = self.run_engine("status", "--json", extra_env={"TEST_FORBID_DOCKER_EXEC": "1"})
        self.assertEqual(st1.returncode, 0, output_of(st1))
        st1_data = json.loads(st1.stdout.decode())
        self.assertEqual(st1_data["slots"][0].get("repos"), [expected_specs])

        att = self.run_engine("attach", name, "--repo", "tools", "--for", "state-test")
        self.assertEqual(att.returncode, 0, output_of(att))
        with open(repos_file, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle), [expected_specs, expected_tools])
        with open(self.harness.lease_path(name), encoding="utf-8") as handle:
            self.assertEqual(json.load(handle).get("repos"), [expected_specs, expected_tools])

        st2 = self.run_engine("status", "--json", extra_env={"TEST_FORBID_DOCKER_EXEC": "1"})
        self.assertEqual(st2.returncode, 0, output_of(st2))
        self.assertEqual(json.loads(st2.stdout.decode())["slots"][0].get("repos"), [expected_specs, expected_tools])

        rel = self.run_engine("release", name, "--for", "state-test")
        self.assertEqual(rel.returncode, 0, output_of(rel))
        self.assertFalse(os.path.exists(repos_file), f"{repos_file} survived teardown_dir")

    def test_empty_catalog_records_empty_repos_list_in_lease_and_status_json(self):
        self.build_project(repos_catalog={"version": 1, "repos": []})
        acq = self.run_engine("acquire", "--for", "empty-cat")
        self.assertEqual(acq.returncode, 0, output_of(acq))
        name = self.harness.acquired_slot_name(acq)

        with open(self.harness.lease_path(name), encoding="utf-8") as handle:
            self.assertEqual(json.load(handle).get("repos"), [])
        st = self.run_engine("status", "--json", extra_env={"TEST_FORBID_DOCKER_EXEC": "1"})
        self.assertEqual(st.returncode, 0, output_of(st))
        self.assertEqual(json.loads(st.stdout.decode())["slots"][0].get("repos"), [])


class TestMultiRepoIdentityAndReadonlyGuards(MultiRepoIntegrationBase):
    """Part 3: generalized git identity/checkStat across all attached repos and ro hooks/pushurl."""

    def test_git_identity_and_check_stat_applied_to_root_and_all_present_attached_repos(self):
        ro_url, _ = self.make_bare_repo("specs")
        rw_url, _ = self.make_bare_repo("tools")
        opt_url, _ = self.make_bare_repo("unused")
        catalog = {
            "version": 1,
            "repos": [
                {"alias": "specs", "path": "libs/specs", "url": ro_url, "mode": "ro", "attach": "default"},
                {"alias": "tools", "path": "libs/tools", "url": rw_url, "mode": "rw", "attach": "default"},
                {"alias": "unused", "path": "libs/unused", "url": opt_url, "mode": "rw", "attach": "optional"},
            ],
        }
        self.build_project(repos_catalog=catalog)
        acq = self.run_engine("acquire", "--for", "id-check")
        self.assertEqual(acq.returncode, 0, output_of(acq))
        name = self.harness.acquired_slot_name(acq)
        slot_dir = self.harness.slot_dir(name)

        for repo_path in (
            slot_dir,
            os.path.join(slot_dir, "libs", "specs"),
            os.path.join(slot_dir, "libs", "tools"),
        ):
            self.assertEqual(self.real_git(repo_path, "config", "--local", "user.name"), "demo-agent", repo_path)
            self.assertEqual(self.real_git(repo_path, "config", "--local", "user.email"), "demo-agent@example.com", repo_path)
            self.assertEqual(self.real_git(repo_path, "config", "--local", "gpg.format"), "ssh", repo_path)
            self.assertEqual(self.real_git(repo_path, "config", "--local", "commit.gpgsign"), "true", repo_path)
            self.assertEqual(self.real_git(repo_path, "config", "--local", "tag.gpgsign"), "true", repo_path)
            self.assertEqual(self.real_git(repo_path, "config", "--local", "core.checkStat"), "minimal", repo_path)

        with open(os.path.join(self.harness.fixture_repo, "project.env"), "w", encoding="utf-8") as handle:
            handle.write(
                project_env_text(
                    DEFAULT_BRANCH="main",
                    SLOT_REPOS_FILE=".devcontainer/repos.json",
                    SLOT_GIT_NAME="Updated Bot",
                    SLOT_GIT_EMAIL="updated-bot@example.com",
                )
            )
        rst = self.run_engine("restart", name)
        self.assertEqual(rst.returncode, 0, output_of(rst))
        for repo_path in (
            slot_dir,
            os.path.join(slot_dir, "libs", "specs"),
            os.path.join(slot_dir, "libs", "tools"),
        ):
            self.assertEqual(self.real_git(repo_path, "config", "--local", "user.name"), "Updated Bot", repo_path)
            self.assertEqual(self.real_git(repo_path, "config", "--local", "user.email"), "updated-bot@example.com", repo_path)

    def test_readonly_hooks_and_pushurl_enforced_on_ro_repos_and_absent_on_rw_repos(self):
        ro_url, _ = self.make_bare_repo("specs")
        catalog = {
            "version": 1,
            "repos": [
                {"alias": "specs", "path": "deps/specs", "url": ro_url, "mode": "ro", "attach": "default", "rw_alias": "tools"},
                {"alias": "tools", "path": "deps/tools", "url": ro_url, "mode": "rw", "attach": "default"},
            ],
        }
        self.build_project(repos_catalog=catalog)
        acq = self.run_engine("acquire", "--for", "ro-guard")
        self.assertEqual(acq.returncode, 0, output_of(acq))
        name = self.harness.acquired_slot_name(acq)
        slot_dir = self.harness.slot_dir(name)

        ro_dir = os.path.join(slot_dir, "deps", "specs")
        rw_dir = os.path.join(slot_dir, "deps", "tools")

        hooks_path = self.real_git(ro_dir, "config", "--local", "core.hooksPath")
        self.assertTrue(hooks_path, "core.hooksPath not configured on ro repo")
        hooks_abs = hooks_path if os.path.isabs(hooks_path) else os.path.join(ro_dir, hooks_path)
        for hook in ("pre-commit", "pre-push"):
            hp = os.path.join(hooks_abs, hook)
            self.assertTrue(os.path.isfile(hp), f"missing {hook}")
            self.assertTrue(os.access(hp, os.X_OK), f"{hook} not executable")
        self.assertEqual(
            self.real_git(ro_dir, "config", "--local", "remote.origin.pushurl"),
            "read-only://slot-repo-is-read-only--specs",
        )

        with open(os.path.join(ro_dir, "blocked.txt"), "w", encoding="utf-8") as handle:
            handle.write("nope\n")
        self.real_git(ro_dir, "add", "blocked.txt")
        commit_try = subprocess.run(
            [
                self.harness.real_git_bin, "-C", ro_dir,
                "-c", "commit.gpgsign=false",
                "-c", "user.name=fixture",
                "-c", "user.email=fixture@example.com",
                "commit", "-m", "try",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertNotEqual(commit_try.returncode, 0)
        self.assertRegex(output_of(commit_try), r"(?i)read-only|specs|tools")

        push_try = subprocess.run(
            [self.harness.real_git_bin, "-C", ro_dir, "push", "origin", "HEAD:refs/heads/nope"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertNotEqual(push_try.returncode, 0)
        self.assertRegex(output_of(push_try), r"(?i)read-only|specs|tools")

        rw_hooks = subprocess.run(
            [self.harness.real_git_bin, "-C", rw_dir, "config", "--local", "core.hooksPath"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ).stdout.decode().strip()
        self.assertEqual(rw_hooks, "")
        rw_pushurl = subprocess.run(
            [self.harness.real_git_bin, "-C", rw_dir, "config", "--local", "remote.origin.pushurl"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ).stdout.decode().strip()
        self.assertEqual(rw_pushurl, "")
        with open(os.path.join(rw_dir, "ok.txt"), "w", encoding="utf-8") as handle:
            handle.write("ok\n")
        self.real_git(rw_dir, "add", "ok.txt")
        self.real_git(rw_dir, "commit", "-q", "-m", "rw commit succeeds")

    def test_legacy_docs_preserves_docs_repo_pushurl_and_refusal_wording(self):
        legacy_url, _ = self.make_bare_repo("legacydocs", github_url="https://github.com/acme/demo.docs.git")
        self.build_project(project_env_extra={"DOCS_REPO_URL": legacy_url})
        acq = self.run_engine("acquire", "--for", "legacy-ro")
        self.assertEqual(acq.returncode, 0, output_of(acq))
        name = self.harness.acquired_slot_name(acq)
        docs_dir = os.path.join(self.harness.slot_dir(name), "docs", "repo")
        self.assertEqual(
            self.real_git(docs_dir, "config", "--local", "remote.origin.pushurl"),
            "read-only://docs-repo-is-read-only--clone-docs-fix",
        )
        with open(os.path.join(docs_dir, "nope.md"), "w", encoding="utf-8") as handle:
            handle.write("nope\n")
        self.real_git(docs_dir, "add", "nope.md")
        commit_try = subprocess.run(
            [
                self.harness.real_git_bin, "-C", docs_dir,
                "-c", "commit.gpgsign=false",
                "-c", "user.name=fixture",
                "-c", "user.email=fixture@example.com",
                "commit", "-m", "try",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertNotEqual(commit_try.returncode, 0)
        self.assertIn("docs/repo is read-only", output_of(commit_try))
        self.assertIn("docs/fix", output_of(commit_try))
        with open(self.harness.lease_path(name), encoding="utf-8") as handle:
            self.assertEqual(
                json.load(handle).get("repos"),
                [{"alias": "docs", "path": "docs/repo", "slug": "acme/demo.docs", "mode": "ro", "attach": "default"}],
            )


class TestForgeAppPreflightAndDoctor(MultiRepoIntegrationBase):
    """Part 3: FORGE_AUTH=app preflight and doctor check for owner match & FORGE_APP_REPOS coverage."""

    def _setup_app_project(
        self,
        *,
        catalog=None,
        docs_repo_url=None,
        app_repos="web specs docs",
        primary_origin="https://github.com/acme/web.git",
        forge_auth="app",
    ):
        key = os.path.join(self.harness.tmp, "keys", "app.pem")
        os.makedirs(os.path.dirname(key), exist_ok=True)
        shutil.copyfile(KEY_FILE, key)
        os.chmod(key, 0o600)
        if forge_auth == "app":
            parent_env = (
                "FORGE_APP_ID=1234567\n"
                "FORGE_APP_INSTALLATION_ID=7654321\n"
                "FORGE_APP_SLUG=demo-agent\n"
                "FORGE_APP_BOT_ID=424242\n"
                f"FORGE_APP_KEY_FILE={key}\n"
                f"FORGE_APP_REPOS={app_repos}\n"
            )
        else:
            parent_env = IDENTITY_ENV + "GH_TOKEN=ghs_SENTINEL_mr\n"
        extra = {"FORGE_AUTH": forge_auth, "FORGE_AUTH_FILE": ".devcontainer/.env"}
        if docs_repo_url:
            extra["DOCS_REPO_URL"] = docs_repo_url
        self.build_project(
            repos_catalog=catalog,
            project_env_extra=extra,
            parent_env=parent_env,
            origin_url=primary_origin,
        )

    def test_app_mode_accepts_same_owner_project_repos_in_forge_app_repos_and_any_auth_none(self):
        specs_url, _ = self.make_bare_repo("specs", github_url="https://github.com/acme/specs.git")
        pub_url, _ = self.make_bare_repo("pub", github_url="https://github.com/other-org/public-lib.git")
        catalog = {
            "version": 1,
            "repos": [
                {"alias": "specs", "path": "deps/specs", "url": specs_url, "mode": "ro", "attach": "default", "auth": "project"},
                {"alias": "docs", "path": "deps/docs", "url": "git" + "@" + "github.com:acme/docs.git", "mode": "rw", "attach": "optional", "auth": "project"},
                {"alias": "pub", "path": "deps/pub", "url": pub_url, "mode": "ro", "attach": "default", "auth": "none"},
            ],
        }
        self._setup_app_project(catalog=catalog, app_repos="web specs docs")
        doc = self.run_engine("doctor")
        self.assertEqual(doc.returncode, 0, output_of(doc))
        acq = self.run_engine("acquire", "--for", "app-ok")
        self.assertEqual(acq.returncode, 0, output_of(acq))

    def test_app_mode_rejects_project_repo_missing_from_forge_app_repos_in_doctor_and_acquire(self):
        specs_url, _ = self.make_bare_repo("specs", github_url="https://github.com/acme/specs.git")
        catalog = {
            "version": 1,
            "repos": [
                {"alias": "specs", "path": "deps/specs", "url": specs_url, "mode": "ro", "attach": "optional", "auth": "project"},
            ],
        }
        self._setup_app_project(catalog=catalog, app_repos="web")
        for verb_args in (("doctor",), ("acquire", "--for", "app-fail")):
            with self.subTest(cmd=verb_args):
                res = self.run_engine(*verb_args)
                out = output_of(res)
                self.assertNotEqual(res.returncode, 0, out)
                self.assertNotIn("unknown key", out)
                self.assertIn("FORGE_APP_REPOS", out)
                self.assertIn("specs", out)
                self.assertEqual(self.harness.volume_names(), [])

    def test_app_mode_rejects_project_repo_with_different_github_owner_in_doctor_and_acquire(self):
        specs_url, _ = self.make_bare_repo("specs", github_url="https://github.com/other-org/specs.git")
        catalog = {
            "version": 1,
            "repos": [
                {"alias": "specs", "path": "deps/specs", "url": specs_url, "mode": "ro", "attach": "default", "auth": "project"},
            ],
        }
        self._setup_app_project(catalog=catalog, app_repos="web specs")
        for verb_args in (("doctor",), ("acquire", "--for", "owner-fail")):
            with self.subTest(cmd=verb_args):
                res = self.run_engine(*verb_args)
                out = output_of(res)
                self.assertNotEqual(res.returncode, 0, out)
                self.assertNotIn("unknown key", out)
                self.assertIn("other-org", out)
                self.assertIn("acme", out)
                self.assertEqual(self.harness.volume_names(), [])

    def test_app_mode_rejects_legacy_docs_repo_url_mismatch(self):
        self._setup_app_project(
            catalog=None,
            docs_repo_url="https://github.com/other-org/web.docs.git",
            app_repos="web web.docs",
        )
        doc = self.run_engine("doctor")
        self.assertNotEqual(doc.returncode, 0, output_of(doc))
        self.assertIn("other-org", output_of(doc))

        acq = self.run_engine("acquire", "--for", "legacy-owner-fail")
        self.assertNotEqual(acq.returncode, 0, output_of(acq))
        self.assertIn("other-org", output_of(acq))
        self.assertEqual(self.harness.volume_names(), [])

    def test_token_mode_allows_cross_owner_project_repos(self):
        specs_url, _ = self.make_bare_repo("specs", github_url="https://github.com/other-org/specs.git")
        catalog = {
            "version": 1,
            "repos": [
                {"alias": "specs", "path": "deps/specs", "url": specs_url, "mode": "ro", "attach": "default", "auth": "project"},
            ],
        }
        self._setup_app_project(catalog=catalog, forge_auth="token")
        doc = self.run_engine("doctor")
        self.assertEqual(doc.returncode, 0, output_of(doc))


if __name__ == "__main__":
    unittest.main()
