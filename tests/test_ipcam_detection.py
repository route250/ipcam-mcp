import asyncio
from contextlib import asynccontextmanager
from ipaddress import IPv4Network
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, Mock, patch

from ipcam_mcp import camera
from ipcam_mcp.state import CameraState, Discovery, Settings
from tools.hank_feature_packets import frame


class DetectionScopesTest(unittest.IsolatedAsyncioTestCase):
    async def test_clears_nonempty_scope_and_reads_it_back_on_fresh_connections(self):
        empty = bytes(72)
        replies = iter((
            frame(58, b"\x02\x16\x0f\x00" + bytes(71) + b"\x01"),
            frame(56, b"\x00\x00"),
            frame(58, b"\x02\x16\x0f\x00" + empty),
            frame(58, b"\x02\x16\x12\x00" + empty),
        ))
        requests = []

        @asynccontextmanager
        async def login(host):
            reader = asyncio.StreamReader()
            reader.feed_data(next(replies))
            reader.feed_eof()
            writer = Mock()
            yield reader, writer
            writer.write.assert_called_once()
            requests.append(writer.write.call_args.args[0])

        with patch.object(camera, "login", login):
            await camera.clear_detection_scopes("camera")
        self.assertEqual(requests, [
            frame(57, b"\x00\x00\x00\x00"),
            frame(55, b"\x02\x16\x0f\x00" + empty),
            frame(57, b"\x00\x00\x00\x00"),
            frame(57, b"\x01\x00\x00\x00"),
        ])

    async def test_checks_scopes_before_each_video_connection(self):
        host = "192.168.1.123"
        settings = Settings(IPv4Network("192.168.1.0/24"), host, None, Path("state.json"),
                            disable_detection_regions=True)
        state = CameraState(settings)
        discovery = Discovery(settings)
        controller = Mock(cancel_camera=AsyncMock())
        calls = []

        async def clear(host):
            calls.append("scopes")

        async def stream(host, publish, publish_audio):
            calls.append("video")
            if calls.count("video") == 2:
                discovery.host = "192.168.1.124"
                discovery.changed.set()

        with patch.object(state, "poll", AsyncMock()), patch("ipcam_mcp.state.RETRY_DELAYS", (0,)), \
             patch.object(camera, "clear_detection_scopes", clear), patch.object(camera, "stream", stream):
            await state.connected(host, controller, discovery)

        self.assertEqual(calls, ["scopes", "video", "scopes", "video"])
