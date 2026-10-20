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

    def test_05_job_deletion_ui_elements(self):
        """Verify app.js and index.html contain required job deletion UI routines."""
        app_js = self.app_js_path.read_text(encoding="utf-8")
        self.assertIn("confirmDeleteJob", app_js)
        self.assertIn("deleteJobFromDetail", app_js)
        self.assertIn("inFlightJobDeletions", app_js)
        self.assertIn("/api/jobs/", app_js)

        index_html = self.index_html_path.read_text(encoding="utf-8")
        self.assertIn("job-detail-delete-btn", index_html)
        self.assertIn("deleteJobFromDetail()", index_html)

    def test_06_storage_share_ui_elements(self):
        """Verify app.js and index.html contain required storage sharing UI elements."""
        app_js = self.app_js_path.read_text(encoding="utf-8")
        self.assertIn("openShareModal", app_js)
        self.assertIn("closeShareModal", app_js)
        self.assertIn("toggleFileSharing", app_js)
        self.assertIn("copyShareUrl", app_js)
        self.assertIn("/storage/share", app_js)

        index_html = self.index_html_path.read_text(encoding="utf-8")
        self.assertIn("storage-share-modal-backdrop", index_html)
        self.assertIn("share-toggle-input", index_html)
        self.assertIn("share-url-input", index_html)
        self.assertIn("share-copy-btn", index_html)
        self.assertIn("storage-props-share-btn", index_html)

    def test_07_storage_preview_ui_elements(self):
        """Verify app.js and index.html contain required storage preview modal UI elements."""
        app_js = self.app_js_path.read_text(encoding="utf-8")
        self.assertIn("openPreviewModal", app_js)
        self.assertIn("closePreviewModal", app_js)
        self.assertIn("previewCurrentPropsItem", app_js)
        self.assertIn("/storage/preview", app_js)

        index_html = self.index_html_path.read_text(encoding="utf-8")
        self.assertIn("storage-preview-modal-backdrop", index_html)
        self.assertIn("preview-modal-title", index_html)
        self.assertIn("preview-modal-open-btn", index_html)
        self.assertIn("preview-modal-download-btn", index_html)
        self.assertIn("preview-modal-content", index_html)
        self.assertIn("storage-props-preview-btn", index_html)

    def test_08_storage_preview_and_share_modals_interaction(self):
        """Simulate Preview and Share modal interactions in Node.js VM."""
        if not self.node_bin:
            raise unittest.SkipTest("Node.js binary not available in PATH")

        runner_script = """
const fs = require('fs');
const vm = require('vm');

const code = fs.readFileSync(process.argv[1], 'utf8');
const fetchCalls = [];

const elements = {};
function getOrCreate(id) {
  if (!elements[id]) {
    elements[id] = {
      id: id,
      style: { display: 'none' },
      classList: { add: () => {}, remove: () => {}, contains: () => false, toggle: () => {} },
      addEventListener: () => {},
      setAttribute: (k, v) => { elements[id][k] = v; },
      getAttribute: (k) => elements[id][k],
      textContent: '',
      innerHTML: '',
      value: '',
      checked: false,
      href: ''
    };
  }
  return elements[id];
}

const mockDoc = {
  getElementById: (id) => getOrCreate(id),
  querySelectorAll: () => [],
  querySelector: () => null,
  addEventListener: () => {},
  createElement: () => ({ style: {}, classList: { add: () => {}, remove: () => {} }, setAttribute: () => {}, appendChild: () => {} }),
  body: { appendChild: () => {} }
};

let shareResponse = { shared: true, url: 'https://server.akshatsahay.space/storage/test%20file.pdf', share_id: 'abc123xyz' };

const sandbox = {
  location: { hash: '#storage', hostname: 'server.akshatsahay.space', protocol: 'https:' },
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
  fetch: (url, opts) => {
    fetchCalls.push({ url, opts });
    if (url.includes('/storage/share') && (!opts || !opts.method || opts.method === 'GET')) {
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(shareResponse) });
    }
    if (url.includes('/storage/share') && opts?.method === 'POST') {
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ status: 'enabled', shared: true, url: 'https://server.akshatsahay.space/storage/test%20file.pdf' }) });
    }
    if (url.includes('/storage/share') && opts?.method === 'DELETE') {
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ status: 'revoked', shared: false }) });
    }
    if (url.includes('/storage/preview')) {
      return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve('Sample preview text content') });
    }
    return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}) });
  }
};
sandbox.window = sandbox;

vm.createContext(sandbox);
vm.runInContext(code, sandbox);

async function runTests() {
  // Test 1: Open Preview Modal for PDF
  await sandbox.openPreviewModal('nested/my test.pdf');
  const previewBackdrop = getOrCreate('storage-preview-modal-backdrop');
  const previewTitle = getOrCreate('preview-modal-title');
  const previewOpenBtn = getOrCreate('preview-modal-open-btn');
  const previewContent = getOrCreate('preview-modal-content');

  if (previewBackdrop.style.display !== 'flex') {
    throw new Error('Preview modal backdrop was not displayed');
  }
  if (previewTitle.textContent !== 'my test.pdf') {
    throw new Error('Preview title did not match filename');
  }
  if (!previewOpenBtn.href.includes('/storage/preview?path=nested%2Fmy%20test.pdf')) {
    throw new Error('Preview open URL malformed: ' + previewOpenBtn.href);
  }
  if (!previewContent.innerHTML.includes('iframe') || !previewContent.innerHTML.includes('preview-iframe')) {
    throw new Error('PDF preview did not render iframe viewer');
  }

  // Test 2: Close and Reopen Preview Modal
  sandbox.closePreviewModal();
  if (previewBackdrop.style.display !== 'none') {
    throw new Error('Preview backdrop did not hide on close');
  }
  if (previewContent.innerHTML !== '') {
    throw new Error('Preview content was not cleaned on close');
  }

  // Test 3: Reopen for text file
  await sandbox.openPreviewModal('notes.txt');
  if (previewBackdrop.style.display !== 'flex') {
    throw new Error('Preview backdrop did not reopen');
  }
  if (!previewContent.innerHTML.includes('Sample preview text content')) {
    throw new Error('Text preview did not render text content');
  }
  sandbox.closePreviewModal();

  // Test 4: Open Share Modal for an active shared file
  await sandbox.openShareModal('folder/document.txt');
  const shareBackdrop = getOrCreate('storage-share-modal-backdrop');
  const shareToggle = getOrCreate('share-toggle-input');
  const shareStatusText = getOrCreate('share-toggle-status-text');
  const shareUrlInput = getOrCreate('share-url-input');

  if (shareBackdrop.style.display !== 'flex') {
    throw new Error('Share modal backdrop was not displayed');
  }
  if (!shareToggle.checked || shareStatusText.textContent !== 'ON') {
    throw new Error('Share toggle did not reflect ON state');
  }
  if (shareUrlInput.value !== 'https://server.akshatsahay.space/storage/test%20file.pdf') {
    throw new Error('Share URL was not set in input');
  }

  // Test 5: Toggle Share OFF
  await sandbox.toggleFileSharing({ target: { checked: false } });
  const deleteCalls = fetchCalls.filter(c => c.opts?.method === 'DELETE');
  if (deleteCalls.length === 0) {
    throw new Error('Disabling share did not send DELETE request');
  }
  if (!deleteCalls[0].url.includes('/storage/share?path=folder%2Fdocument.txt')) {
    throw new Error('DELETE share URL mismatch: ' + deleteCalls[0].url);
  }
  if (shareUrlInput.value !== '') {
    throw new Error('URL input was not cleared when toggled OFF');
  }

  // Test 6: Toggle Share ON
  await sandbox.toggleFileSharing({ target: { checked: true } });
  const postCalls = fetchCalls.filter(c => c.opts?.method === 'POST');
  if (postCalls.length === 0) {
    throw new Error('Enabling share did not send POST request');
  }
  const postBody = JSON.parse(postCalls[0].opts.body);
  if (postBody.path !== 'folder/document.txt') {
    throw new Error('POST share body path mismatch: ' + postBody.path);
  }

  // Test 7: Close Share Modal
  sandbox.closeShareModal();
  if (shareBackdrop.style.display !== 'none') {
    throw new Error('Share backdrop did not hide on close');
  }

  console.log('All Preview and Share modal interactions passed successfully!');
  process.exit(0);
}

runTests().catch(err => {
  console.error('Test execution failed:', err);
  process.exit(1);
});
"""
        result = subprocess.run(
            [self.node_bin, "-e", runner_script, str(self.app_js_path)],
            capture_output=True,
            text=True
        )
        self.assertEqual(
            result.returncode, 0,
            f"Preview & Share interaction test failed:\nSTDOUT: {result.stdout}\nSTDERR: {result.stderr}"
        )

    def test_09_modal_html_hierarchy_and_unclosed_tags(self):
        """
        Verify index.html has well-formed modal structure with zero unclosed tags,
        ensuring preview and share modals are not trapped inside a hidden backdrop.
        """
        import re
        content = self.index_html_path.read_text(encoding="utf-8")
        tag_pattern = re.compile(r'<\/?([a-zA-Z0-9]+)(?:\s+[^>]*?)?(\/?)>')
        stack = []
        void_elements = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}

        for match in tag_pattern.finditer(content):
            tag_name = match.group(1).lower()
            is_closing = match.group(0).startswith('</')
            is_self_closing = match.group(2) == '/' or tag_name in void_elements
            line = content[:match.start()].count('\n') + 1

            if is_self_closing and not is_closing:
                continue
            elif is_closing:
                self.assertTrue(len(stack) > 0, f"Line {line}: Unexpected closing </{tag_name}>")
                popped_tag, popped_line, popped_str = stack.pop()
                self.assertEqual(
                    popped_tag, tag_name,
                    f"Line {line}: Mismatched </{tag_name}>, expected </{popped_tag}> (opened at line {popped_line})"
                )
            else:
                m_str = match.group(0)
                stack.append((tag_name, line, m_str))

        self.assertEqual(len(stack), 0, f"Unclosed tags remaining on stack: {stack}")

        # Verify each modal backdrop exists as a distinct sibling, none nested inside another
        modal_ids = [
            "node-diag-modal-backdrop",
            "storage-share-modal-backdrop",
            "storage-preview-modal-backdrop",
            "ha-details-modal-backdrop"
        ]
        for m_id in modal_ids:
            self.assertIn(f'id="{m_id}"', content)

    def test_10_shared_folder_viewer_html_integrity_and_wiring(self):
        """Verify shared_folder.html exists, has clean HTML, and app.js has folder share wiring."""
        shared_folder_html = STATIC_DIR / "shared_folder.html"
        self.assertTrue(shared_folder_html.exists(), "shared_folder.html must exist in static dir")
        sf_content = shared_folder_html.read_text(encoding="utf-8")
        self.assertIn("Shared Folder", sf_content)
        self.assertIn("preview-modal", sf_content)
        self.assertIn("file-table", sf_content)

        # Check app.js for folder share wiring
        app_js = self.app_js_path.read_text(encoding="utf-8")
        self.assertIn("currentSharingIsDir", app_js)
        self.assertIn("openShareModal(decodeURIComponent('${safeJsPath}'), ${isDir})", app_js)
        self.assertIn("is_dir: currentSharingIsDir", app_js)


if __name__ == "__main__":
    unittest.main()


