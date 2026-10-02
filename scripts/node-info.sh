#!/data/data/com.termux/files/usr/bin/bash

# ==============================================================================
# PersonalServer - Node Information Script
# ==============================================================================

BASE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
CONFIG_DIR="$BASE_DIR/config"
NODE_JSON="$CONFIG_DIR/node.json"
NODE_CONF="$CONFIG_DIR/node.conf"

echo "=========================================="
echo " PersonalServer Node Information"
echo "=========================================="

if [ -f "$NODE_JSON" ]; then
    python3 -c "
import json
try:
    with open('$NODE_JSON') as f:
        d = json.load(f)
        for k, v in d.items():
            print(f\"{k.replace('_', ' ').title():<16}: {v}\")
except Exception as e:
    print(f\"Error reading node.json: {e}\")
" 2>/dev/null
elif [ -f "$NODE_CONF" ]; then
    while IFS='=' read -r key val; do
        [[ -z "$key" || "$key" =~ ^# ]] && continue
        printf "%-16s: %s\n" "$key" "$val"
    done < "$NODE_CONF"
else
    echo "No configuration file found in $CONFIG_DIR"
fi
echo "=========================================="
