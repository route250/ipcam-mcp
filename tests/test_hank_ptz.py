import struct
import unittest
from unittest.mock import MagicMock, patch

from tools import hank_ptz


class PulseTest(unittest.TestCase):
    def run_pulse(self, *, duration=0.050, reject_stop=False, stop_login_seconds=0.214):
        now = 0.0
        events = []
        sockets = [MagicMock() for _ in range(4)]
        for index, sock in enumerate(sockets):
            sock.__enter__.return_value = sock
            sock.sendall.side_effect = (
                lambda packet, index=index: events.append((index, packet, now))
            )

        def sleep(seconds):
            nonlocal now
            now += seconds

        def receive(sock, deadline, timeout):
            index = sockets.index(sock)
            if sock.sendall.call_args.args[0] == hank_ptz.PACKET1:
                if index == 2:
                    if now + stop_login_seconds > deadline:
                        sleep(deadline - now)
                        raise hank_ptz._DeadlineReached
                    sleep(stop_login_seconds)
                return 2, struct.pack(">IHH", 1, 0, 0)
            if index == 2 and reject_stop:
                return 16, bytes([1, 0, 0, 0])
            return 16, bytes(4)

        with (patch.object(hank_ptz.socket, "create_connection", side_effect=sockets),
              patch.object(hank_ptz, "_receive_frame", side_effect=receive),
              patch.object(hank_ptz.time, "monotonic", side_effect=lambda: now),
              patch.object(hank_ptz.time, "sleep", side_effect=sleep)):
            timing = hank_ptz.pulse("R", duration=duration)
        return timing, events

    def test_slow_stop_login_finishes_before_the_movement_timer_starts(self):
        timing, events = self.run_pulse()
        self.assertEqual([(index, packet) for index, packet, _ in events], [
            (0, hank_ptz.PACKET1), (0, hank_ptz._packet("H")),
            (1, hank_ptz.PACKET1), (2, hank_ptz.PACKET1),
            (1, hank_ptz._packet("R")), (2, hank_ptz._packet("H")),
        ])
        self.assertAlmostEqual(events[-2][2], 0.214)
        self.assertAlmostEqual(timing["send_interval_seconds"], 0.050)
        self.assertEqual(timing["stop_sends"], 1)

    def test_rejected_stop_is_retried_on_a_new_authenticated_connection(self):
        timing, events = self.run_pulse(reject_stop=True)
        self.assertEqual([(index, packet) for index, packet, _ in events[-3:]], [
            (2, hank_ptz._packet("H")), (3, hank_ptz.PACKET1), (3, hank_ptz._packet("H")),
        ])
        self.assertEqual(timing["stop_sends"], 2)

    def test_one_second_movement_prepares_stop_before_sending_direction(self):
        timing, events = self.run_pulse(duration=1.0)
        self.assertEqual([(index, packet) for index, packet, _ in events[-3:]], [
            (2, hank_ptz.PACKET1), (1, hank_ptz._packet("R")), (2, hank_ptz._packet("H")),
        ])
        self.assertAlmostEqual(timing["send_interval_seconds"], 1.0)

    def test_long_movement_prepares_stop_one_second_before_sending_it(self):
        for duration in (1.01, 5.0):
            with self.subTest(duration=duration):
                timing, events = self.run_pulse(duration=duration)
                self.assertEqual([(index, packet) for index, packet, _ in events[-3:]], [
                    (1, hank_ptz._packet("R")), (2, hank_ptz.PACKET1), (2, hank_ptz._packet("H")),
                ])
                self.assertAlmostEqual(events[-2][2] - events[-3][2], duration - 1.0)
                self.assertAlmostEqual(timing["send_interval_seconds"], duration)

    def test_long_movement_uses_a_fresh_connection_if_login_exceeds_the_stop_deadline(self):
        timing, events = self.run_pulse(duration=5.0, stop_login_seconds=1.5)
        self.assertEqual([(index, packet) for index, packet, _ in events[-3:]], [
            (2, hank_ptz.PACKET1), (3, hank_ptz.PACKET1), (3, hank_ptz._packet("H")),
        ])
        self.assertAlmostEqual(events[-2][2] - events[-3][2], 1.0)
        self.assertAlmostEqual(timing["send_interval_seconds"], 5.0)
