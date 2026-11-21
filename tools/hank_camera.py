#!/usr/bin/env python3
"""Capture a finite HEVC stream from the verified camera protocol on TCP 90."""

from __future__ import annotations

import argparse
import json
import io
import math
import os
from pathlib import Path
import socket
import struct
import sys
import time
from typing import BinaryIO


# These exact messages were verified against this camera. PACKET1 advertises
# 128 bytes but actually carries 120; preserve the verified wire message.
PACKET1 = bytes.fromhex("494e46010001000100000080") + b"admin" + bytes(115)
PACKET2 = bytes.fromhex("494e4603000100010000001804030400") + bytes(20)
MAX_BODY_BYTES = 4 * 1024 * 1024
METADATA_BYTES = 24


class CaptureError(Exception):
    """An unexpected camera response or interrupted transport."""


class CaptureTransportError(CaptureError):
    """A timeout or disconnected peer, distinguishable from invalid frames."""


class _DeadlineReached(Exception):
    pass


def _write_all(destination: BinaryIO, payload: bytes) -> None:
    remaining = memoryview(payload)
    while remaining:
        count = destination.write(remaining)
        if not isinstance(count, int) or not 0 < count <= len(remaining):
            raise CaptureError("Destination did not accept video bytes")
        remaining = remaining[count:]


def _receive_exact(sock: socket.socket, size: int, deadline: float, timeout: float) -> bytes:
    data = bytearray()
    while len(data) < size:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _DeadlineReached
        sock.settimeout(min(timeout, remaining))
        try:
            chunk = sock.recv(size - len(data))
        except socket.timeout as exc:
            if time.monotonic() >= deadline:
                raise _DeadlineReached from exc
            raise CaptureTransportError("Camera receive timeout") from exc
        if not chunk:
            raise CaptureTransportError("Camera closed the connection during capture")
        data.extend(chunk)
    return bytes(data)


def _receive_frame(sock: socket.socket, deadline: float, timeout: float) -> tuple[int, bytes]:
    header = _receive_exact(sock, 12, deadline, timeout)
    if header[:3] != b"INF" or header[4:8] != b"\x00\x01\x00\x01":
        raise CaptureError("Unexpected camera frame header")
    length = int.from_bytes(header[8:12], "big")
    if length > MAX_BODY_BYTES:
        raise CaptureError(f"Camera frame exceeds {MAX_BODY_BYTES} bytes")
    return header[3], _receive_exact(sock, length, deadline, timeout)


def _capture_into(
    host, destination, *, seconds, port, timeout, max_bytes
) -> dict[str, str | int | float]:
    if not math.isfinite(seconds) or not 0 < seconds <= 300:
        raise ValueError("Seconds must be greater than zero and at most 300")
    if not math.isfinite(timeout) or not 0 < timeout <= 60:
        raise ValueError("Timeout must be greater than zero and at most 60")
    if not 1 <= port <= 65535:
        raise ValueError("Port must be between 1 and 65535")
    if not isinstance(max_bytes, int) or not 0 < max_bytes <= 64 * 1024 * 1024:
        raise ValueError("Output limit must be between 1 byte and 64 MiB")
    frames = 0
    video_bytes = 0
    audio_packets = 0
    stop_reason = "duration"
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        sock.sendall(PACKET1)
        try:
            command, body = _receive_frame(sock, time.monotonic() + timeout, timeout)
        except _DeadlineReached as exc:
            raise CaptureError("Camera authentication timed out") from exc
        if command != 2 or len(body) != 8:
            raise CaptureError("Unexpected camera login response format")
        session_id, login_result, login_reserved = struct.unpack(">IHH", body)
        if session_id == 0 or login_result != 0 or login_reserved != 0:
            raise CaptureError(
                f"Camera login failed (session {session_id}, result {login_result}, "
                f"reserved {login_reserved})"
            )
        sock.settimeout(timeout)
        sock.sendall(PACKET2)
        deadline = time.monotonic() + seconds
        while True:
            try:
                command, body = _receive_frame(sock, deadline, timeout)
            except _DeadlineReached:
                break
            if command not in (4, 6):
                raise CaptureError(f"Unexpected media command {command}")
            if len(body) < METADATA_BYTES:
                raise CaptureError("Media frame is shorter than its metadata")
            payload_size = int.from_bytes(body[12:16], "big")
            if payload_size != len(body) - METADATA_BYTES:
                raise CaptureError("Media payload length does not match frame length")
            if command == 6:
                payload = body[METADATA_BYTES:]
                if not payload.startswith((b"\x00\x00\x00\x01", b"\x00\x00\x01")):
                    raise CaptureError("Video payload lacks an Annex B start code")
                if video_bytes + len(payload) > max_bytes:
                    stop_reason = "size_limit"
                    break
                _write_all(destination, payload)
                frames += 1
                video_bytes += len(payload)
            else:
                audio_packets += 1
    if not frames:
        raise CaptureError("No complete video frame fit within the capture limits")
    return {
        "host": host,
        "port": port,
        "seconds": seconds,
        "video_frames": frames,
        "video_bytes": video_bytes,
        "audio_packets_discarded": audio_packets,
        "codec": "hevc",
        "stop_reason": stop_reason,
    }


