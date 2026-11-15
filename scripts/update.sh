#!/usr/bin/env bash

# ==============================================================================
# PersonalServer - Production Release Update Wrapper
# Safe, verified, non-destructive release updates with offline support & rollback
# ==============================================================================

set -e

BASE_DIR="$(cd "$(dirname "$0")/.." && pwd)"

# Detect python executable
if command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="python3"
elif command -v python >/dev/null 2>&1; then
    PYTHON_BIN="python"
else
    echo "Error: Python 3 is required for release management." >&2
    exit 1
fi

RELEASE_SCRIPT="$BASE_DIR/scripts/release_manager.py"
if [ ! -f "$RELEASE_SCRIPT" ]; then
    echo "Error: Release manager script missing at $RELEASE_SCRIPT" >&2
    exit 1
fi

# If no arguments provided, default to 'update'
if [ $# -eq 0 ]; then
    "$PYTHON_BIN" "$RELEASE_SCRIPT" update
else
    "$PYTHON_BIN" "$RELEASE_SCRIPT" "$@"
fi
EXIT_CODE=$?
exit $EXIT_CODE
