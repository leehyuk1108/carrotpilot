"""Same-LAN key connection with all features on by default.

The key page's same-origin POST activates defaults. GET and boot never enroll.
Onroad, power and thermal guards remain independent of these feature defaults.
"""
from pathlib import Path

from openpilot.system.hylink import runtime, setup

def page_allowed(params):
  # Diagnostics remain readable offroad when the guard is unhealthy, but keys
  # and activation still require the original, stricter readiness predicate.
  return params.get_bool("IsOffroad") and not params.get_bool("IsOnroad")


def pairing_allowed(params):
  return page_allowed(params) and runtime.fresh_record(runtime.STATE_PATH).get("offroad") is True


def status(params):
  if not pairing_allowed(params):
    raise ValueError("시동을 끄고 콤마의 전원·온도·차량 연결 상태를 확인해 주세요.")
  config = runtime.read_json(runtime.CONFIG_PATH)
  if config.get("device_id") != runtime.param_text(params, "DongleId") or config.get("registered") is not True:
    return {"ready": False}
  return {"ready": True, "key": config["token"], "enabled": config.get("enabled") is True,
          "deviceId": config["device_id"], "media": config.get("media_enabled") is True,
          "impact": config.get("impact_enabled") is True, "remote": config.get("remote_enabled") is True}


def connect(params, body, enroll=None):
  # Network authorization and CSRF protection belong to connect_server.Handler.
  if not pairing_allowed(params):
    raise ValueError("시동을 끄고 콤마의 전원·온도·차량 연결 상태를 확인해 주세요.")
  if not runtime.CONFIG_PATH.exists() and Path("/data/wayon_cloud/config.json").exists():
    setup.import_legacy(params)
  config = (enroll or setup.enroll)(params, activate=False)
  # Registration can block on network; never activate after an ignition transition.
  if not pairing_allowed(params):
    raise ValueError("차량 상태가 바뀌었어요. 주차 후 다시 연결해 주세요.")
  # Upgrade existing enrollments without rotating the key. Read-only polling
  # never re-enables a connection stopped from the comma's own settings.
  config.update(enabled=True, media_enabled=True, impact_enabled=True, remote_enabled=True)
  runtime.write_json(runtime.CONFIG_PATH, config)
  return {"ok": True, **status(params)}
