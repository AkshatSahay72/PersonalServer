import json
import os
from pathlib import Path

base_dir = Path.home() / "PersonalServer"

# 1. Update registration.json
reg_file = base_dir / "runtime" / "registration.json"
if reg_file.exists():
    with open(reg_file, "r") as f:
        reg = json.load(f)
    reg["controller_url"] = "http://100.85.108.5:8000"
    with open(reg_file, "w") as f:
        json.dump(reg, f, indent=2)
    print(f"Updated {reg_file} with controller_url: http://100.85.108.5:8000")

# 2. Update config/node.json if needed
node_file = base_dir / "config" / "node.json"
if node_file.exists():
    with open(node_file, "r") as f:
        node = json.load(f)
    node["CONTROLLER_URL"] = "http://100.85.108.5:8000"
    with open(node_file, "w") as f:
        json.dump(node, f, indent=2)
    print(f"Updated {node_file} with CONTROLLER_URL")

# 3. Create config/controller.json
ctrl_file = base_dir / "config" / "controller.json"
with open(ctrl_file, "w") as f:
    json.dump({"url": "http://100.85.108.5:8000"}, f, indent=2)
print(f"Created {ctrl_file}")
