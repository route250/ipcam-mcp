"""Wire layout regression checks, independent of camera state or credentials."""
import importlib.util
from pathlib import Path
import unittest

SPEC = importlib.util.spec_from_file_location(
    "hank_feature_packets", Path(__file__).resolve().parents[1] / "tools/hank_feature_packets.py"
)
packets = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(packets)


class FeaturePacketsTest(unittest.TestCase):
    def test_time_native_epoch_after_40_unused_bytes(self):
        packet = packets.time_set(0x12345678)
        self.assertEqual(packet[:12].hex(), "494e46640001000100000084")
        self.assertEqual(len(packet), 144)
        self.assertEqual(packet[12:28], b"set" + bytes(13))
        self.assertEqual(packet[28:92], b"TimeInfo" + bytes(56))
        self.assertEqual(packet[92:140], bytes(48))
        self.assertEqual(packet[140:], b"\x78\x56\x34\x12")

    def test_rejects_epoch_outside_signed_32_bit_range(self):
        with self.assertRaises(ValueError):
            packets.time_set(0x80000000)


if __name__ == "__main__":
    unittest.main()
