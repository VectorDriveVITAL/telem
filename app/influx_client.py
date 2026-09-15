"""
Thin wrapper around the InfluxDB Python client.

Everything else in the app goes through the functions here rather than
touching `influxdb_client` directly — that keeps the Flux query strings in
one place and makes it straightforward to swap storage later if you ever
need to (e.g. moving to InfluxDB Cloud, or a different bucket layout).
"""
from datetime import datetime, timezone
from typing import Optional

from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write_api import SYNCHRONOUS

from .config import settings

_client: Optional[InfluxDBClient] = None


def get_client() -> InfluxDBClient:
    global _client
    if _client is None:
        _client = InfluxDBClient(
            url=settings.influx_url,
            token=settings.influx_token,
            org=settings.influx_org,
        )
    return _client


def close_client():
    global _client
    if _client is not None:
        _client.close()
        _client = None


def write_points(points: list[Point]):
    """Batch-write a list of Points. Used for both the initial history
    backfill and the periodic live ticks."""
    write_api = get_client().write_api(write_options=SYNCHRONOUS)
    write_api.write(bucket=settings.influx_bucket, org=settings.influx_org, record=points)


def make_point(measurement: str, tags: dict, fields: dict, time: Optional[datetime] = None) -> Point:
    p = Point(measurement)
    for k, v in tags.items():
        p = p.tag(k, v)
    for k, v in fields.items():
        p = p.field(k, float(v))
    if time is not None:
        p = p.time(time, WritePrecision.S)
    return p


def query_latest(measurement: str, tag_key: str, tag_value: str) -> Optional[dict]:
    """Return the most recent field values for one series, as a plain dict,
    or None if nothing has been written yet."""
    flux = f'''
    from(bucket: "{settings.influx_bucket}")
      |> range(start: -1h)
      |> filter(fn: (r) => r._measurement == "{measurement}")
      |> filter(fn: (r) => r.{tag_key} == "{tag_value}")
      |> last()
    '''
    tables = get_client().query_api().query(flux, org=settings.influx_org)
    result: dict = {}
    for table in tables:
        for record in table.records:
            result[record.get_field()] = record.get_value()
            result["_time"] = record.get_time()
    return result or None


def query_timeseries(measurement: str, tag_key: str, tag_value: str, field: str, minutes: int) -> list[dict]:
    """Return up to ~60 evenly-spaced points for one field over the last
    `minutes` minutes, aggregated with mean() so the chart stays smooth
    regardless of how many raw points fall in the window."""
    every = max(1, minutes // 60)
    flux = f'''
    from(bucket: "{settings.influx_bucket}")
      |> range(start: -{minutes}m)
      |> filter(fn: (r) => r._measurement == "{measurement}")
      |> filter(fn: (r) => r.{tag_key} == "{tag_value}")
      |> filter(fn: (r) => r._field == "{field}")
      |> aggregateWindow(every: {every}m, fn: mean, createEmpty: false)
      |> sort(columns: ["_time"])
    '''
    tables = get_client().query_api().query(flux, org=settings.influx_org)
    points = []
    for table in tables:
        for record in table.records:
            points.append({
                "time": record.get_time().isoformat(),
                "value": record.get_value(),
            })
    return points


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
