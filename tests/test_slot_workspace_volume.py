import os
import unittest

import test_doctor
import test_seed_contract as seed
from test_doctor import output_of

NL = chr(10)

MOUNTING = NL.join(
    [
        "services:",
        "  app:",
        "    image: node:22-bookworm",
        "    volumes:",
        "      - ${WORKSPACE_DIR:-..}:/workspace",
        "    ports:",
        '      - "127.0.0.1:${SSH_PORT}:22"',
        "",
    ]
)

DECLARED = MOUNTING + NL.join(
    [
        "volumes:",
        "  slot-workspace:",
        "    external: true",
        "    name: ${SLOT_WORKSPACE_VOLUME:-demo-workspace}",
        "",
    ]
)

# The stanza the message must name, piece by piece.
STANZA = ("slot-workspace:", "external: true", "SLOT_WORKSPACE_VOLUME:-")


def warning_lines(text):
    return [line for line in text.splitlines() if "slot-workspace" in line]


class DoctorWarnsWithoutASlotWorkspaceVolume(test_doctor.DoctorTestCase):
    def compose(self, text):
        seed.write(os.path.join(self.harness.fixture_repo, ".devcontainer", "docker-compose.yml"), text)

    def test_a_compose_file_mounting_workspace_dir_without_the_volume_is_reported_with_the_stanza(self):
        self.compose(MOUNTING)

        result = self.doctor()

        text = output_of(result)
        line = self.line_with(text, "slot-workspace")
        self.assertRegex(line, r"(?i)^warn")
        self.assertIn("WORKSPACE_DIR", text)
        for piece in STANZA:
            self.assertIn(piece, text)

    def test_the_report_is_a_warning_and_does_not_fail_the_run(self):
        self.compose(MOUNTING)

        result = self.doctor()

        self.assertEqual(result.returncode, 0, output_of(result))
        self.assertIn("doctor: every prerequisite is met", output_of(result))

    def test_a_compose_file_that_declares_the_volume_is_not_reported(self):
        self.compose(DECLARED)

        result = self.doctor()

        self.assertEqual(result.returncode, 0, output_of(result))
        self.assertEqual(warning_lines(output_of(result)), [])

    def test_a_compose_file_that_does_not_mount_workspace_dir_is_not_reported(self):
        self.compose(seed.GOOD_COMPOSE)

        result = self.doctor()

        self.assertEqual(result.returncode, 0, output_of(result))
        self.assertEqual(warning_lines(output_of(result)), [])


class AcquireStillAcceptsAComposeFileWithoutTheVolume(seed.SeedTestCase):
    def test_run_adds_its_own_overlay_so_the_missing_volume_does_not_refuse_the_acquire(self):
        seed.write(os.path.join(self.harness.fixture_repo, ".devcontainer", "docker-compose.yml"), MOUNTING)

        result = self.harness.run_acquire()

        self.assertEqual(result.returncode, 0, output_of(result))


class AdoptReportsAMissingSlotWorkspaceVolume(seed.SeedTestCase):
    def test_adopt_names_the_missing_stanza_for_a_project_compose_file(self):
        root = self.fresh_repo()
        seed.write(self.dc(root, "docker-compose.yml"), MOUNTING)

        result = self.engine("adopt", root)

        text = output_of(result)
        self.assertEqual(result.returncode, 0, text)
        for piece in STANZA:
            self.assertIn(piece, text)

    def test_adopt_is_quiet_when_the_volume_is_declared(self):
        root = self.fresh_repo()
        seed.write(self.dc(root, "docker-compose.yml"), DECLARED)

        result = self.engine("adopt", root)

        self.assertEqual(result.returncode, 0, output_of(result))
        self.assertEqual(warning_lines(output_of(result)), [])


if __name__ == "__main__":
    unittest.main()
