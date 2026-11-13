"""Pure Carrot -> navdy.openpilot.v1 display mapping.

Projection and display conventions adapted from Wayon navdy_op_bridge.py at
b7660d1d403938c464d4f14f842449e3f0e64ebd (helpers, geometry and payload).
No transport, Params, clock, SP services, raw CAN or control writes. The caller
passes None for stale/invalid services and adds any transport timestamp.
All speed fields ending in Kph are km/h regardless of the vehicle's UI units.
"""
from __future__ import annotations

import bisect
from itertools import islice
import math
from typing import Any

from openpilot.selfdrive.car.outside_air_temp import outside_air_temp_c
from openpilot.selfdrive.car.stock_lane_markings import stock_lane_codes
from openpilot.selfdrive.navdy.deceleration import badge_code

KPH_PER_MS = 3.6
MAX_MODEL_SAMPLES = 64
MAX_PROJECTED_POINTS = 10
MAX_DISTANCE_M = 80.0


def finite_float(value: Any, default: float = 0.0) -> float:
  try:
    number = float(value)
  except (TypeError, ValueError, OverflowError):
    return default
  return number if math.isfinite(number) else default


def rounded(value: Any, digits: int = 1) -> float:
  return round(finite_float(value), digits)


def enum_text(value: Any) -> str:
  return str(value).split(".")[-1]


def _items(value: Any, count: int) -> list:
  return list(islice(value, count)) if value is not None else []


def _pair_text(left: bool, right: bool, both: str) -> str:
  return both if left and right else "left" if left else "right" if right else "off"


def resolve_operating_state(*, connected: bool, onroad: bool, door_open: bool = False,
                            gear: str = "unknown") -> str:
  if not connected:
    return "disconnected"
  if gear == "reverse":
    return "reverse"
  if onroad:
    return "onroad_door" if door_open else "onroad"
  return "offroad_door" if door_open else "offroad"


def _speed_kph(holder: Any, names: tuple[str, ...], scale: float = 1.0) -> float:
  for name in names:
    speed = finite_float(getattr(holder, name, 0.0)) * scale
    if 0.0 < speed < 255.0:
      return speed
  return 0.0


def set_speed_kph(car_state: Any, car_control: Any = None) -> float:
  # card.py publishes vCruise[Cluster] in km/h; CruiseState/HUDControl use m/s.
  return (_speed_kph(car_state, ("vCruiseCluster", "vCruise"))
          or _speed_kph(getattr(car_state, "cruiseState", None), ("speedCluster", "speed"), KPH_PER_MS)
          or _speed_kph(getattr(car_control, "hudControl", None), ("setSpeed",), KPH_PER_MS))


def _model_points(x_values: Any, y_values: Any) -> list[tuple[float, float]]:
  points = []
  for raw_x, raw_y in zip(_items(x_values, MAX_MODEL_SAMPLES), _items(y_values, MAX_MODEL_SAMPLES), strict=False):
    x, y = finite_float(raw_x, math.nan), finite_float(raw_y, math.nan)
    # Preserve sub-millimeter negative origin jitter at standstill, not gaps
    # in an established forward trajectory or invalid/nonfinite samples.
    if points and -0.001 <= x < 0.0 and points[-1][0] <= 0.001 and abs(y - points[-1][1]) <= 0.001:
      continue
    if not (math.isfinite(x) and math.isfinite(y) and 0.0 <= x <= 10000.0 and abs(y) <= 100.0):
      break
    if points and x <= points[-1][0]:
      # A stopping/turning trajectory can fold back at its far end. Keep
      # only this frame's unambiguous forward prefix, never sort the tail.
      break
    points.append((x, y))
  return points if len(points) >= 2 else []


def model_line_y_at_values(x_values: Any, y_values: Any, distance_m: float) -> float | None:
  points = _model_points(x_values, y_values)
  if len(points) < 2 or not points[0][0] <= distance_m <= points[-1][0]:
    return None
  for (x0, y0), (x1, y1) in zip(points, points[1:], strict=False):
    if x0 <= distance_m <= x1:
      return y0 + (y1 - y0) * (distance_m - x0) / (x1 - x0)
  return None


