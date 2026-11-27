"""Session-based PCM and speech playback with server-timed SSE notifications."""

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
from .tts import split_speech, synthesize

MAX_SAMPLES = 60 * RATE
MAX_JOBS = 32
MAX_INDEXES = 128
MAX_TEXT_BYTES = 8192
AUDIO_ERRORS = (OSError, EOFError, TimeoutError, camera.CameraError)
LOG = logging.getLogger(__name__)


@dataclass
class Job:
    id: int
    payload: bytes | None
    indexes: deque[int]
    text: str | None = None
    payload_start: int = 0
    offset: int = 0
    started: bool = False
    completion_index_reached: bool = False

    @property
    def length(self):
        return None if self.payload is None else self.payload_start + len(self.payload)


class Session:
    def __init__(self, state):
        self.state = state
        self.token = secrets.token_urlsafe(24)
        self.events = asyncio.Queue()
        self.changed = asyncio.Event()
        self.pending = deque()
        self.active = None
        self.operation = None
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

    def enqueue(self, payload, indexes, *, text=None):
        job = Job(self.next_id, payload, deque(indexes), text=text)
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
            reason = "completed" if self.active.completion_index_reached else "cancelled"
            self.finish(self.active, reason)
            self.active = None
        while self.pending:
            self.finish(self.pending.popleft(), "cancelled")
        return self.interrupt(self.operation)

    @staticmethod
    def interrupt(task):
        if task is not None and not task.done() and not task.cancelling():
            task.cancel()
        return task

    def pause(self):
        self.paused = True
        return self.interrupt(self.segment)

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.discard()
        self.sender.shutdown(wait=False)
        self.changed.set()

    async def run(self):
        from_idle = True
        while not self.closed:
            if self.paused or not self.pending:
                from_idle = True
                self.changed.clear()
                await self.changed.wait()
                continue
            job = self.active = self.pending.popleft()
            operation = self.play_speech(job, from_idle) if job.text is not None else self.play_chunk(job)
            self.operation = asyncio.create_task(operation)
            try:
                reason, = await asyncio.gather(self.operation, return_exceptions=True)
            finally:
                self.operation = None
            from_idle = self.active is not job or not job.started
            if self.active is not job:
                continue
            self.active = None
            if isinstance(reason, BaseException):
                LOG.error("Unexpected audio playback failure: id=%d started=%s offset=%d length=%s (8 kHz samples)",
                          job.id, job.started, job.offset, job.length, exc_info=reason)
                reason = "failed"
            self.finish(job, reason)

    async def wait_resumed(self):
        while self.paused:
            self.changed.clear()
            await self.changed.wait()

    async def prepare(self, text, max_samples):
        pcm = await synthesize(text, max_samples)
        return await asyncio.to_thread(encode_mulaw, pcm)

    async def play_speech(self, job, from_idle):
        sections = split_speech(job.text, from_idle)
        section = next(sections, None)
        synthesis = None
        try:
            if section is None:
                raise ValueError("TTS text contains no speech sections")
            while section is not None:
                await self.wait_resumed()
                if synthesis is None:
                    synthesis = asyncio.create_task(self.prepare(section, MAX_SAMPLES - job.offset))
                payload = await asyncio.shield(synthesis)
                synthesis = None
                section = next(sections, None)
                length = job.offset + len(payload)
                job.payload_start = job.offset
                job.payload = payload
                await self.wait_resumed()
                if section is not None:
                    synthesis = asyncio.create_task(self.prepare(section, MAX_SAMPLES - length))
                reason = await self.play_chunk(job)
                if reason != "completed":
                    return reason
            return "completed"
        except (OSError, ValueError) as error:
            LOG.warning("Speech synthesis failed: id=%d started=%s offset=%d length=%s (8 kHz samples): %s: %s",
                        job.id, job.started, job.offset, job.length, type(error).__name__, error)
            return "failed"
        finally:
            if synthesis is not None:
                self.interrupt(synthesis)
                await asyncio.gather(synthesis, return_exceptions=True)

    async def play_chunk(self, job):
        while job.offset < job.length:
            await self.wait_resumed()
            segment = self.segment = asyncio.create_task(self.play(job))
            try:
                await asyncio.wait((segment,))
                if not segment.cancelled():
                    return segment.result()
            finally:
                if not segment.done():
                    self.interrupt(segment)
                    await asyncio.gather(segment, return_exceptions=True)
                self.segment = None
        return "completed"

    async def play(self, job):
        host = self.state.host
        sample_rate = self.state.microphone.sample_rate
        if not host or sample_rate not in camera.MIC_SAMPLE_RATES:
            LOG.warning("Audio playback unavailable: id=%d started=%s offset=%d length=%s (8 kHz samples), "
                        "camera_connected=%s sample_rate=%s",
                        job.id, job.started, job.offset, job.length, bool(host), sample_rate)
            return "failed"

        def started():
            if self.active is job and not job.started:
                job.started = True
                self.emit("start", id=job.id)

        def progress(offset, reached):
            if self.active is job:
                job.offset = job.payload_start + offset
                if reached:
                    job.indexes.popleft()
                    job.completion_index_reached = not job.indexes
                    self.emit("index", id=job.id, index=job.offset)

        try:
            indexes = tuple(index - job.payload_start for index in job.indexes if index <= job.length)
            await send_in_worker(self.sender, host, job.payload, job.offset - job.payload_start, indexes,
                                 started, progress, sample_rate=sample_rate)
        except AUDIO_ERRORS as error:
            LOG.warning("Audio playback failed: id=%d started=%s offset=%d length=%s (8 kHz samples): %s: %s",
                        job.id, job.started, job.offset, job.length, type(error).__name__, error)
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
    return length, parse_indexes(request, length)


