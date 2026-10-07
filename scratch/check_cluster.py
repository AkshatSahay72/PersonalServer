import urllib.request
import json

print("--- DIRECT CONTROLLER (http://100.85.108.5:8000/cluster) ---")
req = urllib.request.Request("http://100.85.108.5:8000/cluster")
with urllib.request.urlopen(req, timeout=10) as resp:
    data = json.loads(resp.read().decode())
    print("Cluster Summary:", json.dumps(data["cluster"], indent=2))
    for n in data["nodes"]:
        print(f"Node: {n['name']} ({n['node_id']}) -> Status: {n['status']}, Last Seen: {n['last_seen']}")

print("\n--- PUBLIC CONSOLE (https://server.akshatsahay.space/api/cluster) ---")
req2 = urllib.request.Request("https://server.akshatsahay.space/api/cluster", headers={"User-Agent": "Mozilla/5.0"})
try:
    with urllib.request.urlopen(req2, timeout=10) as resp:
        data2 = json.loads(resp.read().decode())
        print("Cluster Summary:", json.dumps(data2.get("cluster"), indent=2))
        for n in data2.get("nodes", []):
            print(f"Node: {n['name']} ({n['node_id']}) -> Status: {n['status']}")
except Exception as e:
    print("Public API query error:", e)
