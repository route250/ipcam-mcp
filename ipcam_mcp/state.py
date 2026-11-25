"""Camera discovery, telemetry and the latest-frame cache."""

import asyncio
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from ipaddress import IPv4Address, IPv4Network
import json
import logging
import os
from pathlib import Path
import re
from tempfile import NamedTemporaryFile
import time

from tinytuya.core import decrypt_udp
from tools import camera_info, hank_environment, hank_ptz, ipcam_angles
from . import camera
from .mic import Microphone

LOG = logging.getLogger(__name__)
PRIVATE_NETWORKS = tuple(map(IPv4Network, ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")))
RETRY_DELAYS = (1, 2, 4, 8, 16, 30)
READ_ERRORS = (OSError, ValueError, camera.CameraError, camera_info.CameraInfoError,
               hank_environment.EnvironmentReadError, ipcam_angles.PTZError, EOFError)


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def camera_mac_address(host):
    """Return the Linux ARP entry for a directly reachable camera, if available."""
    try:
        entries = Path("/proc/net/arp").read_text().splitlines()[1:]
    except OSError:
        return None
    for entry in entries:
        ip, kind, flags, mac, *_ = entry.split()
        if (ip == host and kind == "0x1" and int(flags, 16) & 2
                and mac != "00:00:00:00:00:00"
                and re.fullmatch(r"(?:[0-9a-f]{2}:){5}[0-9a-f]{2}", mac)):
            return mac
    return None


@dataclass(frozen=True)
class Settings:
    lan: IPv4Network
    camera_host: str | None
    device_id: str | None
    state_path: Path
    mic_source: str = "camera"
    usb_mic_device: str | None = None
    disable_detection_regions: bool = False

    def accepts(self, host):
        try:
            address = IPv4Address(host)
        except ValueError:
            return False
        excluded = (self.lan.network_address, self.lan.broadcast_address) if self.lan.prefixlen < 31 else ()
        return address in self.lan and address not in excluded

    @classmethod
    def load(cls, path):
        data = json.loads(path.read_text())
        keys = {"lan_cidr", "camera_host", "device_id", "calibration_path", "mic_source", "usb_mic_device",
                "disable_detection_regions"}
        if not isinstance(data, dict) or set(data) - keys:
            raise ValueError("invalid configuration keys")
        lan = IPv4Network(data["lan_cidr"])
        if not any(lan.subnet_of(network) for network in PRIVATE_NETWORKS):
            raise ValueError("lan_cidr must be within an RFC 1918 IPv4 network")
        host, device_id = data.get("camera_host"), data.get("device_id")
        if device_id is not None and (not isinstance(device_id, str) or not device_id.strip()):
            raise ValueError("device_id must be a nonempty string")
        mic_source = data.get("mic_source", "camera")
        usb_mic_device = data.get("usb_mic_device")
        if mic_source not in ("camera", "usb"):
            raise ValueError("mic_source must be camera or usb")
        if usb_mic_device is not None and (not isinstance(usb_mic_device, str)
                                           or not usb_mic_device.strip() or "\0" in usb_mic_device):
            raise ValueError("usb_mic_device must be a nonempty ALSA PCM name")
        if mic_source == "usb" and usb_mic_device is None:
            raise ValueError("usb_mic_device is required when mic_source is usb")
        disable_detection_regions = data.get("disable_detection_regions", False)
        if not isinstance(disable_detection_regions, bool):
            raise ValueError("disable_detection_regions must be a boolean")
        # Accept the old calibration_path key when loading existing setups.
        result = cls(lan, host, device_id, path.with_name("ipcam-server-state.json"),
                     mic_source=mic_source, usb_mic_device=usb_mic_device,
                     disable_detection_regions=disable_detection_regions)
        if host is not None and (not isinstance(host, str) or not result.accepts(host)):
            raise ValueError("camera_host must be a unicast IPv4 address within lan_cidr")
        if not host and not device_id:
            raise ValueError("camera_host or device_id is required")
        return result


class Discovery(asyncio.DatagramProtocol):
    def __init__(self, settings):
        self.settings = settings
        self.host = None
        self.changed = asyncio.Event()

    def datagram_received(self, data, source):
        if not 1 <= len(data) <= 4096 or not self.settings.accepts(source[0]):
            return
        try:
            notice = json.loads(decrypt_udp(data))
        except Exception:
            # The LAN packet may be malformed or belong to an unrelated device.
            return
        if (isinstance(notice, dict) and notice.get("gwId") == self.settings.device_id
                and notice.get("ip") == source[0]):
            if self.host != source[0]:
                LOG.info("Camera discovery broadcast received from %s", source[0])
                self.host = source[0]
                self.changed.set()


@dataclass(frozen=True)
class Sample:
    received: float
    utc: str
    value: object

    @classmethod
    def now(cls, value):
        return cls(time.monotonic(), utc_now(), value)


@dataclass(frozen=True)
class Frame:
    received: float
    jpeg: bytes
    status: dict


class CameraState:
    def __init__(self, settings):
        self.settings = settings
        self.host = None
        self.mac_address = None
        self.position = None
        self.samples = deque()
        self.frame = None
        self.frame_id = 0
        self.frame_changed = asyncio.Event()
        self.microphone = Microphone()
        self.ptz_state = "idle"
        self.position_lock = asyncio.Lock()
        self.position_recovery_attempted = set()

    def current_frame(self, now=None):
        if now is None:
            now = time.monotonic()
        return self.frame if self.frame and now - self.frame.received < 5 else None

    def status(self, now=None, utc=None, frame=None):
        if now is None:
            now = time.monotonic()
        if utc is None:
            utc = utc_now()
        if frame is None:
            snapshot = self.current_frame(now)
            frame = snapshot.status["frame"] if snapshot else None
        position = dict(status="unavailable", sampled_at_utc=None, pan_steps=None, tilt_steps=None,
                        pan_degrees=None, tilt_degrees=None)
        if (self.ptz_state not in {"homing", "homing_unconfirmed"}
                and self.position and now - self.position.received <= 2):
            steps = self.position.value
            position.update(steps, **ipcam_angles.angles_from_steps(steps),
                            status="ok", sampled_at_utc=self.position.utc)
        samples = [sample for sample in self.samples if now - 5 < sample.received <= now]
        count = len(samples)
        environment = dict(status="unavailable", sampled_at_utc=None, temperature_c=None,
                           relative_humidity_percent=None, sample_count=count)
        if count:
            environment.update(status="ok", sampled_at_utc=samples[-1].utc,
                               temperature_c=sum(s.value[0] for s in samples) / count,
                               relative_humidity_percent=sum(s.value[1] for s in samples) / count)
        return dict(at_utc=utc, state="ready" if frame else "connecting" if self.host else "discovering",
                    camera=dict(ip_address=self.host, mac_address=self.mac_address),
                    frame=frame, ptz_state=self.ptz_state, position=position, environment=environment)

    def _notify_frame(self):
        self.frame_changed.set()
        self.frame_changed = asyncio.Event()

    def publish(self, jpeg):
        self.frame_id += 1
        now, utc = time.monotonic(), utc_now()
        metadata = dict(id=self.frame_id, received_at_utc=utc, width=640, height=360)
        self.frame = Frame(now, jpeg, self.status(now, utc, metadata))
        self._notify_frame()

    def clear_frame(self):
        self.frame = None
        self._notify_frame()

    async def read_steps(self, host):
        async with self.position_lock:
            return await asyncio.to_thread(ipcam_angles.read_steps, dict(host=host, timeout=camera.TIMEOUT))

    async def position_sample(self, host):
        steps = await self.read_steps(host)
        self.position = Sample.now(steps)

    async def environment_sample(self, host):
        sample = Sample.now(await camera.environment(host))
        while self.samples and self.samples[0].received <= sample.received - 5:
            self.samples.popleft()
        self.samples.append(sample)

    async def poll(self, probe, host):
        while True:
            start = asyncio.get_running_loop().time()
            try:
                await probe(host)
            except READ_ERRORS as error:
                LOG.debug("%s failed for %s (%s)", probe.__name__, host, type(error).__name__)
            elapsed = asyncio.get_running_loop().time() - start
            await asyncio.sleep(1 - elapsed % 1)

    def saved_host(self):
        if not self.settings.device_id:
            return None
        try:
            data = json.loads(self.settings.state_path.read_text())
            host = data["camera_host"]
            if (data["device_id"] == self.settings.device_id and data["lan_cidr"] == str(self.settings.lan)
                    and isinstance(host, str) and self.settings.accepts(host)):
                return host
        except (OSError, ValueError, TypeError, KeyError):
            pass
        return None

    def save_host(self, host):
        destination = self.settings.state_path
        temp = None
        try:
            with NamedTemporaryFile(mode="w", dir=destination.parent, delete=False) as file:
                temp = Path(file.name)
                json.dump(dict(camera_host=host, device_id=self.settings.device_id,
                               lan_cidr=str(self.settings.lan)), file)
                file.flush()
                os.fsync(file.fileno())
            temp.replace(destination)
        except OSError:
            LOG.warning("Cannot save the adopted camera address", exc_info=True)
        finally:
            if temp:
                temp.unlink(missing_ok=True)

    async def adopt(self, host):
        await asyncio.to_thread(camera.check_model, host)
        await camera.set_time(host)
        try:
            steps = await self.read_steps(host)
        except ipcam_angles.PositionNotFound:
            if host in self.position_recovery_attempted:
                raise
            self.position_recovery_attempted.add(host)
            LOG.info("Position file missing at %s; attempting up and down pulses", host)
            for direction in ("U", "D"):
                await asyncio.to_thread(hank_ptz.pulse, direction, host=host, duration=0.1)
            await asyncio.sleep(0.5)
            steps = await self.read_steps(host)
        position = Sample.now(steps)
        self.position = position
        self.host = host
        self.mac_address = camera_mac_address(host)
        await asyncio.to_thread(self.save_host, host)
        LOG.info("Camera adopted at %s", host)

    async def connected(self, host, controller, discovery):
        failures = 0
        polls = [asyncio.create_task(self.poll(probe, host))
                 for probe in (self.position_sample, self.environment_sample)]
        try:
            while failures < 3:
                previous_frame = self.frame_id
                try:
                    if self.settings.disable_detection_regions:
                        await camera.clear_detection_scopes(host)
                    await camera.stream(host, self.publish, self.microphone.publish)
                except (ExceptionGroup, *READ_ERRORS) as error:
                    detail = error.exceptions[0] if isinstance(error, ExceptionGroup) else error
                    LOG.warning("Video disconnected from %s: %s", host, detail)
                finally:
                    self.clear_frame()
                    self.microphone.reset()
                    controller.stop_manual_for_video()
                if discovery.changed.is_set() and discovery.host != host:
                    break
                failures = 0 if self.frame_id != previous_frame else failures + 1
                if failures < 3:
                    await asyncio.sleep(RETRY_DELAYS[max(0, failures - 1)])
        finally:
            for poll in polls:
                poll.cancel()
            await asyncio.gather(*polls, return_exceptions=True)
            await controller.cancel_camera(host)
            controller.forget_directions(host)
            self.position = None
            self.samples.clear()
            self.host = None
            self.mac_address = None

    async def run(self, controller):
        protocol = Discovery(self.settings)
        udp = None
        if self.settings.device_id:
            try:
                udp, _ = await asyncio.get_running_loop().create_datagram_endpoint(
                    lambda: protocol, local_addr=("0.0.0.0", 6667))
            except OSError:
                LOG.warning("Cannot listen for discovery on UDP 6667", exc_info=True)
        failures = 0
        try:
            while True:
                protocol.changed.clear()
                saved = await asyncio.to_thread(self.saved_host)
                candidates = dict.fromkeys((protocol.host, self.settings.camera_host, saved))
                for host in filter(None, candidates):
                    try:
                        await self.adopt(host)
                    except READ_ERRORS as error:
                        self.position = None
                        LOG.warning("Camera candidate %s rejected: %s", host, error)
                        continue
                    failures = 0
                    await self.connected(host, controller, protocol)
                    break
                try:
                    async with asyncio.timeout(RETRY_DELAYS[min(failures, len(RETRY_DELAYS) - 1)]):
                        await protocol.changed.wait()
                except TimeoutError:
                    pass
                failures += 1
        finally:
            if udp:
                udp.close()
