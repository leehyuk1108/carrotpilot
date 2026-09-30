"""Payload-free upload diagnostics. The local page never probes the cloud.

Heartbeat writes stay in tmpfs. Persist only completed attempts, at most once
per five minutes for unchanged results, so a reboot retains useful history.
"""

import hashlib
import math
import os
import socket
import time
from datetime import datetime, UTC
from functools import lru_cache
from pathlib import Path

import psutil
import requests

from openpilot.system.hylink import runtime
from openpilot.system.hylink.transport import UploadHTTPError, UploadPayloadError, UploadResponseError

STATUS_PATH = runtime.RUNTIME_ROOT / "telemetry.json"
HISTORY_PATH = runtime.CONFIG_PATH.with_name("telemetry_history.json")
HEARTBEAT_INTERVAL = 15
HEARTBEAT_TTL = 45
HISTORY_INTERVAL = 300

# All public descriptions are fixed strings. Never expose exceptions, payloads,
# URLs, device IDs, tokens, or arbitrary strings read from a status file.
MESSAGES = {
  "ok": ("success", "최근 업로드 성공", "클라우드가 차량 정보 저장을 확인했어요.", ""),
  "starting": ("waiting", "첫 업로드 확인 중", "키 등록과 실제 업로드는 별도로 확인해요.", "잠시 기다려 주세요. 주차 중에는 최대 5분 간격으로 전송해요."),
  "uploading": ("waiting", "차량 정보 전송 중", "클라우드의 응답을 기다리고 있어요.", ""),
  "no_record": (
    "warning",
    "전송 기록 확인 필요",
    "이 버전에서 기록한 송신 프로그램 상태가 아직 없어요.",
    "처음 연결했다면 잠시 기다려 주세요. 계속 같다면 실행 중인 버전과 hylink_telemetry 로그를 확인해 주세요.",
  ),
  "process_stopped": (
    "error",
    "송신 프로그램 동작 확인 필요",
    "최근 동작 기록이 없거나 기록한 프로그램이 종료됐어요.",
    "콤마의 hylink_telemetry 실행 로그를 확인해 주세요. 재부팅만으로 반복 오류가 해결되지는 않아요.",
  ),
  "stale": ("warning", "업로드가 지연되고 있어요", "프로그램은 응답하지만 정기 전송 시각을 넘겼어요.", "콤마 인터넷과 송신 프로그램 로그를 확인해 주세요."),
  "config_missing": (
    "error",
    "차량 연결 설정 확인 필요",
    "콤마의 연결 설정이 없거나 읽을 수 없어요.",
    "이 페이지를 다시 열어 연결을 완료해 주세요. 기존 키나 설정 파일을 임의로 삭제하지 마세요.",
  ),
  "config_invalid": (
    "error",
    "차량 연결 설정 오류",
    "서버 주소 또는 키 형식이 올바르지 않아요.",
    "설정 파일을 임의로 바꾸지 말고 설치한 브랜치와 연결 설정을 확인해 주세요.",
  ),
  "not_registered": (
    "warning",
    "클라우드 등록 미완료",
    "연결 키는 준비됐지만 서버 등록이 아직 완료되지 않았어요.",
    "위의 연결 오류 안내와 콤마 인터넷을 확인한 뒤 이 페이지를 다시 열어 주세요.",
  ),
  "device_mismatch": (
    "error",
    "기기 등록 정보 불일치",
    "저장된 연결 설정이 현재 콤마의 기기 ID와 달라요.",
    "다른 기기의 설정을 복사하지 말고 이 콤마의 등록 정보를 확인해 주세요.",
  ),
  "disabled": (
    "warning",
    "정보 전송이 중지돼 있어요",
    "앱에 유효한 키가 있어도 콤마의 전송은 따로 중지될 수 있어요.",
    "다시 연결하려면 시동을 끈 뒤 이 페이지를 새로 열어 주세요.",
  ),
  "auth": (
    "error",
    "클라우드 인증 실패",
    "클라우드가 콤마의 연결 키를 받아들이지 않았어요.",
    "현재 키로 계속 연결되지 않으면 위의 ‘키 재발급’을 눌러 주세요. 새 키는 앱에 다시 입력해야 해요.",
  ),
  "enrollment_conflict": (
    "error",
    "기존 등록 키와 불일치",
    "이 기기가 서버에 다른 키로 등록돼 있어요.",
    "위의 ‘키 재발급’으로 이 기기의 키를 교체한 뒤 앱에 새 키를 입력해 주세요.",
  ),
  "rate_limited": (
    "error",
    "클라우드 요청 제한",
    "서버가 요청을 잠시 제한했어요.",
    "자동 재시도를 기다려 주세요. 계속되면 서버의 사용량과 제한을 확인해야 해요.",
  ),
  "server": ("error", "클라우드 서버 오류", "서버가 요청을 정상 처리하지 못했어요.", "자동 재시도를 기다려 주세요. 계속되면 서버 상태를 확인해야 해요."),
  "http": ("error", "서버가 요청을 거부했어요", "업로드 요청에 정상 응답을 받지 못했어요.", "아래 HTTP 상태와 설치한 브랜치를 전달해 주세요."),
  "tls": (
    "error",
    "보안 연결 실패",
    "콤마에서 서버의 보안 연결을 확인하지 못했어요.",
    "콤마 날짜·시간, 인터넷 로그인 화면, 인증서 및 네트워크 차단 여부를 확인해 주세요.",
  ),
  "timeout": (
    "error",
    "서버 응답 시간 초과",
    "콤마가 제한 시간 안에 응답을 받지 못했어요.",
    "휴대폰이 아니라 콤마 자체의 Wi-Fi·핫스팟 인터넷을 확인해 주세요.",
  ),
  "dns": ("error", "서버 주소를 찾지 못했어요", "콤마에서 클라우드 주소를 해석하지 못했어요.", "콤마의 인터넷과 DNS 설정을 확인해 주세요."),
  "network": (
    "error",
    "서버에 연결하지 못했어요",
    "콤마에서 클라우드로 연결하지 못했어요.",
    "콤마의 인터넷, Wi-Fi·핫스팟 및 네트워크 차단 여부를 확인해 주세요.",
  ),
  "payload": (
    "error",
    "차량 데이터 형식 오류",
    "전송할 수 없는 값이 있어 차량 정보 업로드가 실패했어요.",
    "센서값·데이터 생성 로그를 확인해야 해요. 키를 다시 입력해서 해결되는 문제는 아니에요.",
  ),
  "response": (
    "error",
    "서버 저장 확인 실패",
    "요청 후 정상적인 저장 완료 응답을 확인하지 못했어요.",
    "서버·네트워크 상태를 확인해 주세요. 실제 저장 여부는 아직 확정할 수 없어요.",
  ),
  "initialize": ("error", "송신 프로그램 초기화 오류", "프로그램 초기화 중 오류가 발생했어요.", "설치한 버전과 hylink_telemetry 시작 로그를 확인해 주세요."),
  "internal": ("error", "송신 프로그램 오류", "정보 전송 중 내부 오류가 발생했어요.", "아래 진단 코드와 hylink_telemetry 로그를 전달해 주세요."),
  "cache": (
    "warning",
    "업로드 성공 · 로컬 저장 확인 필요",
    "클라우드는 저장을 확인했지만 콤마의 최근 위치 캐시를 저장하지 못했어요.",
    "콤마 저장 공간과 파일 권한을 확인해 주세요.",
  ),
}


