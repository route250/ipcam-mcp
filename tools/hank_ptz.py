#!/usr/bin/env python3
"""Bounded PTZ requests using one authenticated connection per request.

ACKs indicate protocol acceptance; they do not prove physical motor operation.
"""

from __future__ import annotations

import argparse
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


class PTZError(Exception):
    """A PTZ request or its acknowledgement could not be confirmed."""


def _packet(direction: str, speed: int = 1) -> bytes:
    # Set both axis speeds; auxiliary/channel/reserved values are zero.
    body = bytes((ord(direction), speed, speed, 0, 0, 0, 0, 0))
    return bytes.fromhex("494e460f0001000100000008") + body


def _request(host, direction, *, port, timeout, duration=None, before_send=None, speed=1):
    """Authenticate, send one PTZ request, read its ACK, and close."""
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(PACKET1)
            command, body = _receive_frame(sock, time.monotonic() + timeout, timeout)
            if command != 2 or len(body) != 8:
                raise PTZError("Unexpected camera login response")
            session, result, reserved = struct.unpack(">IHH", body)
            if not session or result or reserved:
                raise PTZError("Camera login was not accepted")
            wait = timeout if duration is None else min(timeout, duration)
            sent_at = time.monotonic()
            if before_send is not None:
                before_send(sent_at)
            sock.settimeout(wait)
            sock.sendall(_packet(direction, speed))
            command, body = _receive_frame(sock, sent_at + wait, wait)
            if command != 16 or body != bytes(4):
                raise PTZError("Unexpected PTZ acknowledgement")
            return {"direction": direction, "acknowledged": True}
    except (OSError, CaptureError, _DeadlineReached) as exc:
        raise PTZError(f"PTZ transport failed: {type(exc).__name__}") from exc


def _validate(host, port, timeout):
    if not isinstance(host, str) or not host:
        raise ValueError("Host must not be empty")
    if not 1 <= port <= 65535:
        raise ValueError("Port must be between 1 and 65535")
    if not math.isfinite(timeout) or not 0 < timeout <= 3:
        raise ValueError("Timeout must be greater than zero and at most 3 seconds")


def stop(host: str = "192.168.0.123", *, port: int = 90, timeout: float = 1.0, before_send=None) -> dict:
    """Send H (stop) on a fresh connection and require its protocol ACK."""
    _validate(host, port, timeout)
    return _request(host, "H", port=port, timeout=timeout, before_send=before_send)


