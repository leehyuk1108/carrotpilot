"""Mirror the Traverse camera's forward-collision alert onto openpilot's replacement 0x370.

With openpilot longitudinal on the Traverse SDGM camera harness, the panda blocks the
camera's ASCMActiveCruiseControlStatus (0x370) and openpilot sends its own. The stock
FCW then still pulses the seat (that path does not go through the comma), but the red
windshield LED, which follows 0x370 FCWAlert, stays dark. Receive-only: this reads the
camera frame and only feeds the FCWAlert field of the frame openpilot already sends.
"""
import math

from opendbc.car import structs
from opendbc.car.gm.values import CAR, GMSafetyFlags

NetworkLocation = structs.CarParams.NetworkLocation

# CarState updates at 100 Hz and the camera sends 0x370 at 25 Hz, so 25 updates without
# a camera frame is about six missed frames. A stale camera never keeps the LED on.
STOCK_FCW_TIMEOUT_FRAMES = 25


def stock_fcw_supported(CP) -> bool:
  return bool(CP.carFingerprint == CAR.CHEVROLET_TRAVERSE and CP.openpilotLongitudinalControl
              and CP.networkLocation == NetworkLocation.fwdCamera
              and CP.safetyConfigs and CP.safetyConfigs[0].safetyParam & GMSafetyFlags.HW_SDGM)


def _alert_value(value) -> int | None:
  if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
    return None
  return int(value) if value in (0, 1, 2, 3) else None


class StockFcw:
  def __init__(self):
    self.alert = 0
    self.frames_since_update = STOCK_FCW_TIMEOUT_FRAMES + 1

  def update(self, values) -> int:
    """values: every FCWAlert received from the camera 0x370 in this CarState update."""
    received = [alert for alert in map(_alert_value, values or ()) if alert is not None]
    if received:
      self.alert = max(received)
      self.frames_since_update = 0
    else:
      self.frames_since_update = min(self.frames_since_update + 1, STOCK_FCW_TIMEOUT_FRAMES + 1)
      if self.frames_since_update > STOCK_FCW_TIMEOUT_FRAMES:
        self.alert = 0
    return self.alert
