"""Session-based PCM playback with server-timed SSE notifications."""

import asyncio
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import json
import logging
import re
import secrets

from fastapi import HTTPException, Request
from starlette.requests import ClientDisconnect
from starlette.responses import StreamingResponse

from . import camera
from .audio_codec import RATE, encode_mulaw
from .audio_sender import send_in_worker

MAX_SAMPLES = 60 * RATE
MAX_JOBS = 32
MAX_INDEXES = 128
AUDIO_ERRORS = (OSError, EOFError, TimeoutError, camera.CameraError)
LOG = logging.getLogger(__name__)


@dataclass
class Job:
    id: int
    payload: bytes
    indexes: deque[int]
    offset: int = 0
    started: bool = False

    @property
    def length(self):
        return len(self.payload)


class Session:
    def __init__(self, state):
        self.state = state
        self.token = secrets.token_urlsafe(24)
        self.events = asyncio.Queue()
        self.changed = asyncio.Event()
        self.pending = deque()
        self.active = None
        self.segment = None
        self.paused = False
        self.closed = False
        self.generation = 0
        self.next_id = 1
        self.outstanding = 0
        self.uploads = 0
        self.encoding = asyncio.Lock()
        self.sender = ThreadPoolExecutor(max_workers=1, thread_name_prefix="camera-audio-send")
        self.worker = asyncio.create_task(self.run())

    def enqueue(self, payload, indexes):
        job = Job(self.next_id, payload, deque(indexes))
        self.next_id += 1
        self.outstanding += 1
        self.pending.append(job)
        self.changed.set()
        return job.id

    def emit(self, event, **data):
        self.events.put_nowait((event, data))

    def finish(self, job, reason):
        self.emit("end", id=job.id, length=job.length, reason=reason)

    def discard(self):
        self.generation += 1
        if self.active is not None:
            self.finish(self.active, "cancelled")
            self.active = None
        while self.pending:
            self.finish(self.pending.popleft(), "cancelled")
        return self.interrupt()

    def interrupt(self):
        segment = self.segment
        if segment is not None and not segment.done() and not segment.cancelling():
            segment.cancel()
        return segment

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.discard()
        self.sender.shutdown(wait=False)
        self.changed.set()

    async def run(self):
        while not self.closed:
            if self.paused or (self.active is None and not self.pending):
                self.changed.clear()
                await self.changed.wait()
                continue
            if self.active is None:
                self.active = self.pending.popleft()
            job = self.active
            self.segment = asyncio.create_task(self.play(job))
            try:
                reason, = await asyncio.gather(self.segment, return_exceptions=True)
            finally:
                self.segment = None
            if self.active is not job or isinstance(reason, asyncio.CancelledError):
                continue
            self.active = None
            if isinstance(reason, BaseException):
                LOG.error("Unexpected audio playback failure", exc_info=reason)
                reason = "failed"
            self.finish(job, reason)

    async def play(self, job):
        if job.offset == job.length:
            return "completed"
        host = self.state.host
        if not host:
            return "failed"

        def started():
            if self.active is job and not job.started:
                job.started = True
                self.emit("start", id=job.id)

        def progress(offset, reached):
            if self.active is job:
                job.offset = offset
                if reached:
                    job.indexes.popleft()
                    self.emit("index", id=job.id, index=offset)

        try:
            await send_in_worker(self.sender, host, job.payload, job.offset, tuple(job.indexes),
                                 started, progress)
        except AUDIO_ERRORS as error:
            LOG.warning("Audio playback failed: %s", error)
            return "failed"
        return "completed"

    async def stream(self):
        yield sse("session", {"session": self.token})
        while True:
            try:
                async with asyncio.timeout(10):
                    event, data = await self.events.get()
            except TimeoutError:
                yield ": keepalive\n\n"
                continue
            yield sse(event, data)
            if event == "end":
                self.outstanding -= 1


def sse(event, data):
    return f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


