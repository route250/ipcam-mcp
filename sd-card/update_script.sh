#!/bin/sh
# The vendor executes this hook using the temporary SD mount before hiapp starts.
set -u

BB=/bin/busybox
SD=/tmp/mmc
WORK=/tmp/ipcam-sd-wifi
umask 077

if [ "${1:-}" = --keep-jst ]; then
    while :; do
        current_tz=
        if [ -r /tmp/TZ ]; then
            IFS= read -r current_tz < /tmp/TZ || :
        fi
        [ "$current_tz" = JST-9 ] || printf 'JST-9\n' > /tmp/TZ
        sleep 5
    done
fi

set_camera_input_volume() (
    config=/mnt/flash/data/hank_default_parameter.ini
    source=$config
    if [ ! -f "$source" ]; then
        source=/mnt/flash/databak/hank_default_parameter.ini
        [ -f "$source" ] || source=/mnt/flash/Server/databak/hank_default_parameter.ini
    fi
    [ -f "$source" ] || exit 1

    updated=$("$BB" mktemp /tmp/ipcam-input-volume.XXXXXX) || exit 1
    staged=
    trap '"$BB" rm -f "$updated"; [ -z "$staged" ] || "$BB" rm -f "$staged"' EXIT
    "$BB" awk '
        /^[[:blank:]]*\[/ {
            parameter = ($0 ~ /^[[:blank:]]*\[PARAMETER_ITEM\][[:space:]]*$/)
        }
        parameter && /^[[:blank:]]*InPutVolume[[:blank:]]*=/ {
            count++
            if (!sub(/=[[:blank:]]*[0-9]+/, "=5")) invalid = 1
        }
        { print }
        END { if (count != 1 || invalid) exit 1 }
    ' "$source" > "$updated" || exit 1

    if [ "$source" = "$config" ]; then
        current=$("$BB" md5sum < "$config") && desired=$("$BB" md5sum < "$updated") || exit 1
        [ "$current" != "$desired" ] || exit 0
    fi

    staged=$("$BB" mktemp "$config.ipcam.XXXXXX") || exit 1
    "$BB" cp -p "$source" "$staged" && "$BB" cat "$updated" > "$staged" &&
        "$BB" mv "$staged" "$config" && "$BB" sync
)

if [ -s "$SD/ipcam-telnet-local.sh" ]; then
    /bin/sh "$SD/ipcam-telnet-local.sh" || echo 'Local Telnet setup failed'
fi
printf 'JST-9\n' > /tmp/TZ
set_camera_input_volume || echo 'Camera microphone input volume setup failed'

[ -x "$SD/ipcam-wifi-launch" ] || exit 0
"$BB" mkdir "$WORK" 2>/dev/null || exit 0
"$BB" cp "$SD/ipcam-wifi-launch" "$WORK/launch" && chmod 700 "$WORK/launch" || exit 1
printf '#!/bin/sh\nexec /bin/sh /tmp/update_script.sh --keep-jst\n' > "$WORK/timezone.sh" || exit 1
"$WORK/launch" detach-script "$WORK/timezone.sh" "$WORK/timezone.log" || echo 'JST watchdog did not start'

[ -s "$SD/sd_wifi_boot.sh" ] && [ -s "$SD/sd_http_watchdog.sh" ] || exit 0
/bin/sh "$SD/sd_wifi_boot.sh" --check "$SD" || exit 0
"$BB" cp "$SD/sd_wifi_boot.sh" "$WORK/boot.sh" &&
    "$BB" cp "$SD/sd_http_watchdog.sh" "$WORK/http.sh" || exit 1
printf '#!/bin/sh\n/bin/sh %s/boot.sh && exec /bin/sh %s/http.sh\n' "$WORK" "$WORK" > "$WORK/start.sh" || exit 1

if [ ! -e "$SD/ipcam-wifiinfo.disabled" ] && [ -s "$SD/WifiInfo.ipcam-dummy" ] &&
   [ -s "$SD/ipcam-wifiinfo-bind.sh" ]; then
    /bin/sh "$SD/ipcam-wifiinfo-bind.sh" "$SD" "$WORK" || echo 'WifiInfo overlay failed'
fi
if ! "$WORK/launch" detach-script "$WORK/start.sh" "$WORK/boot.log"; then
    if [ -f "$WORK/wifiinfo-overlay" ]; then
        "$BB" umount /mnt/flash/data/WifiInfo
    fi
    exit 1
fi
