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
HIDDEN_BAND = (VIEW_CENTER_X, 1000.0, VIEW_CENTER_X, 1000.0)  # a valid, zero-area path far below the HUD's display


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
  """The road surface of one model message, parsed once (lookups run many times per payload).

  The surface is the plane through the own-lane lines (laneLines 1 and 2) at each distance: depth(x, y) follows
  their heights across, so each lane line keeps its own height and a vehicle between or beside them sits on the same
  tilted road. Samples with an implausible height are dropped before any interpolation. Without lane heights the
  plan's height plus the default camera height, else the flat default."""

  def __init__(self, model: Any):
    lanes = _items(getattr(model, "laneLines", None), 3)
    self.left = self._line(lanes[1]) if len(lanes) > 1 else None
    self.right = self._line(lanes[2]) if len(lanes) > 2 else None
    position = getattr(model, "position", None)
    plan = self._line(position, with_y=False)
    self.plan = plan
    every = {VIEW_MIN_X_M, MAX_DISTANCE_M}
    for line in (self.left, self.right, self.plan):
      if line is not None:
        every.update(x for x in line[0] if VIEW_MIN_X_M < x < MAX_DISTANCE_M)
    self.lookup_x = sorted(every)   # every sample column: between two of them each value is linear
    self.at_columns = [self._lookup_at(x) for x in self.lookup_x]
    # Where depth's form can change: the lane lines' samples and ends, and the plan's samples where no lane line is
    # there (the plan is used only then). Solving and shaping split only here.
    grid = {VIEW_MIN_X_M, MAX_DISTANCE_M}
    for line in (self.left, self.right):
      if line is not None:
        grid.update(x for x in line[0] if VIEW_MIN_X_M < x < MAX_DISTANCE_M)
    for x, (left, right, _) in zip(self.lookup_x, self.at_columns, strict=True):
      if left is None and right is None:
        grid.add(x)
    self.grid = sorted(grid)
    self.edges = {line[0][i] for line in (self.left, self.right, self.plan) if line is not None for i in (0, -1)}
    self.side_columns = [self._sides(x) for x in self.grid]   # (y_left, z_left, y_right, z_right) or None
    self.entries: dict = {}
    self.columns = [self._column_bounds(x) for x in self.grid]   # (min, max) depth candidates at each column
    pieces = len(self.grid) - 1
    self.hidden_until = self.grid[0]   # the road is hidden on every piece before this column
    for i in range(pieces):
      if not self._settled(i, self.grid[i], self.grid[i + 1], False):
        break
      self.hidden_until = self.grid[i + 1]
    self.shown_from = self.grid[-1]    # and in view on every piece from this column on
    for i in reversed(range(pieces)):
      if not self._settled(i, self.grid[i], self.grid[i + 1], True):
        break
      self.shown_from = self.grid[i]

  @staticmethod
  def _line(line: Any, with_y: bool = True):
    xs, ys = getattr(line, "x", None), getattr(line, "y", None) if with_y else getattr(line, "x", None)
    zs = _line_depths(line)
    if zs is None:
      return None
    points = _model_points(xs, ys)
    out = ([], [], [])
    for index, (x, y) in enumerate(points):
      z = _depth(zs[index]) if index < len(zs) else None
      if z is not None:
        out[0].append(x); out[1].append(y); out[2].append(z)
    return out if len(out[0]) >= 2 else None

  @staticmethod
  def _at(xs: list, values: list, x: float) -> float | None:
    if not xs[0] <= x <= xs[-1]:
      return None
    i = min(len(xs) - 1, max(1, bisect.bisect_left(xs, x)))
    x0, x1, v0, v1 = xs[i - 1], xs[i], values[i - 1], values[i]
    return v0 + (v1 - v0) * (x - x0) / (x1 - x0) if x1 > x0 else v1

  def _lookup_at(self, x: float) -> tuple:
    sides = []
    for line in (self.left, self.right):
      z = self._at(line[0], line[2], x) if line is not None else None
      sides.append(None if z is None else (self._at(line[0], line[1], x), z))
    plan = self._at(self.plan[0], self.plan[2], x) if self.plan is not None else None
    return sides[0], sides[1], plan

  def _lookup(self, x: float) -> tuple:
    """((y, z) of the left line, of the right line, plan z) at x, each None where absent. Every line sample inside
    the grid is a column, so between two columns each value is linear and a line is there only if it is at both
    columns: one bisect and a blend instead of a bisect per line and value."""
    grid = self.lookup_x
    i = bisect.bisect_left(grid, x)
    if i < len(grid) and grid[i] == x:
      return self.at_columns[i]
    if i == 0 or i == len(grid):
      return self._lookup_at(x)
    t = (x - grid[i - 1]) / (grid[i] - grid[i - 1])
    a, b = self.at_columns[i - 1], self.at_columns[i]
    left = (a[0][0] + t * (b[0][0] - a[0][0]), a[0][1] + t * (b[0][1] - a[0][1])) if a[0] and b[0] else None
    right = (a[1][0] + t * (b[1][0] - a[1][0]), a[1][1] + t * (b[1][1] - a[1][1])) if a[1] and b[1] else None
    plan = a[2] + t * (b[2] - a[2]) if a[2] is not None and b[2] is not None else None
    return left, right, plan

  def depth(self, x: float, y: float | None = None) -> float:
    left, right, plan = self._lookup(x)
    if left is not None and right is not None:
      (y_left, z_left), (y_right, z_right) = left, right
      if y is not None and y_right - y_left > 0.5:
        t = max(-2.0, min(3.0, (y - y_left) / (y_right - y_left)))   # beside the lane: the same tilt, bounded
        return z_left + t * (z_right - z_left)
      return (z_left + z_right) / 2.0
    if left is not None or right is not None:
      return (left or right)[1]
    if plan is not None:
      return DEFAULT_ROAD_DEPTH_M + plan
    return DEFAULT_ROAD_DEPTH_M

  def _sides(self, x: float):
    """(y_left, z_left, y_right, z_right) where both own-lane lines have a height at x, else None (as depth())."""
    left, right, _ = self._lookup(x)
    return None if left is None or right is None else (left[0], left[1], right[0], right[1])

  def _column_bounds(self, x: float) -> tuple[float, float]:
    """Every depth formula's value at column x: each line, both clamps of the lateral mix, the plan, the default.
    Within a sample piece each formula is linear in x (the unclamped mix lies between the clamps), so the two
    columns of a piece bound depth anywhere on it."""
    left, right, plan = self._lookup(x)
    values = [DEFAULT_ROAD_DEPTH_M] + [side[1] for side in (left, right) if side is not None]
    if left is not None and right is not None:
      values += [left[1] - 2.0 * (right[1] - left[1]), left[1] + 3.0 * (right[1] - left[1])]
    if plan is not None:
      values.append(DEFAULT_ROAD_DEPTH_M + plan)
    return min(values), max(values)

  def _settled(self, i: int, a: float, b: float, visible: bool) -> bool:
    """True when the road keeps `visible` on all of [a, b] inside sample piece i, by that piece's bounds."""
    (low0, high0), (low1, high1) = self.columns[i], self.columns[i + 1]
    if visible:
      return a - VIEW_K * max(high0, high1) >= 0.0   # x - K depth >= 0 throughout
    return b - VIEW_K * min(low0, low1) < 0.0        # x - K depth < 0 throughout

  def _weight(self, x: float, y: float) -> float:
    """Positive factor that makes weight * (x - K depth) a polynomial in x on one piece: the lane width while the
    lateral mix applies (depth is a ratio there), else 1 (depth is linear)."""
    sides = self._sides(x)
    if sides is None:
      return 1.0
    width = sides[2] - sides[0]
    return width if width > 0.5 and -2.0 < (y - sides[0]) / width < 3.0 else 1.0

  def _regimes(self, a: float, b: float, y_at: Any) -> list:
    """Split a sample piece where the depth formula changes: lane width 0.5 m, lateral mix clamps at -2 and 3.
    Inside one grid interval both lane lines are linear (or absent), so their columns give them directly."""
    cuts = [a, b]
    m1, m2 = a + (b - a) / 3.0, a + 2.0 * (b - a) / 3.0
    i = min(len(self.grid) - 2, max(0, bisect.bisect_right(self.grid, m1) - 1))
    c0, c1 = self.side_columns[i], self.side_columns[i + 1]
    if c0 is not None and c1 is not None:
      x0, x1 = self.grid[i], self.grid[i + 1]

      def side(m: float) -> tuple:
        t = (m - x0) / (x1 - x0)
        y_left = c0[0] + t * (c1[0] - c0[0])
        return y_left, c0[2] + t * (c1[2] - c0[2]) - y_left   # left line, width
      (l1, w1), (l2, w2) = side(m1), side(m2)
      u1, u2 = y_at(m1) - l1, y_at(m2) - l2
      for g1, g2 in ((w1 - 0.5, w2 - 0.5), (u1 + 2.0 * w1, u2 + 2.0 * w2), (u1 - 3.0 * w1, u2 - 3.0 * w2)):
        if g1 != g2:   # each is linear in x on the piece
          x = m1 + (m2 - m1) * g1 / (g1 - g2)
          if a < x < b:
            cuts.append(x)
    cuts.sort()
    return list(zip(cuts, cuts[1:]))

  def _change(self, p: float, q: float, y_at: Any, visible: bool) -> float | None:
    """First x in [p, q] where the road leaves `visible`, on a piece with one depth formula. There
    weight * (x - K depth) is a polynomial of degree <= 2 in x, fitted exactly from three inner points (so a
    jump at either end is seen as a change at that end), and its roots are solved in closed form."""
    h = (q - p) / 4.0
    if not h > 0.0:
      return None

    def value(x: float) -> float:
      y = y_at(x)
      return self._weight(x, y) * (x - VIEW_K * self.depth(x, y))
    v1, v2, v3 = value(p + h), value(p + 2.0 * h), value(p + 3.0 * h)
    slope, curve = (v3 - v1) / (2.0 * h), (v1 - 2.0 * v2 + v3) / (2.0 * h * h)   # around the middle, tau in [-2h, 2h]
    start = v2 - 2.0 * h * slope + 4.0 * h * h * curve
    if (start >= 0.0) if not visible else (start < -1e-6):   # a search may start on its own entry root
      return p
    scale = abs(v1) + abs(v2) + abs(v3) + 1e-12
    if abs(curve) * 4.0 * h * h <= 1e-12 * scale:
      roots = [-v2 / slope] if slope != 0.0 else []
    else:
      disc = slope * slope - 4.0 * curve * v2
      if disc < 0.0:
        return None
      k = -0.5 * (slope + math.copysign(math.sqrt(disc), slope))
      roots = [k / curve] + ([v2 / k] if k != 0.0 else [])
    eps = 1e-9 * h
    for tau in sorted(min(2.0 * h, max(-2.0 * h, r)) for r in roots if -2.0 * h - eps <= r <= 2.0 * h + eps):
      rising = slope + 2.0 * curve * tau   # a touching double root is not a change
      if (rising > 0.0) if not visible else (rising < 0.0):
        return p + 2.0 * h + tau
    return None

  def crossing(self, x0: float, y0: float, x1: float, y1: float, visible: bool) -> float | None:
    """First x in [x0, x1] on the straight run (x0, y0)-(x1, y1) where the road leaves `visible` (in view means
    x >= K depth(x, y)), or None. Exact and complete: split at every sample column and formula change, so a short
    stretch inside a long line segment, or between two hidden ends, is found too."""
    if not x1 > x0:
      return None
    slope = (y1 - y0) / (x1 - x0)
    if visible and x0 >= self.shown_from:
      return None
    if not visible and x0 < self.hidden_until:
      if x1 <= self.hidden_until:
        return None
      x0, y0 = self.hidden_until, y0 + slope * (self.hidden_until - x0)   # same straight run, later start

    def y_at(x: float, x0: float = x0, y0: float = y0) -> float:
      return y0 + slope * (x - x0)
    first, last = bisect.bisect_right(self.grid, x0), bisect.bisect_left(self.grid, x1)
    cuts = [x0] + self.grid[first:last] + [x1]
    for k, (a, b) in enumerate(zip(cuts, cuts[1:], strict=False)):
      if self._settled(min(len(self.grid) - 2, max(0, first - 1 + k)), a, b, visible):
        continue
      for p, q in self._regimes(a, b, y_at):
        x = self._change(p, q, y_at, visible)
        if x is not None:
          return x
    return None

  def between(self, a: float, b: float) -> list:
    """Sample columns strictly between a and b."""
    return self.grid[bisect.bisect_right(self.grid, a):bisect.bisect_left(self.grid, b)]

  def entry(self, y: float = 0.0) -> float:
    """First distance where the road at lateral y enters the bottom row (x = K depth)."""
    key = round(y, 4)
    if key not in self.entries:
      x = self.crossing(self.grid[0], key, self.grid[-1], key, visible=False)
      self.entries[key] = VIEW_K * DEFAULT_ROAD_DEPTH_M if x is None else x
    return self.entries[key]


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
         tuple(edge(getattr(line, "z", None)) + edge(getattr(line, "x", None)) + edge(getattr(line, "y", None))
               for line in lanes[1:3]))
  if _PROFILE_CACHE[0] != key:
    _PROFILE_CACHE[0], _PROFILE_CACHE[1] = key, _RoadProfile(model)
  return _PROFILE_CACHE[1]


