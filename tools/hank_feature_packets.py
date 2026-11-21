"""Build the INF time-setting packet used by the LAN server."""

import struct


def frame(command: int, body: bytes) -> bytes:
    return b"INF" + bytes([command]) + b"\x00\x01\x00\x01" + struct.pack(">I", len(body)) + body


def time_set(epoch: int) -> bytes:
    """Method 0: signed 32-bit Unix seconds at body offset 128."""
    if type(epoch) is not int or not 0 <= epoch <= 0x7FFFFFFF:
        raise ValueError("epoch must be a nonnegative signed 32-bit integer")
    body = bytearray(132)
    body[:16] = b"set".ljust(16, b"\0")
    body[16:80] = b"TimeInfo".ljust(64, b"\0")
    struct.pack_into("<I", body, 84, 0)
    struct.pack_into("<i", body, 128, epoch)
    return frame(100, body)
