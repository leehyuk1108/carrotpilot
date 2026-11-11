"""My Traverse (Wayon) device key: a masked view, and the full key only after an explicit, same-origin
request from the comma's own address on the owner's network.

The key lives in /data/wayon_cloud/config.json (outside git). The comma uses it to upload drives and
photos to the My Traverse cloud, so it is never sent to the browser unless the user asks to copy it.
"""
from __future__ import annotations

import hmac
import ipaddress
import json
import secrets
import time
from pathlib import Path
from urllib.parse import urlsplit

from aiohttp import web

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
  if config is not None:
    body.update({
      "device_id": config["device_id"],
      "endpoint_host": urlsplit(config["endpoint"]).hostname or "",
      "key_masked": mask_token(config["token"]),
      "key_length": len(config["token"]),
    })
  return response(body)


async def reveal(request: web.Request) -> web.Response:
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

  now = time.monotonic()
  if now - state["last_reveal"] < REVEAL_INTERVAL_S:
    return response({"ok": False, "error": "Wait a moment before copying the key again."}, 429)
  state["last_reveal"] = now

  config, reason = read_config()
  if config is None:
    return response({"ok": False, "reason": reason, "error": "No My Traverse key is set up on this comma."}, 404)
  return response({"ok": True, "key": config["token"], "device_id": config["device_id"]})


def register(app: web.Application) -> None:
  app[STATE_KEY] = {"csrf": secrets.token_urlsafe(32), "last_reveal": float("-inf")}
  app.router.add_get("/api/wayon_key", get_status)
  app.router.add_post("/api/wayon_key/reveal", reveal)