def road_depth_at(model: Any, x: float, y: float | None = None) -> float:
  """Depth of the road below the camera at camera distance x (and lateral y), one surface for every drawing."""
  return _profile(model).depth(x, y)


def bottom_distance(model: Any, y: float = 0.0) -> float:
  """Camera distance where the road at lateral y enters the bottom row: the first x with x >= K * depth(x, y)."""
  return _profile(model).entry(y)


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
  x = camera_x(distance)
  if x < 3.0 * VIEW_K * DEFAULT_ROAD_DEPTH_M:   # only a near vehicle can be under the hood line (~21 m bound)
    x = max(x, profile.entry(lateral))
  uv = _view_uv(x, lateral, profile.depth(x, lateral))
  if uv is None or not VIEW_BAND_X[0] <= uv[0] <= VIEW_BAND_X[1]:
    return None
  return {"screenX": rounded(uv[0]), "screenY": rounded(min(uv[1], VIEW_BOTTOM_Y)), "pxPerM": rounded(VIEW_F / x, 2)}


def _surface_stretch(points: list, surface: Any) -> list:
  stretch, started = [], False
  for (xa, ya), (xb, yb) in zip(points, points[1:], strict=False):
    if xb <= VIEW_MIN_X_M:
      continue
    if xa < VIEW_MIN_X_M:
      ya, xa = ya + (yb - ya) * (VIEW_MIN_X_M - xa) / (xb - xa), VIEW_MIN_X_M

    def at(x: float, xa=xa, ya=ya, xb=xb, yb=yb) -> tuple:
      y = ya + (yb - ya) * (x - xa) / (xb - xa)
      return x, y, surface.depth(x, y)
    if not started:
      x = surface.crossing(xa, ya, xb, yb, visible=False)
      if x is None:
        continue
      started = True
      stretch.append(at(x))
      if not xb > x:
        continue
      xa, ya = x, stretch[-1][1]
    exit_x = surface.crossing(xa, ya, xb, yb, visible=True)
    end = at(exit_x) if exit_x is not None else (xb, yb, surface.depth(xb, yb))
    inner = _road_candidates(surface, at, stretch[-1], end)
    stretch += _bends(stretch[-1], inner, end) + [end]   # the road's own shape between the line's samples
    if exit_x is not None:
      break
  return stretch


