import asyncio
import json
import struct
from threading import get_ident
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ipcam_mcp.app import create_app
from ipcam_mcp import camera
from ipcam_mcp.audio_sender import encode_mulaw
from tools.hank_camera import PACKET1


class AudioTest(unittest.IsolatedAsyncioTestCase):
    def request(self, method, path, body=b"", **headers):
        incoming, outgoing = asyncio.Queue(), asyncio.Queue()
        incoming.put_nowait({"type": "http.request", "body": body})
        scope = {"type": "http", "asgi": {"spec_version": "2.3"}, "http_version": "1.1",
                 "method": method, "scheme": "http", "path": path, "query_string": b"",
                 "headers": [(b"host", b"127.0.0.1:8000"), *(
                     (key.replace("_", "-").encode(), value.encode()) for key, value in headers.items())]}
        task = asyncio.create_task(self.app(scope, incoming.get, outgoing.put))
        return task, incoming, outgoing

    async def receive(self, outgoing):
        return await asyncio.wait_for(outgoing.get(), 2)

    async def event(self, outgoing, kind):
        body = (await self.receive(outgoing))["body"].decode().splitlines()
        self.assertEqual(body[0], f"event: {kind}")
        return json.loads(body[1].removeprefix("data: "))

    async def post(self, path, status, body=b"", **headers):
        task, _, outgoing = self.request("POST", path, body, x_audio_session=self.token, **headers)
        await asyncio.wait_for(task, 2)
        self.assertEqual((await self.receive(outgoing))["status"], status)
        return (await self.receive(outgoing))["body"]

    async def upload(self, samples, indexes=""):
        body = await self.post("/audio", 202, bytes(2 * samples),
                               content_type="application/octet-stream",
                               x_audio_length=str(samples), x_audio_indexes=indexes)
        return json.loads(body)["id"]

    async def fake_camera(self, reader, writer):
        def packet(command, body):
            return b"INF" + bytes([command]) + b"\0\x01\0\x01" + struct.pack(">I", len(body)) + body

        try:
            self.assertEqual(await reader.readexactly(len(PACKET1)), PACKET1)
            writer.write(packet(2, struct.pack(">IHH", 1, 0, 0)))
            self.assertEqual(await reader.readexactly(len(camera.SPEAKER_START)), camera.SPEAKER_START)
            writer.write(packet(8, b"\0\x02\x01" + bytes(21)))
            while not reader.at_eof():
                try:
                    self.frames.put_nowait(await camera.read_frame(reader))
                except asyncio.IncompleteReadError:
                    break
        finally:
            writer.close()

    async def test_sse_pause_resume_cancel_and_normal_completion(self):
        self.frames = asyncio.Queue()
        encode_threads, send_threads = set(), set()
        send_frame = camera.send_speaker_frame

        def track_encode(pcm):
            encode_threads.add(get_ident())
            return encode_mulaw(pcm)

        async def track_send(*args):
            send_threads.add(get_ident())
            await send_frame(*args)

        self.enterContext(patch("ipcam_mcp.audio.encode_mulaw", track_encode))
        self.enterContext(patch.object(camera, "send_speaker_frame", track_send))
        server = await asyncio.start_server(self.fake_camera, "127.0.0.1", 0)
        self.addAsyncCleanup(server.wait_closed)
        self.addCleanup(server.close)
        self.enterContext(patch.object(camera, "PORT", server.sockets[0].getsockname()[1]))
        self.enterContext(patch("ipcam_mcp.app.CameraState", return_value=SimpleNamespace(host="127.0.0.1")))
        self.app = create_app(None)
        stream, incoming, outgoing = self.request("GET", "/audio")

        async def disconnect():
            incoming.put_nowait({"type": "http.disconnect"})
            await asyncio.wait_for(stream, 2)

        self.addAsyncCleanup(disconnect)
        self.assertEqual((await self.receive(outgoing))["status"], 200)
        self.token = (await self.event(outgoing, "session"))["session"]
        await self.post("/audio", 400, b"\0", content_type="application/octet-stream", x_audio_length="1")

        first = await self.upload(8000, "0,1600")
        self.assertEqual(await self.event(outgoing, "start"), {"id": first})
        self.assertEqual(await self.event(outgoing, "index"), {"id": first, "index": 0})
        command, frame = await self.receive(self.frames)
        self.assertEqual(command, 4)
        self.assertEqual(frame, bytes.fromhex("d30201001f400010") +
                         struct.pack(">IIII", 1, 320, 0, 0) + b"\xff" * 320)
        await self.post("/audio/pause", 204)
        second = await self.upload(320)
        with self.assertRaises(TimeoutError):
            await asyncio.wait_for(outgoing.get(), 0.24)
        await self.post("/audio/resume", 204)
        self.assertEqual(await self.event(outgoing, "index"), {"id": first, "index": 1600})
        await self.post("/audio/cancel", 204)
        self.assertEqual(await self.event(outgoing, "end"),
                         {"id": first, "length": 8000, "reason": "cancelled"})
        self.assertEqual(await self.event(outgoing, "end"),
                         {"id": second, "length": 320, "reason": "cancelled"})

        await self.post("/audio/pause", 204)
        last = await self.upload(321, "321")
        next_id = await self.upload(320)
        await self.post("/audio/resume", 204)
        self.assertEqual(last, 3)
        self.assertEqual(await self.event(outgoing, "start"), {"id": last})
        self.assertEqual(await self.event(outgoing, "index"), {"id": last, "index": 321})
        self.assertEqual(await self.event(outgoing, "end"),
                         {"id": last, "length": 321, "reason": "completed"})
        self.assertEqual(await self.event(outgoing, "start"), {"id": next_id})
        self.assertEqual(await self.event(outgoing, "end"),
                         {"id": next_id, "length": 320, "reason": "completed"})
        self.assertTrue(encode_threads)
        self.assertEqual(len(send_threads), 1)
        self.assertTrue(encode_threads.isdisjoint(send_threads))
        self.assertNotIn(get_ident(), encode_threads | send_threads)
