#!/usr/bin/env python3
"""Record the camera microphone from ipcam-mcp /mic into a local WAV."""

import argparse
import math
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit, urlunsplit
import wave

from websockets.exceptions import WebSocketException
from websockets.sync.client import connect

RATE = 8000
SAMPLE_BYTES = 2
SEQUENCE_BYTES = 4
MESSAGE_BYTES = SEQUENCE_BYTES + 320 * SAMPLE_BYTES
MAX_WAV_SAMPLES = (2**32 - 1 - 36) // SAMPLE_BYTES


def record(server, path, samples, *, denoise=False):
    url = urlsplit(server)
    if (url.scheme not in {"http", "https"} or not url.hostname or url.username is not None
            or url.path not in {"", "/"} or url.query or url.fragment):
        raise ValueError("--server must be a base URL such as http://127.0.0.1:9928")
    scheme = "wss" if url.scheme == "https" else "ws"
    uri = urlunsplit((scheme, url.netloc, "/mic", "denoise=1" if denoise else "", ""))
    origin = f"{url.scheme}://{url.netloc}"
    discontinuities = 0
    previous = None
    with connect(uri, origin=origin, compression=None, proxy=None, max_size=MESSAGE_BYTES) as socket:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as target, wave.open(target, "wb") as output:
            output.setparams((1, SAMPLE_BYTES, RATE, 0, "NONE", ""))
            remaining = samples
            while remaining:
                message = socket.recv(timeout=5)
                if not isinstance(message, bytes) or len(message) != MESSAGE_BYTES:
                    raise ValueError("Invalid microphone frame")
                sequence = int.from_bytes(message[:SEQUENCE_BYTES], "big")
                if previous is not None and sequence != (previous + 1) & 0xFFFFFFFF:
                    discontinuities += 1
                previous = sequence
                pcm = message[SEQUENCE_BYTES:SEQUENCE_BYTES + remaining * SAMPLE_BYTES]
                output.writeframesraw(pcm)
                remaining -= len(pcm) // SAMPLE_BYTES
    return discontinuities


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wav", type=Path, help="new local WAV file")
    parser.add_argument("--server", default="http://127.0.0.1:9928", help="ipcam-mcp base URL")
    parser.add_argument("--seconds", type=float, default=5, help="audio duration (default: 5)")
    parser.add_argument("--denoise", action=argparse.BooleanOptionalAction, default=False,
                        help="subscribe to noise-reduced audio (default: off)")
    args = parser.parse_args()
    if not math.isfinite(args.seconds) or not 1 / RATE <= args.seconds <= MAX_WAV_SAMPLES / RATE:
        parser.error("--seconds is outside the range of a PCM WAV file")
    samples = round(args.seconds * RATE)
    try:
        discontinuities = record(args.server, args.wav, samples, denoise=args.denoise)
    except KeyboardInterrupt:
        print("Microphone recording interrupted", file=sys.stderr)
        return 130
    except (OSError, WebSocketException, ValueError) as error:
        print(f"Microphone recording failed: {error}", file=sys.stderr)
        return 1
    print(f"Saved {samples / RATE:.3f} seconds to {args.wav}")
    if discontinuities:
        print(f"Sequence discontinuities: {discontinuities}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