def model_line_y_at(line: Any, distance_m: float) -> float | None:
  return model_line_y_at_values(getattr(line, "x", None), getattr(line, "y", None), distance_m)


# Navdy road view (HUD v183+): a virtual pinhole camera at the comma camera, looking straight along the car
# (calibrated frame: x forward, y right, z down), drawn 1:1 into the HUD's 320x100 path view, so lanes and vehicles
# sit where the road camera sees them (user request 2026-11-13: "match the real road"). u = 160 + F y / x,
# v = HORIZON + F z / x. Road points have z = the road's depth below the camera (model lane z, about the camera's
# 1.36 m height). Everything nearer than the bottom row is clipped (lines) or held at it (vehicles). F and HORIZON
# were fitted by overlaying the drawing on 2026-11-13 road video: the horizon sits near the top and the bottom row
# is about 7 m ahead, as under the hood line of the camera.
VIEW_F = 473.0
VIEW_CENTER_X = 160.0
VIEW_HORIZON_Y = 8.0
VIEW_BOTTOM_Y = 100.0
VIEW_MIN_X_M = 0.5
VIEW_BAND_X = (-160.0, 480.0)     # the HUD band around the 320 px view is 640 px wide
DEFAULT_ROAD_DEPTH_M = 1.36       # road below the camera when the model gives no lane height
RADAR_TO_CAMERA_M = 1.52          # radarState/liveTracks dRel is from the radar; the view's origin is the camera


VIEW_K = VIEW_F / (VIEW_BOTTOM_Y - VIEW_HORIZON_Y)   # a road point is on the bottom row where x = K * depth
MAX_DEPTH_M = 20.0                                     # beyond this a height is not a road (an uphill can go below 0)


def _view_uv(x: float, y: float, z: float) -> tuple[float, float] | None:
  if not (math.isfinite(x) and math.isfinite(y) and math.isfinite(z)) or not VIEW_MIN_X_M <= x <= MAX_DISTANCE_M + 20.0:
    return None
  return VIEW_CENTER_X + VIEW_F * y / x, VIEW_HORIZON_Y + VIEW_F * z / x


def _line_depths(line_or_values: Any, from_line: bool = True) -> list | None:
  """A line's per-sample depths, or None when it has none (missing, or all exactly 0: an unset field)."""
  values = getattr(line_or_values, "z", None) if from_line else line_or_values
  values = _items(values, MAX_MODEL_SAMPLES)
  if not values or all(finite_float(v, math.nan) == 0.0 for v in values):
    return None
  return values


def _depth(value: Any) -> float | None:
  z = finite_float(value, math.nan)
  return z if math.isfinite(z) and abs(z) <= MAX_DEPTH_M else None


class _RoadProfile:
  """The model's road height, parsed once per model message (lookups run many times per payload)."""

  def __init__(self, model: Any):
    self.lanes = []
    for line in _items(getattr(model, "laneLines", None), 3)[1:3]:
      zs = _line_depths(line)
      points = _model_points(getattr(line, "x", None), zs) if zs is not None else []
      if points:
        self.lanes.append(([x for x, _ in points], [z for _, z in points]))
    position = getattr(model, "position", None)
    zs = _line_depths(position)
    points = _model_points(getattr(position, "x", None), zs) if zs is not None else []
    self.plan = ([x for x, _ in points], [z for _, z in points]) if points else None
    self.entry = None

  @staticmethod
  def _at(xs: list, zs: list, x: float) -> float | None:
    if not xs[0] <= x <= xs[-1]:
      return None
    i = min(len(xs) - 1, max(1, bisect.bisect_left(xs, x)))
    x0, x1, z0, z1 = xs[i - 1], xs[i], zs[i - 1], zs[i]
    return _depth(z0 + (z1 - z0) * (x - x0) / (x1 - x0)) if x1 > x0 else _depth(z1)

  def depth(self, x: float) -> float:
    depths = [z for z in (self._at(xs, zs, x) for xs, zs in self.lanes) if z is not None]
    if depths:
      return sum(depths) / len(depths)
    z = self._at(*self.plan, x) if self.plan else None
    return DEFAULT_ROAD_DEPTH_M + z if z is not None else DEFAULT_ROAD_DEPTH_M

  def bottom(self) -> float:
    if self.entry is None:
      self.entry = self._bottom()
    return self.entry

  def _bottom(self) -> float:
    def ahead(x):
      return x - VIEW_K * self.depth(x)
    low = VIEW_MIN_X_M
    if ahead(low) >= 0.0:
      return low
    x = low
    while x < MAX_DISTANCE_M:
      nxt = min(MAX_DISTANCE_M, x + 1.0)
      if ahead(nxt) >= 0.0:
        a, b = x, nxt
        for _ in range(24):
          mid = (a + b) / 2.0
          a, b = (mid, b) if ahead(mid) < 0.0 else (a, mid)
        return b
      x = nxt
    return VIEW_K * DEFAULT_ROAD_DEPTH_M


