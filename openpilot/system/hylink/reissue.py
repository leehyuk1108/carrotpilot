"""Explicit, offroad-only key replacement with a crash-safe retry journal.

Persist the candidate BEFORE asking the server to revoke the old key. A lost
reply or reboot retries the same operation, never generates yet another key.
The comma private key is used locally for a short-lived, operation-bound proof;
it is never sent to Wayon or exposed to the browser.
"""
import hashlib
import json
import os
import re
import secrets
import tempfile

import requests

from openpilot.system.hylink import runtime


class ReissueError(Exception):
  pass


def pending_path():
  return runtime.CONFIG_PATH.with_name("key-reissue.json")


def read_private(path):
  try:
    with path.open("rb") as stream:
      value = stream.read(65537)
    if len(value) > 65536:
      raise ValueError("size")
    data = json.loads(value)
    if not isinstance(data, dict):
      raise ValueError("shape")
    return data
  except FileNotFoundError:
    return {}
  except (OSError, ValueError, RecursionError) as exc:
    raise ReissueError("저장된 연결 정보를 읽지 못했어요. 설정 파일을 삭제하지 말고 저장 공간을 확인해 주세요.") from exc


def sync_directory(path):
  fd = os.open(path, os.O_RDONLY)
  try:
    os.fsync(fd)
  finally:
    os.close(fd)


def durable_write(path, value):
  # Used only for explicit credential changes, not periodic telemetry writes.
  path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
  fd, name = tempfile.mkstemp(prefix=".key-", dir=path.parent)
  try:
    with os.fdopen(fd, "w") as stream:
      json.dump(value, stream, allow_nan=False)
      stream.flush()
      os.fsync(stream.fileno())
    os.replace(name, path)
    sync_directory(path.parent)
  finally:
    if os.path.exists(name):
      os.unlink(name)


def device_proof(device_id, request_id, key_hash):
  from openpilot.common.api import Api
  from openpilot.common.time_helpers import system_time_valid

  if not system_time_valid():
    raise ReissueError("콤마의 시간이 맞지 않아요. 인터넷에 연결한 뒤 다시 눌러 주세요.")
  try:
    return Api(device_id).get_token({"hylink": {
      "purpose": "key-reissue-v1", "requestId": request_id, "newKeyHash": key_hash,
    }}, expiry_hours=5 / 60)
  except Exception as exc:
    raise ReissueError("콤마의 기기 인증정보를 읽지 못했어요. comma 등록 상태를 확인해 주세요.") from exc


ERRORS = {
  "device_proof_rejected": "콤마의 기기 인증에 실패했어요. 기기 시간과 comma 등록 상태를 확인해 주세요.",
  "authority_unavailable": "comma 인증 서버에 연결하지 못했어요. 인터넷을 확인한 뒤 다시 눌러 주세요.",
  "device_not_registered": "이 기기의 서버 등록을 확인해야 해요. 페이지를 새로 연 뒤에도 같으면 관리자에게 알려 주세요.",
  "reissue_superseded": "다른 재발급 요청과 충돌했어요. 설정 파일을 삭제하지 말고 관리자에게 알려 주세요.",
  "reissue_busy": "다른 재발급 처리가 진행됐어요. 30초 후 다시 눌러 주세요.",
}


def replace_key(params, *, resume_only=False, post=None, proof=None):
  from openpilot.system.hylink.pairing import pairing_allowed

  if not pairing_allowed(params):
    raise ReissueError("시동을 끄고 콤마의 차량 연결·온도 상태를 확인한 뒤 다시 눌러 주세요.")
  device_id = runtime.param_text(params, "DongleId")
  if not re.fullmatch(r"[0-9a-f]{16}", device_id):
    raise ReissueError("comma 기기 등록이 완료된 뒤 다시 눌러 주세요.")
  config = read_private(runtime.CONFIG_PATH)
  if config.get("device_id") not in (None, device_id):
    raise ReissueError("다른 기기의 연결 설정이 저장돼 있어요. 이 콤마의 설정부터 확인해 주세요.")
  pending = read_private(pending_path())
  if not pending:
    if pending_path().exists():
      raise ReissueError("진행 중인 재발급 정보가 비어 있어요. 파일을 삭제하지 말고 관리자에게 알려 주세요.")
    if resume_only:
      raise ReissueError("진행 중인 재발급 정보가 없어요. 페이지를 다시 열어 주세요.")
    pending = {"device_id": device_id, "request_id": secrets.token_hex(16), "token": "wayon_" + secrets.token_urlsafe(32)}
    durable_write(pending_path(), pending)
  if (pending.get("device_id") != device_id or not re.fullmatch(r"[0-9a-f]{32}", str(pending.get("request_id", "")))
      or not re.fullmatch(r"wayon_[A-Za-z0-9_-]{43}", str(pending.get("token", "")))):
    raise ReissueError("진행 중인 재발급 정보가 올바르지 않아요. 파일을 삭제하지 말고 관리자에게 알려 주세요.")
  key_hash = hashlib.sha256(pending["token"].encode()).hexdigest()
  identity = (proof or device_proof)(device_id, pending["request_id"], key_hash)
  if not pairing_allowed(params):
    raise ReissueError("차량 상태가 바뀌었어요. 주차 후 다시 눌러 주세요.")
  try:
    response = (post or requests.post)(runtime.ENDPOINT + "/api/devices/reissue", json={
      "deviceId": device_id, "requestId": pending["request_id"], "key": pending["token"], "proof": identity,
    }, timeout=(5, 15), allow_redirects=False)
    if response.status_code == 404:
      raise ReissueError("서버의 재발급 기능이 아직 준비되지 않았어요. 서버 업데이트 후 다시 눌러 주세요.")
    result = response.json()
    if not isinstance(result, dict):
      raise ValueError("response")
    if response.status_code != 200:
      raise ReissueError(ERRORS.get(result.get("error"), "서버 응답을 확인하지 못했어요. 인터넷을 확인한 뒤 다시 눌러 주세요."))
    if (result.get("ok") is not True or result.get("deviceId") != device_id or result.get("requestId") != pending["request_id"]
        or result.get("keyHash") != key_hash):
      raise ValueError("response")
  except ReissueError:
    raise
  except Exception as exc:
    raise ReissueError("재발급 결과를 확인하지 못했어요. 새 키는 보관돼 있으니 인터넷을 확인한 뒤 다시 눌러 주세요.") from exc
  # The server has replaced the key. Always save it, even if ignition changed
  # while waiting; never reactivate features or expose the key onroad.
  allowed = pairing_allowed(params)
  config.update(device_id=device_id, endpoint=runtime.ENDPOINT, token=pending["token"], registered=True,
                enabled=allowed, media_enabled=allowed, impact_enabled=allowed, remote_enabled=allowed)
  durable_write(runtime.CONFIG_PATH, config)
  pending_path().unlink()
  sync_directory(pending_path().parent)
  if not allowed:
    raise ReissueError("새 키를 저장했어요. 시동을 끈 뒤 이 페이지를 다시 열어 키를 복사해 주세요.")
  return config
