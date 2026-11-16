"""
Unit tests for PersonalServer Production Release Management and Auto-Sync Subsystem.
Verifies release configuration, safe update preflights, syntax verification,
and branch continuity for production cluster nodes.
"""

import os
import sys
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

# Import release_manager module from scripts/
SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import release_manager


class TestProductionAutoSync(unittest.TestCase):

    def test_release_config_defaults_to_main_and_auto_start(self):
        """Verify release.conf specifies channel=main and auto_update_on_start=true."""
        config = release_manager.load_release_config()
        self.assertEqual(config.get("channel"), "main")
        self.assertTrue(config.get("auto_update_on_start"))
        self.assertTrue(config.get("offline_safe"))

    @patch("release_manager.run_git_command")
    def test_fetch_release_target_remote_success(self, mock_git):
        """Test fetch_release_target successfully discovers target commit on main branch."""
        # 1st call: rev-parse local
        # 2nd call: fetch origin main
        # 3rd call: rev-parse remote/main
        mock_git.side_effect = [
            (0, "local_hash_111", ""),
            (0, "", ""),
            (0, "remote_hash_222", ""),
        ]
        ok, commit, msg = release_manager.fetch_release_target("main", "origin", timeout=5)
        self.assertTrue(ok)
        self.assertEqual(commit, "remote_hash_222")

    @patch("release_manager.run_git_command")
    def test_fetch_release_target_offline_fallback(self, mock_git):
        """Test fetch_release_target safely falls back to local commit when remote is offline."""
        mock_git.side_effect = [
            (0, "local_hash_111", ""),       # rev-parse local
            (1, "", "Connection timed out"),  # fetch failed
            (1, "", "Connection timed out"),  # fallback fetch failed
        ]
        ok, commit, msg = release_manager.fetch_release_target("main", "origin", timeout=5)
        self.assertTrue(ok)
        self.assertEqual(commit, "local_hash_111")
        self.assertIn("Remote fetch failed", msg)

    @patch("release_manager.run_git_command")
    @patch("release_manager.check_local_modifications")
    def test_apply_release_preserves_main_branch(self, mock_check_mods, mock_git):
        """Test apply_release checks out target commit on named channel branch without detached HEAD."""
        mock_check_mods.return_value = (False, [])
        # get_current_git_commit before and after
        with patch("release_manager.get_current_git_commit") as mock_get_commit:
            mock_get_commit.side_effect = ["old_commit_000", "new_commit_999"]
            mock_git.return_value = (0, "", "")

            state = {"installed_commit": "old_commit_000"}
            ok, msg = release_manager.apply_release("new_commit_999", state, force=False, channel="main")

            self.assertTrue(ok)
            self.assertEqual(state["installed_commit"], "new_commit_999")
            self.assertEqual(state["rollback_commit"], "old_commit_000")
            # Verify checkout was called with -B main
            mock_git.assert_called_with(["checkout", "--quiet", "-B", "main", "new_commit_999"])

    @patch("release_manager.check_local_modifications")
    def test_apply_release_aborts_on_uncommitted_mods(self, mock_check_mods):
        """Test apply_release prevents overwriting local changes when force=False."""
        mock_check_mods.return_value = (True, ["custom_file.py"])
        state = {"installed_commit": "old_commit"}
        ok, msg = release_manager.apply_release("new_commit", state, force=False, channel="main")
        self.assertFalse(ok)
        self.assertIn("Local modifications present", msg)


if __name__ == "__main__":
    unittest.main()
