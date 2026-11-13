"""Bounded Navdy-only radar overlay. Never publishes or modifies control inputs."""
from __future__ import annotations

from itertools import islice
import math

from openpilot.selfdrive.navdy.display import (
  _lead_lane, _view_uv, camera_x, finite_float, model_line_y_at, navdy_vehicle_view, road_depth_at, rounded,
)

MAX_TRACKS = 64
MAX_VEHICLES = 8
TRACK_MAX_AGE = 0.35
MAX_DISTANCE = 80.0
CLUSTER_GAP_M = 3.5     # radar points closer than a car length in one lane may be one vehicle (a truck's side)
CLUSTER_LATERAL_M = 1.2  # within a truck's width
CLUSTER_SPEED_MPS = 1.5  # moving together
CLUSTER_MAX_M = 18.0     # no single vehicle is longer
CLUSTER_TRUCK_M = 4.5    # points spread over this length: a long vehicle


def vehicle_lane(model, distance, lateral):
  lane = _lead_lane(model, distance, lateral)
  if lane:
    return lane
  # Wayon's display fallback uses the path and measured/default lane width (at the camera distance, like the view).
  distance = camera_x(distance)
  path_y = model_line_y_at(getattr(model, 'position', None), distance)
  if path_y is None:
    return ''
  lanes = list(islice(getattr(model, 'laneLines', ()), 4))
  probs = list(islice(getattr(model, 'laneLineProbs', ()), 4))
  boundaries = [model_line_y_at(line, distance) for line in lanes]
  if len(boundaries) >= 4 and len(probs) >= 4:
    if boundaries[0] is not None and lateral < boundaries[0] and min(probs[:2]) >= 0.2:
      return ''
    if boundaries[3] is not None and lateral > boundaries[3] and min(probs[2:4]) >= 0.2:
      return ''
  width = 3.6
  if len(boundaries) >= 3 and boundaries[1] is not None and boundaries[2] is not None:
    measured = boundaries[2] - boundaries[1]
    if 2.4 <= measured <= 4.8:
      width = measured
  offset = lateral - path_y
  if abs(offset) <= width / 2:
    return 'center'
  if -1.5 * width <= offset < -width / 2:
    return 'left'
  if width / 2 < offset <= 1.5 * width:
    return 'right'
  return ''


def lane_heading(model, lane, distance):
  """Direction of the vehicle's lane (radians, right-positive) around a radar distance, 0 when unknown."""
  distance = camera_x(distance)
  near, far = max(0.0, distance - 4.0), min(MAX_DISTANCE, distance + 4.0)
  lanes = list(islice(getattr(model, 'laneLines', ()), 4))
  indices = {'left': (0, 1), 'right': (2, 3)}.get(lane)

  def center(d):
    if indices and len(lanes) > indices[1]:
      ys = [model_line_y_at(lanes[index], d) for index in indices]
      if all(y is not None for y in ys):
        return sum(ys) / 2
    return model_line_y_at(getattr(model, 'position', None), d)

  y0, y1 = center(near), center(far)
  if y0 is None or y1 is None or far - near < 1.0:
    return 0.0
  return math.atan2(y1 - y0, far - near)


SPRITE_PITCH_DEG = 2.5  # pitch the Navdy v180 vehicle sprites were rendered at


def vehicle_yaw(model, lane, distance, lateral):
  """Sprite yaw that makes a vehicle's body follow its lane in the Navdy road view.

  Step one metre along the lane and read the screen motion (dx sideways, dy up) in the view's perspective
  (display.navdy_vehicle_view). A sprite rendered at a fixed pitch p with yaw y has its long axis at
  tan(tilt) = tan(y) / sin(p) on screen, so matching the lane's tilt dx/dy gives y = atan(sin(p) * dx / dy).
  Payload sign: negative for a left-lane vehicle, whose front leans right on screen.
  """
  heading = lane_heading(model, lane, distance)
  step = 1.0 if distance + 1.0 <= MAX_DISTANCE else -1.0
  x0 = camera_x(distance)
  y1 = lateral + math.tan(heading) * step
  start = _view_uv(x0, lateral, road_depth_at(model, x0, lateral))
  end = _view_uv(x0 + step, y1, road_depth_at(model, x0 + step, y1))
  if not start or not end:
    return 0.0
  dx, dy = (end[0] - start[0]) / step, (start[1] - end[1]) / step
  if dy <= 0.0:
    return 0.0
  yaw = math.degrees(math.atan(math.sin(math.radians(SPRITE_PITCH_DEG)) * dx / dy))
  return rounded(max(-24.0, min(24.0, -yaw)))


