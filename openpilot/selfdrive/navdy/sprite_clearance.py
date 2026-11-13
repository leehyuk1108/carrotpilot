"""Keep an adjacent-lane vehicle's Navdy sprite outside our lane when the vehicle itself is outside it (display only).

The HUD (v180 VehicleSprites) sizes a sprite from its old marker, w = 12 + 46.5 * (1 - d/80), body = 1.64 w * 234/256
canvas px for 1.9 m of car. Against the road (navdy_project_point's lateral scale) that draws a 1.9 m car 2.2-2.5 m
wide, and an adjacent lane's yawed frame shows the car's side toward the centre. A car centred in the next lane
therefore covered our lane line. Measured on the 2026-11-13 drives, 80% of left-lane and 53% of right-lane radar
cars were drawn inside our lane, and correcting the radar's lateral alone pushed the right side to 78%.

For a left/right-lane vehicle whose centre is clearly outside our line, the drawn position moves outward just enough
for the sprite's inner edge (computed with the HUD's own sizing and frame table below) to clear the line. A vehicle
that really straddles the line (centre within STRADDLE_M of it) is drawn where measured; in between, the drawn
distance follows a straight line, so a vehicle never jumps and its measurement noise grows about 1.1x there (locally up to 2x where the rounded yaw steps). Only screenX/yawDeg change; lateralM stays the measurement.
The frame table is the v180 assets/openpilot_vehicles/meta.txt (sha256 ead9c7c9...); keep both in step.
"""
from __future__ import annotations

from itertools import islice
import math

from openpilot.selfdrive.navdy.display import finite_float, model_line_y_at, navdy_project_point, rounded
from openpilot.selfdrive.navdy.vehicles import vehicle_yaw

STRADDLE_M = 0.5     # centre this close to our line (or inside it): drawn as measured
SETTLED_M = 1.45     # centre this far out (about a lane's half width): sprite fully clear of our line
CLEAR_M = 0.05       # gap kept between the sprite's inner edge and our line
LINE_MIN_PROB = 0.3  # our line must be drawn (laneLineProbs) for this to apply
CAR_WIDTH_M = 1.9    # VehicleSprites.CAR_WIDTH_M
OLD_CAR_FRACTION = 234.0 / 256.0

SPRITES = {  # kind: [(frame width px, rear-centre anchor x px, px per metre)] for payload yaw -24..24 step 2
  'car': [
    (221, 54.9, 61.6), (215, 56.4, 61.6), (207, 57.1, 61.6), (199, 57.7, 61.6), (192, 58.5, 61.6),
    (184, 59.2, 61.6), (175, 60.1, 61.6), (167, 60.9, 61.6), (159, 61.8, 61.6), (151, 62.7, 61.6),
    (141, 62.6, 61.6), (133, 63.6, 61.6), (127, 63.5, 61.6), (133, 69.5, 61.6), (141, 78.4, 61.6),
    (151, 88.3, 61.6), (159, 97.2, 61.6), (167, 106.1, 61.6), (175, 115, 61.6), (184, 123.8, 61.6),
    (192, 132.6, 61.6), (199, 141.3, 61.6), (207, 150, 61.6), (215, 158.6, 61.6), (221, 166.1, 61.6),
  ],
  'truck': [
    (234, 53, 46.2), (225, 53.4, 46.2), (216, 53.8, 46.2), (206, 54.3, 46.2), (196, 54.9, 46.2),
    (186, 55.5, 46.2), (177, 56.1, 46.2), (167, 56.8, 46.2), (156, 57.5, 46.2), (147, 58.2, 46.2),
    (136, 59, 46.2), (126, 58.7, 46.2), (118, 59.5, 46.2), (125, 67.2, 46.2), (136, 78, 46.2),
    (146, 88.7, 46.2), (157, 99.4, 46.2), (166, 110.1, 46.2), (177, 120.8, 46.2), (187, 131.4, 46.2),
    (196, 142, 46.2), (207, 152.6, 46.2), (215, 162.1, 46.2), (225, 172.5, 46.2), (234, 181.9, 46.2),
  ],
  'moto': [
    (64, 7.5, 61.6), (61, 7.4, 61.6), (59, 8.3, 61.6), (56, 8.3, 61.6), (54, 8.3, 61.6),
    (51, 8.3, 61.6), (48, 8.3, 61.6), (46, 9.4, 61.6), (45, 10.4, 61.6), (45, 13.5, 61.6),
    (45, 16.6, 61.6), (45, 19.7, 61.6), (45, 22.8, 61.6), (45, 25.8, 61.6), (45, 28.9, 61.6),
    (45, 32, 61.6), (44, 34.1, 61.6), (46, 37.1, 61.6), (48, 40.2, 61.6), (51, 43.2, 61.6),
    (53, 45.2, 61.6), (56, 48.2, 61.6), (59, 51.2, 61.6), (61, 53.1, 61.6), (64, 57.1, 61.6),
  ],
  'ped': [
    (42, 21, 72.5), (42, 21, 72.5), (44, 22, 72.5), (44, 22, 72.5), (44, 22, 72.5),
    (44, 22, 72.5), (44, 22, 72.5), (45, 23, 72.5), (45, 23, 72.5), (45, 23, 72.5),
    (45, 23, 72.5), (45, 23, 72.5), (45, 23, 72.5), (45, 23, 72.5), (45, 23, 72.5),
    (45, 23, 72.5), (45, 23, 72.5), (45, 23, 72.5), (45, 23, 72.5), (44, 23, 72.5),
    (44, 23, 72.5), (44, 23, 72.5), (43, 22, 72.5), (43, 22, 72.5), (42, 22, 72.5),
  ],
}


