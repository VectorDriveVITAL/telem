"""InfluxDB writes and bounded, timestamp-preserving history queries."""

import json
from datetime import datetime, timedelta, timezone

from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write_api import SYNCHRONOUS

from .config import settings

_client = None
MAX_POINTS = 20000


class QueryTooLarge(ValueError):
    pass


def get_client():
    global _client
    if _client is None:
        _client = InfluxDBClient(
            url=settings.influx_url,
            token=settings.influx_token,
            org=settings.influx_org,
            timeout=20000,
        )
    return _client


def close_client():
    global _client
    if _client is not None:
        _client.close()
        _client = None


def write_points(points):
    if not points:
        return
    with get_client().write_api(write_options=SYNCHRONOUS) as api:
        api.write(bucket=settings.influx_bucket, org=settings.influx_org, record=points)


def make_point(measurement, tags, fields, time=None):
    point = Point(measurement)
    for key, value in tags.items():
        point = point.tag(key, value)
    for key, value in fields.items():
        point = point.field(key, float(value))
    if time is not None:
        point = point.time(time, WritePrecision.NS)
    return point


def utcnow():
    return datetime.now(timezone.utc)


def _utc(dt):
    return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def window(minutes=60, start=None, end=None):
    end = _utc(end) if end else utcnow()
    start = _utc(start) if start else end - timedelta(minutes=minutes)
    if start >= end:
        raise ValueError("start must be before end")
    if (end - start).total_seconds() > 31 * 86400:
        raise ValueError("select at most 31 days per request")
    return start, end


def query_rows(
    measurement,
    tag_key,
    tag_value,
    fields,
    start,
    end,
    aggregation="mean",
    interval_seconds=60,
):
    # JSON string quoting is also valid Flux string quoting; no identifiers from
    # the request are interpolated as executable Flux expressions.
    q = json.dumps
    flux = f"""from(bucket: {q(settings.influx_bucket)})
      |> range(start: time(v: {q(start.isoformat())}), stop: time(v: {q(end.isoformat())}))
      |> filter(fn: (r) => r._measurement == {q(measurement)} and r[{q(tag_key)}] == {q(tag_value)})
      |> filter(fn: (r) => contains(value: r._field, set: {q(fields)}))
      |> group(columns: ["_field"])
    """
    if aggregation != "raw":
        if aggregation not in {"mean", "min", "max", "last"}:
            raise ValueError("unsupported aggregation")
        flux += f'|> aggregateWindow(every: {int(interval_seconds)}s, fn: {aggregation}, createEmpty: false, timeSrc: "_start")\n'
    flux += f'|> group(columns: []) |> sort(columns: ["_time", "_field"]) |> limit(n: {MAX_POINTS + 1})'
    records = get_client().query_api().query_stream(flux, org=settings.influx_org)
    rows = []
    try:
        for record in records:
            if record.get_value() is None:
                continue
            rows.append(
                {
                    "time": record.get_time().isoformat(),
                    "metric": record.get_field(),
                    "value": record.get_value(),
                }
            )
            if len(rows) > MAX_POINTS:
                raise QueryTooLarge(
                    "too many readings; select a shorter window or an aggregated export"
                )
    finally:
        records.close()
    return rows


def query_history(
    measurement,
    tag_key,
    tag_value,
    fields,
    minutes=60,
    start=None,
    end=None,
    aggregation="mean",
    interval_seconds=None,
):
    start, end = window(minutes, start, end)
    seconds = (
        max(1, int((end - start).total_seconds() / 240))
        if interval_seconds is None
        else interval_seconds
    )
    rows = query_rows(
        measurement, tag_key, tag_value, fields, start, end, aggregation, seconds
    )
    aligned = {}
    for row in rows:
        aligned.setdefault(row["time"], dict.fromkeys(fields))[row["metric"]] = row[
            "value"
        ]
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "metrics": fields,
        "aggregation": aggregation,
        "interval_seconds": seconds if aggregation != "raw" else None,
        "points": [
            {"time": t, "values": values} for t, values in sorted(aligned.items())
        ],
    }


def query_timeseries(
    measurement,
    tag_key,
    tag_value,
    field,
    minutes=60,
    start=None,
    end=None,
    aggregation="mean",
    interval_seconds=None,
):
    # Legacy single-metric response is preserved.
    history = query_history(
        measurement,
        tag_key,
        tag_value,
        [field],
        minutes,
        start,
        end,
        aggregation,
        interval_seconds or max(60, minutes // 60 * 60),
    )
    return [
        {"time": p["time"], "value": p["values"][field]}
        for p in history["points"]
        if p["values"][field] is not None
    ]


def query_latest(measurement, tag_key, tag_value):
    q = json.dumps
    flux = f"""from(bucket: {q(settings.influx_bucket)}) |> range(start: -1h)
      |> filter(fn: (r) => r._measurement == {q(measurement)} and r[{q(tag_key)}] == {q(tag_value)})
      |> group(columns: ["_field"]) |> last()"""
    result = {}
    for table in get_client().query_api().query(flux, org=settings.influx_org):
        for record in table.records:
            result[record.get_field()] = record.get_value()
            result["_time"] = record.get_time()
    return result or None
