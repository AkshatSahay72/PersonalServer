#!/data/data/com.termux/files/usr/bin/bash

# ==============================================================================
# PersonalServer - Service Status Script
# ==============================================================================

BASE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
RUNTIME_DIR="$BASE_DIR/runtime"
LOGS_DIR="$BASE_DIR/logs"
CONFIG_DIR="$BASE_DIR/config"

NODE_JSON="$CONFIG_DIR/node.json"
NODE_CONF="$CONFIG_DIR/node.conf"
NODE_API_PID_FILE="$RUNTIME_DIR/node-api.pid"
CLOUDFLARED_PID_FILE="$RUNTIME_DIR/cloudflared.pid"
CLOUDFLARED_LOG="$LOGS_DIR/cloudflared.log"

echo "=========================================="
echo " PersonalServer Status"
echo "=========================================="

# Node Information
echo "Node:"
if [ -f "$NODE_JSON" ]; then
    python3 -c "
import json
try:
    with open('$NODE_JSON') as f:
        d = json.load(f)
        print(f\"  ID: {d.get('node_id', 'unknown')}\")
        print(f\"  Name: {d.get('name', 'unknown')}\")
        print(f\"  Platform: {d.get('platform', 'unknown')}\")
        print(f\"  Architecture: {d.get('architecture', 'unknown')}\")
except Exception:
    pass
" 2>/dev/null
elif [ -f "$NODE_CONF" ]; then
    echo "  ID: $(grep '^NODE_ID=' "$NODE_CONF" | cut -d= -f2)"
    echo "  Name: $(grep '^NODE_NAME=' "$NODE_CONF" | cut -d= -f2)"
    echo "  Platform: termux"
    echo "  Architecture: $(uname -m)"
else
    echo "  ID: unknown"
    echo "  Name: $(hostname 2>/dev/null || echo unknown)"
    echo "  Platform: $(uname -s)"
    echo "  Architecture: $(uname -m)"
fi

echo
echo "Services:"
echo

all_healthy=true

# ------------------------------------------------------------------------------
# 1. Node API Status
# ------------------------------------------------------------------------------
echo "Node API"
node_api_pid=""
if [ -f "$NODE_API_PID_FILE" ]; then
    node_api_pid=$(cat "$NODE_API_PID_FILE" 2>/dev/null)
fi

if [ -n "$node_api_pid" ] && kill -0 "$node_api_pid" 2>/dev/null && ps -p "$node_api_pid" -o args= 2>/dev/null | grep -q "services/node-api/app.py"; then
    echo "  Process: running"
    echo "  PID: $node_api_pid"
else
    # Check if running without recorded PID
    detected_pid=$(pgrep -f "services/node-api/app.py" | head -n 1)
    if [ -n "$detected_pid" ]; then
        echo "  Process: running (untracked PID)"
        echo "  PID: $detected_pid"
        node_api_pid="$detected_pid"
    else
        echo "  Process: stopped"
        echo "  PID: -"
        all_healthy=false
    fi
fi

echo "  Port: 8080"

# Check Health endpoint
if [ -n "$node_api_pid" ]; then
    health_resp=$(curl -s -m 2 http://127.0.0.1:8080/health 2>/dev/null)
    if echo "$health_resp" | grep -q '"status": "online"'; then
        echo "  Health: healthy (online)"
    else
        echo "  Health: unhealthy"
        all_healthy=false
    fi
else
    echo "  Health: not running"
fi

echo

# ------------------------------------------------------------------------------
# 2. Cloudflare Tunnel Status
# ------------------------------------------------------------------------------
echo "Cloudflare"
cf_pid=""
if [ -f "$CLOUDFLARED_PID_FILE" ]; then
    cf_pid=$(cat "$CLOUDFLARED_PID_FILE" 2>/dev/null)
fi

if [ -n "$cf_pid" ] && kill -0 "$cf_pid" 2>/dev/null && ps -p "$cf_pid" -o args= 2>/dev/null | grep -q "cloudflared"; then
    echo "  Process: running"
    echo "  PID: $cf_pid"
else
    detected_cf_pid=$(pgrep -f "cloudflared tunnel run" | head -n 1)
    if [ -n "$detected_cf_pid" ]; then
        echo "  Process: running (untracked PID)"
        echo "  PID: $detected_cf_pid"
        cf_pid="$detected_cf_pid"
    else
        echo "  Process: stopped"
        echo "  PID: -"
        all_healthy=false
    fi
fi

# Determine connection state
if [ -n "$cf_pid" ]; then
    if [ -f "$CLOUDFLARED_LOG" ]; then
        if tail -n 30 "$CLOUDFLARED_LOG" 2>/dev/null | grep -qE "Registered tunnel connection|QUIC connection successful|Connection .* registered|PASS.*QUIC|PASS.*HTTP/2"; then
            echo "  Connection: connected"
        elif tail -n 30 "$CLOUDFLARED_LOG" 2>/dev/null | grep -qiE "error|fail|retrying"; then
            echo "  Connection: degraded/retrying"
            all_healthy=false
        else
            echo "  Connection: active"
        fi
    else
        echo "  Connection: running (no log file)"
    fi
else
    echo "  Connection: disconnected"
fi

echo "=========================================="
if [ "$all_healthy" = true ]; then
    exit 0
else
    exit 1
fi
