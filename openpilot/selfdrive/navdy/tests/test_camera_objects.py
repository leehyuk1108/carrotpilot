import math
from types import SimpleNamespace as NS

import pytest

from openpilot.selfdrive.navdy import camera_objects as co
from openpilot.selfdrive.navdy.display import _view_uv
from openpilot.selfdrive.navdy.tests.test_display import model, payload
from openpilot.selfdrive.navdy.vehicles import MAX_VEHICLES, SPRITE_PITCH_DEG, annotate_camera, vehicle_yaw


def track_frame(ident=5, kind=1, azimuth_deg=0.0, range_m=30.0, width_m=1.75, burst=0):
  az = round(azimuth_deg / 0.1) & 0x3FF
  rng = round(range_m / 0.1) & 0xFFF
  data = bytearray(8)
  data[0] = ((ident & 0x3F) << 2) | (burst & 3)
  data[1] = ((kind & 0xF) << 3) | (az >> 7)
  data[2] = ((az & 0x7F) << 1) | (rng >> 11)
  data[3] = (rng >> 3) & 0xFF
  data[4] = (rng & 7) << 5
  data[7] = round(width_m / 0.25) & 0x3F
  return bytes(data)


def lamp_frame(brake=False, turn=0, burst=0):
  data = bytearray(8)
  data[0] = (burst & 3) << 6
  data[3] = (turn >> 1) & 3
  data[4] = ((turn & 1) << 7) | ((2 if brake else 0) << 5)
  return bytes(data)


def observe(cam, t, ident=7, brake=False, turn=0, burst=None):
  """One camera observation: track-1 frame A then its lamp frame B 5 ms later (same burst)."""
  burst = int(t * 25) & 3 if burst is None else burst
  cam.feed([(0x421, track_frame(ident=ident, burst=burst), 1)], t)
  cam.feed([(0x427, lamp_frame(brake=brake, turn=turn, burst=burst), 1)], t + 0.005)


def strip(lamps):
  return None if lamps is None else {k: v for k, v in lamps.items() if k != 'id'}


def test_track_decoding_matches_the_dbc_layout_and_left_positive_azimuth():
  left = co.decode_track(track_frame(ident=7, kind=2, azimuth_deg=6.0, range_m=31.6, width_m=2.5))
  assert left['id'] == 7 and left['kind'] == 'truck' and left['width'] == 2.5
  assert left['distance'] == pytest.approx(31.6 * math.cos(math.radians(6.0)) - 1.6, abs=0.01)
  # Camera azimuth is left-positive; the overlay is right-positive, so a left object has negative lateral.
  assert left['lateral'] == pytest.approx(-31.6 * math.sin(math.radians(6.0)), abs=0.01)
  right = co.decode_track(track_frame(azimuth_deg=-6.0))
  assert right['lateral'] > 0
  assert [co.decode_track(track_frame(kind=k))['kind'] for k in (1, 2, 3, 4)] == ['car', 'truck', 'moto', 'ped']
  assert co.decode_track(track_frame(burst=3))['id'] == 5


@pytest.mark.parametrize('frame', [track_frame(ident=0), track_frame(kind=0), track_frame(kind=5),
                                   track_frame(range_m=0.5), track_frame()[:7]])
def test_invalid_tracks_are_ignored(frame):
  assert co.decode_track(frame) is None


def test_lamp_bits():
  assert co.decode_lamps(lamp_frame(brake=True)) == (True, 0)
  assert co.decode_lamps(lamp_frame(turn=2, burst=3)) == (False, 2)
  assert co.decode_lamps(lamp_frame(brake=True, turn=3)) == (True, 3)
  assert co.decode_lamps(lamp_frame()[:6]) is None


def test_objects_keep_only_fresh_object_bus_frames_and_mark_track_one():
  cam = co.CameraObjects()
  cam.feed([(0x421, track_frame(ident=1), 1), (0x441, track_frame(ident=2, kind=4), 1),
            (0x422, track_frame(ident=3), 0), (0x123, track_frame(ident=4), 1)], 10.0)
  objects = cam.objects(10.1)
  assert [(o['id'], o['track1']) for o in objects] == [(1, True), (2, False)]
  assert cam.objects(10.0 + co.OBJECT_MAX_AGE + 0.01) == []


