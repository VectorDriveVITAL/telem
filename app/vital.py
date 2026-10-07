"""Optional forwarding of buoy readings, real and simulated, to the VITAL (Upstream) sensor API.

Credentials come from the dashboard's VITAL tab (saved in STATE_DB) or, until
that tab is first used, from VITAL_USERNAME/PASSWORD/CAMPAIGN_ID. Each
buoy becomes a station named after the buoy in that campaign; each field becomes
a sensor with the field name as its alias. Readings are queued and uploaded in
batches through the API's sensors + measurements CSV endpoint, which reuses
sensors by alias and ignores duplicate (sensor, time) measurements, so a failed
batch is simply sent again.
"""

import csv
import io
import json
import queue
import threading
import time
import uuid
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from . import state_store
from .config import settings

# Simulated buoys produce a reading every TICK_SECONDS; batching for 30 s keeps
# it to about one upload per buoy per 30 s instead of one every few seconds.
FLUSH_SECONDS = 30
RETRY_SECONDS = 30
MAX_ROWS_PER_UPLOAD = 5000

# ponytail: in-memory queue, unsent readings are lost on restart (they remain in
# InfluxDB); add a SQLite outbox if VITAL must receive every reading.
_queue = queue.Queue(maxsize=100_000)
_stations = {}
_token = None
_started = False
_lock = threading.Lock()  # a credential change waits for the upload in progress
_creds = {}  # api_url, username, password, campaign_id
_status = {"campaign_name": None, "last_upload": None, "last_error": None}


def enabled():
    return bool(
        _creds.get("username") and _creds.get("password") and _creds.get("campaign_id")
    )


def forward(buoy_id, ts, lat, lng, fields, units):
    """Queue one reading. Never blocks or raises into the ingest or simulator path."""
    values = {k: v for k, v in fields.items() if k not in ("lat", "lng")}
    if not enabled() or not values:
        return
    row = (buoy_id, ts.isoformat(), lat, lng, values, units)
    try:
        _queue.put_nowait(row)
    except queue.Full:
        print(f"[vital] queue full, dropped reading for {buoy_id}")


def start():
    global _creds
    saved = state_store.load_vital()
    _creds = saved if saved is not None else {
        "api_url": settings.vital_api_url,
        "username": settings.vital_username,
        "password": settings.vital_password,
        "campaign_id": settings.vital_campaign_id,
    }
    _ensure_thread()


def _ensure_thread():
    global _started
    if enabled() and not _started:
        _started = True
        threading.Thread(target=_run, name="vital-forwarder", daemon=True).start()
    if enabled():
        print(f"[vital] forwarding buoy readings to campaign {_creds['campaign_id']}")


def status():
    return {
        "enabled": enabled(),
        "api_url": _creds.get("api_url") or settings.vital_api_url,
        "username": _creds.get("username", ""),
        "campaign_id": _creds.get("campaign_id", ""),
        "password_set": bool(_creds.get("password")),
        "queued": _queue.qsize(),
        **_status,
    }


def configure(api_url, username, password, campaign_id):
    """Log in and look up the campaign; only then save and use the credentials.
    A blank password keeps the saved one. Raises ValueError with a readable reason."""
    global _creds, _token, _stations
    creds = {
        "api_url": api_url.strip() or settings.vital_api_url,
        "username": username.strip(),
        "password": password or _creds.get("password", ""),
        "campaign_id": str(campaign_id).strip(),
    }
    if not all(creds.values()):
        raise ValueError("Username, password and campaign id are required")
    base = creds["api_url"].rstrip("/")
    try:
        token = _login(creds)
    except HTTPError as exc:
        raise ValueError(f"VITAL rejected the login (HTTP {exc.code})")
    except (URLError, OSError) as exc:
        raise ValueError(f"Could not reach {base}: {exc}")
    try:
        req = Request(
            f"{base}/campaigns/{creds['campaign_id']}",
            headers={"Authorization": f"Bearer {token}"},
        )
        with urlopen(req, timeout=30) as r:
            name = json.load(r)["name"]
    except HTTPError as exc:
        raise ValueError(
            f"Campaign {creds['campaign_id']} is not accessible (HTTP {exc.code})"
        )
    with _lock:
        _creds, _token, _stations = creds, token, {}
        _status.update(campaign_name=name, last_error=None)
    state_store.save_vital(creds)
    _ensure_thread()
    return status()


def disconnect():
    global _creds, _token, _stations
    with _lock:
        _creds, _token, _stations = {}, None, {}
        _status.update(campaign_name=None, last_error=None)
    state_store.save_vital({})
    return status()


def build_csvs(rows):
    """Return (sensors_csv, measurements_csv) for one buoy's queued rows."""
    units = {}
    for *_, values, row_units in rows:
        for key in values:
            units.setdefault(key, row_units.get(key, ""))
    aliases = sorted(units)
    sensors, measurements = io.StringIO(), io.StringIO()
    w = csv.writer(sensors, lineterminator="\n")
    w.writerow(["alias", "variablename", "units", "postprocess"])
    w.writerows([a, a, units[a], False] for a in aliases)
    w = csv.writer(measurements, lineterminator="\n")
    w.writerow(["collectiontime", "Lat_deg", "Lon_deg", *aliases])
    for _, ts, lat, lng, values, _ in rows:
        w.writerow([ts, lat, lng, *(values.get(a, "") for a in aliases)])
    return sensors.getvalue(), measurements.getvalue()


