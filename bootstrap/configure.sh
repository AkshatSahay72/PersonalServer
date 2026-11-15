#!/usr/bin/env bash
# =============================================================================
# PersonalServer — Node Configuration & Identity Generator (configure.sh)
# =============================================================================
# Configures directory structures, generates or preserves node identity,
# and safely stores credentials without overwriting existing configurations.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_DIR="${PERSONALSERVER_BASE_DIR:-$(cd "$SCRIPT_DIR/.." && pwd)}"

if [ -z "${PLATFORM:-}" ]; then
    # shellcheck disable=SC1091
    source "$SCRIPT_DIR/detect.sh"
fi

# Parse optional arguments
NODE_NAME=""
NODE_ROLE=""
CONTROLLER_URL=""
ENROLLMENT_TOKEN=""

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
        *)
            shift
            ;;
    esac
done

# 1. Create standard directory structure
echo "[+] Initializing directory structure at: $BASE_DIR"
mkdir -p "$BASE_DIR"/{config/secrets,data,logs,runtime/jobs,storage/applications,scripts}
chmod 700 "$BASE_DIR/config/secrets" || true

# 2. Node Identity & Configuration
CONFIG_FILE="$BASE_DIR/config/node.json"
NODE_CONF="$BASE_DIR/config/node.conf"

if [ -f "$CONFIG_FILE" ]; then
    echo "[*] Existing node configuration found at $CONFIG_FILE. Preserving node identity."
    # If custom name or role was provided, safely update fields via Python
    if [ -n "$NODE_NAME" ] || [ -n "$NODE_ROLE" ]; then
        "$PYTHON_CMD" - <<EOF
import json
try:
    with open("$CONFIG_FILE", "r") as f:
        data = json.load(f)
    if "$NODE_NAME":
        data["name"] = "$NODE_NAME"
    if "$NODE_ROLE":
        data["role"] = "$NODE_ROLE"
    with open("$CONFIG_FILE", "w") as f:
        json.dump(data, f, indent=2)
except Exception as e:
    print(f"Warning updating config: {e}")
EOF
    fi
else
    echo "[+] Generating fresh node identity..."
    
    # Generate unique ID
    RANDOM_HEX="$(od -An -N6 -tx1 /dev/urandom 2>/dev/null | tr -d ' \n' || date +%s)"
    NODE_ID="server-${PLATFORM}-${RANDOM_HEX}"
    
    # Determine default name
    if [ -z "$NODE_NAME" ]; then
        if [ "$PLATFORM" = "termux" ]; then
            NODE_NAME="$(getprop net.hostname 2>/dev/null || hostname 2>/dev/null || echo "android-node")"
        else
            NODE_NAME="$(hostname 2>/dev/null || echo "${PLATFORM}-node")"
        fi
    fi

    # Determine default role
    ROLE="${NODE_ROLE:-worker}"

    # Build node.json
    cat > "$CONFIG_FILE" <<EOF
{
  "node_id": "$NODE_ID",
  "name": "$NODE_NAME",
  "role": "$ROLE",
  "platform": "$PLATFORM",
  "os": "$OS",
  "architecture": "$ARCH",
  "cpu_cores": $CPU_CORES,
  "ram_mb": $RAM_MB,
  "storage_gb": $STORAGE_GB,
  "capabilities": {
    "compute": true,
    "storage": true,
    "network": true,
    "python_runtime": true,
    "container_runtime:docker": false,
    "container_runtime:proot": false
  }
}
EOF
    chmod 600 "$CONFIG_FILE"
    echo "[OK] Generated node configuration: $CONFIG_FILE (ID: $NODE_ID)"
fi

# 3. Controller & Secret Configuration
if [ -n "$CONTROLLER_URL" ]; then
    echo "CONTROLLER_URL=$CONTROLLER_URL" > "$NODE_CONF"
    chmod 600 "$NODE_CONF"
    echo "[OK] Saved controller URL configuration: $NODE_CONF"
fi

if [ -n "$ENROLLMENT_TOKEN" ]; then
    TOKEN_FILE="$BASE_DIR/config/secrets/enrollment.token"
    echo -n "$ENROLLMENT_TOKEN" > "$TOKEN_FILE"
    chmod 600 "$TOKEN_FILE"
    echo "[OK] Saved enrollment token securely (mode 0600)"
fi

export BASE_DIR CONFIG_FILE
