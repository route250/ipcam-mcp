"""Create the camera SD card's local Wi-Fi settings."""

import argparse
import getpass
import hashlib
import os
from ipaddress import IPv4Address
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, help="mounted SD card root")
    parser.add_argument("--ssid", required=True)
    parser.add_argument("--probe-ip", type=IPv4Address, required=True,
                        help="same-LAN address that answers ICMP echo")
    args = parser.parse_args()

    ssid = args.ssid.encode()
    if not 1 <= len(ssid) <= 32:
        parser.error("SSID must be 1 to 32 UTF-8 bytes")
    passphrase = getpass.getpass("Wi-Fi passphrase: ").encode()
    if not 8 <= len(passphrase) <= 63:
        parser.error("Wi-Fi passphrase must be 8 to 63 bytes")
    psk = hashlib.pbkdf2_hmac("sha1", passphrase, ssid, 4096, 32).hex()
    conf = ("ctrl_interface=/tmp/ipcam-sd-wifi\nnetwork={\n"
            f"    ssid={ssid.hex()}\n    psk={psk}\n    key_mgmt=WPA-PSK\n"
            "}\n")
    # Restrict permissions on newly created files.
    # SD filesystems without Unix permissions use the mount's access policy.
    os.umask(0o077)
    (args.directory / "wpa_supplicant.conf").write_text(conf)
    (args.directory / "lan-probe-ip").write_text(f"{args.probe_ip}\n")


if __name__ == "__main__":
    main()
