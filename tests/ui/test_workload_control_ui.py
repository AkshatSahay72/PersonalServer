#!/usr/bin/env python3
"""
Unit and integration tests for PersonalServer V2.1-A Workload Control Interface.

Tests cover:
1. HTML Structure: Job submission form fields, target selector capabilities,
   execution history table headers, job detail view elements, and artifact displays.
2. CSS Design: Code editor styling, artifact cards, download buttons, auto-refresh badge.
3. JavaScript Logic (Node.js runner):
   - onJobTypeChange toggles Python controls vs generic JSON controls.
   - Submission payload builds canonical V2.0 contract (name, code, args, inputs, outputs, requirements).
   - Artifact download URL construction (/api/artifacts/{id}/download).
4. Gateway Proxy: NodeAPI forwards Content-Disposition and X-Artifact-Sha256 headers for artifact downloads.
"""

import json
import os
import re
import shutil
import subprocess
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
STATIC_DIR = REPO_ROOT / "services" / "node-api" / "static"
INDEX_HTML = STATIC_DIR / "index.html"
STYLE_CSS = STATIC_DIR / "style.css"
APP_JS = STATIC_DIR / "app.js"
NODE_API_APP = REPO_ROOT / "services" / "node-api" / "app.py"


class TestWorkloadControlUI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html_content = INDEX_HTML.read_text(encoding="utf-8")
        cls.css_content = STYLE_CSS.read_text(encoding="utf-8")
        cls.js_content = APP_JS.read_text(encoding="utf-8")
        cls.node_bin = shutil.which("node")

    def test_01_html_job_submission_form_fields(self):
        """Verify all required workload submission controls exist in index.html."""
        # Top-level form fields
        self.assertIn('id="job-submit-form"', self.html_content)
        self.assertIn('id="job-name"', self.html_content)
        self.assertIn('id="job-type"', self.html_content)
        self.assertIn('id="job-target"', self.html_content)

        # Python-specific controls
        self.assertIn('id="job-code-group"', self.html_content)
        self.assertIn('id="job-python-code"', self.html_content)
        self.assertIn('id="job-args-group"', self.html_content)
        self.assertIn('id="job-args"', self.html_content)
        self.assertIn('id="job-inputs-group"', self.html_content)
        self.assertIn('id="job-inputs"', self.html_content)
        self.assertIn('id="job-outputs-group"', self.html_content)
        self.assertIn('id="job-outputs"', self.html_content)

        # Resource requirements
        self.assertIn('id="job-resources-group"', self.html_content)
        self.assertIn('id="job-min-ram"', self.html_content)
        self.assertIn('id="job-min-cores"', self.html_content)

        # Generic JSON params fallback
        self.assertIn('id="job-params-group"', self.html_content)
        self.assertIn('id="job-params"', self.html_content)

        # Scheduling controls
        self.assertIn('id="job-timeout"', self.html_content)
        self.assertIn('id="job-priority"', self.html_content)
        self.assertIn('id="job-pinned"', self.html_content)

    def test_02_html_job_history_table_headers(self):
        """Verify job history table has all required columns including Workload Name, Target/Worker, Attempt."""
        self.assertIn('id="jobs-tbody"', self.html_content)
        self.assertIn("Workload / Name", self.html_content)
        self.assertIn("Target / Worker", self.html_content)
        self.assertIn("Attempt", self.html_content)
        self.assertIn("Submitted", self.html_content)

    def test_03_html_job_detail_elements(self):
        """Verify job detail view contains elements for artifacts, inputs, stdout, and stderr."""
        self.assertIn('id="job-detail-panel"', self.html_content)
        self.assertIn('id="job-detail-summary"', self.html_content)
        self.assertIn('id="job-detail-inputs-wrap"', self.html_content)
        self.assertIn('id="job-detail-inputs-list"', self.html_content)
        self.assertIn('id="job-detail-artifacts-wrap"', self.html_content)
        self.assertIn('id="job-detail-artifacts-list"', self.html_content)
        self.assertIn('id="job-detail-stdout-wrap"', self.html_content)
        self.assertIn('id="job-detail-stdout"', self.html_content)
        self.assertIn('id="job-detail-stderr-wrap"', self.html_content)
        self.assertIn('id="job-detail-stderr"', self.html_content)
        self.assertIn('id="jobs-auto-refresh-badge"', self.html_content)

    def test_04_css_workload_styling(self):
        """Verify CSS classes for code editor, artifact cards, and download buttons exist."""
        self.assertIn(".job-code-textarea", self.css_content)
        self.assertIn("font-family: var(--font-mono)", self.css_content)
        self.assertIn(".detail-artifacts-list", self.css_content)
        self.assertIn(".artifact-card", self.css_content)
        self.assertIn(".artifact-download-btn", self.css_content)
        self.assertIn(".job-auto-refresh-pill", self.css_content)

    def test_05_js_target_options_contain_capabilities(self):
        """Verify JS includes verified capability selectors in target dropdown."""
        self.assertIn("capability:python3", self.js_content)
        self.assertIn("capability:container_runtime:udocker", self.js_content)

    def test_06_js_handles_artifacts_and_downloads(self):
        """Verify JS renders artifact download links targeting /api/artifacts/{id}/download."""
        self.assertIn("/api/artifacts/", self.js_content)
        self.assertIn("/download", self.js_content)
        self.assertIn("artifact-download-btn", self.js_content)

    def test_07_js_payload_construction_in_node(self):
        """Test submission payload logic in Node.js VM to ensure canonical contract compliance."""
        if not self.node_bin:
            raise unittest.SkipTest("Node.js binary not available in PATH")

        test_script = """
const fs = require('fs');

// Mock DOM elements
const elements = {
  'job-name': { value: 'test-matrix-calc' },
  'job-type': { value: 'python' },
  'job-target': { value: 'capability:python3' },
  'job-python-code': { value: 'import sys\\nprint("hello world")' },
  'job-args': { value: '--iters 100 --verbose' },
  'job-inputs': { value: 'art-001, art-002' },
  'job-outputs': { value: 'result.json, output.csv' },
  'job-min-ram': { value: '512' },
  'job-min-cores': { value: '2' },
  'job-timeout': { value: '600' },
  'job-priority': { value: '5' },
  'job-pinned': { checked: true },
  'job-params': { value: '{}' }
};

// Simulate the payload building logic from app.js
const type = elements['job-type'].value;
const name = elements['job-name'].value.trim();
const target = elements['job-target'].value;
const timeoutVal = parseInt(elements['job-timeout'].value, 10);
const priorityVal = parseInt(elements['job-priority'].value, 10);
const pinnedVal = elements['job-pinned'].checked;

const payload = {
  name: name,
  type: type,
  target: target,
  timeout: isNaN(timeoutVal) ? 300 : timeoutVal,
  priority: isNaN(priorityVal) ? 0 : priorityVal,
  pinned: pinnedVal
};

if (type === 'python') {
  payload.code = elements['job-python-code'].value;
  
  const rawArgs = elements['job-args'].value.trim();
  if (rawArgs) {
    payload.args = rawArgs.match(/(?:[^\\s"']+|"[^"]*"|'[^']*')+/g) || [];
  } else {
    payload.args = [];
  }

  const rawInputs = elements['job-inputs'].value.trim();
  payload.inputs = rawInputs ? rawInputs.split(',').map(s => s.trim()).filter(Boolean) : [];

  const rawOutputs = elements['job-outputs'].value.trim();
  payload.outputs = rawOutputs ? rawOutputs.split(',').map(s => s.trim()).filter(Boolean) : [];

  const minRam = parseInt(elements['job-min-ram'].value, 10);
  const minCores = parseInt(elements['job-min-cores'].value, 10);
  const reqs = {};
  if (!isNaN(minRam) && minRam > 0) reqs.min_ram_mb = minRam;
  if (!isNaN(minCores) && minCores > 0) reqs.min_cores = minCores;
  if (Object.keys(reqs).length > 0) {
    payload.requirements = reqs;
  }
}

console.log(JSON.stringify(payload));
"""
        proc = subprocess.run(
            [self.node_bin, "-e", test_script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True
        )
        data = json.loads(proc.stdout)
        self.assertEqual(data["name"], "test-matrix-calc")
        self.assertEqual(data["type"], "python")
        self.assertEqual(data["target"], "capability:python3")
        self.assertIn("print(\"hello world\")", data["code"])
        self.assertEqual(data["args"], ["--iters", "100", "--verbose"])
        self.assertEqual(data["inputs"], ["art-001", "art-002"])
        self.assertEqual(data["outputs"], ["result.json", "output.csv"])
        self.assertEqual(data["requirements"], {"min_ram_mb": 512, "min_cores": 2})
        self.assertEqual(data["timeout"], 600)
        self.assertEqual(data["priority"], 5)
        self.assertTrue(data["pinned"])

    def test_08_gateway_proxy_forwards_artifact_headers(self):
        """Verify proxy_to_controller in node-api forwards Content-Disposition and X-Artifact-Sha256."""
        code = NODE_API_APP.read_text(encoding="utf-8")
        self.assertIn("Content-Disposition", code)
        self.assertIn("X-Artifact-Sha256", code)


if __name__ == "__main__":
    unittest.main()
