"""Calibrate the pan and tilt reference for the HTTP server."""

import argparse
import ipaddress
from pathlib import Path

import camera_info
import hank_ptz
import ipcam_angles
import ipcam_config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ipcam_config.DEFAULT_CONFIG,
                        help="calibration JSON used by the server")
    parser.add_argument("command", choices=["calibrate"])
    parser.add_argument("--host", required=True, type=ipaddress.IPv4Address)
    args = parser.parse_args()
    connection = {"host": str(args.host), "port": 90, "timeout": 1.0}

    try:
        with ipcam_config.locked(args.config):
            data = ipcam_config.load(args.config)
            model = camera_info._read_file(str(args.host), 10081, 1.0, "/device_model.txt")
            if model.rstrip(b"\r\n") != b"IPC-G3B2D0-TY":
                raise ValueError("unexpected camera model")
            data["step_calibration"] = ipcam_angles.calibrate(data, connection)
            ipcam_config.save(args.config, data)
    except (OSError, ValueError, camera_info.CameraInfoError, hank_ptz.PTZError) as error:
        parser.exit(1, f"Calibration failed: {error}\n")
    print(f"Calibration saved to {args.config}")


if __name__ == "__main__":
    main()
