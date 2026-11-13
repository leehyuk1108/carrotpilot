import copy
import math

import pytest

from openpilot.selfdrive.navdy import sprite_clearance as sc
from openpilot.selfdrive.navdy.display import navdy_project_point
from openpilot.selfdrive.navdy.tests.test_display import model
from openpilot.selfdrive.navdy.vehicles import vehicle_yaw


def car(lateral, distance=30.0, lane=None, kind=None, **extra):
  lane = lane or ('left' if lateral < -1.8 else 'right' if lateral > 1.8 else 'center')
  m = model()
  screen = navdy_project_point(distance, lateral, 0.0)
  v = {'trackId': 7, 'screenX': screen[0], 'screenY': screen[1], 'yawDeg': vehicle_yaw(m, lane, distance, lateral),
       'distanceM': distance, 'lateralM': lateral, 'lane': lane, 'source': 'radar'}
  if kind:
    v['kind'] = kind
  return dict(v, **extra)


def settled(*vehicles, m=None):
  payload = {'navVehicles': [copy.deepcopy(v) for v in vehicles]}
  sc.settle_adjacent(payload, model() if m is None else m)
  return payload['navVehicles']


def drawn_out(v, line=-1.8):
  """Distance of the drawn position outside our line, from screenX (lanes in test model are straight)."""
  scale = sc.lateral_scale(v['distanceM'])
  x_line = navdy_project_point(v['distanceM'], line, 0.0)[0]
  return abs(v['screenX'] - x_line) / scale


def test_the_hud_draws_a_car_wider_than_it_is():
  # Half a 1.9 m car is 0.95 m; the v180 sprite reaches about 1.3 m at these ranges.
  for distance in (10.0, 30.0, 50.0):
    assert sc.inner_extent_m('car', 0.0, distance, 'left') > 1.15
  # The yawed adjacent-lane frame reaches further toward the centre.
  assert sc.inner_extent_m('car', -4.0, 30.0, 'left') > sc.inner_extent_m('car', 0.0, 30.0, 'left')
  assert sc.inner_extent_m('car', 4.0, 30.0, 'right') > sc.inner_extent_m('car', 0.0, 30.0, 'right')


def test_frame_index_matches_java_round_half_up():
  assert [sc.frame_index(y) for y in (-30, -24, -1.0, 0, 1.0, 3.0, 24, math.nan)] == [0, 0, 12, 12, 13, 14, 24, 12]


@pytest.mark.parametrize('side,lateral', [('left', -3.3), ('right', 3.3)])
def test_car_outside_our_lane_is_drawn_outside_it(side, lateral):
  measured = car(lateral)
  out = settled(measured)[0]
  line = -1.8 if side == 'left' else 1.8
  required = sc.inner_extent_m(None, out['yawDeg'], 30.0, side) + sc.CLEAR_M
  assert drawn_out(out, line) == pytest.approx(required, abs=0.02)
  assert (out['screenX'] < measured['screenX']) if side == 'left' else (out['screenX'] > measured['screenX'])
  assert out['lateralM'] == lateral and out['lane'] == side and out['screenY'] == measured['screenY']


def test_straddling_car_and_own_lane_cars_are_drawn_as_measured():
  straddling, own, far = car(-2.25), car(0.4), car(-5.0)  # 0.45 m outside the line; own lane; well clear already
  assert settled(straddling, own, far) == [straddling, own, far]


def test_position_never_jumps_while_a_car_drifts_out():
  previous = None
  for step in range(0, 121):
    lateral = -1.8 - 0.3 - step * 0.01          # 0.3 m .. 1.5 m outside the line
    out = drawn_out(settled(car(lateral))[0])
    if previous is not None:
      assert out >= previous - 0.03            # monotonic within a sub-pixel (~0.75 px; the rounded yaw feeds back)
      assert out - previous <= 0.05            # no jump: under ~1 canvas px per 1 cm (a frame step would be ~0.1 m)
    previous = out


def test_kind_sets_the_reach():
  truck = settled(car(-3.0, kind='truck'))[0]
  moto = settled(car(-3.0, kind='moto'))[0]
  assert drawn_out(truck) != drawn_out(moto)


def test_undrawn_or_missing_line_leaves_it_alone():
  m = model()
  m.laneLineProbs = [0.8, 0.1, 0.95, 0.7]      # our left line not drawn
  v = car(-3.0)
  assert settled(v, m=m) == [v]
  from types import SimpleNamespace as NS
  short = model()
  short.laneLines[1] = NS(x=[0.0, 20.0, 40.0], y=[-1.8] * 3)  # our line ends before the car
  v = car(-3.0, distance=60.0)
  assert settled(v, m=short) == [v]
  assert sc.settle_adjacent({'navVehicles': []}, None) is None


def test_bridge_settles_after_every_vehicle_is_known(monkeypatch):
  from types import SimpleNamespace as NS
  from openpilot.selfdrive.navdy import bridge
  from openpilot.selfdrive.navdy.tests.test_bridge import Messages
  from openpilot.selfdrive.navdy.tests.test_vehicles import data, point
  from openpilot.selfdrive.navdy.vehicles import VehicleOverlay
  clock = NS(now=100.)
  monkeypatch.setattr(bridge.time, 'monotonic', lambda: clock.now)
  sm = Messages()
  sm.data['liveTracks'] = data([point(7, distance=30., lateral=3.0)])  # left-positive: 3.0 m left
  overlay = VehicleOverlay()
  bridge.live_payload(sm, 1, vehicles=overlay)
  clock.now = 100.2
  sm.recv_time['liveTracks'] = clock.now
  sm.logMonoTime['liveTracks'] = int(clock.now * 1e9)
  v = bridge.live_payload(sm, 2, vehicles=overlay)['navVehicles'][0]
  assert v['lane'] == 'left' and v['lateralM'] == -3.0
  assert v['screenX'] < navdy_project_point(30.0, -3.0, 0.0)[0]


def test_reach_interpolates_between_frames_and_handles_bad_yaw():
  at_frame = sc.inner_extent_m('car', -4.0, 30.0, 'left')
  between = sc.inner_extent_m('car', -3.0, 30.0, 'left')
  assert sc.inner_extent_m('car', -2.0, 30.0, 'left') < between < at_frame
  assert sc.inner_extent_m('car', math.nan, 30.0, 'left') == sc.inner_extent_m('car', 0.0, 30.0, 'left')
  assert sc.inner_extent_m('car', 99.0, 30.0, 'right') == sc.inner_extent_m('car', 24.0, 30.0, 'right')
