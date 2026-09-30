import hashlib
import http.client
import json
import threading
from types import SimpleNamespace

import pytest
import requests

from openpilot.system.hylink import connect_server, pairing, reissue, runtime
from openpilot.system.hylink.tests.test_integration import configured

__all__ = ["configured"]


def proof(*args):
  return "synthetic-device-proof"


def success(url, **kwargs):
  assert url == runtime.ENDPOINT + "/api/devices/reissue"
  assert kwargs["allow_redirects"] is False and kwargs["timeout"] == (5, 15)
  body = kwargs["json"]
  assert body["proof"] == "synthetic-device-proof"
  assert reissue.read_private(reissue.pending_path())["token"] == body["key"]
  assert reissue.pending_path().stat().st_mode & 0o777 == 0o600
  result = {"ok": True, "deviceId": body["deviceId"], "requestId": body["requestId"],
            "keyHash": hashlib.sha256(body["key"].encode()).hexdigest()}
  return SimpleNamespace(status_code=200, json=lambda: result)


def test_explicit_reissue_preserves_settings_and_durable_key(configured):
  params, before = configured
  after = reissue.replace_key(params, post=success, proof=proof)
  assert after["token"] != before["token"]
  assert after["device_id"] == before["device_id"] and after["registered"]
  assert all(after[k] for k in ("enabled", "media_enabled", "impact_enabled", "remote_enabled"))
  assert runtime.read_json(runtime.CONFIG_PATH) == after
  assert runtime.CONFIG_PATH.stat().st_mode & 0o777 == 0o600
  assert not reissue.pending_path().exists()
  assert pairing.status(params)["key"] == after["token"]


@pytest.mark.parametrize("failure", ["network", "invalid_json", "wrong_ack", "auth", "undeployed"])
def test_failure_keeps_old_config_and_retries_same_candidate(configured, failure):
  params, before = configured
  def fail(url, **kwargs):
    if failure == "network":
      raise requests.Timeout("sensitive upstream diagnostic")
    if failure == "invalid_json":
      return SimpleNamespace(status_code=200, json=list)
    if failure == "wrong_ack":
      return SimpleNamespace(status_code=200, json=lambda: {"ok": True, "keyHash": "wrong"})
    return SimpleNamespace(status_code=404 if failure == "undeployed" else 403,
                           json=lambda: {"error": "device_proof_rejected"})
  with pytest.raises(reissue.ReissueError) as error:
    reissue.replace_key(params, post=fail, proof=proof)
  assert "sensitive" not in str(error.value)
  pending = reissue.read_private(reissue.pending_path())
  assert runtime.read_json(runtime.CONFIG_PATH) == before
  assert pairing.status(params) == {"ready": False}  # Old key might already be revoked.
  after = reissue.replace_key(params, resume_only=True, post=success, proof=proof)
  assert after["token"] == pending["token"]


def test_resume_on_page_reopen_uses_same_operation(configured, monkeypatch):
  params, _ = configured
  def timeout(*args, **kwargs):
    raise requests.Timeout()
  with pytest.raises(reissue.ReissueError):
    reissue.replace_key(params, post=timeout, proof=proof)
  pending = reissue.read_private(reissue.pending_path())
  monkeypatch.setattr(reissue.requests, "post", success)
  monkeypatch.setattr(reissue, "device_proof", proof)
  result = pairing.connect(params, {})
  assert result["key"] == pending["token"]


def test_local_save_failure_after_server_commit_keeps_candidate(configured, monkeypatch):
  params, before = configured
  write = reissue.durable_write
  def fail_config(path, value):
    if path == runtime.CONFIG_PATH:
      raise OSError("disk full")
    write(path, value)
  monkeypatch.setattr(reissue, "durable_write", fail_config)
  with pytest.raises(OSError):
    reissue.replace_key(params, post=success, proof=proof)
  pending = reissue.read_private(reissue.pending_path())
  assert runtime.read_json(runtime.CONFIG_PATH) == before
  monkeypatch.setattr(reissue, "durable_write", write)
  after = reissue.replace_key(params, resume_only=True, post=success, proof=proof)
  assert after["token"] == pending["token"]


