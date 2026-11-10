import glob
import os
import re
import shutil
import subprocess
import unittest

from harness import DevContainerTestCase, REPO_ROOT, SCRIPT_REL, project_env_text

ENGINE = os.path.join(REPO_ROOT, SCRIPT_REL)
TEMPLATES = os.path.join(REPO_ROOT, "templates")
NL = chr(10)
IDENTITY = "GIT_USER_NAME=demo-agent" + NL + "GIT_USER_EMAIL=demo-agent@example.com" + NL
SEEDED = ["docker-compose.yml", "engine.Dockerfile", "engine-entrypoint.sh", "agent.env.example"]


def output_of(result):
    return result.stdout.decode() + result.stderr.decode()


def read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def write(path, text, mode=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    if mode is not None:
        os.chmod(path, mode)


GOOD_COMPOSE = NL.join(
    [
        "services:",
        "  app:",
        "    image: node:22-bookworm",
        "    environment:",
        "      GH_TOKEN: ${GH_TOKEN}",
        "      - GIT_USER_NAME",
        "    ports:",
        '      - "127.0.0.1:${SSH_PORT}:22"',
        "",
    ]
)


class SeedTestCase(DevContainerTestCase):
    def setUp(self):
        super().setUp()
        self.harness.build_fixture_repo(with_parent_env=IDENTITY)

    def engine(self, *args, cwd=None, **env):
        overrides = dict(env)
        overrides["DEV_CONTAINER_CALLER_PWD"] = cwd or self.harness.fixture_repo
        return subprocess.run(
            ["bash", ENGINE, *args],
            cwd=self.harness.tmp,
            env=self.harness._env(overrides),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=120,
        )

    def fresh_repo(self, name="seeded-app", auth=None):
        other = os.path.join(self.harness.tmp, name)
        os.makedirs(other)
        git = lambda *args: subprocess.run(
            [self.harness.real_git_bin, "-C", other, *args],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        git("init", "-q")
        git("config", "user.email", "f@example.com")
        git("config", "user.name", "f")
        write(os.path.join(other, "package.json"), "{}" + NL)
        write(os.path.join(other, "package-lock.json"), "{}" + NL)
        git("add", "-A")
        git("commit", "-qm", "init")
        if auth is not None:
            write(
                os.path.join(other, "project.env"),
                project_env_text(
                    PROJECT=name, FORGE_AUTH=auth, FORGE_AUTH_FILE=".devcontainer/agent.env",
                    COMPOSE_DIR=".devcontainer",
                ),
            )
        return other

    def dc(self, root, name):
        return os.path.join(root, ".devcontainer", name)


class AdoptSeedsTheStarterFilesOnce(SeedTestCase):
    def test_adopt_writes_the_compose_file_the_image_the_entrypoint_and_the_credential_template(self):
        root = self.fresh_repo()

        result = self.engine("adopt", root)

        self.assertEqual(result.returncode, 0, output_of(result))
        for name in SEEDED:
            self.assertTrue(os.path.isfile(self.dc(root, name)), name)
        self.assertTrue(os.access(self.dc(root, "engine-entrypoint.sh"), os.X_OK))
        self.assertIn("app:", read(self.dc(root, "docker-compose.yml")))

    def test_every_seeded_file_says_which_template_version_it_came_from(self):
        root = self.fresh_repo()

        self.engine("adopt", root)

        for name in SEEDED:
            head = "".join(read(self.dc(root, name)).splitlines(True)[:3])
            self.assertRegex(head, r"dev-container-engine template: %s version: [0-9]+" % re.escape(name))

    def test_a_second_adopt_touches_none_of_them(self):
        root = self.fresh_repo()
        self.engine("adopt", root)
        times = {n: os.stat(self.dc(root, n)).st_mtime_ns for n in SEEDED}

        result = self.engine("adopt", root)

        self.assertEqual(result.returncode, 0, output_of(result))
        for name in SEEDED:
            self.assertEqual(os.stat(self.dc(root, name)).st_mtime_ns, times[name], name)

    def test_an_edited_image_survives_even_when_the_compose_file_has_to_be_written_again(self):
        root = self.fresh_repo()
        self.engine("adopt", root)
        write(self.dc(root, "engine.Dockerfile"), "FROM scratch" + NL + "# ours" + NL)
        os.remove(self.dc(root, "docker-compose.yml"))

        self.engine("adopt", root)

        self.assertEqual(read(self.dc(root, "engine.Dockerfile")), "FROM scratch" + NL + "# ours" + NL)
        self.assertTrue(os.path.isfile(self.dc(root, "docker-compose.yml")))

    def test_only_a_missing_file_is_added(self):
        root = self.fresh_repo()
        self.engine("adopt", root)
        kept = read(self.dc(root, "docker-compose.yml"))
        os.remove(self.dc(root, "engine-entrypoint.sh"))

        self.engine("adopt", root)

        self.assertTrue(os.access(self.dc(root, "engine-entrypoint.sh"), os.X_OK))
        self.assertEqual(read(self.dc(root, "docker-compose.yml")), kept)

    def test_a_project_with_its_own_compose_file_gets_no_starter_stack(self):
        root = self.fresh_repo()
        write(os.path.join(root, ".devcontainer", "docker-compose.yml"), GOOD_COMPOSE)

        self.engine("adopt", root)

        self.assertEqual(read(self.dc(root, "docker-compose.yml")), GOOD_COMPOSE)
        self.assertFalse(os.path.exists(self.dc(root, "engine.Dockerfile")))


class TheCredentialTemplate(SeedTestCase):
    def test_token_mode_template_lists_the_token_name_and_no_value(self):
        root = self.fresh_repo(auth="token")

        self.engine("adopt", root)

        text = read(self.dc(root, "agent.env.example"))
        self.assertRegex(text, r"(?m)^GH_TOKEN=$")
        self.assertNotRegex(text, r"ghp_|github_pat_")

    def test_app_mode_template_lists_every_app_name_and_no_value(self):
        root = self.fresh_repo(auth="app")

        self.engine("adopt", root)

        text = read(self.dc(root, "agent.env.example"))
        for name in ("FORGE_APP_ID", "FORGE_APP_INSTALLATION_ID", "FORGE_APP_SLUG",
                     "FORGE_APP_BOT_ID", "FORGE_APP_KEY_FILE", "FORGE_APP_REPOS"):
            self.assertRegex(text, r"(?m)^%s=$" % name)

    def test_the_template_is_named_after_the_credential_file_and_the_file_itself_is_never_created(self):
        root = self.fresh_repo(auth="token")

        self.engine("adopt", root)

        self.assertTrue(os.path.isfile(os.path.join(root, ".devcontainer", "agent.env.example")))
        self.assertFalse(os.path.exists(os.path.join(root, ".devcontainer", "agent.env")))

    def test_the_slot_copy_still_leaves_secrets_out(self):
        root = self.fresh_repo(auth="token")
        self.engine("adopt", root)
        write(self.dc(root, "agent.env"), "GH_TOKEN=ghp_SENTINEL_seed" + NL, mode=0o600)

        names = subprocess.run(
            ["bash", "-c", ". lib/engine-paths.sh; devcontainer_exclude_words " + root],
            cwd=REPO_ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        ).stdout.decode()

        self.assertIn("--exclude=*.env", names)
        self.assertNotIn("example", names)


class DoctorReportsTheSeededVersions(SeedTestCase):
    def newer_templates(self):
        target = os.path.join(self.harness.tmp, "templates-v2")
        shutil.copytree(TEMPLATES, target)
        for path in glob.glob(os.path.join(target, "*")):
            text = read(path)
            write(path, text.replace("version: 1", "version: 2", 1))
        return target

    def test_doctor_names_each_starter_file_whose_template_moved_on_and_changes_nothing(self):
        root = self.fresh_repo(auth="token")
        self.engine("adopt", root)
        before = {n: read(self.dc(root, n)) for n in SEEDED}

        result = self.engine("doctor", cwd=root, DEV_CONTAINER_TEMPLATES_DIR=self.newer_templates())

        text = output_of(result)
        for name in SEEDED:
            self.assertIn("%s was seeded from template v1, current is v2" % name, text)
        for name in SEEDED:
            self.assertEqual(read(self.dc(root, name)), before[name])


class TheContractIsCheckedAtEveryAcquire(SeedTestCase):
    def compose(self, text):
        write(os.path.join(self.harness.fixture_repo, ".devcontainer", "docker-compose.yml"), text)

    def refused(self, result):
        text = output_of(result)
        self.assertNotEqual(result.returncode, 0, text)
        self.assertEqual(glob.glob(os.path.join(self.harness.state_dir, "leases", "*.json")), [])
        return text

    def assertFixAndNext(self, text):
        self.assertIn("Fix:", text)
        self.assertIn("Next:", text)

    def test_a_compose_file_that_meets_the_contract_is_accepted(self):
        self.compose(GOOD_COMPOSE)

        result = self.harness.run_acquire()

        self.assertEqual(result.returncode, 0, output_of(result))

    def test_a_file_that_is_not_a_compose_stack_at_all_is_not_judged(self):
        result = self.harness.run_acquire()

        self.assertEqual(result.returncode, 0, output_of(result))

    def test_a_compose_file_without_the_app_service_is_refused(self):
        self.compose("services:" + NL + "  web:" + NL + "    image: x" + NL)

        text = self.refused(self.harness.run_acquire())

        self.assertIn("app", text)
        self.assertIn("docker-compose.yml", text)
        self.assertFixAndNext(text)

    def test_the_live_service_project_env_names_must_exist_too(self):
        with open(os.path.join(self.harness.fixture_repo, "project.env"), "a", encoding="utf-8") as handle:
            handle.write('LIVE_SERVICE="runner"' + NL)
        self.compose(GOOD_COMPOSE)

        text = self.refused(self.harness.run_acquire())

        self.assertIn("runner", text)
        self.assertFixAndNext(text)

    def test_a_literal_host_port_is_refused_with_the_line_and_the_variable_to_use(self):
        self.compose(GOOD_COMPOSE.replace("${SSH_PORT}", "2222"))

        text = self.refused(self.harness.run_acquire())

        self.assertIn("SLOT_PORTS", text)
        self.assertIn("docker-compose.yml:", text)
        self.assertFixAndNext(text)

    def test_a_port_variable_that_slot_ports_does_not_name_is_refused(self):
        self.compose(GOOD_COMPOSE.replace("${SSH_PORT}", "${DEBUG_PORT}"))

        text = self.refused(self.harness.run_acquire())

        self.assertIn("DEBUG_PORT", text)
        self.assertFixAndNext(text)

    def test_a_port_variable_with_a_default_is_accepted_when_slot_ports_names_it(self):
        self.compose(GOOD_COMPOSE.replace("${SSH_PORT}", "${SSH_PORT:-2222}"))

        result = self.harness.run_acquire()

        self.assertEqual(result.returncode, 0, output_of(result))

    def test_a_container_only_port_is_accepted(self):
        self.compose(GOOD_COMPOSE.replace('"127.0.0.1:${SSH_PORT}:22"', '"22"'))

        result = self.harness.run_acquire()

        self.assertEqual(result.returncode, 0, output_of(result))

    def test_a_token_written_into_compose_is_refused_and_its_value_is_never_printed(self):
        secret = "ghp_" + "SENTINELcompose0123456789abcdef"
        self.compose(GOOD_COMPOSE.replace("${GH_TOKEN}", secret))

        text = self.refused(self.harness.run_acquire())

        self.assertIn("docker-compose.yml:", text)
        self.assertNotIn(secret, text)
        self.assertFixAndNext(text)

    def test_a_secret_named_variable_with_a_literal_value_is_refused(self):
        self.compose(GOOD_COMPOSE.replace("      - GIT_USER_NAME", "      DB_PASSWORD: hunter2hunter2hunter2xyz"))

        text = self.refused(self.harness.run_acquire())

        self.assertIn("DB_PASSWORD", text)
        self.assertNotIn("hunter2", text)

    def test_a_short_local_database_password_is_not_taken_for_a_credential(self):
        self.compose(GOOD_COMPOSE.replace("      - GIT_USER_NAME", "      POSTGRES_PASSWORD: postgres"))

        result = self.harness.run_acquire()

        self.assertEqual(result.returncode, 0, output_of(result))

    def test_secrets_passed_by_name_or_by_reference_are_accepted(self):
        self.compose(GOOD_COMPOSE.replace("      - GIT_USER_NAME", "      - GH_TOKEN" + NL + "      API_KEY: ${API_KEY}"))

        result = self.harness.run_acquire()

        self.assertEqual(result.returncode, 0, output_of(result))

    def test_doctor_reports_the_same_problems(self):
        self.compose(GOOD_COMPOSE.replace("${SSH_PORT}", "2222"))

        result = self.engine("doctor")

        text = output_of(result)
        self.assertNotEqual(result.returncode, 0, text)
        self.assertIn("SLOT_PORTS", text)


    def worker_mounting(self, source):
        return GOOD_COMPOSE + NL.join(
            ["  worker:", "    image: x", "    volumes:", "      - " + source + ":/workspace", ""]
        )

    def test_workspace_dir_as_a_host_path_outside_app_is_refused_with_file_line_and_fix(self):
        self.compose(self.worker_mounting("${WORKSPACE_DIR}"))

        text = self.refused(self.harness.run_acquire())

        self.assertIn("docker-compose.yml:12 uses ${WORKSPACE_DIR} as a host path in the worker service", text)
        self.assertFixAndNext(text)

    def test_workspace_dir_with_a_default_or_subpath_outside_app_is_refused(self):
        for source in ("${WORKSPACE_DIR:-..}", "${WORKSPACE_DIR}/src"):
            with self.subTest(source=source):
                self.compose(self.worker_mounting(source))

                text = self.refused(self.harness.run_acquire())

                self.assertIn("docker-compose.yml:12 uses ${WORKSPACE_DIR}", text)

    def test_workspace_dir_as_a_long_syntax_bind_source_outside_app_is_refused(self):
        self.compose(
            GOOD_COMPOSE
            + NL.join(
                ["  worker:", "    image: x", "    volumes:", "      - type: bind",
                 "        source: ${WORKSPACE_DIR}", "        target: /workspace", ""]
            )
        )

        text = self.refused(self.harness.run_acquire())

        self.assertIn("docker-compose.yml:13 uses ${WORKSPACE_DIR}", text)

    def test_doctor_reports_workspace_dir_outside_app_and_changes_nothing(self):
        broken = self.worker_mounting("${WORKSPACE_DIR}")
        self.compose(broken)

        result = self.engine("doctor")

        text = output_of(result)
        self.assertNotEqual(result.returncode, 0, text)
        self.assertIn("docker-compose.yml:12 uses ${WORKSPACE_DIR}", text)
        self.assertEqual(read(os.path.join(self.harness.fixture_repo, ".devcontainer", "docker-compose.yml")), broken)

    def test_workspace_dir_as_the_app_service_host_path_is_accepted(self):
        for source in ("${WORKSPACE_DIR:-..}", "${WORKSPACE_DIR}"):
            with self.subTest(source=source):
                self.compose(GOOD_COMPOSE.replace(
                    "    ports:", "    volumes:" + NL + "      - " + source + ":/workspace" + NL + "    ports:"))

                result = self.harness.run_acquire()

                self.assertEqual(result.returncode, 0, output_of(result))

    def test_workspace_dir_in_a_non_path_position_outside_app_is_accepted(self):
        self.compose(GOOD_COMPOSE + NL.join(
            ["  worker:", "    image: x", "    environment:", "      - WORKSPACE_DIR=${WORKSPACE_DIR}", ""]))

        result = self.harness.run_acquire()

        self.assertEqual(result.returncode, 0, output_of(result))


if __name__ == "__main__":
    unittest.main()
