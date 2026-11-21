"""Run the local IP camera server."""

import argparse
import logging
from pathlib import Path
import shutil
import ipaddress
import re
import wave

import uvicorn

from .app import create_app
from .noise_reduction import load_noise_profile
from .state import Settings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--host", default="127.0.0.1", help="listen address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8000, help="listen port (default: 8000)")
    parser.add_argument("--allowed-host", action="append", default=[], metavar="NAME",
                        help="additional HTTP hostname or IPv4 address; repeat for aliases")
    parser.add_argument("--debug", action="store_true", help="log camera retries and PTZ feedback")
    parser.add_argument("--mic-noise-profile", type=Path,
                        help="noise WAV (default: mic-noise-profile.wav beside the configuration if present)")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be in 1..65535")
    try:
        ipaddress.IPv4Address(args.host)
    except ValueError:
        parser.error("--host must be an IPv4 listen address")
    if any(not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", host)
           for host in args.allowed_host):
        parser.error("--allowed-host must be a hostname or IPv4 address without a port")
    names = {"127.0.0.1", "localhost", *args.allowed_host}
    if args.host != "0.0.0.0":
        names.add(args.host)
    allowed_hosts = {f"{name.lower()}:{args.port}" for name in names}
    try:
        settings = Settings.load(args.config)
    except (OSError, ValueError, TypeError, KeyError) as error:
        parser.error(f"Invalid server configuration: {error}")
    profile = None
    profile_path = args.mic_noise_profile or args.config.with_name("mic-noise-profile.wav")
    if args.mic_noise_profile is not None or profile_path.exists():
        try:
            profile = load_noise_profile(profile_path)
        except (OSError, ValueError, EOFError, wave.Error) as error:
            parser.error(f"Invalid microphone noise profile: {error}")
    if shutil.which("ffmpeg") is None:
        parser.error("ffmpeg is required")
    logging.basicConfig(level=logging.INFO)
    if args.debug:
        logging.getLogger("ipcam_mcp").setLevel(logging.DEBUG)
    uvicorn.run(create_app(settings, allowed_hosts=allowed_hosts, mic_noise_profile=profile),
                host=args.host, port=args.port, workers=1, access_log=False,
                proxy_headers=False, ws_max_size=1024, timeout_graceful_shutdown=5)


if __name__ == "__main__":
    main()