def move(
    direction: str,
    *,
    host: str = "192.168.0.123",
    duration: float = 0.3,
    speed: int = 1,
    port: int = 90,
    timeout: float = 1.0,
) -> dict:
    """Request U/D/L/R, then stop using a separate authenticated connection.

    Duration (0, 10.0] bounds the wait before starting the stop connection.
    Stop authentication and transport add latency. Network or power failures
    prevent a guarantee of physical stopping. Speeds 1 and 2 are supported.
    A failed final stop is retried once on another fresh connection.
    """
    _validate(host, port, timeout)
    direction = direction.upper()
    if direction not in ("U", "D", "L", "R"):
        raise ValueError("Direction must be U, D, L, or R")
    if not math.isfinite(duration) or not 0 < duration <= 10.0:
        raise ValueError("Duration must be greater than zero and at most 10.0 seconds")
    if type(speed) is not int or speed not in (1, 2):
        raise ValueError("Speed must be 1 or 2")
    # Stop-first verifies the independent stop request before any movement.
    stop(host, port=port, timeout=timeout)
    sent_at = None
    motion_error = None
    stop_attempts = 1

    def remember_start(value):
        nonlocal sent_at
        sent_at = value

    try:
        _request(
            host, direction, port=port, timeout=timeout, duration=duration,
            before_send=remember_start, speed=speed,
        )
        remaining = sent_at + duration - time.monotonic()
        if remaining > 0:
            time.sleep(remaining)
    except BaseException as exc:
        motion_error = exc
    finally:
        stop_started_at = time.monotonic()
        try:
            stop(host, port=port, timeout=timeout)
        except BaseException:
            stop_attempts = 2
            try:
                stop(host, port=port, timeout=timeout)
            except BaseException as exc:
                raise PTZError("Stop could not be confirmed after two fresh connections") from exc
    if motion_error is not None:
        raise motion_error
    return {
        "direction": direction,
        "duration": duration,
        "speed": speed,
        "acknowledged": True,
        "stop_acknowledged": True,
        "stop_attempts": stop_attempts,
        "stop_connection_started_after_seconds": stop_started_at - sent_at,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["stop", "U", "D", "L", "R"])
    parser.add_argument("--host", default="192.168.0.123")
    parser.add_argument("--port", type=int, default=90)
    parser.add_argument("--timeout", type=float, default=1.0)
    parser.add_argument("--duration", type=float, default=0.3)
    parser.add_argument("--speed", type=int, choices=(1, 2), default=1)
    args = parser.parse_args()
    try:
        if args.action == "stop":
            result = stop(args.host, port=args.port, timeout=args.timeout)
        else:
            result = move(args.action, host=args.host, duration=args.duration,
                          port=args.port, timeout=args.timeout, speed=args.speed)
    except (PTZError, ValueError) as exc:
        print(f"PTZ request failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result))
    return 0


def pulse(direction, *, host='192.168.0.123', port=90, timeout=1.0,
          duration=0.1, cancelled=lambda: False):
    """Stop at the pulse deadline before waiting for the movement ACK.

    Short pulses must not use the pulse duration as their ACK timeout.
    This is the independently verified HTTP-feedback transport sequence.
    """
    _validate(host, port, timeout)
    if direction not in ('U', 'D', 'L', 'R') or not math.isfinite(duration) or not 0 < duration <= 10:
        raise ValueError('pulse must be U/D/L/R and duration in (0, 10]')
    if cancelled():
        raise PTZError('movement interrupted by stop')
    stop(host, port=port, timeout=timeout)
    stop_sent_at = []

    def record_stop(sent_at):
        stop_sent_at.append(sent_at)

    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(PACKET1)
            command, body = _receive_frame(sock, time.monotonic()+timeout, timeout)
            if command != 2 or len(body) != 8:
                raise PTZError('Unexpected camera login response')
            session, status, reserved = struct.unpack('>IHH', body)
            if not session or status or reserved:
                raise PTZError('Camera login was not accepted')
            try:
                if cancelled():
                    raise PTZError('movement interrupted by stop')
                move_sent_at = time.monotonic()
                sock.sendall(_packet(direction, 1))
                deadline = time.monotonic() + duration
                while time.monotonic() < deadline:
                    if cancelled():
                        raise PTZError('movement interrupted by stop')
                    time.sleep(min(.02, max(0, deadline-time.monotonic())))
            finally:
                stop_started_at = time.monotonic()
                try:
                    stop(host, port=port, timeout=timeout, before_send=record_stop)
                except Exception:
                    stop(host, port=port, timeout=timeout, before_send=record_stop)
                stop_ack_at = time.monotonic()
            command, body = _receive_frame(sock, time.monotonic()+timeout, timeout)
            if command != 16 or body != bytes(4):
                raise PTZError('Unexpected PTZ acknowledgement')
    except (OSError, CaptureError, _DeadlineReached) as exc:
        raise PTZError(f'PTZ pulse transport failed: {type(exc).__name__}') from exc
    return {'direction': direction, 'duration': duration, 'acknowledged': True,
            'stop_acknowledged': True,
            'wait_seconds': stop_started_at - move_sent_at,
            'stop_setup_seconds': stop_sent_at[0] - stop_started_at,
            'send_interval_seconds': stop_sent_at[0] - move_sent_at,
            'stop_ack_seconds': stop_ack_at - move_sent_at,
            'stop_sends': len(stop_sent_at)}


if __name__ == "__main__":
    raise SystemExit(main())
