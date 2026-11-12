"""My Traverse (Wayon) device key: a masked view, and the full key only after an explicit, same-origin
request from the comma's own address on the owner's network.

The key lives in /data/wayon_cloud/config.json (outside git). The comma uses it to upload drives and
photos to the My Traverse cloud, so it is never sent to the browser unless the user asks to copy it.

A comma without a key gets its own: it makes a key, proves to the cloud that it is this comma (a short JWT
signed with its comma device key, naming the new key's hash, which the cloud checks with comma's device
API) and registers its DongleId; only then it writes its config. The key is kept in a pending file first,
made durable before the cloud is contacted, so a retry after a lost reply or a failed write reuses the same
key, which the cloud accepts again for the same device. Keys and proofs are never logged.
"""
from __future__ import annotations

import asyncio
import fcntl
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import time
from pathlib import Path
from urllib.parse import urlsplit

from aiohttp import ClientError, ClientSession, ClientTimeout, web

CONFIG_PATH = Path("/data/wayon_cloud/config.json")

# Private LAN, loopback and Tailscale (CGNAT and its IPv6 range). The request must also name the comma's
# own address in Host, which turns away DNS-rebinding hostnames. Requests that carry proxy headers are
# refused too. This only narrows who can reach the key: anyone who can reach Carrot Web from these networks
# can already read the same file through its terminal and tools, so it is not an owner check.
NETWORKS = tuple(ipaddress.ip_network(value) for value in (
  "127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10",
  "::1/128", "fd7a:115c:a1e0::/48",
))
PROXY_HEADERS = ("forwarded", "via", "x-real-ip")
PROXY_HEADER_PREFIX = "x-forwarded-"
SECURITY_HEADERS = {
  "Cache-Control": "no-store",
  "Referrer-Policy": "no-referrer",
  "X-Content-Type-Options": "nosniff",
}
CSRF_HEADER = "X-Carrot-CSRF"
REVEAL_INTERVAL_S = 2.0
MAX_BODY = 1024
STATE_KEY = web.AppKey("wayon_key_state", dict)
FORBIDDEN_TEXT = "The My Traverse key is only available from the comma's own address on your network."
REGISTER_PURPOSE = "wayon-key-register-v1"
PROOF_LIFETIME_H = 4 / 60
DONGLE_ID = re.compile(r"[0-9a-f]{16}")
KEY_PATTERN = re.compile(r"wayon_[A-Za-z0-9_-]{40,96}")
HOST_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
HOSTNAME = re.compile(rf"{HOST_LABEL}(?:\.{HOST_LABEL})+")
ENDPOINT_PATH = re.compile(r"(?:/[A-Za-z0-9._~-]+)*")
PRIVATE_SUFFIXES = (".local", ".localhost", ".internal", ".lan", ".home.arpa", ".test", ".invalid")
CLOUD_TIMEOUT_S = 15.0
CLOUD_MAX_REPLY = 4096
ACTION_INTERVAL_S = 2.0


class CloudUnavailable(Exception):
  pass


