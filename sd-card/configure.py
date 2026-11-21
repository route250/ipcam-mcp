"""Create the camera SD card's local Wi-Fi settings."""

import argparse
import getpass
import hashlib
import os
from ipaddress import IPv4Address, IPv4Network
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, help="mounted SD card root")
    parser.add_argument("--ssid", action="append", required=True,
                        help="Wi-Fi network name (repeat for each network)")
    parser.add_argument("--probe-ip", type=IPv4Address, action="append", required=True,
                        help="same-LAN address that answers ICMP echo (repeat for alternatives)")
    parser.add_argument("--lan-cidr", type=IPv4Network,
                        help="optional network and prefix to require from DHCP")
    args = parser.parse_args()

    ssids = [name.encode() for name in args.ssid]
    if any(not 1 <= len(ssid) <= 32 for ssid in ssids):
        parser.error("SSID must be 1 to 32 UTF-8 bytes")
    if len(set(ssids)) != len(ssids):
        parser.error("SSID must be unique")

    networks = []
    for name, ssid in zip(args.ssid, ssids):
        passphrase = getpass.getpass(f"Wi-Fi passphrase for {name!r}: ").encode()
        if not 8 <= len(passphrase) <= 63:
            parser.error("Wi-Fi passphrase must be 8 to 63 bytes")
        psk = hashlib.pbkdf2_hmac("sha1", passphrase, ssid, 4096, 32).hex()
        networks.append(f"network={{\n    ssid={ssid.hex()}\n    psk={psk}\n"
                        "    key_mgmt=WPA-PSK\n}\n")

    # Keep all networks in the default priority group so the scan can rank them.
    conf = "ctrl_interface=/tmp/ipcam-sd-wifi\n" + "".join(networks)
    # Restrict permissions on newly created files.
    # SD filesystems without Unix permissions use the mount's access policy.
    os.umask(0o077)
    (args.directory / "wpa_supplicant.conf").write_text(conf)
    (args.directory / "lan-probe-ip").write_text("".join(f"{ip}\n" for ip in dict.fromkeys(args.probe_ip)))
    if args.lan_cidr is not None:
        (args.directory / "lan-cidr").write_text(f"{args.lan_cidr}\n")


if __name__ == "__main__":
    main()
