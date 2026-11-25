#!/usr/bin/env python3
"""Bounded PTZ requests using one authenticated connection per request.

ACKs indicate protocol acceptance; they do not prove physical motor operation.
"""

from __future__ import annotations

import argparse
from contextlib import ExitStack, contextmanager, suppress
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


STOP_SETUP_LEAD_SECONDS = 1.0


class PTZError(Exception):
    """A PTZ request or its acknowledgement could not be confirmed."""


def _packet(direction: str, speed: int = 1) -> bytes:
    # Set both axis speeds; auxiliary/channel/reserved values are zero.
    body = bytes((ord(direction), speed, speed, 0, 0, 0, 0, 0))
    return bytes.fromhex("494e460f0001000100000008") + body


def _remaining_timeout(timeout, deadline):
    if deadline is None:
        return timeout
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _DeadlineReached
    return min(timeout, remaining)


@contextmanager
def _authenticated_connection(host, port, timeout, *, deadline=None):
    connect_timeout = _remaining_timeout(timeout, deadline)
    with socket.create_connection((host, port), timeout=connect_timeout) as sock:
        sock.settimeout(_remaining_timeout(timeout, deadline))
        sock.sendall(PACKET1)
        now = time.monotonic()
        login_deadline = now + timeout if deadline is None else min(now + timeout, deadline)
        command, body = _receive_frame(sock, login_deadline, timeout)
        if command != 2 or len(body) != 8:
            raise PTZError("Unexpected camera login response")
        session, result, reserved = struct.unpack(">IHH", body)
        if not session or result or reserved:
            raise PTZError("Camera login was not accepted")
        yield sock


def _send_request(sock, direction, wait, *, before_send=None, speed=1):
    sock.settimeout(wait)
    sent_at = time.monotonic()
    if before_send is not None:
        before_send(sent_at)
    sock.sendall(_packet(direction, speed))
    _require_ack(sock, sent_at + wait, wait)


def _require_ack(sock, deadline, timeout):
    command, body = _receive_frame(sock, deadline, timeout)
    if command != 16 or body != bytes(4):
        raise PTZError("Unexpected PTZ acknowledgement")


def _request(host, direction, *, port, timeout, duration=None, before_send=None, speed=1):
    """Authenticate, send one PTZ request, read its ACK, and close."""
    try:
        with _authenticated_connection(host, port, timeout) as sock:
            wait = timeout if duration is None else min(timeout, duration)
            _send_request(sock, direction, wait, before_send=before_send, speed=speed)
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


def _wait_until(deadline, cancelled):
    while not cancelled():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(.02, remaining))
    raise PTZError('movement interrupted by stop')


def pulse(direction, *, host='192.168.0.123', port=90, timeout=1.0,
          duration=0.1, cancelled=lambda: False):
    """Prepare the stop connection up to one second before sending H."""
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
        with _authenticated_connection(host, port, timeout) as move_sock, ExitStack() as stop_connection:
            stop_sock = None
            if duration <= STOP_SETUP_LEAD_SECONDS:
                stop_sock = stop_connection.enter_context(_authenticated_connection(host, port, timeout))
            if cancelled():
                raise PTZError('movement interrupted by stop')
            move_sent_at = time.monotonic()
            deadline = move_sent_at + duration
            try:
                move_sock.sendall(_packet(direction, 1))
                if stop_sock is None:
                    _wait_until(deadline - STOP_SETUP_LEAD_SECONDS, cancelled)
                    with suppress(OSError, CaptureError, _DeadlineReached, PTZError):
                        stop_sock = stop_connection.enter_context(
                            _authenticated_connection(host, port, timeout, deadline=deadline))
                _wait_until(deadline, cancelled)
            finally:
                stop_started_at = time.monotonic()
                try:
                    if stop_sock is None:
                        stop(host, port=port, timeout=timeout, before_send=record_stop)
                    else:
                        _send_request(stop_sock, 'H', timeout, before_send=record_stop)
                except (OSError, CaptureError, _DeadlineReached, PTZError):
                    with suppress(OSError):
                        stop_connection.close()
                    stop(host, port=port, timeout=timeout, before_send=record_stop)
                stop_ack_at = time.monotonic()
            _require_ack(move_sock, time.monotonic() + timeout, timeout)
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