def test_turn_side_is_held_between_flashes_and_hazard_is_ignored():
  cam = co.CameraObjects()
  observe(cam, 10.0, turn=2)
  observe(cam, 10.2)
  assert strip(cam.lead_lamps(10.21)) == {'brake': False, 'turn': 'left'}
  observe(cam, 11.12)
  assert strip(cam.lead_lamps(11.15)) == {'brake': False, 'turn': 'left'}
  observe(cam, 11.24)
  assert cam.lead_lamps(11.25)['turn'] == ''
  observe(cam, 12.0, brake=True, turn=4)
  assert strip(cam.lead_lamps(12.01)) == {'brake': True, 'turn': ''}
  assert cam.lead_lamps(12.01 + co.OBJECT_MAX_AGE + 0.01) is None


def test_a_new_lead_inherits_neither_turn_nor_brake():
  cam = co.CameraObjects()
  observe(cam, 10.0, ident=7, brake=True, turn=2)
  assert strip(cam.lead_lamps(10.01)) == {'brake': True, 'turn': 'left'}
  cam.feed([(0x421, track_frame(ident=8, burst=1), 1)], 10.04)   # new object, its B not here yet
  assert cam.lead_lamps(10.05) is None
  cam.feed([(0x427, lamp_frame(turn=0, burst=1), 1)], 10.045)
  assert strip(cam.lead_lamps(10.05)) == {'brake': False, 'turn': ''}
  observe(cam, 10.4, ident=8, turn=3)
  assert cam.lead_lamps(10.41)['turn'] == 'right'
  # Track 1 going stale also ends everything it showed.
  assert cam.lead_lamps(10.4 + co.OBJECT_MAX_AGE + 0.05) is None


def test_lamp_frames_from_another_burst_or_too_late_are_never_attached():
  cam = co.CameraObjects()
  observe(cam, 10.0, ident=7, burst=1, brake=False)
  cam.feed([(0x421, track_frame(ident=8, burst=2), 1)], 10.2)
  cam.feed([(0x427, lamp_frame(turn=2, brake=True, burst=1), 1)], 10.21)   # the old object's late B
  cam.feed([(0x427, lamp_frame(burst=2), 1)], 10.215)                       # this burst's B
  assert strip(cam.lead_lamps(10.22)) == {'brake': False, 'turn': ''}
  cam.feed([(0x421, track_frame(ident=8, burst=3), 1)], 10.24)
  cam.feed([(0x427, lamp_frame(turn=3, burst=3), 1)], 10.24 + co.LAMP_PAIR_WINDOW + 0.01)
  assert cam.lead_lamps(10.29)['turn'] == ''


class FakeEvent:
  def __init__(self, t, frames, valid=True):
    self.logMonoTime = int(t * 1e9)
    self.valid = valid
    self.can = [NS(address=a, dat=d, src=s) for a, d, s in frames]


def test_frames_from_events_keeps_camera_addresses_of_valid_events():
  events = [FakeEvent(1.0, [(0x421, b'a' * 8, 1), (0x1E9, b'b' * 8, 0)]), FakeEvent(1.01, [(0x427, b'c' * 8, 1)]),
            FakeEvent(1.02, [(0x421, b'd' * 8, 1)], valid=False)]
  assert co.frames_from_events(events) == [(1.0, [(0x421, b'a' * 8, 1)]), (1.01, [(0x427, b'c' * 8, 1)])]


def test_door_monitor_feeds_the_camera_only_onroad_on_gm_and_never_raises():
  from openpilot.selfdrive.navdy.ambient import DoorMonitor
  cam = co.CameraObjects()
  doors = DoorMonitor(path=__import__('pathlib').Path('/nonexistent/navdy-door-test'), camera=cam)
  events = [FakeEvent(10.0, [(0x421, track_frame(ident=3), 1)])]
  doors.gm = True
  doors.feed_camera(events, False)
  assert cam.objects(10.05) == []
  doors.feed_camera(events, True)
  assert [o['id'] for o in cam.objects(10.05)] == [3]
  doors.gm = False
  cam.clear()
  doors.feed_camera(events, True)
  assert cam.objects(10.05) == []
  doors.gm = True
  doors.feed_camera([object()], True)
  assert doors.camera_error.startswith('AttributeError')


def road_motion(distance, lateral):
  """Straight road: screen motion (dx right, dy up) per metre forward, from the projection itself."""
  step = 1.0 if distance + 1.0 <= 80.0 else -1.0
  x = distance + 1.52  # radar dRel -> camera
  a, b = _view_uv(x, lateral, 1.36), _view_uv(x + step, lateral, 1.36)
  return (b[0] - a[0]) / step, (a[1] - b[1]) / step


