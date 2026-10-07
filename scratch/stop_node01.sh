#!/data/data/com.termux/files/usr/bin/bash

# ==============================================================================
# PersonalServer - Service Shutdown Script (Node 01 Control Plane)
# ==============================================================================

BASE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
RUNTIME_DIR="$BASE_DIR/runtime"

CONTROLLER_PID_FILE="$RUNTIME_DIR/controller.pid"
ROUTER_PID_FILE="$RUNTIME_DIR/router.pid"
NODE_API_PID_FILE="$RUNTIME_DIR/node-api.pid"
CLOUDFLARED_PID_FILE="$RUNTIME_DIR/cloudflared.pid"

echo "=========================================="
echo " PersonalServer - Stopping Services (Node 01)"
echo "=========================================="

stop_process() {
    local service_name="$1"
    local pid_file="$2"
    local pattern="$3"

    echo -n "Stopping $service_name... "

    local pid=""
    if [ -f "$pid_file" ]; then
        pid=$(cat "$pid_file" 2>/dev/null)
    fi

    # If no PID from file, check if running by pattern
    if [ -z "$pid" ] || ! kill -0 "$pid" 2>/dev/null; then
        if [ -n "$pid" ]; then
            rm -f "$pid_file"
        fi
        pid=$(pgrep -f "$pattern" | head -n 1)
    fi

    if [ -z "$pid" ]; then
        echo "not running"
        rm -f "$pid_file"
        return 0
    fi

    # Verify PID matches pattern to protect unrelated processes
    if ! ps -p "$pid" -o args= 2>/dev/null | grep -q "$pattern"; then
        echo "PID $pid does not match $service_name (skipping unsafe kill)"
        rm -f "$pid_file"
        return 1
    fi

    # Attempt graceful shutdown (SIGTERM)
    kill "$pid" 2>/dev/null

    local count=0
    while kill -0 "$pid" 2>/dev/null && [ $count -lt 10 ]; do
        sleep 0.5
        count=$((count + 1))
    done

    # Force kill if still running after 5 seconds
    if kill -0 "$pid" 2>/dev/null; then
        kill -9 "$pid" 2>/dev/null
        sleep 0.5
    fi

    if ! kill -0 "$pid" 2>/dev/null; then
        echo "stopped (PID: $pid)"
        rm -f "$pid_file"
        return 0
    else
        echo "FAILED to stop (PID: $pid)"
        return 1
    fi
}

stop_process "Cloudflare Tunnel" "$CLOUDFLARED_PID_FILE" "cloudflared"
cf_stopped=$?

stop_process "Node API" "$NODE_API_PID_FILE" "services/node-api/app.py"
api_stopped=$?

stop_process "Application Router" "$ROUTER_PID_FILE" "router/app_router.py"
router_stopped=$?

stop_process "Controller" "$CONTROLLER_PID_FILE" "controller/controller.py"
ctrl_stopped=$?

echo "=========================================="
if [ $ctrl_stopped -eq 0 ] && [ $router_stopped -eq 0 ] && [ $api_stopped -eq 0 ] && [ $cf_stopped -eq 0 ]; then
    echo "PersonalServer services stopped successfully."
    exit 0
else
    echo "PersonalServer stopped with warnings."
    exit 1
fi
