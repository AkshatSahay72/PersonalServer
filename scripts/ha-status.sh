#!/usr/bin/env bash
# ==============================================================================
# PersonalServer High Availability (HA) Status Inspector
# ==============================================================================
# Queries the active cluster controller or local standby to display:
# - HA Role (Leader, Standby, Candidate)
# - Current Leader Node ID & URL
# - Election Term & State Version
# - Leader Lease Validity & Remaining Seconds
# - Connected Standby Nodes
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

CONTROLLER_URL="${PERSONALSERVER_CONTROLLER_URL:-http://127.0.0.1:8000}"

echo "=================================================="
echo " PersonalServer High Availability (HA) Status"
echo "=================================================="
echo "Target: ${CONTROLLER_URL}/ha/status"
echo ""

if command -v curl >/dev/null 2>&1; then
    RESPONSE=$(curl -s --connect-timeout 3 "${CONTROLLER_URL}/ha/status" || echo "")
elif command -v python3 >/dev/null 2>&1; then
    RESPONSE=$(python3 -c "
import urllib.request, json, sys
try:
    with urllib.request.urlopen('${CONTROLLER_URL}/ha/status', timeout=3) as r:
        print(r.read().decode())
except Exception as e:
    sys.exit(1)
" 2>/dev/null || echo "")
else
    echo "Error: Neither curl nor python3 is available." >&2
    exit 1
fi

if [ -z "${RESPONSE}" ]; then
    echo "Status: ❌ UNREACHABLE (Controller at ${CONTROLLER_URL} is down or partitioned)"
    echo "Tip: Run failover discovery with: python3 -m config.ha_manager"
    exit 1
fi

if command -v python3 >/dev/null 2>&1; then
    python3 -c "
import json, sys
data = json.loads('''${RESPONSE}''')
role = data.get('role', 'unknown').upper()
leader_id = data.get('leader_id', 'unknown')
leader_url = data.get('leader_url', 'unknown')
term = data.get('term', 0)
version = data.get('state_version', 0)
valid = data.get('lease_valid', False)
rem = data.get('lease_remaining_sec', 0)
standbys = data.get('standbys', [])

icon = '👑' if role == 'LEADER' else '🛡️'
print(f'Cluster HA Role:     {icon} {role}')
print(f'Active Leader ID:    {leader_id}')
print(f'Leader Endpoint:     {leader_url}')
print(f'Election Term:       {term}')
print(f'State DB Version:    {version}')
print(f'Leader Lease:        {'VALID' if valid else 'EXPIRED'} ({rem}s remaining)')
print(f'Standby Nodes:       {len(standbys)} synced')
for s in standbys:
    print(f'  * {s.get(\"node_id\")} ({s.get(\"url\")}) - Version {s.get(\"state_version\")}')
"
else
    echo "${RESPONSE}"
fi
echo "=================================================="
