#!/bin/sh
# Run after Wi-Fi setup. The HTTP document root contains only server inputs.
set -eu

BB=/bin/busybox
WORK=/tmp/ipcam-sd-wifi
ROOT="$WORK/http"
mkdir -p "$ROOT"
ln -sf /mnt/flash/data/device_model.txt "$ROOT/device_model.txt"
ln -sf /mnt/flash/data/motorPosition "$ROOT/motorPosition"

pid=
address=
while :; do
    next_address=
    if [ -s "$WORK/ip" ]; then
        IFS= read -r next_address < "$WORK/ip" || :
    fi
    if [ "$address" != "$next_address" ] && [ -n "$pid" ]; then
        kill "$pid" 2>/dev/null || :
        wait "$pid" 2>/dev/null || :
        pid=
    fi
    address="$next_address"
    if [ -n "$address" ] && { [ -z "$pid" ] || ! kill -0 "$pid" 2>/dev/null; }; then
        "$BB" httpd -f -p "$address:10081" -h "$ROOT" &
        pid=$!
    fi
    sleep 30
done