def merge_long_vehicles(entries):
  """A truck or bus returns several radar points along its side; draw it once, at its rearmost point.

  From each rearmost point r, later points in the same lane join while each is within CLUSTER_GAP_M of the previous
  one, and all stay within CLUSTER_LATERAL_M and CLUSTER_SPEED_MPS of r and CLUSTER_MAX_M behind it. A run is one
  vehicle only with at least three points (the side of a long vehicle); two points are kept as they are, since
  two short vehicles or a motorcycle can be that close. Spread over CLUSTER_TRUCK_M or more it is drawn as a truck
  unless the camera names it later."""
  merged, used = [], set()
  ordered = sorted(entries, key=lambda vehicle: vehicle['distanceM'])
  for i, rear in enumerate(ordered):
    if i in used:
      continue
    run, last = [i], rear
    for j in range(i + 1, len(ordered)):
      other = ordered[j]
      if j in used or other['lane'] != rear['lane']:
        continue
      if other['distanceM'] - last['distanceM'] >= CLUSTER_GAP_M or other['distanceM'] - rear['distanceM'] > CLUSTER_MAX_M:
        break
      if (abs(other['lateralM'] - rear['lateralM']) < CLUSTER_LATERAL_M
          and abs(other['relativeSpeedMps'] - rear['relativeSpeedMps']) < CLUSTER_SPEED_MPS):
        run.append(j)
        last = other
    if len(run) < 3:
      merged.append(rear)
      continue
    used.update(run[1:])
    if last['distanceM'] - rear['distanceM'] >= CLUSTER_TRUCK_M and 'kind' not in rear:
      rear = dict(rear, kind='truck')
    merged.append(rear)
  return merged


class VehicleOverlay:
  def __init__(self):
    self.tracks = {}
    self.timestamp = 0.0
    self.flipped = None
    self.display_data = None
    self.alignment = None

  def clear(self):
    self.tracks.clear()
    self.timestamp = 0.0
    self.flipped = None
    self.display_data = None
    self.alignment = None

  def update(self, payload, model, data, *, timestamp, now, radar_alignment=None):
    """radar_alignment (car.radar_display_alignment.RadarDisplayAlignment or None): radar lateral -> camera frame."""
    errors = getattr(data, 'errors', None)
    if (not (payload['active'] or payload.get('roadPreview')) or not payload.get('navPathLeft') or data is None or errors is None
        or any(bool(getattr(errors, name, False)) for name in
               ('canError', 'radarFault', 'wrongConfig', 'radarUnavailableTemporary'))
        or not math.isfinite(timestamp) or not math.isfinite(now) or timestamp <= 0
        or not 0 <= now - timestamp <= TRACK_MAX_AGE):
      self.clear()
      return
    flipped = bool(getattr(data, 'radarTrackFlipped', False))
    display_data = bool(getattr(data, 'hasDisplayData', False))
    alignment = None if radar_alignment is None else radar_alignment.key()
    if (timestamp < self.timestamp or flipped != self.flipped or display_data != self.display_data
        or alignment != self.alignment):
      self.clear()
    self.flipped = flipped
    self.display_data = display_data
    self.alignment = alignment
    self.tracks = {key: value for key, value in self.tracks.items() if now - value['time'] <= TRACK_MAX_AGE}
    if timestamp > self.timestamp:
      self.timestamp = timestamp
      current = {}
      # An advertised display stream is authoritative even when empty.
      points = getattr(data, 'displayPoints', ()) if display_data else getattr(data, 'points', ())
      for point in islice(points, MAX_TRACKS):
        ident = finite_float(getattr(point, 'trackId', None), math.nan)
        distance = finite_float(getattr(point, 'dRel', None), math.nan)
        lateral = finite_float(getattr(point, 'yRel', None), math.nan)
        speed = finite_float(getattr(point, 'vRel', None), math.nan)
        if (not math.isfinite(ident) or not 0 <= ident <= 2**53 - 1 or ident != int(ident)
            or not 2.0 <= distance <= MAX_DISTANCE or not abs(lateral) <= 15.0 or not abs(speed) <= 100.0):
          continue
        ident = int(ident)
        if ident in current:
          continue
        previous = self.tracks.get(ident)
        # Published liveTracks already includes RadarTrackFlip. Convert only
        # radar-left-positive to model-right-positive; never flip a second time.
        lateral = -lateral
        if radar_alignment is not None:
          lateral = radar_alignment.right(distance, lateral)
        values = {'dRel': distance, 'yRel': lateral, 'vRel': speed}
        if (previous and timestamp - previous['time'] <= TRACK_MAX_AGE
            and abs(previous['dRel'] - distance) <= max(8.0, distance * 0.35)
            and abs(previous['yRel'] - lateral) <= 2.5):
          values = {key: previous[key] + 0.55 * (value - previous[key]) for key, value in values.items()}
          samples = min(2, previous['samples'] + 1)
        else:
          samples = 1
        current[ident] = dict(values, time=timestamp, samples=samples)
      # A fresh complete radar snapshot may explicitly remove a target.
      self.tracks = current

    leads = payload['navVehicles']
    surrounding = []
    for ident, track in self.tracks.items():
      if track['samples'] < 2:
        continue
      distance, lateral = track['dRel'], track['yRel']
      lane = vehicle_lane(model, distance, lateral)
      if not lane:
        continue
      # Preserve actual lead positions/highlights; raw points only fill in
      # additional vehicles. Display streams may assign independent IDs; use
      # tight geometric matching there, never ID equality across namespaces.
      if any((not display_data and lead['trackId'] == ident) or
             ((display_data or lead['source'] == 'vision') and lead['lane'] == lane
              and abs(lead['distanceM'] - distance) <= (max(2.5, distance * 0.06) if display_data else max(5.0, distance * 0.25))
              and abs(lead['lateralM'] - lateral) <= (1.0 if display_data else 1.5)
              and abs(lead['relativeSpeedMps'] - track['vRel']) <= (3.0 if display_data else 5.0)) for lead in leads):
        continue
      view = navdy_vehicle_view(model, distance, lateral)
      if view is None:
        continue
      surrounding.append({
        'trackId': ident, 'screenX': view['screenX'], 'screenY': view['screenY'], 'pxPerM': view['pxPerM'],
        'yawDeg': vehicle_yaw(model, lane, distance, lateral), 'distanceM': rounded(distance),
        'relativeSpeedMps': rounded(track['vRel']), 'lateralM': rounded(lateral, 2),
        'lane': lane, 'source': 'radar', 'longitudinalLead': False, 'confidence': 0.65,
      })
    surrounding = merge_long_vehicles(surrounding)
    surrounding.sort(key=lambda vehicle: vehicle['distanceM'])
    selected = leads + surrounding[:max(0, MAX_VEHICLES - len(leads))]
    payload['navVehicles'] = sorted(selected, key=lambda vehicle: vehicle['distanceM'], reverse=True)


