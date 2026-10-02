#!/data/data/com.termux/files/usr/bin/bash

# ==============================================================================
# PersonalServer - Service Restart Script
# ==============================================================================

BASE_DIR="$(cd "$(dirname "$0")/.." && pwd)"

echo "=========================================="
echo " PersonalServer - Restarting Services"
echo "=========================================="

"$BASE_DIR/scripts/stop.sh"
sleep 1
echo
"$BASE_DIR/scripts/start.sh"
