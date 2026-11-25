import asyncio
from ipaddress import IPv4Network
import json
from pathlib import Path
import socket
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, Mock, patch

from tinytuya.core import AESCipher, udpkey

from ipcam_mcp.state import CameraState, Settings


class CameraDiscoveryTest(unittest.IsolatedAsyncioTestCase):
    async def test_notification_reconnects_at_new_ip_after_video_disconnects(self):
        old_ip, new_ip = "127.0.0.2", "127.0.0.3"
        mac = "02:01:02:03:04:05"
        old_ready, new_ready, disconnect = (asyncio.Event() for _ in range(3))
        loop = asyncio.get_running_loop()
        create_endpoint = loop.create_datagram_endpoint
        listener = loop.create_future()

        async def bind_discovery(factory, *, local_addr):
            transport, protocol = await create_endpoint(factory, local_addr=("127.0.0.1", 0))
            listener.set_result((transport.get_extra_info("sockname"), protocol.changed))
            return transport, protocol

        async def stream(host, publish, microphone):
            publish(b"jpeg")
            if host == old_ip:
                old_ready.set()
                await disconnect.wait()
                raise ConnectionResetError()
            self.assertEqual(host, new_ip)
            new_ready.set()
            await asyncio.Future()

        with TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            state = CameraState(Settings(IPv4Network("127.0.0.0/24"), old_ip, "camera", path))
            state.read_steps = AsyncMock(return_value={"pan_steps": 100, "tilt_steps": 200})
            state.environment_sample = AsyncMock()
            controller = Mock(cancel_camera=AsyncMock())
            with (
                patch.object(loop, "create_datagram_endpoint", side_effect=bind_discovery),
                patch("ipcam_mcp.state.camera.check_model") as check_model,
                patch("ipcam_mcp.state.camera.set_time", new_callable=AsyncMock),
                patch("ipcam_mcp.state.camera_mac_address", return_value=mac),
                patch("ipcam_mcp.state.camera.stream", side_effect=stream),
            ):
                task = asyncio.create_task(state.run(controller))
                try:
                    async with asyncio.timeout(3):
                        endpoint, notice_received = await listener
                        await old_ready.wait()
                        self.assertEqual(state.status()["camera"], {"ip_address": old_ip, "mac_address": mac})
                        payload = json.dumps({"gwId": "camera", "ip": new_ip}).encode()
                        packet = AESCipher(udpkey).encrypt(payload, use_base64=False)
                        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
                            sender.bind((new_ip, 0))
                            sender.sendto(packet, endpoint)
                        await notice_received.wait()
                        self.assertEqual(state.status()["camera"]["ip_address"], old_ip)
                        disconnect.set()
                        await new_ready.wait()
                        self.assertEqual(state.status()["camera"], {"ip_address": new_ip, "mac_address": mac})
                        self.assertEqual(json.loads(path.read_text())["camera_host"], new_ip)
                        self.assertEqual([call.args[0] for call in check_model.call_args_list], [old_ip, new_ip])
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