CAMERA_GATE_DISTANCE = 5.0  # metres, same gates as the phone live view
CAMERA_GATE_LATERAL = 1.8
CAMERA_TRACK_ID_BASE = 900000


def _camera_vehicle(model, obj):
  distance, lateral = obj['distance'], obj['lateral']
  if not 2.0 <= distance <= MAX_DISTANCE or not abs(lateral) <= 15.0:
    return None
  lane = vehicle_lane(model, distance, lateral)
  if not lane:
    return None
  view = navdy_vehicle_view(model, distance, lateral)
  if view is None:
    return None
  return {
    'trackId': CAMERA_TRACK_ID_BASE + int(obj['id']), 'screenX': view['screenX'], 'screenY': view['screenY'],
    'pxPerM': view['pxPerM'], 'yawDeg': vehicle_yaw(model, lane, distance, lateral), 'distanceM': rounded(distance),
    'relativeSpeedMps': 0.0, 'lateralM': rounded(lateral, 2), 'lane': lane, 'source': 'vision',
    'longitudinalLead': False, 'confidence': 0.6, 'kind': obj['kind'],
  }


def annotate_camera(payload, model, objects, lamps):
  """Give navVehicles the stock camera's kind and lead lamps, and add camera-only objects.

  Display only. Radar/lead entries keep their positions; a camera object within 5 m / 1.8 m lends
  its kind. Track 1's lamps go to whichever entry carries track 1. Camera objects nobody covers
  (pedestrians, motorcycles, vehicles radar missed) are added like the phone live view does.
  """
  vehicles = payload.get('navVehicles')
  if not isinstance(vehicles, list):
    return
  for vehicle in vehicles:
    if 'yawDeg' not in vehicle:
      vehicle['yawDeg'] = vehicle_yaw(model, vehicle.get('lane', ''), vehicle['distanceM'], vehicle['lateralM'])
  pairs = []
  for v_index, vehicle in enumerate(vehicles):
    for o_index, obj in enumerate(objects):
      dd, dl = abs(vehicle['distanceM'] - obj['distance']), abs(vehicle['lateralM'] - obj['lateral'])
      if dd < CAMERA_GATE_DISTANCE and dl < CAMERA_GATE_LATERAL:
        pairs.append((dd / CAMERA_GATE_DISTANCE + dl / CAMERA_GATE_LATERAL, v_index, o_index))
  matched_vehicles, matched_objects, track1 = set(), set(), None
  track1_id = next((obj['id'] for obj in objects if obj.get('track1')), None)
  for _, v_index, o_index in sorted(pairs):
    if v_index in matched_vehicles or o_index in matched_objects:
      continue
    matched_vehicles.add(v_index)
    matched_objects.add(o_index)
    vehicles[v_index]['kind'] = objects[o_index]['kind']
    if objects[o_index].get('track1'):
      track1 = vehicles[v_index]
  extra = []
  if (payload.get('active') or payload.get('roadPreview')) and payload.get('navPathLeft'):
    for o_index, obj in enumerate(objects):
      if o_index in matched_objects:
        continue
      vehicle = _camera_vehicle(model, obj)
      if vehicle is not None:
        extra.append(vehicle)
        if obj.get('track1'):
          track1 = vehicle
  if lamps is not None and track1 is not None and lamps.get('id') == track1_id:
    track1['brakeLight'] = bool(lamps['brake'])
    track1['turnSignal'] = lamps['turn']
  extra.sort(key=lambda vehicle: vehicle['distanceM'])
  selected = vehicles + extra[:max(0, MAX_VEHICLES - len(vehicles))]
  payload['navVehicles'] = sorted(selected, key=lambda vehicle: vehicle['distanceM'], reverse=True)
