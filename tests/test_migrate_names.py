import os
import stat
import subprocess
import unittest

from harness import DevContainerTestCase, REPO_ROOT, SCRIPT_REL

ENGINE = os.path.join(REPO_ROOT, SCRIPT_REL)

REQUIRED = [
    'PROJECT="demo"',
    'DEFAULT_BRANCH="main"',
    'SLOT_PORTS="SSH_PORT"',
    'COMPOSE_FILES="docker-compose.yml"',
]
SECRET_TOKEN = "ghs_SENTINEL_9f3a_token"
SECRET_KEY_PATH = "/srv/SENTINEL_9f3a/app.pem"
SECRET_SLUG = "SENTINEL_9f3a-slug"
OLD_APP = [
    ("AGENT_APP_ID", "FORGE_APP_ID", "SENTINEL_9f3a_id"),
    ("AGENT_APP_INSTALLATION_ID", "FORGE_APP_INSTALLATION_ID", "SENTINEL_9f3a_inst"),
    ("AGENT_APP_SLUG", "FORGE_APP_SLUG", SECRET_SLUG),
    ("AGENT_APP_BOT_ID", "FORGE_APP_BOT_ID", "SENTINEL_9f3a_bot"),
    ("AGENT_APP_KEY_FILE", "FORGE_APP_KEY_FILE", SECRET_KEY_PATH),
    ("AGENT_APP_REPOS", "FORGE_APP_REPOS", "SENTINEL_9f3a_repo SENTINEL_9f3a_docs"),
]
SECRET_VALUES = [SECRET_TOKEN] + [value for _, _, value in OLD_APP]


def output_of(result):
    return result.stdout.decode() + result.stderr.decode()


def read_bytes(path):
    with open(path, "rb") as handle:
        return handle.read()


def write_bytes(path, data, mode=0o644):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(data)
    os.chmod(path, mode)


