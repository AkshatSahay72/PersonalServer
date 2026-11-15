#!/usr/bin/env bash
# =============================================================================
# PersonalServer — Universal Bootstrap Wrapper
# =============================================================================
# Forwards execution to the modular bootstrapper in bootstrap/install.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_SCRIPT="$SCRIPT_DIR/bootstrap/install.sh"

if [ -f "$INSTALL_SCRIPT" ]; then
    exec bash "$INSTALL_SCRIPT" "$@"
else
    echo "Error: $INSTALL_SCRIPT not found." >&2
    exit 1
fi
