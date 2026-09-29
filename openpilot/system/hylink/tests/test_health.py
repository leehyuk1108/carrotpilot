"""Local upload diagnostics: fault injection only, never real cloud requests."""

import json
import socket
from types import SimpleNamespace

import pytest
import requests

from openpilot.system.hylink import health, runtime, telemetry, transport
from openpilot.system.hylink.tests import test_integration

configured = test_integration.configured


@pytest.fixture
def clock(monkeypatch):
  now = [1000.0]
  monkeypatch.setattr(health.time, "monotonic", lambda: now[0])
  monkeypatch.setattr(health.time, "time", lambda: 1800000000 + now[0])
  monkeypatch.setattr(health, "boot_id", lambda: "test-boot")
  return now


@pytest.mark.parametrize(
  "error,stage,code,status",
  [
    (transport.UploadHTTPError(401), "upload", "auth", 401),
    (transport.UploadHTTPError(403), "upload", "auth", 403),
    (transport.UploadHTTPError(409), "enrollment", "enrollment_conflict", 409),
    (transport.UploadHTTPError(409), "upload", "http", 409),
    (transport.UploadHTTPError(429), "upload", "rate_limited", 429),
    (transport.UploadHTTPError(503), "upload", "server", 503),
    (requests.exceptions.SSLError("secret"), "upload", "tls", None),
    (requests.exceptions.Timeout("secret"), "upload", "timeout", None),
    (requests.exceptions.ConnectionError(socket.gaierror(-2, "secret")), "upload", "dns", None),
    (requests.exceptions.ConnectionError("secret"), "upload", "network", None),
    (transport.UploadPayloadError("secret"), "upload", "payload", None),
    (transport.UploadResponseError("secret"), "upload", "response", None),
    (RuntimeError("secret"), "payload", "payload", None),
    (RuntimeError("secret"), "initialize", "initialize", None),
    (RuntimeError("secret"), "unexpected-secret-stage", "internal", None),
  ],
)
def test_failure_is_typed_and_redacted(error, stage, code, status):
  result = health.failure(error, stage)
  assert result["code"] == code and result["httpStatus"] == status
  assert "secret" not in json.dumps(result)


def test_success_failure_retry_and_privacy(configured, clock):
  params, config = configured
  reporter = health.TelemetryHealth(config)
  assert health.snapshot(params)["code"] == "starting"
  reporter.attempt(False)
  assert health.snapshot(params)["code"] == "uploading"
  reporter.success()
  success = health.snapshot(params)
  assert success["code"] == "ok" and not success["historyOnly"]
  clock[0] += 30
  reporter.attempt(False)
  reporter.failed(requests.exceptions.Timeout("secret GPS: 37.1234"), "upload", delay=30)
  result = health.snapshot(params)
  assert result["code"] == "timeout" and result["retryInSeconds"] == 30
  assert result["lastSuccessAt"] == success["lastSuccessAt"]
  saved = runtime.read_json(health.STATUS_PATH)
  saved.update(token=config["token"], gps={"latitude": 37.1234}, private="secret")
  runtime.write_json(health.STATUS_PATH, saved)
  public = json.dumps(health.snapshot(params))
  for secret in ("secret", "37.1234", config["token"], config["device_id"], health.identity(config)):
    assert secret not in public
  for secret in ("secret", "37.1234", config["token"], config["device_id"]):
    assert secret not in health.HISTORY_PATH.read_text()
  reporter.success()
  result = health.snapshot(params)
  assert result["code"] == "ok" and result["lastError"] is None and result["retryInSeconds"] is None


def test_boot_process_and_freshness_cannot_reuse_old_success(configured, clock, monkeypatch):
  params, config = configured
  reporter = health.TelemetryHealth(config)
  reporter.attempt(True)
  reporter.success()
  clock[0] += 76
  reporter.heartbeat()
  assert health.snapshot(params)["code"] == "stale"
  clock[0] += health.HEARTBEAT_TTL + 1
  assert health.snapshot(params)["code"] == "process_stopped"
  reporter.success()

  def dead(*args):
    raise ProcessLookupError()

  with monkeypatch.context() as patch:
    patch.setattr(health.os, "kill", dead)
    assert health.snapshot(params)["code"] == "process_stopped"
  monkeypatch.setattr(health, "boot_id", lambda: "new-boot")
  result = health.snapshot(params)
  assert result["code"] == "process_stopped" and result["historyOnly"]
  health.TelemetryHealth(config)
  result = health.snapshot(params)
  assert result["code"] == "starting" and result["historyOnly"] and result["lastSuccessAt"]


@pytest.mark.parametrize(
  "change,code",
  [
    ({"enabled": False}, "disabled"),
    ({"registered": False, "enabled": False}, "not_registered"),
    ({"device_id": "fedcba9876543210"}, "device_mismatch"),
    ({"endpoint": "http://invalid"}, "config_invalid"),
    ({"token": "bad"}, "config_invalid"),
  ],
)
def test_config_problems_are_distinct(configured, change, code):
  params, config = configured
  runtime.write_json(runtime.CONFIG_PATH, {**config, **change})
  assert health.snapshot(params)["code"] == code


