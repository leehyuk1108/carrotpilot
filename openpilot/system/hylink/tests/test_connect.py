import http.client
import json
import os
import socket
import threading
import time
from types import SimpleNamespace

import pytest

from openpilot.system.hylink import connect_server, pairing, runtime, setup, telemetry
from openpilot.system.hylink.tests import test_integration
from openpilot.system.manager.process_config import managed_processes

configured = test_integration.configured


def test_local_subnet_only_and_no_dns_rebinding():
  interfaces = {"wlan0": [SimpleNamespace(family=socket.AF_INET, address="192.168.1.2", netmask="255.255.255.0")]}
  assert connect_server.same_lan("192.168.1.2:1108", "192.168.1.50", interfaces)
  for host, peer in [("evil.example:1108", "192.168.1.50"), ("192.168.1.2:1108", "192.168.2.50"),
                     ("192.168.1.2:1108", "8.8.8.8"), ("192.168.1.3:1108", "192.168.1.50"),
                     ("192.168.1.2:80", "192.168.1.50"), ("evil@192.168.1.2:1108", "192.168.1.50")]:
    assert not connect_server.same_lan(host, peer, interfaces)
  assert not connect_server.same_lan("192.168.1.2:1108", "192.168.1.50", {"tun0": interfaces["wlan0"]})


def test_local_key_reuses_existing_identity_without_extra_code(configured):
  params, config = configured
  assert pairing.status(params)["key"] == config["token"]
  result = pairing.connect(params, {})
  assert result["key"] == config["token"]
  assert all(result[feature] for feature in ("enabled", "media", "impact", "remote"))
  assert runtime.remote_ready(False, params)
  params.put_bool("IsOnroad", True)
  with pytest.raises(ValueError):
    pairing.status(params)
  with pytest.raises(ValueError):
    pairing.connect(params, {"consent": True})
  assert not runtime.remote_ready(False, params)


def test_first_registration_rechecks_ignition_before_enabling_defaults(configured):
  params, config = configured
  runtime.CONFIG_PATH.unlink()
  def enroll(params, activate):
    assert activate is False
    params.put_bool("IsOnroad", True)
    return {**config, "enabled": False}
  with pytest.raises(ValueError, match="차량 상태"):
    pairing.connect(params, {}, enroll)
  assert not runtime.CONFIG_PATH.exists()


def test_pending_registration_does_not_enable_until_offroad_recheck(configured):
  params, _ = configured
  runtime.CONFIG_PATH.unlink()
  result = setup.enroll(params, lambda *a, **k: SimpleNamespace(status_code=200), activate=False)
  assert result["registered"] is True and result["enabled"] is False
  assert pairing.status(params)["key"] == result["token"]
  assert not runtime.enabled(False, params, None)


def test_first_page_connection_enables_every_feature_without_settings(configured):
  params, _ = configured
  runtime.CONFIG_PATH.unlink()
  def enroll(params, activate):
    return setup.enroll(params, lambda *a, **k: SimpleNamespace(status_code=200), activate=activate)
  result = pairing.connect(params, {}, enroll)
  assert all(result[feature] for feature in ("enabled", "media", "impact", "remote"))
  assert runtime.remote_ready(False, params)
  assert runtime.media_ready(False, params)
  assert runtime.impact_ready(False, params)


def test_existing_disabled_features_upgrade_but_key_get_never_reenables(configured):
  params, config = configured
  config.update(enabled=False, media_enabled=False, impact_enabled=False, remote_enabled=False)
  runtime.write_json(runtime.CONFIG_PATH, config)
  assert not pairing.status(params)["enabled"]
  assert runtime.read_json(runtime.CONFIG_PATH) == config
  # Even a cached old page with unchecked options receives the new defaults.
  result = pairing.connect(params, {"media": False, "impact": False, "remote": False})
  assert result["key"] == config["token"]
  assert all(result[feature] for feature in ("enabled", "media", "impact", "remote"))
  config = runtime.read_json(runtime.CONFIG_PATH)
  config["enabled"] = False
  runtime.write_json(runtime.CONFIG_PATH, config)
  assert not pairing.status(params)["enabled"]
  assert runtime.read_json(runtime.CONFIG_PATH) == config