@pytest.mark.parametrize('distance', [8.0, 20.0, 45.0, 70.0, 79.5])
def test_sprite_axis_follows_the_lane_lines(distance):
  straight = model()
  left = vehicle_yaw(straight, 'left', distance, -3.6)
  right = vehicle_yaw(straight, 'right', distance, 3.6)
  dx, dy = road_motion(distance, -3.6)
  # A sprite at pitch p and yaw y draws its long axis at tan(tilt) = tan(y) / sin(p); it must match dx/dy.
  sprite_tilt = math.tan(math.radians(-left)) / math.sin(math.radians(SPRITE_PITCH_DEG))
  assert sprite_tilt == pytest.approx(dx / dy, rel=0.05)
  assert right == pytest.approx(-left, abs=0.15)
  # In the camera view a straight lane 3.6 m over tilts by 3.6 / 1.36 at every distance: about 6.6 degrees.
  assert left == pytest.approx(-math.degrees(math.atan(math.sin(math.radians(SPRITE_PITCH_DEG)) * 3.6 / 1.36)), abs=0.15)
  assert vehicle_yaw(straight, 'center', distance, 0.0) == pytest.approx(0.0, abs=0.15)
  assert abs(vehicle_yaw(straight, 'left', distance, -7.2)) > abs(left)


def car_payload(**kwargs):
  result = payload(model=model(), **kwargs)
  result['navVehicles'] = [
    {'trackId': 1, 'screenX': 160, 'screenY': 50, 'distanceM': 30.0, 'relativeSpeedMps': -1.0, 'lateralM': 0.0,
     'lane': 'center', 'source': 'radar', 'longitudinalLead': True, 'confidence': 0.9},
    {'trackId': 2, 'screenX': 80, 'screenY': 60, 'yawDeg': -3.0, 'distanceM': 20.0, 'relativeSpeedMps': 0.0,
     'lateralM': -3.6, 'lane': 'left', 'source': 'radar', 'longitudinalLead': False, 'confidence': 0.65},
  ]
  return result


def obj(ident, kind, distance, lateral, track1=False):
  return {'id': ident, 'kind': kind, 'distance': distance, 'lateral': lateral, 'width': 1.8, 'track1': track1}


def test_kinds_and_track_one_lamps_attach_to_the_matching_vehicles():
  result = car_payload()
  annotate_camera(result, model(), [obj(1, 'car', 31.0, 0.3, track1=True), obj(2, 'truck', 22.0, -3.2)],
                  {'brake': True, 'turn': 'right', 'id': 1})
  by_id = {v['trackId']: v for v in result['navVehicles']}
  assert by_id[1]['kind'] == 'car' and by_id[1]['brakeLight'] is True and by_id[1]['turnSignal'] == 'right'
  assert by_id[2]['kind'] == 'truck' and 'brakeLight' not in by_id[2]
  assert 'yawDeg' in by_id[1] and by_id[2]['yawDeg'] == -3.0  # lead gains a yaw; overlay yaw kept
  assert len(result['navVehicles']) == 2


def test_lamps_of_another_object_are_never_attached():
  result = car_payload()
  annotate_camera(result, model(), [obj(1, 'car', 31.0, 0.3, track1=True)], {'brake': True, 'turn': 'left', 'id': 2})
  assert all('brakeLight' not in v and 'turnSignal' not in v for v in result['navVehicles'])


def test_snapshot_returns_objects_and_lamps_of_one_observation():
  cam = co.CameraObjects()
  observe(cam, 10.0, ident=7, brake=True, turn=3)
  objects, lamps = cam.snapshot(10.01)
  assert [o['id'] for o in objects if o['track1']] == [7] and lamps == {'brake': True, 'turn': 'right', 'id': 7}
  cam.feed([(0x421, track_frame(ident=8, burst=1), 1)], 10.04)
  objects, lamps = cam.snapshot(10.05)
  assert [o['id'] for o in objects if o['track1']] == [8] and lamps is None


def test_far_objects_do_not_lend_their_kind_and_become_their_own_entries():
  result = car_payload()
  result['active'] = True
  result['navPathLeft'] = result.get('navPathLeft') or [1.0]
  annotate_camera(result, model(), [obj(3, 'ped', 12.0, 3.4), obj(4, 'moto', 40.0, 0.0)], None)
  kinds = {v['trackId']: v.get('kind') for v in result['navVehicles']}
  assert kinds[1] is None and kinds[2] is None
  added = [v for v in result['navVehicles'] if v['trackId'] >= 900000]
  assert {v['kind'] for v in added} == {'ped', 'moto'}
  assert all(v['source'] == 'vision' and 'yawDeg' in v and v['lane'] for v in added)


