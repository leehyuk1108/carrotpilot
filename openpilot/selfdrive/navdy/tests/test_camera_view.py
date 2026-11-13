"""Navdy road drawn as the comma camera sees it (display.navdy_vehicle_view / navdy_model_line, HUD v183 pxPerM)."""
import math
from types import SimpleNamespace as NS

import pytest

from openpilot.selfdrive.navdy.display import (
  VIEW_BOTTOM_Y, VIEW_F, VIEW_K, bottom_distance, navdy_model_geometry, navdy_model_line, navdy_vehicle_view,
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
  left, right = geometry['navPathLeft'], geometry['navPathRight']
  assert left[1] == left[3] == right[1] == right[3] == VIEW_BOTTOM_Y   # zero-height band on the bottom row
  assert left[0] < 160 < right[0]


def test_truck_side_points_become_one_vehicle_at_its_rear():
  points = [entry(8.7, 3.46), entry(11.1, 2.63), entry(14.0, 2.58)]   # one dump truck (2026-11-13 log)
  merged = merge_long_vehicles(points)
  assert [(v['distanceM'], v['lateralM'], v.get('kind')) for v in merged] == [(8.7, 3.46, 'truck')]


def test_separate_cars_and_other_lanes_stay_separate():
  queue = [entry(10.0, 3.2), entry(16.5, 3.4), entry(23.0, 3.3)]       # a car length plus a gap apart
  assert len(merge_long_vehicles(queue)) == 3
  assert len(merge_long_vehicles([entry(10.0, 3.2), entry(13.6, 3.2)])) == 2   # 3.6 m: not one vehicle
  side_by_side = [entry(10.0, 3.3), entry(11.0, -3.3, lane='left')]
  assert len(merge_long_vehicles(side_by_side)) == 2
  offset = [entry(10.0, 2.0), entry(12.0, 4.0)]                        # 2 m apart sideways
  assert len(merge_long_vehicles(offset)) == 2


def test_short_cluster_stays_a_car_and_a_camera_kind_wins():
  assert merge_long_vehicles([entry(10.0, 3.0), entry(12.0, 3.1)])[0].get('kind') is None
  named = merge_long_vehicles([entry(8.0, 3.0, kind='car'), entry(11.0, 3.0), entry(14.0, 3.0)])
  assert [v.get('kind') for v in named] == ['car']


def test_points_moving_apart_or_across_are_separate_vehicles():
  crossing = [entry(10.0, 2.2, speed=-12.0), entry(13.0, 3.0, speed=12.0)]   # e.g. a motorcycle passing
  assert len(merge_long_vehicles(crossing)) == 2
  assert len(merge_long_vehicles([entry(10.0, 2.2), entry(12.0, 3.6)])) == 2   # 1.4 m sideways


def test_a_run_longer_than_any_vehicle_is_split():
  run = [entry(10.0 + 3.0 * i, 3.2) for i in range(10)]                         # 27 m of points 3 m apart
  merged = merge_long_vehicles(run)
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


def test_short_path_with_a_zero_depth_end_does_not_raise():
  m = model()
  for line in m.laneLines:
    line.z = [math.nan] * len(line.x)
  m.position = NS(x=[0.0, 1.0, 2.0], y=[0.0, 0.0, 0.0], z=[0.0, -0.68, -1.36])
  geometry = navdy_model_geometry(m)
  assert geometry['navPathLeft'][1] == VIEW_BOTTOM_Y


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
