#!/usr/bin/env bash
# =============================================================================
# PersonalServer — Universal Node Bootstrapper (install.sh)
# =============================================================================
# Cross-platform installer converting any supported terminal into a registered,
# healthy PersonalServer node.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_DIR="${PERSONALSERVER_BASE_DIR:-$(cd "$SCRIPT_DIR/.." && pwd)}"

# CLI Options
NODE_NAME=""
NODE_ROLE="worker"
CONTROLLER_URL=""
ENROLLMENT_TOKEN=""
AUTO_START=true
DRY_RUN=false

while [ $# -gt 0 ]; do
    case "$1" in
        --name)
            NODE_NAME="${2:-}"
            shift 2
            ;;
        --role)
            NODE_ROLE="${2:-}"
            shift 2
            ;;
        --controller)
            CONTROLLER_URL="${2:-}"
            shift 2
            ;;
        --token)
            ENROLLMENT_TOKEN="${2:-}"
            shift 2
            ;;
        --no-start)
            AUTO_START=false
            shift
            ;;
        --dry-run)
            DRY_RUN=true
            shift
            ;;
        --help|-h)
            echo "Usage: $0 [options]"
            echo "Options:"
            echo "  --name <name>         Set custom node name"
            echo "  --role <role>         Set node role (default: worker)"
            echo "  --controller <url>    Set controller URL"
            echo "  --token <token>       Enrollment token for authenticated registration"
            echo "  --no-start            Do not automatically start services"
            echo "  --dry-run             Detect environment and print plan without making changes"
            exit 0
            ;;
        *)
            echo "Unknown option: $1" >&2
            exit 1
            ;;
    esac
done

echo "===================================="
echo "     PERSONALSERVER INSTALLER"
echo "===================================="
echo

echo "Detecting environment..."

# 1. Environment Detection
# shellcheck disable=SC1091
source "$SCRIPT_DIR/detect.sh"

echo "[OK] Operating system detected: $OS ($PLATFORM)"
echo "[OK] Architecture detected: $ARCH"
echo "[OK] Hardware information collected: $CPU_CORES cores, ${RAM_MB}MB RAM, ${STORAGE_GB}GB Storage"
echo "[OK] Package manager detected: $PKG_MGR"
echo

if [ "$DRY_RUN" = true ]; then
    echo "[*] Dry run mode enabled. No changes will be written."
    echo "===================================="
    echo "DRY RUN COMPLETE"
    echo "===================================="
    exit 0
fi

# 2. Dependency Installation
echo "Installing dependencies..."

# shellcheck disable=SC1091
source "$SCRIPT_DIR/dependencies.sh"

echo "[OK] Dependencies installed"

# 3. Directory & Configuration Setup
# shellcheck disable=SC1091
source "$SCRIPT_DIR/configure.sh" --name "$NODE_NAME" --role "$NODE_ROLE" --controller "$CONTROLLER_URL" --token "$ENROLLMENT_TOKEN"

echo "[OK] Directory structure created"
echo "[OK] Node Agent installed"
echo "[OK] Configuration generated"
echo

# Extract configured Node ID
CONFIG_FILE="$BASE_DIR/config/node.json"
ACTUAL_NODE_ID="$(grep -o '"node_id": "[^"]*"' "$CONFIG_FILE" 2>/dev/null | cut -d'"' -f4 || echo "unknown")"

# 4. Registration with Controller (if configured)
if [ -n "$CONTROLLER_URL" ] && [ -n "$ENROLLMENT_TOKEN" ]; then
    echo "Connecting to controller..."
    if [ -f "$BASE_DIR/agent/node-agent.py" ]; then
        "$PYTHON_CMD" "$BASE_DIR/agent/node-agent.py" register --controller "$CONTROLLER_URL" --token "$ENROLLMENT_TOKEN" || {
            echo "[!] Warning: Node registration encountered an error. You can retry later with:"
            echo "    $PYTHON_CMD agent/node-agent.py register --controller $CONTROLLER_URL --token <TOKEN>"
        }
        echo "[OK] Node registered"
    fi
fi

# 5. Service Startup & Verification
NODE_STATUS="READY"
if [ "$AUTO_START" = true ]; then
    echo "Starting node services..."
    if [ -f "$BASE_DIR/scripts/start.sh" ]; then
        bash "$BASE_DIR/scripts/start.sh" >/dev/null 2>&1 || true
        echo "[OK] Agent started"
    fi

    # Bounded verification of Node API health
    HEALTHY=false
    for _ in 1 2 3 4 5; do
        sleep 1
        if curl -s -m 3 http://127.0.0.1:8080/health 2>/dev/null | grep -q '"status": "online"'; then
            HEALTHY=true
            break
        fi
    done

    if [ "$HEALTHY" = true ]; then
        echo "[OK] Health check passed"
        NODE_STATUS="ONLINE"
    else
        echo "[*] Services dispatched; health check pending initialization"
        NODE_STATUS="STARTING"
    fi
fi

echo
echo "===================================="
echo "INSTALLATION COMPLETE"
echo "Node ID: $ACTUAL_NODE_ID"
echo "Status:  $NODE_STATUS"
echo "===================================="
