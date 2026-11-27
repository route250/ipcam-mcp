import asyncio
import json
from pathlib import Path
import shlex
import struct
import sys
from tempfile import TemporaryDirectory
from threading import get_ident
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ipcam_mcp.app import create_app
from ipcam_mcp import camera
from ipcam_mcp.audio_codec import encode_mulaw
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

    async def speak(self, text, indexes=""):
        body = await self.post("/tts", 202, text.encode("utf-8"),
                               content_type="text/plain; charset=utf-8", x_audio_indexes=indexes)
        return json.loads(body)["id"]

    def tts_command(self, script):
        directory = Path(self.enterContext(TemporaryDirectory()))
        command = directory / "tts.sh"
        command.write_text("#!/bin/sh\nset -eu\n" + script)
        command.chmod(0o700)
        self.enterContext(patch.dict("os.environ", {"IPCAM_TTS_COMMAND": str(command)}))
        return directory

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

    async def connect_audio(self, sample_rate=8000, mic_source="camera"):
        self.frames = asyncio.Queue()
        server = await asyncio.start_server(self.fake_camera, "127.0.0.1", 0)
        self.addAsyncCleanup(server.wait_closed)
        self.addCleanup(server.close)
        self.enterContext(patch.object(camera, "PORT", server.sockets[0].getsockname()[1]))
        microphone = SimpleNamespace(sample_rate=sample_rate)
        state = SimpleNamespace(host="127.0.0.1", microphone=microphone)
        self.enterContext(patch("ipcam_mcp.app.CameraState", return_value=state))
        self.app = create_app(SimpleNamespace(mic_source=mic_source, usb_mic_device="test-usb"))
        stream, incoming, outgoing = self.request("GET", "/audio")

        async def disconnect():
            incoming.put_nowait({"type": "http.disconnect"})
            await asyncio.wait_for(stream, 2)

        self.addAsyncCleanup(disconnect)
        self.assertEqual((await self.receive(outgoing))["status"], 200)
        self.token = (await self.event(outgoing, "session"))["session"]
        return outgoing

    async def test_16khz_speaker_frames_preserve_8khz_playback_duration(self):
        outgoing = await self.connect_audio(sample_rate=16000, mic_source="usb")
        pcm = struct.pack("<320h", *(i * 50 for i in range(320)))
        job = json.loads(await self.post(
            "/audio", 202, pcm, content_type="application/octet-stream",
            x_audio_length="320", x_audio_indexes="160",
        ))["id"]
        self.assertEqual(await self.event(outgoing, "start"), {"id": job})
        for sequence in (1, 2):
            command, frame = await self.receive(self.frames)
            self.assertEqual(command, 4)
            self.assertEqual(frame[:24], bytes.fromhex("d30201003e800010") +
                             struct.pack(">IIII", sequence, 320, (sequence - 1) * 20, 0))
            samples = encode_mulaw(pcm)[(sequence - 1) * 160:sequence * 160]
            self.assertEqual(frame[24::2], samples)
            self.assertEqual(frame[25::2], samples)
        self.assertEqual(await self.event(outgoing, "index"), {"id": job, "index": 160})
        self.assertEqual(await self.event(outgoing, "end"), {"id": job, "length": 320, "reason": "completed"})

    async def test_sse_pause_resume_cancel_and_normal_completion(self):
        encode_threads, send_threads = set(), set()
        send_frame = camera.send_speaker_frame

        def track_encode(pcm):
            encode_threads.add(get_ident())
            return encode_mulaw(pcm)

        async def track_send(*args, **kwargs):
            send_threads.add(get_ident())
            await send_frame(*args, **kwargs)

        self.enterContext(patch("ipcam_mcp.audio.encode_mulaw", track_encode))
        self.enterContext(patch.object(camera, "send_speaker_frame", track_send))
        outgoing = await self.connect_audio()
        await self.post("/audio", 400, b"\0", content_type="application/octet-stream", x_audio_length="1")

        first = await self.upload(8000, "0,1600")
        self.assertEqual(await self.event(outgoing, "start"), {"id": first})
        self.assertEqual(await self.event(outgoing, "index"), {"id": first, "index": 0})
        command, frame = await self.receive(self.frames)
        self.assertEqual(command, 4)
        self.assertEqual(frame, bytes.fromhex("d30201001f400010") +
                         struct.pack(">IIII", 1, 320, 0, 0) + b"\xff" * 320)
        await self.post("/audio/pause", 204)
        second = await self.upload(320, "0")
        with self.assertRaises(TimeoutError):
            await asyncio.wait_for(outgoing.get(), 0.24)
        await self.post("/audio/resume", 204)
        self.assertEqual(await self.event(outgoing, "index"), {"id": first, "index": 1600})
        await self.post("/audio/cancel", 204)
        self.assertEqual(await self.event(outgoing, "end"),
                         {"id": first, "length": 8000, "reason": "completed"})
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

    async def test_tts_shares_pcm_queue_and_sample_indexes(self):
        directory = self.tts_command(f"""
cd "$(dirname "$0")"
printf '%s' "$1" > text
exec {shlex.quote(sys.executable)} - "$2" <<'PY'
import sys
import wave

with wave.open(sys.argv[1], "wb") as output:
    output.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
    output.writeframes(bytes(640))
PY
""")
        outgoing = await self.connect_audio()
        await self.post("/audio/pause", 204)
        text = 'カメラです"$(false)"。'
        speech = await self.speak(text, "0,320")
        pcm = await self.upload(320)
        short = await self.speak("短い区間", "8000")
        self.assertEqual((speech, pcm, short), (1, 2, 3))
        self.assertFalse((directory / "text").exists())

        await self.post("/audio/resume", 204)
        self.assertEqual(await self.event(outgoing, "start"), {"id": speech})
        self.assertEqual((directory / "text").read_text(), text)
        for index in (0, 320):
            self.assertEqual(await self.event(outgoing, "index"), {"id": speech, "index": index})
        self.assertEqual(await self.event(outgoing, "end"),
                         {"id": speech, "length": 320, "reason": "completed"})
        self.assertEqual(await self.event(outgoing, "start"), {"id": pcm})
        self.assertEqual(await self.event(outgoing, "end"),
                         {"id": pcm, "length": 320, "reason": "completed"})
        self.assertEqual(await self.event(outgoing, "start"), {"id": short})
        self.assertEqual(await self.event(outgoing, "end"),
                         {"id": short, "length": 320, "reason": "completed"})

    async def test_tts_plays_first_section_before_next_is_ready_and_resumes_after_pause(self):
        calls = asyncio.Queue()
        release_second = asyncio.Event()
        second_ready = asyncio.Event()

        async def synthesize(text, max_samples):
            calls.put_nowait(text)
            if text == "こんにちは。":
                await release_second.wait()
                second_ready.set()
            return bytes(6400 if text == "やあ" else 640)

        self.enterContext(patch("ipcam_mcp.audio.synthesize", synthesize))
        outgoing = await self.connect_audio()
        speech = await self.speak("やあ、こんにちは。元気だったかな？　私は元気だよ！", "3200,3360")
        queued = await self.speak(
            "続けて、読みます。\n次です！？　How are you?! I’m fine, thanks.\nVersion 3.14 works!\t区切り"
        )
        self.assertEqual(await self.receive(calls), "やあ")
        self.assertEqual(await self.receive(calls), "こんにちは。")
        self.assertEqual(await self.event(outgoing, "start"), {"id": speech})
        await self.post("/audio/pause", 204)
        release_second.set()
        await asyncio.wait_for(second_ready.wait(), 2)
        with self.assertRaises(TimeoutError):
            await asyncio.wait_for(outgoing.get(), 0.1)
        self.assertTrue(calls.empty())

        await self.post("/audio/resume", 204)
        for index in (3200, 3360):
            self.assertEqual(await self.event(outgoing, "index"), {"id": speech, "index": index})
        self.assertEqual(await self.event(outgoing, "end"),
                         {"id": speech, "length": 4160, "reason": "completed"})
        self.assertEqual(await self.event(outgoing, "start"), {"id": queued})
        self.assertEqual(await self.event(outgoing, "end"),
                         {"id": queued, "length": 1920, "reason": "completed"})
        self.assertEqual([calls.get_nowait() for _ in range(8)], [
            "元気だったかな？", "私は元気だよ！", "続けて、読みます。", "次です！？",
            "How are you?!", "I’m fine, thanks.", "Version 3.14 works!", "区切り",
        ])
        self.assertTrue(calls.empty())

        after_idle = await self.speak("もう一度、試します。")
        self.assertEqual(await self.event(outgoing, "start"), {"id": after_idle})
        self.assertEqual(await self.event(outgoing, "end"),
                         {"id": after_idle, "length": 640, "reason": "completed"})
        self.assertEqual([calls.get_nowait() for _ in range(2)], ["もう一度", "試します。"])
        self.assertTrue(calls.empty())

    async def test_failed_later_tts_section_ends_before_next_job_starts(self):
        fail_second = asyncio.Event()

        async def synthesize(text, max_samples):
            if text == "次の文。":
                await fail_second.wait()
                raise OSError("mock synthesis failure")
            return bytes(640)

        self.enterContext(patch("ipcam_mcp.audio.synthesize", synthesize))
        outgoing = await self.connect_audio()
        speech = await self.speak("最初の文。次の文。")
        self.assertEqual(await self.event(outgoing, "start"), {"id": speech})
        queued = await self.speak("次の再生。")

        with self.assertLogs("ipcam_mcp.audio", level="WARNING") as logs:
            fail_second.set()
            self.assertEqual(await self.event(outgoing, "end"),
                             {"id": speech, "length": 320, "reason": "failed"})
        self.assertIn(f"id={speech} started=True offset=320 length=320", logs.output[0])
        self.assertIn("OSError: mock synthesis failure", logs.output[0])
        self.assertEqual(await self.event(outgoing, "start"), {"id": queued})
        self.assertEqual(await self.event(outgoing, "end"),
                         {"id": queued, "length": 320, "reason": "completed"})

    async def test_cancel_while_playing_stops_next_tts_section(self):
        calls = asyncio.Queue()
        cancelled = asyncio.Event()

        async def synthesize(text, max_samples):
            calls.put_nowait(text)
            if text == "合成中。":
                try:
                    await asyncio.Future()
                finally:
                    cancelled.set()
            return bytes(16000)

        self.enterContext(patch("ipcam_mcp.audio.synthesize", synthesize))
        outgoing = await self.connect_audio()
        speech = await self.speak("再生中、合成中。最後。", "0,8000")
        self.assertEqual(await self.receive(calls), "再生中")
        self.assertEqual(await self.receive(calls), "合成中。")
        self.assertEqual(await self.event(outgoing, "start"), {"id": speech})
        self.assertEqual(await self.event(outgoing, "index"), {"id": speech, "index": 0})
        await self.post("/audio/cancel", 204)
        self.assertTrue(cancelled.is_set())
        self.assertTrue(calls.empty())
        self.assertEqual(await self.event(outgoing, "end"),
                         {"id": speech, "length": 8000, "reason": "cancelled"})

        pcm = await self.upload(320)
        self.assertEqual(await self.event(outgoing, "start"), {"id": pcm})
        self.assertEqual(await self.event(outgoing, "end"),
                         {"id": pcm, "length": 320, "reason": "completed"})

    async def test_tts_cancel_after_final_index_reports_completion_only_for_active_job(self):
        async def synthesize(text, max_samples):
            return bytes(2 * 40_000)

        self.enterContext(patch("ipcam_mcp.audio.synthesize", synthesize))
        outgoing = await self.connect_audio()
        speech = await self.speak("再生中。", "0,8000")
        queued = await self.speak("待機中。", "0")
        self.assertEqual(await self.event(outgoing, "start"), {"id": speech})
        for index in (0, 8000):
            self.assertEqual(await self.event(outgoing, "index"), {"id": speech, "index": index})
        await self.post("/audio/cancel", 204)
        self.assertEqual(await self.event(outgoing, "end"),
                         {"id": speech, "length": 40_000, "reason": "completed"})
        self.assertEqual(await self.event(outgoing, "end"),
                         {"id": queued, "length": None, "reason": "cancelled"})

    async def test_cancel_during_synthesis_stops_child_and_clears_queue(self):
        directory = self.tts_command("""
cd "$(dirname "$0")"
sleep 30 &
child=$!
printf '%s\n%s\n' "$child" "$2" > starting
mv starting started
wait "$child"
""")
        outgoing = await self.connect_audio()
        speech = await self.speak("合成中にキャンセル")
        queued = await self.speak("合成の順番待ち")
        started = directory / "started"
        async with asyncio.timeout(2):
            while not started.exists():
                await asyncio.sleep(0.01)
        child, output = started.read_text().splitlines()
        await self.post("/audio/pause", 204)
        self.assertTrue(Path(output).parent.exists())
        await self.post("/audio/cancel", 204)
        self.assertFalse(Path(output).parent.exists())
        async with asyncio.timeout(2):
            while True:
                try:
                    process_state = Path(f"/proc/{child}/stat").read_text().rsplit(")", 1)[1].split()[0]
                except FileNotFoundError:
                    break
                if process_state == "Z":
                    break
                await asyncio.sleep(0.01)
        for job in (speech, queued):
            self.assertEqual(await self.event(outgoing, "end"),
                             {"id": job, "length": None, "reason": "cancelled"})
        pcm = await self.upload(320)
        await self.post("/audio/resume", 204)
        self.assertEqual(await self.event(outgoing, "start"), {"id": pcm})
        self.assertEqual(await self.event(outgoing, "end"),
                         {"id": pcm, "length": 320, "reason": "completed"})
