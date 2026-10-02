#!/usr/bin/env bash

# ==============================
# Personal Server - System Detect
# ==============================

ARCH="$(uname -m)"
OS="$(uname -s)"

# Detect platform
if [ -n "${PREFIX:-}" ] && [ -d "$PREFIX" ]; then
    PLATFORM="termux"

elif [ -f /etc/os-release ]; then
    . /etc/os-release

    case "${ID:-}" in
        debian)
            PLATFORM="debian"
            ;;
        ubuntu)
            PLATFORM="ubuntu"
            ;;
        *)
            PLATFORM="linux"
            ;;
    esac

else
    PLATFORM="unknown"
fi

# CPU
CPU_CORES="$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 1)"

# RAM
if [ -r /proc/meminfo ]; then
    RAM_MB="$(
        awk '/MemTotal:/ {
            printf "%d", $2 / 1024
        }' /proc/meminfo
    )"
else
    RAM_MB=0
fi

# Storage
STORAGE_KB="$(df -Pk "$HOME" 2>/dev/null | awk 'NR==2 {print $2}')"

STORAGE_GB="$(
    awk "BEGIN {
        printf \"%d\", ${STORAGE_KB:-0} / 1024 / 1024
    }"
)"