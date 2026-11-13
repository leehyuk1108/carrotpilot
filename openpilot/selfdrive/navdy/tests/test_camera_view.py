"""Navdy road drawn as the comma camera sees it (display.navdy_vehicle_view / navdy_model_line, HUD v183 pxPerM)."""
import math
from types import SimpleNamespace as NS

import pytest

from openpilot.selfdrive.navdy.display import (
  HIDDEN_BAND, VIEW_BOTTOM_Y, VIEW_F, VIEW_K, _profile, bottom_distance, navdy_model_geometry, navdy_model_line,
  navdy_vehicle_view, road_depth_at,
)
from openpilot.selfdrive.navdy.tests.test_display import model
from openpilot.selfdrive.navdy.vehicles import vehicle_lane


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


def test_every_radar_point_of_a_dense_run_is_drawn():
  # A truck's side and a dense queue look alike to the radar (Codex review 2026-11-13): nothing is merged or dropped.
  from openpilot.selfdrive.navdy.tests.test_display import car, payload as make_payload
  from openpilot.selfdrive.navdy.tests.test_vehicles import data, point
  from openpilot.selfdrive.navdy.vehicles import VehicleOverlay
  radar = data([point(i + 1, 10.0 + 3.0 * i, -3.2, 0.0) for i in range(6)])   # radar left-positive: right lane
  for speed in (0.0, 20.0):
    overlay, m = VehicleOverlay(), model()
    for t in (100.0, 100.2):
      result = make_payload(model=m, car_state=car(vEgo=speed, vEgoCluster=0))
      overlay.update(result, m, radar, timestamp=t, now=t)
    radar_entries = [v for v in result['navVehicles'] if v['source'] == 'radar']
    assert sorted(v['trackId'] for v in radar_entries) == [1, 2, 3, 4, 5, 6]
    assert all('kind' not in v for v in radar_entries)


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


def first_visible(f, lo, hi):
  assert f(lo) < 0 <= f(hi)
  for _ in range(80):
    mid = (lo + hi) / 2
    lo, hi = (lo, mid) if f(mid) >= 0 else (mid, hi)
  return hi


def test_a_visible_stretch_between_two_hidden_samples_is_the_first_crossing():
  # Codex review r3: the lateral mix alone makes the road visible between 6 and 8 m while both samples are hidden
  xs = [0.0, 6.0, 8.0, 20.0, 80.0]
  mean6, mean8 = (6.0 + 0.01) / VIEW_K, (8.0 + 0.01) / VIEW_K
  left = NS(x=xs, y=[-0.8, -0.8, -0.4, -0.4, -0.4], z=[1.36, mean6 - 0.14, mean8, 1.36, 1.36])
  right = NS(x=xs, y=[2.8, 2.8, 3.2, 3.2, 3.2], z=[1.36, mean6 + 0.14, mean8, 1.36, 1.36])
  m = NS(position=NS(x=xs, y=[0.0] * 5), laneLines=[left, left, right, right], laneLineProbs=[0.9] * 4)
  f = lambda x: x - VIEW_K * road_depth_at(m, x, 1.0)
  assert f(6.0) < 0 and f(8.0) < 0 and f(7.0) > 0
  first = first_visible(f, 6.0, 7.0)
  assert bottom_distance(m, 1.0) == pytest.approx(first, abs=1e-6)   # 6.134 m, not the next crossing at 8.009 m
  near, path_right = navdy_vehicle_view(m, 2.0, 1.0), navdy_model_geometry(m)['navPathRight']
  assert near == {'screenX': round(160 + VIEW_F / first, 1), 'screenY': VIEW_BOTTOM_Y, 'pxPerM': round(VIEW_F / first, 2)}
  assert (near['screenX'], near['screenY']) == (path_right[0], path_right[1])


