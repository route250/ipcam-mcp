import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from ipcam_mcp.state import CameraState, Settings
from tools import ipcam_angles
from ipaddress import IPv4Network


class StartupPositionTest(unittest.IsolatedAsyncioTestCase):
    def state(self):
        return CameraState(Settings(IPv4Network('192.168.0.0/24'), None, 'id',
                                    Path('/nonexistent/state')))

    async def test_404_moves_once_then_reads_position(self):
        state = self.state()
        steps = {'pan_steps': 100, 'tilt_steps': 200}
        state.read_steps = AsyncMock(side_effect=[ipcam_angles.PositionNotFound(), steps])
        state.save_host = lambda host: None
        with patch('ipcam_mcp.state.camera.check_model'), \
             patch('ipcam_mcp.state.camera.set_time', new_callable=AsyncMock) as set_time, \
             patch('ipcam_mcp.state.hank_ptz.pulse') as pulse, \
             patch('ipcam_mcp.state.asyncio.sleep', new_callable=AsyncMock):
            await state.adopt('192.168.0.123')
        set_time.assert_awaited_once()
        self.assertEqual([call.args[0] for call in pulse.call_args_list], ['U', 'D'])
        self.assertTrue(all(call.kwargs['duration'] == 0.1 for call in pulse.call_args_list))
        self.assertEqual(state.position.value, steps)

    async def test_other_errors_do_not_move_and_404_does_not_repeat(self):
        state = self.state()
        state.read_steps = AsyncMock(side_effect=ipcam_angles.PTZError('bad response'))
        with patch('ipcam_mcp.state.camera.check_model'), \
             patch('ipcam_mcp.state.camera.set_time', new_callable=AsyncMock), \
             patch('ipcam_mcp.state.hank_ptz.pulse') as pulse, \
             patch('ipcam_mcp.state.asyncio.sleep', new_callable=AsyncMock):
            with self.assertRaises(ipcam_angles.PTZError):
                await state.adopt('192.168.0.123')
            pulse.assert_not_called()
            state.read_steps.side_effect = ipcam_angles.PositionNotFound()
            for _ in range(2):
                with self.assertRaises(ipcam_angles.PositionNotFound):
                    await state.adopt('192.168.0.123')
            self.assertEqual(pulse.call_count, 2)

    async def test_failed_up_never_starts_down(self):
        state = self.state()
        state.read_steps = AsyncMock(side_effect=ipcam_angles.PositionNotFound())
        with patch('ipcam_mcp.state.camera.check_model'), \
             patch('ipcam_mcp.state.camera.set_time', new_callable=AsyncMock), \
             patch('ipcam_mcp.state.hank_ptz.pulse', side_effect=ipcam_angles.PTZError('stop failed')) as pulse:
            with self.assertRaises(ipcam_angles.PTZError):
                await state.adopt('192.168.0.123')
            self.assertEqual([call.args[0] for call in pulse.call_args_list], ['U'])
