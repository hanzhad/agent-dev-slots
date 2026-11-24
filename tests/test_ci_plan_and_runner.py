"""Tests for CI plan selection, sharded test runner, leg completeness guard,
workflow contract, and template-cached DevContainerHarness isolation (#55).
"""

import atexit
import importlib.util
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TESTS_DIR)
if TESTS_DIR not in sys.path:
    sys.path.insert(0, TESTS_DIR)

import harness

RUN_TESTS = os.path.join(ROOT, "scripts", "ci", "run-tests.py")
SELECT_PLAN = os.path.join(ROOT, "scripts", "ci", "select-ci-plan.py")
CHECK_LEGS = os.path.join(ROOT, "scripts", "ci", "check-test-legs.py")
TIMINGS_FILE = os.path.join(ROOT, "scripts", "ci", "test-timings.json")
CI_WORKFLOW = os.path.join(ROOT, ".github", "workflows", "ci.yml")
DOCKER_TESTS_DIR = os.path.join(ROOT, "tests", "docker")

HEAVY_SUITES = (
    "tests/test_toolbox.py",
    "tests/test_evict_and_reap.py",
    "tests/test_limits.py",
    "tests/test_pool.py",
    "tests/test_agent_auth.py",
    "tests/test_delivery.py",
)


def _load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _timing_entries(timings_data):
    if isinstance(timings_data, dict) and isinstance(timings_data.get("files"), dict):
        return timings_data["files"]
    return timings_data


def _extract_timing_weight(entries, rel_path):
    val = entries.get(rel_path)
    if val is None:
        val = entries.get(os.path.basename(rel_path))
    if isinstance(val, (int, float)) and not isinstance(val, bool) and val > 0:
        return float(val)
    if isinstance(val, dict):
        for key in ("duration_s", "weight", "duration", "seconds", "tests"):
            v = val.get(key)
            if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
                return float(v)
    return None


