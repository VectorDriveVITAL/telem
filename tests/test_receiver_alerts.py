import pytest

from app import calibration, generator as fleet, receiver, receiver_alerts as ra, state_store
from app.config import settings
from app.security import control


def link(**kw):
    base = {"watching": True, "since_start_s": 100.0, "age_s": 2.0, "recent": [], "outcomes": []}
    return {**base, **kw}


def pkts(snr, rssi, n=5):
    return [{"snr": snr, "rssi": rssi} for _ in range(n)]


def test_silence_escalates_and_only_counts_while_watching():
    assert ra.link_conditions(link(age_s=5)) == []
    assert ra.link_conditions(link(age_s=30))[0][0] == "warning"
    assert ra.link_conditions(link(age_s=90))[0] == ("critical", "no radio packets for 90 s")
    assert ra.link_conditions(link(age_s=90, watching=False)) == []  # bridge stopped on purpose
    assert ra.link_conditions(link(age_s=None, since_start_s=10)) == []  # still starting
    assert ra.link_conditions(link(age_s=None, since_start_s=45))[0][0] == "warning"


def test_weak_link_and_loss_raise_conditions():
    assert ra.link_conditions(link(recent=pkts(snr=12, rssi=-18))) == []
    assert ra.link_conditions(link(recent=pkts(snr=-3, rssi=-110)))[0][0] == "warning"  # SNR margin 4.5
    assert ra.link_conditions(link(recent=pkts(snr=-6, rssi=-110)))[0][0] == "critical"  # SNR margin 1.5
    assert ra.link_conditions(link(recent=pkts(snr=12, rssi=-18, n=2))) == []  # too few packets to judge
    assert ra.link_conditions(link(outcomes=[True] * 7 + [False] * 3))[0][0] == "warning"
    assert ra.link_conditions(link(outcomes=[True] * 4 + [False] * 6))[0][0] == "critical"


def hull(rh_start, rh_end, minutes=30, core_t=22.0, water=21.0):
    n = minutes * 12
    return [
        {"time": 1000 + i * 5, "core_humidity": rh_start + (rh_end - rh_start) * i / (n - 1),
         "core_temp": core_t, "temp": water}
        for i in range(n)
    ]


def test_hull_humidity_rise_and_condensation():
    assert ra.hull_conditions(hull(60, 60.2, water=30)) == []
    assert ra.hull_conditions(hull(60, 63, water=30))[0][0] == "warning"  # +6 %RH/h
    rising = ra.hull_conditions(hull(60, 70, water=30))  # +20 %RH/h
    assert rising[0][0] == "critical" and "possible leak" in rising[0][1]
    # 22 C air at 90 %RH has a dew point near 20.3 C: water at 21 C is within the 3 C margin
    assert any("condensation margin low" in r for _, r in ra.hull_conditions(hull(90, 90)))
    assert any(s == "critical" for s, _ in ra.hull_conditions(hull(95, 95, water=19)))
    assert ra.hull_conditions([]) == []
    assert ra.hull_conditions(hull(60, 70, minutes=5, water=30)) == []  # too short a span to trust a rate


def test_stuck_sensors_need_five_minutes_of_identical_readings():
    assert ra.stuck_conditions({"temp": [1.0, 2.0] * 40}) == []
    out = ra.stuck_conditions({"turbidity": [0.0] * 60, "ph": [7.5] * 59 + [7.6]})
    assert out == [("warning", "turbidity: reading flat for 5 min")]


# ---- conditions become real alerts and incidents -----------------------------------------------


@pytest.fixture
def fleet_state(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "state_db", str(tmp_path / "s.sqlite3"))
    empty = {"services": {}, "buoys": {}, "alerts": [], "incidents": {}, "maintenance": [], "mutes": {}}
    monkeypatch.setattr(fleet, "_state", empty)
    monkeypatch.setattr(fleet, "write_points", lambda points: None)
    fleet.register_service("lora-receiver", warn_ms=7500, crit_ms=15000)
    fleet.ingest_service("lora-receiver", 5000, 0.2, 0)
    return fleet


def test_set_conditions_raises_and_clears_an_incident(fleet_state):
    fleet_state.set_conditions("service", "lora-receiver", [("critical", "no radio packets for 90 s")])
    svc = fleet_state.get_service_state("lora-receiver")
    assert svc["status"] == "critical" and "no radio packets" in svc["status_text"]
    [inc] = fleet_state.get_incidents()
    assert inc["status"] == "ongoing" and inc["severity"] == "critical"
    fleet_state.set_conditions("service", "lora-receiver", [])
    assert fleet_state.get_service_state("lora-receiver")["status"] == "healthy"
    assert fleet_state.get_incidents()[0]["status"] == "resolved"


