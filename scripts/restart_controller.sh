#!/bin/bash
pkill -f 'controller/controller.py' 2>/dev/null || true
sleep 2
cd "$(dirname "$0")/.." || exit 1
ROLE_FLAG=""
if [ -f config/node.conf ]; then
    HA_ROLE=$(grep '^PERSONALSERVER_HA_ROLE=' config/node.conf | cut -d'=' -f2 | tr -d '"' | tr -d "'" | tr -d '\r')
    if [ -n "$HA_ROLE" ]; then
        ROLE_FLAG="--role $HA_ROLE"
    fi
fi
nohup python controller/controller.py start --host 0.0.0.0 --port 8000 $ROLE_FLAG </dev/null >> logs/controller.log 2>&1 &
sleep 1
