import unittest
from unittest.mock import AsyncMock, Mock

from ipcam_mcp.ptz import Controller, ControlError


class FeedbackTest(unittest.IsolatedAsyncioTestCase):
    def make_controller(self, positions):
        state = Mock()
        state.status.return_value = {'position': {}}
        controller = Controller(state)
        controller.acknowledge_stop = AsyncMock(return_value=True)
        controller.settled_steps = AsyncMock(side_effect=[{'pan_steps': p} for p in positions])
        controller.pulse = AsyncMock()
        return controller

    async def test_large_move_is_bounded_and_uses_stopped_feedback(self):
        controller = self.make_controller([1000, 1390, 1780, 2000, 2000])
        result = await controller.feedback('192.168.0.123', {'pan_steps_per_second': 390},
                                           {'pan_steps': 2000})
        self.assertTrue(result['reached'])
        self.assertEqual(controller.pulse.await_count, 3)
        self.assertTrue(all(call.args[2] <= 1 for call in controller.pulse.await_args_list))

    async def test_repeated_overshoot_stops_instead_of_oscillating(self):
        controller = self.make_controller([1000, 1060, 990])
        with self.assertRaises(ControlError) as error:
            await controller.feedback('192.168.0.123', {'pan_steps_per_second': 390},
                                      {'pan_steps': 1030})
        self.assertEqual(error.exception.code, 'motion_failed')
        self.assertEqual([call.args[1] for call in controller.pulse.await_args_list], ['R', 'L'])