SHAPE_TOLERANCE_PX = 0.3
REFINE_TOLERANCE_PX = 0.1   # candidates are found finer than they are kept
REFINE_DEPTH = 8


def _road_candidates(surface: Any, at: Any, first: tuple, last: tuple) -> list:
  """Road points strictly between the road points first and last of one straight line segment that can carry the
  road's shape.

  At every column where depth's form changes the road point is a candidate; where it can jump (a lane line's
  samples end, the lane width passes 0.5 m, the lateral mix clamps) both sides are. Between those, midpoints are
  added wherever the curved surface leaves the straight view segment by more than REFINE_TOLERANCE_PX.
  _bends then keeps what shows."""
  start, end = first[0], last[0]
  if not end > start:
    return []
  slope = (last[1] - first[1]) / (end - start)

  def y_at(x: float) -> float:
    return first[1] + slope * (x - start)
  inset = 1e-6
  cuts = [start] + surface.between(start, end) + [end]
  marks = []   # inner boundaries in order: (x, may jump)
  for a, b in zip(cuts, cuts[1:], strict=False):
    marks += [(p, True) for p, _ in surface._regimes(a, b, y_at)[1:]]
    if b < end:
      marks.append((b, b in surface.edges))
  points, anchor = [], first
  if start in surface.edges:
    anchor = at(start + inset)
    points.append(anchor)
  for x, jump in marks:
    if jump:
      before, after = at(x - inset), at(x + inset)
      _refine(at, anchor, before, points)
      points += [before, after]
      anchor = after
    else:
      here = at(x)
      _refine(at, anchor, here, points)
      points.append(here)
      anchor = here
  if end in surface.edges:
    before = at(end - inset)
    _refine(at, anchor, before, points)
    points.append(before)
  else:
    _refine(at, anchor, last, points)
  return points


