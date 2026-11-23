"""Tests for CI plan selection, sharded test runner, leg completeness guard,
workflow contract, and template-cached DevContainerHarness isolation (#55).
"""

import atexit
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TESTS_DIR)
if TESTS_DIR not in sys.path:
    sys.path.insert(0, TESTS_DIR)

import harness

RUN_TESTS = os.path.join(ROOT, "scripts", "ci", "run-tests.py")
SELECT_PLAN = os.path.join(ROOT, "scripts", "ci", "select-ci-plan.py")
CHECK_LEGS = os.path.join(ROOT, "scripts", "ci", "check-test-legs.py")
CI_WORKFLOW = os.path.join(ROOT, ".github", "workflows", "ci.yml")


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

    def test_writes_to_github_output_when_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            gh_out = os.path.join(tmp, "github_output.txt")
            out = self._run_plan(["README.md"], event_name="pull_request", github_output=gh_out)
            self.assertEqual(out.get("all"), "false")
            self.assertEqual(out.get("run_docker"), "false")


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
