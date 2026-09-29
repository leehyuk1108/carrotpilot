import pytest
import requests


@pytest.fixture(autouse=True)
def no_external_http(monkeypatch):
  def forbidden(*args, **kwargs):
    raise AssertionError("Tests must not contact a real Cloud/vehicle endpoint")
  monkeypatch.setattr(requests.sessions.Session, "request", forbidden)


@pytest.fixture(autouse=True)
def private_health_files(tmp_path, monkeypatch):
  from openpilot.system.hylink import health
  monkeypatch.setattr(health, "STATUS_PATH", tmp_path / "telemetry.json")
  monkeypatch.setattr(health, "HISTORY_PATH", tmp_path / "telemetry_history.json")
