import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import unittest

if os.path.dirname(__file__) not in sys.path:
    sys.path.insert(0, os.path.dirname(__file__))

from harness import (
    DevContainerHarness,
    DevContainerTestCase,
    PROJECT_PREFIX,
    project_env_text,
)

MAIN_ENV = (
    "GIT_USER_NAME=demo-agent\n"
    "GIT_USER_EMAIL=demo-agent@example.com\n"
)

DOCKER_CAPTURE_WRAPPER = """#!/usr/bin/env bash
set -u
here="$(dirname "$0")"
if [[ -n "${DOCKER_CAPTURE_DIR:-}" ]]; then
  idx=0
  while [[ -e "$DOCKER_CAPTURE_DIR/$idx.argv" ]]; do
    idx=$((idx + 1))
  done
  printf '%s\\n' "$@" > "$DOCKER_CAPTURE_DIR/$idx.argv"
  env > "$DOCKER_CAPTURE_DIR/$idx.env"
  if [[ "${1:-}" == "run" ]] || [[ "${1:-}" == "compose" && " $* " == *" exec "* ]] || [[ "${1:-}" == "compose" && " $* " == *" run "* ]]; then
    cat > "$DOCKER_CAPTURE_DIR/$idx.stdin"
    exec "$here/docker-base" "$@" < "$DOCKER_CAPTURE_DIR/$idx.stdin"
  fi
fi
exec "$here/docker-base" "$@"
"""


def sha256_hex(data):
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


