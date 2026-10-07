#!/usr/bin/env python3
"""
PersonalServer Minimal Test Application (Phase 11B)
==================================================
A lightweight HTTP service for validating containerized workload deployments.
"""

from http.server import BaseHTTPRequestHandler, HTTPServer
import os

PORT = int(os.environ.get("PORT", 8000))

class DemoAppHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"PersonalServer\nApplication is running.\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        print(f"[DEMO-APP] {self.command} {self.path} - {args[0] if args else ''}")

if __name__ == "__main__":
    server = HTTPServer(("0.0.0.0", PORT), DemoAppHandler)
    print(f"PersonalServer Demo App running on port {PORT}")
    server.serve_forever()
