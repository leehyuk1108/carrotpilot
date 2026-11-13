"""Navdy road drawn as the comma camera sees it (display.navdy_vehicle_view / navdy_model_line, HUD v183 pxPerM)."""
from types import SimpleNamespace as NS

import pytest

from openpilot.selfdrive.navdy.display import (
  VIEW_BOTTOM_Y, VIEW_F, navdy_model_geometry, navdy_vehicle_view, road_depth_at,
)
from openpilot.selfdrive.navdy.tests.test_display import model
from openpilot.selfdrive.navdy.vehicles import merge_long_vehicles


def entry(distance, lateral, lane='right', **extra):
  return dict({'trackId': int(distance * 10), 'distanceM': distance, 'lateralM': lateral, 'lane': lane,
               'source': 'radar'}, **extra)


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
  side_by_side = [entry(10.0, 3.3), entry(11.0, -3.3, lane='left')]
  assert len(merge_long_vehicles(side_by_side)) == 2
  offset = [entry(10.0, 2.0), entry(12.0, 4.0)]                        # 2 m apart sideways
  assert len(merge_long_vehicles(offset)) == 2


def test_short_cluster_stays_a_car_and_a_camera_kind_wins():
  assert merge_long_vehicles([entry(10.0, 3.0), entry(12.0, 3.1)])[0].get('kind') is None
  named = merge_long_vehicles([entry(8.0, 3.0, kind='car'), entry(11.0, 3.0), entry(14.0, 3.0)])
  assert [v.get('kind') for v in named] == ['car']
