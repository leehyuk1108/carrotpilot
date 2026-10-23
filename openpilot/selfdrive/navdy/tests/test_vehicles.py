import copy
import json
import math
from types import SimpleNamespace as NS
import zlib

import pytest

from openpilot.selfdrive.navdy import bridge
from openpilot.selfdrive.navdy.display import navdy_lead_geometry
from openpilot.selfdrive.navdy.tests.test_bridge import Messages
from openpilot.selfdrive.navdy.tests.test_display import lead, model, payload, selfdrive
from openpilot.selfdrive.navdy.transport import encode_frame
from openpilot.selfdrive.navdy.vehicles import MAX_TRACKS, MAX_VEHICLES, TRACK_MAX_AGE, VehicleOverlay


def point(ident=1, distance=30., lateral=3.6, speed=-1.):
  return NS(trackId=ident, dRel=distance, yRel=lateral, vRel=speed, measured=True)


def data(points=None, **errors):
  return NS(points=[point()] if points is None else points, errors=NS(**errors), radarTrackFlipped=False)


def render(overlay, radar=None, *, timestamp=100., now=None, m=None, leads=None, **kwargs):
  m = model() if m is None else m
  result = payload(model=m, radar_state=leads, **kwargs)
  overlay.update(result, m, data() if radar is None else radar, timestamp=timestamp,
                 now=timestamp if now is None else now)
  return result


def confirmed(overlay=None, radar=None, **kwargs):
  overlay = VehicleOverlay() if overlay is None else overlay
  render(overlay, radar, timestamp=100., **kwargs)
  return render(overlay, radar, timestamp=100.2, **kwargs)


def test_surrounding_left_center_right_requires_two_distinct_samples():
  overlay = VehicleOverlay()
  radar = data([point(1, lateral=3.6), point(2, lateral=0), point(3, lateral=-3.6)])
  original = copy.deepcopy(radar)
  assert render(overlay, radar)['navVehicles'] == []
  assert render(overlay, radar, now=100.1)['navVehicles'] == []
  result = render(overlay, radar, timestamp=100.2)
  cars = {car['lane']: car for car in result['navVehicles']}
  assert len(cars) == 3
  assert cars['left']['screenX'] < 160 < cars['right']['screenX']
  assert cars['left']['lateralM'] == -3.6 and cars['right']['lateralM'] == 3.6
  assert cars['left']['yawDeg'] < 0 < cars['right']['yawDeg']
  assert radar == original


def test_preserves_leads_and_every_non_vehicle_field():
  radar = data([point(42, 40., 0.), point(2, lateral=3.6), point(3, lateral=-3.6)])
  leads = NS(leadOne=lead(), leadTwo=lead())
  plan = NS(longitudinalPlanSource='lead0')
  result = confirmed(radar=radar, leads=leads, longitudinal_plan=plan)
  baseline = payload(model=model(), radar_state=leads, longitudinal_plan=plan)
  assert len(result['navVehicles']) == 3
  assert [car for car in result['navVehicles'] if car['trackId'] == 42] == baseline['navVehicles']
  assert {k: v for k, v in result.items() if k != 'navVehicles'} == {k: v for k, v in baseline.items() if k != 'navVehicles'}


def test_camera_only_lead_is_not_duplicated_by_matching_raw_point():
  result = confirmed(radar=data([point(42, 40., 0.)]), leads=NS(leadOne=lead(radar=False, radarTrackId=-1)))
  assert len(result['navVehicles']) == 1 and result['navVehicles'][0]['source'] == 'vision'


def test_different_radar_ids_are_not_merged_just_because_they_are_close():
  result = confirmed(radar=data([point(43, 44., 0.)]), leads=NS(leadOne=lead()))
  assert len(result['navVehicles']) == 2


