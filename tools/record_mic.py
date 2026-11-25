#!/usr/bin/env python3
"""Record the microphone selected by ipcam-mcp into a local WAV."""

import argparse
import json
import math
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit, urlunsplit
import wave

from websockets.exceptions import WebSocketException
from websockets.sync.client import connect

SAMPLE_RATES = (8000, 16000)
SAMPLE_BYTES = 2
SEQUENCE_BYTES = 4
SOURCE_FRAME_SAMPLES = 320
MAX_MESSAGE_BYTES = SEQUENCE_BYTES + 640 * SAMPLE_BYTES
MAX_WAV_SAMPLES = (2**32 - 1 - 36) // SAMPLE_BYTES


def record(server, path, seconds, *, denoise=False):
    url = urlsplit(server)
    if (url.scheme not in {"http", "https"} or not url.hostname or url.username is not None
            or url.path not in {"", "/"} or url.query or url.fragment):
        raise ValueError("--server must be a base URL such as http://127.0.0.1:9928")
    scheme = "wss" if url.scheme == "https" else "ws"
    uri = urlunsplit((scheme, url.netloc, "/mic", "metadata=1&denoise=1" if denoise else "metadata=1", ""))
    origin = f"{url.scheme}://{url.netloc}"
    discontinuities = 0
    previous = None
    with connect(uri, origin=origin, compression=None, proxy=None, max_size=MAX_MESSAGE_BYTES) as socket:
        message = socket.recv(timeout=5)
        sample_rate = 8000  # Servers preceding metadata=1 send the first PCM frame immediately.
        frame_samples = SOURCE_FRAME_SAMPLES
        if isinstance(message, str):
            metadata = json.loads(message)
            if (not isinstance(metadata, dict) or metadata.get("type") != "format"
                    or metadata.get("sample_rate") not in SAMPLE_RATES):
                raise ValueError("Invalid microphone format")
            sample_rate = int(metadata["sample_rate"])
            frame_samples = metadata.get("frame_samples", SOURCE_FRAME_SAMPLES)
            if type(frame_samples) is not int or frame_samples not in (SOURCE_FRAME_SAMPLES, sample_rate * 40 // 1000):
                raise ValueError("Invalid microphone frame sample count")
            message = socket.recv(timeout=5)
        message_bytes = SEQUENCE_BYTES + frame_samples * SAMPLE_BYTES
        sequence_step = frame_samples // SOURCE_FRAME_SAMPLES
        if not math.isfinite(seconds) or not 1 / sample_rate <= seconds <= MAX_WAV_SAMPLES / sample_rate:
            raise ValueError("Duration is outside the range of a PCM WAV file")
        samples = round(seconds * sample_rate)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as target, wave.open(target, "wb") as output:
            output.setparams((1, SAMPLE_BYTES, sample_rate, 0, "NONE", ""))
            remaining = samples
            while remaining:
                if not isinstance(message, bytes) or len(message) != message_bytes:
                    raise ValueError("Invalid microphone frame")
                sequence = int.from_bytes(message[:SEQUENCE_BYTES], "big")
                if previous is not None and sequence != (previous + sequence_step) & 0xFFFFFFFF:
                    discontinuities += 1
                previous = sequence
                pcm = message[SEQUENCE_BYTES:SEQUENCE_BYTES + remaining * SAMPLE_BYTES]
                output.writeframesraw(pcm)
                remaining -= len(pcm) // SAMPLE_BYTES
                if remaining:
                    message = socket.recv(timeout=5)
    return samples, sample_rate, discontinuities


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wav", type=Path, help="new local WAV file")
    parser.add_argument("--server", default="http://127.0.0.1:9928", help="ipcam-mcp base URL")
    parser.add_argument("--seconds", type=float, default=5, help="audio duration (default: 5)")
    parser.add_argument("--denoise", action=argparse.BooleanOptionalAction, default=False,
                        help="subscribe to noise-reduced audio (default: off)")
    args = parser.parse_args()
    try:
        samples, sample_rate, discontinuities = record(args.server, args.wav, args.seconds, denoise=args.denoise)
    except KeyboardInterrupt:
        print("Microphone recording interrupted", file=sys.stderr)
        return 130
    except (OSError, TimeoutError, WebSocketException, ValueError) as error:
        print(f"Microphone recording failed: {error}", file=sys.stderr)
        return 1
    print(f"Saved {samples / sample_rate:.3f} seconds at {sample_rate} Hz to {args.wav}")
    if discontinuities:
        print(f"Sequence discontinuities: {discontinuities}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