def test_auto_connection_rejects_another_vehicle_identity(configured):
  params, config = configured
  params.put("DongleId", "fedcba9876543210")
  with pytest.raises(ValueError, match="different device"):
    pairing.connect(params, {})
  assert runtime.read_json(runtime.CONFIG_PATH) == config


def test_network_failure_keeps_enrollment_identity(configured):
  params, _ = configured
  runtime.CONFIG_PATH.unlink()
  def enroll(params, activate):
    return setup.enroll(params, lambda *a, **k: SimpleNamespace(status_code=503), activate=activate)
  with pytest.raises(RuntimeError):
    pairing.connect(params, {}, enroll)
  pending = runtime.read_json(runtime.CONFIG_PATH)
  assert not pending["enabled"] and pending["token"].startswith("wayon_")


def test_http_key_page_csrf_and_onroad_cutoff(configured):
  params, config = configured
  server = connect_server.ConnectServer(("127.0.0.1", 0), params)
  thread = threading.Thread(target=server.serve_forever, daemon=True)
  thread.start()
  def request(method, path, body=None, **extra):
    connection = http.client.HTTPConnection(*server.server_address, timeout=2)
    headers = {"Host": "127.0.0.1:1108", **extra}
    try:
      connection.request(method, path, body, headers)
      response = connection.getresponse()
      return response.status, dict(response.getheaders()), response.read().decode()
    finally:
      connection.close()
  try:
    code, headers, body = request("GET", "/")
    assert code == 200 and config["token"] not in body
    assert body.count("<button") == 2 and 'type="checkbox"' not in body
    assert 'id="setup"' not in body and 'id="copy"' in body
    assert runtime.read_json(runtime.CONFIG_PATH) == config
    assert headers["Cache-Control"] == "no-store" and "Access-Control-Allow-Origin" not in headers
    code, _, body = request("GET", "/api/key", **{"X-Hylink-Request": "1"})
    assert code == 200 and json.loads(body)["key"] == config["token"]
    assert request("GET", "/api/key")[0] == 403
    assert request("GET", "/api/health")[0] == 403
    code, _, result = request("GET", "/api/health", **{"X-Hylink-Request": "1"})
    assert code == 200 and json.loads(result)["code"] == "no_record"
    assert config["token"] not in result and config["device_id"] not in result
    assert runtime.read_json(runtime.CONFIG_PATH) == config  # Read-only diagnostics.
    runtime.STATE_PATH.unlink()  # Broken guard must not hide diagnostics or expose a key.
    assert request("GET", "/")[0] == 200
    assert request("GET", "/api/health", **{"X-Hylink-Request": "1"})[0] == 200
    assert request("GET", "/api/key", **{"X-Hylink-Request": "1"})[0] == 409
    assert request("POST", "/api/connect", "{}", **{"Content-Type": "application/json", "Origin": "http://127.0.0.1:1108",
                                                     "X-Hylink-Request": "1"})[0] == 400
    params.put_bool("IsOnroad", True)
    assert request("GET", "/api/health", **{"X-Hylink-Request": "1"})[0] == 409
    params.put_bool("IsOnroad", False)
    runtime.write_json(runtime.STATE_PATH, {"pid": os.getpid(), "at": time.monotonic(), "offroad": True})
    assert request("GET", "/", Host="evil.example:1108")[0] == 403
    body = "{}"
    assert request("POST", "/api/connect", body, **{"Content-Type": "application/json", "Origin": "http://evil.example", "X-Hylink-Request": "1"})[0] == 403
    code, _, result = request("POST", "/api/connect", body, **{"Content-Type": "application/json", "Origin": "http://127.0.0.1:1108", "X-Hylink-Request": "1"})
    assert code == 200 and all(json.loads(result)[key] for key in ("enabled", "media", "impact", "remote"))
    params.put_bool("IsOnroad", True)
    assert request("GET", "/api/key", **{"X-Hylink-Request": "1"})[0] == 409
    assert request("GET", "/")[0] == 409
  finally:
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def test_onroad_has_no_key_server_guard_camera_imu_or_remote_process(configured):
  params, _ = configured
  for name in ("hylink_connect", "hylink_guard", "hylink_worker", "hylink_impact", "hylink_live", "hylink_relay", "hylink_remote", "hylink_encoderd"):
    assert not managed_processes[name].should_run(True, params, None), name
  assert managed_processes["hylink_telemetry"].should_run(True, params, None)
  features = telemetry.feature_status(params)
  assert features["liveReady"] and features["impactReady"] and not features["remoteReady"]
  params.put_bool("IsOnroad", True)
  features = telemetry.feature_status(params)
  assert not features["liveReady"] and not features["impactReady"] and not features["remoteReady"]


