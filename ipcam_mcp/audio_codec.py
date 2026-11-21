"""G.711 μ-law conversion for signed 16-bit little-endian PCM."""

from bisect import bisect_right
import struct

RATE = 8000


def decode_mulaw_sample(code):
    value = code ^ 255
    magnitude = (((value & 15) << 3) + 132) << ((value >> 4) & 7)
    return 132 - magnitude if value & 128 else magnitude - 132


def make_mulaw_table():
    # Use the same quantization as the verified speaker client.
    thresholds = [(decode_mulaw_sample(index ^ 255) + decode_mulaw_sample((index + 1) ^ 255) + 4) >> 3
                  for index in range(127)]
    table = bytearray(16384)
    table[8192] = 255
    for magnitude in range(1, 8192):
        code = bisect_right(thresholds, magnitude)
        table[8192 + magnitude] = code ^ 255
        table[8192 - magnitude] = code ^ 127
    table[0] = table[1]
    return bytes(table)


MULAW = make_mulaw_table()


def encode_mulaw(pcm):
    return bytes(MULAW[(sample + 32768) >> 2] for (sample,) in struct.iter_unpack("<h", pcm))


_PCM16 = tuple(struct.pack("<h", decode_mulaw_sample(code)) for code in range(256))


def decode_mulaw(payload):
    return b"".join(_PCM16[code] for code in payload)
