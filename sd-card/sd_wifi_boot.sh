#!/bin/sh
# WTW-IPW308TTW/B: use SD Wi-Fi settings after the camera's recovery AP is ready.
set -u

BB=/bin/busybox
SD=/mnt/flash/mmc
WORK=/tmp/ipcam-sd-wifi
CONF="$SD/wpa_supplicant.conf"
TARGET=/tmp/wpa_supplicant.conf
WIFI=/mnt/flash/Server/resource/wifi

wifiinfo_trial() {
    [ -f "$WORK/wifiinfo-overlay" ]
}

fallback_wifiinfo() {
    wifiinfo_trial || return 0
    [ ! -f "$WORK/commit" ] || return 0
    "$BB" mkdir "$WORK/wifiinfo-fallback" 2>/dev/null || return 0
    # Persist the skip flag before rebooting so the same trial cannot loop.
    if sd_mounted && : > "$SD/ipcam-wifiinfo.disabled" && "$BB" sync; then
        echo 'WiFiInfo trial failed; rebooting with the original Wi-Fi startup'
        "$BB" reboot
    else
        echo 'Could not save the WiFiInfo skip flag; leaving reboot to operator'
    fi
}

sd_mounted() {
    "$BB" awk -v sd="$SD" '$2==sd {found=1} END {exit !found}' /proc/mounts
}

has_default_route() {
    "$BB" awk '$2=="00000000" {found=1} END {exit !found}' /proc/net/route
}

