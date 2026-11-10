import json
import os

from test_doctor import DoctorTestCase, output_of


class DoctorNamesVanishedRoots(DoctorTestCase):
    def projects_dir(self):
        path = os.path.join(self.harness.state_dir, "projects")
        os.makedirs(path, exist_ok=True)
        return path

    def register(self, key, label, root):
        path = os.path.join(self.projects_dir(), key + ".json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"root": root, "base": label, "label": label}, handle)
        return path

    def vanished(self):
        return os.path.join(self.harness.tmp, "gone-checkout")

    def test_a_root_that_no_longer_exists_gets_one_info_line(self):
        gone = self.vanished()
        self.register("deadbeef", "oldproj", gone)

        result = self.doctor()

        lines = [line for line in output_of(result).splitlines() if "no longer exists" in line]
        self.assertEqual(
            lines,
            [f"note  registered project oldproj (deadbeef) root {gone} no longer exists"],
        )

    def test_the_exit_status_is_that_of_a_registry_without_it(self):
        before = self.doctor()
        self.register("deadbeef", "oldproj", self.vanished())

        after = self.doctor()

        self.assertEqual(after.returncode, before.returncode, output_of(after))

    def test_a_root_that_exists_gets_no_line(self):
        self.register("cafe0001", "liveproj", self.harness.fixture_repo)

        result = self.doctor()

        self.assertNotIn("liveproj", output_of(result))
        self.assertNotIn("no longer exists", output_of(result))

    def test_the_line_is_neither_a_warning_nor_a_failure(self):
        self.register("deadbeef", "oldproj", self.vanished())

        result = self.doctor()

        line = self.line_with(output_of(result), "no longer exists")
        self.assertFalse(line.startswith(("warn", "FAIL")), line)
        self.assertNotIn("FAIL", line)

    def test_the_registry_is_left_as_it_was(self):
        path = self.register("deadbeef", "oldproj", self.vanished())
        with open(path, encoding="utf-8") as handle:
            before = handle.read()

        self.doctor()

        with open(path, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), before)
