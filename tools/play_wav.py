#!/usr/bin/env python3
"""Play a local WAV on the camera through the ipcam-mcp /audio API."""

import argparse
from contextlib import closing
from http.client import HTTPConnection, HTTPException, HTTPSConnection
import json
from pathlib import Path
import subprocess
import sys
from urllib.parse import urlsplit

SAMPLE_RATE = 8000
MAX_SECONDS = 60
TIMEOUT = 20


def convert_wav(path):
    path = path.resolve(strict=True)
    if not path.is_file():
        raise ValueError("Expected a local WAV file")
    result = subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-xerror", "-f", "wav", "-i", str(path),
         "-map", "0:a:0", "-ac", "1", "-ar", str(SAMPLE_RATE), "-c:a", "pcm_s16le",
         "-t", str(MAX_SECONDS + 0.001), "-f", "s16le", "pipe:1"],
        capture_output=True,
    )
    if result.returncode:
        raise ValueError(result.stderr.decode(errors="replace").strip() or "WAV conversion failed")
    samples = len(result.stdout) // 2
    if not 1 <= samples <= MAX_SECONDS * SAMPLE_RATE:
        raise ValueError(f"WAV must contain audio and be at most {MAX_SECONDS} seconds")
    return result.stdout


def events(response):
    event = ""
    data = []
    while line := response.readline():
        line = line.decode("utf-8").rstrip("\r\n")
        if not line:
            if data:
                payload = json.loads("\n".join(data))
                if not isinstance(payload, dict):
                    raise ValueError("Invalid SSE payload from /audio")
                yield event, payload
            event, data = "", []
        elif line.startswith("event:"):
            event = line[6:].removeprefix(" ")
        elif line.startswith("data:"):
            data.append(line[5:].removeprefix(" "))
    raise ValueError("Audio SSE connection closed before playback ended")


def require_status(response, expected, operation):
    if response.status != expected:
        detail = response.read(1024).decode(errors="replace").strip()
        raise ValueError(f"{operation} failed (HTTP {response.status}): {detail or response.reason}")


def play_pcm(server, pcm):
    url = urlsplit(server)
    if (url.scheme not in {"http", "https"} or not url.hostname or url.username is not None
            or url.path not in {"", "/"} or url.query or url.fragment):
        raise ValueError("--server must be a base URL such as http://127.0.0.1:9928")
    connect = HTTPSConnection if url.scheme == "https" else HTTPConnection
    with closing(connect(url.hostname, url.port, timeout=TIMEOUT)) as sse:
        sse.request("GET", "/audio", headers={"Accept": "text/event-stream"})
        with sse.getresponse() as response:
            require_status(response, 200, "Opening the audio session")
            if response.headers.get_content_type() != "text/event-stream":
                raise ValueError("Expected an SSE stream from /audio")
            stream = events(response)
            event, data = next(stream)
            token = data.get("session")
            if event != "session" or not isinstance(token, str) or not token:
                raise ValueError("Audio server did not return a session token")
            with closing(connect(url.hostname, url.port, timeout=TIMEOUT)) as upload:
                upload.request("POST", "/audio", body=pcm, headers={
                    "Content-Type": "application/octet-stream",
                    "X-Audio-Session": token,
                    "X-Audio-Length": str(len(pcm) // 2),
                })
                with upload.getresponse() as accepted:
                    require_status(accepted, 202, "Uploading the audio")
                    reply = json.load(accepted)
                    playback_id = reply.get("id") if isinstance(reply, dict) else None
                    if type(playback_id) is not int:
                        raise ValueError("Audio server did not return a playback ID")
            for event, data in stream:
                if event != "end" or data.get("id") != playback_id:
                    continue
                reason = data.get("reason")
                if reason != "completed":
                    raise ValueError(f"Playback {playback_id} ended with reason {reason!r}")
                samples = data.get("length")
                if samples != len(pcm) // 2:
                    raise ValueError("Playback completion length does not match the upload")
                return playback_id, samples


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wav", type=Path, help="local WAV file, up to 60 seconds")
    parser.add_argument("--server", default="http://127.0.0.1:9928", help="ipcam-mcp base URL")
    args = parser.parse_args()
    try:
        playback_id, samples = play_pcm(args.server, convert_wav(args.wav))
    except KeyboardInterrupt:
        print("Playback interrupted", file=sys.stderr)
        return 130
    except (OSError, HTTPException, ValueError) as error:
        print(f"Audio playback failed: {error}", file=sys.stderr)
        return 1
    print(f"Playback {playback_id} completed ({samples} samples)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