@lru_cache(maxsize=1)
def boot_id():
  try:
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
  except OSError:
    return str(psutil.boot_time())


def identity(config):
  values = [config.get(key) for key in ("endpoint", "device_id", "token")]
  if not all(isinstance(value, str) and value for value in values):
    return ""
  return hashlib.sha256("\0".join(values).encode()).hexdigest()


def number(value):
  try:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None
  except OverflowError:
    return None


def timestamp(value):
  try:
    return datetime.fromtimestamp(value, UTC).isoformat(timespec="seconds").replace("+00:00", "Z") if number(value) else None
  except (ValueError, OSError, OverflowError):
    return None


def failure(error, stage):
  status = error.status_code if isinstance(error, UploadHTTPError) else None
  if status is not None:
    code = (
      "auth"
      if status in (401, 403)
      else "enrollment_conflict"
      if status == 409 and stage == "enrollment"
      else "rate_limited"
      if status == 429
      else "server"
      if status >= 500
      else "http"
    )
  elif isinstance(error, requests.exceptions.SSLError):
    code = "tls"
  elif isinstance(error, requests.exceptions.Timeout):
    code = "timeout"
  elif isinstance(error, requests.exceptions.ConnectionError):
    # urllib3 nests name-resolution failures; inspect types, never messages.
    pending, seen = [error], set()
    code = "network"
    while pending and len(seen) < 16:
      item = pending.pop()
      if id(item) in seen:
        continue
      seen.add(id(item))
      if isinstance(item, socket.gaierror) or type(item).__name__ == "NameResolutionError":
        code = "dns"
        break
      pending.extend(
        value
        for value in (getattr(item, "__cause__", None), getattr(item, "__context__", None), getattr(item, "reason", None), *getattr(item, "args", ()))
        if isinstance(value, BaseException)
      )
  elif isinstance(error, UploadPayloadError) or stage == "payload":
    code = "payload"
  elif isinstance(error, UploadResponseError):
    code = "response"
  else:
    code = "initialize" if stage == "initialize" else "internal"
  return {"code": code, "httpStatus": status, "stage": stage if stage in ("initialize", "payload", "upload", "enrollment") else "unknown"}


