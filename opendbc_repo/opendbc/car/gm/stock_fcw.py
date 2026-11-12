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

# The camera sends 0x370 at 25 Hz. Age is measured on the CAN packet clock, so a slow or
# stalled CarState loop cannot stretch it: a stale camera never keeps the LED on.
STOCK_FCW_MAX_AGE_NS = 250_000_000


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
    self.sample_ns = 0

  def update(self, values, sample_ns: int, now_ns: int) -> int:
    """values: FCWAlert of every camera 0x370 parsed in this update, oldest first.
    sample_ns: CAN time of the newest of those frames. now_ns: CAN time of the newest packet."""
    received = [alert for alert in map(_alert_value, values or ()) if alert is not None]
    if received:
      self.alert = received[-1]  # the camera's current value, including a clear
      self.sample_ns = sample_ns
    if self.alert and not 0 <= now_ns - self.sample_ns <= STOCK_FCW_MAX_AGE_NS:
      self.alert = 0
    return self.alert
