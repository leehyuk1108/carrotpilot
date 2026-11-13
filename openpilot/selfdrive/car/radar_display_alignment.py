"""Display-only lateral alignment of the front radar into the camera frame: Navdy road view and the C4 lead bar.

Control never imports this. radard, the planner and every warning keep the radar's own yRel: a source fix in
radar_interface changed lead selection in an A/B replay (Codex P1: right-edge tracks became leadOne), so the
user chose to correct only these two displays (2026-11-13).

On the Traverse (SDGM) the front radar's yRel lies right of the camera frame. Against the camera model's lanes,
moving radar targets sat right of their lane centre by 0.25 m at 5 m up to 0.70 m at 75 m, alike in the left,
own and right lane and on four drives over two days (6,790 samples; robust fit 0.29 + 0.0064 * range). The model
lead and the stock camera objects agree with the road video; the radar put a lead car and a bus straight ahead
on the right lane line (docs/traverse_radar_lateral_alignment_20261113.md). The fit covers 0-80 m, so the
correction stops growing past 80 m.
"""
from __future__ import annotations

import math

TRAVERSE = 'CHEVROLET_TRAVERSE'
OFFSET_M = 0.29   # metres the radar sits right of the camera frame at zero range
SLOPE = 0.0064    # plus this much per metre of range (about 0.37 degrees)
MAX_DISTANCE_M = 80.0


class RadarDisplayAlignment:
  def __init__(self, offset_m: float, slope: float):
    self.offset_m = offset_m
    self.slope = slope

  def key(self) -> tuple[float, float]:
    return (self.offset_m, self.slope)

  def shift(self, distance: float) -> float:
    """How far right of the camera frame the radar reports a target at this range (metres)."""
    return self.offset_m + self.slope * max(0.0, min(MAX_DISTANCE_M, distance))

  def right(self, distance: float, lateral: float) -> float:
    """Right-positive radar lateral (model frame) -> camera frame."""
    if not math.isfinite(distance) or not math.isfinite(lateral):
      return lateral
    return lateral - self.shift(distance)

  def left(self, distance: float, y_rel: float) -> float:
    """Left-positive radar yRel (radarState / liveTracks) -> camera frame."""
    if not math.isfinite(distance) or not math.isfinite(y_rel):
      return y_rel
    return y_rel + self.shift(distance)


def for_fingerprint(fingerprint) -> RadarDisplayAlignment | None:
  return RadarDisplayAlignment(OFFSET_M, SLOPE) if str(fingerprint) == TRAVERSE else None


def read_alignment(params=None) -> RadarDisplayAlignment | None:
  """From CarParamsPersistent (the last fingerprinted car); None when unknown, unreadable or not a Traverse."""
  try:
    if params is None:
      from openpilot.common.params import Params
      params = Params()
    raw = params.get('CarParamsPersistent')
    if not raw:
      return None
    from openpilot.cereal import car
    with car.CarParams.from_bytes(raw) as cp:
      return for_fingerprint(cp.carFingerprint)
  except Exception:
    return None
