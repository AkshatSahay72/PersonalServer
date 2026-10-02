import urllib.request
import json

reg = json.load(open('runtime/registration.json'))
req = urllib.request.Request(
    f"{reg['controller_url']}/nodes/{reg['node_id']}/jobs/next",
    headers={"Authorization": f"Bearer {reg['auth_token']}"}
)
with urllib.request.urlopen(req) as resp:
    print(resp.read().decode())
