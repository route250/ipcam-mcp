import asyncio
import unittest
from unittest.mock import AsyncMock, Mock, patch

from ipcam_mcp.ptz import Controller, ControlError, INITIAL_SPEEDS

HOST = "192.168.0.123"


class FeedbackTest(unittest.IsolatedAsyncioTestCase):
    def make_controller(self, positions):
        state = Mock(host=HOST, ptz_state="idle")
        state.status.return_value = {"position": {}}
        controller = Controller(state)
        controller.acknowledge_stop = AsyncMock(return_value=True)
        controller.settled_steps = AsyncMock(
            side_effect=[{"pan_steps": pan, "tilt_steps": tilt} for pan, tilt in positions]
        )
        controller.pulse = AsyncMock()
        return controller

    async def test_single_miss_and_correction_keep_the_initial_speed(self):
        controller = self.make_controller([(1000, 500), (1600, 500), (1800, 500)])
        result = await controller.goto(1800, None, steps=True)
        first, retry = controller.pulse.await_args_list
        self.assertEqual(result["target_steps"], {"pan_steps": 1800, "tilt_steps": 500})
        self.assertEqual(first.args[:2], (HOST, "R"))
        self.assertAlmostEqual(first.args[2], 800 / INITIAL_SPEEDS["R"])
        self.assertAlmostEqual(retry.args[2], 200 / INITIAL_SPEEDS["R"])
        self.assertEqual(controller.speeds, {})
        self.assertEqual(len(controller.speed_samples[(HOST, "R")]), 1)

    async def test_three_consistent_misses_update_only_the_measured_direction(self):
        controller = self.make_controller([(1000, 500), (1600, 500), (1800, 500)] * 3
                                          + [(1000, 500), (1800, 500)])
        for _ in range(3):
            await controller.goto(1800, None, steps=True)
        learned = 0.75 * INITIAL_SPEEDS["R"] + 0.25 * (600 / (800 / INITIAL_SPEEDS["R"]))
        self.assertAlmostEqual(controller.speeds[HOST]["R"], learned)
        self.assertEqual(controller.speeds[HOST]["L"], INITIAL_SPEEDS["L"])
        controller.pulse.reset_mock()
        await controller.goto(1800, None, steps=True)
        controller.pulse.assert_awaited_once()
        self.assertAlmostEqual(controller.pulse.await_args.args[2], 800 / learned)

    async def test_first_try_success_keeps_original_speed_and_discards_prior_miss(self):
        controller = self.make_controller([(1000, 500), (1900, 500), (1800, 500),
                                           (1000, 500), (1810, 500)])
        await controller.goto(1800, None, steps=True)
        self.assertIn((HOST, "R"), controller.speed_samples)
        controller.pulse.reset_mock()
        await controller.goto(1800, None, steps=True)
        controller.pulse.assert_awaited_once_with(HOST, "R", 800 / INITIAL_SPEEDS["R"])
        self.assertEqual(controller.speed_samples, {})
        self.assertEqual(controller.speeds, {})

    async def test_motion_attempts_stop_after_three_retries(self):
        controller = self.make_controller([(1000, 500)] * 9)
        with self.assertRaisesRegex(ControlError, "three retries") as error:
            await controller.goto(1100, 600, steps=True)
        self.assertEqual(error.exception.code, "motion_failed")
        self.assertEqual([call.args[1] for call in controller.pulse.await_args_list], ["R", "D"] * 4)
        self.assertEqual(controller.speeds, {})

    async def test_angles_share_the_step_feedback_and_tolerance(self):
        controller = self.make_controller([(2035, 900)])
        result = await controller.goto(0, 0)
        self.assertEqual(result["target_steps"], {"pan_steps": 2020, "tilt_steps": 891})
        self.assertEqual(result["error_steps"], {"pan_steps": 15, "tilt_steps": 9})
        controller.pulse.assert_not_awaited()

    async def test_only_known_same_axis_reversals_add_time(self):
        controller = self.make_controller([
            (1500, 600), (1700, 600),
            (1700, 600), (1700, 400),
            (1700, 400), (1500, 400), (1500, 600),
            (1500, 600), (1300, 600),
        ])
        controller.pulse = Controller.pulse.__get__(controller)
        timing = dict(wait_seconds=0.5, stop_setup_seconds=0.005, send_interval_seconds=0.505,
                      stop_ack_seconds=0.55, stop_sends=1)
        with patch("ipcam_mcp.ptz.hank_ptz.pulse", return_value=timing) as transport:
            await controller.goto(1700, None, steps=True)
            await controller.goto(None, 400, steps=True)
            await controller.goto(1500, 600, steps=True)
            await controller.goto(1300, None, steps=True)
        self.assertEqual([call.args[0] for call in transport.call_args_list], ["R", "U", "L", "D", "L"])
        for call, extra in zip(transport.call_args_list, [0, 0, 0.300, 0.390, 0]):
            self.assertAlmostEqual(call.kwargs["duration"], 200 / INITIAL_SPEEDS[call.args[0]] + extra)

    async def test_invalid_step_input_never_sends_movement(self):
        controller = self.make_controller([])
        for pan, tilt in [(True, None), (1.1, None), (4101, 3), (None, -1), (None, None)]:
            with self.assertRaises(ControlError) as error:
                await controller.goto(pan, tilt, steps=True)
            self.assertEqual(error.exception.code, "invalid_argument")
        controller.acknowledge_stop.assert_not_awaited()
        controller.pulse.assert_not_awaited()

    async def test_direction_pad_learns_from_the_stopped_position(self):
        controller = self.make_controller([(1000, 630)] * 3)
        controller.state.read_steps = AsyncMock(return_value={"pan_steps": 1000, "tilt_steps": 500})
        # Use the real stop path so the test records the time H is sent.
        controller.acknowledge_stop = Controller.acknowledge_stop.__get__(controller)
        started = asyncio.Event()

        async def motor(host, direction, can_send=None):
            self.assertEqual(host, HOST)
            if can_send:
                self.assertTrue(can_send())
            if direction == "D":
                started.set()

        owner = object()
        with patch("ipcam_mcp.ptz.camera.motor", side_effect=motor) as transport:
            for _ in range(3):
                started.clear()
                controller.drive(owner, "down")
                task = controller.task
                await asyncio.wait_for(started.wait(), 1)
                await asyncio.sleep(0.27)
                controller.stop_manual(owner)
                await asyncio.wait_for(task, 2)
        self.assertEqual([call.args[1] for call in transport.await_args_list], ["H", "D", "H"] * 3)
        self.assertIn(HOST, controller.speeds)
        self.assertGreater(controller.speeds[HOST]["D"], INITIAL_SPEEDS["D"])
        self.assertEqual(controller.speeds[HOST]["U"], INITIAL_SPEEDS["U"])
        self.assertEqual(controller.last_directions, {(HOST, "tilt_steps"): "D"})
