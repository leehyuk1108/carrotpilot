"""Navdy road drawn as the comma camera sees it (display.navdy_vehicle_view / navdy_model_line, HUD v183 pxPerM)."""
import math
from types import SimpleNamespace as NS

import pytest

from openpilot.selfdrive.navdy.display import (
  HIDDEN_BAND, VIEW_BOTTOM_Y, VIEW_F, VIEW_K, bottom_distance, navdy_model_geometry, navdy_model_line, navdy_vehicle_view,
  road_depth_at,
)
from openpilot.selfdrive.navdy.tests.test_display import model
from openpilot.selfdrive.navdy.vehicles import merge_long_vehicles, vehicle_lane


def entry(distance, lateral, lane='right', speed=0.0, **extra):
  return dict({'trackId': int(distance * 10), 'distanceM': distance, 'lateralM': lateral, 'lane': lane,
               'relativeSpeedMps': speed, 'source': 'radar'}, **extra)


def test_vehicle_carries_its_sprite_scale_and_sits_on_the_road_row():
  view = navdy_vehicle_view(model(), 28.48, 2.0)  # 30 m from the camera
  assert view == {'screenX': round(160 + VIEW_F * 2 / 30, 1), 'screenY': round(8 + VIEW_F * 1.36 / 30, 1),
                  'pxPerM': round(VIEW_F / 30, 2)}


def test_vehicle_nearer_than_the_bottom_row_is_held_there_at_that_scale():
  near, bottom = navdy_vehicle_view(model(), 2.0, 0.5), navdy_vehicle_view(model(), 5.0, 0.5)
  assert near['screenY'] == bottom['screenY'] == VIEW_BOTTOM_Y
  assert near == bottom  # both drawn as at the bottom row, never larger than it


def test_vehicle_outside_the_hud_band_is_not_drawn():
  assert navdy_vehicle_view(model(), 10.0, 3.5) is not None
  assert navdy_vehicle_view(model(), 10.0, 20.0) is None
  assert navdy_vehicle_view(model(), float('nan'), 0.0) is None


def test_road_height_follows_the_model_lanes():
  hill = model()
  for line in hill.laneLines:
    line.z = [1.36 - 0.02 * x for x in line.x]   # road rising ahead
  assert road_depth_at(model(), 40.0) == pytest.approx(1.36)
  assert road_depth_at(hill, 40.0) == pytest.approx(1.36 - 0.8)
  flat, rising = navdy_vehicle_view(model(), 38.48, 0.0), navdy_vehicle_view(hill, 38.48, 0.0)
  assert rising['screenY'] < flat['screenY']


def test_stopped_short_path_still_draws_lanes_and_vehicles():
  stopped = model()
  stopped.position = NS(x=[0.0, 1.0, 2.0], y=[0.0, 0.0, 0.0])
  geometry = navdy_model_geometry(stopped)
  assert geometry['navLaneLeft'] and geometry['navLaneRight']
  assert geometry['navPathLeft'] == geometry['navPathRight'] == list(HIDDEN_BAND)   # zero-area band off the view


FAST = 20.0   # m/s ego speed: separate vehicles cannot follow within CLUSTER_GAP_M
TRUCK_SIDE = [(8.7, 3.46), (11.1, 2.63), (14.0, 2.58)]   # one dump truck (2026-11-13 log, ego 10.5 km/h)


def test_truck_side_points_become_one_vehicle_at_its_rear():
  merged = merge_long_vehicles([entry(*p) for p in TRUCK_SIDE], FAST)
  assert [(v['distanceM'], v['lateralM'], v.get('kind')) for v in merged] == [(8.7, 3.46, 'truck')]
  vouched = merge_long_vehicles([entry(*p) for p in TRUCK_SIDE], 10.5 / 3.6, trucks=[(11.5, 3.3)])
  assert [(v['distanceM'], v.get('kind')) for v in vouched] == [(8.7, 'truck')]   # slow, but the camera saw a truck


def test_slow_dense_run_without_a_camera_truck_keeps_every_point():
  queue = [entry(10.0 + 3.0 * i, 3.2) for i in range(4)]   # Codex review: two cars with two points each, say
  assert len(merge_long_vehicles(queue)) == 4                                    # no speed known
  assert len(merge_long_vehicles(queue, 0.0)) == 4                               # stopped
  assert len(merge_long_vehicles(queue, 10.5 / 3.6)) == 4                        # crawling
  assert len(merge_long_vehicles([entry(*p) for p in TRUCK_SIDE], 10.5 / 3.6)) == 3
  passing = [entry(10.0 + 3.0 * i, 3.2, speed=-15.0) for i in range(4)]          # we pass a crawling queue at 72 km/h
  assert len(merge_long_vehicles(passing, FAST)) == 4
  assert len(merge_long_vehicles(queue, FAST)) == 1                              # 3 m bumper gaps at 72 km/h: one body