def test_a_sparse_path_finds_the_short_visible_gap_like_a_vehicle():
  xs, zs = [0.0, 6.0, 6.7, 6.9, 7.0, 10.0, 20.0, 80.0], [2.0, 2.0, 1.29, 1.29, 2.0, 2.0, 2.0, 2.0]
  lanes = [NS(x=xs, y=[y] * len(xs), z=zs) for y in (-5.4, -1.8, 1.8, 5.4)]
  m = NS(position=NS(x=[0.0, 80.0], y=[0.0, 0.0]), laneLines=lanes, laneLineProbs=[0.9] * 4)   # two path samples
  near, path_right = navdy_vehicle_view(m, 2.0, 1.0), navdy_model_geometry(m)['navPathRight']
  assert (near['screenX'], near['screenY']) == (path_right[0], path_right[1])   # both at the gap, not 24.7 px apart
  assert near['screenY'] == VIEW_BOTTOM_Y


def test_a_dip_inside_one_path_segment_keeps_the_band_area():
  # Codex review r4: entry and exit inside the one sparse path segment left the band flat on the bottom row
  xs, zs = [0.0, 6.0, 6.7, 6.9, 7.0, 10.0, 20.0, 80.0], [2.0, 2.0, 1.29, 1.29, 2.0, 2.0, 2.0, 2.0]
  lanes = [NS(x=xs, y=[y] * len(xs), z=zs) for y in (-5.4, -1.8, 1.8, 5.4)]
  sparse = NS(position=NS(x=[0.0, 80.0], y=[0.0, 0.0]), laneLines=lanes, laneLineProbs=[0.9] * 4)
  dense = NS(position=NS(x=xs[:], y=[0.0] * len(xs)), laneLines=lanes, laneLineProbs=[0.9] * 4)   # same straight path
  a, b = navdy_model_geometry(sparse), navdy_model_geometry(dense)
  for key in ('navPathLeft', 'navPathRight'):
    assert a[key] == b[key]                                  # the drawing does not depend on the path's sampling
    assert min(a[key][1::2]) < VIEW_BOTTOM_Y - 3.0           # the band keeps its area (the dip shows)
  assert min(a['navPathRight'][1::2]) == pytest.approx(min(a['navLaneRight'][1::2]), abs=1.0)


def test_a_slanted_segment_starting_on_hidden_road_keeps_its_line():
  m = model()   # one long path segment from 0 m: the search skips the hidden near road but stays on the same line
  m.position = NS(x=[0.0, 80.0], y=[0.0, 4.0])
  right = navdy_model_geometry(m)['navPathRight']
  x = VIEW_K * 1.36
  assert (right[0], right[1]) == (round(160 + VIEW_F * (1.0 + 0.05 * x) / x, 1), VIEW_BOTTOM_Y)
  assert len(right) == 4 and right[2:] == [round(160 + VIEW_F * 5.0 / 80, 1), round(8 + VIEW_F * 1.36 / 80, 1)]


@pytest.mark.parametrize('lateral', [1.04, 3.04])
def test_a_near_vehicle_meets_the_line_at_its_exact_lateral(lateral):
  xs = [0.0, 10.0, 20.0, 40.0, 60.0, 80.0]   # Codex review r3: a banked road and a lateral between 0.1 m steps
  left, right = NS(x=xs, y=[-1.8] * 6, z=[1.46] * 6), NS(x=xs, y=[1.8] * 6, z=[1.26] * 6)
  m = NS(position=NS(x=xs, y=[0.0] * 6), laneLines=[left, left, right, right], laneLineProbs=[0.9] * 4)
  x = bottom_distance(m, lateral)
  assert x == pytest.approx(VIEW_K * road_depth_at(m, x, lateral), abs=1e-6)
  line = navdy_model_line(xs, [lateral] * 6, surface=_profile(m))
  near = navdy_vehicle_view(m, 2.0, lateral)
  assert (near['screenX'], near['screenY']) == (line[0], line[1]) == (line[0], VIEW_BOTTOM_Y)


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
