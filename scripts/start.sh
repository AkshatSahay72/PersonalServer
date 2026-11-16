#!/usr/bin/env bash

# ==============================================================================
# PersonalServer - Service Startup Script
# ==============================================================================

BASE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
RUNTIME_DIR="$BASE_DIR/runtime"
LOGS_DIR="$BASE_DIR/logs"
CONFIG_DIR="$BASE_DIR/config"
SECRETS_DIR="$CONFIG_DIR/secrets"

mkdir -p "$RUNTIME_DIR" "$LOGS_DIR"

NODE_API_PID_FILE="$RUNTIME_DIR/node-api.pid"
NODE_WORKER_PID_FILE="$RUNTIME_DIR/node-worker.pid"
CLOUDFLARED_PID_FILE="$RUNTIME_DIR/cloudflared.pid"
NODE_API_LOG="$LOGS_DIR/node-api.log"
NODE_WORKER_LOG="$LOGS_DIR/node-worker.log"
CLOUDFLARED_LOG="$LOGS_DIR/cloudflared.log"
TOKEN_FILE="$SECRETS_DIR/cloudflared.token"
NODE_API_SCRIPT="$BASE_DIR/services/node-api/app.py"
NODE_AGENT_SCRIPT="$BASE_DIR/agent/node-agent.py"

echo "=========================================="
echo " PersonalServer - Starting Services"
echo "=========================================="

# ------------------------------------------------------------------------------
# 0. Release Update Preflight (Optional & Configurable)
# ------------------------------------------------------------------------------
RELEASE_CONF="$CONFIG_DIR/release.conf"
if [ -f "$RELEASE_CONF" ] && grep -qiE "^AUTO_UPDATE_ON_START=(true|1|yes)" "$RELEASE_CONF" 2>/dev/null; then
    echo -n "Checking release updates... "
    if [ -f "$BASE_DIR/scripts/update.sh" ]; then
        if bash "$BASE_DIR/scripts/update.sh" --preflight >/dev/null 2>&1; then
            echo "preflight OK"
        else
            echo "skipped / offline (continuing startup)"
        fi
    else
        echo "update script not found"
    fi
fi

# Helper: check if a PID is alive and matches expected command pattern
is_pid_running() {
    local pid="$1"
    local pattern="$2"
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
        if [ -n "$pattern" ]; then
            if ps -p "$pid" -o args= 2>/dev/null | grep -q "$pattern"; then
                return 0
            fi
            return 1
        fi
        return 0
    fi
    return 1
}

# ------------------------------------------------------------------------------
# 1. Start Node API
# ------------------------------------------------------------------------------
echo -n "Starting Node API... "

node_api_running=false
if [ -f "$NODE_API_PID_FILE" ]; then
    stored_pid=$(cat "$NODE_API_PID_FILE" 2>/dev/null)
    if is_pid_running "$stored_pid" "services/node-api/app.py"; then
        echo "already running (PID: $stored_pid)"
        node_api_running=true
    else
        # Stale PID file
        rm -f "$NODE_API_PID_FILE"
    fi
fi

if [ "$node_api_running" = false ]; then
    # Check if a process is already running without PID file
    existing_pid=$(pgrep -f "services/node-api/app.py" | head -n 1)
    if [ -n "$existing_pid" ]; then
        echo "$existing_pid" > "$NODE_API_PID_FILE"
        echo "already running (PID: $existing_pid, PID file recovered)"
        node_api_running=true
    else
        if [ ! -f "$NODE_API_SCRIPT" ]; then
            echo "FAILED (Script missing: $NODE_API_SCRIPT)"
        else
            nohup python "$NODE_API_SCRIPT" >> "$NODE_API_LOG" 2>&1 &
            new_pid=$!
            echo "$new_pid" > "$NODE_API_PID_FILE"

            # Verify startup
            sleep 1
            if is_pid_running "$new_pid" "services/node-api/app.py"; then
                echo "started (PID: $new_pid)"
                node_api_running=true
            else
                echo "FAILED to start. Check $NODE_API_LOG"
                rm -f "$NODE_API_PID_FILE"
            fi
        fi
    fi
fi

# ------------------------------------------------------------------------------
# 2. Start Cloudflare Tunnel
# ------------------------------------------------------------------------------
echo -n "Starting Cloudflare Tunnel... "