class TelemetryHealth:
  def __init__(self, config):
    self.identity = identity(config)
    history = runtime.read_json(HISTORY_PATH)
    recent = runtime.read_json(STATUS_PATH)
    old = recent if recent.get("boot") == boot_id() and recent.get("identity") == self.identity else history
    fields = {
      "result",
      "last_success_at",
      "last_success_mono",
      "success_boot",
      "last_attempt_at",
      "last_failure_at",
      "error",
      "http_status",
      "stage",
      "failures",
      "retry_at",
      "interval",
      "cache_warning",
    }
    self.record = {key: value for key, value in old.items() if key in fields} if self.identity and old.get("identity") == self.identity else {}
    same_boot = history.get("boot") == boot_id() and history.get("identity") == self.identity
    self.stage = "initialize"
    self.record.update(identity=self.identity, pid=os.getpid(), boot=boot_id(), phase="starting")
    self.last_write = -math.inf
    self.last_persist = (number(history.get("at")) or -math.inf) if same_boot else -math.inf
    self.persisted_result = tuple(history.get(key) for key in ("result", "error", "http_status", "cache_warning")) if same_boot else None
    self.heartbeat(force=True)

  def bind(self, config):
    current = identity(config)
    if current != self.identity:
      self.identity = current
      self.record = {"identity": current, "pid": os.getpid(), "boot": boot_id(), "phase": "starting"}
      self.last_persist, self.persisted_result = -math.inf, None
    self.heartbeat()

  def heartbeat(self, force=False, persist=False):
    now = time.monotonic()
    if not force and now - self.last_write < HEARTBEAT_INTERVAL:
      return
    self.record.update(at=now)
    self.last_write = now
    try:
      runtime.write_json(STATUS_PATH, self.record)
    except (OSError, ValueError):
      pass  # Diagnostics must never stop telemetry.
    result = self.result_signature()
    if persist and (result != self.persisted_result or now - self.last_persist >= HISTORY_INTERVAL):
      try:
        runtime.write_json(HISTORY_PATH, self.record)
        self.last_persist, self.persisted_result = now, result
      except (OSError, ValueError):
        pass

  def result_signature(self):
    return tuple(self.record.get(key) for key in ("result", "error", "http_status", "cache_warning"))

  def attempt(self, onroad):
    # Wall time is for display only; freshness, retries and throttling use monotonic time.
    self.record.update(phase="uploading", interval=30 if onroad else 300, last_attempt_at=time.time(), retry_at=None)  # noqa: TID251
    self.heartbeat(force=True)

  def success(self, cache_warning=False):
    self.record.update(
      phase="idle",
      result="success",
      last_success_at=time.time(),  # noqa: TID251
      last_success_mono=time.monotonic(),
      success_boot=boot_id(),
      error=None,
      http_status=None,
      stage=None,
      failures=0,
      retry_at=None,
      cache_warning=cache_warning,
    )
    self.heartbeat(force=True, persist=True)

  def failed(self, error, stage, delay=None):
    info = failure(error, stage)
    self.record.update(
      phase="failed",
      result="failure",
      error=info["code"],
      http_status=info["httpStatus"],
      stage=info["stage"],
      last_failure_at=time.time(),  # noqa: TID251
      failures=min(9999, int(number(self.record.get("failures")) or 0) + 1),
      retry_at=time.monotonic() + delay if delay is not None else None,
    )
    self.heartbeat(force=True, persist=True)
    return info


