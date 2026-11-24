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
