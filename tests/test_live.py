"""Optional integration checks against a running development instance.

TELEM_TEST_URL=http://127.0.0.1:8000 python -m pytest tests/test_live.py -q
Creates isolated sources and removes them afterwards; historical samples remain.
"""

import csv
import io
import os
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest

pytestmark = pytest.mark.skipif(
    not os.getenv("TELEM_TEST_URL"),
    reason="set TELEM_TEST_URL for real InfluxDB integration tests",
)


@pytest.fixture
def live():
    with httpx.Client(
        base_url=os.getenv("TELEM_TEST_URL", "http://127.0.0.1:8000"),
        trust_env=False,
        timeout=30,
    ) as c:
        yield c


def test_real_history_exports_and_old_contract(live):
    name = "integration-" + uuid.uuid4().hex[:8]
    assert (
        live.post(
            "/api/buoys",
            json={
                "id": name,
                "lat": 29.7,
                "lng": -95.4,
                "sensors": ["temp"],
                "units": {"temp": "°C"},
            },
        ).status_code
        == 201
    )
    try:
        end = datetime.now(timezone.utc)
        start = end - timedelta(minutes=5)
        samples = [
            {
                "time": (end - timedelta(minutes=4 - i)).isoformat(),
                "temp": 20 + i,
                "battery": 80 - i,
                "solar_watts": 2 + i,
                "signal_dbm": -70 - i,
            }
            for i in range(3)
        ]
        response = live.post(f"/api/ingest/buoys/{name}/batch", json=samples)
        assert response.status_code == 200, response.text
        params = {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "metrics": "temp,battery,solar_watts,signal_dbm",
            "aggregation": "raw",
        }
        response = live.get(f"/api/buoys/{name}/timeseries", params=params)
        assert response.status_code == 200, response.text
        history = response.json()
        assert len(history["points"]) == 3
        assert [p["values"]["temp"] for p in history["points"]] == [20, 21, 22]
        assert [p["values"]["signal_dbm"] for p in history["points"]] == [-70, -71, -72]
        assert datetime.fromisoformat(
            history["points"][0]["time"]
        ) == datetime.fromisoformat(samples[0]["time"])
        response = live.get(
            f"/api/buoys/{name}/timeseries", params={"metric": "temp", "minutes": 60}
        )
        assert response.status_code == 200, response.text
        assert isinstance(response.json(), list) and {"time", "value"} == set(
            response.json()[0]
        )
        response = live.get(
            "/api/export", params=dict(params, source=name, kind="buoy")
        )
        assert response.status_code == 200, response.text
        rows = list(csv.DictReader(io.StringIO(response.text.lstrip("\ufeff"))))
        assert len(rows) == 12 and set(r["metric"] for r in rows) == {
            "temp",
            "battery",
            "solar_watts",
            "signal_dbm",
        }
        response = live.get(
            "/api/export",
            params=dict(
                params,
                source=name,
                kind="buoy",
                aggregation="mean",
                interval_seconds=60,
            ),
        )
        assert response.status_code == 200, response.text
        assert (
            len(list(csv.DictReader(io.StringIO(response.text.lstrip("\ufeff"))))) == 12
        )
        bad = live.get(
            f"/api/buoys/{name}/timeseries", params={"metrics": 'temp","bad'}
        )
        assert bad.status_code == 400
    finally:
        assert live.delete(f"/api/buoys/{name}").status_code == 204


def test_power_history_and_service_metric_extension(live):
    name = "service-" + uuid.uuid4().hex[:8]
    assert live.post("/api/services", json={"name": name}).status_code == 201
    try:
        assert (
            live.post(
                f"/api/ingest/services/{name}",
                json={"latency_ms": 150, "rps": 300, "error_rate": 0.5},
            ).status_code
            == 200
        )
        response = live.get(
            f"/api/services/{name}/timeseries",
            params={"metrics": "latency_ms,rps,error_rate", "aggregation": "raw"},
        )
        assert response.status_code == 200, response.text
        assert response.json()["points"][-1]["values"] == {
            "latency_ms": 150,
            "rps": 300,
            "error_rate": 0.5,
        }
    finally:
        live.delete(f"/api/services/{name}")