_PROFILE_CACHE: list = [None, None]  # (key, profile): the one model message currently being drawn


def _profile(model: Any) -> _RoadProfile:
  """Reuse the parse while the same model message is drawn; a cheap content key guards a reused object id."""
  lanes = _items(getattr(model, "laneLines", None), 3)
  position = getattr(model, "position", None)

  def edge(values):
    try:
      count = len(values)
      return (count, finite_float(values[0], math.nan), finite_float(values[count - 1], math.nan)) if count else ()
    except (TypeError, IndexError, KeyError):
      return ()
  key = (id(model), getattr(model, "frameId", None), edge(getattr(position, "x", None)), edge(getattr(position, "z", None)),
         tuple(edge(getattr(line, "z", None)) + edge(getattr(line, "x", None)) for line in lanes[1:3]))
  if _PROFILE_CACHE[0] != key:
    _PROFILE_CACHE[0], _PROFILE_CACHE[1] = key, _RoadProfile(model)
  return _PROFILE_CACHE[1]


def road_depth_at(model: Any, x: float) -> float:
  """Depth of the road below the camera at camera distance x, one policy for lanes, path and vehicles.

  The mean of the own-lane lines' depth there (either one if only one has it), else the plan's height plus the
  default camera height, else the flat-road default. A rising road may give a small or negative depth; it is kept."""
  return _profile(model).depth(x)


def bottom_distance(model: Any) -> float:
  """Camera distance where the road enters the bottom row: the first x with x >= K * depth(x).

  Found on the road profile itself (a slope moves it); the flat-road value when the profile never crosses."""
  return _profile(model).bottom()


def navdy_project_point(x_value: Any, y_value: Any, z_value: Any = DEFAULT_ROAD_DEPTH_M) -> list[float]:
  """One road point in view px, or [] when invalid or behind the camera (no bottom clipping here)."""
  x, y = finite_float(x_value, math.nan), finite_float(y_value, math.nan)
  z = finite_float(z_value, math.nan)
  if not 0.0 <= x <= MAX_DISTANCE_M or not math.isfinite(y) or abs(y) > 100.0:
    return []
  uv = _view_uv(max(x, VIEW_MIN_X_M), y, z)
  return [rounded(uv[0]), rounded(uv[1])] if uv is not None else []


def camera_x(distance: float) -> float:
  """radarState/liveTracks dRel (from the radar) -> distance from the camera, the model's and the view's origin."""
  return distance + RADAR_TO_CAMERA_M


def navdy_vehicle_view(model: Any, distance: float, lateral: float) -> dict[str, float] | None:
  """Ground point (screenX/Y) and HUD sprite scale (pxPerM) of a radar-referenced vehicle at distance/lateral.

  Nearer than where the road enters the bottom row it is drawn at that crossing (it would be under the hood
  line), on the same road point and scale as the lanes there. None when invalid or outside the HUD band."""
  if not math.isfinite(distance) or not math.isfinite(lateral):
    return None
  profile = _profile(model)
  x = max(camera_x(distance), profile.bottom())
  uv = _view_uv(x, lateral, profile.depth(x))
  if uv is None or not VIEW_BAND_X[0] <= uv[0] <= VIEW_BAND_X[1]:
    return None
  return {"screenX": rounded(uv[0]), "screenY": rounded(min(uv[1], VIEW_BOTTOM_Y)), "pxPerM": rounded(VIEW_F / x, 2)}