def test_ignition_transition_saves_new_key_without_enabling(configured):
  params, before = configured
  def switch_ignition(url, **kwargs):
    response = success(url, **kwargs)
    params.put_bool("IsOnroad", True)
    return response
  with pytest.raises(reissue.ReissueError, match="새 키를 저장"):
    reissue.replace_key(params, post=switch_ignition, proof=proof)
  after = runtime.read_json(runtime.CONFIG_PATH)
  assert after["token"] != before["token"] and after["registered"]
  assert not any(after[k] for k in ("enabled", "media_enabled", "impact_enabled", "remote_enabled"))
  assert not reissue.pending_path().exists()
  with pytest.raises(ValueError):
    pairing.status(params)


def test_onroad_or_stale_guard_never_signs_or_sends(configured):
  params, before = configured
  params.put_bool("IsOnroad", True)
  with pytest.raises(reissue.ReissueError):
    reissue.replace_key(params)
  params.put_bool("IsOnroad", False)
  runtime.STATE_PATH.unlink()
  with pytest.raises(reissue.ReissueError):
    reissue.replace_key(params)
  assert runtime.read_json(runtime.CONFIG_PATH) == before
  assert not reissue.pending_path().exists()


@pytest.mark.parametrize("broken", ["{}", "[]", "{broken", "x" * 65537])
def test_corrupt_journal_is_not_overwritten(configured, broken):
  params, before = configured
  reissue.pending_path().write_text(broken)
  with pytest.raises(reissue.ReissueError):
    reissue.replace_key(params, post=success, proof=proof)
  assert reissue.pending_path().read_text() == broken
  assert runtime.read_json(runtime.CONFIG_PATH) == before


def test_wrong_device_journal_never_sent(configured):
  params, before = configured
  reissue.durable_write(reissue.pending_path(), {"device_id": "fedcba9876543210", "request_id": "a" * 32, "token": "wayon_" + "a" * 43})
  with pytest.raises(reissue.ReissueError):
    reissue.replace_key(params, post=success, proof=proof)
  assert runtime.read_json(runtime.CONFIG_PATH) == before


def test_real_proof_binds_operation_and_key(configured, monkeypatch):
  import jwt
  from cryptography.hazmat.primitives.asymmetric import rsa
  from cryptography.hazmat.primitives.serialization import Encoding, PrivateFormat, NoEncryption
  from openpilot.common import api, time_helpers
  params, before = configured
  private = rsa.generate_private_key(65537, 2048)
  pem = private.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
  monkeypatch.setattr(api, "get_key_pair", lambda: ("RS256", pem, None))
  monkeypatch.setattr(time_helpers, "system_time_valid", lambda: True)
  token = reissue.device_proof(before["device_id"], "a" * 32, "b" * 64)
  payload = jwt.decode(token, private.public_key(), algorithms=["RS256"])
  assert payload["identity"] == before["device_id"]
  assert payload["exp"] - payload["iat"] == 300
  assert payload["hylink"] == {"purpose": "key-reissue-v1", "requestId": "a" * 32, "newKeyHash": "b" * 64}


def test_http_reissue_lan_csrf_offroad_and_shared_lock(configured, monkeypatch):
  params, before = configured
  monkeypatch.setattr(reissue, "device_proof", proof)
  monkeypatch.setattr(reissue.requests, "post", success)
  server = connect_server.ConnectServer(("127.0.0.1", 0), params)
  thread = threading.Thread(target=server.serve_forever, daemon=True)
  thread.start()
  def request(method="POST", **extra):
    connection = http.client.HTTPConnection(*server.server_address, timeout=2)
    headers = {"Host": "127.0.0.1:1108", "Origin": "http://127.0.0.1:1108",
               "Content-Type": "application/json", "X-Hylink-Request": "1", **extra}
    try:
      connection.request(method, "/api/reissue", "{}", headers)
      response = connection.getresponse()
      return response.status, json.loads(response.read())
    finally:
      connection.close()
  try:
    assert request(Origin="http://evil.invalid")[0] == 403
    assert request(Host="evil.invalid:1108")[0] == 403
    assert request(**{"X-Hylink-Request": ""})[0] == 403
    assert request("GET")[0] == 404
    server.pair_lock.acquire()
    assert request()[0] == 409
    server.pair_lock.release()
    params.put_bool("IsOnroad", True)
    assert request()[0] == 503
    assert runtime.read_json(runtime.CONFIG_PATH) == before
    params.put_bool("IsOnroad", False)
    code, result = request()
    assert code == 200 and result["key"] != before["token"] and result["enabled"]
    assert "proof" not in json.dumps(result)
  finally:
    server.shutdown()
    server.server_close()
    thread.join()