cloudflared_running=false
if [ -f "$CLOUDFLARED_PID_FILE" ]; then
    stored_cf_pid=$(cat "$CLOUDFLARED_PID_FILE" 2>/dev/null)
    if is_pid_running "$stored_cf_pid" "cloudflared"; then
        echo "already running (PID: $stored_cf_pid)"
        cloudflared_running=true
    else
        rm -f "$CLOUDFLARED_PID_FILE"
    fi
fi

if [ "$cloudflared_running" = false ]; then
    existing_cf_pid=$(pgrep -f "cloudflared tunnel" | head -n 1)
    if [ -n "$existing_cf_pid" ]; then
        echo "$existing_cf_pid" > "$CLOUDFLARED_PID_FILE"
        echo "already running (PID: $existing_cf_pid, PID file recovered)"
        cloudflared_running=true
    else
        if ! command -v cloudflared >/dev/null 2>&1; then
            echo "skipped (not installed on worker node)"
            cloudflared_running="not_installed"
        elif [ ! -f "$TOKEN_FILE" ]; then
            echo "skipped (no tunnel token configured for worker node)"
            cloudflared_running="not_configured"
        else
            export GODEBUG="netdns=go"
            if [ -f "/data/data/com.termux/files/usr/etc/tls/cert.pem" ]; then
                export SSL_CERT_FILE="/data/data/com.termux/files/usr/etc/tls/cert.pem"
                export SSL_CERT_DIR="/data/data/com.termux/files/usr/etc/tls"
            elif [ -f "/etc/ssl/certs/ca-certificates.crt" ]; then
                export SSL_CERT_FILE="/etc/ssl/certs/ca-certificates.crt"
            fi
            nohup cloudflared tunnel --no-prechecks run --token-file "$TOKEN_FILE" >> "$CLOUDFLARED_LOG" 2>&1 &
            new_cf_pid=$!
            echo "$new_cf_pid" > "$CLOUDFLARED_PID_FILE"

            sleep 2
            if is_pid_running "$new_cf_pid" "cloudflared"; then
                echo "started (PID: $new_cf_pid)"
                cloudflared_running=true
            else
                echo "FAILED to start. Check $CLOUDFLARED_LOG"
                rm -f "$CLOUDFLARED_PID_FILE"
            fi
        fi
    fi
fi

# ------------------------------------------------------------------------------
# 3. Start Node Worker Daemon
# ------------------------------------------------------------------------------
echo -n "Starting Node Worker... "

node_worker_running=false
if [ -f "$NODE_WORKER_PID_FILE" ]; then
    stored_worker_pid=$(cat "$NODE_WORKER_PID_FILE" 2>/dev/null)
    if is_pid_running "$stored_worker_pid" "agent/node-agent.py work"; then
        echo "already running (PID: $stored_worker_pid)"
        node_worker_running=true
    else
        rm -f "$NODE_WORKER_PID_FILE"
    fi
fi

if [ "$node_worker_running" = false ]; then
    existing_worker_pid=$(pgrep -f "agent/node-agent.py work" | head -n 1)
    if [ -n "$existing_worker_pid" ]; then
        echo "$existing_worker_pid" > "$NODE_WORKER_PID_FILE"
        echo "already running (PID: $existing_worker_pid, PID file recovered)"
        node_worker_running=true
    else
        if [ ! -f "$NODE_AGENT_SCRIPT" ]; then
            echo "FAILED (Script missing: $NODE_AGENT_SCRIPT)"
        else
            nohup python "$NODE_AGENT_SCRIPT" work --daemon >> "$NODE_WORKER_LOG" 2>&1 &
            new_worker_pid=$!
            echo "$new_worker_pid" > "$NODE_WORKER_PID_FILE"

            sleep 1
            if is_pid_running "$new_worker_pid" "agent/node-agent.py work"; then
                echo "started (PID: $new_worker_pid)"
                node_worker_running=true
            else
                echo "FAILED to start. Check $NODE_WORKER_LOG"
                rm -f "$NODE_WORKER_PID_FILE"
            fi
        fi
    fi
fi

echo "=========================================="
if [ "$node_api_running" = true ]; then
    echo "PersonalServer services started successfully."
    exit 0
else
    echo "PersonalServer failed to start Node API."
    exit 1
fi