def snapshot(params):
  config = runtime.read_json(runtime.CONFIG_PATH)
  fingerprint = identity(config)
  record = runtime.read_json(STATUS_PATH)
  current = bool(fingerprint and record.get("identity") == fingerprint and record.get("boot") == boot_id())
  if not current:
    record = runtime.read_json(HISTORY_PATH)
    if not fingerprint or record.get("identity") != fingerprint:
      record = {}
  at = number(record.get("at"))
  alive = False
  if current and at is not None and 0 <= time.monotonic() - at <= HEARTBEAT_TTL:
    try:
      pid = record.get("pid")
      if isinstance(pid, int) and pid > 1:
        os.kill(pid, 0)
        alive = True
    except (OSError, ValueError, OverflowError):
      pass
  error = record.get("error")
  error = error if isinstance(error, str) and error in MESSAGES else None
  try:
    valid_config = runtime.read_config(params)
    if not config:
      code = "config_missing"
    elif config.get("device_id") != runtime.param_text(params, "DongleId"):
      code = "device_mismatch"
    elif config.get("registered") is not True:
      code = "not_registered"
    elif config.get("enabled") is not True:
      code = "disabled"
    elif not valid_config:
      code = "config_invalid"
    elif not alive:
      code = "process_stopped" if record else "no_record"
    elif record.get("phase") == "uploading":
      code = "uploading"
    elif record.get("phase") == "starting":
      code = "starting"
    elif record.get("result") == "failure" and error:
      code = error
    elif record.get("success_boot") != boot_id() or number(record.get("last_success_mono")) is None:
      code = "starting"
    else:
      age = time.monotonic() - record["last_success_mono"]
      interval = 30 if record.get("interval") == 30 else 300
      code = ("cache" if record.get("cache_warning") is True else "ok") if 0 <= age <= interval + HEARTBEAT_TTL else "stale"
  except Exception:
    code = "config_invalid"
  level, title, detail, action = MESSAGES[code]
  retry_at = number(record.get("retry_at"))
  retry = max(0, math.ceil(retry_at - time.monotonic())) if alive and retry_at is not None else None
  status = record.get("http_status")
  # Explicit public allowlist; never return the saved record directly.
  return {
    "level": level,
    "code": code,
    "title": title,
    "detail": detail,
    "action": action,
    "lastSuccessAt": timestamp(record.get("last_success_at")),
    "lastAttemptAt": timestamp(record.get("last_attempt_at")),
    "historyOnly": not current or record.get("success_boot") != boot_id(),
    "process": "running" if alive else "unconfirmed",
    "lastError": error,
    "lastErrorTitle": MESSAGES[error][1] if error else None,
    "lastFailureAt": timestamp(record.get("last_failure_at")),
    "httpStatus": status if isinstance(status, int) and 100 <= status <= 599 else None,
    "retryInSeconds": retry if retry is not None and retry <= 300 else None,
  }
