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
# own address in Host, which turns away DNS-rebinding hostnames and forwarding through another machine.
NETWORKS = tuple(ipaddress.ip_network(value) for value in (
  "127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10",
  "::1/128", "fd7a:115c:a1e0::/48",
))
FORWARD_HEADERS = ("Forwarded", "X-Forwarded-For", "X-Forwarded-Host")
CSRF_HEADER = "X-Carrot-CSRF"
REVEAL_INTERVAL_S = 2.0
MAX_BODY = 1024
STATE_KEY = web.AppKey("wayon_key_state", dict)
FORBIDDEN_TEXT = "The My Traverse key is only available from the comma's own address on your network."


def validate_request(request: web.Request, mutate: bool = False) -> None:
  try:
    peer = ipaddress.ip_address(request.remote)
    local = ipaddress.ip_address(request.transport.get_extra_info("sockname")[0])
    hostname = urlsplit("http://" + request.host).hostname
    target = local if hostname == "localhost" and local.is_loopback else ipaddress.ip_address(hostname)
    if target != local or not any(peer in net for net in NETWORKS):
      raise ValueError("not local")
    if request.headers.get("Sec-Fetch-Site", "same-origin") not in ("same-origin", "none"):
      raise ValueError("cross-site")
    if any(name in request.headers for name in FORWARD_HEADERS):
      raise ValueError("forwarded")
    origin = request.headers.get("Origin")
    if (mutate or origin) and origin != f"{request.scheme}://{request.host}":
      raise ValueError("origin")
  except (ValueError, TypeError, AttributeError, IndexError):
    raise web.HTTPForbidden(text=FORBIDDEN_TEXT) from None


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
  return web.json_response(body, status=status, headers={
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
  })


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
    raise web.HTTPForbidden(text="Refresh this page before continuing.")
  if request.content_type != "application/json" or (request.content_length or 0) > MAX_BODY:
    raise web.HTTPBadRequest(text="Expected a small JSON request.")
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
    raise web.HTTPBadRequest(text="Invalid request.") from None
  if body.get("confirmed") is not True:
    raise web.HTTPBadRequest(text="Explicit confirmation is required.")

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