def test_camera_only_entries_need_an_active_drive_and_respect_the_vehicle_limit():
  result = car_payload()
  result['active'] = False
  annotate_camera(result, model(), [obj(3, 'ped', 12.0, 3.4)], None)
  assert len(result['navVehicles']) == 2
  result = car_payload()
  result['active'] = True
  result['navPathLeft'] = result.get('navPathLeft') or [1.0]
  annotate_camera(result, model(), [obj(10 + i, 'car', 10.0 + 6 * i, 3.5) for i in range(10)], None)
  assert len(result['navVehicles']) == MAX_VEHICLES
  distances = [v['distanceM'] for v in result['navVehicles']]
  assert distances == sorted(distances, reverse=True)


# ---- Radar tunnel flag (bus1 0x460 byte3 bit3), for the dark-ambient tunnel exception ----

def radar_header(tunnel, length=8):
  data = bytearray(length)
  if length > 3 and tunnel:
    data[3] = 0x08
  return bytes(data)


def test_radar_tunnel_flag_is_read_and_expires():
  from openpilot.selfdrive.navdy.camera_objects import CameraObjects, TUNNEL_ADDRESS, TUNNEL_MAX_AGE
  camera = CameraObjects()
  assert camera.tunnel(10.0) is None
  camera.feed([(TUNNEL_ADDRESS, radar_header(True), 1)], 10.0)
  assert camera.tunnel(10.0) is True
  assert camera.tunnel(10.0 + TUNNEL_MAX_AGE) is True
  assert camera.tunnel(10.0 + TUNNEL_MAX_AGE + 0.01) is None
  camera.feed([(TUNNEL_ADDRESS, radar_header(False), 1)], 11.0)
  assert camera.tunnel(11.0) is False
  # Other buses, short frames and an older frame never change it.
  camera.feed([(TUNNEL_ADDRESS, radar_header(True), 0), (TUNNEL_ADDRESS, radar_header(True, 4), 1)], 11.1)
  camera.feed([(TUNNEL_ADDRESS, radar_header(True), 1)], 10.5)
  assert camera.tunnel(11.1) is False
  assert camera.objects(11.1) == []
  camera.clear()
  assert camera.tunnel(11.1) is None


def test_live_tunnel_reads_the_can_clock_after_the_snapshot(monkeypatch):
  from openpilot.selfdrive.navdy import camera_objects as mod
  camera = mod.CameraObjects()
  clock = {'boot': 500.0}
  monkeypatch.setattr(mod, 'boottime_seconds', lambda: clock['boot'])
  monkeypatch.setattr(mod.time, 'monotonic', lambda: 100.0)  # after a suspend: far behind the CAN clock
  camera.feed([(mod.TUNNEL_ADDRESS, radar_header(True), 1)], 500.0)
  assert camera.tunnel() is True
  clock['boot'] = 500.0 + mod.TUNNEL_MAX_AGE + 0.01
  assert camera.tunnel() is None

  # The CAN clock moves on every read, and the door-monitor thread stores a newer header while the
  # bridge waits for the lock: a time taken before the snapshot would make that header "future".
  clock['boot'] = 600.0

  def ticking():
    clock['boot'] += 0.5
    return clock['boot']
  monkeypatch.setattr(mod, 'boottime_seconds', ticking)
  real_lock = camera.lock

  class FeedWhileLocking:
    def __enter__(self):
      real_lock.__enter__()
      camera.tunnel_seen = (clock['boot'] + 0.25, True)
    def __exit__(self, *exc):
      return real_lock.__exit__(*exc)

  camera.lock = FeedWhileLocking()
  assert camera.tunnel() is True


def test_frames_from_events_keeps_the_radar_header():
  from types import SimpleNamespace as NS
  from openpilot.selfdrive.navdy.camera_objects import TUNNEL_ADDRESS, frames_from_events
  event = NS(valid=True, logMonoTime=2_000_000_000,
             can=[NS(address=TUNNEL_ADDRESS, dat=radar_header(True), src=1), NS(address=0x123, dat=b'x', src=1)])
  assert frames_from_events([event]) == [(2.0, [(TUNNEL_ADDRESS, radar_header(True), 1)])]
