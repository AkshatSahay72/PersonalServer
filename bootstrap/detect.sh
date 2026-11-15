#!/usr/bin/env bash
# =============================================================================
# PersonalServer — Universal Environment Detection (detect.sh)
# =============================================================================
# Detects OS, platform, CPU architecture, resources, package manager, and python.

set -euo pipefail

# 1. Basic OS & Architecture
OS="$(uname -s 2>/dev/null || echo "Unknown")"
ARCH="$(uname -m 2>/dev/null || echo "Unknown")"

# Normalize ARCH
case "$ARCH" in
    x86_64|amd64)
        ARCH="x86_64"
        ;;
    aarch64|arm64)
        ARCH="aarch64"
        ;;
    armv7l|armhf)
        ARCH="armv7l"
        ;;
    i386|i686)
        ARCH="x86"
        ;;
esac

# 2. Platform & Distribution Detection
PKG_MGR="unknown"
PLATFORM="unknown"
DISTRO="unknown"
DISTRO_VERSION=""

if [ -n "${PREFIX:-}" ] && [ -d "${PREFIX:-}" ] && [ -x "${PREFIX:-}/bin/pkg" ]; then
    PLATFORM="termux"
    DISTRO="termux"
    PKG_MGR="pkg"
elif [ -f /etc/os-release ]; then
    # Standard Linux distribution detection via /etc/os-release
    # shellcheck disable=SC1091
    . /etc/os-release
    DISTRO="${ID:-linux}"
    DISTRO_VERSION="${VERSION_ID:-}"

    case "$DISTRO" in
        debian)
            PLATFORM="debian"
            PKG_MGR="apt"
            ;;
        ubuntu)
            PLATFORM="ubuntu"
            PKG_MGR="apt"
            ;;
        raspbian)
            PLATFORM="raspberrypi"
            PKG_MGR="apt"
            ;;
        fedora|rhel|centos)
            PLATFORM="redhat"
            if command -v dnf >/dev/null 2>&1; then
                PKG_MGR="dnf"
            else
                PKG_MGR="yum"
            fi
            ;;
        arch|manjaro)
            PLATFORM="arch"
            PKG_MGR="pacman"
            ;;
        alpine)
            PLATFORM="alpine"
            PKG_MGR="apk"
            ;;
        *)
            PLATFORM="linux"
            if command -v apt-get >/dev/null 2>&1; then
                PKG_MGR="apt"
            elif command -v dnf >/dev/null 2>&1; then
                PKG_MGR="dnf"
            fi
            ;;
    esac
elif [ "$OS" = "Darwin" ]; then
    PLATFORM="macos"
    DISTRO="darwin"
    if command -v brew >/dev/null 2>&1; then
        PKG_MGR="brew"
    fi
fi

# 3. Hardware Resource Probing
CPU_CORES="$(nproc 2>/dev/null || getconf _NPROCESSORS_ONLN 2>/dev/null || echo 1)"

RAM_MB=0
if [ -r /proc/meminfo ]; then
    RAM_MB="$(awk '/MemTotal:/ { printf "%d", $2 / 1024 }' /proc/meminfo 2>/dev/null || echo 0)"
elif command -v sysctl >/dev/null 2>&1; then
    MEM_BYTES="$(sysctl -n hw.memsize 2>/dev/null || echo 0)"
    RAM_MB="$(( MEM_BYTES / 1024 / 1024 ))"
fi

STORAGE_GB=0
STORAGE_TARGET="${PERSONALSERVER_BASE_DIR:-$HOME}"
if command -v df >/dev/null 2>&1; then
    STORAGE_KB="$(df -Pk "$STORAGE_TARGET" 2>/dev/null | awk 'NR==2 {print $2}' || echo 0)"
    if [ -n "$STORAGE_KB" ] && [ "$STORAGE_KB" -gt 0 ] 2>/dev/null; then
        STORAGE_GB="$(( STORAGE_KB / 1024 / 1024 ))"
    fi
fi

# 4. Python Runtime Detection
PYTHON_CMD=""
for cmd in python3 python; do
    if command -v "$cmd" >/dev/null 2>&1; then
        if "$cmd" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 7) else 1)' >/dev/null 2>&1; then
            PYTHON_CMD="$cmd"
            break
        fi
    fi
done

# 5. Sudo Privileges Check
SUDO_CMD=""
if [ "$(id -u 2>/dev/null || echo 1)" -ne 0 ]; then
    if command -v sudo >/dev/null 2>&1; then
        SUDO_CMD="sudo"
    fi
fi

# Export variables for consuming scripts
export OS ARCH PLATFORM DISTRO DISTRO_VERSION PKG_MGR CPU_CORES RAM_MB STORAGE_GB PYTHON_CMD SUDO_CMD

# If executed directly (not sourced), output summary
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    echo "OS:             $OS"
    echo "Platform:       $PLATFORM"
    echo "Distro:         $DISTRO ($DISTRO_VERSION)"
    echo "Architecture:   $ARCH"
    echo "Package Mgr:    $PKG_MGR"
    echo "CPU Cores:      $CPU_CORES"
    echo "RAM:            ${RAM_MB} MB"
    echo "Storage:        ${STORAGE_GB} GB"
    echo "Python:         ${PYTHON_CMD:-Not found}"
    echo "Sudo:           ${SUDO_CMD:-Not required / Not available}"
fi
