import math
from contextlib import contextmanager
from types import SimpleNamespace as NS

import pytest

from openpilot.selfdrive.navdy import camera_objects as co
from openpilot.selfdrive.navdy.display import navdy_project_point
from openpilot.selfdrive.navdy.tests.test_display import model, payload
from openpilot.selfdrive.navdy.vehicles import MAX_VEHICLES, annotate_camera, projection_scale, vehicle_yaw


def track_frame(ident=5, kind=1, azimuth_deg=0.0, range_m=30.0, width_m=1.75):
  az = round(azimuth_deg / 0.1) & 0x3FF
  rng = round(range_m / 0.1) & 0xFFF
  data = bytearray(8)
  data[0] = (ident & 0x3F) << 2
  data[1] = ((kind & 0xF) << 3) | (az >> 7)
  data[2] = ((az & 0x7F) << 1) | (rng >> 11)
  data[3] = (rng >> 3) & 0xFF
  data[4] = (rng & 7) << 5
  data[7] = round(width_m / 0.25) & 0x3F
  return bytes(data)


def lamp_frame(brake=False, turn=0):
  data = bytearray(8)
  data[3] = (turn >> 1) & 3
  data[4] = ((turn & 1) << 7) | ((2 if brake else 0) << 5)
  return bytes(data)


def test_track_decoding_matches_the_dbc_layout_and_left_positive_azimuth():
  left = co.decode_track(track_frame(ident=7, kind=2, azimuth_deg=6.0, range_m=31.6, width_m=2.5))
  assert left['id'] == 7 and left['kind'] == 'truck' and left['width'] == 2.5
  assert left['distance'] == pytest.approx(31.6 * math.cos(math.radians(6.0)) - 1.6, abs=0.01)
  # Camera azimuth is left-positive; the overlay is right-positive, so a left object has negative lateral.
  assert left['lateral'] == pytest.approx(-31.6 * math.sin(math.radians(6.0)), abs=0.01)
  right = co.decode_track(track_frame(azimuth_deg=-6.0))
  assert right['lateral'] > 0
  assert [co.decode_track(track_frame(kind=k))['kind'] for k in (1, 2, 3, 4)] == ['car', 'truck', 'moto', 'ped']


@pytest.mark.parametrize('frame', [track_frame(ident=0), track_frame(kind=0), track_frame(kind=5),
                                   track_frame(range_m=0.5), track_frame()[:7]])
def test_invalid_tracks_are_ignored(frame):
  assert co.decode_track(frame) is None


def test_lamp_bits():
  assert co.decode_lamps(lamp_frame(brake=True)) == (True, 0)
  assert co.decode_lamps(lamp_frame(turn=2)) == (False, 2)
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
  cam.feed([(0x427, lamp_frame(turn=2), 1)], 10.0)
  cam.feed([(0x427, lamp_frame(turn=0), 1)], 10.2)
  assert cam.lead_lamps(10.2) == {'brake': False, 'turn': 'left'}
  cam.feed([(0x427, lamp_frame(turn=0), 1)], 11.15)
  assert cam.lead_lamps(11.15) == {'brake': False, 'turn': 'left'}
  cam.feed([(0x427, lamp_frame(turn=0), 1)], 11.25)
  assert cam.lead_lamps(11.25)['turn'] == ''
  cam.feed([(0x427, lamp_frame(brake=True, turn=4), 1)], 12.0)
  assert cam.lead_lamps(12.0) == {'brake': True, 'turn': ''}
  assert cam.lead_lamps(12.0 + co.OBJECT_MAX_AGE + 0.01) is None


class FakeEvent:
  def __init__(self, t, frames):
    self.logMonoTime = int(t * 1e9)
    self.can = [NS(address=a, dat=d, src=s) for a, d, s in frames]


class FakeLog:
  class Event:
    @staticmethod
    @contextmanager
    def from_bytes(raw):
      yield raw


def test_camera_frames_reads_only_wanted_addresses():
  events = [FakeEvent(1.0, [(0x421, b'a' * 8, 1), (0x1E9, b'b' * 8, 0)]), FakeEvent(1.01, [(0x427, b'c' * 8, 1)])]
  assert co.camera_frames(events, FakeLog) == [(1.0, [(0x421, b'a' * 8, 1)]), (1.01, [(0x427, b'c' * 8, 1)])]


class FakeMessaging:
  def __init__(self, batches):
    self.batches = list(batches)
    self.subscribed = 0

  def sub_sock(self, name, conflate=False):
    assert name == 'can' and conflate is False
    self.subscribed += 1
    return object()

  def drain_sock_raw(self, sock):
    return self.batches.pop(0) if self.batches else []


def test_feed_parses_only_the_newest_events_and_closes_when_disabled():
  seen = []
  raw = [FakeEvent(10.0 + i * 0.01, [(0x421, track_frame(ident=1 + i % 3), 1)]) for i in range(20)]
  feed = co.CameraFeed(FakeMessaging([raw]), parse=lambda r: seen.append(len(r)) or co.camera_frames(r, FakeLog))
  feed.poll(True)
  assert seen == [co.PARSE_EVENTS]
  assert [o['id'] for o in feed.objects.objects(10.2)] == [1 + 19 % 3]
  feed.poll(False)
  assert feed.sock is None and feed.objects.objects(10.2) == []


def test_feed_errors_never_escape():
  feed = co.CameraFeed(FakeMessaging([[object()]]), parse=lambda r: 1 / 0)
  feed.poll(True)
  assert feed.error.startswith('ZeroDivisionError') and feed.sock is None
  assert feed.status(0.0) == {'objects': 0, 'lamps': False, 'error': feed.error}


def independent_yaw(distance, lateral):
  """Straight road: screen sideways motion per metre forward over the width scale (as in the mockup)."""
  a = navdy_project_point(distance, lateral, 0.0)
  b = navdy_project_point(distance + 1.0, lateral, 0.0)
  return -math.degrees(math.asin((b[0] - a[0]) / projection_scale(distance)))


@pytest.mark.parametrize('distance', [8.0, 20.0, 45.0, 70.0])
def test_yaw_follows_the_navdy_projection(distance):
  straight = model()
  left = vehicle_yaw(straight, 'left', distance, -3.6)
  right = vehicle_yaw(straight, 'right', distance, 3.6)
  assert left == pytest.approx(independent_yaw(distance, -3.6), abs=0.15)
  assert right == pytest.approx(-left, abs=0.15)
  # The squeezed road makes adjacent-lane vehicles turn only a few degrees, never the old 8-24.
  assert -7.0 < left < 0 < right < 7.0
  assert vehicle_yaw(straight, 'center', distance, 0.0) == pytest.approx(0.0, abs=0.15)


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
                  {'brake': True, 'turn': 'right'})
  by_id = {v['trackId']: v for v in result['navVehicles']}
  assert by_id[1]['kind'] == 'car' and by_id[1]['brakeLight'] is True and by_id[1]['turnSignal'] == 'right'
  assert by_id[2]['kind'] == 'truck' and 'brakeLight' not in by_id[2]
  assert 'yawDeg' in by_id[1] and by_id[2]['yawDeg'] == -3.0  # lead gains a yaw; overlay yaw kept
  assert len(result['navVehicles']) == 2


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