def test_only_a_camera_truck_inside_the_run_vouches_for_it():
  queue = [entry(10.0 + 3.0 * i, 3.2) for i in range(4)]
  assert len(merge_long_vehicles(queue, 0.0, trucks=[(15.0, -3.3)])) == 4         # truck in the other lane
  assert len(merge_long_vehicles(queue, 0.0, trucks=[(30.0, 3.2)])) == 4          # truck well ahead of the run
  assert len(merge_long_vehicles(queue, 0.0, trucks=[(20.5, 3.4)])) == 1          # truck at its front end


def test_overlay_uses_the_ego_speed_and_the_camera_trucks():
  from openpilot.selfdrive.navdy.tests.test_display import car, payload as make_payload
  from openpilot.selfdrive.navdy.tests.test_vehicles import data, point
  from openpilot.selfdrive.navdy.vehicles import VehicleOverlay
  radar = data([point(i + 1, 10.0 + 3.0 * i, -3.2, 0.0) for i in range(4)])   # radar left-positive: right lane

  def shown(speed, objects=None):
    overlay, m = VehicleOverlay(), model()
    for t in (100.0, 100.2):
      result = make_payload(model=m, car_state=car(vEgo=speed, vEgoCluster=0))
      overlay.update(result, m, radar, timestamp=t, now=t, camera_objects=objects)
    return [v for v in result['navVehicles'] if v['source'] == 'radar']

  assert len(shown(2.0)) == 4
  assert len(shown(20.0)) == 1 and shown(20.0)[0]['kind'] == 'truck'
  assert len(shown(2.0, [{'id': 3, 'kind': 'truck', 'distance': 15.0, 'lateral': 3.2}])) == 1
  assert len(shown(2.0, [{'id': 3, 'kind': 'car', 'distance': 15.0, 'lateral': 3.2}])) == 4


def test_separate_cars_and_other_lanes_stay_separate():
  queue = [entry(10.0, 3.2), entry(16.5, 3.4), entry(23.0, 3.3)]       # a car length plus a gap apart
  assert len(merge_long_vehicles(queue, FAST)) == 3
  assert len(merge_long_vehicles([entry(10.0, 3.2), entry(13.6, 3.2)], FAST)) == 2   # 3.6 m: not one vehicle
  side_by_side = [entry(10.0, 3.3), entry(11.0, -3.3, lane='left')]
  assert len(merge_long_vehicles(side_by_side, FAST)) == 2
  offset = [entry(10.0, 2.0), entry(12.0, 4.0)]                        # 2 m apart sideways
  assert len(merge_long_vehicles(offset, FAST)) == 2


def test_two_close_points_stay_two_and_a_camera_kind_wins():
  assert len(merge_long_vehicles([entry(10.0, 3.2), entry(12.8, 3.2)], FAST)) == 2   # two motorcycles can be that close
  named = merge_long_vehicles([entry(8.0, 3.0, kind='car'), entry(11.0, 3.0), entry(14.0, 3.0)], FAST)
  assert [v.get('kind') for v in named] == ['car']


def test_drifting_speed_or_side_cannot_chain_a_run():
  speeds = [entry(10.0 + 3.0 * i, 3.2, speed=1.4 * i) for i in range(5)]       # 1.4 m/s per step, 5.6 m/s in all
  assert len(merge_long_vehicles(speeds, FAST)) == 5                            # no run of three: nothing merged
  sides = [entry(10.0 + 3.0 * i, 2.0 + 0.7 * i) for i in range(4)]
  assert len(merge_long_vehicles(sides, FAST)) == 4


def test_points_moving_apart_or_across_are_separate_vehicles():
  crossing = [entry(10.0, 2.2, speed=-12.0), entry(13.0, 3.0, speed=12.0)]   # e.g. a motorcycle passing
  assert len(merge_long_vehicles(crossing, FAST)) == 2
  assert len(merge_long_vehicles([entry(10.0, 2.2), entry(12.0, 3.6)], FAST)) == 2   # 1.4 m sideways


def test_a_run_longer_than_any_vehicle_is_split():
  run = [entry(10.0 + 3.0 * i, 3.2) for i in range(10)]                         # 27 m of points 3 m apart
  merged = merge_long_vehicles(run, FAST)
  assert len(merged) == 2 and merged[0]['distanceM'] == 10.0 and merged[0]['kind'] == 'truck'


def sloped(rate):
  m = model()
  for line in m.laneLines:
    line.z = [1.36 + rate * x for x in line.x]
  return m