def test_telemetry_stays_onroad_and_transitions_do_not_wait_for_heartbeat(monkeypatch):
  samples = [(0, False), (1, True), (2, True), (3, False)]
  class Subscriber:
    index = -1
    device = SimpleNamespace(started=False)
    def update(self, timeout):
      self.index += 1
      self.device.started = samples[self.index][1]
    def __getitem__(self, name):
      return self.device
  sm = Subscriber()
  monkeypatch.setattr(telemetry, "Params", lambda: object())
  monkeypatch.setattr(telemetry.messaging, "SubMaster", lambda *a, **k: sm)
  monkeypatch.setattr(telemetry, "read_config", lambda _: {"device_id": "test"} if sm.index < len(samples) - 1 else {})
  monkeypatch.setattr(telemetry, "service_fresh", lambda *a: True)
  monkeypatch.setattr(telemetry.time, "monotonic", lambda: samples[sm.index][0])
  monkeypatch.setattr(telemetry, "feature_status", lambda p: {})
  monkeypatch.setattr(telemetry, "telemetry_payload", lambda *a: {"onroad": sm.device.started, "gps": {}})
  uploads = []
  monkeypatch.setattr(telemetry, "post_json", lambda config, path, body: uploads.append(body["onroad"]) or {"ok": True})
  telemetry.main()
  assert uploads == [False, True, False]


def test_unchanged_driving_data_is_not_serialized_at_subscriber_rate(monkeypatch):
  class Subscriber:
    index = -1
    def update(self, timeout):
      self.index += 1
    def __getitem__(self, name):
      return SimpleNamespace(started=True)
  sm = Subscriber()
  monkeypatch.setattr(telemetry, "Params", lambda: object())
  monkeypatch.setattr(telemetry.messaging, "SubMaster", lambda *a, **k: sm)
  monkeypatch.setattr(telemetry, "read_config", lambda _: {"device_id": "test"} if sm.index < 12 else {})
  monkeypatch.setattr(telemetry, "service_fresh", lambda *a: True)
  monkeypatch.setattr(telemetry.time, "monotonic", lambda: sm.index)
  monkeypatch.setattr(telemetry, "feature_status", lambda p: {})
  builds, uploads = [], []
  def payload(*args):
    builds.append(sm.index)
    return {"onroad": True, "gps": {}}
  monkeypatch.setattr(telemetry, "telemetry_payload", payload)
  monkeypatch.setattr(telemetry, "post_json", lambda *args: uploads.append(sm.index) or {"ok": True})
  telemetry.main()
  assert builds == [0, 5, 10]
  assert uploads == [0]


def test_replaced_key_does_not_wait_for_old_telemetry_backoff(monkeypatch):
  class Subscriber:
    index = -1
    def update(self, timeout):
      self.index += 1
    def __getitem__(self, name):
      return SimpleNamespace(started=False)
  sm = Subscriber()
  monkeypatch.setattr(telemetry, "Params", lambda: object())
  monkeypatch.setattr(telemetry.messaging, "SubMaster", lambda *a, **k: sm)
  monkeypatch.setattr(telemetry, "read_config", lambda _: {"device_id": "test", "token": "old" if sm.index < 0 else "new"} if sm.index < 3 else {})
  monkeypatch.setattr(telemetry, "service_fresh", lambda *a: True)
  monkeypatch.setattr(telemetry.time, "monotonic", lambda: sm.index)
  monkeypatch.setattr(telemetry, "feature_status", lambda p: {})
  monkeypatch.setattr(telemetry, "telemetry_payload", lambda *a: {"onroad": False, "gps": {}})
  uploads = []
  def post(config, *args):
    uploads.append((config["token"], sm.index))
    if config["token"] == "old":
      raise RuntimeError("expired key")
    return {"ok": True}
  monkeypatch.setattr(telemetry, "post_json", post)
  telemetry.main()
  assert uploads == [("old", 0), ("new", 1)]