def test_eight_vehicle_limit_preserves_leads_and_sorts_far_to_near():
  radar = data([point(i, 10. + i * 2., (i % 3 - 1) * 3.6) for i in range(20)])
  leads = NS(leadOne=lead(dRel=79.), leadTwo=lead(dRel=78., radarTrackId=43))
  result = confirmed(radar=radar, leads=leads)
  cars = result['navVehicles']
  assert len(cars) == MAX_VEHICLES
  assert {42, 43} <= {car['trackId'] for car in cars}
  assert [car['distanceM'] for car in cars] == sorted([car['distanceM'] for car in cars], reverse=True)
  encoded = encode_frame(result)
  assert json.loads(encoded)['navVehicles'] == cars
  assert len(encoded) < 32768
  assert zlib.decompress(zlib.compress(encoded)) == encoded


@pytest.mark.parametrize('error', ['canError', 'radarFault', 'wrongConfig', 'radarUnavailableTemporary'])
def test_radar_error_clears_overlay_but_not_published_lead(error):
  overlay = VehicleOverlay()
  assert confirmed(overlay)['navVehicles']
  leads = NS(leadOne=lead())
  result = render(overlay, data(**{error: True}), timestamp=100.3, leads=leads)
  assert result['navVehicles'] == navdy_lead_geometry(model(), leads)['navVehicles']
  assert not overlay.tracks


@pytest.mark.parametrize('age', [TRACK_MAX_AGE + .001, 1., -0.01, math.nan, math.inf])
def test_stale_future_or_nonfinite_capture_clears_overlay(age):
  overlay = VehicleOverlay()
  confirmed(overlay)
  assert render(overlay, timestamp=100.2, now=100.2 + age)['navVehicles'] == []
  assert not overlay.tracks


def test_empty_snapshot_disappearance_and_late_reappearance_require_confirmation():
  overlay = VehicleOverlay()
  assert confirmed(overlay)['navVehicles']
  assert render(overlay, data([]), timestamp=100.3)['navVehicles'] == []
  assert render(overlay, timestamp=100.4)['navVehicles'] == []
  assert render(overlay, timestamp=100.6)['navVehicles']
  assert render(overlay, timestamp=101.)['navVehicles'] == []


@pytest.mark.parametrize('kwargs', [
  {'onroad': False}, {'selfdrive_state': selfdrive(enabled=False, active=False)},
  {'selfdrive_state': None}, {'car_state': None},
])
def test_disengagement_and_invalid_state_clear_immediately(kwargs):
  overlay = VehicleOverlay()
  confirmed(overlay)
  assert render(overlay, timestamp=100.3, **kwargs)['navVehicles'] == []
  assert not overlay.tracks
  assert render(overlay, timestamp=100.4)['navVehicles'] == []


@pytest.mark.parametrize('field,value', [
  ('trackId', -1), ('trackId', math.nan), ('trackId', 2**53), ('trackId', 1.5),
  ('dRel', 1.99), ('dRel', 80.01), ('dRel', math.inf), ('dRel', math.nan),
  ('yRel', math.nan), ('yRel', 16.), ('vRel', math.nan), ('vRel', 101.),
])
def test_bad_points_are_rejected(field, value):
  p = point()
  setattr(p, field, value)
  assert confirmed(radar=data([p]))['navVehicles'] == []


def test_tracker_bounded_and_duplicate_ids_not_counted_as_two_samples():
  overlay = VehicleOverlay()
  assert render(overlay, data([point()] * 500))['navVehicles'] == []
  confirmed(overlay, data([point(i) for i in range(1000)]))
  assert len(overlay.tracks) == MAX_TRACKS


def test_radar_flip_is_not_applied_twice_and_change_resets_smoothing():
  overlay = VehicleOverlay()
  radar = data([point(lateral=-3.6)])
  radar.radarTrackFlipped = True
  assert confirmed(overlay, radar)['navVehicles'][0]['lane'] == 'right'
  radar.radarTrackFlipped = False
  assert render(overlay, radar, timestamp=100.3)['navVehicles'] == []


def test_path_fallback_and_known_outer_boundary():
  m = model()
  m.laneLineProbs = [0.] * 4
  assert confirmed(m=m)['navVehicles'][0]['lane'] == 'left'
  assert confirmed(radar=data([point(lateral=8.)]))['navVehicles'] == []
  m.position.x = []
  assert confirmed(m=m)['navVehicles'] == []


