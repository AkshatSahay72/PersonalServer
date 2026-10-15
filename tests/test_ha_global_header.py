#!/usr/bin/env python3
"""
High Availability Global Header & Telemetry Test Suite
======================================================
Validates that:
1. Global header displays authoritative HA Leader, Term, and Status across all views.
2. The legacy hardcoded wording "👑 Master (Leader)" is eliminated.
3. Node ID to human-readable hostname resolution exists in JavaScript.
4. Five distinct states (Active Leader, Standby, Election, Fenced, Unreachable) are modeled with CSS classes.
5. Interactive HA details modal provides telemetry inspection and one-click leader navigation.
6. JavaScript syntax in `app.js` passes compilation cleanly.
"""

import os
import sys
import re
import shutil
import subprocess
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "services" / "node-api" / "static"


class TestHAGlobalHeaderUI(unittest.TestCase):

    def setUp(self):
        self.html_file = STATIC_DIR / "index.html"
        self.css_file = STATIC_DIR / "style.css"
        self.js_file = STATIC_DIR / "app.js"
        self.node_bin = shutil.which("node")

        self.assertTrue(self.html_file.exists(), "index.html must exist")
        self.assertTrue(self.css_file.exists(), "style.css must exist")
        self.assertTrue(self.js_file.exists(), "app.js must exist")

        with open(self.html_file, "r", encoding="utf-8") as f:
            self.html_content = f.read()

        with open(self.css_file, "r", encoding="utf-8") as f:
            self.css_content = f.read()

        with open(self.js_file, "r", encoding="utf-8") as f:
            self.js_content = f.read()

    def test_01_html_header_ha_button_and_elements_exist(self):
        """Header contains interactive #topbar-ha-btn and structured status elements."""
        self.assertIn('id="topbar-ha-btn"', self.html_content)
        self.assertIn('id="ha-header-dot"', self.html_content)
        self.assertIn('id="ha-header-leader"', self.html_content)
        self.assertIn('id="ha-header-term"', self.html_content)
        self.assertIn('id="ha-header-status"', self.html_content)
        self.assertIn('openHADetailsModal()', self.html_content)

        # Ensure the old redundant static text is removed from the visible topbar
        self.assertNotIn('>👑 Master (Leader)<', self.html_content)
        self.assertNotIn('>HA Master:<', self.html_content)

    def test_02_html_ha_details_modal_exists(self):
        """HA details modal exists in index.html with telemetry fields and actions."""
        self.assertIn('id="ha-details-modal-backdrop"', self.html_content)
        self.assertIn('id="ha-modal-status-banner"', self.html_content)
        self.assertIn('id="ha-modal-leader-id"', self.html_content)
        self.assertIn('id="ha-modal-leader-name"', self.html_content)
        self.assertIn('id="ha-modal-term"', self.html_content)
        self.assertIn('id="ha-modal-lease"', self.html_content)
        self.assertIn('id="ha-modal-standbys"', self.html_content)
        self.assertIn('id="ha-modal-view-node-btn"', self.html_content)
        self.assertIn('id="ha-modal-failover-btn"', self.html_content)

    def test_03_css_contains_all_five_ha_states_and_responsive_rules(self):
        """CSS defines distinct styles for active, standby, election, fenced, and unknown states."""
        required_classes = [
            ".topbar-ha-btn",
            ".ha-status-dot",
            ".ha-dot-active",
            ".ha-dot-standby",
            ".ha-dot-election",
            ".ha-dot-fenced",
            ".ha-dot-unknown",
            ".ha-header-status-badge",
            ".ha-status-active",
            ".ha-status-standby",
            ".ha-status-election",
            ".ha-status-fenced",
            ".ha-status-unknown"
        ]
        for cls in required_classes:
            self.assertIn(cls, self.css_content, f"CSS rule {cls} should be defined in style.css")

        # Responsive hide rule for term
        self.assertIn(".ha-header-term", self.css_content)
        self.assertTrue(
            "max-width" in self.css_content and ".ha-header-term" in self.css_content,
            "Responsive media query should compact the HA label on narrower screens"
        )

    def test_04_js_syntax_valid(self):
        """app.js compiles cleanly with no syntax errors."""
        if not self.node_bin:
            raise unittest.SkipTest("Node.js binary not available in PATH")

        result = subprocess.run(
            [self.node_bin, "-c", str(self.js_file)],
            capture_output=True,
            text=True
        )
        self.assertEqual(
            result.returncode, 0,
            f"JavaScript syntax error in app.js:\n{result.stderr}"
        )

    def test_05_js_core_ha_functions_implemented(self):
        """app.js defines authoritative leader mapping, rendering, error handling, and modal controls."""
        required_identifiers = [
            "resolveNodeDisplayName",
            "renderHAHeader",
            "renderHAHeaderError",
            "renderHADetailsModalContent",
            "renderHADetailsModalError",
            "openHADetailsModal",
            "closeHADetailsModal",
            "viewLeaderNodeFromModal",
            "triggerFailoverFromModal",
            "updateHAStatus"
        ]
        for ident in required_identifiers:
            self.assertIn(ident, self.js_content, f"Function or variable {ident} should be defined in app.js")

    def test_06_js_navigate_and_polling_coverage(self):
        """updateHAStatus() is called inside navigate() and registered with periodic polling."""
        # Check navigate() calls updateHAStatus()
        navigate_match = re.search(r"function\s+navigate\s*\(\)\s*\{([\s\S]*?)\n\}\s*\nwindow\.addEventListener", self.js_content)
        self.assertTrue(navigate_match, "navigate() function must exist in app.js")
        self.assertIn("updateHAStatus()", navigate_match.group(1), "navigate() must call updateHAStatus() on every page switch")

        # Check periodic polling
        self.assertIn("setInterval", self.js_content)
        self.assertTrue(
            "updateHAStatus" in self.js_content and "15000" in self.js_content,
            "Periodic polling for updateHAStatus should be set"
        )


if __name__ == "__main__":
    unittest.main()
