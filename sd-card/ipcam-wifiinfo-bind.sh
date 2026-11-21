#!/bin/sh
# Run before the vendor application reads WifiInfo.
set -eu

BB=/bin/busybox
source_file=$1/WifiInfo.ipcam-dummy
target=/mnt/flash/data/WifiInfo
work=$2

[ -f "$source_file" ] && [ -f "$target" ] || exit 1
"$BB" cp "$source_file" "$work/WifiInfo"
[ "$("$BB" stat -c %s "$work/WifiInfo")" = 112 ] || exit 1
: > "$work/wifiinfo-overlay"
if ! "$BB" mount -o bind "$work/WifiInfo" "$target"; then
    "$BB" rm -f "$work/wifiinfo-overlay"
    exit 1
fi