def test_track_reuse_jump_and_clock_rollback_require_new_confirmation():
  overlay = VehicleOverlay()
  confirmed(overlay)
  assert render(overlay, data([point(distance=70.)]), timestamp=100.3)['navVehicles'] == []
  assert render(overlay, data([point(distance=70.)]), timestamp=100.4)['navVehicles']
  assert render(overlay, timestamp=99.)['navVehicles'] == []


def test_bridge_subscribes_and_applies_only_fresh_tracks(monkeypatch):
  clock = NS(now=100.)
  monkeypatch.setattr(bridge.time, 'monotonic', lambda: clock.now)
  sm = Messages()
  sm.data['liveTracks'] = data([point(1, lateral=3.6), point(2, lateral=-3.6)])
  overlay = VehicleOverlay()
  assert 'liveTracks' in bridge.SERVICES
  assert bridge.live_payload(sm, 1, vehicles=overlay)['navVehicles'] == []
  clock.now = 100.2
  sm.recv_time['liveTracks'] = clock.now
  sm.logMonoTime['liveTracks'] = int(clock.now * 1e9)
  result = bridge.live_payload(sm, 2, vehicles=overlay)
  assert len(result['navVehicles']) == 2
  sm.valid['liveTracks'] = False
  result = bridge.live_payload(sm, 3, vehicles=overlay)
  assert result['active'] and result['navVehicles'] == []
  sm.valid['liveTracks'] = True
  sm.delay('liveTracks', .6)
  assert bridge.live_payload(sm, 4, vehicles=overlay)['navVehicles'] == []


def test_smoothing_uses_fresh_measurements_not_render_ticks():
  overlay = VehicleOverlay()
  confirmed(overlay)
  radar = data([point(distance=32.)])
  first = render(overlay, radar, timestamp=100.3)['navVehicles']
  assert first[0]['distanceM'] == 31.1
  assert render(overlay, radar, timestamp=100.3, now=100.4)['navVehicles'] == first


def test_supported_display_stream_is_authoritative_including_empty():
  overlay = VehicleOverlay()
  radar = data([point(1)])
  radar.hasDisplayData = True
  radar.displayPoints = [point(2, lateral=-3.6)]
  cars = confirmed(overlay, radar)['navVehicles']
  assert [car['trackId'] for car in cars] == [2]
  assert cars[0]['lane'] == 'right'
  radar.displayPoints = []
  assert render(overlay, radar, timestamp=100.3)['navVehicles'] == []
  radar.hasDisplayData = False
  assert render(overlay, radar, timestamp=100.4)['navVehicles'] == []
  assert render(overlay, radar, timestamp=100.6)['navVehicles'][0]['trackId'] == 1


@pytest.mark.parametrize('p,expected_count', [
  (point(42, 40., 3.6), 2),  # Same ID in an independent namespace, different lane.
  (point(99, 40., 0.), 1),   # Different ID, matching lead geometry.
  (point(42, 50., 0.), 2),   # Same ID, same lane, but another vehicle.
  (point(99, 40., 0., 10.), 2),  # Similar position, incompatible velocity.
])
def test_display_stream_dedup_uses_geometry_not_lead_id(p, expected_count):
  radar = data([])
  radar.hasDisplayData = True
  radar.displayPoints = [p]
  leads = NS(leadOne=lead())
  result = confirmed(radar=radar, leads=leads)
  assert len(result['navVehicles']) == expected_count
  assert navdy_lead_geometry(model(), leads)['navVehicles'][0] in result['navVehicles']


def test_changing_track_namespace_requires_two_new_samples():
  overlay = VehicleOverlay()
  radar = data([point()])
  assert confirmed(overlay, radar)['navVehicles']
  radar.hasDisplayData = True
  radar.displayPoints = [point()]
  assert render(overlay, radar, timestamp=100.3)['navVehicles'] == []
  assert render(overlay, radar, timestamp=100.4)['navVehicles']
  radar.hasDisplayData = False
  assert render(overlay, radar, timestamp=100.5)['navVehicles'] == []
  assert render(overlay, radar, timestamp=100.6)['navVehicles']
