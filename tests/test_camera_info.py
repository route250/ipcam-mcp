"""Boundary checks for the bounded, read-only HTTP metadata client."""

import contextlib
import http.server
import threading
import unittest
from unittest import mock

from tools.camera_info import CameraInfoError, get_camera_info


def sysinfo(version=b"V3.1.6-Tuya_230309"):
    data = bytearray(320)
    data[0:12] = b"PRIVATE-DATA"
    data[136:136 + len(version)] = version
    return bytes(data)


@contextlib.contextmanager
def camera(responses):
    requests = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            status, headers, body = responses[self.path]
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port, requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


class CameraInfoTests(unittest.TestCase):
    def responses(self):
        return {"/Version.txt": (200, {}, b"3.1.6"),
                "/device_model.txt": (200, {}, b"IPC-G3B2D0-TY\n"),
                "/SysInfo": (200, {}, sysinfo())}

    def query(self, responses):
        with camera(responses) as (port, requests):
            return get_camera_info("127.0.0.1", port=port), requests

    def test_only_validated_metadata_is_returned(self):
        with mock.patch.dict("os.environ", {"http_proxy": "http://127.0.0.1:1",
                                            "HTTP_PROXY": "http://127.0.0.1:1",
                                            "ALL_PROXY": "http://127.0.0.1:1",
                                            "NO_PROXY": "", "no_proxy": ""}):
            info, requests = self.query(self.responses())
        self.assertEqual(requests, ["/Version.txt", "/device_model.txt", "/SysInfo"])
        self.assertEqual(info["firmware_version"], "V3.1.6-Tuya_230309")
        self.assertEqual(info["model"], "IPC-G3B2D0-TY")
        self.assertNotIn("PRIVATE-DATA", repr(info))

    def test_redirect_is_rejected_without_following(self):
        responses = self.responses()
        responses["/Version.txt"] = (302, {"Location": "/private"}, b"PRIVATE-DATA")
        with camera(responses) as (port, requests):
            with self.assertRaisesRegex(CameraInfoError, "HTTP 200, received 302"):
                get_camera_info("127.0.0.1", port=port)
            self.assertEqual(requests, ["/Version.txt"])

    def test_response_size_bound_with_and_without_content_length(self):
        for headers in [{}, {"Content-Length": "65"}]:
            with self.subTest(headers=headers):
                responses = self.responses()
                responses["/Version.txt"] = (200, headers, b"X" * 65)
                with self.assertRaisesRegex(CameraInfoError, "exceeds 64"):
                    self.query(responses)

    def test_invalid_or_truncated_content_length(self):
        for headers in [{"Content-Length": "bad"}, {"Content-Length": "8"}]:
            with self.subTest(headers=headers):
                responses = self.responses()
                responses["/Version.txt"] = (200, headers, b"3.1.6")
                with self.assertRaises(CameraInfoError):
                    self.query(responses)

    def test_text_rejects_empty_non_ascii_and_control_bytes(self):
        for body in [b"", b"\xff", b"3.1\x001", b"3.1\n6"]:
            with self.subTest(body=body):
                responses = self.responses()
                responses["/device_model.txt"] = (200, {}, body)
                with self.assertRaises(CameraInfoError):
                    self.query(responses)

    def test_unknown_binary_layouts_are_rejected(self):
        wrong_padding = bytearray(sysinfo())
        wrong_padding[199] = 1
        bodies = [sysinfo()[:-1], sysinfo() + b"X", bytes(320),
                  sysinfo(b"X" * 64), bytes(wrong_padding)]
        for body in bodies:
            with self.subTest(length=len(body)):
                responses = self.responses()
                responses["/SysInfo"] = (200, {}, body)
                with self.assertRaises(CameraInfoError):
                    self.query(responses)


if __name__ == "__main__":
    unittest.main()
