import json
import os

nj = os.path.expanduser('~/PersonalServer/config/node.json')
with open(nj, 'r') as f:
    d = json.load(f)
d['CONTROLLER_URL'] = 'http://127.0.0.1:8000'
d['ROUTER_URL'] = 'http://127.0.0.1:8088'
with open(nj, 'w') as f:
    json.dump(d, f, indent=2)

reg = os.path.expanduser('~/PersonalServer/runtime/registration.json')
with open(reg, 'r') as f:
    r = json.load(f)
r['controller_url'] = 'http://127.0.0.1:8000'
with open(reg, 'w') as f:
    json.dump(r, f, indent=2)

print('Updated Node 01 node.json and registration.json')