@pytest.mark.parametrize('rate', [-0.02, 0.06])   # road rising 2 % (depth goes below 0 far out), falling 6 %
def test_vehicle_path_and_lanes_share_the_road_height_on_slopes(rate):
  m = sloped(rate)
  x = 60.0
  vehicle = navdy_vehicle_view(m, x - 1.52, 0.0)
  assert road_depth_at(m, x) == pytest.approx(1.36 + rate * x)          # no flat-road fallback on a real slope
  assert vehicle['screenY'] == pytest.approx(round(8 + VIEW_F * (1.36 + rate * x) / x, 1), abs=0.051)
  geometry = navdy_model_geometry(m)
  left = geometry['navLaneLeft']
  # every lane point sits on the same road height as the vehicle model: v = 8 + F z(x) / x and u = 160 + F y / x
  for u, v in zip(left[::2], left[1::2]):
    xx = VIEW_F * -1.8 / (u - 160)
    assert v == pytest.approx(min(100.0, 8 + VIEW_F * (1.36 + rate * xx) / xx), abs=0.3)


def test_near_vehicle_on_a_slope_sits_where_its_lane_enters_the_view():
  m = sloped(0.10)
  entry_x = bottom_distance(m)
  assert entry_x == pytest.approx(VIEW_K * (1.36 + 0.10 * entry_x), abs=0.01)   # x = K z(x), not K z(near)
  near = navdy_vehicle_view(m, 2.0, 3.6)
  line = navdy_model_line(m.laneLines[0].x, [3.6] * len(m.laneLines[0].x), m.laneLines[0].z)
  assert near['screenY'] == VIEW_BOTTOM_Y and near['screenX'] == pytest.approx(line[0], abs=0.15)
  assert near['pxPerM'] == round(VIEW_F / entry_x, 2)


def tilted():
  m = model()
  m.laneLines[1].z = [1.16] * len(m.laneLines[1].x)
  m.laneLines[2].z = [1.56] * len(m.laneLines[2].x)
  return m


def test_a_tilted_road_is_one_surface_for_lanes_and_vehicles():
  m = tilted()
  right = navdy_model_geometry(m)['navLaneRight']
  u, v = right[-4], right[-3]                                  # a far sample of the right lane line (y 1.8)
  x = VIEW_F * 1.8 / (u - 160)
  vehicle = navdy_vehicle_view(m, x - 1.52, 1.8)
  assert vehicle['screenY'] == pytest.approx(v, abs=0.15)      # the vehicle on the line sits on the line
  near, line = navdy_vehicle_view(m, 2.0, 1.8), right[:2]
  assert near['screenY'] == VIEW_BOTTOM_Y and near['screenX'] == pytest.approx(line[0], abs=0.15)


def test_an_implausible_height_sample_is_dropped_not_interpolated():
  m = model()
  for line in m.laneLines[1:3]:
    line.z = [21.0 if 9.0 < x < 11.0 else 1.36 for x in line.x]   # one sample far beyond a road
  assert road_depth_at(m, 10.0) == pytest.approx(1.36) and road_depth_at(m, 15.0) == pytest.approx(1.36)


def test_the_first_crossing_is_found_even_in_a_short_gap():
  m = model()
  xs, zs = [0, 6, 6.7, 6.9, 7, 10, 20, 80], [2, 2, 1.29, 1.29, 2, 2, 2, 2]
  for line in m.laneLines:
    line.x, line.z = list(map(float, xs)), list(map(float, zs))
    line.y = [line.y[0]] * len(xs)
  line = navdy_model_geometry(m)['navLaneRight']
  near = navdy_vehicle_view(m, 2.0, 1.8)
  assert near['screenX'] == pytest.approx(line[0], abs=0.15) and line[1] == near['screenY'] == VIEW_BOTTOM_Y


def test_bottom_crossing_is_solved_on_the_bent_surface():
  m = model()   # Codex review 2026-11-13: lane positions and heights both change inside one sample segment
  xs = [0.0, 10.0, 20.0, 40.0, 60.0, 80.0]
  m.laneLines[1].x, m.laneLines[1].y, m.laneLines[1].z = xs[:], [-1.8] + [-0.8] * 5, [1.16] + [1.56] * 5
  m.laneLines[2].x, m.laneLines[2].y, m.laneLines[2].z = xs[:], [1.8] + [2.8] * 5, [1.56] + [1.16] * 5
  x = bottom_distance(m, 1.0)
  assert x == pytest.approx(6.92455, abs=2e-3)                     # the chord solution was 7.139 m
  assert abs(x - VIEW_K * road_depth_at(m, x, 1.0)) < 2e-3          # on the surface: no residual
  near = navdy_vehicle_view(m, 2.0, 1.0)
  path_right = navdy_model_geometry(m)['navPathRight']
  assert near['screenY'] == VIEW_BOTTOM_Y == path_right[1]
  assert near['screenX'] == pytest.approx(path_right[0], abs=0.15)  # the near vehicle meets the path edge on the row


