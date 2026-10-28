"""Per-sensor correction applied to readings as they arrive: corrected = raw * scale + offset.

Only new readings are corrected; history keeps the values it was stored with. The table lives
in the state database, so it survives restarts."""

import math
import threading

from . import state_store

_lock = threading.Lock()
_table = None  # {buoy: {field: {"offset": float, "scale": float}}}


def _load():
    global _table
    if _table is None:
        _table = state_store.load_calibration()
    return _table


def reset_cache():
    global _table
    _table = None


def get(buoy):
    with _lock:
        return {f: dict(c) for f, c in _load().get(buoy, {}).items()}


def set_field(buoy, field, offset, scale):
    if not (math.isfinite(offset) and math.isfinite(scale)) or scale == 0:
        raise ValueError("offset and scale must be finite numbers, and scale cannot be 0")
    with _lock:
        _load().setdefault(buoy, {})[field] = {"offset": offset, "scale": scale}
        state_store.save_calibration(_load())
    return get(buoy)


def clear_field(buoy, field):
    with _lock:
        _load().get(buoy, {}).pop(field, None)
        state_store.save_calibration(_load())
    return get(buoy)


def apply(buoy, fields):
    """Return `fields` with the buoy's corrections applied; untouched fields pass through."""
    with _lock:
        cal = _load().get(buoy)
        if not cal:
            return fields
        return {
            k: v * cal[k]["scale"] + cal[k]["offset"] if k in cal else v
            for k, v in fields.items()
        }