def capture_bytes(
    host: str,
    *,
    seconds: float = 1.0,
    port: int = 90,
    timeout: float = 3.0,
    max_bytes: int = 8 * 1024 * 1024,
) -> bytes:
    """Return bounded HEVC Annex B bytes, suitable for ffmpeg stdin.

    Only complete video payloads are returned; audio is discarded. Authentication
    and connection each have a separate timeout budget before the media deadline.
    No reconnection, configuration change, or external program is performed.
    """
    destination = io.BytesIO()
    _capture_into(
        host, destination, seconds=seconds, port=port, timeout=timeout, max_bytes=max_bytes
    )
    return destination.getvalue()


def stream_video(
    host: str,
    destination: BinaryIO,
    *,
    seconds: float = 60.0,
    port: int = 90,
    timeout: float = 3.0,
    max_bytes: int = 64 * 1024 * 1024,
) -> dict[str, str | int | float]:
    """Write verified HEVC payloads incrementally to a binary destination.

    The caller owns the destination and must ensure its writes cannot block
    indefinitely. The camera socket is closed on errors, BrokenPipeError, and
    KeyboardInterrupt; the destination is neither flushed nor closed here.
    Limits and exceptions match capture_bytes; seconds must be in (0, 300].
    """
    return _capture_into(
        host, destination, seconds=seconds, port=port, timeout=timeout,
        max_bytes=max_bytes,
    )


def capture(
    host: str,
    output: str | os.PathLike[str],
    *,
    seconds: float = 3.0,
    port: int = 90,
    timeout: float = 3.0,
    max_bytes: int = 8 * 1024 * 1024,
) -> dict[str, str | int | float]:
    """Save a finite HEVC stream to a new private file (0600, O_EXCL).

    Never overwrites a file. Audio is counted and discarded. A frame cut by the
    deadline is discarded. On error any partial output stays at the requested
    path. Connection and authentication each have a separate timeout budget.
    """
    output_path = Path(output).absolute()
    descriptor = os.open(output_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as destination:
        result = _capture_into(
            host, destination, seconds=seconds, port=port, timeout=timeout,
            max_bytes=max_bytes,
        )
    result["output"] = str(output_path)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="192.168.0.123")
    parser.add_argument("--port", type=int, default=90)
    parser.add_argument("--seconds", type=float, default=3.0)
    parser.add_argument("--timeout", type=float, default=3.0)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        result = capture(
            args.host, args.output, seconds=args.seconds, port=args.port, timeout=args.timeout
        )
    except (OSError, ValueError, CaptureError) as exc:
        print(f"Capture failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
