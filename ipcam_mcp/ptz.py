"""Shared browser and MCP pan/tilt control."""

import asyncio
from contextlib import suppress
import logging
import math
from statistics import median
from threading import Event

from tools import hank_ptz, ipcam_angles
from . import camera
from .state import READ_ERRORS, Sample

LOG = logging.getLogger(__name__)
DIRECTIONS = dict(up="U", down="D", left="L", right="R")
POSITION_TOLERANCE = 20
ANGLE_INCREMENT = 20
MAX_RETRIES = 3
INITIAL_SPEEDS = {"L": 388.58875935930246, "R": 388.58875935930246, "U": 235.0, "D": 235.0}
MOTOR_AXES = {"L": ("pan_steps", -1), "R": ("pan_steps", 1),
              "U": ("tilt_steps", -1), "D": ("tilt_steps", 1)}
AXIS_DIRECTIONS = {"pan_steps": ("L", "R"), "tilt_steps": ("U", "D")}
REVERSAL_SECONDS = {"pan_steps": 0.300, "tilt_steps": 0.390}
TILT_MIN_PULSE_STEPS = 61
TILT_APPROACH_STEPS = 110
MIN_LEARNING_SECONDS = 0.25
SPEED_CONFIRMATIONS = 3
HOMING_TIMEOUT = 30
HOMING_REFRESH_PULSE = 0.1


class ControlError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code

    def response(self):
        return dict(code=self.code, message=str(self))


def stop_error():
    return ControlError("stop_unconfirmed", "The camera did not acknowledge stop")


