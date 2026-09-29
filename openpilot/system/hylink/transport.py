"""Fixed HTTPS destination; bounded requests; never log credentials or response bodies."""
import json
import requests

from openpilot.system.hylink.runtime import ENDPOINT


class UploadHTTPError(RuntimeError):
  def __init__(self, status_code):
    self.status_code = int(status_code)
    super().__init__(f"Hylink HTTP {self.status_code}")


class UploadPayloadError(ValueError):
  pass


class UploadResponseError(RuntimeError):
  pass


def post_json(config, path, payload):
  if config.get("endpoint") != ENDPOINT or path not in ("/api/telemetry", "/api/trips", "/api/snapshot", "/api/impact", "/api/impact-media"):
    raise ValueError("Unexpected Hylink destination")
  try:
    body = json.dumps(payload, allow_nan=False)
  except (ValueError, TypeError):
    raise UploadPayloadError("Invalid upload data") from None
  response = requests.post(ENDPOINT + path, data=body,
                           headers={"Authorization": "Bearer " + config["token"],
                                    "Content-Type": "application/json", "User-Agent": "hylink-wip/1"},
                           timeout=(5, 10), allow_redirects=False)
  if not 200 <= response.status_code < 300:
    raise UploadHTTPError(response.status_code)
  try:
    return response.json() if response.content else {}
  except ValueError:
    raise UploadResponseError("Invalid cloud response") from None