def parse_indexes(request, length):
    raw = request.headers.get("x-audio-indexes", "")
    parts = raw.split(",") if raw else []
    if len(parts) > MAX_INDEXES or any(not re.fullmatch(r"[0-9]{1,6}", part) for part in parts):
        raise HTTPException(400, "X-Audio-Indexes must be a comma-separated list of up to 128 samples")
    indexes = [int(part) for part in parts]
    if indexes != sorted(set(indexes)) or (indexes and indexes[-1] > length):
        raise HTTPException(400, "Indexes must increase strictly and be within the audio length")
    return indexes


async def read_upload(request, limit, description, overflow):
    body = bytearray()
    try:
        async with asyncio.timeout(15):
            async for chunk in request.stream():
                if len(body) + len(chunk) > limit:
                    raise overflow
                body.extend(chunk)
    except TimeoutError:
        raise HTTPException(408, f"{description} upload timed out") from None
    except ClientDisconnect:
        raise HTTPException(400, f"{description} upload disconnected") from None
    return bytes(body)


async def read_pcm(request, size):
    mismatch = HTTPException(400, "PCM byte length does not match X-Audio-Length")
    pcm = await read_upload(request, size, "PCM", mismatch)
    if len(pcm) != size:
        raise mismatch
    return pcm


async def read_text(request):
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "text/plain":
        raise HTTPException(415, "Expected text/plain")
    overflow = HTTPException(413, f"TTS text exceeds {MAX_TEXT_BYTES} bytes")
    body = await read_upload(request, MAX_TEXT_BYTES, "TTS", overflow)
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(400, "TTS text must be valid UTF-8") from None
    if not text.strip() or "\0" in text:
        raise HTTPException(400, "TTS text must be nonempty and contain no NUL bytes")
    return text


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

        @app.post("/tts", status_code=202)
        async def enqueue_tts(request: Request):
            session = self.require_session(request)
            indexes = parse_indexes(request, MAX_SAMPLES)
            if session.outstanding + session.uploads >= MAX_JOBS:
                raise HTTPException(429, "Audio queue is full")
            generation = session.generation
            session.uploads += 1
            try:
                text = await read_text(request)
                if session.closed or session.generation != generation:
                    raise HTTPException(409, "TTS upload was cancelled")
                return {"id": session.enqueue(None, indexes, text=text)}
            finally:
                session.uploads -= 1

        @app.post("/audio/pause", status_code=204)
        async def pause(request: Request):
            session = self.require_session(request)
            segment = session.pause()
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