def round_target_angle(value, low, high):
    multiple, remainder = divmod(abs(value), ANGLE_INCREMENT)
    if remainder * 2 >= ANGLE_INCREMENT:
        multiple += 1
    angle = math.copysign(multiple * ANGLE_INCREMENT, value)
    lowest = math.ceil(low / ANGLE_INCREMENT) * ANGLE_INCREMENT
    highest = math.floor(high / ANGLE_INCREMENT) * ANGLE_INCREMENT
    return min(highest, max(lowest, angle))


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
        self.speeds = {}
        self.speed_samples = {}
        self.last_directions = {}

    def forget_directions(self, host):
        for axis in AXIS_DIRECTIONS:
            self.last_directions.pop((host, axis), None)

    def broadcast(self, state):
        self.state.ptz_state = state
        for queue in self.subscribers.values():
            self.notify(queue, dict(type="state", ptz_state=state))

    @staticmethod
    def notify(queue, message):
        if queue.full():
            queue.get_nowait()
        queue.put_nowait(message)

    def reserve(self, owner, operation):
        if self.state.ptz_state == "stop_unconfirmed":
            raise stop_error()
        if self.state.ptz_state == "homing_unconfirmed":
            raise ControlError("homing_unconfirmed", "The camera did not confirm the end of homing")
        if self.owner is not None:
            raise ControlError("busy", "Another pan/tilt operation owns the camera")
        if (self.closed or not self.state.host
                or (operation != "goto" and not self.state.current_frame())):
            raise ControlError("camera_unavailable", "The camera is unavailable")
        self.owner, self.host = owner, self.state.host
        self.broadcast(operation)

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
        self.reserve(owner, "manual")
        self.desired = direction
        self.deadline, self.lease = now + 0.5, now + 10
        self.task = asyncio.create_task(self.manual(owner, self.host))

    def stop_manual(self, owner):
        self.expired.discard(owner)
        if self.owner is owner and self.state.ptz_state == "manual":
            self.desired = None
            self.updated.set()

    def stop_manual_for_video(self):
        if self.state.ptz_state == "manual":
            self.desired = None
            self.updated.set()

    async def acknowledge_stop(self, host, can_send=None):
        for _ in range(2):
            try:
                await camera.motor(host, "H", can_send=can_send)
                return True
            except READ_ERRORS as error:
                LOG.warning("PTZ stop failed at %s (%s)", host, type(error).__name__)
        return False

    async def finish(self, host, stop_failed=False, *, stopped=False):
        if self.state.ptz_state != "stopping":
            self.broadcast("stopping")
        acknowledged = not stop_failed and (stopped or await self.acknowledge_stop(host))
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

    def learn_speed(self, host, direction, before, after, duration):
        axis, sign = MOTOR_AXES[direction]
        steps = sign * (after[axis] - before[axis])
        limit = ipcam_angles.STEP_LIMITS[axis] if sign > 0 else 0
        if duration < MIN_LEARNING_SECONDS or steps <= 0 or after[axis] == limit:
            return
        key = (host, direction)
        speed = self.speeds.get(host, INITIAL_SPEEDS)[direction]
        if abs(steps - speed * duration) <= POSITION_TOLERANCE:
            self.speed_samples.pop(key, None)
            return
        measured = steps / duration
        initial = INITIAL_SPEEDS[direction]
        if not initial / 4 <= measured <= initial * 4:
            return
        samples = self.speed_samples.setdefault(key, [])
        if samples and (samples[-1] > speed) != (measured > speed):
            samples.clear()
        samples.append(measured)
        if len(samples) == SPEED_CONFIRMATIONS:
            if max(samples) <= 1.25 * min(samples):
                updated = 0.75 * speed + 0.25 * median(samples)
                self.speeds.setdefault(host, INITIAL_SPEEDS.copy())[direction] = updated
                LOG.debug("PTZ speed %s updated from %.1f to %.1f steps/s", direction, speed, updated)
            self.speed_samples.pop(key)

    async def stop_manual_segment(self, host, direction, before, started):
        if before is None or started is None:
            return await self.acknowledge_stop(host)
        stop_times = []

        def before_stop():
            stop_times.append(asyncio.get_running_loop().time())
            return True

        if not await self.acknowledge_stop(host, before_stop):
            return False
        if len(stop_times) == 1 and stop_times[0] - started >= MIN_LEARNING_SECONDS:
            with suppress(ControlError):
                after = await self.settled_steps(host)
                self.learn_speed(host, DIRECTIONS[direction], before, after, stop_times[0] - started)
        return True

    async def manual(self, owner, host):
        move = None
        active = None
        before = started = None
        failure = None
        loop = asyncio.get_running_loop()

        def before_move():
            nonlocal before, started
            if (self.desired == active and loop.time() < min(self.deadline, self.lease)
                    and self.state.current_frame() is not None):
                direction = DIRECTIONS[active]
                axis, _ = MOTOR_AXES[direction]
                key = (host, axis)
                if self.last_directions.get(key) not in (None, direction):
                    before = None  # A reversal does not measure steady movement speed.
                self.last_directions[key] = direction
                started = loop.time()
                return True
            return False

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
                    if not await self.stop_manual_segment(host, active, before, started):
                        raise stop_error()
                    active = before = started = None
                    if not self.desired or loop.time() >= min(self.deadline, self.lease):
                        continue
                    active = self.desired
                    with suppress(ControlError):
                        before = await self.read_steps(host)
                    move = asyncio.create_task(camera.motor(host, DIRECTIONS[active], can_send=before_move))
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
            if active:
                self.last_directions.pop((host, MOTOR_AXES[DIRECTIONS[active]][0]), None)
            LOG.warning("PTZ manual direction failed at %s (%s)", host, type(error).__name__)
            failure = ControlError("motion_failed", "The camera did not acknowledge movement")
        finally:
            if move:
                move.cancel()
                await asyncio.gather(move, return_exceptions=True)
            self.desired = None
            self.broadcast("stopping")
            stop_failed = failure is not None and failure.code == "stop_unconfirmed"
            stopped = not stop_failed and await self.stop_manual_segment(host, active, before, started)
            if not await self.finish(host, stop_failed or not stopped, stopped=stopped):
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
        cancelled = Event()
        task = asyncio.create_task(asyncio.to_thread(
            hank_ptz.pulse, direction, host=host, duration=duration, cancelled=cancelled.is_set))
        key = (host, MOTOR_AXES[direction][0])
        try:
            timing = await asyncio.shield(task)
        except asyncio.CancelledError:
            self.last_directions.pop(key, None)
            cancelled.set()
            with suppress(hank_ptz.PTZError):
                await asyncio.shield(task)
            raise
        except READ_ERRORS:
            self.last_directions.pop(key, None)
            raise
        self.last_directions[key] = direction
        LOG.debug("PTZ timing %s requested=%.6fs wait=%.6fs stop_setup=%.6fs send_interval=%.6fs "
                  "stop_ack=%.6fs stop_sends=%d",
                  direction, duration, timing["wait_seconds"], timing["stop_setup_seconds"],
                  timing["send_interval_seconds"], timing["stop_ack_seconds"], timing["stop_sends"])

    def start_homing(self, owner):
        self.reserve(owner, "homing")
        self.task = asyncio.create_task(self.homing(owner, self.host))

    async def homing(self, owner, host):
        sent = completed = refreshed = False
        failure = None

        def before_send():
            nonlocal sent
            sent = True
            self.forget_directions(host)
            return True

        try:
            # A position file must exist before we can observe its deletion.
            await self.read_steps(host)
            async with asyncio.timeout(HOMING_TIMEOUT):
                try:
                    await camera.motor(host, "Z", can_send=before_send)
                except READ_ERRORS as error:
                    LOG.info("Homing request failed at %s (%s)", host, type(error).__name__)
                    if not sent:
                        raise ControlError("motion_failed", "Could not send the homing request") from None
                    # A lost ACK can occur after the firmware starts moving.
                while not completed:
                    await asyncio.sleep(1)
                    try:
                        await self.state.read_steps(host)
                    except ipcam_angles.PositionNotFound:
                        completed = True
                    except READ_ERRORS as error:
                        LOG.debug("Homing position check failed at %s (%s)", host, type(error).__name__)
            self.state.position = None
            try:
                # A short movement makes the firmware save both step counters again.
                for direction in ("R", "L"):
                    await self.pulse(host, direction, HOMING_REFRESH_PULSE)
                await self.settled_steps(host)
                refreshed = True
            except (ControlError, *READ_ERRORS) as error:
                LOG.info("Homing position refresh failed at %s (%s)", host, type(error).__name__)
                raise ControlError("position_refresh_failed", "Homing finished but position could not be refreshed") from None
        except ControlError as error:
            failure = error
        except TimeoutError:
            failure = ControlError("homing_unconfirmed", "The camera did not confirm the end of homing")
        finally:
            if sent and not refreshed:
                self.state.position = None
            if completed:
                cleanup = asyncio.create_task(self.finish(host))
                self.task = cleanup
                if not await asyncio.shield(cleanup):
                    failure = stop_error()
            else:
                self.owner = self.host = self.task = None
                self.broadcast("homing_unconfirmed" if sent else "idle")
            queue = self.subscribers.get(owner)
            if queue is not None and (refreshed or failure is not None):
                message = dict(type="homing_result", ok=refreshed and failure is None)
                if failure is not None:
                    message.update(failure.response())
                self.notify(queue, message)

    async def feedback(self, host, current, target, *, skipped_axes=()):
        for attempt in range(1 + MAX_RETRIES):
            for axis, goal in target.items():
                delta = goal - current[axis]
                if axis in skipped_axes or abs(delta) <= POSITION_TOLERANCE:
                    continue
                pulse_goal = goal
                if (axis == "tilt_steps" and attempt < MAX_RETRIES
                        and abs(delta) + POSITION_TOLERANCE < TILT_MIN_PULSE_STEPS):
                    # Short tilt pulses travel about 57–61 steps even on reversal.
                    # Back away first so the final approach can use a longer pulse.
                    waypoint = goal - (TILT_APPROACH_STEPS if delta > 0 else -TILT_APPROACH_STEPS)
                    if 0 <= waypoint <= ipcam_angles.STEP_LIMITS[axis]:
                        pulse_goal = waypoint
                travel = pulse_goal - current[axis]
                direction = AXIS_DIRECTIONS[axis][travel > 0]
                speed = self.speeds.get(host, INITIAL_SPEEDS)[direction]
                previous = self.last_directions.get((host, axis))
                reversal = REVERSAL_SECONDS[axis] if previous not in (None, direction) else 0.0
                duration = min(10.0, max(0.02, abs(travel) / speed) + reversal)
                before = current
                await self.pulse(host, direction, duration)
                current = await self.settled_steps(host)
                if attempt == 0 and pulse_goal == goal:
                    if abs(goal - current[axis]) <= POSITION_TOLERANCE:
                        self.speed_samples.pop((host, direction), None)
                    elif not reversal:
                        self.learn_speed(host, direction, before, current, duration)
                LOG.debug("PTZ attempt %d %s %.3f s: %s → %s (target %s) reversal_extra=%.3fs",
                          attempt + 1, direction, duration, before, current, target, reversal)
            errors = {axis: current[axis] - goal for axis, goal in target.items()}
            if all(axis in skipped_axes or abs(error) <= POSITION_TOLERANCE for axis, error in errors.items()):
                result = dict(reached=True, position=self.state.status()["position"], target_steps=target,
                              error_steps=errors, tolerance_steps=POSITION_TOLERANCE)
                if skipped_axes:
                    result["skipped_axes"] = list(skipped_axes)
                return result
        raise ControlError("motion_failed", "The camera did not reach the target within three retries")

    async def goto(self, pan, tilt, *, steps=False, round_to_20_degrees=True):
        limits = ipcam_angles.STEP_LIMITS if steps else ipcam_angles.ANGLE_LIMITS
        target = {}
        for (axis, bounds), value in zip(limits.items(), (pan, tilt)):
            if value is None:
                continue
            low, high = (0, bounds) if steps else bounds
            valid_type = type(value) is int if steps else type(value) in (int, float)
            if not valid_type or not low <= value <= high:
                kind = "an integer" if steps else "a number"
                raise ControlError("invalid_argument", f"{axis} must be {kind} from {low} to {high}")
            target[axis] = value if steps or not round_to_20_degrees else round_target_angle(value, low, high)
        if not target:
            raise ControlError("invalid_argument", "Specify pan or tilt")
        angle_target = None
        if not steps:
            if round_to_20_degrees:
                angle_target = target
            try:
                target = ipcam_angles.angles_to_steps(target.get("pan_degrees"), target.get("tilt_degrees"))
            except ValueError as error:
                raise ControlError("invalid_argument", str(error)) from None
        operation = asyncio.create_task(self.goto_operation(target, angle_target=angle_target))
        try:
            return await asyncio.shield(operation)
        except asyncio.CancelledError:
            operation.cancel()
            # The independent job keeps ownership through its final stop.
            raise

    async def goto_operation(self, target, *, angle_target=None):
        self.reserve(object(), "goto")
        host = self.host
        self.task = asyncio.current_task()
        stop_failed = False
        try:
            async with asyncio.timeout(60):
                if not await self.acknowledge_stop(host):
                    raise stop_error()
                current = await self.settled_steps(host)
                skipped_axes = []
                if angle_target:
                    current_angles = ipcam_angles.angles_from_steps(current)
                    for axis, angle in angle_target.items():
                        if round_target_angle(current_angles[axis], *ipcam_angles.ANGLE_LIMITS[axis]) == angle:
                            skipped_axes.append(axis.replace("_degrees", "_steps"))
                return await self.feedback(host, current, {**current, **target}, skipped_axes=skipped_axes)
        except ControlError as error:
            stop_failed = error.code == "stop_unconfirmed"
            raise
        except READ_ERRORS as error:
            LOG.info("Target movement failed at %s (%s)", host, type(error).__name__)
            raise ControlError("motion_failed", "Target movement failed or expired") from None
        finally:
            cleanup = asyncio.create_task(self.finish(host, stop_failed))
            self.task = cleanup
            if not await asyncio.shield(cleanup):
                raise stop_error()

    async def cancel_camera(self, host):
        if self.host != host or not self.task:
            return
        task = self.task
        if self.state.ptz_state == "manual":
            self.stop_manual(self.owner)
        elif self.state.ptz_state == "goto" or (self.state.ptz_state == "homing" and self.closed):
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
