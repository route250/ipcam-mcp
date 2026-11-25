"""LAN transport for the previously verified camera commands."""

import asyncio
from contextlib import asynccontextmanager
import struct
import time

from tools import camera_info, hank_camera, hank_environment, hank_feature_packets, hank_ptz

PORT = 90
HTTP_PORT = 10081
TIMEOUT = 1
MAX_JPEG = 1024 * 1024
SUBSTREAM = bytes.fromhex("494e4603000100010000001804030500") + bytes(20)
SPEAKER_START = bytes.fromhex("494e4607000100010000001800020100") + bytes(20)
SPEAKER_FRAME_SAMPLES = 320
MIC_FRAME_FORMAT = bytes.fromhex("d3020101")
MIC_SAMPLE_RATES = (8000, 16000)
MIC_FRAME_BYTES = 320


class CameraError(Exception):
    """A camera or decoder response was unavailable or invalid."""


async def read_frame(reader, timeout=TIMEOUT):
    async with asyncio.timeout(timeout):
        header = await reader.readexactly(12)
        if header[:3] != b"INF" or header[4:8] != b"\x00\x01\x00\x01":
            raise CameraError("invalid INF header")
        size = int.from_bytes(header[8:], "big")
        if size > hank_camera.MAX_BODY_BYTES:
            raise CameraError("INF body exceeds size limit")
        return header[3], await reader.readexactly(size)


@asynccontextmanager
async def login(host):
    async with asyncio.timeout(TIMEOUT):
        reader, writer = await asyncio.open_connection(host, PORT)
    try:
        writer.write(hank_camera.PACKET1)
        command, body = await read_frame(reader)
        if command != 2 or len(body) != 8:
            raise CameraError("invalid login response")
        session, status, reserved = struct.unpack(">IHH", body)
        if not session or status or reserved:
            raise CameraError("login rejected")
        yield reader, writer
    finally:
        writer.close()


def check_model(host):
    name = camera_info._read_file(host, HTTP_PORT, TIMEOUT, "/device_model.txt")
    if name.rstrip(b"\r\n") != b"IPC-G3B2D0-TY":
        raise CameraError("unexpected device model")


async def set_time(host):
    packet = hank_feature_packets.time_set(int(time.time()))
    async with login(host) as (reader, writer):
        writer.write(packet)
        command, body = await read_frame(reader)
        if command != 101 or len(body) != 84 or body[:80] != packet[12:92] or body[80] != 0:
            raise CameraError("invalid TimeInfo acknowledgement")


async def read_detection_scope(host, selector):
    async with login(host) as (reader, writer):
        writer.write(hank_feature_packets.scope_get(selector))
        command, body = await read_frame(reader)
    if (command != 58 or len(body) != 76 or body[:2] != b"\x02\x16"
            or body[2] not in (15, 18) or body[3] != 0):
        raise CameraError("invalid detection scope response")
    return body[2], body[4:]


async def clear_detection_scopes(host):
    # The camera can close the connection after a scope request.
    for selector, detector in ((0, "MD"), (1, "PD")):
        height, mask = await read_detection_scope(host, selector)
        if not any(mask):
            continue
        async with login(host) as (reader, writer):
            writer.write(hank_feature_packets.scope_clear(selector, height))
            if await read_frame(reader, timeout=5) != (56, b"\x00\x00"):
                raise CameraError(f"{detector} detection scope update rejected")
        _, mask = await read_detection_scope(host, selector)
        if any(mask):
            raise CameraError(f"{detector} detection scope is not empty after update")


async def environment(host):
    async with login(host) as (reader, writer):
        writer.write(hank_environment.GET_PACKET)
        return hank_environment._decode(*await read_frame(reader))


@asynccontextmanager
async def speaker(host):
    async with login(host) as (reader, writer):
        try:
            writer.write(SPEAKER_START)
            command, body = await read_frame(reader)
            if (command != 8 or len(body) != 24 or body[1:3] != b"\x02\x01"
                    or body[20:22] != b"\x00\x00"):
                raise CameraError("speaker start rejected")
            yield writer
        except asyncio.CancelledError:
            writer.transport.abort()
            raise
        finally:
            writer.close()
            try:
                async with asyncio.timeout(TIMEOUT):
                    await writer.wait_closed()
            except (TimeoutError, asyncio.CancelledError):
                writer.transport.abort()
                raise


