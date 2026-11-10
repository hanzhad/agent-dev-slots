import os
import shutil
import stat
import subprocess
import unittest

from harness import DevContainerTestCase, REPO_ROOT, SCRIPT_REL

ENGINE = os.path.join(REPO_ROOT, SCRIPT_REL)
THIN = os.path.join(REPO_ROOT, "scripts", "dev-container-thin")
FIXTURES = os.path.join(REPO_ROOT, "tests", "fixtures")

REQUIRED = {
    "PROJECT": "demo",
    "DEFAULT_BRANCH": "main",
    "SLOT_PORTS": "SSH_PORT",
    "COMPOSE_FILES": "docker-compose.yml",
}


def output_of(result):
    return result.stdout.decode() + result.stderr.decode()


def read_text(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def write_text(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


class AdoptTestCase(DevContainerTestCase):
    def setUp(self):
        super().setUp()
        self.harness.build_fixture_repo()

    def make_checkout(self, name="alpha-app", branch="main", with_origin=True):
        path = os.path.join(self.harness.tmp, name)
        os.makedirs(path, exist_ok=True)
        write_text(os.path.join(path, "README.md"), f"{name}\n")
        git = lambda *args: subprocess.run(
            [self.harness.real_git_bin, "-C", path, *args],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        git("init", "-q")
        git("config", "user.email", "fixture@example.com")
        git("config", "user.name", "fixture")
        git("checkout", "-q", "-b", branch)
        git("add", "README.md")
        git("commit", "-q", "-m", "initial")
        if with_origin:
            git("remote", "add", "origin", f"https://example.invalid/{name}.git")
        return path

    def add_devcontainer_compose(self, checkout, compose_text=None):
        if compose_text is None:
            compose_text = read_text(
                os.path.join(FIXTURES, "demo-project", ".devcontainer", "docker-compose.yml")
            )
        write_text(os.path.join(checkout, ".devcontainer", "docker-compose.yml"), compose_text)

    def engine_run(self, *args, caller_pwd=None, **extra_env):
        overrides = dict(extra_env)
        if caller_pwd is not None:
            overrides["DEV_CONTAINER_CALLER_PWD"] = caller_pwd
        return subprocess.run(
            ["bash", ENGINE, *args],
            cwd=self.harness.tmp,
            env=self.harness._env(overrides),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
        )

    def adopt(self, checkout, *args, via_arg=False, **extra_env):
        if via_arg:
            return self.engine_run(
                "adopt", checkout, *args, caller_pwd=self.harness.fixture_repo, **extra_env
            )
        return self.engine_run("adopt", *args, caller_pwd=checkout, **extra_env)

    def doctor(self, checkout, **extra_env):
        return self.engine_run("doctor", caller_pwd=checkout, **extra_env)

    def project_keys(self, checkout):
        result = self.engine_run("project", checkout, caller_pwd=checkout)
        out = result.stdout.decode()
        pairs = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
        return result.returncode, pairs, result.stderr.decode()

    def state_files(self):
        found = []
        for root, _dirs, files in os.walk(self.harness.state_dir):
            for name in files:
                found.append(os.path.relpath(os.path.join(root, name), self.harness.state_dir))
        return sorted(found)


class AdoptHelp(AdoptTestCase):
    def test_adopt_help_prints_usage_and_exits_zero(self):
        result = self.engine_run(
            "adopt", "--help",
            caller_pwd=self.harness.fixture_repo,
            DEV_CONTAINER_CALLER="bin/dev-container",
        )

        self.assertEqual(result.returncode, 0, output_of(result))
        self.assertIn("bin/dev-container adopt", result.stdout.decode())


class AdoptExistingContainerSetup(AdoptTestCase):
    def test_adopts_devcontainer_checkout_writes_project_env_and_no_launcher_and_doctor_passes(self):
        checkout = self.make_checkout(name="alpha-app", branch="main")
        self.add_devcontainer_compose(checkout)

        result = self.adopt(checkout)

        self.assertEqual(result.returncode, 0, output_of(result))
        code, got, err = self.project_keys(checkout)
        self.assertEqual(code, 0, err)
        self.assertEqual(got["PROJECT"], "alpha-app")
        self.assertEqual(got["DEFAULT_BRANCH"], "main")
        self.assertEqual(got.get("COMPOSE_DIR", ".devcontainer"), ".devcontainer")
        self.assertEqual(got["COMPOSE_FILES"], "docker-compose.yml")
        self.assertEqual(got["SLOT_PORTS"], "SSH_PORT")

        self.assertFalse(os.path.lexists(os.path.join(checkout, "bin", "dev-container")),
                         "a project carries no launcher: the global dev-container command is the launcher")

        doc = self.doctor(checkout)
        self.assertEqual(doc.returncode, 0, output_of(doc))
        self.assertIn("every prerequisite is met", doc.stdout.decode())

    def test_infers_compose_dir_files_and_slot_ports_from_existing_compose_files(self):
        second = self.make_checkout(name="second-service", branch="master")
        shutil.copytree(
            os.path.join(FIXTURES, "second-project", "deploy"),
            os.path.join(second, "deploy"),
        )

        res_second = self.adopt(second, via_arg=True)

        self.assertEqual(res_second.returncode, 0, output_of(res_second))
        code, got, err = self.project_keys(second)
        self.assertEqual(code, 0, err)
        self.assertEqual(got["PROJECT"], "second-service")
        self.assertEqual(got["DEFAULT_BRANCH"], "master")
        self.assertEqual(got["COMPOSE_DIR"], "deploy/compose")
        self.assertEqual(got["COMPOSE_FILES"], "compose.yml compose.dev.yml")
        self.assertEqual(got["SLOT_PORTS"], "SSH_PORT WEB_PORT")
        self.assertEqual(self.doctor(second).returncode, 0)

        third = self.make_checkout(name="third-service", branch="trunk")
        shutil.copytree(
            os.path.join(FIXTURES, "third-project", "stack"),
            os.path.join(third, "stack"),
        )

        res_third = self.adopt(third, via_arg=True)

        self.assertEqual(res_third.returncode, 0, output_of(res_third))
        code, got, err = self.project_keys(third)
        self.assertEqual(code, 0, err)
        self.assertEqual(got["PROJECT"], "third-service")
        self.assertEqual(got["DEFAULT_BRANCH"], "trunk")
        self.assertEqual(got["COMPOSE_DIR"], "stack")
        self.assertEqual(got["COMPOSE_FILES"], "stack.yaml")
        self.assertEqual(got["SLOT_PORTS"], "GATEWAY_PORT API_PORT ADMIN_PORT METRICS_PORT")
        self.assertEqual(self.doctor(third).returncode, 0)

    def test_leaves_a_private_script_copy_at_bin_dev_container_as_it_is(self):
        checkout = self.make_checkout(name="legacy-app", branch="main")
        self.add_devcontainer_compose(checkout)
        thin = os.path.join(checkout, "bin", "dev-container")
        write_text(thin, "#!/usr/bin/env bash\necho legacy-script\nexit 99\n")
        os.chmod(thin, os.stat(thin).st_mode | stat.S_IEXEC)

        result = self.adopt(checkout)

        self.assertEqual(result.returncode, 0, output_of(result))
        self.assertEqual(read_text(thin), "#!/usr/bin/env bash" + chr(10) + "echo legacy-script" + chr(10) + "exit 99" + chr(10))
        self.assertTrue(os.access(thin, os.X_OK))
        self.assertEqual(self.doctor(checkout).returncode, 0)

    def test_adopts_when_engine_has_only_bin_and_lib_and_writes_no_launcher(self):
        checkout = self.make_checkout(name="container-layout-app", branch="main")
        self.add_devcontainer_compose(checkout)
        engine_dir = os.path.join(self.harness.tmp, "opt-engine")
        shutil.copytree(os.path.join(REPO_ROOT, "bin"), os.path.join(engine_dir, "bin"))
        shutil.copytree(os.path.join(REPO_ROOT, "lib"), os.path.join(engine_dir, "lib"))
        shutil.copytree(os.path.join(REPO_ROOT, "templates"), os.path.join(engine_dir, "templates"))

        result = subprocess.run(
            ["bash", os.path.join(engine_dir, SCRIPT_REL), "adopt"],
            cwd=self.harness.tmp,
            env=self.harness._env({"DEV_CONTAINER_CALLER_PWD": checkout}),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
        )

        self.assertEqual(result.returncode, 0, output_of(result))
        self.assertTrue(os.path.isfile(os.path.join(checkout, "project.env")))
        self.assertFalse(os.path.lexists(os.path.join(checkout, "bin", "dev-container")))

    def test_adopts_a_relative_directory_argument_resolved_against_caller_pwd(self):
        checkout = self.make_checkout(name="rel-app", branch="main")
        self.add_devcontainer_compose(checkout)

        result = self.engine_run("adopt", "rel-app", caller_pwd=self.harness.tmp)

        self.assertEqual(result.returncode, 0, output_of(result))
        self.assertTrue(os.path.isfile(os.path.join(checkout, "project.env")))


class AdoptIdempotence(AdoptTestCase):
    def test_second_adopt_preserves_project_env_and_exclude_file(self):
        checkout = self.make_checkout(name="idempotent-app", branch="main")
        self.add_devcontainer_compose(checkout)
        write_text(os.path.join(checkout, ".devcontainer", ".env"), "LOG_LEVEL=debug\n")

        first = self.adopt(checkout)
        self.assertEqual(first.returncode, 0, output_of(first))

        project_env_path = os.path.join(checkout, "project.env")
        custom_env = read_text(project_env_path) + "SLOT_MEMORY_LIMIT=2g\n"
        write_text(project_env_path, custom_env)
        exclude_path = os.path.join(checkout, ".git", "info", "exclude")
        exclude_before = read_text(exclude_path) if os.path.isfile(exclude_path) else ""

        second = self.adopt(checkout)

        self.assertEqual(second.returncode, 0, output_of(second))
        self.assertEqual(read_text(project_env_path), custom_env)
        exclude_after = read_text(exclude_path) if os.path.isfile(exclude_path) else ""
        self.assertEqual(exclude_after, exclude_before)


class AdoptSecretsAndGitIgnore(AdoptTestCase):
    def test_never_writes_dotenv_secrets_into_project_env_or_checkout_files_and_excludes_dotenv(self):
        checkout = self.make_checkout(name="secret-app", branch="main")
        self.add_devcontainer_compose(checkout)
        root_secret = "root-dotenv-secret-value-901"
        dev_secret = "devcontainer-dotenv-secret-value-902"
        write_text(os.path.join(checkout, ".env"), f"GH_TOKEN={root_secret}\nAPP_KEY=plain\n")
        write_text(
            os.path.join(checkout, ".devcontainer", ".env"),
            f"GH_TOKEN={dev_secret}\nDB_PASS={root_secret}\n",
        )

        result = self.adopt(checkout)

        self.assertEqual(result.returncode, 0, output_of(result))
        project_env = read_text(os.path.join(checkout, "project.env"))
        self.assertNotIn(root_secret, project_env)
        self.assertNotIn(dev_secret, project_env)
        self.assertNotIn("GH_TOKEN", project_env)

        for root, dirs, files in os.walk(checkout):
            dirs[:] = [d for d in dirs if d != ".git"]
            for name in files:
                if name == ".env":
                    continue
                content = read_text(os.path.join(root, name))
                self.assertNotIn(root_secret, content)
                self.assertNotIn(dev_secret, content)

        for rel in (".env", ".devcontainer/.env"):
            check = subprocess.run(
                [self.harness.real_git_bin, "-C", checkout, "check-ignore", "-q", rel],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertEqual(check.returncode, 0, f"{rel} is not ignored by git after adopt")


class AdoptStarterTemplateAndRefusals(AdoptTestCase):
    def test_checkout_with_no_compose_file_gets_starter_template_and_doctor_passes(self):
        checkout = self.make_checkout(name="fresh-app", branch="main", with_origin=False)

        result = self.adopt(checkout)

        self.assertEqual(result.returncode, 0, output_of(result))
        compose_path = os.path.join(checkout, ".devcontainer", "docker-compose.yml")
        self.assertTrue(os.path.isfile(compose_path), "starter .devcontainer/docker-compose.yml was not written")
        self.assertTrue(os.path.isfile(os.path.join(checkout, "project.env")))
        self.assertFalse(os.path.lexists(os.path.join(checkout, "bin", "dev-container")))

        doc = self.doctor(checkout)
        self.assertEqual(doc.returncode, 0, output_of(doc))
        self.assertIn("every prerequisite is met", doc.stdout.decode())

        explicit = self.make_checkout(name="explicit-template-app", branch="main")
        res_explicit = self.adopt(explicit, "--template")
        self.assertEqual(res_explicit.returncode, 0, output_of(res_explicit))
        self.assertTrue(os.path.isfile(os.path.join(explicit, ".devcontainer", "docker-compose.yml")))
        self.assertEqual(self.doctor(explicit).returncode, 0)

    def test_template_never_replaces_a_dockerfile_or_entrypoint_the_project_has(self):
        checkout = self.make_checkout(name="own-image-app", branch="main")
        dev = os.path.join(checkout, ".devcontainer")
        write_text(os.path.join(dev, "compose.yaml"), "services:\n  app:\n    image: x\n")
        write_text(os.path.join(dev, "Dockerfile"), "FROM scratch\n# mine\n")
        write_text(os.path.join(dev, "entrypoint.sh"), "#!/bin/sh\n# mine\n")

        self.adopt(checkout, "--template")

        self.assertTrue(os.path.isfile(os.path.join(dev, "engine.Dockerfile")))
        for name in ("Dockerfile", "entrypoint.sh"):
            with open(os.path.join(dev, name), encoding="utf-8") as f:
                self.assertIn("# mine", f.read(), f"{name} of the project was replaced")

    def test_the_image_adopt_writes_installs_gh(self):
        checkout = self.make_checkout(name="gh-app", branch="main")
        write_text(os.path.join(checkout, ".devcontainer", "compose.yaml"), "services:\n  app:\n    image: x\n")

        self.adopt(checkout, "--template")

        with open(os.path.join(checkout, ".devcontainer", "engine.Dockerfile"), encoding="utf-8") as f:
            dockerfile = f.read()
        self.assertRegex(dockerfile, r"(?s)apt-get install.*? gh .*?apt-get clean")

    def test_no_template_or_ambiguous_compose_layout_refuses_and_says_why(self):
        checkout = self.make_checkout(name="bare-app", branch="main")

        result = self.adopt(checkout, "--no-template")

        text = output_of(result)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("Usage:", text)
        self.assertRegex(text, r"(?i)compose|template")
        self.assertFalse(os.path.exists(os.path.join(checkout, "project.env")))

        ambiguous = self.make_checkout(name="ambiguous-app", branch="main")
        write_text(os.path.join(ambiguous, "deploy", "compose.yml"), "services:\n  app:\n    image: x\n")
        write_text(os.path.join(ambiguous, "stack", "compose.yml"), "services:\n  app:\n    image: y\n")

        res_amb = self.adopt(ambiguous)
        amb_text = output_of(res_amb)
        self.assertNotEqual(res_amb.returncode, 0)
        self.assertNotIn("Usage:", amb_text)
        self.assertRegex(amb_text, r"(?i)compose|ambiguous|multiple")
        self.assertFalse(os.path.exists(os.path.join(ambiguous, "project.env")))

    def test_non_git_directory_is_refused_with_reason(self):
        plain = os.path.join(self.harness.tmp, "not-a-git-repo")
        os.makedirs(plain)

        result = self.adopt(plain, via_arg=True)

        text = output_of(result)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("Usage:", text)
        self.assertRegex(text, r"(?i)git|checkout")
        self.assertFalse(os.path.exists(os.path.join(plain, "project.env")))


class AdoptInvalidExistingProjectEnv(AdoptTestCase):
    def test_existing_project_env_missing_a_required_key_is_refused_by_adopt_and_doctor(self):
        for missing in REQUIRED:
            with self.subTest(missing=missing):
                slug = missing.lower().replace("_", "-")
                checkout = self.make_checkout(name=f"missing-{slug}", branch="main")
                self.add_devcontainer_compose(checkout)
                partial = "".join(f"{k}={v}\n" for k, v in REQUIRED.items() if k != missing)
                write_text(os.path.join(checkout, "project.env"), partial)

                adopted = self.adopt(checkout, via_arg=True)
                adopted_text = output_of(adopted)
                self.assertNotEqual(adopted.returncode, 0)
                self.assertNotIn("Usage:", adopted_text)
                self.assertIn(missing, adopted_text)

                doc = self.doctor(checkout)
                doc_text = output_of(doc)
                self.assertNotEqual(doc.returncode, 0)
                self.assertNotIn("Usage:", doc_text)
                self.assertIn(missing, doc_text)


class AdoptConnectionIsProjectEnvOnly(AdoptTestCase):
    def test_adopt_writes_nothing_to_state_and_removing_project_env_disconnects(self):
        checkout = self.make_checkout(name="solo-app", branch="main")
        self.add_devcontainer_compose(checkout)

        code_before, _, err_before = self.project_keys(checkout)
        self.assertNotEqual(code_before, 0)
        self.assertIn("project.env", err_before)
        doc_before = self.doctor(checkout)
        self.assertNotEqual(doc_before.returncode, 0)
        self.assertIn("project.env", output_of(doc_before))

        fresh_state = os.path.join(self.harness.tmp, "untouched-state")
        state_before = self.state_files()
        adopted = self.adopt(checkout, DEV_CONTAINER_HOME=fresh_state)
        self.assertEqual(adopted.returncode, 0, output_of(adopted))
        self.assertFalse(os.path.exists(fresh_state))
        self.assertEqual(self.state_files(), state_before)

        code_conn, got_conn, err_conn = self.project_keys(checkout)
        self.assertEqual(code_conn, 0, err_conn)
        self.assertEqual(got_conn["PROJECT"], "solo-app")
        self.assertEqual(self.doctor(checkout).returncode, 0)

        os.remove(os.path.join(checkout, "project.env"))

        code_after, _, err_after = self.project_keys(checkout)
        self.assertNotEqual(code_after, 0)
        self.assertIn("project.env", err_after)
        doc_after = self.doctor(checkout)
        self.assertNotEqual(doc_after.returncode, 0)
        self.assertIn("project.env", output_of(doc_after))


class AdoptLibHelpers(AdoptTestCase):
    def commit_all(self, checkout, message="more files"):
        for args in (("add", "-A"), ("commit", "-q", "-m", message)):
            subprocess.run(
                [self.harness.real_git_bin, "-C", checkout, *args],
                check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )

    def lib(self, script, *args):
        env = self.harness._env({})
        env["DEV_CONTAINER_LIB"] = "1"
        cmd = (
            f'. "{ENGINE}"\n'
            f'lib_dir="$(CDPATH= cd -P "$(dirname "{ENGINE}")/../lib" && pwd)"\n' + script
        )
        return subprocess.run(
            ["bash", "-c", cmd, "test", *args],
            cwd=self.harness.tmp, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
        )


class AdoptTrackedAndFixtureFiles(AdoptLibHelpers):
    def test_a_tracked_bin_dev_container_is_left_byte_identical(self):
        checkout = self.make_checkout(name="engine-like-app")
        self.add_devcontainer_compose(checkout)
        thin = os.path.join(checkout, "bin", "dev-container")
        own = "#!/usr/bin/env bash\necho the-project-own-script\n"
        write_text(thin, own)
        os.chmod(thin, 0o755)
        self.commit_all(checkout)
        write_text(thin, own + "# edited, not committed\n")
        before = read_text(thin)

        result = self.adopt(checkout)

        self.assertEqual(result.returncode, 0, output_of(result))
        self.assertEqual(read_text(thin), before)
        self.assertNotIn("bin/dev-container", output_of(result))
        self.assertTrue(os.path.isfile(os.path.join(checkout, "project.env")))

    def test_compose_files_under_fixture_directories_are_not_the_projects_compose_layout(self):
        for name, flags in (("fixtures-app", ()), ("fixtures-template-app", ("--template",))):
            checkout = self.make_checkout(name=name)
            for rel in (
                "tests/fixtures/a/.devcontainer/docker-compose.yml",
                "test/b/compose.yml",
                "tests/testdata/c/deploy/compose.yaml",
                "src/__fixtures__/d/compose.yml",
                "fixtures/e/stack.yml",
            ):
                write_text(os.path.join(checkout, rel), "services:\n  app:\n    image: x\n")

            result = self.adopt(checkout, *flags)

            self.assertEqual(result.returncode, 0, name + ": " + output_of(result))
            self.assertNotRegex(output_of(result), r"(?i)ambiguous")
            code, got, err = self.project_keys(checkout)
            self.assertEqual(code, 0, err)
            self.assertEqual(got["COMPOSE_DIR"], ".devcontainer", name)
            self.assertEqual(got["COMPOSE_FILES"], "docker-compose.yml", name)

    def test_template_uses_devcontainer_without_looking_at_other_compose_directories(self):
        checkout = self.make_checkout(name="two-stacks-app")
        write_text(os.path.join(checkout, "deploy", "compose.yml"), "services:\n  app:\n    image: x\n")
        write_text(os.path.join(checkout, "stack", "compose.yml"), "services:\n  app:\n    image: y\n")

        result = self.adopt(checkout, "--template")

        self.assertEqual(result.returncode, 0, output_of(result))
        code, got, err = self.project_keys(checkout)
        self.assertEqual(code, 0, err)
        self.assertEqual(got["COMPOSE_DIR"], ".devcontainer")
        self.assertEqual(got["COMPOSE_FILES"], "docker-compose.yml")

    def test_has_compose_ignores_fixture_directories(self):
        root = os.path.join(self.harness.tmp, "has-compose-root")
        write_text(os.path.join(root, "tests", "fixtures", "x", "compose.yml"), "services:\n  a:\n    image: x\n")
        self.assertNotEqual(self.lib('adopt_has_compose "$1"', root).returncode, 0)
        write_text(os.path.join(root, "deploy", "compose.yml"), "services:\n  a:\n    image: x\n")
        self.assertEqual(self.lib('adopt_has_compose "$1"', root).returncode, 0)


class AdoptExistingProjectEnvWithoutComposeFile(AdoptLibHelpers):
    def write_project_env(self, checkout, compose_dir, compose_files):
        write_text(
            os.path.join(checkout, "project.env"),
            "PROJECT=named-app\nDEFAULT_BRANCH=main\nSLOT_PORTS=SSH_PORT\n"
            f"COMPOSE_DIR={compose_dir}\nCOMPOSE_FILES={compose_files}\n",
        )

    def test_compose_plan_is_decided_from_strings_and_the_files_that_exist(self):
        root = os.path.join(self.harness.tmp, "plan-root")
        os.makedirs(root)
        plan = lambda d, f: self.lib('adopt_compose_plan "$1" "$2" "$3"', root, d, f).stdout.decode().strip()
        self.assertEqual(plan(".devcontainer", "docker-compose.yml"), "starter")
        self.assertEqual(plan("deploy/compose", "compose.yml"), "missing deploy/compose/compose.yml")
        self.assertEqual(plan("deploy/", "compose.yml"), "missing deploy/compose.yml")
        self.assertEqual(plan(".devcontainer", "compose.yaml"), "missing .devcontainer/compose.yaml")
        write_text(os.path.join(root, ".devcontainer", "docker-compose.yml"), "services:\n  app:\n    image: x\n")
        self.assertEqual(plan(".devcontainer", "docker-compose.yml"), "ok")
        self.assertEqual(
            plan(".devcontainer", "docker-compose.yml docker-compose.dev.yml"),
            "missing .devcontainer/docker-compose.dev.yml",
        )
        write_text(os.path.join(root, "compose.yml"), "services:\n  app:\n    image: x\n")
        self.assertEqual(plan(".", "compose.yml"), "ok")
        self.assertEqual(plan(".", "other.yml"), "missing other.yml")

    def test_adopt_generates_the_starter_when_project_env_names_the_missing_devcontainer_compose(self):
        checkout = self.make_checkout(name="named-starter-app")
        self.write_project_env(checkout, ".devcontainer", "docker-compose.yml")
        write_text(os.path.join(checkout, ".devcontainer", "Dockerfile"), "FROM scratch\n# mine\n")
        before = read_text(os.path.join(checkout, "project.env"))

        result = self.adopt(checkout)

        self.assertEqual(result.returncode, 0, output_of(result))
        dev = os.path.join(checkout, ".devcontainer")
        for name in ("docker-compose.yml", "engine.Dockerfile", "engine-entrypoint.sh"):
            self.assertTrue(os.path.isfile(os.path.join(dev, name)), name + " was not generated")
        self.assertIn("# mine", read_text(os.path.join(dev, "Dockerfile")))
        self.assertEqual(read_text(os.path.join(checkout, "project.env")), before)

    def test_adopt_refuses_when_project_env_names_a_compose_file_that_does_not_exist(self):
        checkout = self.make_checkout(name="named-missing-app")
        self.write_project_env(checkout, "deploy", "compose.yml")

        result = self.adopt(checkout)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("project.env names deploy/compose.yml, which does not exist", output_of(result))
        self.assertFalse(os.path.exists(os.path.join(checkout, "deploy")))
        self.assertFalse(os.path.exists(os.path.join(checkout, ".devcontainer")))

    def test_dotenv_never_creates_a_directory_that_holds_no_compose_file(self):
        root = os.path.join(self.harness.tmp, "dotenv-root")
        os.makedirs(root)
        for d in (".devcontainer", "deploy/compose"):
            res = self.lib('adopt_write_dotenv "$1" "$2"', root, d)
            self.assertEqual(res.returncode, 0, output_of(res))
            self.assertFalse(os.path.exists(os.path.join(root, d.split("/")[0])), d)
        os.makedirs(os.path.join(root, ".devcontainer"))
        self.lib('adopt_write_dotenv "$1" "$2"', root, ".devcontainer")
        self.assertTrue(os.path.isfile(os.path.join(root, ".devcontainer", ".env")))

    def test_auto_connect_with_project_env_follows_the_same_rule(self):
        starter = self.make_checkout(name="auto-named-starter")
        self.write_project_env(starter, ".devcontainer", "docker-compose.yml")
        res = self.lib('CALLER=x; adopt_auto_connect "$1"', starter)
        self.assertEqual(res.returncode, 0, output_of(res))
        self.assertTrue(os.path.isfile(os.path.join(starter, ".devcontainer", "docker-compose.yml")))

        missing = self.make_checkout(name="auto-named-missing")
        write_text(os.path.join(missing, "package-lock.json"), "{}\n")
        self.write_project_env(missing, "deploy", "compose.yml")
        res = self.lib('CALLER=x; adopt_auto_connect "$1"', missing)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("which does not exist", output_of(res))
        self.assertFalse(os.path.exists(os.path.join(missing, ".devcontainer")))
        self.assertFalse(os.path.exists(os.path.join(missing, "deploy")))


if __name__ == "__main__":
    unittest.main()