def _refine(at: Any, a: tuple, b: tuple, points: list, views: tuple | None = None, level: int = 0) -> None:
  """Midpoints between road points a and b where the surface leaves their straight view segment."""
  if level >= REFINE_DEPTH:
    return
  va, vb = views if views is not None else (_view_uv(*a), _view_uv(*b))
  middle = at((a[0] + b[0]) / 2.0)
  vm = _view_uv(*middle)
  if va is None or vb is None or vm is None or _off_line(vm, va, vb) <= REFINE_TOLERANCE_PX:
    return
  _refine(at, a, middle, points, (va, vm), level + 1)
  points.append(middle)
  _refine(at, middle, b, points, (vm, vb), level + 1)


def _bends(start: tuple, inner: list, end: tuple) -> list:
  """The road columns inside one straight line segment whose view point is more than SHAPE_TOLERANCE_PX from the
  view segment between its kept neighbours (Douglas-Peucker). Where the road is planar along the line
  (lane lines between their samples) none is kept, so the drawing is unchanged; where it bends between the line's
  samples (a short visible dip inside one long path segment), its shape stays and the band keeps its area."""
  if not inner:
    return []
  views = [_view_uv(*point) for point in [start] + inner + [end]]
  if any(view is None for view in views):
    return []
  keep = set()

  def split(first: int, last: int) -> None:
    best, index = 0.0, None
    for i in range(first + 1, last):
      off = _off_line(views[i], views[first], views[last])
      if off > best:
        best, index = off, i
    if index is not None and best > SHAPE_TOLERANCE_PX:
      keep.add(index)
      split(first, index)
      split(index, last)
  split(0, len(views) - 1)
  return [inner[i - 1] for i in sorted(keep)]