class SessionResponse(StreamingResponse):
    def __init__(self, session, release):
        super().__init__(session.stream(), media_type="text/event-stream",
                         headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
        self.session = session
        self.release = release

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            self.session.close()
            self.session.worker.add_done_callback(lambda _: self.release())
            await asyncio.shield(self.session.worker)


def parse_headers(request):
    if request.headers.get("content-type", "").lower() != "application/octet-stream":
        raise HTTPException(415, "Expected application/octet-stream")
    length = request.headers.get("x-audio-length", "")
    if not re.fullmatch(r"[0-9]{1,6}", length) or not 1 <= int(length) <= MAX_SAMPLES:
        raise HTTPException(400, f"X-Audio-Length must be 1 to {MAX_SAMPLES} samples")
    length = int(length)
    raw = request.headers.get("x-audio-indexes", "")
    parts = raw.split(",") if raw else []
    if len(parts) > MAX_INDEXES or any(not re.fullmatch(r"[0-9]{1,6}", part) for part in parts):
        raise HTTPException(400, "X-Audio-Indexes must be a comma-separated list of up to 128 samples")
    indexes = [int(part) for part in parts]
    if indexes != sorted(set(indexes)) or (indexes and indexes[-1] > length):
        raise HTTPException(400, "Indexes must increase strictly and be within the audio length")
    return length, indexes


async def read_pcm(request, size):
    pcm = bytearray()
    try:
        async with asyncio.timeout(15):
            async for chunk in request.stream():
                if len(pcm) + len(chunk) > size:
                    raise HTTPException(400, "PCM byte length does not match X-Audio-Length")
                pcm.extend(chunk)
    except TimeoutError:
        raise HTTPException(408, "PCM upload timed out") from None
    except ClientDisconnect:
        raise HTTPException(400, "PCM upload disconnected") from None
    if len(pcm) != size:
        raise HTTPException(400, "PCM byte length does not match X-Audio-Length")
    return bytes(pcm)


class Audio:
    def __init__(self, state):
        self.state = state
        self.session = None

    def require_session(self, request):
        session = self.session
        token = request.headers.get("x-audio-session", "").encode("latin-1")
        if session is None or session.closed or not secrets.compare_digest(token, session.token.encode()):
            raise HTTPException(404, "Audio session is unavailable")
        return session

    def attach(self, app):
        @app.get("/audio")
        async def connect():
            if self.session is not None:
                raise HTTPException(409, "An audio session is already connected")
            self.session = Session(self.state)
            return SessionResponse(self.session, self.release)

        @app.post("/audio", status_code=202)
        async def enqueue(request: Request):
            session = self.require_session(request)
            length, indexes = parse_headers(request)
            if session.outstanding + session.uploads >= MAX_JOBS:
                raise HTTPException(429, "Audio queue is full")
            generation = session.generation
            session.uploads += 1
            try:
                pcm = await read_pcm(request, length * 2)
                async with session.encoding:
                    if session.closed or session.generation != generation:
                        raise HTTPException(409, "Audio upload was cancelled")
                    payload = await asyncio.to_thread(encode_mulaw, pcm)
                    if session.closed or session.generation != generation:
                        raise HTTPException(409, "Audio upload was cancelled")
                    return {"id": session.enqueue(payload, indexes)}
            finally:
                session.uploads -= 1

        @app.post("/audio/pause", status_code=204)
        async def pause(request: Request):
            session = self.require_session(request)
            session.paused = True
            segment = session.interrupt()
            if segment is not None:
                await asyncio.wait((segment,))

        @app.post("/audio/resume", status_code=204)
        async def resume(request: Request):
            session = self.require_session(request)
            session.paused = False
            session.changed.set()

        @app.post("/audio/cancel", status_code=204)
        async def cancel(request: Request):
            session = self.require_session(request)
            segment = session.discard()
            if segment is not None:
                await asyncio.wait((segment,))

    def release(self):
        self.session = None

    async def close(self):
        if self.session is not None:
            self.session.close()
            await asyncio.shield(self.session.worker)
