#!/usr/bin/env bash
# =============================================================================
# PersonalServer — Universal Dependency Installer (dependencies.sh)
# =============================================================================
# Installs system and runtime packages based on the detected platform.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -z "${PLATFORM:-}" ]; then
    # shellcheck disable=SC1091
    source "$SCRIPT_DIR/detect.sh"
fi

echo "Installing dependencies for platform: $PLATFORM ($PKG_MGR)..."

case "$PLATFORM" in
    termux)
        echo "[+] Updating Termux package lists..."
        pkg update -y || apt-get update -y || true

        echo "[+] Installing Termux core packages (python, git, curl, proot)..."
        pkg install -y python git curl proot || apt-get install -y python git curl proot

        if command -v termux-wake-lock >/dev/null 2>&1; then
            echo "[+] Acquiring Termux wake-lock to prevent background process throttling..."
            termux-wake-lock || true
        fi
        ;;

    debian|ubuntu|raspberrypi)
        echo "[+] Updating APT repositories..."
        if [ -n "$SUDO_CMD" ]; then
            $SUDO_CMD apt-get update -y
            $SUDO_CMD apt-get install -y python3 python3-pip python3-venv git curl procps
        elif [ "$(id -u)" -eq 0 ]; then
            apt-get update -y
            apt-get install -y python3 python3-pip python3-venv git curl procps
        else
            echo "[!] Non-root user without sudo; skipping system package installation."
            echo "[*] Ensure python3, git, curl, and procps are installed."
        fi
        ;;

    redhat)
        echo "[+] Installing packages via $PKG_MGR..."
        if [ -n "$SUDO_CMD" ]; then
            $SUDO_CMD "$PKG_MGR" install -y python3 python3-pip git curl procps-ng
        elif [ "$(id -u)" -eq 0 ]; then
            "$PKG_MGR" install -y python3 python3-pip git curl procps-ng
        fi
        ;;

    arch)
        echo "[+] Installing packages via pacman..."
        if [ -n "$SUDO_CMD" ]; then
            $SUDO_CMD pacman -Sy --noconfirm python python-pip git curl procps-ng
        elif [ "$(id -u)" -eq 0 ]; then
            pacman -Sy --noconfirm python python-pip git curl procps-ng
        fi
        ;;

    macos)
        if command -v brew >/dev/null 2>&1; then
            echo "[+] Installing packages via Homebrew..."
            brew install python3 git curl
        else
            echo "[*] Homebrew not found. Please ensure Python 3, git, and curl are installed."
        fi
        ;;

    *)
        echo "[*] Generic platform ($PLATFORM). Verifying existing tools..."
        ;;
esac

# Re-detect Python command in case it was just installed
for cmd in python3 python; do
    if command -v "$cmd" >/dev/null 2>&1; then
        if "$cmd" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 7) else 1)' >/dev/null 2>&1; then
            PYTHON_CMD="$cmd"
            export PYTHON_CMD
            break
        fi
    fi
done

if [ -z "${PYTHON_CMD:-}" ]; then
    echo "[!] Error: Python 3 (>= 3.7) could not be located or installed." >&2
    exit 1
fi

echo "[OK] Python runtime confirmed: $("$PYTHON_CMD" --version)"