def _chord_stretch(points: list, zs: list | None) -> list:
  road = []
  for index, (x, y) in enumerate(points):
    z = _depth(zs[index]) if zs is not None and index < len(zs) else None
    road.append((x, y, DEFAULT_ROAD_DEPTH_M if z is None else z))

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
  return stretch


def navdy_model_line(x_values: Any, y_values: Any, z_values: Any = None, lateral_offset: float = 0.0,
                     surface: Any = None) -> list[float]:
  """The first visible stretch of a model line in view px, at most MAX_PROJECTED_POINTS points.

  surface: the road (_RoadProfile); every point uses its depth, so lines, path and vehicles share one road, and the
  stretch starts where the line first enters the bottom row (x = K z) and, if it later drops out again (a crest),
  ends where it leaves: both solved on that road like a vehicle's crossing, anywhere along a line segment.
  Without it, z_values (a valid per-sample depth) or the flat default, with crossings on the chord between samples."""
  points = [(x, y + lateral_offset) for x, y in _model_points(x_values, y_values) if x <= MAX_DISTANCE_M]
  if surface is not None:
    stretch = _surface_stretch(points, surface)
  else:
    stretch = _chord_stretch(points, _line_depths(z_values, from_line=False))
  projected = [uv for uv in (_view_uv(x, y, z) for x, y, z in stretch) if uv is not None]
  if len(projected) < 2:
    return []
  projected = _reduce(projected, MAX_PROJECTED_POINTS)
  return [value for u, v in projected for value in (rounded(u), rounded(min(v, VIEW_BOTTOM_Y)))]


