#!/usr/bin/env bash
# =============================================================================
# PersonalServer — Node Uninstaller & Cleanup (uninstall.sh)
# =============================================================================
# Safely stops managed services, releases wake-locks, and cleans up runtime state.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_DIR="${PERSONALSERVER_BASE_DIR:-$(cd "$SCRIPT_DIR/.." && pwd)}"

PURGE=false
while [ $# -gt 0 ]; do
    case "$1" in
        --purge)
            PURGE=true
            shift
            ;;
        *)
            shift
            ;;
    esac
done

echo "======================================================="
echo " PersonalServer — Node Uninstaller"
echo "======================================================="

# 1. Stop active services
if [ -f "$BASE_DIR/scripts/stop.sh" ]; then
    echo "[+] Stopping running services via scripts/stop.sh..."
    bash "$BASE_DIR/scripts/stop.sh" || true
fi

# 2. Release Termux wake-lock
if command -v termux-wake-unlock >/dev/null 2>&1; then
    echo "[+] Releasing Termux wake-lock..."
    termux-wake-unlock || true
fi

# 3. Clean up runtime state
echo "[+] Cleaning up runtime PID files and sockets..."
rm -f "$BASE_DIR"/runtime/*.pid "$BASE_DIR"/runtime/*.sock 2>/dev/null || true

# 4. Optional purge of configuration and data
if [ "$PURGE" = true ]; then
    echo "[!] Purge flag specified. Removing config, logs, and data..."
    rm -rf "$BASE_DIR/config" "$BASE_DIR/logs" "$BASE_DIR/data" "$BASE_DIR/runtime"
    echo "[OK] Configuration and data removed."
else
    echo "[*] Preserving node configuration ($BASE_DIR/config) and data."
    echo "[*] Use --purge to completely remove configuration and data."
fi

echo "======================================================="
echo "[OK] PersonalServer uninstallation complete."
echo "======================================================="
