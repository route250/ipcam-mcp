"""Run a replaceable speech synthesizer and validate its output."""

import asyncio
import os
from pathlib import Path
import re
import signal
from tempfile import TemporaryDirectory
import wave

from .audio_codec import RATE

DEFAULT_COMMAND = Path(__file__).resolve().parent.parent / "tts.sh"
SENTENCE_END = r"[。！？!?]|\.{2,}|\.(?![A-Za-z0-9])"
SPEECH_BOUNDARY = re.compile(rf"(?:{SENTENCE_END}|\s)+")
FIRST_SPEECH_BOUNDARY = re.compile(rf"(?:{SENTENCE_END}|\s|、)+")
ENGLISH_SPACE_LEFT = re.compile(r"""[A-Za-z0-9,;:'")\]]""")
ENGLISH_SPACE_RIGHT = re.compile(r"""[A-Za-z0-9'"(\[]""")


def split_speech(text, split_first_comma):
    boundary = FIRST_SPEECH_BOUNDARY if split_first_comma else SPEECH_BOUNDARY
    start = position = 0
    while match := boundary.search(text, position):
        position = match.end()
        if (
            not match[0].strip(" ")
            and match.start() > start
            and ENGLISH_SPACE_LEFT.fullmatch(text[match.start() - 1])
            and ENGLISH_SPACE_RIGHT.match(text, position)
        ):
            continue
        punctuation = re.sub(r"[、\s]", "", match[0])
        section = text[start:match.start()] + punctuation
        start = position
        if section:
            yield section
            boundary = SPEECH_BOUNDARY
    if start < len(text):
        yield text[start:]


async def run_command(text, output):
    command = os.environ.get("IPCAM_TTS_COMMAND") or DEFAULT_COMMAND
    # Keep the spawn task alive if cancellation arrives before it returns the PID.
    launch = asyncio.create_task(asyncio.create_subprocess_exec(
        command, text, output, start_new_session=True,
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
    ))
    try:
        process = await asyncio.shield(launch)
        status = await process.wait()
    except asyncio.CancelledError:
        try:
            process = await launch
        except OSError:
            pass
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.wait()
        raise
    if status != 0:
        raise ValueError(f"TTS command exited with status {status}")


async def synthesize(text, max_samples):
    with TemporaryDirectory(prefix="ipcam-tts-") as directory:
        output = Path(directory) / "speech.wav"
        try:
            async with asyncio.timeout(60):
                await run_command(text, output)
        except TimeoutError:
            raise ValueError("TTS command timed out") from None
        try:
            with wave.open(str(output), "rb") as wav:
                if (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) != (RATE, 1, 2):
                    raise ValueError("Expected 8 kHz mono 16-bit PCM WAV")
                length = wav.getnframes()
                if not 1 <= length <= max_samples:
                    raise ValueError(f"TTS WAV must contain 1 to {max_samples} samples")
                pcm = wav.readframes(length)
                if len(pcm) != length * 2:
                    raise ValueError("TTS WAV is truncated")
                return pcm
        except (EOFError, wave.Error):
            raise ValueError("Invalid TTS WAV") from None