def _off_line(point: tuple, a: tuple, b: tuple) -> float:
  """Distance from a view point to the drawn segment a-b (not its infinite line: a point beyond either end, such
  as a crest that rises above both kept points, is as far as that end)."""
  (u, v), (u0, v0), (u1, v1) = point, a, b
  du, dv = u1 - u0, v1 - v0
  squared = du * du + dv * dv
  t = max(0.0, min(1.0, ((u - u0) * du + (v - v0) * dv) / squared)) if squared > 0.0 else 0.0
  return math.hypot(u - u0 - t * du, v - v0 - t * dv)


def _reduce(points: list, budget: int) -> list:
  """At most `budget` view points that keep the line's shape: the ends, then repeatedly the point farthest off
  the polyline kept so far (top-down Douglas-Peucker with a point budget). While no point is 0.1 px off (the
  output resolution), the longest run of points is split at its middle, so straight stretches stay evenly
  spaced as before. Neither a road bend point nor a path bend can push out a more visible one."""
  if len(points) <= budget:
    return points

  def rank(first: int, last: int) -> tuple | None:
    if last - first < 2:
      return None
    a, b = points[first], points[last]
    off, index = max((_off_line(points[i], a, b), i) for i in range(first + 1, last))
    off = round(off, 1)
    return off, last - first, (first + last) // 2 if off == 0.0 else index

  runs = {(0, len(points) - 1): rank(0, len(points) - 1)}   # each run is ranked once, when it is made
  for _ in range(budget - 2):
    candidates = [(value[0], value[1], -run[0], run) for run, value in runs.items() if value is not None]
    if not candidates:
      break
    first, last = max(candidates)[3]
    split = runs.pop((first, last))[2]
    runs[(first, split)], runs[(split, last)] = rank(first, split), rank(split, last)
  return [points[i] for i in sorted({index for run in runs for index in run})]


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
  profile = _profile(model)  # one road surface for every line

  def line_points(line: Any) -> list[float]:
    return navdy_model_line(getattr(line, "x", None), getattr(line, "y", None), getattr(line, "z", None), surface=profile)

  path_left = navdy_model_line(path_x, path_y, None, -1.0, surface=profile)
  path_right = navdy_model_line(path_x, path_y, None, 1.0, surface=profile)
  if _model_points(path_x, path_y) and not (path_left and path_right):
    # A short plan (stopped, creeping) ends under the hood line: nothing of the band is visible. Keep lanes and
    # vehicles drawn (the HUD draws the road only with a path) with a zero-area band off the view.
    path_left, path_right = list(HIDDEN_BAND), list(HIDDEN_BAND)
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
  # Disengaged but onroad (HUD v183): the road preview, lanes and vehicles without the path (user request
  # 2026-11-13). The HUD draws the road only with a path, so the path becomes a zero-area band off the view.
  preview = not active and driving  # fresh, valid inputs, onroad, not reversing: as for every other driving visual
  if active or preview:
    geometry = navdy_model_geometry(model)
    if geometry and preview:
      geometry["navPathLeft"], geometry["navPathRight"] = list(HIDDEN_BAND), list(HIDDEN_BAND)
      payload["roadPreview"] = True
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