# Validate the list before passing its addresses to ping.
read_probes() {
    "$BB" awk '
        function ipv4(s, a, i) {
            if (split(s,a,".")!=4) return 0
            for (i=1; i<=4; i++)
                if (a[i] !~ /^(0|[1-9][0-9]*)$/ || a[i]>255) return 0
            return 1
        }
        {sub(/^[[:space:]]+/, ""); sub(/[[:space:]]+$/, "")}
        /^($|#)/ {next}
        !ipv4($0) {exit 1}
        {print; count++}
        END {if (!count) exit 1}
    ' "$1"
}

# If configured, require the DHCP address and mask to match the LAN prefix.
matches_lan() {
    [ -f "$SD/lan-cidr" ] || return 0
    "$BB" awk -v ip="$1" -v mask="$2" '
        function ipv4(s, a, i, n) {
            if (split(s,a,".")!=4) return -1
            for (i=1; i<=4; i++) {
                if (a[i] !~ /^(0|[1-9][0-9]*)$/ || a[i]>255) return -1
                n=n*256+a[i]
            }
            return n+0
        }
        NR==1 && split($1,cidr,"/")==2 && NF==1 {
            network=ipv4(cidr[1])
            prefix=cidr[2]
            if (network<0 || prefix !~ /^[0-9]+$/ || prefix>32) exit 1
            block=1
            for (i=prefix; i<32; i++) block*=2
            address=ipv4(ip)
            if (address<0 || ipv4(mask)!=4294967296-block || int(address/block)*block!=network) exit 1
            match_ok=1
        }
        END {if (!match_ok || NR!=1) exit 1}
    ' "$SD/lan-cidr"
}

probe_lan() {
    for probe_ip in $probe_ips; do
        [ ! -f "$WORK/reverting" ] || return 1
        if "$BB" ping -I wlan0 -c 1 -W 1 "$probe_ip" >/dev/null 2>&1; then
            return 0
        fi
    done
    return 1
}

check_sd() {
    config_sd=${1:-$SD}
    read_probes "$config_sd/lan-probe-ip" >/dev/null &&
        [ -s "$config_sd/wpa_supplicant.conf" ] &&
        "$BB" awk '
            {sub(/^[[:space:]]+/, ""); sub(/[[:space:]]+$/, "")}
            /^($|#)/ {next}
            !in_network && $0=="ctrl_interface=/tmp/ipcam-sd-wifi" {ctrl++; next}
            !in_network && /^country=[A-Z][A-Z]$/ {next}
            !in_network && $0=="network={" {
                in_network=1; ssid=psk=wpa_psk=0; next
            }
            in_network && /^ssid=[[:xdigit:]]+$/ {
                value=tolower(substr($0, 6))
                if (length(value)<2 || length(value)>64 || length(value)%2 || seen[value]++) exit 1
                ssid++; next
            }
            in_network && /^psk=[[:xdigit:]]+$/ {
                if (length($0)!=68) exit 1
                psk++; next
            }
            in_network && /^scan_ssid=[01]$/ {next}
            in_network && $0=="key_mgmt=WPA-PSK" {wpa_psk++; next}
            in_network && $0=="}" {
                if (ssid!=1 || psk!=1 || wpa_psk!=1) exit 1
                in_network=0; networks++; next
            }
            {exit 1}
            END {if (ctrl!=1 || networks<1 || in_network) exit 1}
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
    if wifiinfo_trial; then
        fallback_wifiinfo
        return
    fi
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
            matches_lan "$ip" "$subnet" || return 1
            [ ! -f "$WORK/reverting" ] || return 1
            "$BB" ifconfig wlan0 "$ip" netmask "$subnet" up || return 1
            # Apply the LAN address and mask only. DHCP router and DNS are unused.
            printf '%s\n' "$ip" > "$WORK/ip.new" && "$BB" mv "$WORK/ip.new" "$WORK/ip"
            ;;
    esac
}

switch() {
    [ ! -f "$WORK/reverting" ] || return 1
    if wifiinfo_trial; then
        # The stock Wi-Fi code can overwrite /etc/wpa_supplicant.conf.
        station_conf="$WORK/station.conf"
        "$BB" cp "$CONF" "$station_conf" || return 1
    else
        station_conf=/etc/wpa_supplicant.conf
        "$BB" mount -o bind "$CONF" "$TARGET" || return 1
        : > "$WORK/bound"
    fi
    sleep 3
    [ ! -f "$WORK/reverting" ] || return 1
    : > "$WORK/takeover"
    names='hostapd udhcpd'
    if wifiinfo_trial; then
        names="$names wpa_supplicant udhcpc"
    fi
    for name in $names; do
        pids=$("$BB" pidof "$name") || continue
        kill $pids || return 1
    done
    if [ -d /sys/class/net/ap0 ]; then
        "$BB" ifconfig ap0 down || return 1
    fi
    if wifiinfo_trial; then
        "$BB" ifconfig wlan0 0.0.0.0 || return 1
    fi
    "$BB" ifconfig wlan0 up || return 1
    "$WIFI/wpa_supplicant" -Dnl80211 -i wlan0 -c "$station_conf" \
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
        if probe_lan; then
            break
        fi
        attempts=$((attempts - 1))
        sleep 1
    done
    if [ "$attempts" -le 0 ]; then
        echo 'LAN probes did not reply'
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
            if wifiinfo_trial && sd_mounted; then
                echo 'Vendor AP did not start; attempting station mode directly'
                break
            fi
            echo 'Vendor AP or SD mount was not available within 60 seconds'
            return 1
        fi
        sleep 1
        remaining=$((remaining - 1))
    done
    # Let the vendor application finish its initial network handling.
    sleep 20
    check_sd || { echo 'Invalid SD Wi-Fi configuration'; return 1; }
    probe_ips=$(read_probes "$SD/lan-probe-ip") || return 1
    if ! wifiinfo_trial; then
        ap_ready || return 1
        [ -f "$TARGET" ] && [ -s /tmp/hostapd.conf ] && [ -s /tmp/udhcpd.conf ] || return 1
        ! has_default_route || return 1
    else
        # Drop any default route the stock client may have installed.
        "$BB" route del default dev wlan0 2>/dev/null || :
        ! has_default_route || return 1
    fi
    if "$BB" awk '$2=="/tmp/wpa_supplicant.conf" {found=1} END {exit !found}' /proc/mounts; then
        echo 'Wi-Fi file already mounted; leaving network untouched'
        return 1
    fi
    printf '#!/bin/sh\nexec /bin/sh %s/boot.sh --rollback\n' "$WORK" > "$WORK/restore.sh"
    printf '#!/bin/sh\nexec /bin/sh %s/boot.sh --dhcp "$1"\n' "$WORK" > "$WORK/dhcp.sh"
    chmod 700 "$WORK/dhcp.sh" || return 1
    "$WORK/launch" detach-script "$WORK/restore.sh" /tmp/ipcam-sd-wifi-rollback.log || return 1
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
    '') if ! boot; then fallback_wifiinfo; exit 1; fi ;;
    *) exit 2 ;;
esac
