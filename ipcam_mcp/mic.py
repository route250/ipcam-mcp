"""Fan out camera microphone frames to read-only WebSocket subscribers."""

import asyncio
import logging

from starlette.websockets import WebSocketDisconnect

from .audio_codec import decode_mulaw
from .noise_reduction import Denoiser

QUEUE_FRAMES = 5
LOG = logging.getLogger(__name__)


def enqueue_latest(queue, frame):
    if queue.full():
        queue.get_nowait()
    queue.put_nowait(frame)


def close_subscribers(subscribers):
    for queue in subscribers:
        while not queue.empty():
            queue.get_nowait()
        queue.put_nowait(None)
    subscribers.clear()


class Microphone:
    def __init__(self):
        self.subscribers = set()
        self.filtered_subscribers = set()
        self.noise_profile = None
        self.filter_input = None
        self.filter_task = None

    def publish(self, sequence, payload):
        if not self.subscribers and self.filter_input is None:
            return
        frame = sequence + decode_mulaw(payload)
        for queue in self.subscribers:
            enqueue_latest(queue, frame)
        if self.filter_input is not None:
            enqueue_latest(self.filter_input, frame)

    async def filter_frames(self, source):
        try:
            denoiser = Denoiser(self.noise_profile)
            while True:
                frame = await source.get()
                for filtered in await asyncio.to_thread(denoiser.feed, frame):
                    for queue in self.filtered_subscribers:
                        enqueue_latest(queue, filtered)
        except Exception:
            LOG.exception("Microphone noise reduction failed")
            close_subscribers(self.filtered_subscribers)
            self.filter_input = self.filter_task = None

    def stop_filter(self):
        if self.filter_task is not None:
            self.filter_task.cancel()
            self.filter_input = self.filter_task = None

    def reset(self):
        self.stop_filter()
        close_subscribers(self.subscribers)
        close_subscribers(self.filtered_subscribers)

    async def subscribe(self, websocket, *, denoise=False):
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
            try:
                while (frame := await queue.get()) is not None:
                    async with asyncio.timeout(2):
                        await websocket.send_bytes(frame)
            except TimeoutError:
                await websocket.close(code=1013)
            else:
                await websocket.close(code=1012)

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
