import json
import os

files = ['nodes.json', 'jobs.json', 'apps.json']
for fname in files:
    path = os.path.join('controller', 'data', fname)
    size = os.path.getsize(path)
    with open(path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    if fname == 'nodes.json':
        nodes = data.get('nodes', {})
        print(f"nodes.json: size={size} bytes, total_nodes={len(nodes)}")
        for nid, n in nodes.items():
            print(f"  - {nid}: {n.get('name')} | status={n.get('status')} | role={n.get('role')}")
    elif fname == 'jobs.json':
        jobs = data.get('jobs', {})
        print(f"jobs.json: size={size} bytes, total_jobs={len(jobs)}")
    elif fname == 'apps.json':
        apps = data.get('apps', {})
        print(f"apps.json: size={size} bytes, total_apps={len(apps)}")
        for aid, a in apps.items():
            print(f"  - {aid}: {a.get('name')} | status={a.get('status')} | port={a.get('port')}")
