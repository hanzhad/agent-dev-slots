"""Forge-token cache files are named by the project KEY, not by its label.

Two projects may share a label (the registry hands the second one LABEL_KEY, but
a file named by label is still a name the engine cannot vouch for); a root's key
is the one name that is its own. A cache left under the old label name is refused
with a pointer to migrate-identity, as the signing key is.
"""
import os
import unittest

import test_agent_auth as ta
from test_agent_auth import setUpModule, tearDownModule  # noqa: F401  (the App key the base class places)

NL = chr(10)
STAMP = "9999999999" + NL


class TokenFilesNamedByKey(ta.GithubAppRun):

    def key(self):
        return self.harness.project_key

    def legacy(self, suffix=""):
        return os.path.join(self.token_dir(), ta.PROJECT_PREFIX + suffix)

    def test_the_cache_and_nothing_else_is_named_by_the_key(self):
        res = self.run_slot()

        self.assertEqual(res.returncode, 0, ta.text(res))
        self.assertEqual(self.token_entries(), [self.key()])
        self.assertFalse(os.path.exists(self.legacy()))

    def test_the_lock_is_named_by_the_key(self):
        self.use_quick_sleep()
        holder = self.hold_mint()

        res = self.run_slot()

        self.assertEqual(res.returncode, 1, ta.text(res))
        self.assertIn(os.path.join("tokens", self.key() + ".lock"), res.stderr.decode())
        self.release_mint()
        self.finish(holder)

    def test_a_cache_kept_under_the_old_label_name_is_refused_and_points_at_migrate_identity(self):
        ta.write(self.legacy(), STAMP + ta.OLD + NL, 0o600)

        res = self.run_slot()

        self.assertNotEqual(res.returncode, 0)
        self.assertIn("migrate-identity", res.stderr.decode())
        self.assertEqual(self.curl_argv(), [])
        self.assertEqual(self.execs(), [])
        self.assertFalse(os.path.exists(self.cache_file()))
        self.assertEqual(ta.read_if(self.legacy()), STAMP + ta.OLD + NL)

    def test_a_lock_kept_under_the_old_label_name_is_refused_too(self):
        ta.write(self.legacy(".lock"), "", 0o600)

        res = self.run_slot()

        self.assertNotEqual(res.returncode, 0)
        self.assertIn("migrate-identity", res.stderr.decode())
        self.assertEqual(self.curl_argv(), [])
        self.assertEqual(self.execs(), [])

    def test_once_the_key_named_cache_exists_a_leftover_old_name_is_not_in_the_way(self):
        self.write_cache(45 * 60, ta.OLD)
        ta.write(self.legacy(), "1" + NL + "stale" + NL, 0o600)

        res = self.run_slot()

        self.assertEqual(res.returncode, 0, ta.text(res))
        self.assertEqual(self.exec_tokens(), [ta.OLD])
        self.assertEqual(self.curl_argv(), [])


if __name__ == "__main__":
    unittest.main()
