"""HTTP motor-step feedback and the camera model's angle conversion."""
import http.client
import ipaddress
import re

if __package__:
    from .hank_ptz import PTZError
else:
    from hank_ptz import PTZError

PAN_HALF = 177.5
TILT_MIN = -30.0
TILT_MAX = 90.0
PAN_MAX_STEPS = 4100
TILT_MAX_STEPS = 1140
STEP_LIMITS = {'pan_steps': PAN_MAX_STEPS, 'tilt_steps': TILT_MAX_STEPS}
ANGLE_LIMITS = {'pan_degrees': (-PAN_HALF, PAN_HALF), 'tilt_degrees': (TILT_MIN, TILT_MAX)}

# Initial geometry taken from the measured setup of the supported camera model.
PAN_ZERO_STEPS = 2020
TILT_ZERO_STEPS = 891
PAN_STEPS_PER_DEGREE = 11.549295774647888
TILT_STEPS_PER_DEGREE = 7.833333333333333


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


def angles_to_steps(pan, tilt):
    target = {}
    if pan is not None:
        target["pan_steps"] = round(PAN_ZERO_STEPS + pan * PAN_STEPS_PER_DEGREE)
    if tilt is not None:
        target["tilt_steps"] = round(TILT_ZERO_STEPS - tilt * TILT_STEPS_PER_DEGREE)
    if any(not 0 <= value <= STEP_LIMITS[axis] for axis, value in target.items()):
        raise ValueError("Requested angle exceeds the motor step limits")
    return target


def angles_from_steps(steps):
    return {
        "pan_degrees": round((steps["pan_steps"] - PAN_ZERO_STEPS) / PAN_STEPS_PER_DEGREE, 6),
        "tilt_degrees": round((TILT_ZERO_STEPS - steps["tilt_steps"]) / TILT_STEPS_PER_DEGREE, 6),
    }