def test_missing_config_no_record_and_other_identity(configured):
  params, config = configured
  assert health.snapshot(params)["code"] == "no_record"
  reporter = health.TelemetryHealth(config)
  reporter.success()
  runtime.write_json(runtime.CONFIG_PATH, {**config, "token": "wayon_" + "other" * 10})
  result = health.snapshot(params)
  assert result["code"] == "no_record" and result["lastSuccessAt"] is None
  runtime.CONFIG_PATH.unlink()
  assert health.snapshot(params)["code"] == "config_missing"


def test_bad_status_fields_fail_closed(configured, clock):
  params, config = configured
  record = {
    "identity": health.identity(config),
    "boot": health.boot_id(),
    "at": clock[0],
    "pid": 10**300,
    "error": {"secret": "value"},
    "http_status": "secret",
    "retry_at": float("inf"),
    "last_success_at": 10**300,
  }
  health.STATUS_PATH.write_text(json.dumps(record))
  result = health.snapshot(params)
  assert result["code"] == "process_stopped"
  assert result["httpStatus"] is result["lastError"] is result["lastSuccessAt"] is None
  assert "secret" not in json.dumps(result)


def test_ram_heartbeat_and_history_writes_are_throttled_even_on_restart(configured, clock, monkeypatch):
  _, config = configured
  writes = []
  real_write = runtime.write_json

  def write(path, value):
    writes.append(path)
    real_write(path, value)

  monkeypatch.setattr(runtime, "write_json", write)
  reporter = health.TelemetryHealth(config)
  reporter.heartbeat()
  assert writes == [health.STATUS_PATH]
  clock[0] += 15
  reporter.heartbeat()
  assert writes.count(health.STATUS_PATH) == 2
  reporter.failed(ValueError("secret"), "payload")
  assert writes.count(health.HISTORY_PATH) == 1
  for _ in range(3):
    clock[0] += 1
    reporter = health.TelemetryHealth(config)
    reporter.failed(ValueError("same failure"), "payload")
  assert writes.count(health.HISTORY_PATH) == 1
  clock[0] += 300
  reporter.failed(ValueError("same failure"), "payload")
  assert writes.count(health.HISTORY_PATH) == 2
  reporter.success()
  assert writes.count(health.HISTORY_PATH) == 3


def test_diagnostics_write_failure_does_not_block_telemetry(configured, monkeypatch):
  _, config = configured

  def fail(*args):
    raise OSError("disk full")

  monkeypatch.setattr(runtime, "write_json", fail)
  reporter = health.TelemetryHealth(config)
  reporter.attempt(True)
  reporter.failed(transport.UploadHTTPError(500), "upload", 30)
  reporter.success()
  assert reporter.record["result"] == "success"


@pytest.mark.parametrize("stage", ["initialize", "payload"])
def test_main_records_initialization_and_payload_exceptions(configured, monkeypatch, stage):
  monkeypatch.setattr(telemetry, "CONFIG_PATH", runtime.CONFIG_PATH)

  def broken(reporter):
    reporter.stage = stage
    raise TypeError("secret")

  monkeypatch.setattr(telemetry, "run", broken)
  with pytest.raises(TypeError):
    telemetry.main()
  record = runtime.read_json(health.STATUS_PATH)
  assert record["error"] == stage and "secret" not in json.dumps(record)


@pytest.mark.parametrize("response,expected", [({"ok": True}, "success"), ({}, "failure"), ({"ok": False}, "failure"), ([], "failure")])
def test_run_requires_cloud_ack_not_merely_http_success(configured, monkeypatch, response, expected):
  _, config = configured
  sm = SimpleNamespace(index=-1)

  def update(*args):
    sm.index += 1

  sm.update = update
  monkeypatch.setattr(telemetry.messaging, "SubMaster", lambda *a, **kw: sm)
  monkeypatch.setattr(telemetry, "read_config", lambda _: config if sm.index < 0 else {})
  monkeypatch.setattr(telemetry, "service_fresh", lambda *a: False)
  monkeypatch.setattr(telemetry, "feature_status", lambda p: {})
  monkeypatch.setattr(telemetry, "telemetry_payload", lambda *a: {"onroad": False, "gps": {"fresh": True}})
  monkeypatch.setattr(telemetry, "post_json", lambda *a: response)

  def cache_error(*args):
    raise OSError("disk full")

  monkeypatch.setattr(telemetry, "write_json", cache_error)
  reporter = health.TelemetryHealth(config)
  telemetry.run(reporter)
  assert reporter.record["result"] == expected
  if expected == "success":
    assert reporter.record["cache_warning"] is True
    assert health.snapshot(configured[0])["code"] == "cache"
  else:
    assert reporter.record["error"] == "response"


def test_transport_invalid_payload_and_response_are_redacted(configured, monkeypatch):
  _, config = configured
  with pytest.raises(transport.UploadPayloadError, match="Invalid upload data"):
    transport.post_json(config, "/api/telemetry", {"secret": float("nan")})

  def invalid_json():
    raise ValueError("secret response body")

  monkeypatch.setattr(transport.requests, "post", lambda *a, **kw: SimpleNamespace(status_code=200, content=b"secret", json=invalid_json))
  with pytest.raises(transport.UploadResponseError, match="Invalid cloud response") as exc:
    transport.post_json(config, "/api/telemetry", {})
  assert "secret" not in str(exc.value)
