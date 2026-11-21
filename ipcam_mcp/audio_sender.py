"""Encode PCM and pace camera audio on a dedicated sender thread."""

import asyncio
from bisect import bisect_right
from collections import deque
import struct

from . import camera

RATE = 8000
FRAME_SAMPLES = camera.SPEAKER_FRAME_SAMPLES
FRAME_SECONDS = FRAME_SAMPLES / RATE


def decode_mulaw(code):
    value = code ^ 255
    magnitude = (((value & 15) << 3) + 132) << ((value >> 4) & 7)
    return 132 - magnitude if value & 128 else magnitude - 132


def make_mulaw_table():
    # Use the same quantization as the verified speaker client.
    thresholds = [(decode_mulaw(index ^ 255) + decode_mulaw((index + 1) ^ 255) + 4) >> 3
                  for index in range(127)]
    table = bytearray(16384)
    table[8192] = 255
    for magnitude in range(1, 8192):
        code = bisect_right(thresholds, magnitude)
        table[8192 + magnitude] = code ^ 255
        table[8192 - magnitude] = code ^ 127
    table[0] = table[1]
    return bytes(table)


MULAW = make_mulaw_table()


def encode_mulaw(pcm):
    return bytes(MULAW[(sample + 32768) >> 2] for (sample,) in struct.iter_unpack("<h", pcm))


async def wait_until(deadline):
    delay = deadline - asyncio.get_running_loop().time()
    if delay > 0:
        await asyncio.sleep(delay)


async def send_audio(host, payload, offset, indexes, started, progress):
    loop = asyncio.get_running_loop()
    indexes = deque(indexes)

    async def advance(origin, played_samples):
        end = min(offset + played_samples, len(payload))
        while indexes and indexes[0] <= end:
            index = indexes.popleft()
            await wait_until(origin + (index - offset) / RATE)
            progress(index, True)
        await wait_until(origin + played_samples / RATE)
        progress(end, False)

    async with camera.speaker(host) as writer:
        for sequence, start in enumerate(range(offset, len(payload), FRAME_SAMPLES), 1):
            # Send one frame ahead of the estimated playback position.
            if sequence > 1:
                played_samples = (sequence - 2) * FRAME_SAMPLES
                await advance(origin, played_samples)
            frame = payload[start:start + FRAME_SAMPLES].ljust(FRAME_SAMPLES, b"\xff")
            await camera.send_speaker_frame(writer, frame, sequence)
            sent_at = loop.time()
            if sequence == 1:
                origin = sent_at
                started()
            else:
                delay = sent_at - (origin + played_samples / RATE)
                if delay >= FRAME_SECONDS:
                    # Restart the clock instead of catching up with a burst.
                    origin += delay
        await advance(origin, sequence * FRAME_SAMPLES)


async def send_in_worker(executor, host, payload, offset, indexes, started, progress):
    owner = asyncio.get_running_loop()
    ready = owner.create_future()

    async def send():
        owner.call_soon_threadsafe(ready.set_result, (asyncio.get_running_loop(), asyncio.current_task()))
        await send_audio(host, payload, offset, indexes,
                         lambda: owner.call_soon_threadsafe(started),
                         lambda position, reached: owner.call_soon_threadsafe(progress, position, reached))

    result = owner.run_in_executor(executor, asyncio.run, send())
    try:
        await asyncio.shield(result)
    except asyncio.CancelledError:
        await asyncio.wait((ready, result), return_when=asyncio.FIRST_COMPLETED)
        if not result.done():
            worker_loop, task = ready.result()
            # The thread may have finished between checking the result and cancelling.
            if not worker_loop.is_closed():
                try:
                    worker_loop.call_soon_threadsafe(task.cancel)
                except RuntimeError:
                    pass
        await asyncio.gather(result, return_exceptions=True)
        raise
