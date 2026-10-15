#!/data/data/com.termux/files/usr/bin/bash
# =============================================================================
# PersonalServer — Termux Worker Node Provisioning Script (Unrooted Android)
# =============================================================================
# Sets up PersonalServer worker agent in an unrooted Termux environment.
# Configures wake-lock, installs python prerequisites, and generates node identity.

set -euo pipefail

echo "======================================================="
echo " PersonalServer — Termux Worker Setup (Unrooted)"
echo "======================================================="

# 1. Environment Verification
if [ -z "${PREFIX:-}" ] || [ ! -d "$PREFIX" ]; then
    echo "[!] Warning: PREFIX environment variable not set. Are you running inside Termux?"
fi

# 2. Package Installation
echo "[+] Updating Termux package lists and installing core utilities..."
pkg update -y || apt update -y
pkg install -y python git curl proot || apt install -y python git curl proot

# 3. Prevent Android Doze / Process Suspension
if command -v termux-wake-lock >/dev/null 2>&1; then
    echo "[+] Acquiring Termux wake-lock to prevent background process throttling..."
    termux-wake-lock
else
    echo "[*] termux-wake-lock utility not found; ensure Termux:API is installed if running unattended."
fi

# 4. Directory Structure
SERVER_DIR="${PERSONALSERVER_BASE_DIR:-$HOME/PersonalServer}"
echo "[+] Setting up worker directory structure at: $SERVER_DIR"
mkdir -p "$SERVER_DIR"/{config/secrets,data,logs,runtime/jobs,storage/applications,scripts}

# 5. Node Identity Generation
CONFIG_FILE="$SERVER_DIR/config/node.json"
if [ ! -f "$CONFIG_FILE" ]; then
    echo "[+] Generating node identity..."
    NODE_ID="server-termux-$(od -An -N6 -tx1 /dev/urandom | tr -d ' \n' 2>/dev/null || date +%s)"
    HOSTNAME="$(getprop net.hostname 2>/dev/null || hostname 2>/dev/null || echo 'android-worker')"
    CPU_CORES="$(getconf _NPROCESSORS_ONLN 2>/dev/null || nproc 2>/dev/null || echo 4)"
    
    RAM_MB=2048
    if [ -r /proc/meminfo ]; then
        RAM_MB="$(awk '/MemTotal:/ { printf "%d", $2 / 1024 }' /proc/meminfo)"
    fi

    STORAGE_GB=32
    if command -v df >/dev/null 2>&1; then
        STORAGE_KB="$(df -Pk "$HOME" 2>/dev/null | awk 'NR==2 {print $2}')"
        if [ -n "$STORAGE_KB" ] && [ "$STORAGE_KB" -gt 0 ]; then
            STORAGE_GB="$(( STORAGE_KB / 1024 / 1024 ))"
        fi
    fi

    cat > "$CONFIG_FILE" <<EOF
{
  "node_id": "$NODE_ID",
  "name": "$HOSTNAME",
  "role": "compute",
  "platform": "termux",
  "os": "Android Linux",
  "architecture": "aarch64",
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
    echo "[OK] Generated node configuration: $CONFIG_FILE"
else
    echo "[*] Existing node configuration found at $CONFIG_FILE"
fi

echo "======================================================="
echo "[OK] Termux worker provisioning complete."
echo "Next step: Register with the controller using:"
echo "  python agent/node-agent.py register --controller <CONTROLLER_URL> --token <ENROLLMENT_TOKEN>"
echo "  python agent/node-agent.py work"
echo "======================================================="
