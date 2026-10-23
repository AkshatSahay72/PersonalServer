#!/usr/bin/env bash
# ==============================================================================
# PersonalServer - Unified Diagnostic & Recovery Engine (Termux / Linux Wrapper)
# ==============================================================================
set -e

BASE_DIR="$(cd "$(dirname "$0")/.." && pwd)"

# Detect python executable (prefer python3, fallback to python)
if command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="python3"
elif command -v python >/dev/null 2>&1; then
    PYTHON_BIN="python"
else
    echo "Error: Python 3 is required but neither 'python3' nor 'python' was found on PATH." >&2
    exit 2
fi

# Convert convenience subcommands (fix, repair, recover) to CLI flags
ARGS=()
for arg in "$@"; do
    case "$arg" in
        fix|repair|recover)
            ARGS+=("--repair")
            ;;
        dry-run)
            ARGS+=("--dry-run")
            ;;
        watchdog)
            ARGS+=("--watchdog")
            ;;
        *)
            ARGS+=("$arg")
            ;;
    esac
done

# Execute diagnostic engine forwarding processed arguments and preserving exit code
set +e
"$PYTHON_BIN" "$BASE_DIR/scripts/doctor.py" "${ARGS[@]}"
EXIT_CODE=$?
exit $EXIT_CODE
