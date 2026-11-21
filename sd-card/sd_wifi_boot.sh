#!/bin/sh
# WTW-IPW308TTW/B: use SD Wi-Fi settings after the camera's recovery AP is ready.
set -u

BB=/bin/busybox
SD=/mnt/flash/mmc
WORK=/tmp/ipcam-sd-wifi
CONF="$SD/wpa_supplicant.conf"
TARGET=/tmp/wpa_supplicant.conf
WIFI=/mnt/flash/Server/resource/wifi

sd_mounted() {
    "$BB" awk -v sd="$SD" '$2==sd {found=1} END {exit !found}' /proc/mounts
}

has_default_route() {
    "$BB" awk '$2=="00000000" {found=1} END {exit !found}' /proc/net/route
}

check_sd() {
    config_sd=${1:-$SD}
    [ -s "$config_sd/wpa_supplicant.conf" ] && [ -s "$config_sd/lan-probe-ip" ] &&
        "$BB" awk '
            $0=="ctrl_interface=/tmp/ipcam-sd-wifi" {ctrl++}
            /^[[:space:]]*ssid=[[:xdigit:]]+$/ {ssid++; value=$0; sub(/^.*ssid=/,"",value)
                                                   if (length(value)<2 || length(value)>64 || length(value)%2) bad=1}
            /^[[:space:]]*psk=[[:xdigit:]]+$/ {psk++; value=$0; sub(/^.*psk=/,"",value)
                                                  if (length(value)!=64) bad=1}
            END {exit (bad || ctrl!=1 || ssid!=1 || psk!=1)}
        ' "$config_sd/wpa_supplicant.conf"
}

ap_ready() {
    "$BB" pidof hostapd >/dev/null &&
        "$BB" pidof udhcpd >/dev/null &&
        "$BB" ifconfig ap0 | "$BB" grep -q 'inet addr:192\.168\.222\.1 '
}

stop_our_process() {
    [ -s "$WORK/$1.pid" ] || return 0
    IFS= read -r pid < "$WORK/$1.pid" || return 0
    case "$pid" in ''|*[!0-9]*) return 0 ;; esac
    [ "$pid" -gt 1 ] || return 0
    if [ -r "/proc/$pid/cmdline" ] &&
       "$BB" grep -a -q "$WORK/$1.pid" "/proc/$pid/cmdline"; then
        kill "$pid" 2>/dev/null || :
    fi
}

rollback() {
    : > "$WORK/rollback-ready"
    remaining=45
    while [ "$remaining" -gt 0 ]; do
        [ ! -f "$WORK/commit" ] || exit 0
        [ ! -f "$WORK/failure" ] || break
        sleep 1
        remaining=$((remaining - 1))
    done
    [ ! -f "$WORK/commit" ] || exit 0
    : > "$WORK/reverting"
    echo 'Restoring the vendor AP'
    if [ -f "$WORK/takeover" ]; then
        stop_our_process dhcp
        stop_our_process sta
        "$BB" ifconfig wlan0 0.0.0.0 down
    fi
    if [ -f "$WORK/bound" ]; then
        "$BB" umount "$TARGET" || echo 'Could not remove the Wi-Fi bind mount'
    fi
    "$BB" ifconfig ap0 192.168.222.1 netmask 255.255.255.0 up
    "$BB" pidof hostapd >/dev/null || "$WIFI/hostapd" /etc/hostapd.conf -B
    "$BB" pidof udhcpd >/dev/null || "$BB" udhcpd /tmp/udhcpd.conf
    if ap_ready; then
        echo 'Vendor AP restored'
    else
        echo 'Vendor AP restoration could not be verified'
    fi
}

dhcp_event() {
    case "$1" in
        deconfig) "$BB" rm -f "$WORK/ip" ;;
        bound|renew)
            [ -n "${ip:-}" ] && [ -n "${subnet:-}" ] || return 1
            [ ! -f "$WORK/reverting" ] || return 1
            "$BB" ifconfig wlan0 "$ip" netmask "$subnet" up || return 1
            # Apply the LAN address and mask only. DHCP router and DNS are unused.
            printf '%s\n' "$ip" > "$WORK/ip.new" && "$BB" mv "$WORK/ip.new" "$WORK/ip"
            ;;
    esac
}