async def send_speaker_frame(writer, payload, sequence, *, sample_rate=8000):
    interval_ms = SPEAKER_FRAME_SAMPLES * 1000 // sample_rate
    metadata = bytes.fromhex("d3020100") + struct.pack(
        ">HHIIII", sample_rate, 16, sequence, len(payload), (sequence - 1) * interval_ms, 0)
    header = bytes.fromhex("494e460400010001") + struct.pack(">I", len(metadata) + len(payload))
    writer.write(header + metadata + payload)
    async with asyncio.timeout(TIMEOUT):
        await writer.drain()


async def motor(host, direction, can_send=None):
    async with login(host) as (reader, writer):
        if can_send is not None and not can_send():
            return
        write_direction(writer, direction)
        await motor_ack(reader)


def write_direction(writer, direction):
    writer.write(hank_ptz._packet(direction))


async def motor_ack(reader):
    if await read_frame(reader) != (16, bytes(4)):
        raise CameraError("invalid PTZ acknowledgement")


def media_payload(command, body):
    if command not in (4, 6) or len(body) < 24:
        raise CameraError("invalid media frame")
    payload = body[24:]
    if int.from_bytes(body[12:16], "big") != len(payload):
        raise CameraError("invalid media payload length")
    if command == 6 and (
            body[0] not in (0xD1, 0xD2) or body[1:4] != bytes.fromhex("01050f")
            or struct.unpack(">HH", body[4:8]) != (640, 360)
            or not payload.startswith((b"\x00\x00\x01", b"\x00\x00\x00\x01"))):
        raise CameraError("invalid H.264 substream frame")
    return payload


async def feed_media(reader, destination, publish_audio):
    while True:
        command, body = await read_frame(reader, 5)
        payload = media_payload(command, body)
        if command == 6:
            destination.write(payload)
            await destination.drain()
        elif (len(payload) == MIC_FRAME_BYTES and body[:4] == MIC_FRAME_FORMAT
              and body[6:8] == b"\x00\x10" and body[20:24] == b"\0" * 4):
            sample_rate = int.from_bytes(body[4:6], "big")
            if sample_rate in MIC_SAMPLE_RATES:
                publish_audio(body[8:12], payload, sample_rate)


async def publish_jpegs(reader, publish):
    while True:
        async with asyncio.timeout(5):
            if await reader.readline() != b"--ffmpeg\r\n":
                raise CameraError("invalid FFmpeg multipart boundary")
            if (await reader.readline()).lower() != b"content-type: image/jpeg\r\n":
                raise CameraError("invalid FFmpeg frame type")
            key, sep, value = (await reader.readline()).strip().partition(b": ")
            if key.lower() != b"content-length" or not sep or not value.isdigit() or len(value) > 7:
                raise CameraError("invalid FFmpeg frame length")
            length = int(value)
            if not 4 <= length <= MAX_JPEG or await reader.readline() != b"\r\n":
                raise CameraError("invalid FFmpeg frame header")
            jpeg = await reader.readexactly(length)
            if not jpeg.startswith(b"\xff\xd8") or not jpeg.endswith(b"\xff\xd9"):
                raise CameraError("invalid JPEG")
            if await reader.readexactly(2) != b"\r\n":
                raise CameraError("invalid FFmpeg frame separator")
        publish(jpeg)


async def stream(host, publish, publish_audio):
    async with login(host) as (reader, writer):
        decoder = await asyncio.create_subprocess_exec(
            "ffmpeg", "-nostdin", "-loglevel", "error", "-threads", "1",
            "-probesize", "32768", "-analyzeduration", "100000", "-f", "h264", "-i", "pipe:0",
            "-an", "-c:v", "mjpeg", "-threads", "1", "-q:v", "3",
            "-flush_packets", "1", "-f", "mpjpeg", "pipe:1",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            writer.write(SUBSTREAM)
            async with asyncio.TaskGroup() as tasks:
                tasks.create_task(feed_media(reader, decoder.stdin, publish_audio))
                tasks.create_task(publish_jpegs(decoder.stdout, publish))
        finally:
            decoder.stdin.close()
            if decoder.returncode is None:
                try:
                    decoder.kill()
                except ProcessLookupError:
                    pass
            await decoder.communicate()
