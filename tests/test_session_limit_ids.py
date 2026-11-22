import json
import os
import unittest

from harness import DevContainerTestCase

UUID = "4e0c1a6b-1111-4222-8333-444455556666"
PREFIXED = "local_" + UUID


class SessionLimitIdSpelling(DevContainerTestCase):
    ENV_WITH_IDENTITY = "GIT_USER_NAME=demo-agent" + chr(10) + "GIT_USER_EMAIL=demo-agent@example.com" + chr(10)

    def setUp(self):
        super().setUp()
        self.harness.build_fixture_repo(with_parent_env=self.ENV_WITH_IDENTITY)

    def write_limits(self, limits):
        path = os.path.join(self.harness.state_dir, "limits.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(limits, handle)

    def acquire_ok(self, holder, ref):
        result = self.harness.run_acquire(holder=holder, owner="Agent [" + ref + "]")
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        return self.harness.acquired_slot_name(result)

    def assert_refused(self, holder, ref):
        result = self.harness.run_acquire(holder=holder, owner="Agent [" + ref + "]")
        self.assertEqual(result.returncode, 4, result.stderr.decode())
        return result

    def test_a_limit_under_the_prefixed_id_applies_to_the_bare_id(self):
        self.write_limits({"default": 1, "sessions": {PREFIXED: 2}})

        self.acquire_ok("first", UUID)
        self.acquire_ok("second", UUID)
        self.assert_refused("third", UUID)

    def test_a_limit_under_the_bare_id_applies_to_the_prefixed_id(self):
        self.write_limits({"default": 1, "sessions": {UUID: 2}})

        self.acquire_ok("first", PREFIXED)
        self.acquire_ok("second", PREFIXED)
        self.assert_refused("third", PREFIXED)

    def test_an_unknown_id_still_gets_the_default(self):
        self.write_limits({"default": 1, "sessions": {PREFIXED: 5}})

        self.acquire_ok("first", "local_other")
        self.assert_refused("second", "other")

    def test_slots_held_under_one_spelling_count_for_the_other(self):
        self.write_limits({"default": 1})

        self.acquire_ok("first", PREFIXED)
        self.assert_refused("second", UUID)

    def test_slots_held_under_the_bare_spelling_count_for_the_prefixed_one(self):
        self.write_limits({"default": 1})

        self.acquire_ok("first", UUID)
        self.assert_refused("second", PREFIXED)

    def test_a_session_mixing_spellings_is_counted_once_against_a_raised_limit(self):
        self.write_limits({"default": 1, "sessions": {PREFIXED: 2}})

        self.acquire_ok("first", PREFIXED)
        self.acquire_ok("second", UUID)
        self.assert_refused("third", PREFIXED)
        self.assert_refused("fourth", UUID)

    def test_when_both_spellings_have_a_key_the_exact_one_wins(self):
        self.write_limits({"default": 1, "sessions": {PREFIXED: 3, UUID: 2}})

        self.acquire_ok("first", UUID)
        self.acquire_ok("second", UUID)
        self.assert_refused("third", UUID)

    def test_when_both_spellings_have_a_key_the_exact_one_wins_for_the_prefixed_call(self):
        self.write_limits({"default": 1, "sessions": {PREFIXED: 3, UUID: 2}})

        self.acquire_ok("first", PREFIXED)
        self.acquire_ok("second", PREFIXED)
        self.acquire_ok("third", PREFIXED)
        self.assert_refused("fourth", PREFIXED)

    def test_the_session_id_in_the_environment_matches_a_key_in_the_other_spelling(self):
        self.harness.session_id = UUID
        self.write_limits({"default": 1, "sessions": {PREFIXED: 2}})

        self.acquire_ok("first", "x")
        self.acquire_ok("second", "x")
        self.assert_refused("third", "x")

    def test_zero_session_limit_pauses_only_that_session(self):
        self.write_limits({"default": 2, "sessions": {UUID: 0}})

        self.assert_refused("first", UUID)
        self.assert_refused("second", PREFIXED)
        self.acquire_ok("other", "other-session")

    def test_negative_session_limit_clamps_to_zero_and_refuses(self):
        self.write_limits({"default": 2, "sessions": {PREFIXED: -4}})

        self.assert_refused("first", UUID)
        self.assert_refused("second", PREFIXED)

    def test_negative_default_limit_clamps_to_zero_and_refuses(self):
        self.write_limits({"default": -2})

        self.assert_refused("first", UUID)

    def test_session_limit_above_twenty_clamps_to_twenty_and_allows_acquires(self):
        self.write_limits({"default": 1, "sessions": {UUID: 25}})

        self.acquire_ok("first", UUID)
        self.acquire_ok("second", PREFIXED)


if __name__ == "__main__":
    unittest.main()
