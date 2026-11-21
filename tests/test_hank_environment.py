"""Protocol and sensor-failure checks for the environmental getter."""
import math
import struct
import unittest
from unittest import mock

from tools.hank_environment import EnvironmentReadError, GET_BODY, GET_PACKET, read_environment
from tools.hank_camera import PACKET1


def frame(command, body):
    return b"INF" + bytes([command]) + b"\x00\x01\x00\x01" + struct.pack(">I", len(body)) + body


class CameraSocket:
    def __init__(self, command=101, body=None, login=None):
        if body is None:
            body = GET_BODY + struct.pack("<ff", 22.41783332824707, 66.84932708740234) + bytes(24)
        if login is None:
            login = struct.pack(">IHH", 1, 0, 0)
        self.incoming = bytearray(frame(2, login) + frame(command, body))
        self.sent = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def sendall(self, packet):
        self.sent.append(packet)

    def settimeout(self, timeout):
        pass

    def recv(self, size):
        size = min(3, size, len(self.incoming))
        chunk = bytes(self.incoming[:size])
        del self.incoming[:size]
        return chunk


class EnvironmentTests(unittest.TestCase):
    def query(self, camera):
        with mock.patch("tools.hank_environment.socket.create_connection", return_value=camera):
            return read_environment()

    def test_fragmented_response_and_only_authentication_and_get_are_sent(self):
        camera = CameraSocket()
        reading = self.query(camera)
        self.assertAlmostEqual(reading["temperature_c"], 22.41783332824707)
        self.assertAlmostEqual(reading["relative_humidity_percent"], 66.84932708740234)
        self.assertEqual(reading["sensor_status"], "not_reported")
        self.assertEqual(camera.sent, [PACKET1, GET_PACKET])
        self.assertEqual(GET_PACKET[:12], bytes.fromhex("494e46640001000100000054"))
        self.assertEqual(GET_BODY[16:80].rstrip(b"\0"), b"TemperatureHumidityInfo")

    def test_rejected_login_does_not_send_get(self):
        camera = CameraSocket(login=struct.pack(">IHH", 1, 1, 0))
        with self.assertRaisesRegex(EnvironmentReadError, "login was not accepted"):
            self.query(camera)
        self.assertEqual(camera.sent, [PACKET1])

    def test_other_command_and_layout_are_rejected(self):
        for camera in [CameraSocket(command=116), CameraSocket(body=GET_BODY + bytes(31)),
                       CameraSocket(body=GET_BODY + bytes(33))]:
            with self.subTest(camera=camera):
                with self.assertRaisesRegex(EnvironmentReadError, "116-byte"):
                    self.query(camera)

    def test_wrong_echo_result_and_reserved_fields_are_rejected(self):
        original = GET_BODY + struct.pack("<ff", 22, 67) + bytes(24)
        for offset in (0, 16, 80, 92):
            body = bytearray(original)
            body[offset] ^= 1
            with self.subTest(offset=offset):
                with self.assertRaises(EnvironmentReadError):
                    self.query(CameraSocket(body=bytes(body)))

    def test_invalid_and_ambiguous_sensor_values_are_rejected(self):
        for temperature, humidity in [(math.nan, 50), (22, math.inf), (22, -1), (22, 101), (0, 0)]:
            with self.subTest(temperature=temperature, humidity=humidity):
                with self.assertRaises(EnvironmentReadError):
                    self.query(CameraSocket(body=GET_BODY + struct.pack("<ff", temperature, humidity) + bytes(24)))

    def test_eof_is_a_transport_error(self):
        camera = CameraSocket()
        del camera.incoming[-1:]
        with self.assertRaisesRegex(EnvironmentReadError, "transport failed"):
            self.query(camera)


if __name__ == "__main__":
    unittest.main()