switch() {
    [ ! -f "$WORK/reverting" ] || return 1
    "$BB" mount -o bind "$CONF" "$TARGET" || return 1
    : > "$WORK/bound"
    sleep 3
    [ ! -f "$WORK/reverting" ] || return 1
    : > "$WORK/takeover"
    for name in hostapd udhcpd; do
        pids=$("$BB" pidof "$name") || continue
        kill $pids || return 1
    done
    if [ -d /sys/class/net/ap0 ]; then
        "$BB" ifconfig ap0 down || return 1
    fi
    "$BB" ifconfig wlan0 up || return 1
    "$WIFI/wpa_supplicant" -Dnl80211 -i wlan0 -c /etc/wpa_supplicant.conf \
        -P "$WORK/sta.pid" -B || return 1
    "$BB" udhcpc -i wlan0 -t 8 -T 2 -n -p "$WORK/dhcp.pid" -s "$WORK/dhcp.sh" || {
        echo 'DHCP client failed'; return 1;
    }
    [ -s "$WORK/ip" ] || { echo 'No eligible DHCP address'; return 1; }
    echo 'DHCP address applied'
    attempts=5
    while [ "$attempts" -gt 0 ] && [ ! -f "$WORK/reverting" ]; do
        if has_default_route; then
            echo 'Unexpected default route'
            return 1
        fi
        if "$BB" ping -I wlan0 -c 1 -W 1 "$probe_ip" >/dev/null 2>&1; then
            break
        fi
        attempts=$((attempts - 1))
        sleep 1
    done
    if [ "$attempts" -le 0 ]; then
        echo 'LAN probe did not reply'
        "$BB" ifconfig wlan0 | "$BB" grep 'inet addr:' || :
        "$BB" awk 'NR>1 {print "route", $1,$2}' /proc/net/route
        return 1
    fi
    [ ! -f "$WORK/reverting" ] || return 1
    : > "$WORK/commit"
    echo 'Station connected and verified'
}

boot() {
    [ -x "$WIFI/wpa_supplicant" ] || return 1
    [ -x "$WIFI/hostapd" ] || return 1
    remaining=60
    while ! ap_ready || ! sd_mounted; do
        if [ "$remaining" -le 0 ]; then
            echo 'Vendor AP or SD mount was not available within 60 seconds'
            return 1
        fi
        sleep 1
        remaining=$((remaining - 1))
    done
    # Let the vendor application finish its initial network handling.
    sleep 20
    check_sd || { echo 'Invalid SD Wi-Fi configuration'; return 1; }
    IFS= read -r probe_ip < "$SD/lan-probe-ip" || return 1
    case "$probe_ip" in ''|*[!0-9.]*) return 1 ;; esac
    ap_ready || return 1
    [ -f "$TARGET" ] && [ -s /tmp/hostapd.conf ] && [ -s /tmp/udhcpd.conf ] || return 1
    ! has_default_route || return 1
    if "$BB" awk '$2=="/tmp/wpa_supplicant.conf" {found=1} END {exit !found}' /proc/mounts; then
        echo 'Wi-Fi file already mounted; leaving network untouched'
        return 1
    fi
    printf '#!/bin/sh\nexec /bin/sh %s/boot.sh --rollback\n' "$WORK" > "$WORK/restore.sh"
    printf '#!/bin/sh\nexec /bin/sh %s/boot.sh --dhcp "$1"\n' "$WORK" > "$WORK/dhcp.sh"
    chmod 700 "$WORK/dhcp.sh" || return 1
    "$BB" setsid /bin/sh "$WORK/restore.sh" </dev/null >"$WORK/rollback.log" 2>&1 &
    ticks=5
    while [ ! -f "$WORK/rollback-ready" ]; do
        [ "$ticks" -gt 0 ] || return 1
        sleep 1
        ticks=$((ticks - 1))
    done
    if ! switch; then
        echo 'Station verification failed'
        : > "$WORK/failure"
        return 1
    fi
}

case "${1:-}" in
    --check) check_sd "${2:-$SD}" ;;
    --rollback) rollback ;;
    --dhcp) dhcp_event "${2:-}" ;;
    '') boot ;;
    *) exit 2 ;;
esac