def test_set_conditions_is_a_no_op_when_nothing_changed(fleet_state):
    fleet_state.set_conditions("service", "lora-receiver", [("warning", "x")])
    before = len(fleet_state._state["alerts"])
    fleet_state.set_conditions("service", "lora-receiver", [("warning", "x")])
    assert len(fleet_state._state["alerts"]) == before


def test_evaluate_ties_the_link_state_to_the_service(fleet_state, monkeypatch):
    monkeypatch.setattr(receiver, "_alerts", {"link": True, "hull": False, "stuck": False})
    monkeypatch.setattr(receiver, "link_state", lambda now: link(age_s=90))
    ra.evaluate(1000.0)
    assert fleet_state.get_service_state("lora-receiver")["status"] == "critical"
    monkeypatch.setattr(receiver, "_alerts", {"link": False, "hull": False, "stuck": False})  # operator turned it off
    ra.evaluate(1001.0)
    assert fleet_state.get_service_state("lora-receiver")["status"] == "healthy"


def test_evaluate_survives_a_failing_store(fleet_state, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("influx down")

    monkeypatch.setattr(fleet_state, "set_conditions", boom)
    ra.evaluate(1000.0)  # must not raise


# ---- calibration -------------------------------------------------------------------------------


@pytest.fixture
def cal(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "state_db", str(tmp_path / "s.sqlite3"))
    calibration.reset_cache()
    yield calibration
    calibration.reset_cache()


def test_calibration_corrects_new_readings_and_survives_a_restart(cal):
    cal.set_field("b1", "temp", offset=-0.5, scale=1.0)
    cal.set_field("b1", "ph", offset=0.0, scale=1.1)
    assert cal.apply("b1", {"temp": 20.0, "ph": 7.0, "humidity": 50.0}) == pytest.approx(
        {"temp": 19.5, "ph": 7.7, "humidity": 50.0}
    )
    assert cal.apply("other", {"temp": 20.0}) == {"temp": 20.0}
    cal.reset_cache()  # a new process reads it back from the database
    assert cal.get("b1")["temp"] == {"offset": -0.5, "scale": 1.0}
    cal.clear_field("b1", "temp")
    assert cal.apply("b1", {"temp": 20.0})["temp"] == 20.0


@pytest.mark.parametrize("offset, scale", [(0, 0), (float("nan"), 1), (0, float("inf"))])
def test_calibration_rejects_values_that_would_destroy_readings(cal, offset, scale):
    with pytest.raises(ValueError):
        cal.set_field("b1", "temp", offset, scale)


def test_calibration_is_applied_on_ingest(cal, tmp_path, monkeypatch):
    empty = {"services": {}, "buoys": {}, "alerts": [], "incidents": {}, "maintenance": [], "mutes": {}}
    monkeypatch.setattr(fleet, "_state", empty)
    written = []
    monkeypatch.setattr(fleet, "write_points", lambda points: written.append(points))
    fleet.register_buoy("b1", 1.0, 2.0, ["temp"], {"temp": "C"})
    cal.set_field("b1", "temp", offset=-0.5, scale=1.0)
    state = fleet.ingest_buoy("b1", {"temp": 20.0})
    assert state["sensors"]["temp"]["value"] == pytest.approx(19.5)


# ---- the control guard ---------------------------------------------------------------------------


def req(host="127.0.0.1", headers=None):
    from starlette.requests import Request

    scope = {"type": "http", "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
             "client": (host, 5000) if host else None}
    return Request(scope)


def test_guard_allows_the_dashboard_on_this_computer():
    control(req(headers={"host": "localhost:8000", "origin": "http://localhost:8000"}))
    control(req(headers={"host": "localhost:8000"}))  # curl sends no Origin


@pytest.mark.parametrize(
    "host, headers",
    [("192.168.1.20", {"host": "x"}), ("127.0.0.1", {"host": "localhost:8000", "origin": "https://evil.example"}), (None, {})],
)
def test_guard_refuses_other_machines_and_other_web_pages(host, headers):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as e:
        control(req(host, headers))
    assert e.value.status_code == 403


def test_guard_with_a_token_needs_exactly_that_token(monkeypatch):
    from fastapi import HTTPException

    monkeypatch.setattr(settings, "receiver_token", "s3cret")
    control(req("192.168.1.20", {"authorization": "Bearer s3cret"}))  # other machines are fine with it
    for bad in ({}, {"authorization": "Bearer nope"}, {"authorization": "s3cret"}):
        with pytest.raises(HTTPException) as e:
            control(req(headers=bad))
        assert e.value.status_code == 401
