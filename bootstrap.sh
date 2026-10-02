#!/usr/bin/env bash

set -e

echo "================================="
echo "       PERSONAL SERVER"
echo "================================="
echo

SERVER_DIR="$HOME/PersonalServer"

echo "[1/4] Detecting system..."

source "$(dirname "$0")/install/detect.sh"

echo "Platform: $PLATFORM"
echo "OS:       $OS"
echo "ARCH:     $ARCH"
echo "CPU:      $CPU_CORES cores"
echo "RAM:      $RAM_MB MB"
echo "Storage:  $STORAGE_GB GB"

echo
echo "[2/4] Creating server directory..."

mkdir -p "$SERVER_DIR"/{config,data,logs,services,scripts,runtime}
mkdir -p "$SERVER_DIR/config/secrets"

echo "Created: $SERVER_DIR"

echo
echo "[3/4] Generating server identity..."

CONFIG_FILE="$SERVER_DIR/config/node.json"

if [ ! -f "$CONFIG_FILE" ]; then

    if [ -r /dev/urandom ]; then
        NODE_ID="server-$(od -An -N8 -tx1 /dev/urandom | tr -d ' \n')"
    else
        NODE_ID="server-$(date +%s)-$$"
    fi

    HOSTNAME="$(hostname 2>/dev/null || echo unknown)"

    cat > "$CONFIG_FILE" <<EOF
{
  "node_id": "$NODE_ID",
  "name": "$HOSTNAME",
  "role": "compute",
  "platform": "$PLATFORM",
  "os": "$OS",
  "architecture": "$ARCH",
  "cpu_cores": $CPU_CORES,
  "ram_mb": $RAM_MB,
  "storage_gb": $STORAGE_GB
}
EOF

    chmod 600 "$CONFIG_FILE"

    echo "Created node identity: $NODE_ID"

else

    echo "Node configuration already exists."

fi

echo
echo "[4/4] Installation complete."
echo
echo "Personal Server is ready."
echo "Location: $SERVER_DIR"