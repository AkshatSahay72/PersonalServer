#!/usr/bin/env bash

set -e

echo "================================="
echo "       PERSONAL SERVER"
echo "================================="
echo

echo "[1/4] Detecting system..."

OS="$(uname -s)"
ARCH="$(uname -m)"

echo "OS:   $OS"
echo "ARCH: $ARCH"

echo
echo "[2/4] Creating server directory..."

SERVER_DIR="$HOME/personal-server"

mkdir -p "$SERVER_DIR"/{config,data,logs,services,scripts}

echo "Created: $SERVER_DIR"

echo
echo "[3/4] Generating server identity..."

if [ ! -f "$SERVER_DIR/config/server.conf" ]; then
    SERVER_ID="server-$(date +%s)"

    cat > "$SERVER_DIR/config/server.conf" <<EOF
SERVER_ID=$SERVER_ID
SERVER_NAME=$(hostname)
OS=$OS
ARCH=$ARCH
EOF

    echo "Created server identity: $SERVER_ID"
else
    echo "Server configuration already exists."
fi

echo
echo "[4/4] Installation complete."

echo
echo "Personal Server is ready."
echo "Location: $SERVER_DIR"