def _write_file(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


def _parse_kv_output(text):
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


class TestHarnessTemplateCacheAndPorts(unittest.TestCase):
    def test_port_range_widened_for_concurrent_workers(self):
        width = harness.SLOT_PORT_BLOCK * harness.SLOT_COUNT_HELD
        span = (harness._PORT_CEIL - harness._PORT_FLOOR) // width
        self.assertGreaterEqual(harness._PORT_FLOOR, 1024)
        self.assertLessEqual(harness._PORT_CEIL, 65535)
        self.assertGreaterEqual(
            harness._PORT_CEIL - harness._PORT_FLOOR,
            20000,
            "port range should be widened (e.g. 30000..58000) for concurrent workers",
        )
        self.assertGreaterEqual(span, 100)

    def test_dev_container_port_slot_assigns_non_colliding_blocks(self):
        width = harness.SLOT_PORT_BLOCK * harness.SLOT_COUNT_HELD
        bases = []
        for slot_idx in ("0", "1", "2"):
            env = dict(os.environ)
            env["DEV_CONTAINER_PORT_SLOT"] = slot_idx
            proc = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import sys; sys.path.insert(0, sys.argv[1]); import harness; print(harness._PORT_BASE)",
                    TESTS_DIR,
                ],
                capture_output=True,
                text=True,
                env=env,
                check=True,
            )
            bases.append(int(proc.stdout.strip()))

        self.assertEqual(len(set(bases)), 3, f"expected distinct bases, got {bases}")
        for i in range(len(bases)):
            for j in range(i + 1, len(bases)):
                self.assertGreaterEqual(
                    abs(bases[i] - bases[j]),
                    width,
                    f"port blocks {bases[i]} and {bases[j]} overlap (width={width})",
                )

        # Same DEV_CONTAINER_PORT_SLOT should be deterministic across processes.
        env = dict(os.environ)
        env["DEV_CONTAINER_PORT_SLOT"] = "1"
        repeat = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; sys.path.insert(0, sys.argv[1]); import harness; print(harness._PORT_BASE)",
                TESTS_DIR,
            ],
            capture_output=True,
            text=True,
            env=env,
            check=True,
        )
        self.assertEqual(int(repeat.stdout.strip()), bases[1])

    def test_template_cache_eliminates_redundant_git_subprocesses(self):
        script = """
import atexit, subprocess, sys
sys.path.insert(0, sys.argv[1])
import harness

before_cb = atexit._ncallbacks()
h1 = harness.DevContainerHarness()
h1.build_fixture_repo()
after_cb = atexit._ncallbacks()

orig_run = subprocess.run
git_calls = 0
def counting_run(*args, **kwargs):
    global git_calls
    cmd = args[0] if args else kwargs.get("args", [])
    if cmd and "git" in str(cmd[0]):
        git_calls += 1
    return orig_run(*args, **kwargs)

subprocess.run = counting_run
h2 = harness.DevContainerHarness()
h2.build_fixture_repo()
h3 = harness.DevContainerHarness()
h3.build_fixture_repo()

h1.cleanup()
h2.cleanup()
h3.cleanup()
print(f"callbacks_added={after_cb - before_cb}")
print(f"git_calls={git_calls}")
"""
        proc = subprocess.run(
            [sys.executable, "-c", script, TESTS_DIR],
            capture_output=True,
            text=True,
            check=True,
        )
        kv = _parse_kv_output(proc.stdout)
        self.assertGreaterEqual(
            int(kv["callbacks_added"]),
            1,
            "expected atexit cleanup callback registered for template cache",
        )
        self.assertEqual(
            int(kv["git_calls"]),
            0,
            f"expected 0 git subprocesses on cached fixture builds, got {kv['git_calls']}",
        )

    def test_cached_fixture_isolation_and_git_state(self):
        h1 = harness.DevContainerHarness()
        self.addCleanup(h1.cleanup)
        h1.build_fixture_repo()

        h2 = harness.DevContainerHarness()
        self.addCleanup(h2.cleanup)
        h2.build_fixture_repo()

        self.assertNotEqual(h1.fixture_repo, h2.fixture_repo)
        self.assertNotEqual(h1.docs_repo_stub, h2.docs_repo_stub)

        for h in (h1, h2):
            self.assertTrue(os.access(h.script, os.X_OK))
            self.assertTrue(
                os.access(
                    os.path.join(h.fixture_repo, ".devcontainer", "install.sh"),
                    os.X_OK,
                )
            )
            status = subprocess.run(
                [h.real_git_bin, "-C", h.fixture_repo, "status", "--porcelain"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            self.assertEqual(status, "")
            count = subprocess.run(
                [h.real_git_bin, "-C", h.fixture_repo, "rev-list", "--count", "HEAD"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            self.assertEqual(count, "1")
            remote = subprocess.run(
                [h.real_git_bin, "-C", h.fixture_repo, "remote", "get-url", "origin"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            self.assertEqual(remote, "https://example.invalid/fixture.git")

        # Mutate h1 and verify h2 and a fresh h3 remain untouched.
        with open(os.path.join(h1.fixture_repo, "project.env"), "a", encoding="utf-8") as fh:
            fh.write('MUTATED="yes"\n')
        with open(os.path.join(h1.docs_repo_stub, "README.md"), "w", encoding="utf-8") as fh:
            fh.write("mutated docs\n")

        with open(os.path.join(h2.fixture_repo, "project.env"), encoding="utf-8") as fh:
            self.assertNotIn("MUTATED", fh.read())
        with open(os.path.join(h2.docs_repo_stub, "README.md"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "docs repo stub\n")

        h3 = harness.DevContainerHarness()
        self.addCleanup(h3.cleanup)
        h3.build_fixture_repo()
        with open(os.path.join(h3.fixture_repo, "project.env"), encoding="utf-8") as fh:
            self.assertNotIn("MUTATED", fh.read())

    def test_cached_fixture_custom_env_and_project_env(self):
        h = harness.DevContainerHarness()
        self.addCleanup(h.cleanup)
        custom_proj = 'PROJECT="custom"\nDEFAULT_BRANCH="main"\n'
        h.build_fixture_repo(with_parent_env="FOO=bar\n", project_env=custom_proj)

        env_path = os.path.join(h.fixture_repo, ".devcontainer", ".env")
        self.assertTrue(os.path.isfile(env_path))
        self.assertEqual(os.stat(env_path).st_mode & 0o777, 0o600)
        with open(env_path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "FOO=bar\n")

        with open(os.path.join(h.fixture_repo, "project.env"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), custom_proj)

        status = subprocess.run(
            [h.real_git_bin, "-C", h.fixture_repo, "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        self.assertEqual(status, "")

        # Also check project_env="" removes or omits project.env cleanly.
        h_empty = harness.DevContainerHarness()
        self.addCleanup(h_empty.cleanup)
        h_empty.build_fixture_repo(project_env="")
        self.assertFalse(os.path.exists(os.path.join(h_empty.fixture_repo, "project.env")))
        status_empty = subprocess.run(
            [h_empty.real_git_bin, "-C", h_empty.fixture_repo, "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        self.assertEqual(status_empty, "")

    def test_default_env_sets_fast_wait_interval_and_allows_override(self):
        h = harness.DevContainerHarness()
        self.addCleanup(h.cleanup)
        cleaned = dict(os.environ)
        cleaned.pop("DEV_CONTAINER_WAIT_INTERVAL", None)
        with mock.patch.dict(os.environ, cleaned, clear=True):
            env = h._env(None)
            self.assertEqual(
                env.get("DEV_CONTAINER_WAIT_INTERVAL"),
                "0.05",
                "DevContainerHarness default env should set DEV_CONTAINER_WAIT_INTERVAL=0.05",
            )
            overridden = h._env({"DEV_CONTAINER_WAIT_INTERVAL": "0.25"})
            self.assertEqual(overridden.get("DEV_CONTAINER_WAIT_INTERVAL"), "0.25")

    def test_template_cache_keyed_on_project_env_eliminates_git_subprocesses(self):
        script = """
import os, subprocess, sys
sys.path.insert(0, sys.argv[1])
import harness

variants = [
    ("default_fn", harness.project_env_text()),
    ("custom_a", harness.project_env_text(FORGE_AUTH="token", FORGE_AUTH_FILE=".devcontainer/.env")),
    ("custom_b", 'PROJECT="custom"\\nDEFAULT_BRANCH="main"\\n'),
    ("empty", ""),
]

# Warm one template entry per distinct project_env payload.
warmed = []
for label, penv in variants:
    h = harness.DevContainerHarness()
    h.build_fixture_repo(project_env=penv)
    warmed.append((label, penv, h))

orig_run = subprocess.run
git_calls = {}
current_label = None
def counting_run(*args, **kwargs):
    cmd = args[0] if args else kwargs.get("args", [])
    if cmd and "git" in str(cmd[0]) and current_label is not None:
        git_calls[current_label] = git_calls.get(current_label, 0) + 1
    return orig_run(*args, **kwargs)

subprocess.run = counting_run
second = []
for label, penv in variants:
    current_label = label
    h = harness.DevContainerHarness()
    h.build_fixture_repo(project_env=penv)
    second.append((label, penv, h))
current_label = None
subprocess.run = orig_run

# Verify contents, clean git status, and mutation isolation.
for (label, penv, h1), (_, _, h2) in zip(warmed, second):
    p2 = os.path.join(h2.fixture_repo, "project.env")
    if penv:
        with open(p2, encoding="utf-8") as fh:
            assert fh.read() == penv, f"{label}: unexpected project.env content"
    else:
        assert not os.path.exists(p2), f"{label}: expected project.env omitted"
    st = orig_run(
        [h2.real_git_bin, "-C", h2.fixture_repo, "status", "--porcelain"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert st == "", f"{label}: dirty repo {st!r}"
    if penv:
        with open(os.path.join(h1.fixture_repo, "project.env"), "a", encoding="utf-8") as fh:
            fh.write('MUTATED="1"\\n')
        with open(p2, encoding="utf-8") as fh:
            assert "MUTATED" not in fh.read(), f"{label}: mutation leaked across harnesses"

for _, _, h in warmed + second:
    h.cleanup()

for label, _ in variants:
    print(f"{label}={git_calls.get(label, 0)}")
"""
        proc = subprocess.run(
            [sys.executable, "-c", script, TESTS_DIR],
            capture_output=True,
            text=True,
            check=True,
        )
        kv = _parse_kv_output(proc.stdout)
        for label in ("default_fn", "custom_a", "custom_b", "empty"):
            self.assertEqual(
                int(kv[label]),
                0,
                f"expected 0 git subprocesses on cached project_env={label}, got {kv[label]}",
            )

    def test_docs_repo_stub_is_lazy_and_isolated(self):
        script = """
import os, shutil, sys
sys.path.insert(0, sys.argv[1])
import harness

orig_copytree = shutil.copytree
docs_copies = 0
def counting_copytree(src, dst, *args, **kwargs):
    global docs_copies
    if os.path.basename(str(src)) == "docs-repo-stub" or os.path.basename(str(dst)) == "docs-repo-stub":
        docs_copies += 1
    return orig_copytree(src, dst, *args, **kwargs)

shutil.copytree = counting_copytree

h1 = harness.DevContainerHarness()
h1.build_fixture_repo()
exists_before = os.path.exists(os.path.join(h1.tmp, "docs-repo-stub"))
copies_before = docs_copies

p1 = h1.docs_repo_stub
p1_again = h1.docs_repo_stub
copies_after_h1 = docs_copies
assert p1 == p1_again
with open(os.path.join(p1, "README.md"), encoding="utf-8") as fh:
    readme_text = fh.read()
assert readme_text == "docs repo stub\\n", repr(readme_text)

# Setter must keep working for callers that relocate harness paths.
custom_path = os.path.join(h1.tmp, "relocated-docs")
h1.docs_repo_stub = custom_path
assert h1.docs_repo_stub == custom_path

h1.cleanup()
print(f"exists_before={int(exists_before)}")
print(f"copies_before={copies_before}")
print(f"copies_after_h1={copies_after_h1}")
"""
        proc = subprocess.run(
            [sys.executable, "-c", script, TESTS_DIR],
            capture_output=True,
            text=True,
            check=True,
        )
        kv = _parse_kv_output(proc.stdout)
        self.assertEqual(
            int(kv["exists_before"]),
            0,
            "docs-repo-stub should not be materialized before access",
        )
        self.assertEqual(
            int(kv["copies_before"]),
            0,
            "shutil.copytree should not run for docs-repo-stub when unused",
        )
        self.assertEqual(
            int(kv["copies_after_h1"]),
            1,
            "docs-repo-stub should be materialized lazily exactly once on access",
        )

    def test_toolbox_hoists_openssl_genrsa_to_setUpModule(self):
        script = """
import os, subprocess, sys
sys.path.insert(0, sys.argv[1])
import test_toolbox

assert callable(getattr(test_toolbox, "setUpModule", None)), "missing setUpModule in test_toolbox"
assert callable(getattr(test_toolbox, "tearDownModule", None)), "missing tearDownModule in test_toolbox"

test_toolbox.setUpModule()
try:
    orig_run = subprocess.run
    openssl_calls = 0
    def counting_run(*args, **kwargs):
        global openssl_calls
        cmd = args[0] if args else kwargs.get("args", [])
        if cmd and "openssl" in str(cmd[0]):
            openssl_calls += 1
        return orig_run(*args, **kwargs)

    subprocess.run = counting_run
    tc = test_toolbox.TestToolboxCliAndLifecycle("test_acquire_with_toolbox_builds_lazily_writes_state_renders_overlay_and_runs_toolbox")
    tc.setUp()
    try:
        tc._setup_app_auth()
        key_path = os.path.join(tc.harness.tmp, "app-keys", "app.pem")
        assert os.path.isfile(key_path), f"missing app key at {key_path}"
        assert (os.stat(key_path).st_mode & 0o777) == 0o600
        with open(key_path, encoding="utf-8") as fh:
            assert "-----BEGIN" in fh.read()
    finally:
        subprocess.run = orig_run
        tc.doCleanups()
finally:
    test_toolbox.tearDownModule()

print(f"openssl_calls={openssl_calls}")
"""
        proc = subprocess.run(
            [sys.executable, "-c", script, TESTS_DIR],
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        kv = _parse_kv_output(proc.stdout)
        self.assertEqual(
            int(kv["openssl_calls"]),
            0,
            f"expected 0 openssl genrsa calls inside _setup_app_auth, got {kv['openssl_calls']}",
        )


class TestRunTestsScript(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = self._tmp.name

    def _run(self, *args):
        self.assertTrue(os.path.isfile(RUN_TESTS), f"missing {RUN_TESTS}")
        return subprocess.run(
            [sys.executable, RUN_TESTS, *args],
            capture_output=True,
            text=True,
        )

    def test_passes_and_writes_report_schema(self):
        t1 = _write_file(
            os.path.join(self.dir, "test_one.py"),
            "import unittest\nclass T1(unittest.TestCase):\n    def test_ok(self): pass\n",
        )
        t2 = _write_file(
            os.path.join(self.dir, "test_two.py"),
            "import unittest\nclass T2(unittest.TestCase):\n"
            "    def test_a(self): pass\n    def test_b(self): pass\n",
        )
        files_txt = _write_file(os.path.join(self.dir, "files.txt"), f"{t1}\n{t2}\n")
        report_path = os.path.join(self.dir, "report.json")

        proc = self._run(
            "--leg", "shard-1", "--files-from", files_txt, "--workers", "2", "--report", report_path
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        with open(report_path, encoding="utf-8") as fh:
            report = json.load(fh)

        self.assertEqual(report["leg"], "shard-1")
        by_file = {entry["file"]: entry for entry in report["files"]}
        self.assertEqual(len(by_file), 2)
        self.assertEqual(by_file[t1]["tests"], 1)
        self.assertEqual(by_file[t2]["tests"], 2)
        for entry in report["files"]:
            self.assertEqual(entry["failures"], 0)
            self.assertEqual(entry["errors"], 0)
            self.assertEqual(entry["skipped"], 0)
            self.assertIsInstance(entry["duration_s"], (int, float))
            self.assertGreaterEqual(entry["duration_s"], 0.0)

    def test_reports_failures_errors_and_skips(self):
        t_fail = _write_file(
            os.path.join(self.dir, "test_fail.py"),
            "import unittest\nclass TF(unittest.TestCase):\n"
            "    def test_f(self): self.fail('boom')\n",
        )
        t_err = _write_file(
            os.path.join(self.dir, "test_err.py"),
            "import unittest\nclass TE(unittest.TestCase):\n"
            "    def test_e(self): raise RuntimeError('err')\n",
        )
        t_skip = _write_file(
            os.path.join(self.dir, "test_skip.py"),
            "import unittest\nclass TS(unittest.TestCase):\n"
            "    @unittest.skip('later')\n    def test_s(self): pass\n",
        )

        f_fail = _write_file(os.path.join(self.dir, "f1.txt"), f"{t_fail}\n")
        r_fail = os.path.join(self.dir, "r1.json")
        p1 = self._run("--leg", "s1", "--files-from", f_fail, "--report", r_fail)
        self.assertNotEqual(p1.returncode, 0)
        with open(r_fail, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["files"][0]["failures"], 1)

        f_err = _write_file(os.path.join(self.dir, "f2.txt"), f"{t_err}\n")
        r_err = os.path.join(self.dir, "r2.json")
        p2 = self._run("--leg", "s2", "--files-from", f_err, "--report", r_err)
        self.assertNotEqual(p2.returncode, 0)
        with open(r_err, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["files"][0]["errors"], 1)

        f_skip = _write_file(os.path.join(self.dir, "f3.txt"), f"{t_skip}\n")
        r_skip = os.path.join(self.dir, "r3.json")
        p3 = self._run("--leg", "s3", "--files-from", f_skip, "--report", r_skip)
        self.assertEqual(p3.returncode, 0, p3.stdout + p3.stderr)
        with open(r_skip, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["files"][0]["skipped"], 1)

    def test_exits_nonzero_when_zero_tests_ran(self):
        empty_list = _write_file(os.path.join(self.dir, "empty.txt"), "")
        p_empty = self._run("--leg", "s0", "--files-from", empty_list)
        self.assertNotEqual(p_empty.returncode, 0)

        zero_test_file = _write_file(
            os.path.join(self.dir, "test_zero.py"),
            "import unittest\nclass TZ(unittest.TestCase):\n    pass\n",
        )
        zero_list = _write_file(os.path.join(self.dir, "zero.txt"), f"{zero_test_file}\n")
        p_zero = self._run("--leg", "s0", "--files-from", zero_list)
        self.assertNotEqual(p_zero.returncode, 0)

    def test_sharding_is_deterministic_balanced_and_validates_bounds(self):
        paths = []
        for idx, extra_lines in enumerate((5, 40, 80, 120, 160, 200), start=1):
            pad = "\n".join(f"# line {n}" for n in range(extra_lines))
            p = _write_file(
                os.path.join(self.dir, f"test_s{idx}.py"),
                f"{pad}\nimport unittest\nclass T{idx}(unittest.TestCase):\n    def test_ok(self): pass\n",
            )
            paths.append(p)
        files_txt = _write_file(os.path.join(self.dir, "all.txt"), "\n".join(paths) + "\n")

        seen = []
        shard_counts = []
        for shard in (1, 2, 3):
            rep = os.path.join(self.dir, f"shard_{shard}.json")
            proc = self._run(
                "--leg", f"shard-{shard}", "--shard", f"{shard}/3", "--files-from", files_txt, "--report", rep
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            with open(rep, encoding="utf-8") as fh:
                entries = json.load(fh)["files"]
            shard_counts.append(len(entries))
            seen.extend(e["file"] for e in entries)

        self.assertEqual(sorted(seen), sorted(paths))
        self.assertEqual(shard_counts, [2, 2, 2])

        # Determinism check
        rep_again = os.path.join(self.dir, "shard_1_again.json")
        self._run("--leg", "shard-1", "--shard", "1/3", "--files-from", files_txt, "--report", rep_again)
        with open(os.path.join(self.dir, "shard_1.json"), encoding="utf-8") as f1, open(
            rep_again, encoding="utf-8"
        ) as f2:
            self.assertEqual(
                [e["file"] for e in json.load(f1)["files"]],
                [e["file"] for e in json.load(f2)["files"]],
            )

        # Invalid shard bounds
        for bad in ("0/3", "4/3", "bad"):
            p_bad = self._run("--shard", bad, "--files-from", files_txt)
            self.assertNotEqual(p_bad.returncode, 0)

    def test_workers_isolate_dev_container_port_slot(self):
        out_log = os.path.join(self.dir, "slots.log")
        files = []
        for idx in (1, 2):
            p = _write_file(
                os.path.join(self.dir, f"test_w{idx}.py"),
                "import os, time, unittest\n"
                "class TW(unittest.TestCase):\n"
                "    def test_slot(self):\n"
                "        time.sleep(0.05)\n"
                f"        with open({out_log!r}, 'a', encoding='utf-8') as fh:\n"
                "            fh.write(os.environ.get('DEV_CONTAINER_PORT_SLOT', '') + '\\n')\n",
            )
            files.append(p)
        files_txt = _write_file(os.path.join(self.dir, "workers.txt"), "\n".join(files) + "\n")
        proc = self._run("--leg", "w", "--files-from", files_txt, "--workers", "2")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        with open(out_log, encoding="utf-8") as fh:
            slots = [line.strip() for line in fh if line.strip()]
        self.assertEqual(len(slots), 2)
        self.assertEqual(len(set(slots)), 2, f"expected distinct worker port slots, got {slots}")

    def test_timings_file_exists_and_spreads_heavy_suites_across_shards(self):
        self.assertTrue(os.path.isfile(TIMINGS_FILE), f"missing {TIMINGS_FILE}")
        with open(TIMINGS_FILE, encoding="utf-8") as fh:
            raw_timings = json.load(fh)
        entries = _timing_entries(raw_timings)
        self.assertIsInstance(entries, dict)
        self.assertGreater(len(entries), 0)

        for suite in HEAVY_SUITES:
            weight = _extract_timing_weight(entries, suite)
            self.assertIsNotNone(
                weight, f"{TIMINGS_FILE} missing positive numeric weight for {suite}"
            )

        runner = _load_module("run_tests_mod", RUN_TESTS)
        all_files = runner.discover_test_files(os.path.join(ROOT, "tests"))
        rel_files = [os.path.relpath(p, ROOT) for p in all_files]
        self.assertGreater(len(rel_files), 10)

        shards = [runner.select_shard(rel_files, i, 6) for i in range(1, 7)]
        flattened = [p for bucket in shards for p in bucket]
        self.assertEqual(sorted(flattened), sorted(rel_files))
        self.assertEqual(len(flattened), len(set(flattened)))

        heavy_set = set(HEAVY_SUITES)
        shard_heavy_counts = [sum(1 for p in bucket if p in heavy_set) for bucket in shards]
        distinct_heavy_shards = sum(1 for c in shard_heavy_counts if c > 0)
        self.assertGreaterEqual(
            distinct_heavy_shards,
            5,
            f"heavy suites piled into too few shards: {shard_heavy_counts}",
        )
        self.assertLessEqual(
            max(shard_heavy_counts),
            2,
            f"heavy suites piled into one shard: {shard_heavy_counts}",
        )

        # Weighted loads across the 6 shards should be balanced.
        shard_weights = []
        for bucket in shards:
            total_w = 0.0
            for p in bucket:
                w = _extract_timing_weight(entries, p)
                if w is not None:
                    total_w += w
            shard_weights.append(total_w)
        mean_w = sum(shard_weights) / len(shard_weights)
        self.assertGreater(mean_w, 0.0)
        self.assertLessEqual(
            max(shard_weights),
            mean_w * 1.5,
            f"weighted shard loads are skewed: {shard_weights}",
        )

    def test_measured_timings_override_test_count_and_line_count(self):
        self.assertTrue(os.path.isfile(TIMINGS_FILE), f"missing {TIMINGS_FILE}")
        with open(TIMINGS_FILE, encoding="utf-8") as fh:
            real_data = json.load(fh)
        real_entries = _timing_entries(real_data)
        sample_key, sample_val = next(iter(real_entries.items()))
        use_basename = "/" not in sample_key

        def make_entry(weight_val):
            if isinstance(sample_val, dict):
                out = dict(sample_val)
                for k in ("duration_s", "weight", "duration", "seconds", "tests"):
                    if k in out:
                        out[k] = weight_val
                        return out
                out["duration_s"] = weight_val
                return out
            return weight_val

        # Copy run-tests.py into an isolated temp repo with synthetic test-timings.json.
        tmp_runner = os.path.join(self.dir, "scripts", "ci", "run-tests.py")
        os.makedirs(os.path.dirname(tmp_runner), exist_ok=True)
        shutil.copyfile(RUN_TESTS, tmp_runner)

        many_methods = "\n".join(f"    def test_m{i}(self): pass" for i in range(20))
        pad = "\n".join(f"# line {i}" for i in range(500))
        files_spec = {
            "tests/test_slow_a.py": (
                "import unittest\nclass A(unittest.TestCase):\n    def test_ok(self): pass\n",
                100.0,
            ),
            "tests/test_slow_b.py": (
                "import unittest\nclass B(unittest.TestCase):\n    def test_ok(self): pass\n",
                100.0,
            ),
            "tests/test_fast_many_c.py": (
                f"{pad}\nimport unittest\nclass C(unittest.TestCase):\n{many_methods}\n",
                1.0,
            ),
            "tests/test_fast_tiny_d.py": (
                "import unittest\nclass D(unittest.TestCase):\n    def test_ok(self): pass\n",
                1.0,
            ),
        }
        synthetic_entries = {}
        for rel_path, (src, weight) in files_spec.items():
            _write_file(os.path.join(self.dir, rel_path), src)
            k = os.path.basename(rel_path) if use_basename else rel_path
            synthetic_entries[k] = make_entry(weight)

        if isinstance(real_data, dict) and isinstance(real_data.get("files"), dict):
            payload = dict(real_data)
            payload["files"] = synthetic_entries
        else:
            payload = synthetic_entries
        _write_file(
            os.path.join(self.dir, "scripts", "ci", "test-timings.json"),
            json.dumps(payload, indent=2),
        )
        manifest = _write_file(
            os.path.join(self.dir, "selected.txt"),
            "\n".join(files_spec.keys()) + "\n",
        )

        # Verify both discovery sharding and --files-from + --shard respect measured weights.
        for mode, extra_args in (("discover", []), ("files-from", ["--files-from", manifest])):
            with self.subTest(mode=mode):
                buckets = []
                for idx in (1, 2):
                    rep = os.path.join(self.dir, f"rep_{mode}_{idx}.json")
                    proc = subprocess.run(
                        [
                            sys.executable,
                            tmp_runner,
                            "--leg",
                            f"s-{idx}",
                            "--shard",
                            f"{idx}/2",
                            *extra_args,
                            "--report",
                            rep,
                        ],
                        cwd=self.dir,
                        capture_output=True,
                        text=True,
                    )
                    self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                    with open(rep, encoding="utf-8") as fh:
                        buckets.append({e["file"] for e in json.load(fh)["files"]})
                self.assertEqual(buckets[0] | buckets[1], set(files_spec.keys()))
                self.assertEqual(buckets[0] & buckets[1], set())
                slow_a_in_0 = "tests/test_slow_a.py" in buckets[0]
                slow_b_in_0 = "tests/test_slow_b.py" in buckets[0]
                self.assertNotEqual(
                    slow_a_in_0,
                    slow_b_in_0,
                    f"measured weights should separate slow_a and slow_b across shards, got {buckets}",
                )

    def test_unrecorded_files_fall_back_to_test_count_then_line_count(self):
        # Part A: unrecorded files with more `def test_` methods outweigh longer comment-padded files.
        ten_tests = "\n".join(f"    def test_t{i}(self): pass" for i in range(10))
        pad_600 = "\n".join(f"# comment {i}" for i in range(600))
        many_a = _write_file(
            os.path.join(self.dir, "test_unrec_many_a.py"),
            f"import unittest\nclass MA(unittest.TestCase):\n{ten_tests}\n",
        )
        many_b = _write_file(
            os.path.join(self.dir, "test_unrec_many_b.py"),
            f"import unittest\nclass MB(unittest.TestCase):\n{ten_tests}\n",
        )
        padded_c = _write_file(
            os.path.join(self.dir, "test_unrec_padded_c.py"),
            f"{pad_600}\nimport unittest\nclass PC(unittest.TestCase):\n    def test_ok(self): pass\n",
        )
        tiny_d = _write_file(
            os.path.join(self.dir, "test_unrec_tiny_d.py"),
            "import unittest\nclass TD(unittest.TestCase):\n    def test_ok(self): pass\n",
        )
        files_txt = _write_file(
            os.path.join(self.dir, "unrec.txt"),
            "\n".join([many_a, many_b, padded_c, tiny_d]) + "\n",
        )
        buckets = []
        for idx in (1, 2):
            rep = os.path.join(self.dir, f"unrec_{idx}.json")
            proc = self._run(
                "--leg", f"u-{idx}", "--shard", f"{idx}/2", "--files-from", files_txt, "--report", rep
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            with open(rep, encoding="utf-8") as fh:
                buckets.append({e["file"] for e in json.load(fh)["files"]})
        self.assertNotEqual(
            many_a in buckets[0],
            many_b in buckets[0],
            f"def test_ count fallback should split many_a and many_b across shards, got {buckets}",
        )

        # Part B: unrecorded files with 0 `def test_` fall back to line count.
        runner = _load_module("run_tests_fallback", RUN_TESTS)
        dyn_big = _write_file(
            os.path.join(self.dir, "test_dyn_big.py"),
            "\n".join(f"# line {i}" for i in range(600)) + "\n",
        )
        dyn_m1 = _write_file(
            os.path.join(self.dir, "test_dyn_m1.py"),
            "\n".join(f"# line {i}" for i in range(30)) + "\n",
        )
        dyn_m2 = _write_file(
            os.path.join(self.dir, "test_dyn_m2.py"),
            "\n".join(f"# line {i}" for i in range(30)) + "\n",
        )
        dyn_s = _write_file(
            os.path.join(self.dir, "test_dyn_s.py"),
            "\n".join(f"# line {i}" for i in range(10)) + "\n",
        )
        dyn_paths = [dyn_big, dyn_m1, dyn_m2, dyn_s]
        b0 = set(runner.select_shard(dyn_paths, 1, 2))
        b1 = set(runner.select_shard(dyn_paths, 2, 2))
        self.assertEqual(b0 | b1, set(dyn_paths))
        self.assertEqual(b0 & b1, set())
        big_bucket = b0 if dyn_big in b0 else b1
        self.assertEqual(
            big_bucket,
            {dyn_big},
            f"line-count fallback should isolate the 600-line 0-test file, got {b0} / {b1}",
        )


class TestSelectCiPlanScript(unittest.TestCase):
    def _run_plan(self, changed_files, event_name="pull_request", github_output=None):
        self.assertTrue(os.path.isfile(SELECT_PLAN), f"missing {SELECT_PLAN}")
        env = dict(os.environ)
        env["EVENT_NAME"] = event_name
        if github_output is not None:
            env["GITHUB_OUTPUT"] = github_output
        else:
            env.pop("GITHUB_OUTPUT", None)
        proc = subprocess.run(
            [sys.executable, SELECT_PLAN],
            input="\n".join(changed_files) + ("\n" if changed_files else ""),
            capture_output=True,
            text=True,
            env=env,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        raw = proc.stdout
        if github_output is not None:
            with open(github_output, encoding="utf-8") as fh:
                raw = fh.read()
        return _parse_kv_output(raw)

    def test_full_run_on_push_workflow_dispatch_and_fail_closed(self):
        expected_matrix = [f"shard-{i}" for i in range(1, 7)]
        cases = [
            ("push", ["README.md"]),
            ("workflow_dispatch", []),
            ("pull_request", ["__fail_closed__"]),
        ]
        for event_name, files in cases:
            with self.subTest(event_name=event_name, files=files):
                out = self._run_plan(files, event_name=event_name)
                self.assertEqual(out.get("all"), "true")
                self.assertEqual(out.get("run_docker"), "true")
                self.assertEqual(json.loads(out["matrix"]), expected_matrix)

    def test_full_run_on_core_and_ci_paths(self):
        expected_matrix = [f"shard-{i}" for i in range(1, 7)]
        trigger_paths = [
            ".github/workflows/ci.yml",
            "scripts/ci/run-tests.py",
            "tests/harness.py",
            "bin/dev-container",
            "lib/slots.sh",
            "templates/docker-compose.yml",
            "docker/entrypoint.sh",
            "Dockerfile",
            "compose.yml",
        ]
        for path in trigger_paths:
            with self.subTest(path=path):
                out = self._run_plan([path], event_name="pull_request")
                self.assertEqual(out.get("all"), "true")
                self.assertEqual(out.get("run_docker"), "true")
                self.assertEqual(json.loads(out["matrix"]), expected_matrix)

        mixed = self._run_plan(["README.md", "lib/slots.sh"], event_name="pull_request")
        self.assertEqual(mixed.get("all"), "true")
        self.assertEqual(mixed.get("run_docker"), "true")

    def test_docs_and_readme_only_pr_skips_tests_and_docker(self):
        out = self._run_plan(["docs/guide.md", "README.md"], event_name="pull_request")
        self.assertEqual(out.get("all"), "false")
        self.assertEqual(out.get("run_docker"), "false")
        self.assertEqual(json.loads(out["matrix"]), [])

    def test_specific_non_docker_test_files_pr_selects_only_those(self):
        out = self._run_plan(["tests/test_scan.py", "tests/test_pool.py"], event_name="pull_request")
        self.assertEqual(out.get("all"), "false")
        self.assertEqual(out.get("run_docker"), "false")
        self.assertGreater(len(json.loads(out["matrix"])), 0)
        self.assertIn("tests/test_scan.py", out.get("scope", ""))
        self.assertIn("tests/test_pool.py", out.get("scope", ""))
        self.assertNotIn("tests/test_adopt.py", out.get("scope", ""))

    def test_docker_test_file_enables_docker(self):
        out = self._run_plan(["tests/docker/test_overlay.py"], event_name="pull_request")
        self.assertEqual(out.get("run_docker"), "true")

    def test_deleted_unit_test_files_are_excluded_from_scope(self):
        out_only_deleted = self._run_plan(
            ["tests/test_deleted_nonexistent.py"], event_name="pull_request"
        )
        self.assertEqual(out_only_deleted.get("all"), "false")
        self.assertEqual(out_only_deleted.get("run_docker"), "false")
        self.assertEqual(json.loads(out_only_deleted["matrix"]), [])
        self.assertEqual(out_only_deleted.get("scope"), "none")

        out_mixed = self._run_plan(
            ["tests/test_deleted_nonexistent.py", "tests/test_scan.py"],
            event_name="pull_request",
        )
        self.assertEqual(out_mixed.get("all"), "false")
        self.assertEqual(out_mixed.get("run_docker"), "false")
        self.assertEqual(json.loads(out_mixed["matrix"]), ["selected"])
        self.assertEqual(out_mixed.get("scope"), "tests/test_scan.py")

    def test_writes_to_github_output_when_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            gh_out = os.path.join(tmp, "github_output.txt")
            out = self._run_plan(["README.md"], event_name="pull_request", github_output=gh_out)
            self.assertEqual(out.get("all"), "false")
            self.assertEqual(out.get("run_docker"), "false")

    def test_selected_tests_sharded_into_multiple_legs_above_two_files(self):
        plan_mod = _load_module("select_ci_plan_mod", SELECT_PLAN)
        shard_count = plan_mod.SHARD_COUNT

        # Pure table-driven check across boundary counts 1..20.
        for count in (1, 2, 3, 4, 5, 6, 7, 10, 11, 12, 13, 20):
            files = [f"tests/test_case_{i}.py" for i in range(1, count + 1)]
            with self.subTest(count=count):
                all_flag, matrix, run_docker, scope = plan_mod.select_plan(
                    files, "pull_request", exists_fn=lambda _: True
                )
                self.assertFalse(all_flag)
                self.assertFalse(run_docker)
                self.assertEqual(scope.split(), files)
                if count <= 2:
                    self.assertEqual(matrix, ["selected"])
                else:
                    expected_k = min(shard_count, math.ceil(count / 2))
                    self.assertEqual(matrix, [f"selected-{i}" for i in range(1, expected_k + 1)])

        # Deduplication and deleted-file filtering happen before leg-count calculation.
        _, dup_matrix, _, _ = plan_mod.select_plan(
            ["tests/test_a.py", "tests/test_b.py", "tests/test_a.py", "tests/test_b.py"],
            "pull_request",
            exists_fn=lambda _: True,
        )
        self.assertEqual(dup_matrix, ["selected"])

        existing = {"tests/test_a.py", "tests/test_b.py"}
        _, filtered_matrix, _, filtered_scope = plan_mod.select_plan(
            ["tests/test_a.py", "tests/test_b.py", "tests/test_gone_1.py", "tests/test_gone_2.py"],
            "pull_request",
            exists_fn=lambda p: p in existing,
        )
        self.assertEqual(filtered_matrix, ["selected"])
        self.assertEqual(filtered_scope, "tests/test_a.py tests/test_b.py")

        # Mixing 3 unit tests with a docker test emits sharded selected legs AND run_docker=true.
        _, mixed_matrix, mixed_docker, mixed_scope = plan_mod.select_plan(
            ["tests/test_a.py", "tests/test_b.py", "tests/test_c.py", "tests/docker/test_overlay.py"],
            "pull_request",
            exists_fn=lambda _: True,
        )
        self.assertTrue(mixed_docker)
        self.assertEqual(mixed_matrix, ["selected-1", "selected-2"])
        self.assertEqual(mixed_scope.split(), ["tests/test_a.py", "tests/test_b.py", "tests/test_c.py"])

        # End-to-end CLI flow: select-ci-plan.py -> run-tests.py --files-from --shard -> check-test-legs.py.
        real_five = sorted(
            os.path.relpath(os.path.join(TESTS_DIR, name), ROOT)
            for name in os.listdir(TESTS_DIR)
            if name.startswith("test_") and name.endswith(".py")
        )[:5]
        self.assertEqual(len(real_five), 5)
        cli_out = self._run_plan(real_five, event_name="pull_request")
        legs = json.loads(cli_out["matrix"])
        self.assertEqual(legs, ["selected-1", "selected-2", "selected-3"])
        self.assertEqual(cli_out["scope"].split(), real_five)

        with tempfile.TemporaryDirectory() as tmp:
            # Verify --files-from + --shard i/len(legs) covers synthetic selected files cleanly.
            synth_files = []
            for idx in range(1, 6):
                p = _write_file(
                    os.path.join(tmp, f"test_sel_{idx}.py"),
                    f"import unittest\nclass S{idx}(unittest.TestCase):\n    def test_ok(self): pass\n",
                )
                synth_files.append(p)
            manifest = _write_file(os.path.join(tmp, "selected.txt"), "\n".join(synth_files) + "\n")
            reports_dir = os.path.join(tmp, "reports")
            os.makedirs(reports_dir)
            for idx, leg in enumerate(legs, start=1):
                rep = os.path.join(reports_dir, f"{leg}.json")
                proc = subprocess.run(
                    [
                        sys.executable,
                        RUN_TESTS,
                        "--leg",
                        leg,
                        "--files-from",
                        manifest,
                        "--shard",
                        f"{idx}/{len(legs)}",
                        "--report",
                        rep,
                    ],
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            chk = subprocess.run(
                [sys.executable, CHECK_LEGS, "--scope", " ".join(synth_files), reports_dir],
                input="\n".join(synth_files) + "\n",
                capture_output=True,
                text=True,
            )
            self.assertEqual(chk.returncode, 0, chk.stdout + chk.stderr)

    def test_full_run_prefixes_uses_image_instead_of_docker(self):
        plan_mod = _load_module("select_ci_plan_prefix_mod", SELECT_PLAN)
        self.assertIn("image/", plan_mod.FULL_RUN_PREFIXES)
        self.assertNotIn("docker/", plan_mod.FULL_RUN_PREFIXES)

        for path in ("image/tools.txt", "image/Dockerfile", "image/toolbox/Dockerfile", "image/README.md"):
            with self.subTest(full_path=path):
                self.assertTrue(plan_mod._is_full_run_path(path), f"expected full-run match for {path}")

        # Adjacent negative boundary: "docker/" prefix was removed, so docker/README.md is not matched by FULL_RUN_PREFIXES.
        self.assertFalse(plan_mod._is_full_run_path("docker/README.md"))

        # CLI check: image/README.md must trigger a full run rather than being skipped as README.
        out_readme = self._run_plan(["image/README.md"], event_name="pull_request")
        self.assertEqual(out_readme.get("all"), "true")
        self.assertEqual(out_readme.get("run_docker"), "true")
        self.assertEqual(json.loads(out_readme["matrix"]), [f"shard-{i}" for i in range(1, 7)])

        out_tools = self._run_plan(["image/tools.txt"], event_name="pull_request")
        self.assertEqual(out_tools.get("all"), "true")
        self.assertEqual(out_tools.get("run_docker"), "true")


class TestCheckTestLegsScript(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = self._tmp.name
        self.reports_dir = os.path.join(self.dir, "reports")
        os.makedirs(self.reports_dir)

    def _write_report(self, name, leg, file_entries):
        path = os.path.join(self.reports_dir, name)
        _write_file(path, json.dumps({"leg": leg, "files": file_entries}))
        return path

    def _run_check(self, tracked_files, scope="all", summary_path=None):
        self.assertTrue(os.path.isfile(CHECK_LEGS), f"missing {CHECK_LEGS}")
        env = dict(os.environ)
        if summary_path is not None:
            env["GITHUB_STEP_SUMMARY"] = summary_path
        else:
            env.pop("GITHUB_STEP_SUMMARY", None)
        return subprocess.run(
            [sys.executable, CHECK_LEGS, "--scope", scope, self.reports_dir],
            input="\n".join(tracked_files) + "\n",
            capture_output=True,
            text=True,
            env=env,
        )

    def test_passes_when_all_scoped_files_run_once_and_writes_summary(self):
        self._write_report(
            "r1.json",
            "shard-1",
            [
                {"file": "tests/test_a.py", "tests": 2, "failures": 0, "errors": 0, "skipped": 0, "duration_s": 0.1},
                {"file": "tests/test_b.py", "tests": 3, "failures": 0, "errors": 0, "skipped": 0, "duration_s": 0.2},
            ],
        )
        self._write_report(
            "r2.json",
            "shard-2",
            [
                {"file": "tests/test_c.py", "tests": 1, "failures": 0, "errors": 0, "skipped": 0, "duration_s": 0.1},
            ],
        )
        summary = os.path.join(self.dir, "summary.md")
        proc = self._run_check(
            ["tests/test_a.py", "tests/test_b.py", "tests/test_c.py"],
            scope="all",
            summary_path=summary,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        with open(summary, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("|", text)
        self.assertIn("shard-1", text)
        self.assertIn("shard-2", text)

    def test_fails_on_missed_file(self):
        self._write_report(
            "r1.json",
            "shard-1",
            [{"file": "tests/test_a.py", "tests": 1, "failures": 0, "errors": 0, "skipped": 0, "duration_s": 0.1}],
        )
        proc = self._run_check(["tests/test_a.py", "tests/test_b.py"], scope="all")
        self.assertNotEqual(proc.returncode, 0)

    def test_fails_on_duplicate_file_across_legs(self):
        entry = {"file": "tests/test_a.py", "tests": 1, "failures": 0, "errors": 0, "skipped": 0, "duration_s": 0.1}
        self._write_report("r1.json", "shard-1", [entry])
        self._write_report("r2.json", "shard-2", [entry])
        proc = self._run_check(["tests/test_a.py"], scope="all")
        self.assertNotEqual(proc.returncode, 0)

    def test_fails_on_failure_or_error_in_report(self):
        self._write_report(
            "r1.json",
            "shard-1",
            [{"file": "tests/test_a.py", "tests": 1, "failures": 1, "errors": 0, "skipped": 0, "duration_s": 0.1}],
        )
        self.assertNotEqual(self._run_check(["tests/test_a.py"], scope="all").returncode, 0)

        self._write_report(
            "r1.json",
            "shard-1",
            [{"file": "tests/test_a.py", "tests": 1, "failures": 0, "errors": 1, "skipped": 0, "duration_s": 0.1}],
        )
        self.assertNotEqual(self._run_check(["tests/test_a.py"], scope="all").returncode, 0)

    def test_scoped_run_checks_only_scoped_files(self):
        self._write_report(
            "r1.json",
            "shard-1",
            [{"file": "tests/test_a.py", "tests": 1, "failures": 0, "errors": 0, "skipped": 0, "duration_s": 0.1}],
        )
        ok = self._run_check(
            ["tests/test_a.py", "tests/test_b.py", "tests/test_c.py"],
            scope="tests/test_a.py",
        )
        self.assertEqual(ok.returncode, 0, ok.stdout + ok.stderr)

        missing = self._run_check(
            ["tests/test_a.py", "tests/test_b.py"],
            scope="tests/test_b.py",
        )
        self.assertNotEqual(missing.returncode, 0)

    def test_empty_reports_dir_fails_for_non_empty_scope(self):
        proc = self._run_check(["tests/test_a.py"], scope="all")
        self.assertNotEqual(proc.returncode, 0)


class TestCiWorkflowContract(unittest.TestCase):
    def test_ci_workflow_triggers_pins_and_jobs(self):
        with open(CI_WORKFLOW, encoding="utf-8") as fh:
            text = fh.read()

        self.assertIn("workflow_dispatch:", text)
        self.assertIn("concurrency:", text)
        self.assertIn("ci-${{ github.ref }}", text)
        self.assertIn("cancel-in-progress: true", text)

        expected_pins = {
            "actions/checkout": "3d3c42e5aac5ba805825da76410c181273ba90b1",
            "actions/upload-artifact": "cf430e030ddbb5b0abf93d22962f4752f3646cd9",
            "actions/download-artifact": "9000827ccba6bdab643e8b6fd33ac0654aef8333",
        }
        uses_matches = re.findall(r"uses:\s*([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)@([^\s#]+)", text)
        self.assertGreater(len(uses_matches), 0)
        for action, ref in uses_matches:
            self.assertIn(action, expected_pins, f"unexpected action {action}")
            self.assertEqual(ref, expected_pins[action], f"{action} not pinned to {expected_pins[action]}")

        for job in ("changes:", "test-leg:", "test:", "scan:", "docker:"):
            self.assertIn(job, text, f"missing job {job}")

        self.assertIn("scripts/ci/select-ci-plan.py", text)
        self.assertIn("scripts/ci/run-tests.py", text)
        self.assertIn("--workers 2", text)
        self.assertIn("fail-fast: false", text)
        self.assertIn("scripts/ci/check-test-legs.py", text)
        self.assertIn("!cancelled()", text)
        self.assertIn("needs.changes.outputs.run_docker", text)


_DOCKERKIT_FAKE_PREFIX = """
import atexit, json, os, subprocess, sys, unittest

sys.path.insert(0, sys.argv[1])
os.environ["DEV_CONTAINER_DOCKER_TESTS"] = "1"

import dockerkit

calls = []
built_images = set()

def fake_run(cmd, *args, **kwargs):
    cmd = list(cmd)
    env = kwargs.get("env") or {}
    stdout = ""
    stderr = ""
    rc = 0
    if cmd and cmd[0] == "docker":
        sub = cmd[1] if len(cmd) > 1 else ""
        if sub == "build":
            tag = None
            for i, tok in enumerate(cmd):
                if tok == "-t" and i + 1 < len(cmd):
                    tag = cmd[i + 1]
                    break
            if tag:
                built_images.add(tag)
            calls.append(("docker_build", tag, cmd))
        elif sub == "image" and len(cmd) > 2 and cmd[2] == "inspect":
            target = cmd[-1]
            rc = 0 if target in built_images else 1
            calls.append(("docker_image_inspect", target, rc))
        elif sub == "inspect":
            target = cmd[-1]
            rc = 0 if target in built_images else 1
            stdout = "[]" if rc == 0 else ""
            calls.append(("docker_inspect", target, rc))
        elif sub == "rmi":
            targets = [t for t in cmd[2:] if not t.startswith("-")]
            for t in targets:
                built_images.discard(t)
            calls.append(("docker_rmi", targets))
        elif sub == "rm":
            targets = [t for t in cmd[2:] if not t.startswith("-")]
            calls.append(("docker_rm", targets))
        elif sub == "run":
            if "stat" in cmd:
                stdout = "999\\n"
            calls.append(("docker_run", cmd))
        else:
            calls.append(("docker_other", cmd))
    elif len(cmd) >= 3 and cmd[0] == "/bin/sh" and cmd[1].endswith("scripts/engine") and cmd[2] == "install":
        img = env.get("DEV_CONTAINER_ENGINE_IMAGE", "dev-container-engine:local")
        built_images.add(img)
        calls.append(("engine_install", img))
    else:
        calls.append(("other", cmd))
    return subprocess.CompletedProcess(cmd, rc, stdout=stdout, stderr=stderr)

dockerkit.subprocess.run = fake_run
"""


class TestDockerkitImageCaching(unittest.TestCase):
    def _run_dockerkit_probe(self, body):
        proc = subprocess.run(
            [sys.executable, "-c", _DOCKERKIT_FAKE_PREFIX + body, DOCKER_TESTS_DIR],
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return json.loads(proc.stdout)

    def test_real_engine_case_shares_engine_image_and_isolates_per_test_state(self):
        body = """
before_cb = atexit._ncallbacks()
records = []
rmi_between_tests = []
rm_after_first = []

class SharedCase(dockerkit.RealEngineCase):
    def test_one(self):
        records.append({
            "image": self.image,
            "name": self.name,
            "tmp": self.tmp,
            "state": self.state,
            "call_idx": len(calls),
        })

    def test_two(self):
        # Inspect calls that happened between test_one setup and test_two setup.
        start = records[0]["call_idx"]
        for entry in calls[start:]:
            if entry[0] == "docker_rmi":
                rmi_between_tests.extend(entry[1])
            elif entry[0] == "docker_rm":
                rm_after_first.extend(entry[1])
        records.append({
            "image": self.image,
            "name": self.name,
            "tmp": self.tmp,
            "state": self.state,
            "call_idx": len(calls),
        })

suite = unittest.TestSuite([SharedCase("test_one"), SharedCase("test_two")])
res = unittest.TestResult()
suite.run(res)
assert res.wasSuccessful(), (res.failures, res.errors)

after_cb = atexit._ncallbacks()
all_rmi_targets = [t for entry in calls if entry[0] == "docker_rmi" for t in entry[1]]

print(json.dumps({
    "records": records,
    "rmi_between_tests": rmi_between_tests,
    "rm_after_first": rm_after_first,
    "callbacks_added": after_cb - before_cb,
    "all_rmi_targets": all_rmi_targets,
}))
"""
        data = self._run_dockerkit_probe(body)
        r1, r2 = data["records"]

        self.assertEqual(
            r1["image"],
            r2["image"],
            f"RealEngineCase should share engine image across tests, got {r1['image']} vs {r2['image']}",
        )
        self.assertNotEqual(r1["name"], r2["name"], "container names must be isolated per test")
        self.assertNotEqual(r1["tmp"], r2["tmp"], "temp directories must be isolated per test")
        self.assertNotEqual(r1["state"], r2["state"], "state directories must be isolated per test")
        self.assertIn(r1["name"], data["rm_after_first"], "per-test container should still be removed")
        self.assertNotIn(
            r1["image"],
            data["rmi_between_tests"],
            f"shared engine image {r1['image']} should not be rmi'd between tests",
        )
        # Shared engine image must be cleaned up via atexit or tearDownClass.
        self.assertTrue(
            data["callbacks_added"] >= 1 or r1["image"] in data["all_rmi_targets"],
            "shared engine image must register atexit cleanup or be removed in tearDownClass",
        )

    def test_real_engine_case_fresh_image_opt_out_builds_and_removes_per_test(self):
        body = """
has_attr = hasattr(dockerkit.RealEngineCase, "fresh_image")
default_val = getattr(dockerkit.RealEngineCase, "fresh_image", None)

shared_images = []
fresh_images = []

class DefaultCase(dockerkit.RealEngineCase):
    def test_shared(self):
        shared_images.append(self.image)

class FreshCase(dockerkit.RealEngineCase):
    fresh_image = True

    def test_fresh_one(self):
        fresh_images.append(self.image)

    def test_fresh_two(self):
        fresh_images.append(self.image)

suite = unittest.TestSuite([
    DefaultCase("test_shared"),
    FreshCase("test_fresh_one"),
    FreshCase("test_fresh_two"),
])
res = unittest.TestResult()
suite.run(res)
assert res.wasSuccessful(), (res.failures, res.errors)

all_rmi_targets = [t for entry in calls if entry[0] == "docker_rmi" for t in entry[1]]
print(json.dumps({
    "has_attr": has_attr,
    "default_val": default_val,
    "shared_images": shared_images,
    "fresh_images": fresh_images,
    "all_rmi_targets": all_rmi_targets,
}))
"""
        data = self._run_dockerkit_probe(body)
        self.assertTrue(data["has_attr"], "RealEngineCase must define fresh_image opt-out attribute")
        self.assertFalse(data["default_val"], "RealEngineCase.fresh_image must default to False")
        f1, f2 = data["fresh_images"]
        shared = data["shared_images"][0]
        self.assertNotEqual(f1, f2, "fresh_image=True tests must build distinct images per test")
        self.assertNotEqual(f1, shared, "fresh_image=True must not reuse the shared image")
        self.assertNotEqual(f2, shared, "fresh_image=True must not reuse the shared image")
        self.assertIn(f1, data["all_rmi_targets"], "fresh_image=True must rmi per-test image on cleanup")
        self.assertIn(f2, data["all_rmi_targets"], "fresh_image=True must rmi per-test image on cleanup")

    def test_docker_test_suites_reuse_shared_fixture_image(self):
        body = """
import test_overlay
import test_volume_lifecycle

def run_pair(base_cls):
    start_idx = len(calls)
    fixtures = []
    rmi_between = []

    class Probe(base_cls):
        def test_a(self):
            fixtures.append((self.fixture, len(calls)))

        def test_b(self):
            for entry in calls[fixtures[0][1]:]:
                if entry[0] == "docker_rmi":
                    rmi_between.extend(entry[1])
            fixtures.append((self.fixture, len(calls)))

    suite = unittest.TestSuite([Probe("test_a"), Probe("test_b")])
    res = unittest.TestResult()
    suite.run(res)
    assert res.wasSuccessful(), (res.failures, res.errors)

    fixture_builds = [
        entry for entry in calls[start_idx:]
        if entry[0] == "docker_build" and dockerkit.FIXTURE_DIR in entry[2]
    ]
    return {
        "fixtures": [f for f, _ in fixtures],
        "fixture_builds": len(fixture_builds),
        "rmi_between": rmi_between,
    }

out = {
    "overlay": run_pair(test_overlay.OverlayBuildsOnFixtureProjects),
    "volume": run_pair(test_volume_lifecycle.TestVolumeLifecycle),
}
print(json.dumps(out))
"""
        data = self._run_dockerkit_probe(body)
        for suite_name, info in data.items():
            with self.subTest(suite=suite_name):
                f1, f2 = info["fixtures"]
                self.assertEqual(
                    f1,
                    f2,
                    f"{suite_name} should reuse cached fixture image across tests, got {f1} vs {f2}",
                )
                self.assertLessEqual(
                    info["fixture_builds"],
                    1,
                    f"{suite_name} rebuilt fixture image {info['fixture_builds']} times across 2 tests",
                )
                self.assertNotIn(
                    f1,
                    info["rmi_between"],
                    f"{suite_name} rmi'd shared fixture image {f1} between tests",
                )

