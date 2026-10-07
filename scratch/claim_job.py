import urllib.request
import json
from pathlib import Path

# Load node registration
reg_path = Path('runtime/registration.json')
if not reg_path.exists():
    reg_path = Path(__file__).resolve().parent.parent / 'runtime' / 'registration.json'

with open(reg_path, 'r', encoding='utf-8') as f:
    reg = json.load(f)

req = urllib.request.Request(
    f"{reg['controller_url']}/nodes/{reg['node_id']}/jobs/next",
    headers={"Authorization": f"Bearer {reg['auth_token']}"}
)
with urllib.request.urlopen(req) as resp:
    print(resp.read().decode())
