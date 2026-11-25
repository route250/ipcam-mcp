"""Capture and fan out microphone audio to read-only WebSocket subscribers."""

import asyncio
from contextlib import suppress
import logging

from starlette.websockets import WebSocketDisconnect

from .audio_codec import decode_mulaw
from .noise_reduction import Denoiser

QUEUE_FRAMES = 5
USB_SAMPLE_RATE = 8000
PCM_FRAME_BYTES = 320 * 2
SEQUENCE_BYTES = 4
LOG = logging.getLogger(__name__)


def enqueue_latest(queue, frame):
    if queue.full():
        queue.get_nowait()
    queue.put_nowait(frame)


def close_subscribers(subscribers, code=1012):
    for queue in subscribers:
        while not queue.empty():
            queue.get_nowait()
        queue.put_nowait(code)
    subscribers.clear()


class Microphone:
    def __init__(self):
        self.subscribers = set()
        self.filtered_subscribers = set()
        self.sample_rate = None
        self.noise_profile = None
        self.filter_input = None
        self.filter_task = None

    def publish(self, sequence, payload, sample_rate):
        self.sample_rate = sample_rate
        if not self.subscribers and self.filter_input is None:
            return
        self.publish_pcm(sequence, decode_mulaw(payload), sample_rate)

    def publish_pcm(self, sequence, pcm, sample_rate):
        self.sample_rate = sample_rate
        frame = (sample_rate, sequence + pcm)
        for queue in self.subscribers:
            enqueue_latest(queue, frame)
        if self.filter_input is not None:
            enqueue_latest(self.filter_input, frame)

    async def run_usb(self, device):
        while True:
            try:
                recorder = await asyncio.create_subprocess_exec(
                    "arecord", "-q", "-D", device, "-t", "raw", "-f", "S16_LE", "-c", "1",
                    "-r", str(USB_SAMPLE_RATE), "--period-time=40000", "--buffer-time=160000",
                    stdout=asyncio.subprocess.PIPE, stdin=asyncio.subprocess.DEVNULL,
                )
                try:
                    sequence = 0
                    while True:
                        async with asyncio.timeout(2):
                            pcm = await recorder.stdout.readexactly(PCM_FRAME_BYTES)
                        self.publish_pcm(sequence.to_bytes(4, "big"), pcm, USB_SAMPLE_RATE)
                        sequence = (sequence + 1) & 0xFFFFFFFF
                finally:
                    with suppress(ProcessLookupError):
                        recorder.kill()
                    await recorder.communicate()
            except (OSError, EOFError, TimeoutError) as error:
                LOG.warning("USB microphone %s unavailable (%s), retrying", device, type(error).__name__)
            finally:
                self.reset()
            await asyncio.sleep(2)

    async def filter_frames(self, source):
        try:
            profile_rate, noise_power = self.noise_profile
            denoiser = Denoiser(noise_power)
            while True:
                sample_rate, frame = await source.get()
                if sample_rate != profile_rate:
                    close_subscribers(self.filtered_subscribers, 1008)
                    self.filter_input = self.filter_task = None
                    return
                for filtered in await asyncio.to_thread(denoiser.feed, frame):
                    for queue in self.filtered_subscribers:
                        enqueue_latest(queue, (sample_rate, filtered))
        except Exception:
            LOG.exception("Microphone noise reduction failed")
            close_subscribers(self.filtered_subscribers)
            self.filter_input = self.filter_task = None

    def stop_filter(self):
        if self.filter_task is not None:
            self.filter_task.cancel()
            self.filter_input = self.filter_task = None

    def reset(self):
        self.sample_rate = None
        self.stop_filter()
        close_subscribers(self.subscribers)
        close_subscribers(self.filtered_subscribers)

    async def subscribe(self, websocket, *, denoise=False, metadata=False):
        await websocket.accept()
        if denoise and self.noise_profile is None:
            await websocket.close(code=1008, reason="Microphone noise profile is not configured")
            return
        queue = asyncio.Queue(maxsize=QUEUE_FRAMES)
        subscribers = self.filtered_subscribers if denoise else self.subscribers
        subscribers.add(queue)
        if denoise and self.filter_task is None:
            self.filter_input = asyncio.Queue(maxsize=QUEUE_FRAMES)
            self.filter_task = asyncio.create_task(self.filter_frames(self.filter_input))

        async def send():
            active_rate = None
            pending = None
            try:
                while True:
                    item = await queue.get()
                    async with asyncio.timeout(2):
                        if isinstance(item, int):
                            reason = "Noise profile sample rate does not match microphone" if item == 1008 else ""
                            await websocket.close(code=item, reason=reason)
                            return
                        sample_rate, frame = item
                        if active_rate is None:
                            if metadata:
                                await websocket.send_json({"type": "format", "sample_rate": sample_rate,
                                                           "frame_samples": sample_rate * 40 // 1000})
                            elif sample_rate != 8000:
                                await websocket.close(code=1008, reason="Use metadata=1 for 16 kHz microphone audio")
                                return
                            active_rate = sample_rate
                        elif active_rate != sample_rate:
                            await websocket.close(code=1012, reason="Microphone sample rate changed")
                            return
                        if sample_rate == 16000:
                            current = int.from_bytes(frame[:SEQUENCE_BYTES], "big")
                            if pending is None or current != ((pending[0] + 1) & 0xFFFFFFFF):
                                pending = current, frame
                                continue
                            frame = pending[1] + frame[SEQUENCE_BYTES:]
                            pending = None
                        await websocket.send_bytes(frame)
            except TimeoutError:
                await websocket.close(code=1013)

        async def receive():
            while (await websocket.receive())["type"] != "websocket.disconnect":
                pass

        tasks = [asyncio.create_task(send()), asyncio.create_task(receive())]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except (OSError, WebSocketDisconnect):
            pass
        finally:
            subscribers.discard(queue)
            if denoise and not subscribers:
                self.stop_filter()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
