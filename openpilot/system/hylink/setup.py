"""Explicit enrollment; preserve pending identity through failed cloud requests."""
import argparse
import re
import secrets
from pathlib import Path

import requests

from openpilot.common.params import Params
from openpilot.system.hylink.runtime import CONFIG_PATH, ENDPOINT, param_text, read_json, write_json
from openpilot.system.hylink.transport import UploadHTTPError


def import_legacy(params, path=Path("/data/wayon_cloud/config.json")):
  legacy = read_json(path)
  device_id = param_text(params, "DongleId")
  if (legacy.get("device_id") != device_id or not re.fullmatch(r"[0-9a-f]{16}", device_id)
      or legacy.get("endpoint", "").rstrip("/") != ENDPOINT
      or not re.fullmatch(r"wayon_[A-Za-z0-9_-]{32,128}", str(legacy.get("token", "")))):
    raise ValueError("Legacy identity is missing, invalid, or belongs to another device.")
  existing = read_json(CONFIG_PATH)
  if existing.get("registered"):
    raise ValueError("Hylink is already enrolled; its key will not be overwritten.")
  # Copy only identity, never legacy per-car commands, CAN assumptions, or feature toggles.
  write_json(CONFIG_PATH, {"device_id": device_id, "endpoint": ENDPOINT, "token": legacy["token"],
                           "enabled": False, "media_enabled": False, "impact_enabled": False})


def enroll(params, post=requests.post, activate=True):
  device_id = param_text(params, "DongleId")
  if not re.fullmatch(r"[0-9a-f]{16}", device_id):
    raise ValueError("Wait for this device to register with comma before enrolling Hylink.")
  config = read_json(CONFIG_PATH)
  if config.get("device_id") not in (None, device_id):
    raise ValueError("This config belongs to a different device; do not reuse its key.")
  # Persist a pending key first so an interrupted registration retries the SAME key.
  if not config:
    config = {"device_id": device_id, "endpoint": ENDPOINT,
              "token": "wayon_" + secrets.token_urlsafe(32), "enabled": False,
              "media_enabled": False, "impact_enabled": False}
    write_json(CONFIG_PATH, config)
  if config.get("endpoint") != ENDPOINT or not re.fullmatch(r"wayon_[A-Za-z0-9_-]{32,128}", str(config.get("token", ""))):
    raise ValueError("Invalid enrollment configuration.")
  if not config.get("registered"):
    response = post(ENDPOINT + "/api/devices/register",
                    json={"deviceId": device_id, "key": config["token"]},
                    headers={"Authorization": "Bearer " + config["token"]},
                    timeout=(5, 15), allow_redirects=False)
    if not 200 <= response.status_code < 300:
      raise UploadHTTPError(response.status_code)
    config["registered"] = True
  if activate:
    config["enabled"] = True
  write_json(CONFIG_PATH, config)
  return config


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("command", choices=["enable", "disable", "status", "show-key", "import-legacy",
                                          "media-on", "media-off", "impact-on", "impact-off", "remote-on", "remote-off"])
  args = parser.parse_args()
  params = Params()
  config = read_json(CONFIG_PATH)
  if args.command == "enable":
    config = enroll(params)
  elif args.command == "import-legacy":
    import_legacy(params)
    config = read_json(CONFIG_PATH)
  elif args.command == "disable":
    if config:
      config["enabled"] = False
      write_json(CONFIG_PATH, config)
  elif args.command in ("media-on", "media-off", "impact-on", "impact-off", "remote-on", "remote-off"):
    if not config.get("registered") or config.get("device_id") != param_text(params, "DongleId"):
      raise ValueError("Enroll this device first.")
    feature, action = args.command.split("-")
    config[feature + "_enabled"] = action == "on"
    write_json(CONFIG_PATH, config)
  elif args.command == "show-key":
    if not config.get("registered") or config.get("device_id") != param_text(params, "DongleId"):
      raise ValueError("Enroll this device first.")
    print(config["token"])
    return
  print({key: config.get(key) for key in ("device_id", "enabled", "media_enabled", "impact_enabled", "remote_enabled")})


if __name__ == "__main__":
  main()
