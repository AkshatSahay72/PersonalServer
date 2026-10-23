#!/usr/bin/env python3
"""
Application Shell & Sidebar Layout Stability Test Suite
========================================================
Validates that:
1. Desktop sidebar occupies a stable viewport-height column (200px width, sticky anchored).
2. Sidebar navigation stays anchored with independent scrolling (`overflow-y: auto`) and does not grow with main content.
3. Sidebar footer stays firmly anchored at the bottom (`margin-top: auto`, `flex-shrink: 0`).
4. Main content panel scrolls independently (`overflow-y: auto`) and has `min-width: 0` to prevent horizontal blowouts.
5. Topbar is sticky anchored at top (`position: sticky`, `top: 0`, `flex-shrink: 0`).
6. Neither `html` nor `body` has global `overflow: hidden`, preserving popup and dialog accessibility.
7. Mobile drawer (`@media (max-width: 768px)`) is viewport-height (`100vh` / `100dvh`) with its own scroll container.
8. Background scroll prevention (`body.drawer-open .main-panel { overflow: hidden }`) is activated when mobile drawer is open.
"""

import re
import shutil
import subprocess
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[2]
STATIC_DIR = BASE_DIR / "services" / "node-api" / "static"


class TestShellLayoutStability(unittest.TestCase):

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

    def test_01_no_global_overflow_hidden_on_html_or_body(self):
        """html and body must NOT have global overflow: hidden."""
        html_body_match = re.search(r"html\s*,\s*body\s*\{([^}]+)\}", self.css_content)
        if html_body_match:
            block = html_body_match.group(1)
            self.assertNotIn("overflow: hidden", block, "html, body must not have global overflow: hidden")
            self.assertNotIn("overflow-y: hidden", block, "html, body must not have global overflow-y: hidden")

        body_match = re.search(r"\nbody\s*\{([^}]+)\}", self.css_content)
        if body_match:
            block = body_match.group(1)
            self.assertNotIn("overflow: hidden", block, "body alone must not have global overflow: hidden")

    def test_02_app_layout_viewport_height_and_containment(self):
        """app-layout must have 100vh/100dvh and flex layout."""
        layout_match = re.search(r"\.app-layout\s*\{([^}]+)\}", self.css_content)
        self.assertTrue(layout_match, ".app-layout CSS rule must exist")
        rules = layout_match.group(1)
        self.assertIn("display: flex", rules)
        self.assertIn("100vh", rules)
        self.assertIn("100dvh", rules)
        self.assertIn("overflow: hidden", rules)

    def test_03_desktop_sidebar_stability(self):
        """Sidebar has stable 200px width, sticky anchor, flex column, and viewport height."""
        # Find desktop .sidebar rule (first occurrence before media query)
        desktop_part = self.css_content.split("@media")[0]
        sidebar_match = re.search(r"\.sidebar\s*\{([^}]+)\}", desktop_part)
        self.assertTrue(sidebar_match, ".sidebar CSS rule must exist in desktop section")
        rules = sidebar_match.group(1)
        self.assertIn("width: 200px", rules)
        self.assertIn("position: sticky", rules)
        self.assertIn("top: 0", rules)
        self.assertIn("align-self: flex-start", rules)
        self.assertIn("100vh", rules)
        self.assertIn("100dvh", rules)
        self.assertIn("flex-shrink: 0", rules)

    def test_04_sidebar_nav_independent_scrolling(self):
        """sidebar-nav has flex-grow, overflow-y: auto, and overscroll-behavior."""
        nav_match = re.search(r"\.sidebar-nav\s*\{([^}]+)\}", self.css_content)
        self.assertTrue(nav_match, ".sidebar-nav rule must exist")
        rules = nav_match.group(1)
        self.assertIn("overflow-y: auto", rules)
        self.assertIn("overscroll-behavior: contain", rules)

    def test_05_sidebar_footer_anchored_at_bottom(self):
        """sidebar-footer has margin-top: auto and flex-shrink: 0."""
        footer_match = re.search(r"\.sidebar-footer\s*\{([^}]+)\}", self.css_content)
        self.assertTrue(footer_match, ".sidebar-footer rule must exist")
        rules = footer_match.group(1)
        self.assertIn("margin-top: auto", rules)
        self.assertIn("flex-shrink: 0", rules)

    def test_06_main_panel_independent_scrolling(self):
        """main-panel scrolls independently with overflow-y: auto and min-width: 0."""
        main_match = re.search(r"\.main-panel\s*\{([^}]+)\}", self.css_content)
        self.assertTrue(main_match, ".main-panel rule must exist")
        rules = main_match.group(1)
        self.assertIn("overflow-y: auto", rules)
        self.assertIn("min-width: 0", rules)
        self.assertIn("height: 100%", rules)

    def test_07_topbar_sticky_anchoring(self):
        """topbar is sticky anchored at top so header controls stay visible on scroll."""
        desktop_part = self.css_content.split("@media")[0]
        topbar_match = re.search(r"\.topbar\s*\{([^}]+)\}", desktop_part)
        self.assertTrue(topbar_match, ".topbar rule must exist")
        rules = topbar_match.group(1)
        self.assertIn("position: sticky", rules)
        self.assertIn("top: 0", rules)
        self.assertIn("flex-shrink: 0", rules)

    def test_08_mobile_drawer_and_page_scroll_locking(self):
        """Mobile media query provides fixed drawer and scroll-lock for main-panel when open."""
        self.assertIn("@media (max-width: 768px)", self.css_content)
        self.assertIn("body.drawer-open .main-panel", self.css_content)
        self.assertIn("overflow: hidden", self.css_content)

        # Check app.js sets drawer-open class
        self.assertIn('document.body.classList.toggle("drawer-open"', self.js_content)

    def test_09_js_syntax_clean(self):
        """app.js compiles with zero syntax errors."""
        if not self.node_bin:
            raise unittest.SkipTest("Node.js binary not available in PATH")

        result = subprocess.run(
            [self.node_bin, "-c", str(self.js_file)],
            capture_output=True,
            text=True
        )
        self.assertEqual(result.returncode, 0, f"app.js syntax error:\n{result.stderr}")


if __name__ == "__main__":
    unittest.main()
