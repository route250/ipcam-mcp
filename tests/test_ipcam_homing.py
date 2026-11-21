import asyncio
import unittest
from unittest.mock import AsyncMock, Mock, call, patch

from ipcam_mcp import camera
from ipcam_mcp.ptz import ControlError, Controller
from tools.ipcam_angles import PositionNotFound


class HomingTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.state = Mock(host="192.168.0.123", ptz_state="idle")
        self.state.current_frame.return_value = object()
        self.state.read_steps = AsyncMock(return_value={"pan_steps": 100, "tilt_steps": 200})
        self.controller = Controller(self.state)
        self.owner = object()
        self.queue = asyncio.Queue()
        self.controller.subscribers[self.owner] = self.queue

    def result(self):
        while not self.queue.empty():
            message = self.queue.get_nowait()
            if message["type"] == "homing_result":
                return message
        self.fail("Missing homing result")

    async def test_homing_refreshes_steps_before_releasing_exclusive_control(self):
        acknowledged = asyncio.Event()
        steps = {"pan_steps": 100, "tilt_steps": 200}
        refreshed = {"pan_steps": 2053, "tilt_steps": 570}
        self.state.read_steps.side_effect = [steps, steps, PositionNotFound(), *([refreshed] * 3)]

        async def motor(host, direction, can_send=None):
            self.assertEqual(host, self.state.host)
            if direction == "Z":
                self.assertTrue(can_send())
                acknowledged.set()
            else:
                self.assertEqual(direction, "H")

        async def pulse(host, direction, duration):
            self.assertEqual(self.state.read_steps.await_count, 3)
            self.assertIs(self.controller.owner, self.owner)

        with patch("ipcam_mcp.ptz.camera.motor", side_effect=motor) as transport, \
                patch.object(self.controller, "pulse", side_effect=pulse) as refresh:
            self.controller.start_homing(self.owner)
            operation = self.controller.task
            await asyncio.wait_for(acknowledged.wait(), 1)
            self.controller.stop_manual(self.owner)
            with self.assertRaises(ControlError) as drive:
                self.controller.drive(object(), "up")
            self.assertEqual(drive.exception.code, "busy")
            with self.assertRaises(ControlError) as goto:
                await self.controller.goto(0, 0)
            self.assertEqual(goto.exception.code, "busy")
            await asyncio.wait_for(operation, 5)
            self.assertEqual([args.args[1] for args in transport.await_args_list], ["Z", "H"])
            self.assertEqual(refresh.await_args_list,
                             [call(self.state.host, "R", 0.1), call(self.state.host, "L", 0.1)])

        self.assertEqual(self.state.read_steps.await_count, 6)
        self.assertEqual(self.state.ptz_state, "idle")
        self.assertEqual(self.state.position.value, refreshed)
        self.assertEqual(self.result(), {"type": "homing_result", "ok": True})

    async def test_failed_first_pulse_stops_without_reversing(self):
        steps = {"pan_steps": 100, "tilt_steps": 200}
        self.state.read_steps.side_effect = [steps, PositionNotFound()]

        async def motor(_host, direction, can_send=None):
            if direction == "Z":
                can_send()

        with patch("ipcam_mcp.ptz.camera.motor", side_effect=motor) as transport, \
                patch.object(self.controller, "pulse", side_effect=camera.CameraError("Pulse failed")) as refresh:
            self.controller.start_homing(self.owner)
            await self.controller.task
            refresh.assert_awaited_once_with(self.state.host, "R", 0.1)
            self.assertEqual([args.args[1] for args in transport.await_args_list], ["Z", "H"])
        self.assertEqual(self.state.ptz_state, "idle")
        self.assertIsNone(self.state.position)
        self.assertEqual(self.result()["code"], "position_refresh_failed")

    async def test_rejects_homing_without_a_position_baseline(self):
        self.state.read_steps.side_effect = PositionNotFound()
        with patch("ipcam_mcp.ptz.camera.motor") as transport:
            self.controller.start_homing(self.owner)
            await self.controller.task
            transport.assert_not_awaited()
        self.assertEqual(self.state.ptz_state, "idle")
        self.assertEqual(self.result()["code"], "position_unavailable")

    async def test_lost_ack_does_not_release_control_or_retry_the_request(self):
        async def lost_ack(_host, _direction, can_send):
            can_send()
            raise camera.CameraError("No ACK")

        with patch("ipcam_mcp.ptz.camera.motor", side_effect=lost_ack) as transport, \
                patch("ipcam_mcp.ptz.HOMING_TIMEOUT", 0.01):
            self.controller.start_homing(self.owner)
            await self.controller.task
            transport.assert_awaited_once()

        self.assertEqual(self.state.ptz_state, "homing_unconfirmed")
        self.assertEqual(self.result()["code"], "homing_unconfirmed")
        with self.assertRaises(ControlError) as drive:
            self.controller.drive(object(), "up")
        self.assertEqual(drive.exception.code, "homing_unconfirmed")