def frame_index(yaw: float) -> int:
  """VehicleSprites.frameIndex (Java Math.round: half up)."""
  clamped = 0.0 if not math.isfinite(yaw) else max(-24.0, min(24.0, yaw))
  return max(0, min(24, math.floor((clamped + 24.0) / 2.0 + 0.5)))


def lateral_scale(distance: float) -> float:
  """navdy_project_point's canvas px per metre at this distance."""
  normalized = (max(0.0, min(80.0, distance)) / 80.0) ** 0.65
  return 44.0 * (1.0 - normalized) + 8.0 * normalized


def _reach_px(frames, position: float, side: str) -> float:
  width, anchor, _ = frames[position]
  return (width - anchor) if side == 'left' else anchor


def inner_extent_m(kind, yaw: float, distance: float, side: str) -> float:
  """How far the drawn sprite reaches from its anchor toward our lane, in road metres at that distance.

  Interpolated between neighbouring yaw frames, so the drawn position moves smoothly with yaw instead of stepping
  when the HUD switches frames (a frame step is about 0.1 m of reach)."""
  frames = SPRITES.get(kind, SPRITES['car'])
  clamped = max(-24.0, min(24.0, yaw)) if math.isfinite(yaw) else 0.0
  position = (clamped + 24.0) / 2.0
  low = min(23, int(math.floor(position)))
  fraction = position - low
  reach_px = _reach_px(frames, low, side) * (1.0 - fraction) + _reach_px(frames, low + 1, side) * fraction
  ppm = frames[low][2]
  marker = 12.0 + 46.5 * max(0.0, min(1.0, 1.0 - distance / 80.0))
  canvas_per_px = 1.64 * marker * OLD_CAR_FRACTION / (CAR_WIDTH_M * ppm)
  return reach_px * canvas_per_px / lateral_scale(distance)


def settle_adjacent(payload, model) -> None:
  vehicles = payload.get('navVehicles')
  if not isinstance(vehicles, list) or model is None:
    return
  lanes = list(islice(getattr(model, 'laneLines', ()), 4))
  probs = list(islice(getattr(model, 'laneLineProbs', ()), 4))
  position = getattr(model, 'position', None)
  for vehicle in vehicles:
    side = vehicle.get('lane')
    if side not in ('left', 'right') or len(lanes) < 3 or len(probs) < 3:
      continue
    index = 1 if side == 'left' else 2
    if finite_float(probs[index], 0.0) < LINE_MIN_PROB:
      continue
    distance = finite_float(vehicle.get('distanceM'), math.nan)
    lateral = finite_float(vehicle.get('lateralM'), math.nan)
    line = model_line_y_at(lanes[index], distance) if math.isfinite(distance) else None
    if line is None or not math.isfinite(lateral):
      continue
    sign = -1.0 if side == 'left' else 1.0      # outward direction, right-positive frame
    out = sign * (lateral - line)               # centre's distance outside our line
    if out <= STRADDLE_M:
      continue
    yaw = finite_float(vehicle.get('yawDeg'), 0.0)
    shown = out
    for _ in range(2):                          # the frame (yaw) depends on the drawn position
      required = inner_extent_m(vehicle.get('kind'), yaw, distance, side) + CLEAR_M
      if out >= SETTLED_M:
        shown = max(out, required)
      else:                                     # straight line from "as measured" to "clear": no jump, mild gain
        settled_at = max(SETTLED_M, required)
        shown = STRADDLE_M + (out - STRADDLE_M) * (settled_at - STRADDLE_M) / (SETTLED_M - STRADDLE_M)
      yaw = vehicle_yaw(model, side, distance, line + sign * shown)
    if shown <= out + 1e-6:
      continue
    drawn = line + sign * shown
    screen = navdy_project_point(distance, drawn, model_line_y_at(position, distance))
    if not screen:
      continue
    vehicle['screenX'] = rounded(max(4.0, min(316.0, screen[0])))
    vehicle['screenY'] = screen[1]
    vehicle['yawDeg'] = yaw
