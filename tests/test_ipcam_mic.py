import asyncio
import struct
import unittest
from unittest.mock import AsyncMock, Mock

import numpy as np

from ipcam_mcp import camera
from ipcam_mcp.mic import Microphone, QUEUE_FRAMES
from ipcam_mcp.noise_reduction import Denoiser, WINDOW_SAMPLES


class MicrophoneTest(unittest.IsolatedAsyncioTestCase):
    async def test_16khz_subscription_pairs_consecutive_frames_across_sequence_wrap(self):
        mic = Microphone()
        outgoing = asyncio.Queue()
        incoming = asyncio.Queue()
        websocket = Mock(
            accept=AsyncMock(),
            send_json=AsyncMock(side_effect=outgoing.put_nowait),
            send_bytes=AsyncMock(side_effect=outgoing.put_nowait),
            receive=AsyncMock(side_effect=incoming.get),
        )
        task = asyncio.create_task(mic.subscribe(websocket, metadata=True))
        try:
            await asyncio.sleep(0)
            frames = ((0xFFFFFFFF, b"a"), (0, b"b"), (2, b"c"), (4, b"d"), (5, b"e"))
            for sequence, sample in frames:
                mic.publish_pcm(sequence.to_bytes(4, "big"), sample * 640, 16000)
            self.assertEqual(await asyncio.wait_for(outgoing.get(), 1),
                             {"type": "format", "sample_rate": 16000, "frame_samples": 640})
            self.assertEqual(await asyncio.wait_for(outgoing.get(), 1), b"\xff\xff\xff\xff" + b"a" * 640 + b"b" * 640)
            self.assertEqual(await asyncio.wait_for(outgoing.get(), 1), b"\x00\x00\x00\x04" + b"d" * 640 + b"e" * 640)
            self.assertTrue(outgoing.empty())
        finally:
            incoming.put_nowait({"type": "websocket.disconnect"})
            await asyncio.wait_for(task, 1)

    async def test_audio_is_extracted_before_the_video_decoder(self):
        reader = asyncio.StreamReader()
        decoder = Mock(drain=AsyncMock())
        publish = Mock()

        def feed(command, prefix, sequence, payload):
            body = prefix + struct.pack(">IIII", sequence, len(payload), 0, 0) + payload
            reader.feed_data(b"INF" + bytes((command,)) + b"\0\x01\0\x01"
                             + struct.pack(">I", len(body)) + body)

        video = b"\0\0\x01\x65"
        feed(6, bytes.fromhex("d101050f02800168"), 1, video)
        feed(4, bytes.fromhex("d30201001f400010"), 2, b"\xff" * 320)
        feed(4, bytes.fromhex("d30201011f400010"), 3, b"\xff" * 320)
        feed(4, bytes.fromhex("d30201013e800010"), 4, b"\xff" * 320)
        feed(4, bytes.fromhex("d302010100000010"), 5, b"\xff" * 320)
        reader.feed_eof()
        with self.assertRaises(asyncio.IncompleteReadError):
            await camera.feed_media(reader, decoder, publish)
        decoder.write.assert_called_once_with(video)
        self.assertEqual([call.args for call in publish.call_args_list], [
            (bytes.fromhex("00000003"), b"\xff" * 320, 8000),
            (bytes.fromhex("00000004"), b"\xff" * 320, 16000),
        ])

    async def test_slow_subscriber_keeps_the_latest_five_frames(self):
        mic = Microphone()
        slow = asyncio.Queue(maxsize=QUEUE_FRAMES)
        fast = asyncio.Queue(maxsize=QUEUE_FRAMES)
        mic.subscribers.update((slow, fast))

        for sequence in range(8):
            header = sequence.to_bytes(4, "big")
            mic.publish(header, b"\xff" * 320, 16000)
            self.assertEqual(fast.get_nowait(), (16000, header + bytes(640)))

        self.assertEqual([int.from_bytes(slow.get_nowait()[1][:4], "big") for _ in range(QUEUE_FRAMES)],
                         [3, 4, 5, 6, 7])
        mic.reset()
        self.assertEqual(slow.get_nowait(), 1012)
        self.assertEqual(fast.get_nowait(), 1012)
        self.assertFalse(mic.subscribers)

    async def test_denoiser_preserves_pcm_alignment_and_resets_on_sequence_gap(self):
        denoiser = Denoiser(np.zeros(WINDOW_SAMPLES // 2 + 1))
        rng = np.random.default_rng(42)

        def make_frame(sequence):
            samples = rng.integers(-32768, 32768, size=320, dtype=np.int16)
            return sequence.to_bytes(4, "big") + samples.astype("<i2").tobytes()

        first = [make_frame(sequence) for sequence in range(8)]
        second = [make_frame(sequence) for sequence in range(10, 18)]
        for contiguous in (first, second):
            received = [output for frame in contiguous for output in denoiser.feed(frame)]
            self.assertEqual(received, contiguous[:-3])
