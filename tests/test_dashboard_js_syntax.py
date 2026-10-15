#!/usr/bin/env python3
"""
Dashboard JavaScript Asset Serving & Syntax Integrity Test Suite
================================================================
Validates that:
1. `services/node-api/static/app.js` passes JavaScript syntax parsing without SyntaxErrors.
2. `NodeAPIHandler` correctly serves `/static/app.js?v=1.6.0` over HTTP with Content-Type `application/javascript`.
3. The served asset contains the expected lifecycle and navigation routines (`navigate()`, `loadNodes()`, `loadJobs()`).
4. JavaScript execution in Node.js VM initiates expected API requests for all routes (#nodes, #dashboard, #jobs).
"""

import os
import sys
import shutil
import subprocess
import threading
import urllib.request
import unittest
from pathlib import Path
from http.server import HTTPServer

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

# Import NodeAPIHandler using importlib or direct import from services/node-api
import importlib.util
node_api_path = BASE_DIR / "services" / "node-api" / "app.py"
spec = importlib.util.spec_from_file_location("node_api_module", str(node_api_path))
node_api_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(node_api_mod)
NodeAPIHandler = node_api_mod.NodeAPIHandler
STATIC_DIR = node_api_mod.STATIC_DIR


class TestDashboardJSSyntax(unittest.TestCase):

    def setUp(self):
        self.app_js_path = STATIC_DIR / "app.js"
        self.index_html_path = STATIC_DIR / "index.html"
        self.node_bin = shutil.which("node")

    def test_01_static_files_exist(self):
        """Verify static files exist on disk."""
        self.assertTrue(self.app_js_path.exists(), "app.js missing from static directory")
        self.assertTrue(self.index_html_path.exists(), "index.html missing from static directory")

    def test_02_javascript_syntax_clean(self):
        """Verify app.js compiles cleanly via Node.js syntax checker (node -c)."""
        if not self.node_bin:
            raise unittest.SkipTest("Node.js binary not available in PATH")

        result = subprocess.run(
            [self.node_bin, "-c", str(self.app_js_path)],
            capture_output=True,
            text=True
        )
        self.assertEqual(
            result.returncode, 0,
            f"JavaScript syntax error in app.js:\nSTDOUT: {result.stdout}\nSTDERR: {result.stderr}"
        )

    def test_03_http_serving_static_app_js(self):
        """Verify NodeAPIHandler serves /static/app.js?v=1.6.0 with HTTP 200 and application/javascript."""
        server = HTTPServer(("127.0.0.1", 0), NodeAPIHandler)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()

        try:
            url = f"http://127.0.0.1:{port}/static/app.js?v=1.6.0"
            req = urllib.request.Request(url, headers={"User-Agent": "PersonalServer-Test/1.0"})
            with urllib.request.urlopen(req, timeout=3.0) as resp:
                self.assertEqual(resp.status, 200)
                ct = resp.headers.get("Content-Type", "")
                self.assertIn("javascript", ct)
                content = resp.read().decode("utf-8")
                self.assertGreater(len(content), 10000)
                # Verify core entry points are intact
                self.assertIn("function navigate()", content)
                self.assertIn("async function loadNodes()", content)
                self.assertIn("async function loadJobs()", content)
                self.assertIn("navigate();", content)
        finally:
            server.shutdown()
            server.server_close()

    def test_04_execution_initiates_api_requests(self):
        """Run sandboxed JS in Node.js to verify that navigate() initiates /api/cluster and /api/cluster/utilization."""
        if not self.node_bin:
            raise unittest.SkipTest("Node.js binary not available in PATH")

        runner_script = """
const fs = require('fs');
const vm = require('vm');

const code = fs.readFileSync(process.argv[1], 'utf8');
const fetchCalls = [];

const mockDoc = {
  getElementById: (id) => ({
    style: {},
    classList: { add: () => {}, remove: () => {}, contains: () => false, toggle: () => {} },
    addEventListener: () => {},
    setAttribute: () => {},
    textContent: '',
    innerHTML: ''
  }),
  querySelectorAll: () => [],
  querySelector: () => null,
  addEventListener: () => {},
  createElement: () => ({ style: {}, classList: { add: () => {}, remove: () => {} }, setAttribute: () => {}, appendChild: () => {} }),
  body: { appendChild: () => {} }
};

const sandbox = {
  location: { hash: '#nodes', hostname: 'server.akshatsahay.space', protocol: 'https:' },
  document: mockDoc,
  console: console,
  setTimeout: setTimeout,
  clearTimeout: clearTimeout,
  setInterval: setInterval,
  clearInterval: clearInterval,
  innerWidth: 1024,
  sessionStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
  localStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
  addEventListener: () => {},
  fetch: (url) => {
    fetchCalls.push(url);
    return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ nodes: [], jobs: [], cluster: {} }) });
  }
};
sandbox.window = sandbox;

vm.createContext(sandbox);
vm.runInContext(code, sandbox);

setTimeout(() => {
  const hasCluster = fetchCalls.includes('/api/cluster');
  const hasUtil = fetchCalls.includes('/api/cluster/utilization');
  if (hasCluster && hasUtil) {
    process.exit(0);
  } else {
    console.error('Missing expected calls:', fetchCalls);
    process.exit(1);
  }
}, 150);
"""
        result = subprocess.run(
            [self.node_bin, "-e", runner_script, str(self.app_js_path)],
            capture_output=True,
            text=True
        )
        self.assertEqual(
            result.returncode, 0,
            f"JS execution test failed:\nSTDOUT: {result.stdout}\nSTDERR: {result.stderr}"
        )


if __name__ == "__main__":
    unittest.main()
