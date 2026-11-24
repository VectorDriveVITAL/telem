import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pi"))
import service_health as sh  # noqa: E402


def _is_active(ok):
    return lambda *a, **k: subprocess.CompletedProcess(a, 0 if ok else 3)


def test_inactive_unit_is_down(monkeypatch):
    monkeypatch.setattr(sh.subprocess, "run", _is_active(False))
    assert sh.check("telegraf", None) == (sh.DOWN_MS, 100.0)


def test_active_unit_without_port_is_zero(monkeypatch):
    monkeypatch.setattr(sh.subprocess, "run", _is_active(True))
    assert sh.check("telegraf", None) == (0.0, 0.0)


def test_active_unit_with_closed_port_is_down(monkeypatch):
    monkeypatch.setattr(sh.subprocess, "run", _is_active(True))
    assert sh.check("mosquitto", 1) == (sh.DOWN_MS, 100.0)  # port 1 refuses connections


def test_real_service_without_rps_reports_none():
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as c:
        c.post("/api/services", json={"name": "svc-rps-test"})
        c.post("/api/ingest/services/svc-rps-test", json={"latency_ms": 5, "error_rate": 0})
        row = next(s for s in c.get("/api/services").json() if s["name"] == "svc-rps-test")
        assert row["rps"] is None
        c.post("/api/ingest/services/svc-rps-test", json={"rps": 12})
        row = next(s for s in c.get("/api/services").json() if s["name"] == "svc-rps-test")
        assert row["rps"] == 12
        c.delete("/api/services/svc-rps-test")


def test_rate_needs_two_samples_and_survives_counter_reset():
    assert sh.rate(None, 10, 100.0) == (None, (10, 100.0))
    assert sh.rate((10, 100.0), 40, 110.0) == (3.0, (40, 110.0))
    assert sh.rate((40, 110.0), 5, 120.0) == (None, (5, 120.0))  # telem restarted
    assert sh.rate((5, 120.0), None, 130.0) == (None, None)


def test_influx_total_sums_request_counters(monkeypatch):
    text = 'x 1\nhttp_api_requests_total{path="/a"} 3\nhttp_api_requests_total{path="/b"} 4\nother{a="b"} 9\n'
    monkeypatch.setattr(sh, "_get", lambda url: text)
    assert sh.influx_total() == 7


def test_health_counts_requests_but_not_service_reports():
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as c:
        before = c.get("/api/health").json()["requests_total"]
        c.post("/api/ingest/services/nope", json={"latency_ms": 1})
        after = c.get("/api/health").json()["requests_total"]
        assert after - before == 1  # only the second health call


def test_prom_total_filters_by_label(monkeypatch):
    text = 'm{event="up"} 2\nm{event="stats"} 5\nm{event="up",x="1"} 1\n'
    monkeypatch.setattr(sh, "_get", lambda url: text)
    assert sh.prom_total("u", 'm{event="up"') == 3