def navdy_model_line(x_values: Any, y_values: Any, z_values: Any = None, lateral_offset: float = 0.0,
                     depth: Any = None) -> list[float]:
  """The first visible stretch of a model line in view px, at most MAX_PROJECTED_POINTS points.

  z_values: the line's own depth per sample (laneLines z). A missing or out-of-range depth uses depth(x) when
  given (road_depth_at), else the flat default. The stretch starts where the 3D line enters the bottom row
  (x = K z) and, if it later drops out again (a crest), ends where it leaves; both points are on the 3D line."""
  points = [(x, y) for x, y in _model_points(x_values, y_values) if x <= MAX_DISTANCE_M]
  zs = _line_depths(z_values, from_line=False)
  road = []
  for index, (x, y) in enumerate(points):
    z = _depth(zs[index]) if zs is not None and index < len(zs) else None
    if z is None:
      z = depth(x) if depth is not None else DEFAULT_ROAD_DEPTH_M
    road.append((x, y + lateral_offset, z))

  def crossing(a, b):
    (x0, y0, z0), (x1, y1, z1) = a, b
    denominator = (x1 - x0) - VIEW_K * (z1 - z0)
    t = (VIEW_K * z0 - x0) / denominator if denominator != 0.0 else 1.0
    t = max(0.0, min(1.0, t))
    return x0 + t * (x1 - x0), y0 + t * (y1 - y0), z0 + t * (z1 - z0)

  visible = [x >= VIEW_K * z and x >= VIEW_MIN_X_M for x, _, z in road]
  first = next((i for i, v in enumerate(visible) if v), None)
  if first is None:
    return []
  stretch = [crossing(road[first - 1], road[first])] if first > 0 else []
  for i in range(first, len(road)):
    if not visible[i]:
      stretch.append(crossing(road[i - 1], road[i]))
      break
    stretch.append(road[i])
  projected = [uv for uv in (_view_uv(x, y, z) for x, y, z in stretch) if uv is not None]
  if len(projected) < 2:
    return []
  if len(projected) > MAX_PROJECTED_POINTS:
    last = len(projected) - 1
    projected = [projected[round(i * last / (MAX_PROJECTED_POINTS - 1))] for i in range(MAX_PROJECTED_POINTS)]
  return [value for u, v in projected for value in (rounded(u), rounded(min(v, VIEW_BOTTOM_Y)))]


def _probability(values: list, index: int) -> float:
  return max(0.0, min(1.0, finite_float(values[index]))) if index < len(values) else 0.0


def _lane_type(code: Any, probability: Any) -> str:
  # Hyundai publishes color (0..3) * 10 + type (0..8); Xiaoge replaces
  # only the type with 0/1. Reject reserved codes instead of coercing them.
  if isinstance(code, bool) or not isinstance(code, int) or not 0 <= code <= 38 or code % 10 > 8:
    return "unknown"
  if isinstance(probability, bool) or not isinstance(probability, (int, float)) or not 0.3 < probability <= 1.0:
    return "unknown"
  # Match C4, including double/inner markings. The existing Navdy renderer
  # treats unknown as dashed; do not infer a style from geometry alone.
  return "dashed" if code % 10 == 0 else "solid"


