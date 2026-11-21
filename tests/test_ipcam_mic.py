import asyncio
import struct
import unittest
from unittest.mock import AsyncMock, Mock

import numpy as np

from ipcam_mcp import camera
from ipcam_mcp.mic import Microphone, QUEUE_FRAMES
from ipcam_mcp.noise_reduction import Denoiser, WINDOW_SAMPLES


class MicrophoneTest(unittest.IsolatedAsyncioTestCase):
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
        feed(4, camera.MIC_FRAME_PREFIX, 3, b"\xff" * 320)
        reader.feed_eof()
        with self.assertRaises(asyncio.IncompleteReadError):
            await camera.feed_media(reader, decoder, publish)
        decoder.write.assert_called_once_with(video)
        publish.assert_called_once_with(bytes.fromhex("00000003"), b"\xff" * 320)

    async def test_slow_subscriber_keeps_the_latest_five_frames(self):
        mic = Microphone()
        slow = asyncio.Queue(maxsize=QUEUE_FRAMES)
        fast = asyncio.Queue(maxsize=QUEUE_FRAMES)
        mic.subscribers.update((slow, fast))

        for sequence in range(8):
            header = sequence.to_bytes(4, "big")
            mic.publish(header, b"\xff" * 320)
            self.assertEqual(fast.get_nowait(), header + bytes(640))

        self.assertEqual([int.from_bytes(slow.get_nowait()[:4], "big") for _ in range(QUEUE_FRAMES)],
                         [3, 4, 5, 6, 7])
        mic.reset()
        self.assertIsNone(slow.get_nowait())
        self.assertIsNone(fast.get_nowait())
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
