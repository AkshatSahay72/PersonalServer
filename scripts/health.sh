#!/data/data/com.termux/files/usr/bin/bash

# ==============================================================================
# PersonalServer - Node & Service Health Report
# ==============================================================================

BASE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
CONFIG_DIR="$BASE_DIR/config"
RUNTIME_DIR="$BASE_DIR/runtime"

NODE_JSON="$CONFIG_DIR/node.json"
NODE_CONF="$CONFIG_DIR/node.conf"

echo "=========================================="
echo " PersonalServer Health Report"
echo "=========================================="

echo "=== NODE IDENTITY ==="
if [ -f "$NODE_JSON" ]; then
    python3 -c "
import json
try:
    with open('$NODE_JSON') as f:
        d = json.load(f)
        print(f\"Node: {d.get('name', 'unknown')}\")
        print(f\"ID:   {d.get('node_id', 'unknown')}\")
        print(f\"Role: {d.get('role', 'unknown')}\")
        print(f\"OS:   {d.get('os', 'unknown')} ({d.get('architecture', 'unknown')})\")
except Exception:
    pass
" 2>/dev/null
elif [ -f "$NODE_CONF" ]; then
    echo "Node: $(grep '^NODE_NAME=' "$NODE_CONF" | cut -d= -f2)"
    echo "ID:   $(grep '^NODE_ID=' "$NODE_CONF" | cut -d= -f2)"
    echo "Role: $(grep '^NODE_ROLE=' "$NODE_CONF" | cut -d= -f2)"
else
    echo "Node identity not configured."
fi
echo

echo "=== SYSTEM METRICS ==="
echo "Uptime:    $(uptime | sed 's/^[ \t]*//')"
echo "CPU Cores: $(nproc 2>/dev/null || echo 'N/A')"
echo

echo "=== STORAGE ==="
df -h "$HOME" | tail -1 | awk '{printf "Total: %s | Used: %s | Free: %s | Usage: %s\n", $2, $3, $4, $5}'
echo

echo "=== MEMORY ==="
free -h | grep "Mem:" | awk '{printf "Total: %s | Used: %s | Free: %s | Available: %s\n", $2, $3, $4, $7}'
echo

echo "=== SERVICE STATUS ==="

# Check Node API
node_api_pid=$(pgrep -f "services/node-api/app.py" | head -n 1)
if [ -n "$node_api_pid" ]; then
    health_json=$(curl -s -m 2 http://127.0.0.1:8080/health 2>/dev/null)
    if echo "$health_json" | grep -q '"status": "online"'; then
        echo "Node API:   ONLINE (PID: $node_api_pid, Port: 8080, Health: OK)"
    else
        echo "Node API:   DEGRADED (PID: $node_api_pid, Port: 8080, Health: Unresponsive)"
    fi
else
    echo "Node API:   OFFLINE"
fi

# Check Cloudflare
cf_pid=$(pgrep -f "cloudflared tunnel run" | head -n 1)
if [ -n "$cf_pid" ]; then
    echo "Cloudflare: ONLINE (PID: $cf_pid, Tunnel: Active)"
else
    echo "Cloudflare: OFFLINE"
fi

echo "=========================================="