def navdy_model_geometry(model: Any) -> dict[str, Any]:
  position = getattr(model, "position", None)
  lanes = _items(getattr(model, "laneLines", None), 4)
  if position is None or len(lanes) < 3:
    return {}
  path_x, path_y = getattr(position, "x", None), getattr(position, "y", None)
  profile = _profile(model)
  road = profile.depth  # one height policy for every line

  def line_points(line: Any) -> list[float]:
    return navdy_model_line(getattr(line, "x", None), getattr(line, "y", None), getattr(line, "z", None), depth=road)

  path_left = navdy_model_line(path_x, path_y, None, -1.0, depth=road)
  path_right = navdy_model_line(path_x, path_y, None, 1.0, depth=road)
  path_points = _model_points(path_x, path_y)
  if path_points and not (path_left and path_right):
    # A short plan (stopped, creeping) ends under the hood line: nothing of the band is visible. Keep lanes and
    # vehicles drawn with a zero-area band on the bottom row where the road enters the view.
    entry = profile.bottom()
    end_y = path_points[-1][1]
    path_left, path_right = ([rounded(VIEW_CENTER_X + VIEW_F * (end_y + side) / entry), VIEW_BOTTOM_Y] * 2
                             for side in (-1.0, 1.0))
  geometry = {
    "navPathLeft": path_left,
    "navPathRight": path_right,
    "navLaneLeft": line_points(lanes[1]),
    "navLaneRight": line_points(lanes[2]),
  }
  if not all(len(points) >= 4 for points in geometry.values()):
    return {}
  probs = _items(getattr(model, "laneLineProbs", None), 4)
  for index, side in enumerate(("FarLeft", "Left", "Right", "FarRight")):
    geometry[f"navLane{side}Prob"] = rounded(_probability(probs, index), 2)
  if len(lanes) >= 4:
    geometry["navLaneFarLeft"] = line_points(lanes[0])
    geometry["navLaneFarRight"] = line_points(lanes[3])
  edges = _items(getattr(model, "roadEdges", None), 2)
  stds = _items(getattr(model, "roadEdgeStds", None), 2)
  for index, side in enumerate(("Left", "Right")):
    if index >= len(edges) or index >= len(stds):
      continue
    std = finite_float(stds[index], math.inf)
    confidence = max(0.0, min(1.0, 1.0 - std)) if std >= 0.0 else 0.0
    points = line_points(edges[index])
    if confidence >= 0.5 and len(points) >= 4:
      geometry[f"navRoadEdge{side}"] = points
      geometry[f"navRoadEdge{side}Prob"] = rounded(confidence, 2)
  return geometry


def _lead_lane(model: Any, distance: float, y: float) -> str:
  """Lane of a radar-referenced vehicle, read from the model lanes at its camera distance."""
  lanes = _items(getattr(model, "laneLines", None), 4)
  probs = _items(getattr(model, "laneLineProbs", None), 4)
  boundaries = [model_line_y_at(line, camera_x(distance)) for line in lanes]
  for left, right, lane, threshold in ((1, 2, "center", 0.3), (0, 1, "left", 0.2), (2, 3, "right", 0.2)):
    if right >= len(boundaries) or min(_probability(probs, left), _probability(probs, right)) < threshold:
      continue
    low, high = boundaries[left], boundaries[right]
    if low is not None and high is not None and low < high and low <= y <= high:
      return lane
  return ""


def navdy_lead_geometry(model: Any, radar_state: Any, longitudinal_plan: Any = None,
                        radar_alignment: Any = None) -> dict[str, Any]:
  """Only the two published, visible leads, never inferred raw radar tracks.

  radar_alignment (car.radar_display_alignment.RadarDisplayAlignment or None) moves a radar-measured lead's
  lateral into the camera frame of the drawn lanes, for display only; vision leads already are in it.
  """
  vehicles = []
  source = enum_text(getattr(longitudinal_plan, "longitudinalPlanSource", ""))
  for index, name in enumerate(("leadOne", "leadTwo")):
    lead = getattr(radar_state, name, None)
    if not bool(getattr(lead, "status", False)):
      continue
    distance = finite_float(getattr(lead, "dRel", None), math.nan)
    lateral = -finite_float(getattr(lead, "yRel", None), math.nan)
    relative_speed = finite_float(getattr(lead, "vRel", None), math.nan)
    if not 2.0 <= distance <= MAX_DISTANCE_M or not math.isfinite(relative_speed):
      continue
    if radar_alignment is not None and bool(getattr(lead, "radar", False)):
      lateral = radar_alignment.right(distance, lateral)
    lane = _lead_lane(model, distance, lateral)
    if not lane:
      continue
    view = navdy_vehicle_view(model, distance, lateral)
    if view is None:
      continue
    track_id = int(finite_float(getattr(lead, "radarTrackId", -1), -1))
    if track_id >= 0 and any(vehicle["trackId"] == track_id for vehicle in vehicles):
      continue
    vehicle = {
      "trackId": track_id,
      "screenX": view["screenX"],
      "screenY": view["screenY"],
      "pxPerM": view["pxPerM"],
      "distanceM": rounded(distance),
      "relativeSpeedMps": rounded(relative_speed),
      "lateralM": rounded(lateral, 2),
      "lane": lane,
      "source": "radar" if bool(getattr(lead, "radar", False)) else "vision",
      "longitudinalLead": source == f"lead{index}",
      "confidence": rounded(max(0.0, min(1.0, finite_float(getattr(lead, "modelProb", 0.0)))), 2),
    }
    vehicles.append(vehicle)
  return {"navVehicles": sorted(vehicles, key=lambda vehicle: vehicle["distanceM"], reverse=True)}