def mode_of(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def project_text(*lines):
    return ("\n".join(REQUIRED + list(lines)) + "\n").encode()


def app_file(names):
    body = ["# the App's settings", "GH_TOKEN=" + SECRET_TOKEN]
    for index, (old, new, value) in enumerate(OLD_APP):
        body.append(f"{(old, new)[names]}={value}")
    return ("\n".join(body) + "\n").encode()


class MigrateNamesCase(DevContainerTestCase):
    def setUp(self):
        super().setUp()
        self.harness.build_fixture_repo()
        self.checkout = os.path.join(self.harness.tmp, "alpha-app")
        os.makedirs(self.checkout)
        git = lambda *args: subprocess.run(
            [self.harness.real_git_bin, "-C", self.checkout, *args],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        git("init", "-q")
        git("config", "user.email", "fixture@example.com")
        git("config", "user.name", "fixture")
        self.project_env = os.path.join(self.checkout, "project.env")
        self.credentials = os.path.join(self.checkout, ".devcontainer", ".env")

    def engine_run(self, *args, caller_pwd=None, **extra_env):
        env = dict(extra_env)
        env["DEV_CONTAINER_CALLER_PWD"] = caller_pwd or self.checkout
        return subprocess.run(
            ["bash", ENGINE, *args], cwd=self.harness.tmp, env=self.harness._env(env),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60,
        )

    def migrate(self, *args, **extra_env):
        return self.engine_run("migrate-names", *args, **extra_env)

    def reader(self):
        result = self.engine_run("project", self.checkout)
        pairs = dict(l.split("=", 1) for l in result.stdout.decode().split("\n") if "=" in l)
        return result.returncode, pairs, result.stderr.decode()

    def old_token_project(self, mode=0o644):
        write_bytes(
            self.project_env,
            project_text('AGENT_AUTH="container-env"', 'AGENT_AUTH_ENV_FILE=".devcontainer/.env"'),
            mode,
        )
        write_bytes(self.credentials, b"GH_TOKEN=" + SECRET_TOKEN.encode() + b"\n", 0o600)

    def old_app_project(self, mode=0o644):
        write_bytes(
            self.project_env,
            project_text("AGENT_AUTH=github-app", "AGENT_AUTH_ENV_FILE=.devcontainer/.env"),
            mode,
        )
        write_bytes(self.credentials, app_file(0), 0o600)

    def assertNoSecret(self, text):
        for value in SECRET_VALUES:
            self.assertNotIn(value, text)


class MigrateHelp(MigrateNamesCase):
    def test_help_prints_the_usage_line_and_exits_zero(self):
        result = self.engine_run("migrate-names", "--help", DEV_CONTAINER_CALLER="bin/dev-container")

        self.assertEqual(result.returncode, 0, output_of(result))
        self.assertIn("bin/dev-container migrate-names", result.stdout.decode())
        self.assertIn("--dry-run", result.stdout.decode())


class MigrateTokenMode(MigrateNamesCase):
    def test_renames_the_keys_and_the_mode_in_project_env_and_reads_clean_after(self):
        self.old_token_project()

        result = self.migrate()

        self.assertEqual(result.returncode, 0, output_of(result))
        self.assertEqual(
            read_bytes(self.project_env),
            project_text('FORGE_AUTH="token"', 'FORGE_AUTH_FILE=".devcontainer/.env"'),
        )
        code, got, err = self.reader()
        self.assertEqual(code, 0, err)
        self.assertEqual(got["FORGE_AUTH"], "token")
        self.assertEqual(got["FORGE_AUTH_FILE"], ".devcontainer/.env")
        self.assertNotIn("deprecated", err)

    def test_leaves_the_credential_file_alone_in_token_mode(self):
        self.old_token_project()
        before = read_bytes(self.credentials)

        self.migrate()

        self.assertEqual(read_bytes(self.credentials), before)

    def test_prints_key_names_and_never_a_value(self):
        self.old_token_project()

        result = self.migrate()

        out = output_of(result)
        self.assertIn("AGENT_AUTH -> FORGE_AUTH", out)
        self.assertIn("container-env -> token", out)
        for name in ("AGENT_AUTH", "FORGE_AUTH", "AGENT_AUTH_ENV_FILE", "FORGE_AUTH_FILE", "container-env", "token"):
            self.assertIn(name, out)
        self.assertNoSecret(out)
        self.assertNotIn(".devcontainer/.env", out)

    def test_an_old_mode_under_the_new_key_is_rewritten(self):
        write_bytes(self.project_env, project_text("FORGE_AUTH=github-app", "FORGE_AUTH_FILE=.devcontainer/.env"))
        write_bytes(self.credentials, app_file(1), 0o600)

        result = self.migrate()

        self.assertEqual(result.returncode, 0, output_of(result))
        self.assertEqual(
            read_bytes(self.project_env),
            project_text("FORGE_AUTH=app", "FORGE_AUTH_FILE=.devcontainer/.env"),
        )

    def test_changes_only_the_names_not_comments_other_lines_or_the_missing_final_newline(self):
        text = b"# AGENT_AUTH=container-env is how it was\n" + project_text(
            'AGENT_AUTH="container-env"', "AGENT_AUTH_ENV_FILE=.devcontainer/.env",
            "SLOT_ENV_SET=NOTE=AGENT_AUTH",
        ).rstrip(b"\n")
        write_bytes(self.project_env, text)
        write_bytes(self.credentials, b"GH_TOKEN=x\n", 0o600)

        result = self.migrate()

        self.assertEqual(result.returncode, 0, output_of(result))
        self.assertEqual(
            read_bytes(self.project_env),
            b"# AGENT_AUTH=container-env is how it was\n" + project_text(
                'FORGE_AUTH="token"', "FORGE_AUTH_FILE=.devcontainer/.env", "SLOT_ENV_SET=NOTE=AGENT_AUTH",
            ).rstrip(b"\n"),
        )

    def test_keeps_carriage_returns(self):
        write_bytes(
            self.project_env,
            b"\r\n".join(
                line.encode() for line in REQUIRED + ["AGENT_AUTH=container-env", "AGENT_AUTH_ENV_FILE=.env"]
            ) + b"\r\n",
        )
        write_bytes(os.path.join(self.checkout, ".env"), b"GH_TOKEN=x\n", 0o600)

        result = self.migrate()

        self.assertEqual(result.returncode, 0, output_of(result))
        self.assertTrue(read_bytes(self.project_env).endswith(b"FORGE_AUTH=token\r\nFORGE_AUTH_FILE=.env\r\n"))


class MigrateAppMode(MigrateNamesCase):
    def test_renames_the_six_app_keys_in_the_credential_file_and_keeps_every_value(self):
        self.old_app_project()

        result = self.migrate()

        self.assertEqual(result.returncode, 0, output_of(result))
        self.assertEqual(read_bytes(self.credentials), app_file(1))
        self.assertEqual(
            read_bytes(self.project_env),
            project_text("FORGE_AUTH=app", "FORGE_AUTH_FILE=.devcontainer/.env"),
        )

    def test_the_engine_reads_the_migrated_files_without_a_notice(self):
        self.old_app_project()
        self.migrate()

        code, got, err = self.reader()

        self.assertEqual(code, 0, err)
        self.assertEqual(got["FORGE_AUTH"], "app")
        self.assertNotIn("deprecated", err)

    def test_names_every_renamed_key_and_prints_no_value(self):
        self.old_app_project()

        out = output_of(self.migrate())

        for old, new, _ in OLD_APP:
            self.assertIn(old, out)
            self.assertIn(new, out)
        self.assertNoSecret(out)
        self.assertNotIn("GH_TOKEN", out)

    def test_only_the_old_app_names_that_are_present_are_renamed(self):
        write_bytes(self.project_env, project_text("FORGE_AUTH=app", "FORGE_AUTH_FILE=.devcontainer/.env"))
        body = app_file(1).replace(b"FORGE_APP_SLUG", b"AGENT_APP_SLUG")
        write_bytes(self.credentials, body, 0o600)

        out = output_of(self.migrate())

        self.assertEqual(read_bytes(self.credentials), app_file(1))
        self.assertIn("AGENT_APP_SLUG", out)
        self.assertNotIn("AGENT_APP_ID", out)


class MigrateDryRun(MigrateNamesCase):
    def test_writes_nothing_and_says_what_it_would_rename(self):
        self.old_app_project()
        before = (read_bytes(self.project_env), read_bytes(self.credentials))
        mtimes = (os.stat(self.project_env).st_mtime_ns, os.stat(self.credentials).st_mtime_ns)

        result = self.migrate("--dry-run")

        out = output_of(result)
        self.assertEqual(result.returncode, 0, out)
        self.assertEqual((read_bytes(self.project_env), read_bytes(self.credentials)), before)
        self.assertEqual((os.stat(self.project_env).st_mtime_ns, os.stat(self.credentials).st_mtime_ns), mtimes)
        self.assertIn("dry run", out)
        for old, new, _ in OLD_APP:
            self.assertIn(f"{old} -> {new}", out)
        self.assertIn("AGENT_AUTH -> FORGE_AUTH", out)
        self.assertNoSecret(out)

    def test_a_refusal_is_the_same_in_a_dry_run(self):
        write_bytes(self.project_env, project_text("AGENT_AUTH=app", "FORGE_AUTH=app"))

        result = self.migrate("--dry-run")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("both set the same thing", output_of(result))
        self.assertNotIn("Usage:", output_of(result))


class MigrateIsIdempotent(MigrateNamesCase):
    def test_a_second_run_changes_nothing_and_says_so(self):
        self.old_app_project()
        self.migrate()
        after = (read_bytes(self.project_env), read_bytes(self.credentials))
        mtimes = (os.stat(self.project_env).st_mtime_ns, os.stat(self.credentials).st_mtime_ns)

        result = self.migrate()

        out = output_of(result)
        self.assertEqual(result.returncode, 0, out)
        self.assertEqual((read_bytes(self.project_env), read_bytes(self.credentials)), after)
        self.assertEqual((os.stat(self.project_env).st_mtime_ns, os.stat(self.credentials).st_mtime_ns), mtimes)
        self.assertIn("nothing to rename", out)

    def test_a_project_that_never_had_the_old_names_is_left_alone(self):
        write_bytes(self.project_env, project_text("FORGE_AUTH=token", "FORGE_AUTH_FILE=.env"))
        before = read_bytes(self.project_env)

        result = self.migrate()

        self.assertEqual(result.returncode, 0, output_of(result))
        self.assertEqual(read_bytes(self.project_env), before)
        self.assertIn("nothing to rename", output_of(result))

    def test_a_project_with_no_credential_setting_at_all_is_left_alone(self):
        write_bytes(self.project_env, project_text())
        before = read_bytes(self.project_env)

        result = self.migrate()

        self.assertEqual(result.returncode, 0, output_of(result))
        self.assertEqual(read_bytes(self.project_env), before)


class MigrateKeepsModes(MigrateNamesCase):
    def test_a_private_credential_file_and_a_private_project_env_stay_private(self):
        self.old_app_project(mode=0o600)

        self.migrate()

        self.assertIn(b"FORGE_APP_ID", read_bytes(self.credentials))
        self.assertEqual(mode_of(self.credentials), 0o600)
        self.assertEqual(mode_of(self.project_env), 0o600)

    def test_an_ordinary_project_env_stays_ordinary(self):
        self.old_app_project(mode=0o644)

        self.migrate()

        self.assertIn(b"FORGE_AUTH", read_bytes(self.project_env))
        self.assertEqual(mode_of(self.project_env), 0o644)

    def test_leaves_no_temporary_file_beside_either_file(self):
        self.old_app_project()

        self.migrate()

        self.assertIn(b"FORGE_APP_ID", read_bytes(self.credentials))
        self.assertEqual(sorted(os.listdir(self.checkout)), [".devcontainer", ".git", "project.env"])
        self.assertEqual(os.listdir(os.path.dirname(self.credentials)), [".env"])


class MigrateRefuses(MigrateNamesCase):
    def snapshot(self):
        return {
            path: read_bytes(path)
            for path in (self.project_env, self.credentials)
            if os.path.exists(path)
        }

    def test_a_project_setting_given_under_both_names_is_refused_naming_both(self):
        for old, new in (("AGENT_AUTH", "FORGE_AUTH"), ("AGENT_AUTH_ENV_FILE", "FORGE_AUTH_FILE")):
            for order in ((old, new), (new, old)):
                with self.subTest(old=old, order=order):
                    lines = [f"{order[0]}=token", f"{order[1]}=token"]
                    if old == "AGENT_AUTH_ENV_FILE":
                        lines = [f"{order[0]}=.env", f"{order[1]}=.env"]
                    write_bytes(self.project_env, project_text(*lines))
                    before = self.snapshot()

                    result = self.migrate()

                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("both set the same thing", output_of(result))
                    self.assertIn(old, output_of(result))
                    self.assertIn(new, output_of(result))
                    self.assertEqual(self.snapshot(), before)

    def test_a_credential_setting_given_under_both_names_is_refused_and_nothing_is_written(self):
        self.old_app_project()
        write_bytes(self.credentials, app_file(0) + b"FORGE_APP_SLUG=other\n", 0o600)
        before = self.snapshot()

        result = self.migrate()

        out = output_of(result)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("both set the same thing", out)
        self.assertIn("AGENT_APP_SLUG", out)
        self.assertIn("FORGE_APP_SLUG", out)
        self.assertNoSecret(out)
        self.assertEqual(self.snapshot(), before)

    def test_a_credential_file_path_that_climbs_out_of_the_checkout_is_refused(self):
        for entry in ("../outside.env", "/etc/agent.env"):
            with self.subTest(entry):
                write_bytes(self.project_env, project_text("AGENT_AUTH=github-app", f"AGENT_AUTH_ENV_FILE={entry}"))
                before = self.snapshot()

                result = self.migrate()

                self.assertNotEqual(result.returncode, 0)
                self.assertIn("not a path inside the checkout", output_of(result))
                self.assertIn(entry, output_of(result))
                self.assertEqual(self.snapshot(), before)

    def test_a_checkout_without_project_env_is_refused(self):
        result = self.migrate()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("has no project.env", output_of(result))

    def test_an_unknown_flag_is_refused(self):
        result = self.migrate("--force")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unknown flag: --force", output_of(result))


class MigrateWhereItRuns(MigrateNamesCase):
    def test_a_missing_credential_file_is_named_and_project_env_is_still_migrated(self):
        write_bytes(self.project_env, project_text("AGENT_AUTH=github-app", "AGENT_AUTH_ENV_FILE=.devcontainer/.env"))

        result = self.migrate()

        out = output_of(result)
        self.assertEqual(result.returncode, 0, out)
        self.assertIn("not found", out)
        self.assertEqual(
            read_bytes(self.project_env),
            project_text("FORGE_AUTH=app", "FORGE_AUTH_FILE=.devcontainer/.env"),
        )

    def test_takes_the_checkout_as_an_argument(self):
        self.old_app_project()

        result = self.migrate(self.checkout, caller_pwd=self.harness.fixture_repo)

        self.assertEqual(result.returncode, 0, output_of(result))
        self.assertEqual(read_bytes(self.credentials), app_file(1))

    def test_runs_from_a_subdirectory_of_the_checkout(self):
        self.old_app_project()

        result = self.migrate(caller_pwd=os.path.join(self.checkout, ".devcontainer"))

        self.assertEqual(result.returncode, 0, output_of(result))
        self.assertEqual(read_bytes(self.credentials), app_file(1))


if __name__ == "__main__":
    unittest.main()
