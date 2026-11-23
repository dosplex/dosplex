#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
# DosPlex End-to-End Input Handover Test (Python IPC version)

IPC_SOCK="${DOSPLEX_HUD_IPC_SOCK:-/run/dosplex/hud-ipc.sock}"
LOG_FILE="dosplex-test.log"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

send_ipc() {
    local cmd="$1"
    python3 -c "import socket; s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM); s.sendto(b'$cmd', '$IPC_SOCK')"
}

check_log() {
    local pattern="$1"
    local timeout=2
    local count=0
    while ! grep -q "$pattern" "$LOG_FILE"; do
        sleep 0.5
        count=$((count + 1))
        if [ $count -gt $((timeout * 2)) ]; then
            return 1
        fi
    done
    return 0
}

echo "--- DosPlex Input Handover Test ---"

if [ ! -S "$IPC_SOCK" ]; then
    echo -e "${RED}[FAIL]${NC} IPC socket $IPC_SOCK not found."
    exit 1
fi

DEVICE="Test Mouse"

echo "[1/3] Testing: Assign to Guest"
send_ipc "ROUTE $DEVICE right"
if check_log "Added dynamic route for $DEVICE to right"; then
    echo -e "${GREEN}[OK]${NC} Compositor acknowledged route to guest"
else
    echo -e "${RED}[FAIL]${NC} Compositor did not log route update"
fi

echo "[2/3] Testing: Re-assign to Primary"
send_ipc "ROUTE $DEVICE left"
if check_log "Updated dynamic route for $DEVICE to left"; then
    echo -e "${GREEN}[OK]${NC} Compositor acknowledged route update to primary"
else
    echo -e "${RED}[FAIL]${NC} Compositor did not log route update"
fi

echo "[3/4] Testing: Follow Cursor"
send_ipc "ROUTE $DEVICE follow"
if check_log "Updated dynamic route for $DEVICE to follow"; then
    echo -e "${GREEN}[OK]${NC} Compositor acknowledged follow-cursor route"
else
    echo -e "${RED}[FAIL]${NC} Compositor did not log follow-cursor route"
fi

echo "[4/4] Testing: Reset to Default"
send_ipc "ROUTE $DEVICE default"
if check_log "Removing dynamic route for $DEVICE"; then
    echo -e "${GREEN}[OK]${NC} Compositor acknowledged route removal"
else
    echo -e "${RED}[FAIL]${NC} Compositor did not log route removal"
fi

echo "----------------------------------"
echo "E2E Test Complete"
