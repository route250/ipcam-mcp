"""Absolute angles controlled by LAN pulses and HTTP motor-step feedback."""
from datetime import datetime, timezone
import http.client
import ipaddress
import math
import re
import time

if __package__:
    from .hank_ptz import pulse, stop, PTZError
else:
    from hank_ptz import pulse, stop, PTZError

PAN_HALF = 177.5
TILT_MIN = -30.0
TILT_MAX = 90.0
PAN_MAX_STEPS = 4100
TILT_MAX_STEPS = 1140
DEFAULT_TOLERANCE = 10
REFERENCE_TOLERANCE = 5


class PositionNotFound(PTZError):
    """The position endpoint returned HTTP 404."""


def read_steps(connection, http_port=10081):
    """Read only the bounded motorPosition endpoint; no proxies or redirects."""
    host = connection['host']
    address = ipaddress.IPv4Address(host)
    if not any(address in ipaddress.ip_network(net) for net in
               ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')):
        raise ValueError('motor position host must be a private LAN IPv4 address')
    if type(http_port) is not int or not 1 <= http_port <= 65535:
        raise ValueError('position_http_port must be an integer in 1..65535')
    client = http.client.HTTPConnection(host, http_port, timeout=connection['timeout'])
    try:
        client.request('GET', '/motorPosition', headers={'Cache-Control': 'no-cache'})
        response = client.getresponse()
        if response.status == 404:
            raise PositionNotFound('motorPosition returned HTTP 404')
        body = response.read(129)
        if response.status != 200 or len(body) > 128:
            raise PTZError('motorPosition must return HTTP 200 and at most 128 bytes')
        match = re.fullmatch(rb'\s*([0-9]+)\s+([0-9]+)\s*', body.rstrip(b'\0'))
        if not match:
            raise PTZError('invalid motorPosition: expected two ASCII step integers')
        pan, tilt = map(int, match.groups())
        if not 0 <= pan <= PAN_MAX_STEPS or not 0 <= tilt <= TILT_MAX_STEPS:
            raise PTZError('motorPosition is outside the verified device step limits')
        return {'pan_steps': pan, 'tilt_steps': tilt}
    except (OSError, http.client.HTTPException) as exc:
        raise PTZError(f'Cannot read HTTP motorPosition: {type(exc).__name__}') from exc
    finally:
        client.close()


def settled_steps(connection, http_port, cancelled=lambda: False):
    # Persistence can lag movement. Require three equal reads after a 350 ms wait.
    deadline = time.monotonic() + 3
    previous, equal = None, 0
    time.sleep(.35)
    while time.monotonic() < deadline:
        if cancelled():
            raise PTZError('movement interrupted by stop')
        current = read_steps(connection, http_port)
        equal = equal+1 if current == previous else 1
        if equal >= 3:
            return current
        previous = current
        time.sleep(.15)
    raise PTZError('HTTP motor position did not settle within 3 seconds')


def model(data, host, port):
    c = data.get('step_calibration')
    if not isinstance(c, dict) or c.get('host') != host or c.get('port') != port:
        raise ValueError('no matching step calibration; run ipcam reset --calibrate')
    if c.get('position_http_port', 10081) != data.get('position_http_port', 10081):
        raise ValueError('step calibration does not match the HTTP position port')
    keys = ('pan_zero_steps', 'pan_steps_per_degree', 'tilt_zero_steps',
            'tilt_steps_per_degree', 'pan_steps_per_second', 'tilt_steps_per_second')
    if not all(type(c.get(k)) in (int, float) and math.isfinite(c[k]) for k in keys):
        raise ValueError('invalid step calibration')
    if (not 0 <= c['pan_zero_steps'] <= PAN_MAX_STEPS or
            not 0 < c['tilt_zero_steps'] < TILT_MAX_STEPS or
            any(c[k] <= 0 for k in keys if 'per_' in k)):
        raise ValueError('invalid step calibration limits or rates')
    return c


def validate_angles(pan=None, tilt=None):
    if pan is None and tilt is None:
        raise ValueError('specify --pan or --tilt')
    for value, low, high, name in ((pan, -PAN_HALF, PAN_HALF, 'pan'),
                                  (tilt, TILT_MIN, TILT_MAX, 'tilt')):
        if value is not None and (not math.isfinite(value) or not low <= value <= high):
            raise ValueError(f'{name} must be between {low} and {high} degrees')


def target_steps(c, current, pan=None, tilt=None):
    validate_angles(pan, tilt)
    target = dict(current)
    if pan is not None:
        target['pan_steps'] = round(c['pan_zero_steps'] + pan*c['pan_steps_per_degree'])
    if tilt is not None:
        # U decreases the camera's tilt counter.
        target['tilt_steps'] = round(c['tilt_zero_steps'] - tilt*c['tilt_steps_per_degree'])
    for key, maximum in (('pan_steps', PAN_MAX_STEPS), ('tilt_steps', TILT_MAX_STEPS)):
        if not 0 <= target[key] <= maximum:
            raise ValueError('requested angle exceeds calibrated motor step limits')
    return target


def angles_from_steps(c, current):
    return {'pan': round((current['pan_steps']-c['pan_zero_steps'])/c['pan_steps_per_degree'], 6),
            'tilt': round((c['tilt_zero_steps']-current['tilt_steps'])/c['tilt_steps_per_degree'], 6),
            'units': 'degrees', 'estimated': True, 'source': 'http_motor_steps'}


def position(c, current):
    a = angles_from_steps(c, current)
    return {**current, 'known': True, 'units': 'motor_steps', 'source': 'http_motorPosition',
            'pan_degrees': a['pan'], 'tilt_degrees': a['tilt'], 'angles_estimated': True}


def execute(c, target, connection, http_port=10081, cancelled=lambda: False, report=None,
            tolerance=DEFAULT_TOLERANCE):
    """Bounded feedback: both axes rechecked after every pulse and at completion."""
    if type(tolerance) is not int or not 1 <= tolerance <= 10:
        raise ValueError('tolerance must be an integer in 1..10 steps')
    moves = []
    try:
        stop(**connection)
        current = settled_steps(connection, http_port, cancelled)
        # Check both axes every iteration: late persistence or external motion
        # cannot leave the first axis unchecked while correcting the second.
        for attempt in range(33):
            if cancelled():
                raise PTZError('movement interrupted by stop')
            errors = {key: target[key]-current[key] for key in ('pan_steps', 'tilt_steps')}
            if all(abs(v) <= tolerance for v in errors.values()):
                verified = settled_steps(connection, http_port, cancelled)
                if all(abs(target[k]-verified[k]) <= tolerance for k in errors):
                    return {'position': position(c, verified), 'angles': angles_from_steps(c, verified),
                            'target_steps': target, 'error_steps': {k: verified[k]-target[k] for k in errors},
                            'tolerance_steps': tolerance, 'reached': True, 'moves': moves}
                current = verified
                continue
            if attempt == 32:
                break
            axis = max(errors, key=lambda k: abs(errors[k]) if abs(errors[k]) > tolerance else -1)
            delta = errors[axis]
            if axis == 'pan_steps':
                direction = 'R' if delta > 0 else 'L'
                rate = c['pan_steps_per_second']
            else:
                direction = 'D' if delta > 0 else 'U'
                rate = c['tilt_steps_per_second']
            # First move on each axis covers the estimated full distance.
            # Only the measured residual uses short correction pulses.
            same_axis = [m for m in moves if m['axis'] == axis]
            crossings = sum(a['direction'] != b['direction'] for a, b in zip(same_axis, same_axis[1:]))
            if same_axis and same_axis[-1]['direction'] != direction:
                crossings += 1
            if not same_axis:
                duration = min(10.0, max(.005, abs(delta)/rate))
                phase = 'initial'
            else:
                duration = min(.2, max(.005, .7*abs(delta)/rate/(1+crossings)))
                phase = 'correction'
            before = current
            motion = pulse(direction, duration=duration, cancelled=cancelled, **connection)
            current = settled_steps(connection, http_port, cancelled)
            item = {'axis': axis, 'before': before, 'after': current,
                    'direction': direction, 'duration': duration, 'phase': phase, **motion}
            moves.append(item)
            if report:
                report(item)
            if len(same_axis) >= 2 and all(m['before'] == m['after'] for m in same_axis[-2:]) and before == current:
                raise PTZError('no motor step progress after three pulses')
        raise PTZError(f'target not reached within 32 pulses; target={target}, actual={current}')
    finally:
        # HTTP failure, malformed data, Ctrl-C, external stop and pulse ACK
        # failure must all end with an independent stop. Never report success
        # if this final stop cannot be acknowledged.
        try:
            stop(**connection)
        except Exception:
            stop(**connection)


def calibrate(data, connection, http_port=10081, cancelled=lambda: False):
    """Recreate the user's bottom+1s level reference and measure its steps."""
    seconds = data.get('level_from_bottom_seconds', 1.0)
    if type(seconds) not in (int, float) or not math.isfinite(seconds) or not 0 < seconds <= 3:
        raise ValueError('level_from_bottom_seconds must be in (0, 3]')
    old = data.get('calibration') or {}
    pan_seconds = old.get('pan_seconds', 10.551)
    if type(pan_seconds) not in (int, float) or not math.isfinite(pan_seconds) or not 1 <= pan_seconds <= 15:
        raise ValueError('invalid horizontal travel time for initial pulse estimate')
    read_steps(connection, http_port)  # Fail before moving if feedback is absent.
    pulse('D', duration=10, cancelled=cancelled, **connection)
    bottom = settled_steps(connection, http_port, cancelled)
    if abs(bottom['tilt_steps']-TILT_MAX_STEPS) > REFERENCE_TOLERANCE:
        raise PTZError('downward reference did not reach verified bottom step limit')
    pulse('U', duration=seconds,
          cancelled=cancelled, **connection)
    level = settled_steps(connection, http_port, cancelled)
    span = bottom['tilt_steps']-level['tilt_steps']
    if not 50 <= span <= 500:
        raise PTZError('invalid bottom-to-level step count')
    return {'host': connection['host'], 'port': connection['port'],
            'position_http_port': http_port, 'level_from_bottom_seconds': seconds,
            'pan_zero_steps': PAN_MAX_STEPS/2, 'pan_steps_per_degree': PAN_MAX_STEPS/355,
            'tilt_zero_steps': level['tilt_steps'], 'tilt_steps_per_degree': span/30,
            'pan_steps_per_second': PAN_MAX_STEPS/pan_seconds,
            'tilt_steps_per_second': span/seconds,
            'method': 'http_steps; pan355deg; bottom=-30deg; measured_level=0deg',
            'calibrated_at': datetime.now(timezone.utc).isoformat()}