def address(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
  parsed = ipaddress.ip_address(value)
  # A dual-stack socket reports IPv4 peers as ::ffff:a.b.c.d; judge them as the IPv4 address they are.
  return getattr(parsed, "ipv4_mapped", None) or parsed


def proxied(headers) -> bool:
  return any(name.lower() in PROXY_HEADERS or name.lower().startswith(PROXY_HEADER_PREFIX) for name in headers)


def forbidden(text: str) -> web.HTTPForbidden:
  return web.HTTPForbidden(text=text, headers=SECURITY_HEADERS)


def bad_request(text: str) -> web.HTTPBadRequest:
  return web.HTTPBadRequest(text=text, headers=SECURITY_HEADERS)


def validate_request(request: web.Request, mutate: bool = False) -> None:
  try:
    peer = address(request.remote)
    local = address(request.transport.get_extra_info("sockname")[0])
    hostname = urlsplit("http://" + request.host).hostname
    target = local if hostname == "localhost" and local.is_loopback else address(hostname)
    if target != local or not any(peer in net for net in NETWORKS):
      raise ValueError("not local")
    if request.headers.get("Sec-Fetch-Site", "same-origin") not in ("same-origin", "none"):
      raise ValueError("cross-site")
    if proxied(request.headers):
      raise ValueError("proxied")
    origin = request.headers.get("Origin")
    if (mutate or origin) and origin != f"{request.scheme}://{request.host}":
      raise ValueError("origin")
  except (ValueError, TypeError, AttributeError, IndexError):
    raise forbidden(FORBIDDEN_TEXT) from None


def read_config() -> tuple[dict | None, str]:
  try:
    data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
  except FileNotFoundError:
    return None, "missing"
  except (OSError, ValueError):
    return None, "unreadable"
  if not isinstance(data, dict):
    return None, "invalid"
  token = data.get("token")
  if not isinstance(token, str) or not token.strip():
    return None, "no_key"
  return {
    "token": token.strip(),
    "device_id": str(data.get("device_id") or "").strip(),
    "endpoint": str(data.get("endpoint") or "").strip(),
  }, "ok"


def mask_token(token: str) -> str:
  if len(token) <= 12:
    return "•" * len(token)
  return f"{token[:4]}…{token[-4:]}"


def response(body: dict, status: int = 200) -> web.Response:
  return web.json_response(body, status=status, headers=SECURITY_HEADERS)


async def get_status(request: web.Request) -> web.Response:
  validate_request(request)
  config, reason = read_config()
  body = {"ok": True, "configured": config is not None, "reason": reason, "csrf": request.app[STATE_KEY]["csrf"]}
  if config is None:
    local_id = dongle_id()
    body.update({"local_device_id": local_id,
                 "can_register": reason == "missing" and not config_slot_taken() and bool(local_id)})
  if config is not None:
    body.update({
      "device_id": config["device_id"],
      "endpoint_host": urlsplit(config["endpoint"]).hostname or "",
      "key_masked": mask_token(config["token"]),
      "key_length": len(config["token"]),
    })
  return response(body)


async def confirmed_body(request: web.Request) -> dict:
  """Same-origin, CSRF-protected, small JSON object with confirmed: true (every action that changes or
  reveals something)."""
  validate_request(request, mutate=True)
  state = request.app[STATE_KEY]
  if not hmac.compare_digest(request.headers.get(CSRF_HEADER, ""), state["csrf"]):
    raise forbidden("Refresh this page before continuing.")
  if request.content_type != "application/json" or (request.content_length or 0) > MAX_BODY:
    raise bad_request("Expected a small JSON request.")
  try:
    raw = bytearray()
    async for chunk in request.content.iter_chunked(512):
      raw.extend(chunk)
      if len(raw) > MAX_BODY:
        raise ValueError("too large")
    body = json.loads(raw)
    if not isinstance(body, dict):
      raise ValueError("not an object")
  except (ValueError, TypeError):
    raise bad_request("Invalid request.") from None
  if body.get("confirmed") is not True:
    raise bad_request("Explicit confirmation is required.")
  return body


async def reveal(request: web.Request) -> web.Response:
  await confirmed_body(request)
  state = request.app[STATE_KEY]

  now = time.monotonic()
  if now - state["last_reveal"] < REVEAL_INTERVAL_S:
    return response({"ok": False, "error": "Wait a moment before copying the key again."}, 429)
  state["last_reveal"] = now

  config, reason = read_config()
  if config is None:
    return response({"ok": False, "reason": reason, "error": "No My Traverse key is set up on this comma."}, 404)
  return response({"ok": True, "key": config["token"], "device_id": config["device_id"]})


def dongle_id() -> str:
  """This comma's DongleId, only when it is a real comma-issued ID (16 lowercase hex characters)."""
  try:
    from openpilot.common.params import Params
    value = Params().get("DongleId")
  except Exception:
    return ""
  if isinstance(value, bytes):
    value = value.decode("utf-8", "replace")
  value = str(value or "").strip()
  return value if DONGLE_ID.fullmatch(value) else ""


def clean_endpoint(value) -> str | None:
  """An https cloud address with a public DNS name, no credentials, query, fragment or odd port."""
  if not isinstance(value, str) or not value or len(value) > 200:
    return None
  try:
    parts = urlsplit(value.strip())
    port = parts.port
  except ValueError:
    return None
  host = parts.hostname or ""
  if (parts.scheme != "https" or parts.username is not None or parts.password is not None or parts.query
      or parts.fragment or port not in (None, 443) or "@" in parts.netloc):
    return None
  if not HOSTNAME.fullmatch(host) or host.replace(".", "").isdigit() or host.endswith(PRIVATE_SUFFIXES):
    return None
  path = parts.path.rstrip("/")
  if not ENDPOINT_PATH.fullmatch(path) or any(part in (".", "..") for part in path.split("/")):
    return None
  return f"https://{host}{path}"


def cloud_endpoint() -> str | None:
  """The My Traverse cloud every comma of this fork talks to (wayon_identity's default)."""
  try:
    from openpilot.system.wayon_identity import DEFAULT_ENDPOINT
  except Exception:
    return None
  return clean_endpoint(DEFAULT_ENDPOINT)


def device_proof(device: str, request_id: str, key_hash: str) -> str:
  """A short-lived JWT signed with this comma's device key (the one comma's servers know), bound to the
  new key's hash and this request. Raises if the comma has no device key."""
  from openpilot.common.api import Api
  api = Api(device)
  if api.private_key is None:
    raise OSError("no comma device key")
  return api.get_token(payload_extra={"wayon": {"purpose": REGISTER_PURPOSE, "requestId": request_id, "newKeyHash": key_hash}},
                       expiry_hours=PROOF_LIFETIME_H)


def write_private_json(path: Path, data: dict, replace: bool = True) -> None:
  """Write a 0600 JSON file atomically (exclusive temp file, fsync, publish, fsync the directory).

  With replace=False the file is published with a hard link, which fails if anything (even a dangling
  symlink) already has that name, so an existing file is never overwritten."""
  path.parent.mkdir(parents=True, exist_ok=True)
  temp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
  fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
  try:
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
      json.dump(data, handle, indent=2)
      handle.write("\n")
      handle.flush()
      os.fsync(handle.fileno())
    if replace:
      os.replace(temp, path)
    else:
      os.link(temp, path)
      temp.unlink()
  except BaseException:
    try:
      temp.unlink()
    except OSError:
      pass
    raise
  fsync_directory(path.parent)


def fsync_directory(directory: Path) -> None:
  """Make a rename or link in this directory durable; an error means it may not be."""
  fd = os.open(directory, os.O_RDONLY)
  try:
    os.fsync(fd)
  finally:
    os.close(fd)


def config_slot_taken() -> bool:
  """Anything at the config path, including a dangling symlink that reads as missing."""
  return CONFIG_PATH.is_symlink() or CONFIG_PATH.exists()


def pending_matches(config: dict) -> bool:
  try:
    data = json.loads(pending_path().read_text(encoding="utf-8"))
  except (OSError, ValueError):
    return False
  return (isinstance(data, dict) and data.get("token") == config["token"] and data.get("endpoint") == config["endpoint"]
          and data.get("device_id") == config["device_id"])


def pending_path() -> Path:
  return CONFIG_PATH.with_name("register.pending.json")


class PendingConflict(Exception):
  pass


def read_pending(endpoint: str, device: str) -> str | None:
  """The key of an earlier attempt for this cloud and this comma, None if there was none. Any other
  pending file (another cloud, another DongleId, unreadable) may hold a key the cloud already accepted,
  so it is never replaced automatically."""
  path = pending_path()
  if not path.exists() and not path.is_symlink():
    return None
  try:
    data = json.loads(path.read_text(encoding="utf-8"))
  except (OSError, ValueError):
    raise PendingConflict("unreadable") from None
  if not (isinstance(data, dict) and isinstance(data.get("token"), str) and KEY_PATTERN.fullmatch(data["token"])):
    raise PendingConflict("invalid")
  if data.get("endpoint") != endpoint or data.get("device_id") != device:
    raise PendingConflict(urlsplit(str(data.get("endpoint") or "")).hostname or "unknown")
  return data["token"]


def register_file_lock():
  """A non-blocking lock shared with any other process that registers this comma; None if busy."""
  path = CONFIG_PATH.with_name(".register.lock")
  path.parent.mkdir(parents=True, exist_ok=True)
  fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
  try:
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
  except OSError:
    os.close(fd)
    return None
  return fd


def drop_pending() -> None:
  try:
    pending_path().unlink()
  except OSError:
    pass


async def cloud_post(url: str, payload: dict, token: str | None = None) -> tuple[int, dict]:
  """POST JSON to the cloud without following redirects; a small JSON reply or CloudUnavailable."""
  headers = {"Content-Type": "application/json", "User-Agent": "carrot-web-wayon-key/1"}
  if token:
    headers["Authorization"] = f"Bearer {token}"
  try:
    async with ClientSession(timeout=ClientTimeout(total=CLOUD_TIMEOUT_S)) as session:
      async with session.post(url, json=payload, headers=headers, allow_redirects=False) as reply:
        status = reply.status
        raw = bytearray()
        # One read() may return only the first network piece; collect until EOF or past the cap.
        while len(raw) <= CLOUD_MAX_REPLY:
          chunk = await reply.content.read(CLOUD_MAX_REPLY + 1 - len(raw))
          if not chunk:
            break
          raw.extend(chunk)
  except (ClientError, asyncio.TimeoutError, OSError, ValueError):
    raise CloudUnavailable() from None
  try:
    body = json.loads(raw) if len(raw) <= CLOUD_MAX_REPLY else {}
  except ValueError:
    body = {}
  return status, body if isinstance(body, dict) else {}


def failure(error: str, status: int) -> web.Response:
  return response({"ok": False, "error": error}, status)


def throttled(state: dict, name: str) -> bool:
  now = time.monotonic()
  if now - state[name] < ACTION_INTERVAL_S:
    return True
  state[name] = now
  return False


async def register_device(request: web.Request) -> web.Response:
  body = await confirmed_body(request)
  state = request.app[STATE_KEY]
  if state["register_lock"].locked() or throttled(state, "last_register"):
    return failure("잠시 뒤 다시 시도해 주세요.", 429)
  async with state["register_lock"]:
    try:
      lock = register_file_lock()
    except OSError:
      return failure("등록 준비 파일을 만들 수 없습니다.", 500)
    if lock is None:
      return failure("다른 등록이 진행 중입니다. 잠시 뒤 다시 시도해 주세요.", 429)
    try:
      return await register_locked(body)
    finally:
      os.close(lock)


async def register_locked(body: dict) -> web.Response:
  config, reason = read_config()
  if config is not None:
    # An earlier attempt published this config but could not confirm it durable: finish it.
    if pending_matches(config):
      try:
        fsync_directory(CONFIG_PATH.parent)
      except OSError:
        return failure("키 파일 저장을 아직 확인하지 못했습니다. 잠시 뒤 다시 눌러 주세요.", 500)
      drop_pending()
      return response({"ok": True, "device_id": config["device_id"], "endpoint_host": urlsplit(config["endpoint"]).hostname})
    return failure("이 콤마에는 이미 My Traverse 키가 있습니다.", 409)
  if reason != "missing" or config_slot_taken():
    return failure("키 파일 자리에 읽을 수 없는 파일이나 링크가 있어 덮어쓰지 않습니다.", 409)
  endpoint = cloud_endpoint()
  if endpoint is None:
    return failure("My Traverse 클라우드 주소를 찾지 못했습니다.", 500)
  device = dongle_id()
  if not device:
    return failure("이 콤마의 기기 ID(DongleId)를 읽지 못했습니다. comma 서버에 등록된 콤마만 등록할 수 있습니다.", 409)
  try:
    token = read_pending(endpoint, device)
  except PendingConflict as conflict:
    return failure(f"이전 키 발급 시도 기록이 다른 클라우드나 기기용이거나 읽을 수 없습니다({conflict}). "
                   f"그 시도가 실패한 것이 확실하면 {pending_path()} 파일을 지운 뒤 다시 시도하세요.", 409)
  try:
    if token is None:
      token = "wayon_" + secrets.token_urlsafe(48)
      write_private_json(pending_path(), {"endpoint": endpoint, "device_id": device, "token": token})
    else:
      # A pending file left by a failed attempt may never have been made durable: confirm it first.
      fsync_directory(pending_path().parent)
  except OSError:
    return failure("등록 준비 파일 저장을 확인하지 못했습니다. 잠시 뒤 다시 시도해 주세요.", 500)
  try:
    request_id = secrets.token_urlsafe(18)
    proof = device_proof(device, request_id, hashlib.sha256(token.encode()).hexdigest())
  except Exception:
    return failure("이 콤마의 comma 기기 키로 확인 서명을 만들지 못했습니다.", 409)
  try:
    status, reply = await cloud_post(f"{endpoint}/api/devices/self-register",
                                     {"deviceId": device, "key": token, "requestId": request_id, "proof": proof})
  except CloudUnavailable:
    return failure("클라우드에 연결하지 못했습니다. 잠시 뒤 다시 눌러 주세요.", 502)
  if status == 200 and reply.get("ok") is True and reply.get("deviceId") == device:
    # Only this comma's own cloud, key and ID. vehicle_status_url is the owner car's external status feed
    # in the uploader's defaults; an empty value keeps a new comma from reading it.
    config = {"endpoint": endpoint, "token": token, "device_id": device, "vehicle_status_url": ""}
    try:
      write_private_json(CONFIG_PATH, config, replace=False)
    except FileExistsError:
      return failure("등록 중에 다른 키 파일이 생겨 덮어쓰지 않았습니다. 화면을 새로 고쳐 확인해 주세요.", 409)
    except OSError:
      # The pending key stays; pressing again finishes with the same key (or confirms a published file).
      return failure("등록은 됐지만 키 파일 저장을 확인하지 못했습니다. 다시 누르면 같은 키로 마무리합니다.", 500)
    drop_pending()
    return response({"ok": True, "device_id": device, "endpoint_host": urlsplit(endpoint).hostname})
  error = reply.get("error")
  if status == 409 and error == "device_already_registered":
    drop_pending()
    return failure("이 콤마는 이미 다른 키로 클라우드에 등록돼 있습니다. 키 재발급이 필요합니다.", 409)
  if status == 403 and error == "device_proof_rejected":
    return failure("comma 서버에서 이 기기를 확인하지 못했습니다. comma 서버에 등록된 콤마여야 하고, 콤마 시계가 맞아야 합니다.", 403)
  if status == 429 and error == "self_register_limit":
    return failure("지금은 클라우드가 새 기기를 더 받을 수 없습니다(사용량 보호 한도). 관리자에게 문의해 주세요.", 429)
  if status == 503 and error == "device_proof_unavailable":
    return failure("comma 서버 확인이 잠시 안 됩니다. 잠시 뒤 다시 눌러 주세요.", 502)
  if status in (404, 405):
    return failure("클라우드가 아직 기기 키 발급을 지원하지 않습니다.", 502)
  return failure(f"클라우드가 등록을 받아들이지 않았습니다(HTTP {status}).", 502)


def register(app: web.Application) -> None:
  app[STATE_KEY] = {"csrf": secrets.token_urlsafe(32), "last_reveal": float("-inf"),
                    "last_register": float("-inf"), "register_lock": asyncio.Lock()}
  app.router.add_get("/api/wayon_key", get_status)
  app.router.add_post("/api/wayon_key/reveal", reveal)
  app.router.add_post("/api/wayon_key/register", register_device)
