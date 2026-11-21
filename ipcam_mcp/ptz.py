"""Shared browser and MCP pan/tilt control."""

import asyncio
from collections import defaultdict
from contextlib import suppress
import logging

from tools import hank_ptz, ipcam_angles
from . import camera
from .state import READ_ERRORS, Sample

LOG = logging.getLogger(__name__)
DIRECTIONS = dict(up="U", down="D", left="L", right="R")
POSITION_TOLERANCE = 20


class ControlError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code

    def response(self):
        return dict(code=self.code, message=str(self))


def stop_error():
    return ControlError("stop_unconfirmed", "The camera did not acknowledge stop")


class Controller:
    def __init__(self, state):
        self.state = state
        self.owner = None
        self.host = None
        self.task = None
        self.recovery = None
        self.closed = False
        self.desired = None
        self.updated = asyncio.Event()
        self.deadline = 0
        self.lease = 0
        self.expired = set()
        self.subscribers = {}

    def broadcast(self, state):
        self.state.ptz_state = state
        for queue in self.subscribers.values():
            self.notify(queue, dict(type="state", ptz_state=state))

    @staticmethod
    def notify(queue, message):
        if queue.full():
            queue.get_nowait()
        queue.put_nowait(message)

    def reserve(self, owner, manual):
        if self.state.ptz_state == "stop_unconfirmed":
            raise stop_error()
        if self.owner is not None:
            raise ControlError("busy", "Another pan/tilt operation owns the camera")
        if self.closed or not self.state.host or (manual and not self.state.current_frame()):
            raise ControlError("camera_unavailable", "The camera is unavailable")
        self.owner, self.host = owner, self.state.host
        self.broadcast("manual" if manual else "goto")

    def drive(self, owner, direction):
        if direction not in DIRECTIONS:
            raise ControlError("invalid_argument", "Unknown drive direction")
        if owner in self.expired:
            raise ControlError("motion_failed", "Release the direction pad before moving again")
        now = asyncio.get_running_loop().time()
        if self.owner is owner:
            if not self.desired or now >= min(self.deadline, self.lease):
                raise ControlError("busy", "The previous movement is stopping")
            self.desired = direction
            self.deadline = now + 0.5
            self.updated.set()
            return
        self.reserve(owner, manual=True)
        self.desired = direction
        self.deadline, self.lease = now + 0.5, now + 10
        self.task = asyncio.create_task(self.manual(owner, self.host))

    def stop_manual(self, owner):
        self.expired.discard(owner)
        if self.owner is owner:
            self.desired = None
            self.updated.set()

    def stop_manual_for_video(self):
        if self.state.ptz_state == "manual":
            self.desired = None
            self.updated.set()

    async def acknowledge_stop(self, host):
        for _ in range(2):
            try:
                await camera.motor(host, "H")
                return True
            except READ_ERRORS as error:
                LOG.warning("PTZ stop failed at %s (%s)", host, type(error).__name__)
        return False

    async def finish(self, host, stop_failed=False):
        self.broadcast("stopping")
        acknowledged = not stop_failed and await self.acknowledge_stop(host)
        self.owner = self.host = self.task = None
        self.desired = None
        if acknowledged:
            self.broadcast("idle")
        else:
            self.broadcast("stop_unconfirmed")
            if not self.closed:
                self.recovery = asyncio.create_task(self.recover(host))
        return acknowledged

    async def recover(self, host):
        while True:
            await asyncio.sleep(5)
            try:
                await camera.motor(host, "H")
            except READ_ERRORS:
                continue
            self.broadcast("idle")
            return

    async def manual(self, owner, host):
        move = None
        active = None
        failure = None
        loop = asyncio.get_running_loop()
        try:
            while self.desired:
                expires = min(self.deadline, self.lease)
                if loop.time() >= expires:
                    if loop.time() >= self.lease:
                        self.expired.add(owner)
                    raise ControlError("motion_failed", "The manual movement expired")
                if active != self.desired:
                    if move:
                        move.cancel()
                        await asyncio.gather(move, return_exceptions=True)
                        move = None
                    if not await self.acknowledge_stop(host):
                        raise stop_error()
                    if not self.desired or loop.time() >= min(self.deadline, self.lease):
                        continue
                    active = self.desired
                    move = asyncio.create_task(camera.motor(
                        host, DIRECTIONS[active],
                        can_send=lambda: (self.desired == active
                                          and loop.time() < min(self.deadline, self.lease)
                                          and self.state.current_frame() is not None),
                    ))
                    continue
                if move and move.done():
                    move.result()
                    move = None
                self.updated.clear()
                wake = asyncio.create_task(self.updated.wait())
                try:
                    await asyncio.wait([wake, move] if move else [wake],
                                       timeout=max(0, expires - loop.time()),
                                       return_when=asyncio.FIRST_COMPLETED)
                finally:
                    wake.cancel()
                    await asyncio.gather(wake, return_exceptions=True)
        except ControlError as error:
            failure = error
        except READ_ERRORS as error:
            LOG.warning("PTZ manual direction failed at %s (%s)", host, type(error).__name__)
            failure = ControlError("motion_failed", "The camera did not acknowledge movement")
        finally:
            if move:
                move.cancel()
                await asyncio.gather(move, return_exceptions=True)
            if not await self.finish(host, failure is not None and failure.code == "stop_unconfirmed"):
                failure = stop_error()
            queue = self.subscribers.get(owner)
            if failure and queue:
                self.notify(queue, dict(type="error", ok=False, **failure.response()))

    async def read_steps(self, host):
        try:
            steps = await self.state.read_steps(host)
        except READ_ERRORS as error:
            LOG.debug("Position feedback failed at %s: %s", host, error)
            raise ControlError("position_unavailable", "Position feedback is unavailable") from None
        self.state.position = Sample.now(steps)
        return steps

    async def settled_steps(self, host):
        try:
            async with asyncio.timeout(3):
                await asyncio.sleep(0.35)
                previous, equal = None, 0
                while equal < 3:
                    try:
                        current = await self.read_steps(host)
                    except ControlError:
                        previous, equal = None, 0
                    else:
                        equal = equal + 1 if current == previous else 1
                        previous = current
                    if equal < 3:
                        await asyncio.sleep(0.15)
                return current
        except TimeoutError:
            raise ControlError("position_unavailable", "The position did not settle within 3 seconds") from None

    async def pulse(self, host, direction, duration):
        # This transport always sends stop in a finally block, even on a failed move.
        task = asyncio.create_task(asyncio.to_thread(
            hank_ptz.pulse, direction, host=host, duration=duration))
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await asyncio.shield(task)
            raise

    async def feedback(self, host, calibration, target):
        corrections = defaultdict(list)
        if not await self.acknowledge_stop(host):
            raise stop_error()
        steps = await self.settled_steps(host)
        for attempt in range(13):
            errors = {key: target[key] - steps[key] for key in target}
            axis = max(errors, key=lambda key: abs(errors[key]))
            if abs(errors[axis]) <= POSITION_TOLERANCE:
                steps = await self.settled_steps(host)
                if all(abs(steps[key] - target[key]) <= POSITION_TOLERANCE for key in target):
                    return dict(reached=True, position=self.state.status()["position"], target_steps=target,
                                error_steps={key: steps[key] - target[key] for key in target},
                                tolerance_steps=POSITION_TOLERANCE)
                continue
            if attempt == 12:
                break
            pan = axis == "pan_steps"
            delta = errors[axis]
            direction = ("R" if delta > 0 else "L") if pan else ("D" if delta > 0 else "U")
            rate = calibration["pan_steps_per_second" if pan else "tilt_steps_per_second"]
            history = corrections[axis]
            # Reversing twice indicates that the requested precision is below
            # the reliably achievable pulse size; stop rather than oscillate.
            directions = [entry[0] for entry in history] + [direction]
            crossings = sum(a != b for a, b in zip(directions, directions[1:]))
            if crossings >= 2:
                break
            duration = min(1.0, max(0.02, 0.7 * abs(delta) / rate))
            await self.pulse(host, direction, duration)
            current = await self.settled_steps(host)
            LOG.debug("PTZ pulse %s %.3f s: %s → %s (target %s)",
                      direction, duration, steps, current, target)
            history.append((direction, current != steps))
            if len(history) >= 3 and not any(progress for _, progress in history[-3:]):
                break
            steps = current
        raise ControlError("motion_failed", "The camera did not reach the target")

    async def goto(self, pan, tilt):
        try:
            if any(value is not None and type(value) not in (int, float) for value in (pan, tilt)):
                raise ValueError("Angles must be JSON numbers")
            ipcam_angles.validate_angles(pan, tilt)
        except (ValueError, OverflowError) as error:
            raise ControlError("invalid_argument", str(error)) from None
        operation = asyncio.create_task(self.goto_operation(pan, tilt))
        try:
            return await asyncio.shield(operation)
        except asyncio.CancelledError:
            operation.cancel()
            # MCP uses an AnyIO cancellation scope which can cancel each subsequent await.
            # The independent job retains ownership while its final stop runs.
            raise

    async def goto_operation(self, pan, tilt):
        if self.state.host and not self.state.calibration:
            raise ControlError("calibration_required", "The camera has no matching step calibration")
        self.reserve(object(), manual=False)
        host, calibration = self.host, self.state.calibration
        self.task = asyncio.current_task()
        moved = False
        stop_failed = False
        try:
            async with asyncio.timeout(60):
                steps = await self.read_steps(host)
                try:
                    target = ipcam_angles.target_steps(calibration, steps, pan, tilt)
                except ValueError as error:
                    raise ControlError("invalid_argument", str(error)) from None
                moved = True
                return await self.feedback(host, calibration, target)
        except ControlError as error:
            stop_failed = error.code == "stop_unconfirmed"
            raise
        except READ_ERRORS as error:
            LOG.info("Angle movement failed at %s (%s)", host, type(error).__name__)
            raise ControlError("motion_failed", "Angle movement failed or expired") from None
        finally:
            if moved:
                cleanup = asyncio.create_task(self.finish(host, stop_failed))
                self.task = cleanup
                if not await asyncio.shield(cleanup):
                    raise stop_error()
            else:
                self.owner = self.host = self.task = None
                self.broadcast("idle")

    async def cancel_camera(self, host):
        if self.host != host or not self.task:
            return
        task = self.task
        if self.state.ptz_state == "manual":
            self.stop_manual(self.owner)
        elif self.state.ptz_state == "goto":
            task.cancel()
        with suppress(asyncio.CancelledError, ControlError):
            await asyncio.shield(task)
        if self.task and self.task is not task and self.state.ptz_state == "stopping":
            await asyncio.shield(self.task)

    async def close(self):
        self.closed = True
        await self.cancel_camera(self.host)
        if self.recovery:
            self.recovery.cancel()
            await asyncio.gather(self.recovery, return_exceptions=True)