class SlotFsTestCase(DevContainerTestCase):

    def _install_docker_capture_wrapper(self):
        docker_bin = os.path.join(self.harness.stub_bin, "docker")
        docker_base = os.path.join(self.harness.stub_bin, "docker-base")
        if not os.path.exists(docker_base):
            os.rename(docker_bin, docker_base)
            self.harness._write_stub("docker", DOCKER_CAPTURE_WRAPPER)

    def build_repo(self, extra_files=None, slot_copy_files=None, untracked_files=None):
        self._install_docker_capture_wrapper()
        overrides = {}
        if slot_copy_files is not None:
            overrides["SLOT_COPY_FILES"] = slot_copy_files
        proj_env = project_env_text(**overrides)
        self.harness.build_fixture_repo(with_parent_env=MAIN_ENV, project_env=proj_env)
        if extra_files:
            for rel_path, content in extra_files.items():
                full = os.path.join(self.harness.fixture_repo, rel_path)
                os.makedirs(os.path.dirname(full), exist_ok=True)
                mode = "wb" if isinstance(content, bytes) else "w"
                kwargs = {} if isinstance(content, bytes) else {"encoding": "utf-8"}
                with open(full, mode, **kwargs) as handle:
                    handle.write(content)
            git = self.harness.real_git_bin
            subprocess.run(
                [git, "-C", self.harness.fixture_repo, "add", "-f", "--", *extra_files.keys()],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            subprocess.run(
                [git, "-C", self.harness.fixture_repo, "commit", "-q", "-m", "seed test files"],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        if untracked_files:
            for rel_path, content in untracked_files.items():
                full = os.path.join(self.harness.fixture_repo, rel_path)
                os.makedirs(os.path.dirname(full), exist_ok=True)
                with open(full, "w", encoding="utf-8") as handle:
                    handle.write(content)

    def acquire_slot(self, holder="fs-holder"):
        res = self.harness.run("acquire", "--for", holder)
        self.assertEqual(res.returncode, 0, res.stderr.decode())
        return self.harness.acquired_slot_name(res)

    def token_path(self, name):
        sid = f"{PROJECT_PREFIX}-{name}"
        return os.path.join(self.harness.state_dir, "lease-tokens", f"{sid}.token")

    def read_lease_token(self, name):
        path = self.token_path(name)
        self.assertTrue(os.path.isfile(path), f"missing lease token file: {path}")
        with open(path, encoding="utf-8") as handle:
            return handle.read().strip()

    def run_fs(self, op, slot, payload, capture_dir=None, harness=None):
        target = harness or self.harness
        overrides = {"DOCKER_CAPTURE_DIR": capture_dir} if capture_dir else None
        raw = json.dumps(payload).encode("utf-8")
        return target.run("fs", op, slot, "--stdin-json", port_overrides=overrides, input=raw)

    def assert_fs_ok(self, result, context=""):
        stdout = result.stdout.decode("utf-8", errors="replace")
        stderr = result.stderr.decode("utf-8", errors="replace")
        msg = f"rc={result.returncode}\nstdout={stdout}\nstderr={stderr}\n{context}"
        self.assertEqual(result.returncode, 0, msg)
        data = json.loads(stdout)
        self.assertIs(data.get("ok"), True, msg)
        return data

    def assert_fs_refused(self, result, context=""):
        stdout = result.stdout.decode("utf-8", errors="replace").strip()
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        msg = f"rc={result.returncode}\nstdout={stdout}\nstderr={stderr}\n{context}"
        self.assertNotIn("Usage:", stdout + stderr, msg)
        self.assertNotIn("unknown command", (stdout + stderr).lower(), msg)
        parsed = None
        if stdout.startswith("{"):
            try:
                parsed = json.loads(stdout)
            except json.JSONDecodeError:
                parsed = None
        if parsed is not None and "ok" in parsed:
            self.assertIs(parsed["ok"], False, msg)
            self.assertTrue(parsed.get("error"), msg)
        else:
            self.assertNotEqual(result.returncode, 0, msg)
            self.assertTrue(stderr or stdout, msg)


class LeaseTokenLifecycle(SlotFsTestCase):

    def test_acquire_writes_sha256_in_lease_and_0600_raw_token_in_tokens_dir(self):
        self.build_repo()
        name1 = self.acquire_slot("holder-1")
        lease_file = self.harness.lease_path(name1)
        tok_file = self.token_path(name1)

        self.assertTrue(os.path.isfile(tok_file), f"token file not created at {tok_file}")
        mode = stat.S_IMODE(os.stat(tok_file).st_mode)
        self.assertEqual(mode, 0o600, f"expected 0600 on {tok_file}, got {oct(mode)}")

        raw_token = self.read_lease_token(name1)
        self.assertRegex(raw_token, r"^[0-9a-f]{64}$")

        with open(lease_file, encoding="utf-8") as handle:
            raw_lease_text = handle.read()
        lease_json = json.loads(raw_lease_text)

        self.assertNotIn(raw_token, raw_lease_text, "raw lease_token leaked into leases/<sid>.json")
        self.assertNotIn("lease_token", lease_json)
        digest = lease_json.get("lease_token_sha256")
        self.assertIsNotNone(digest, "lease_token_sha256 missing from leases/<sid>.json")
        self.assertRegex(digest, r"^[0-9a-f]{64}$")
        self.assertEqual(sha256_hex(raw_token), digest)

        name2 = self.acquire_slot("holder-2")
        self.assertNotEqual(raw_token, self.read_lease_token(name2))

    def test_release_deletes_lease_token_file(self):
        self.build_repo()
        name = self.acquire_slot("holder-rel")
        tok_file = self.token_path(name)
        self.assertTrue(os.path.isfile(tok_file))

        rel = self.harness.run("release", name, "--for", "holder-rel")
        self.assertEqual(rel.returncode, 0, rel.stderr.decode())
        self.assertFalse(os.path.exists(tok_file), f"token file remained after release: {tok_file}")


class SlotFsReadOnlyOps(SlotFsTestCase):

    def test_fs_read_returns_content_lines_sha256_and_supports_offset_and_limit(self):
        body = "line-1\nline-2\nline-3\nline-4\n"
        self.build_repo(extra_files={"README.md": body})
        name = self.acquire_slot()

        full = self.assert_fs_ok(self.run_fs("read", name, {"path": "README.md"}))
        self.assertEqual(full.get("content"), body)
        self.assertEqual(full.get("total_lines"), 4)
        self.assertEqual(full.get("sha256"), sha256_hex(body))

        sliced = self.assert_fs_ok(
            self.run_fs(
                "read",
                name,
                {"path": "README.md", "offset_line": 2, "limit_lines": 2},
            )
        )
        self.assertEqual(sliced.get("content", "").splitlines(), ["line-2", "line-3"])
        self.assertEqual(sliced.get("total_lines"), 4)
        self.assertEqual(sliced.get("sha256"), sha256_hex(body))

    def test_fs_glob_returns_allowed_relative_paths_and_excludes_protected_files(self):
        self.build_repo(
            extra_files={
                "notes.txt": "top\n",
                "src/nested/alpha.txt": "nested\n",
                ".env.example": "SAFE=1\n",
            },
            slot_copy_files="config/master.key",
            untracked_files={"config/master.key": "secret-master-key\n"},
        )
        name = self.acquire_slot()
        slot_dir = self.harness.slot_dir(name)
        for rel, text in (
            (".env", "SECRET=1\n"),
            (".env.local", "SECRET_LOCAL=1\n"),
            (".devcontainer/secret.txt", "DC_SECRET=1\n"),
            (".git/extra.txt", "GIT_INTERNAL=1\n"),
        ):
            target = os.path.join(slot_dir, rel)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "w", encoding="utf-8") as handle:
                handle.write(text)

        txt_glob = self.assert_fs_ok(self.run_fs("glob", name, {"pattern": "**/*.txt"}))
        txt_paths = txt_glob.get("paths", [])
        self.assertIn("notes.txt", txt_paths)
        self.assertIn("src/nested/alpha.txt", txt_paths)
        for p in txt_paths:
            self.assertFalse(p.startswith(".git/") or p == ".git", p)
            self.assertFalse(p.startswith(".devcontainer/") or p == ".devcontainer", p)

        all_glob = self.assert_fs_ok(self.run_fs("glob", name, {"pattern": "**/*"}))
        env_glob = self.assert_fs_ok(self.run_fs("glob", name, {"pattern": ".env*"}))
        combined = set(all_glob.get("paths", [])) | set(env_glob.get("paths", []))

        self.assertIn("notes.txt", combined)
        self.assertIn("src/nested/alpha.txt", combined)
        self.assertIn(".env.example", combined)

        for forbidden in (".env", ".env.local", "project.env", "config/master.key"):
            self.assertNotIn(forbidden, combined)
        for p in combined:
            self.assertFalse(p.startswith(".git/"), f".git leaked in glob: {p}")
            self.assertFalse(p.startswith(".devcontainer/"), f".devcontainer leaked in glob: {p}")

    def test_fs_grep_returns_matches_and_never_searches_git_env_or_slot_copy_files(self):
        needle = "UNIQUE_GREP_NEEDLE_99"
        self.build_repo(
            extra_files={
                "src/app.txt": f"header\nfound {needle} here\nfooter\n",
                ".env.example": f"EXAMPLE={needle}\n",
            },
            slot_copy_files="config/master.key",
            untracked_files={"config/master.key": f"MASTER={needle}\n"},
        )
        name = self.acquire_slot()
        slot_dir = self.harness.slot_dir(name)
        for rel in (
            ".env",
            ".env.local",
            ".devcontainer/agent.env",
            ".git/info/exclude",
        ):
            target = os.path.join(slot_dir, rel)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "a", encoding="utf-8") as handle:
                handle.write(f"\n{needle}\n")
        with open(os.path.join(slot_dir, "project.env"), "a", encoding="utf-8") as handle:
            handle.write(f"# {needle}\n")

        res = self.assert_fs_ok(self.run_fs("grep", name, {"pattern": needle}))
        self.assertIs(res.get("truncated"), False)
        matches = res.get("matches", [])
        app_matches = [m for m in matches if m.get("path") == "src/app.txt"]
        self.assertEqual(len(app_matches), 1, matches)
        self.assertEqual(app_matches[0].get("line"), 2)
        self.assertIn(needle, app_matches[0].get("text", ""))

        matched_paths = {m.get("path", "") for m in matches}
        for forbidden in (".env", ".env.local", "project.env", "config/master.key"):
            self.assertNotIn(forbidden, matched_paths)
        for p in matched_paths:
            self.assertFalse(p.startswith(".git/"), f".git match leaked: {p}")
            self.assertFalse(p.startswith(".devcontainer/"), f".devcontainer match leaked: {p}")

    def test_fs_diff_returns_git_diff_after_tracked_file_modification(self):
        self.build_repo(extra_files={"tracked.txt": "before-change\n"})
        name = self.acquire_slot()
        slot_dir = self.harness.slot_dir(name)
        with open(os.path.join(slot_dir, "tracked.txt"), "w", encoding="utf-8") as handle:
            handle.write("after-change\n")

        res = self.assert_fs_ok(self.run_fs("diff", name, {"paths": []}))
        diff_text = res.get("diff", "")
        self.assertIn("tracked.txt", diff_text)
        self.assertIn("-before-change", diff_text)
        self.assertIn("+after-change", diff_text)

    def test_fs_diff_excludes_tracked_env_files_even_under_directory_paths_and_refuses_symlinks(self):
        self.build_repo(
            extra_files={
                ".env.local": "LOCAL_OLD=1\n",
                "src/.env.production": "PROD_OLD=1\n",
                "src/code.txt": "code-before\n",
                "src/[id]/route.txt": "route-before\n",
                ".env.example": "EXAMPLE_OLD=1\n",
            },
            slot_copy_files="config/master.key",
            untracked_files={"config/master.key": "MASTER_KEY_SECRET=1\n"},
        )
        name = self.acquire_slot()
        slot_dir = self.harness.slot_dir(name)
        for rel, content in (
            (".env.local", "LOCAL_SECRET_NEW=1\n"),
            ("src/.env.production", "PROD_SECRET_NEW=1\n"),
            ("src/code.txt", "code-after\n"),
            ("src/[id]/route.txt", "route-after\n"),
            (".env.example", "EXAMPLE_NEW=1\n"),
        ):
            with open(os.path.join(slot_dir, rel), "w", encoding="utf-8") as handle:
                handle.write(content)

        outside_dir = os.path.join(self.harness.tmp, "outside_diff_dir")
        os.makedirs(outside_dir, exist_ok=True)
        os.symlink("/etc/hosts", os.path.join(slot_dir, "src", "hosts_link"))
        os.symlink(outside_dir, os.path.join(slot_dir, "link_dir"))

        all_res = self.assert_fs_ok(self.run_fs("diff", name, {"paths": []}))
        self.assertIs(all_res.get("truncated"), False)
        all_diff = all_res.get("diff", "")
        self.assertIn("src/code.txt", all_diff)
        self.assertIn("+code-after", all_diff)
        self.assertIn("src/[id]/route.txt", all_diff)
        self.assertIn("+route-after", all_diff)
        self.assertIn(".env.example", all_diff)
        self.assertIn("+EXAMPLE_NEW=1", all_diff)
        for secret in (
            ".env.local",
            "LOCAL_OLD",
            "LOCAL_SECRET_NEW",
            ".env.production",
            "PROD_OLD",
            "PROD_SECRET_NEW",
            "MASTER_KEY_SECRET",
        ):
            self.assertNotIn(secret, all_diff)

        src_res = self.assert_fs_ok(self.run_fs("diff", name, {"paths": ["src"]}))
        self.assertIs(src_res.get("truncated"), False)
        src_diff = src_res.get("diff", "")
        self.assertIn("src/code.txt", src_diff)
        self.assertIn("+code-after", src_diff)
        self.assertNotIn(".env.example", src_diff)
        for secret in (".env.production", "PROD_OLD", "PROD_SECRET_NEW"):
            self.assertNotIn(secret, src_diff)

        for bad_paths in (
            [".env.local"],
            ["src/.env.production"],
            ["config/master.key"],
            ["src/hosts_link"],
            ["link_dir"],
            ["../etc/passwd"],
        ):
            self.assert_fs_refused(self.run_fs("diff", name, {"paths": bad_paths}), str(bad_paths))

    def test_fs_diff_supports_cached_and_truncates_oversized_diff_cleanly(self):
        self.build_repo(
            extra_files={
                "staged.txt": "staged-before\n",
                "unstaged.txt": "unstaged-before\n",
                ".env.local": "STAGED_ENV_OLD=1\n",
                "big.txt": "small\n",
            }
        )
        name = self.acquire_slot()
        slot_dir = self.harness.slot_dir(name)

        with open(os.path.join(slot_dir, "staged.txt"), "w", encoding="utf-8") as handle:
            handle.write("staged-after\n")
        with open(os.path.join(slot_dir, ".env.local"), "w", encoding="utf-8") as handle:
            handle.write("STAGED_ENV_SECRET=1\n")
        subprocess.run(
            [self.harness.real_git_bin, "-C", slot_dir, "add", "-f", "--", "staged.txt", ".env.local"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        with open(os.path.join(slot_dir, "unstaged.txt"), "w", encoding="utf-8") as handle:
            handle.write("unstaged-after\n")

        cached_res = self.assert_fs_ok(self.run_fs("diff", name, {"cached": True}))
        self.assertIs(cached_res.get("truncated"), False)
        cached_diff = cached_res.get("diff", "")
        self.assertIn("staged.txt", cached_diff)
        self.assertIn("+staged-after", cached_diff)
        self.assertNotIn("unstaged.txt", cached_diff)
        self.assertNotIn("+unstaged-after", cached_diff)
        for secret in (".env.local", "STAGED_ENV_OLD", "STAGED_ENV_SECRET"):
            self.assertNotIn(secret, cached_diff)

        unstaged_res = self.assert_fs_ok(self.run_fs("diff", name, {"cached": False}))
        self.assertIs(unstaged_res.get("truncated"), False)
        unstaged_diff = unstaged_res.get("diff", "")
        self.assertIn("unstaged.txt", unstaged_diff)
        self.assertIn("+unstaged-after", unstaged_diff)
        self.assertNotIn("+staged-after", unstaged_diff)

        with open(os.path.join(slot_dir, "big.txt"), "w", encoding="utf-8") as handle:
            handle.write("\u00e9" * 80000 + "\n")

        big_res = self.assert_fs_ok(self.run_fs("diff", name, {"paths": ["big.txt"]}))
        self.assertIs(big_res.get("truncated"), True)
        big_diff = big_res.get("diff", "")
        self.assertGreater(len(big_diff.encode("utf-8")), 0)
        self.assertLessEqual(len(big_diff.encode("utf-8")), 131072)
        self.assertNotIn("\ufffd", big_diff)

        capped_res = self.assert_fs_ok(
            self.run_fs("diff", name, {"paths": ["unstaged.txt"], "max_bytes": 32})
        )
        self.assertIs(capped_res.get("truncated"), True)
        capped_bytes = capped_res.get("diff", "").encode("utf-8")
        self.assertGreater(len(capped_bytes), 0)
        self.assertLessEqual(len(capped_bytes), 32)

    def test_fs_grep_and_glob_support_contract_fields_fixed_strings_path_and_max_results(self):
        self.build_repo(
            extra_files={
                "top.txt": "call foo(0) at root\n",
                "src/a.txt": "first foo(1)\nsecond foo(2)\n",
                "src/b.py": "third foo(3)\n",
                "src/.env.local": "secret foo(99)\n",
            }
        )
        name = self.acquire_slot()
        slot_dir = self.harness.slot_dir(name)
        os.symlink("/etc/hosts", os.path.join(slot_dir, "hosts_link"))

        self.assert_fs_refused(self.run_fs("grep", name, {"pattern": "foo("}))

        scoped_one = self.assert_fs_ok(
            self.run_fs(
                "grep",
                name,
                {"pattern": "foo(", "fixed_strings": True, "path": "src", "max_results": 1},
            )
        )
        self.assertIs(scoped_one.get("truncated"), True)
        self.assertEqual(len(scoped_one.get("matches", [])), 1)
        self.assertEqual(scoped_one["matches"][0].get("path"), "src/a.txt")
        self.assertIn("foo(1)", scoped_one["matches"][0].get("text", ""))

        scoped_all = self.assert_fs_ok(
            self.run_fs(
                "grep",
                name,
                {"pattern": "foo(", "fixed_strings": True, "path": "src", "max_results": 10},
            )
        )
        self.assertIs(scoped_all.get("truncated"), False)
        self.assertEqual(
            [m.get("path") for m in scoped_all.get("matches", [])],
            ["src/a.txt", "src/a.txt", "src/b.py"],
        )

        file_scoped = self.assert_fs_ok(
            self.run_fs("grep", name, {"pattern": "foo(", "fixed_strings": True, "path": "top.txt"})
        )
        self.assertIs(file_scoped.get("truncated"), False)
        self.assertEqual([m.get("path") for m in file_scoped.get("matches", [])], ["top.txt"])

        alias_res = self.assert_fs_ok(
            self.run_fs(
                "grep",
                name,
                {"pattern": "foo(", "is_literal": True, "path": "src", "max_matches": 2},
            )
        )
        self.assertIs(alias_res.get("truncated"), True)
        self.assertEqual(len(alias_res.get("matches", [])), 2)

        glob_scoped = self.assert_fs_ok(
            self.run_fs(
                "grep",
                name,
                {"pattern": "foo(", "fixed_strings": True, "path": "src", "glob": "*.py"},
            )
        )
        self.assertIs(glob_scoped.get("truncated"), False)
        self.assertEqual([m.get("path") for m in glob_scoped.get("matches", [])], ["src/b.py"])

        for bad_path in ("../etc/passwd", "/etc/passwd", "src/.env.local", ".git", "hosts_link"):
            self.assert_fs_refused(
                self.run_fs("grep", name, {"pattern": "foo(", "fixed_strings": True, "path": bad_path}),
                bad_path,
            )

        glob_capped = self.assert_fs_ok(
            self.run_fs("glob", name, {"pattern": "**/*", "max_results": 2})
        )
        self.assertIs(glob_capped.get("truncated"), True)
        self.assertEqual(len(glob_capped.get("paths", [])), 2)

        glob_default = self.assert_fs_ok(
            self.run_fs("glob", name, {"pattern": "**/*"})
        )
        self.assertIs(glob_default.get("truncated"), False)
        self.assertGreater(len(glob_default.get("paths", [])), 2)

        glob_under_cap = self.assert_fs_ok(
            self.run_fs("glob", name, {"pattern": "src/*", "max_results": 10})
        )
        self.assertIs(glob_under_cap.get("truncated"), False)
        self.assertEqual(glob_under_cap.get("paths"), ["src/a.txt", "src/b.py"])


class SlotFsWriteAndEditOps(SlotFsTestCase):

    def test_missing_or_wrong_lease_token_on_write_and_edit_refuses_without_modifying_file(self):
        self.build_repo(extra_files={"target.txt": "clean-state\n"})
        name = self.acquire_slot()
        tok = self.read_lease_token(name)
        wrong_tok = ("0" if tok[0] != "0" else "1") + tok[1:]
        target_path = os.path.join(self.harness.slot_dir(name), "target.txt")

        for bad_payload in (
            {"path": "target.txt", "content": "tampered\n"},
            {"lease_token": "", "path": "target.txt", "content": "tampered\n"},
            {"lease_token": wrong_tok, "path": "target.txt", "content": "tampered\n"},
        ):
            self.assert_fs_refused(self.run_fs("write", name, bad_payload))
            with open(target_path, encoding="utf-8") as handle:
                self.assertEqual(handle.read(), "clean-state\n")

        for bad_edit in (
            {"path": "target.txt", "old_string": "clean-state", "new_string": "tampered"},
            {"lease_token": "", "path": "target.txt", "old_string": "clean-state", "new_string": "tampered"},
            {"lease_token": wrong_tok, "path": "target.txt", "old_string": "clean-state", "new_string": "tampered"},
        ):
            self.assert_fs_refused(self.run_fs("edit", name, bad_edit))
            with open(target_path, encoding="utf-8") as handle:
                self.assertEqual(handle.read(), "clean-state\n")

        ok_write = self.assert_fs_ok(
            self.run_fs("write", name, {"lease_token": tok, "path": "target.txt", "content": "updated\n"})
        )
        self.assertEqual(ok_write.get("bytes"), 8)

    def test_valid_write_creates_parent_dir_and_never_exposes_lease_token_to_container(self):
        self.build_repo()
        name = self.acquire_slot()
        tok = self.read_lease_token(name)
        cap_dir = os.path.join(self.harness.tmp, "docker-cap-write")
        os.makedirs(cap_dir, exist_ok=True)

        res = self.assert_fs_ok(
            self.run_fs(
                "write",
                name,
                {"lease_token": tok, "path": "src/new.txt", "content": "hello\n"},
                capture_dir=cap_dir,
            )
        )
        self.assertEqual(res.get("bytes"), 6)
        self.assertEqual(res.get("sha256"), sha256_hex("hello\n"))

        written = os.path.join(self.harness.slot_dir(name), "src", "new.txt")
        self.assertTrue(os.path.isfile(written))
        with open(written, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "hello\n")

        captured_files = sorted(os.listdir(cap_dir))
        stdin_files = [f for f in captured_files if f.endswith(".stdin")]
        self.assertTrue(stdin_files, "expected at least one container invocation capturing stdin")
        for fname in captured_files:
            with open(os.path.join(cap_dir, fname), encoding="utf-8", errors="replace") as handle:
                body = handle.read()
            self.assertNotIn(tok, body, f"raw lease_token leaked into container {fname}")
            if fname.endswith(".stdin"):
                self.assertNotIn("lease_token", body, f"lease_token key not stripped from container {fname}")

    def test_expected_sha256_cas_enforced_on_write_and_edit(self):
        self.build_repo()
        name = self.acquire_slot()
        tok = self.read_lease_token(name)
        file_path = os.path.join(self.harness.slot_dir(name), "cas.txt")

        v1 = "version-1\n"
        sha1 = sha256_hex(v1)
        self.assert_fs_ok(
            self.run_fs("write", name, {"lease_token": tok, "path": "cas.txt", "content": v1})
        )

        self.assert_fs_refused(
            self.run_fs(
                "write",
                name,
                {
                    "lease_token": tok,
                    "path": "cas.txt",
                    "content": "stale-write\n",
                    "expected_sha256": "f" * 64,
                },
            )
        )
        with open(file_path, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), v1)

        v2 = "version-2\n"
        sha2 = sha256_hex(v2)
        w_ok = self.assert_fs_ok(
            self.run_fs(
                "write",
                name,
                {
                    "lease_token": tok,
                    "path": "cas.txt",
                    "content": v2,
                    "expected_sha256": sha1,
                },
            )
        )
        self.assertEqual(w_ok.get("sha256"), sha256_hex(v2))

        self.assert_fs_refused(
            self.run_fs(
                "edit",
                name,
                {
                    "lease_token": tok,
                    "path": "cas.txt",
                    "old_string": "version-2",
                    "new_string": "stale-edit",
                    "expected_sha256": sha1,
                },
            )
        )
        with open(file_path, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), v2)

        v3 = "version-3\n"
        e_ok = self.assert_fs_ok(
            self.run_fs(
                "edit",
                name,
                {
                    "lease_token": tok,
                    "path": "cas.txt",
                    "old_string": "version-2",
                    "new_string": "version-3",
                    "expected_sha256": sha2,
                },
            )
        )
        self.assertEqual(e_ok.get("replacements"), 1)
        self.assertEqual(e_ok.get("sha256"), sha256_hex(v3))
        with open(file_path, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), v3)

    def test_edit_replaces_exact_unique_match_and_supports_replace_all(self):
        self.build_repo(extra_files={"doc.txt": "alpha\nbeta\ngamma\n"})
        name = self.acquire_slot()
        tok = self.read_lease_token(name)
        doc_path = os.path.join(self.harness.slot_dir(name), "doc.txt")

        one = self.assert_fs_ok(
            self.run_fs(
                "edit",
                name,
                {
                    "lease_token": tok,
                    "path": "doc.txt",
                    "old_string": "beta",
                    "new_string": "BETA",
                },
            )
        )
        self.assertEqual(one.get("replacements"), 1)
        self.assertEqual(one.get("sha256"), sha256_hex("alpha\nBETA\ngamma\n"))

        self.assert_fs_refused(
            self.run_fs(
                "edit",
                name,
                {
                    "lease_token": tok,
                    "path": "doc.txt",
                    "old_string": "missing-token",
                    "new_string": "x",
                },
            )
        )

        dup_initial = "item one\nitem two\n"
        self.assert_fs_ok(
            self.run_fs("write", name, {"lease_token": tok, "path": "dup.txt", "content": dup_initial})
        )
        dup_path = os.path.join(self.harness.slot_dir(name), "dup.txt")

        self.assert_fs_refused(
            self.run_fs(
                "edit",
                name,
                {
                    "lease_token": tok,
                    "path": "dup.txt",
                    "old_string": "item",
                    "new_string": "entry",
                    "replace_all": False,
                },
            )
        )
        with open(dup_path, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), dup_initial)

        both = self.assert_fs_ok(
            self.run_fs(
                "edit",
                name,
                {
                    "lease_token": tok,
                    "path": "dup.txt",
                    "old_string": "item",
                    "new_string": "entry",
                    "replace_all": True,
                },
            )
        )
        self.assertEqual(both.get("replacements"), 2)
        self.assertEqual(both.get("sha256"), sha256_hex("entry one\nentry two\n"))
        with open(dup_path, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "entry one\nentry two\n")


