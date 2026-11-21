#!/bin/sh
# The vendor executes this hook before starting its application.
set -eu

BB=/bin/busybox
SD=/mnt/flash/mmc
WORK=/tmp/ipcam-sd-wifi
umask 077

[ -s "$SD/sd_wifi_boot.sh" ] && [ -s "$SD/sd_http_watchdog.sh" ] || exit 0
/bin/sh "$SD/sd_wifi_boot.sh" --check || exit 0
"$BB" mkdir "$WORK" 2>/dev/null || exit 0
"$BB" cp "$SD/sd_wifi_boot.sh" "$WORK/boot.sh"
"$BB" cp "$SD/sd_http_watchdog.sh" "$WORK/http.sh"
"$BB" setsid /bin/sh -c '/bin/sh "$1/boot.sh" && exec /bin/sh "$1/http.sh"' \
    ipcam "$WORK" </dev/null >"$WORK/boot.log" 2>&1 &