def test_short_path_with_a_zero_depth_end_does_not_raise():
  m = model()
  for line in m.laneLines:
    line.z = [math.nan] * len(line.x)
  m.position = NS(x=[0.0, 1.0, 2.0], y=[0.0, 0.0, 0.0], z=[0.0, -0.68, -1.36])
  geometry = navdy_model_geometry(m)   # must not raise; whatever such a bogus plan draws, lanes are still there
  assert geometry['navLaneLeft'] and geometry['navPathLeft']


def test_a_line_that_drops_out_of_view_again_ends_where_it_leaves():
  line = navdy_model_line([0.0, 8.0, 10.0, 20.0], [1.8] * 4, [1.36, 1.36, 2.5, 2.5])
  points = list(zip(line[::2], line[1::2]))
  assert len(points) == 3 and points[0][1] == points[-1][1] == VIEW_BOTTOM_Y   # enter, 8 m, leave
  assert points[1] == pytest.approx((160 + VIEW_F * 1.8 / 8, 8 + VIEW_F * 1.36 / 8), abs=0.051)


def test_lane_is_read_at_the_camera_distance_like_the_drawing():
  m = model()
  for line in m.laneLines:
    line.y = [y + 0.2 * x for x, y in zip(line.x, line.y)]
  # dRel 20 m is 21.52 m from the camera: lanes there are 0.304 m further right than at 20 m
  assert vehicle_lane(m, 20.0, 6.004) == 'center'


def test_road_profile_is_parsed_once_per_message_but_never_stale():
  m = model()
  assert road_depth_at(m, 40.0) == pytest.approx(1.36)
  for line in m.laneLines:                       # the same object now carries another road
    line.z = [1.36 + 0.01 * x for x in line.x]
  assert road_depth_at(m, 40.0) == pytest.approx(1.76)
  other = sloped(-0.01)                          # a different message
  assert road_depth_at(other, 40.0) == pytest.approx(0.96)
  assert road_depth_at(m, 40.0) == pytest.approx(1.76)


def preview_payload(**kwargs):
  from openpilot.selfdrive.navdy.tests.test_display import lead, payload, selfdrive
  return payload(model=model(), radar_state=NS(leadOne=lead(), leadTwo=lead(status=False)), **kwargs)


def test_disengaged_onroad_sends_the_road_without_the_path():
  from openpilot.selfdrive.navdy.tests.test_display import selfdrive
  result = preview_payload(selfdrive_state=selfdrive(enabled=False, active=False, state='disabled'), onroad=True)
  assert result['active'] is False and result['roadPreview'] is True
  assert result['navLaneLeft'] and result['navLaneRight'] and len(result['navVehicles']) == 1
  assert result['navPathLeft'] == result['navPathRight'] == list(HIDDEN_BAND)


def test_engaged_keeps_the_path_and_offroad_sends_no_road():
  from openpilot.selfdrive.navdy.tests.test_display import selfdrive
  engaged = preview_payload(onroad=True)
  assert engaged['active'] and 'roadPreview' not in engaged and engaged['navPathLeft'] != list(HIDDEN_BAND)
  offroad = preview_payload(selfdrive_state=selfdrive(enabled=False, active=False, state='disabled'), onroad=False)
  assert 'roadPreview' not in offroad and not offroad['navVehicles'] and 'navLaneLeft' not in offroad


def test_raw_tracks_and_camera_objects_also_show_in_the_preview():
  from openpilot.selfdrive.navdy.tests.test_display import payload, selfdrive
  from openpilot.selfdrive.navdy.tests.test_vehicles import data, point
  from openpilot.selfdrive.navdy.vehicles import VehicleOverlay, annotate_camera
  m, overlay = model(), VehicleOverlay()
  for timestamp in (100.0, 100.2):
    result = payload(model=m, selfdrive_state=selfdrive(enabled=False, active=False, state='disabled'), onroad=True)
    overlay.update(result, m, data([point(7, 30.0, -3.6)]), timestamp=timestamp, now=timestamp)
  annotate_camera(result, m, [{'id': 5, 'kind': 'ped', 'distance': 20.0, 'lateral': 3.0}], None)
  assert {v['trackId'] for v in result['navVehicles']} == {7, 900005}


def test_no_preview_on_stale_car_or_selfdrive_state():
  from openpilot.selfdrive.navdy.tests.test_display import selfdrive
  stale_car = preview_payload(selfdrive_state=selfdrive(enabled=False, active=False, state='disabled'), onroad=True,
                              car_state=None)
  stale_selfdrive = preview_payload(selfdrive_state=None, onroad=True)
  for result in (stale_car, stale_selfdrive):
    assert 'roadPreview' not in result and 'navPathLeft' not in result and result['navVehicles'] == []