def _longitudinal_actuator(active: bool, car_control: Any, car_output: Any) -> tuple[str, float]:
  # The GM-specific raw-brake/350 normalization is not portable across Carrot
  # ports. Use the returned SI acceleration, never the requested carControl one.
  if not active or not bool(getattr(car_control, "longActive", False)):
    return "none", 0.0
  accel = finite_float(getattr(getattr(car_output, "actuatorsOutput", None), "accel", None), math.nan)
  if not math.isfinite(accel):
    return "none", 0.0
  if accel > 0.05:
    return "accelerator", min(1.0, accel)
  if accel < -0.05:
    return "brake", min(1.0, -accel)
  return "coast", 0.0


def fuel_percent(car_state: Any) -> float | None:
  """Tank level 0..100 from carState.vehicleCare (Traverse ECM 0x4D1), or None when not valid."""
  care = getattr(car_state, "vehicleCare", None)
  if care is None or not bool(getattr(care, "fuelLevelValid", False)):
    return None
  level = finite_float(getattr(care, "fuelLevel", None), math.nan)
  if not 0.0 <= level <= 1.05:
    return None
  return rounded(min(level, 1.0) * 100.0, 1)


def payload_from_messages(*, car_state: Any = None, selfdrive_state: Any = None,
                          controls_state: Any = None, car_control: Any = None,
                          car_output: Any = None, model: Any = None,
                          radar_state: Any = None, longitudinal_plan: Any = None,
                          carrot_man: Any = None, seq: int = 0, onroad: bool = False,
                          door_open: bool | None = None, radar_alignment: Any = None) -> dict[str, Any]:
  """Map fresh service structs without retaining state or synthesizing alerts.

  Only the display-only deceleration verdict uses carrot_man here; navigation
  feedback is still owned by the bridge separately.
  Alert strings are preserved verbatim, including hold/resume/critical alerts.
  """
  car_valid = (car_state is not None and bool(getattr(car_state, "canValid", False))
               and not bool(getattr(car_state, "canTimeout", False)))
  if onroad:
    # GM speed is signed and its filter can cross zero at rest. Neither is a
    # lost CAN connection; reverse display suppression is handled by gear below.
    car_valid = car_valid and abs(finite_float(getattr(car_state, "vEgo", None), math.inf)) < 100.0
  connected = car_valid and (not onroad or selfdrive_state is not None)
  gear = enum_text(getattr(car_state, "gearShifter", "unknown")).lower() if car_valid else "unknown"
  door = bool(getattr(car_state, "doorOpen", False) if door_open is None else door_open)
  driving = connected and bool(onroad) and gear != "reverse"
  enabled = driving and bool(getattr(selfdrive_state, "enabled", False))
  active = enabled and bool(getattr(selfdrive_state, "active", False))
  engageable = driving and bool(getattr(selfdrive_state, "engageable", False))
  state = enum_text(getattr(selfdrive_state, "state", "unknown")) if driving else "disabled"
  left = driving and bool(getattr(car_state, "leftBlinker", False))
  right = driving and bool(getattr(car_state, "rightBlinker", False))
  left_bsm = driving and bool(getattr(car_state, "leftBlindspot", False))
  right_bsm = driving and bool(getattr(car_state, "rightBlindspot", False))
  soft_hold = int(finite_float(getattr(car_state, "softHoldActive", 0))) if driving else 0
  hold = driving and (bool(getattr(car_state, "brakeHoldActive", False)) or soft_hold == 2)
  standstill = driving and bool(getattr(car_state, "standstill", False))
  stop_icon = driving and (state == "preEnabled" or (standstill and (enabled or hold)))
  speed = _speed_kph(car_state, ("vEgoCluster", "vEgo"), KPH_PER_MS) if driving else 0.0
  physical_acc = _speed_kph(getattr(car_state, "cruiseState", None), ("speedCluster", "speed"), KPH_PER_MS)
  actuator, actuator_level = _longitudinal_actuator(active, car_control, car_output)
  payload = {
    "schema": "navdy.openpilot.v1",
    "seq": max(0, min(2**53 - 1, int(finite_float(seq)))),
    "state": state,
    "enabled": enabled,
    "active": active,
    "engaged": active,
    "disengaged": not enabled,
    "engageable": engageable,
    "opAvailable": engageable,
    "onroad": bool(onroad),
    "doorOpen": door,
    "ambientState": resolve_operating_state(connected=connected, onroad=bool(onroad), door_open=door, gear=gear),
    "gear": gear,
    "standstill": stop_icon,
    "cruiseStandstill": stop_icon,
    "autoHoldActive": hold,
    "softHoldActive": soft_hold,
    "setSpeedKph": rounded(set_speed_kph(car_state, car_control)) if driving else 0.0,
    "actualAccSetKph": rounded(physical_acc) if driving else 0.0,
    "vEgoKph": rounded(speed),
    "speedUnit": "kph",
    "leftBlinker": left,
    "rightBlinker": right,
    "blinkers": _pair_text(left, right, "hazard"),
    "leftBlindspot": left_bsm,
    "rightBlindspot": right_bsm,
    "blindspot": _pair_text(left_bsm, right_bsm, "both"),
    "alertText1": str(getattr(selfdrive_state, "alertText1", "")),
    "alertText2": str(getattr(selfdrive_state, "alertText2", "")),
    "alertType": str(getattr(selfdrive_state, "alertType", "")),
    "alertStatus": enum_text(getattr(selfdrive_state, "alertStatus", "normal")),
    "alertSize": enum_text(getattr(selfdrive_state, "alertSize", "none")),
    "greenLightAlert": False,
    "leadDepartAlert": False,
    "longitudinalActuator": actuator,
    "longitudinalActuatorLevel": rounded(actuator_level, 2),
    "latActive": active and bool(getattr(car_control, "latActive", False)),
    "longActive": active and bool(getattr(car_control, "longActive", False)),
    "activeLaneLine": active and bool(getattr(controls_state, "activeLaneLine", False)),
    "navVehicles": [],
    "decel": 0,
  }
  outside_c = outside_air_temp_c(car_state) if car_valid else None
  if outside_c is not None:
    # Absent key keeps the HUD on its own OBD 0x46 reading.
    payload['outsideTempC'] = rounded(outside_c, 1)
  fuel = fuel_percent(car_state) if car_valid else None
  if fuel is not None:
    # Absent key hides the HUD fuel gauge next to the clock.
    payload['fuelPercent'] = fuel
  if payload['longActive'] and payload['alertSize'] == 'none' and payload['alertStatus'] == 'normal':
    payload['decel'] = badge_code(car_state, selfdrive_state, car_control, longitudinal_plan, carrot_man)
  if active:
    geometry = navdy_model_geometry(model)
    payload.update(geometry)
    if geometry:
      payload.update(navdy_lead_geometry(model, radar_state, longitudinal_plan, radar_alignment))
      probs = _items(getattr(model, "laneLineProbs", None), 3)
      left_code, right_code = stock_lane_codes(car_state) or (
        getattr(car_state, "leftLaneLine", None), getattr(car_state, "rightLaneLine", None),
      )
      for index, side, code in ((1, "Left", left_code), (2, "Right", right_code)):
        payload[f"navLane{side}Type"] = _lane_type(code, probs[index] if index < len(probs) else None)
  return payload