def _run():
    while True:
        rows = [_queue.get()]
        time.sleep(FLUSH_SECONDS)  # let a burst of readings share one upload
        while len(rows) < MAX_ROWS_PER_UPLOAD:
            try:
                rows.append(_queue.get_nowait())
            except queue.Empty:
                break
        by_buoy = {}
        for row in rows:
            by_buoy.setdefault(row[0], []).append(row)
        failed = False
        for buoy_id, buoy_rows in by_buoy.items():
            try:
                with _lock:
                    if not enabled():
                        break  # disconnected while these were queued
                    _upload(buoy_id, buoy_rows)
                _status.update(
                    last_upload=datetime.now(timezone.utc).isoformat(), last_error=None
                )
            except HTTPError as exc:
                detail = exc.read()[:300].decode(errors="replace")
                if 400 <= exc.code < 500 and exc.code not in (401, 408, 429):
                    _error(f"{buoy_id}: HTTP {exc.code}, dropped {len(buoy_rows)} rows: {detail}")
                    continue
                _error(f"{buoy_id}: HTTP {exc.code}, will retry: {detail}")
                failed = _requeue(buoy_rows)
            except Exception as exc:
                _error(f"{buoy_id}: {type(exc).__name__}: {exc}, will retry")
                failed = _requeue(buoy_rows)
        if failed:
            time.sleep(RETRY_SECONDS)


def _error(message):
    print(f"[vital] {message}")
    _status["last_error"] = message


def _requeue(rows):
    for row in rows:
        try:
            _queue.put_nowait(row)
        except queue.Full:
            print("[vital] queue full while retrying, dropped a reading")
            break
    return True


def _upload(buoy_id, rows):
    sensors_csv, measurements_csv = build_csvs(rows)
    boundary = uuid.uuid4().hex
    body = b"".join(
        (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"; "
            f"filename=\"{name}.csv\"\r\nContent-Type: text/csv\r\n\r\n{content}\r\n"
        ).encode()
        for name, content in (
            ("upload_file_sensors", sensors_csv),
            ("upload_file_measurements", measurements_csv),
        )
    ) + f"--{boundary}--\r\n".encode()
    station = _station_id(buoy_id)
    result = _call(
        "POST",
        f"/uploadfile_csv/campaign/{_creds['campaign_id']}/station/{station}/sensor",
        body,
        {"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    for error in result.get("errors") or []:
        print(f"[vital] {buoy_id}: {error.get('message', error)}")


def _station_id(buoy_id):
    if buoy_id in _stations:
        return _stations[buoy_id]
    base = f"/campaigns/{_creds['campaign_id']}/stations"
    for station in _all(base):
        if station["name"] == buoy_id:
            _stations[buoy_id] = station["id"]
            return station["id"]
    created = _call(
        "POST",
        base,
        json.dumps(
            {
                "name": buoy_id,
                "description": "telem buoy",
                "start_date": datetime.now(timezone.utc).isoformat(),
                "station_type": "mobile",
                "timezone": "UTC",
            }
        ).encode(),
        {"Content-Type": "application/json"},
    )
    _stations[buoy_id] = created["id"]
    print(f"[vital] created station {created['id']} for {buoy_id}")
    return created["id"]


def _all(path):
    """Every item of a paginated VITAL listing."""
    items, page = [], 1
    while True:
        listing = _call("GET", f"{path}?page={page}&limit=100")
        items += listing["items"]
        if page >= listing.get("pages", 1):
            return items
        page += 1


def remote_snapshot():
    """What VITAL currently holds for the connected campaign, for the debug view."""
    if not enabled():
        raise ValueError("VITAL is not connected")
    base = f"/campaigns/{_creds['campaign_id']}"
    try:
        campaign = _call("GET", base)
        stations = _all(f"{base}/stations")
        for station in stations:
            station["sensors"] = _all(f"{base}/stations/{station['id']}/sensors")
    except HTTPError as exc:
        raise ValueError(f"VITAL returned HTTP {exc.code} for {exc.url}")
    except (URLError, OSError) as exc:
        raise ValueError(f"Could not reach VITAL: {exc}")
    campaign.pop("stations", None)
    return {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "campaign": campaign,
        "stations": stations,
    }


def _call(method, path, data=None, headers=None):
    global _token
    url = _creds["api_url"].rstrip("/") + path
    for attempt in range(2):
        if not _token:
            _token = _login(_creds)
        h = {**(headers or {}), "Authorization": f"Bearer {_token}"}
        try:
            with urlopen(Request(url, data, h, method=method), timeout=120) as r:
                return json.load(r)
        except HTTPError as exc:
            if exc.code != 401 or attempt:
                raise
            _token = None  # expired; log in again once


def _login(creds):
    form = urlencode(
        {"username": creds["username"], "password": creds["password"]}
    ).encode()
    req = Request(
        creds["api_url"].rstrip("/") + "/token",
        form,
        {"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urlopen(req, timeout=30) as r:
        return json.load(r)["access_token"]
