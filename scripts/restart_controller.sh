#!/bin/bash
pkill -f 'controller/controller.py' 2>/dev/null || true
sleep 2
cd "$(dirname "$0")/.." || exit 1
nohup python controller/controller.py start --host 0.0.0.0 --port 8000 </dev/null >> logs/controller.log 2>&1 &
sleep 1
