#!/usr/bin/env python3
"""Read three known public metadata files from the camera's HTTP service."""

from __future__ import annotations

import argparse
import http.client
import json
import math
import re
import sys


PATH_LIMITS = {"/Version.txt": 64, "/device_model.txt": 64, "/SysInfo": 320}
VERSION_OFFSET = 136
VERSION_FIELD_SIZE = 64


class CameraInfoError(Exception):
    """The camera did not return the expected bounded metadata response."""


def _read_file(host: str, port: int, timeout: float, path: str) -> bytes:
    limit = PATH_LIMITS[path]
    # HTTPConnection connects directly: environment proxy settings are not used.
    connection = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        connection.request("GET", path, headers={"Connection": "close"})
        response = connection.getresponse()
        # No redirects or authentication follow-ups are implemented.
        if response.status != 200:
            raise CameraInfoError(f"{path}: expected HTTP 200, received {response.status}")
        content_length = response.getheader("Content-Length")
        if content_length is not None:
            if not re.fullmatch(r"[0-9]+", content_length):
                raise CameraInfoError(f"{path}: invalid Content-Length")
            if len(content_length) > 6 or int(content_length) > limit:
                raise CameraInfoError(f"{path}: response exceeds {limit} bytes")
        body = response.read(limit + 1)
        if len(body) > limit:
            raise CameraInfoError(f"{path}: response exceeds {limit} bytes")
        if content_length is not None and len(body) != int(content_length):
            raise CameraInfoError(f"{path}: incomplete response")
        return body
    except (OSError, http.client.HTTPException) as exc:
        # Never propagate a peer-controlled status line or response body.
        raise CameraInfoError(f"{path}: HTTP transport failed ({type(exc).__name__})") from exc
    finally:
        connection.close()


def _printable_text(data: bytes, path: str) -> str:
    value = data.rstrip(b"\r\n")
    if not 1 <= len(value) <= 63:
        raise CameraInfoError(f"{path}: expected 1 to 63 printable ASCII bytes")
    if any(byte < 32 or byte > 126 for byte in value):
        raise CameraInfoError(f"{path}: metadata is not printable ASCII")
    return value.decode("ascii")


def _sysinfo_version(data: bytes) -> str:
    if len(data) != 320:
        raise CameraInfoError("/SysInfo: expected the known 320-byte structure")
    field = data[VERSION_OFFSET:VERSION_OFFSET + VERSION_FIELD_SIZE]
    value, separator, padding = field.partition(b"\x00")
    if not separator or any(padding):
        raise CameraInfoError("/SysInfo: version field must have a NUL terminator and zero padding")
    version = _printable_text(value, "/SysInfo version field")
    if not re.fullmatch(r"V[0-9]+(?:\.[0-9]+){1,3}[A-Za-z0-9_.-]*", version):
        raise CameraInfoError("/SysInfo: unrecognized version field at offset 136")
    return version


def get_camera_info(
    host: str, *, port: int = 10081, timeout: float = 5.0
) -> dict[str, str | int]:
    """Return validated metadata; never persist or return raw HTTP responses.

    Exactly the three paths in PATH_LIMITS are requested, once each. SysInfo
    support is restricted to the observed 320-byte layout and version field.
    The rest of that binary file is discarded without being decoded or returned.
    """
    if not host or any(character.isspace() for character in host) or any(
        character in host for character in "/?#@"
    ):
        raise ValueError("Host must be a hostname or IP address without a URL or credentials")
    if not 1 <= port <= 65535:
        raise ValueError("Port must be between 1 and 65535")
    if not math.isfinite(timeout) or not 0 < timeout <= 60:
        raise ValueError("Timeout must be greater than zero and at most 60 seconds")
    version = _printable_text(_read_file(host, port, timeout, "/Version.txt"), "/Version.txt")
    model = _printable_text(_read_file(host, port, timeout, "/device_model.txt"), "/device_model.txt")
    firmware_version = _sysinfo_version(_read_file(host, port, timeout, "/SysInfo"))
    return {"host": host, "port": port, "version": version,
            "model": model, "firmware_version": firmware_version}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="192.168.0.123")
    parser.add_argument("--port", type=int, default=10081)
    parser.add_argument("--timeout", type=float, default=5.0)
    args = parser.parse_args()
    try:
        result = get_camera_info(args.host, port=args.port, timeout=args.timeout)
    except (ValueError, CameraInfoError) as exc:
        print(f"Camera info query failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
