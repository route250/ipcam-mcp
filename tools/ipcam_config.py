"""Load and atomically save the local calibration JSON."""
import contextlib
import fcntl
import json
import os
from pathlib import Path
import tempfile

DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / 'ipcam.json'


def load(path):
    path = Path(path)
    if path.exists():
        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            raise ValueError('configuration must be a JSON object')
    else:
        data = {}
    return data


def save(path, data):
    path = Path(path)
    fd, name = tempfile.mkstemp(prefix=path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f, ensure_ascii=False, indent=2, allow_nan=False)
            f.write('\n')
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


@contextlib.contextmanager
def locked(path):
    with open(str(path) + '.lock', 'a') as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('another CLI motor operation is running') from None
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
