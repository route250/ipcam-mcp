#!/usr/bin/env python3
"""Read the verified temperature/humidity getter on WTW-IPW308TTW/B TCP 90."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
import socket
import struct
import sys
import time

if __package__:
    from .hank_camera import PACKET1, CaptureError, _DeadlineReached, _receive_frame
else:
    from hank_camera import PACKET1, CaptureError, _DeadlineReached, _receive_frame


INFO_NAME = b"TemperatureHumidityInfo"
GET_BODY = b"get".ljust(16, b"\0") + INFO_NAME.ljust(64, b"\0") + bytes(4)
GET_PACKET = b"INF\x64\x00\x01\x00\x01" + struct.pack(">I", len(GET_BODY)) + GET_BODY


class EnvironmentReadError(Exception):
    """The response cannot be used as a confirmed environmental reading."""


def _decode(command: int, body: bytes) -> tuple[float, float]:
    if command != 101 or len(body) != 116:
        raise EnvironmentReadError("Expected command 101 with the verified 116-byte body")
    if body[:80] != GET_BODY[:80]:
        raise EnvironmentReadError("TemperatureHumidityInfo response name does not match")
    if body[80:84] != bytes(4) or body[92:] != bytes(24):
        raise EnvironmentReadError("Unexpected result or reserved fields in the response")
    temperature, humidity = struct.unpack_from("<ff", body, 84)
    if not math.isfinite(temperature) or not math.isfinite(humidity) or not 0 <= humidity <= 100:
        raise EnvironmentReadError("Non-finite temperature or invalid relative humidity")
    # The firmware returns zero-initialized floats even if I2C acquisition fails.
    # The same bytes could describe a genuine 0 C / 0 %RH sample.
    if temperature == 0 and humidity == 0:
        raise EnvironmentReadError("Ambiguous 0 C / 0 %RH response: sensor failure cannot be distinguished")
    return temperature, humidity


def read_environment(host: str = "192.168.0.123", *, port: int = 90, timeout: float = 3.0) -> dict:
    """Request one measurement without changing settings or opening I2C directly.

    Tested with V3.1.6-Tuya_230309. The firmware does not transmit sensor status
    or CRC validation; a successful response is not an accuracy guarantee.
    Temperature already includes the firmware's compensation. Do not add it again.
    """
    if not isinstance(host, str) or not host or any(c.isspace() or c in "/?#@" for c in host):
        raise ValueError("Host must be a hostname or IP address without a URL or credentials")
    if not 1 <= port <= 65535:
        raise ValueError("Port must be between 1 and 65535")
    if not math.isfinite(timeout) or not 0 < timeout <= 60:
        raise ValueError("Timeout must be greater than zero and at most 60 seconds")
    try:
        with socket.create_connection((host, port), timeout=timeout) as connection:
            connection.sendall(PACKET1)
            command, body = _receive_frame(connection, time.monotonic() + timeout, timeout)
            if command != 2 or len(body) != 8:
                raise EnvironmentReadError("Unexpected camera login response")
            session, result, reserved = struct.unpack(">IHH", body)
            if not session or result or reserved:
                raise EnvironmentReadError("Camera login was not accepted")
            connection.settimeout(timeout)
            connection.sendall(GET_PACKET)
            temperature, humidity = _decode(*_receive_frame(connection, time.monotonic() + timeout, timeout))
    except (OSError, CaptureError, _DeadlineReached) as error:
        raise EnvironmentReadError(f"Environment transport failed ({type(error).__name__})") from error
    return {"host": host, "port": port, "temperature_c": temperature,
            "relative_humidity_percent": humidity,
            "received_at_utc": datetime.now(timezone.utc).isoformat(),
            "sensor_status": "not_reported"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="192.168.0.123")
    parser.add_argument("--port", type=int, default=90)
    parser.add_argument("--timeout", type=float, default=3.0)
    args = parser.parse_args()
    try:
        print(json.dumps(read_environment(args.host, port=args.port, timeout=args.timeout), allow_nan=False))
    except (ValueError, EnvironmentReadError) as error:
        print(f"Environment query failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
