"""Carrot web: choose the internal (comma GPU) driving model. See openpilot/selfdrive/modeld/internal_model.py."""
from __future__ import annotations

from typing import Any

from aiohttp import web

from openpilot.selfdrive.modeld import internal_model as im

from ..services.params import HAS_PARAMS, Params

RUNNING_PHASES = ("downloading", "verifying", "validating", "compiling")


def _params() -> Any | None:
  return Params() if HAS_PARAMS and Params is not None else None


def _params_bool(params: Any | None, key: str) -> bool:
  if params is None:
    return False
  try:
    return bool(params.get_bool(key))
  except Exception:
    return False


def build_payload(params: Any | None = None) -> dict[str, Any]:
  requested = im.requested_model()
  status = im.read_json(im.status_path())
  if status.get("sha256") != requested.sha256:
    status = {}  # describes an earlier selection
  runtime = im.read_json(im.runtime_path())
  active = im.find(runtime.get("active_id")) or im.stock_model()
  onroad = _params_bool(params, "IsOnroad")
  engaged = _params_bool(params, "IsEngaged")
  downloaded, total = status.get("downloaded_bytes"), status.get("total_bytes")
  progress = None
  if status.get("phase") == "downloading" and isinstance(downloaded, int) and isinstance(total, int) and total > 0:
    progress = round(min(100.0, max(0.0, downloaded * 100.0 / total)), 1)
  return {
    "ok": True,
    "models": [{
      "id": m.id, "name": m.name, "size": m.size, "stock": m.stock,
      "recommended": m.recommended, "experimental": m.experimental,
    } for m in im.CATALOG],
    "requested": requested.id,
    "phase": "stock" if requested.stock else status.get("phase", "waiting"),
    "error_code": status.get("error_code") if not requested.stock else None,
    "detail": status.get("detail") if not requested.stock else None,
    "downloaded_bytes": downloaded,
    "total_bytes": total,
    "progress": progress,
    "started_at": status.get("started_at"),
    "updated_at": status.get("updated_at"),
    # what modeld used at its last start, and why it fell back to the stock model if it did
    "active": active.id,
    # the selected model actually produced outputs in the last modeld start (not only waited behind an eGPU/Jetlink)
    "in_use": active.stock or bool(runtime.get("used")),
    "fallback_reason": runtime.get("fallback_reason") if runtime.get("sha256") == requested.sha256 else None,
    "onroad": onroad,
    "engaged": engaged,
    "can_select": not onroad and not engaged,
  }


async def api_status(_request: web.Request) -> web.Response:
  return web.json_response(build_payload(_params()))


async def api_select(request: web.Request) -> web.Response:
  try:
    body = await request.json()
  except Exception:
    return web.json_response({"ok": False, "error": "invalid json"}, status=400)
  model = im.find(body.get("id") if isinstance(body, dict) else None)
  if model is None:
    return web.json_response({"ok": False, "error": "unknown model"}, status=400)
  params = _params()
  if _params_bool(params, "IsOnroad") or _params_bool(params, "IsEngaged"):
    return web.json_response({"ok": False, "error": "park and turn the ignition off before changing the driving model"},
                             status=409)
  im.reset_runtime_block(model)
  im.write_request(model)
  return web.json_response(build_payload(params))


def register(app: web.Application) -> None:
  app.router.add_get("/api/internal_model", api_status)
  app.router.add_post("/api/internal_model/select", api_select)
