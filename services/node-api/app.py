from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import subprocess
import re
import os

HOST = "0.0.0.0"
PORT = 8080

CONFIG_FILE = os.path.expanduser(
    "~/PersonalServer/config/node.conf"
)


def load_config():
    config = {}

    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE) as file:
            for line in file:
                line = line.strip()

                if not line or line.startswith("#"):
                    continue

                if "=" in line:
                    key, value = line.split("=", 1)
                    config[key] = value

    return config


def command(cmd):
    try:
        return subprocess.check_output(
            cmd,
            shell=True,
            text=True
        ).strip()
    except Exception:
        return ""


def get_system_info():

    # CPU
    try:
        cpu_cores = int(command("nproc"))
    except Exception:
        cpu_cores = 0

    # Load average
    uptime_output = command("uptime")

    load_match = re.search(
        r"load average:\s*([\d.]+),\s*([\d.]+),\s*([\d.]+)",
        uptime_output
    )

    if load_match:
        load_average = [
            float(load_match.group(1)),
            float(load_match.group(2)),
            float(load_match.group(3))
        ]
    else:
        load_average = []

    # Memory
    memory_output = command("free -h")
    memory = {}

    for line in memory_output.splitlines():

        if line.startswith("Mem:"):
            parts = line.split()
            if len(parts) >= 7:
                memory = {
                    "total": parts[1],
                    "used": parts[2],
                    "available": parts[6]
                }
            elif len(parts) >= 4:
                memory = {
                    "total": parts[1],
                    "used": parts[2],
                    "available": parts[3]
                }

    # Storage
    storage_output = command("df -h ~")
    storage = {}

    lines = storage_output.splitlines()

    if len(lines) >= 2:
        parts = lines[-1].split()
        if len(parts) >= 5:
            storage = {
                "total": parts[1],
                "used": parts[2],
                "available": parts[3],
                "used_percent": parts[4]
            }

    return {
        "cpu_cores": cpu_cores,
        "load_average": load_average,
        "memory": memory,
        "storage": storage
    }


def get_health():

    config = load_config()

    return {
        "status": "online",

        "node": {
            "id": config.get("NODE_ID", "unknown"),
            "name": config.get("NODE_NAME", "unknown"),
            "role": config.get("NODE_ROLE", "unknown")
        },

        "system": get_system_info()
    }


class HealthHandler(BaseHTTPRequestHandler):

    def do_GET(self):

        if self.path == "/health" or self.path == "/status":
            response = get_health()

        else:
            self.send_response(404)
            self.end_headers()
            return

        body = json.dumps(
            response,
            indent=2
        ).encode()

        self.send_response(200)
        self.send_header(
            "Content-Type",
            "application/json"
        )
        self.send_header(
            "Content-Length",
            str(len(body))
        )
        self.end_headers()

        self.wfile.write(body)

    def log_message(self, format, *args):
        print(f"[HTTP] {args[0]}")


if __name__ == "__main__":
    server = HTTPServer((HOST, PORT), HealthHandler)
    print(f"Node API running on {HOST}:{PORT}")
    server.serve_forever()