class SlotFsSecurityBoundaries(SlotFsTestCase):

    def test_path_traversal_and_absolute_paths_refused_on_read_write_and_edit(self):
        self.build_repo(extra_files={"safe/file.txt": "safe\n"})
        name = self.acquire_slot()
        tok = self.read_lease_token(name)

        self.assert_fs_ok(self.run_fs("read", name, {"path": "safe/file.txt"}))

        for bad_path in (
            "../etc/passwd",
            "safe/../../etc/passwd",
            "/etc/passwd",
            "/workspace/safe/file.txt",
        ):
            self.assert_fs_refused(self.run_fs("read", name, {"path": bad_path}), bad_path)
            self.assert_fs_refused(
                self.run_fs("write", name, {"lease_token": tok, "path": bad_path, "content": "x\n"}),
                bad_path,
            )
            self.assert_fs_refused(
                self.run_fs(
                    "edit",
                    name,
                    {"lease_token": tok, "path": bad_path, "old_string": "root", "new_string": "x"},
                ),
                bad_path,
            )

    def test_symlink_escape_o_nofollow_refused_for_leaf_and_intermediate_dir(self):
        self.build_repo(extra_files={"real.txt": "real-content\n"})
        name = self.acquire_slot()
        tok = self.read_lease_token(name)
        slot_dir = self.harness.slot_dir(name)

        self.assert_fs_ok(self.run_fs("read", name, {"path": "real.txt"}))

        outside_file = os.path.join(self.harness.tmp, "outside_secret.txt")
        with open(outside_file, "w", encoding="utf-8") as handle:
            handle.write("outside-untouched\n")

        outside_dir = os.path.join(self.harness.tmp, "outside_dir")
        os.makedirs(outside_dir, exist_ok=True)
        outside_sub_file = os.path.join(outside_dir, "file.txt")
        with open(outside_sub_file, "w", encoding="utf-8") as handle:
            handle.write("sub-untouched\n")

        os.symlink("/etc/hosts", os.path.join(slot_dir, "hosts_link"))
        os.symlink(outside_file, os.path.join(slot_dir, "evil_link"))
        os.symlink(outside_dir, os.path.join(slot_dir, "sub"))
        os.symlink(".git/config", os.path.join(slot_dir, "git_alias"))

        for sym_path in ("hosts_link", "evil_link", "sub/file.txt", "sub/deep/new.txt", "git_alias"):
            self.assert_fs_refused(self.run_fs("read", name, {"path": sym_path}), sym_path)
            self.assert_fs_refused(
                self.run_fs("write", name, {"lease_token": tok, "path": sym_path, "content": "escaped\n"}),
                sym_path,
            )
            self.assert_fs_refused(
                self.run_fs(
                    "edit",
                    name,
                    {
                        "lease_token": tok,
                        "path": sym_path,
                        "old_string": "untouched",
                        "new_string": "escaped",
                    },
                ),
                sym_path,
            )

        with open(outside_file, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "outside-untouched\n")
        with open(outside_sub_file, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "sub-untouched\n")
        self.assertFalse(os.path.exists(os.path.join(outside_dir, "deep")))

    def test_forbidden_paths_refused_while_env_example_is_allowed_on_read_write_and_edit(self):
        self.build_repo(
            extra_files={".env.example": "API_URL=http://localhost:3000\n"},
            slot_copy_files="config/master.key",
            untracked_files={"config/master.key": "super-secret-master-key\n"},
        )
        name = self.acquire_slot()
        tok = self.read_lease_token(name)
        slot_dir = self.harness.slot_dir(name)

        for rel, content in (
            (".env", "LIVE_SECRET=1\n"),
            (".env.local", "LOCAL_SECRET=1\n"),
            (".devcontainer/agent.env", "AGENT_SECRET=1\n"),
        ):
            full = os.path.join(slot_dir, rel)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w", encoding="utf-8") as handle:
                handle.write(content)

        ex_read = self.assert_fs_ok(self.run_fs("read", name, {"path": ".env.example"}))
        self.assertIn("API_URL=", ex_read.get("content", ""))
        self.assert_fs_ok(
            self.run_fs(
                "edit",
                name,
                {
                    "lease_token": tok,
                    "path": ".env.example",
                    "old_string": "3000",
                    "new_string": "4000",
                },
            )
        )
        self.assert_fs_ok(
            self.run_fs(
                "write",
                name,
                {"lease_token": tok, "path": ".env.example", "content": "API_URL=http://localhost:5000\n"},
            )
        )

        forbidden_paths = (
            ".git/config",
            ".git/HEAD",
            ".env",
            ".env.local",
            "project.env",
            ".devcontainer/agent.env",
            "config/master.key",
        )
        snapshots = {}
        for rel in forbidden_paths:
            with open(os.path.join(slot_dir, rel), encoding="utf-8") as handle:
                snapshots[rel] = handle.read()

        for rel in forbidden_paths:
            r_res = self.run_fs("read", name, {"path": rel})
            self.assert_fs_refused(r_res, rel)
            self.assertNotIn("super-secret-master-key", r_res.stdout.decode("utf-8", errors="replace"))

            first_token = snapshots[rel].splitlines()[0][:6]
            self.assert_fs_refused(
                self.run_fs("write", name, {"lease_token": tok, "path": rel, "content": "overwritten\n"}),
                rel,
            )
            self.assert_fs_refused(
                self.run_fs(
                    "edit",
                    name,
                    {"lease_token": tok, "path": rel, "old_string": first_token, "new_string": "pwned"},
                ),
                rel,
            )
            with open(os.path.join(slot_dir, rel), encoding="utf-8") as handle:
                self.assertEqual(handle.read(), snapshots[rel], f"{rel} was modified")

    def test_binary_nul_files_and_oversized_content_refused_while_131072_bytes_allowed(self):
        self.build_repo()
        name = self.acquire_slot()
        tok = self.read_lease_token(name)
        slot_dir = self.harness.slot_dir(name)

        exact_cap = "a" * 131072
        w_cap = self.assert_fs_ok(
            self.run_fs("write", name, {"lease_token": tok, "path": "exact.txt", "content": exact_cap})
        )
        self.assertEqual(w_cap.get("bytes"), 131072)
        r_cap = self.assert_fs_ok(self.run_fs("read", name, {"path": "exact.txt"}))
        self.assertEqual(len(r_cap.get("content", "")), 131072)

        over_cap = "a" * 131073
        self.assert_fs_refused(
            self.run_fs("write", name, {"lease_token": tok, "path": "over.txt", "content": over_cap})
        )
        self.assertFalse(os.path.exists(os.path.join(slot_dir, "over.txt")))

        self.assert_fs_refused(
            self.run_fs(
                "edit",
                name,
                {"lease_token": tok, "path": "exact.txt", "old_string": "aaaa", "new_string": "aaaaa"},
            )
        )

        planted_big = os.path.join(slot_dir, "planted_over.txt")
        with open(planted_big, "w", encoding="utf-8") as handle:
            handle.write(over_cap)
        self.assert_fs_refused(self.run_fs("read", name, {"path": "planted_over.txt"}))
        self.assert_fs_refused(
            self.run_fs(
                "edit",
                name,
                {"lease_token": tok, "path": "planted_over.txt", "old_string": "aaaa", "new_string": "b"},
            )
        )

        planted_bin = os.path.join(slot_dir, "planted.bin")
        with open(planted_bin, "wb") as handle:
            handle.write(b"prefix\x00suffix\n")
        self.assert_fs_refused(self.run_fs("read", name, {"path": "planted.bin"}))
        self.assert_fs_refused(
            self.run_fs(
                "edit",
                name,
                {"lease_token": tok, "path": "planted.bin", "old_string": "prefix", "new_string": "x"},
            )
        )
        self.assert_fs_refused(
            self.run_fs(
                "write",
                name,
                {"lease_token": tok, "path": "new.bin", "content": "hello\x00world\n"},
            )
        )
        self.assertFalse(os.path.exists(os.path.join(slot_dir, "new.bin")))
        self.assert_fs_refused(
            self.run_fs(
                "edit",
                name,
                {"lease_token": tok, "path": "exact.txt", "old_string": "aaaa", "new_string": "aa\x00a"},
            )
        )

    def test_host_gate_refuses_unleased_non_volume_and_foreign_project_slots(self):
        self.build_repo(extra_files={"README.md": "hello\n"})
        name = self.acquire_slot("holder-gate")

        self.assert_fs_ok(self.run_fs("read", name, {"path": "README.md"}))

        other = DevContainerHarness()
        self.addCleanup(other.cleanup)
        shutil.rmtree(other.state_dir)
        shutil.rmtree(other.docker_state)
        other.state_dir = self.harness.state_dir
        other.docker_state = self.harness.docker_state
        other.docker_log = self.harness.docker_log
        other.fixture_repo = os.path.join(other.tmp, "other")
        other.build_fixture_repo(
            with_parent_env="GIT_USER_NAME=other-agent\nGIT_USER_EMAIL=other-agent@example.com\n",
            project_env=project_env_text(PROJECT="other"),
        )
        subprocess.run(
            [other.real_git_bin, "-C", other.fixture_repo, "remote", "set-url", "origin", "https://example.invalid/other.git"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assert_fs_refused(self.run_fs("read", name, {"path": "README.md"}, harness=other))

        self.harness.make_bare_slot("dir-only")
        dir_lease = self.harness.lease_path("dir-only")
        os.makedirs(os.path.dirname(dir_lease), exist_ok=True)
        with open(dir_lease, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "holder": "dir-holder",
                    "owner": "-",
                    "project": f"{PROJECT_PREFIX}-dir-only",
                    "since": 9999999999,
                    "key": "/dev/null",
                    "ssh_port": 39100,
                    "repo_root": os.path.realpath(self.harness.fixture_repo),
                },
                handle,
            )
        self.assert_fs_refused(self.run_fs("read", "dir-only", {"path": "README.md"}))

        rel = self.harness.run("release", name, "--for", "holder-gate")
        self.assertEqual(rel.returncode, 0, rel.stderr.decode())
        self.assert_fs_refused(self.run_fs("read", name, {"path": "README.md"}))


if __name__ == "__main__":
    unittest.main()
