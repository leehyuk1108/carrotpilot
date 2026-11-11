import json
from pathlib import Path

import pytest

from openpilot.selfdrive.carrot.carrot_serv import CarrotServ, NAVI_SOURCE_NAVDY
from openpilot.selfdrive.carrot.tests.test_deceleration_candidates import make_serv, publish
from openpilot.selfdrive.navdy.navigation import FeedbackTracker
from openpilot.selfdrive.navdy.tests.test_section import section_packet


def test_actual_phone_json_types_reach_published_section_and_clear(monkeypatch):
  # Synthetic SDK inputs serialized by the production Java SectionRelay helper.
  fixture = Path(__file__).parents[2] / 'navdy/tests/fixtures/section-phone-20261101.jsonl'
  values = [json.loads(line) for line in fixture.read_text().splitlines()]
  serv, sm, pm, sent, tracker, clock = setup(monkeypatch)
  for i, (guidance, kind, source, cap) in enumerate(zip(
      values, ('mobile', 'fixed', 'none'), ('section', 'cam', 'road'), (110, 80, 200), strict=True)):
    clock[0] = 10 + i * .1
    value = section_packet(i + 1)
    value['navigationFeedback']['guidance'] = guidance
    value.update(cameraType=kind, cameraSpeedKph=80, cameraDistance='1 m')
    tracker.accept(value, clock[0], clock[0])
    result = publish(serv, sm, pm, sent)
    assert (result.desiredSource, result.desiredSpeed) == (source, cap)
    assert any(c.source == 'section' for c in result.decelerationCandidates) == (i < 2)


def setup(monkeypatch, mode=2):
  serv, sm, pm, sent = make_serv(monkeypatch, AutoNaviSpeedCtrlMode=mode, NaviSource=1)
  serv.navi_source = NAVI_SOURCE_NAVDY
  serv.external_navigation_active = True
  tracker = FeedbackTracker()
  clock = [10.0]
  serv.navdy_navigation.read = lambda: tracker.snapshot(clock[0])
  serv.navdy_navigation.section_end = lambda: tracker.section_end(clock[0])
  serv.section_clock = lambda: clock[0]
  serv._update_carrot_navi = CarrotServ._update_carrot_navi.__get__(serv)
  return serv, sm, pm, sent, tracker, clock


@pytest.mark.parametrize('limit', [100, 110])
@pytest.mark.parametrize('mode', [0, 1, 2, 3])
def test_section_survives_mobile_replacement_and_filters_only_mobile(monkeypatch, limit, mode):
  serv, sm, pm, sent, tracker, clock = setup(monkeypatch, mode)
  for i, kind in enumerate(('section', 'mobile', 'mobile', 'fixed', 'section')):
    clock[0] = 10 + i * .2
    value = section_packet(i + 1, limitKph=limit)
    value.update(cameraType=kind, cameraSpeedKph=120, cameraDistance='20 m')
    tracker.accept(value, clock[0], clock[0])
    result = publish(serv, sm, pm, sent)
    if mode == 0:
      assert result.desiredSource == 'road' and result.desiredSpeed == 200
    else:
      assert (result.desiredSource, result.desiredSpeed) == ('section', limit)
      assert (result.xSpdType, result.xSpdLimit) == (4, limit)
      assert any(c.source == 'section' for c in result.decelerationCandidates)


def test_lower_fixed_camera_then_return_to_continuous_section(monkeypatch):
  serv, sm, pm, sent, tracker, clock = setup(monkeypatch)
  value = section_packet(limitKph=100)
  value.update(cameraType='fixed', cameraSpeedKph=80, cameraDistance='1 m')
  tracker.accept(value, 10, 10)
  result = publish(serv, sm, pm, sent)
  assert (result.desiredSource, result.desiredSpeed) == ('cam', 80)
  assert {c.source for c in result.decelerationCandidates} == {'section', 'cam'}
  sm['selfdriveState'].distanceTraveled = 10
  clock[0] = 10.1
  result = publish(serv, sm, pm, sent)
  assert (result.desiredSource, result.desiredSpeed) == ('section', 100)
  assert result.xSpdDist == 7889


def test_hazard_updates_never_rewind_the_other_distance(monkeypatch):
  serv, sm, pm, sent, tracker, clock = setup(monkeypatch)
  for i in range(12):
    clock[0] = 10 + i * .1
    value = section_packet(i + 1, limitKph=100)
    value.update(cameraType='mobile' if i % 2 else 'fixed', cameraSpeedKph=120, cameraDistance=f'{300-i} m')
    tracker.accept(value, clock[0], clock[0])
    sm['selfdriveState'].distanceTraveled = i * 3
    result = publish(serv, sm, pm, sent)
    assert result.xSpdDist == 7899 - i * 3
  # Section changes cannot replay the point camera's pre-motion position.
  serv.xSpdDist = 200
  value = section_packet(13, distanceM=7000)
  value.update(cameraType='mobile', cameraSpeedKph=120, cameraDistance='289 m')
  serv.autoNaviSpeedCtrlMode = 3
  tracker.accept(value, 11.2, 11.2)
  clock[0] = 11.2
  publish(serv, sm, pm, sent)
  assert serv.navdy_section_distance == 7000


@pytest.mark.parametrize('distance', [8, 26])
def test_frozen_near_end_distance_cannot_be_rearmed_by_point_updates(monkeypatch, distance):
  serv, sm, pm, sent, tracker, clock = setup(monkeypatch)
  for i in range(15):
    clock[0] = 10 + i * .1
    value = section_packet(i + 1, distanceM=distance)
    value.update(cameraType='mobile', cameraDistance=f'{300-i} m')
    tracker.accept(value, clock[0], clock[0])
    sm['selfdriveState'].distanceTraveled = i * 3
    result = publish(serv, sm, pm, sent)
    assert (result.desiredSource == 'section') == (i * 3 < distance)


def test_section_change_preserves_point_distance_and_equal_caps_prefer_section(monkeypatch):
  serv, sm, pm, sent, tracker, clock = setup(monkeypatch, 3)
  value = section_packet(limitKph=100)
  value.update(cameraType='fixed', cameraSpeedKph=100, cameraDistance='20 m')
  tracker.accept(value, 10, 10)
  assert publish(serv, sm, pm, sent).desiredSource == 'section'
  serv.xSpdDist = 8
  value = section_packet(2, distanceM=7000)
  value.update(cameraType='fixed', cameraSpeedKph=100, cameraDistance='20 m')
  tracker.accept(value, 10.1, 10.1)
  clock[0] = 10.1
  publish(serv, sm, pm, sent)
  assert serv.xSpdDist == 8


def test_heartbeat_only_stale_section_is_held_until_location_backed_absence(monkeypatch):
  # Guidance keeps updating while the section and road-limit observations go stale: the
  # phone lost GPS (tunnel), not the section. The cap is held, not revoked at expiry.
  serv, sm, pm, sent, tracker, clock = setup(monkeypatch)
  for i in range(51):
    clock[0] = 10 + i * .1
    value = section_packet(sequence=1)
    value['navigationFeedback']['sequence'] = i + 1
    value['cameraType'] = 'section'
    tracker.accept(value, clock[0], clock[0])
    result = publish(serv, sm, pm, sent)
    assert (result.desiredSource, result.desiredSpeed) == ('section', 110)
    assert (serv.navdy_section_hold is not None) == (i >= 40)
  # A fresh road limit alone (no device GPS fix) is not enough to release the hold.
  for i in range(51, 120):
    clock[0] = 10 + i * .1
    value = section_packet(sequence=1)
    value['navigationFeedback']['sequence'] = i + 1
    value['roadPreview']['sequence'] = i + 1
    value['cameraType'] = 'section'
    tracker.accept(value, clock[0], clock[0])
    assert publish(serv, sm, pm, sent).desiredSource == 'section'


def test_clear_then_new_observation_is_authoritative_not_old_distance_memory(monkeypatch):
  serv, sm, pm, sent, tracker, clock = setup(monkeypatch)
  tracker.accept(section_packet(), 10, 10)
  publish(serv, sm, pm, sent)
  sm['selfdriveState'].distanceTraveled = 30
  clock[0] = 10.1
  tracker.accept(section_packet(2, active=False), 10.1, 10.1)
  publish(serv, sm, pm, sent)
  clock[0] = 10.2
  tracker.accept(section_packet(3), 10.2, 10.2)
  publish(serv, sm, pm, sent)
  assert serv.navdy_section_distance == 7899


@pytest.mark.parametrize('clear', ['distance', 'offroute', 'stopped'])
def test_section_revocation_reaches_actual_published_cap(monkeypatch, clear):
  serv, sm, pm, sent, tracker, clock = setup(monkeypatch)
  value = section_packet()
  value['cameraType'] = 'section'
  tracker.accept(value, 10, 10)
  assert publish(serv, sm, pm, sent).desiredSource == 'section'
  newer = section_packet(2)
  newer['cameraType'] = 'section'
  if clear == 'offroute':
    newer['navigationFeedback']['guidance']['section']['offRoute'] = True
  elif clear == 'stopped':
    newer['navigationFeedback']['guidance']['active'] = False
  elif clear == 'distance':
    sm['selfdriveState'].distanceTraveled = 7900
  clock[0] = 10.1
  if clear != 'distance':
    tracker.accept(newer, clock[0], clock[0])
  result = publish(serv, sm, pm, sent)
  assert result.desiredSource == 'road' and result.desiredSpeed == 200
  assert not any(c.source == 'section' for c in result.decelerationCandidates)
  assert serv.navdy_section_hold is None


def lose_section(value, how):
  section = value['navigationFeedback']['guidance']['section']
  if how == 'inactive':
    section.update(active=False, limitKph=0, distanceM=0)
  elif how == 'suspended':
    section['suspended'] = True
  elif how == 'expired':
    section['ageMs'] = 4000
  elif how == 'missing':
    del value['navigationFeedback']['guidance']['section']
  return value


def start_section(monkeypatch, distance=5129, limit=80):
  serv, sm, pm, sent, tracker, clock = setup(monkeypatch)
  value = section_packet(1, limitKph=limit, distanceM=distance)
  value['cameraType'] = 'section'
  tracker.accept(value, 10, 10)
  result = publish(serv, sm, pm, sent)
  assert (result.desiredSource, result.desiredSpeed, result.xSpdDist) == ('section', limit, distance)
  return serv, sm, pm, sent, tracker, clock


def drive(serv, sm, pm, sent, tracker, clock, seq, t, travelled, how='inactive', road=False):
  """One frame of the November 6 tunnel pattern: guidance keeps updating, section/road do not."""
  clock[0] = t
  value = lose_section(section_packet(seq), how)
  value['cameraType'] = 'section'
  value['roadPreview']['sequence'] = seq if road else 1
  if not road:
    value['roadPreview']['roadLimitKph'] = None
  tracker.accept(value, t, t)
  sm['selfdriveState'].distanceTraveled = travelled
  return publish(serv, sm, pm, sent)


@pytest.mark.parametrize('how', ['inactive', 'suspended', 'expired', 'missing'])
def test_tunnel_dropout_keeps_section_cap_for_remaining_distance(monkeypatch, how):
  serv, sm, pm, sent, tracker, clock = start_section(monkeypatch)
  # 000001e3 01:08:57: 5129 m left, guidance continues, section and road limit vanish.
  for i in range(1, 200):
    result = drive(serv, sm, pm, sent, tracker, clock, i + 1, 10 + i, i * 22.0, how)
    assert (result.desiredSource, result.desiredSpeed, result.xSpdType) == ('section', 80, 4)
    assert result.xSpdDist == int(5129 - i * 22.0)
    assert any(c.source == 'section' for c in result.decelerationCandidates)
  # Ego motion consumes the remaining distance; the hold then ends by itself.
  result = drive(serv, sm, pm, sent, tracker, clock, 300, 300, 5129 + 1, how)
  assert result.desiredSource == 'road' and serv.navdy_section_hold is None


def test_hold_survives_link_loss_and_fresh_section_takes_over(monkeypatch):
  serv, sm, pm, sent, tracker, clock = start_section(monkeypatch)
  clock[0] = 13.0  # No feedback for longer than the link TTL.
  sm['selfdriveState'].distanceTraveled = 60
  result = publish(serv, sm, pm, sent)
  assert (result.desiredSource, result.desiredSpeed, result.xSpdDist) == ('section', 80, 5069)
  clock[0] = 14.0
  value = section_packet(2, limitKph=100, distanceM=900)
  value['cameraType'] = 'section'
  tracker.accept(value, 14.0, 14.0)
  result = publish(serv, sm, pm, sent)
  assert (result.desiredSource, result.desiredSpeed, result.xSpdDist) == ('section', 100, 900)
  assert serv.navdy_section_hold is None


def test_location_backed_absence_releases_after_confirmation(monkeypatch):
  serv, sm, pm, sent, tracker, clock = start_section(monkeypatch)
  serv.device_gps_fix_since = 0.0
  # Comma GPS fix and a fresh road limit without a section: wait the confirmation time.
  results = [drive(serv, sm, pm, sent, tracker, clock, i + 2, 10.5 + i * .5, i * 10.0, road=True) for i in range(12)]
  sources = [r.desiredSource for r in results]
  held = [s for s in sources if s == 'section']
  assert sources[:len(held)] == held and 6 <= len(held) <= 8 and sources[-1] == 'road'
  assert serv.navdy_section_hold is None


def test_no_device_gps_fix_keeps_hold_even_with_road_limit(monkeypatch):
  serv, sm, pm, sent, tracker, clock = start_section(monkeypatch)
  serv.device_gps_fix_since = None
  for i in range(30):
    assert drive(serv, sm, pm, sent, tracker, clock, i + 2, 10.5 + i * .5, i * 10.0, road=True).desiredSource == 'section'


@pytest.mark.parametrize('end', ['stopped', 'offroute'])
def test_explicit_end_releases_an_active_hold(monkeypatch, end):
  serv, sm, pm, sent, tracker, clock = start_section(monkeypatch)
  assert drive(serv, sm, pm, sent, tracker, clock, 2, 11, 20).desiredSource == 'section'
  assert serv.navdy_section_hold is not None
  clock[0] = 12
  value = section_packet(3)
  value['cameraType'] = 'section'
  if end == 'stopped':
    value['navigationFeedback']['guidance']['active'] = False
  else:
    value['navigationFeedback']['guidance']['section']['offRoute'] = True
  tracker.accept(value, 12, 12)
  result = publish(serv, sm, pm, sent)
  assert result.desiredSource == 'road' and serv.navdy_section_hold is None


def test_hold_time_bound(monkeypatch):
  serv, sm, pm, sent, tracker, clock = start_section(monkeypatch, distance=1000, limit=80)
  assert drive(serv, sm, pm, sent, tracker, clock, 2, 11, 0).desiredSource == 'section'
  bound = serv.navdy_section_hold[2]
  assert bound == pytest.approx(11 + 1000 / (30 / 3.6) * 1.5 + 60)
  assert drive(serv, sm, pm, sent, tracker, clock, 3, bound - .1, 0).desiredSource == 'section'
  assert drive(serv, sm, pm, sent, tracker, clock, 4, bound + .1, 0).desiredSource == 'road'


@pytest.mark.parametrize('distance', [8, 50])
def test_section_lost_near_its_end_is_not_held(monkeypatch, distance):
  serv, sm, pm, sent, tracker, clock = start_section(monkeypatch, distance=distance)
  result = drive(serv, sm, pm, sent, tracker, clock, 2, 11, 0)
  assert result.desiredSource == 'road' and serv.navdy_section_hold is None


def test_disabled_navigation_deceleration_drops_hold(monkeypatch):
  serv, sm, pm, sent, tracker, clock = start_section(monkeypatch)
  assert drive(serv, sm, pm, sent, tracker, clock, 2, 11, 20).desiredSource == 'section'
  serv.autoNaviSpeedCtrlMode = 0
  monkeypatch.setattr(serv, 'update_params', lambda: None)
  result = drive(serv, sm, pm, sent, tracker, clock, 3, 12, 40)
  assert result.desiredSource == 'road' and serv.navdy_section_hold is None


def test_device_gps_fix_time_tracks_continuous_fix(monkeypatch):
  from types import SimpleNamespace as NS
  serv, *_ = setup(monkeypatch)

  class SM(dict):
    def __init__(self, fix):
      super().__init__(gpsLocationExternal=NS(hasFix=fix, latitude=37.46, longitude=126.63, bearingDeg=10., speed=20.),
                       carState=NS(vehicleGps=NS(valid=False)))
      self.updated = {'gpsLocationExternal': True}
      self.valid = {'gpsLocationExternal': True, 'carState': True}
      self.alive = {'gpsLocationExternal': True, 'carState': True}
      self.recv_time = {'gpsLocationExternal': 0.}

  CarrotServ._update_gps(serv, 20., SM(True), 'gpsLocationExternal')
  first = serv.device_gps_fix_since
  assert first is not None
  CarrotServ._update_gps(serv, 20., SM(True), 'gpsLocationExternal')
  assert serv.device_gps_fix_since == first
  CarrotServ._update_gps(serv, 20., SM(False), 'gpsLocationExternal')  # Tunnel: fix lost.
  assert serv.device_gps_fix_since is None


def test_gps_recovery_and_absence_confirm_concurrently(monkeypatch):
  serv, sm, pm, sent, tracker, clock = start_section(monkeypatch)
  assert drive(serv, sm, pm, sent, tracker, clock, 2, 10.5, 10.0).desiredSource == 'section'  # Hold, no road limit.
  serv.device_gps_fix_since = 11.0  # Fix returns together with a fresh road limit and still no section.
  sources = {}
  for i, t in enumerate((11.0, 12.0, 13.0, 13.9, 14.1)):
    sources[t] = drive(serv, sm, pm, sent, tracker, clock, i + 3, t, 20.0 + i, road=True).desiredSource
  assert [sources[t] for t in (11.0, 12.0, 13.0, 13.9)] == ['section'] * 4
  assert sources[14.1] == 'road'


@pytest.mark.parametrize('live', [True, False])
def test_gas_floor_from_lower_curve_cannot_lift_target_above_section(monkeypatch, live):
  serv, sm, pm, sent, tracker, clock = start_section(monkeypatch)
  serv.turnSpeedControlMode = 1
  monkeypatch.setattr(serv, 'update_params', lambda: None)
  sm['carState'].vEgo = 30.0  # 108 km/h, already above the 80 km/h section.

  def frame(seq, t, gas):
    clock[0] = t
    value = section_packet(seq, limitKph=80, distanceM=5000) if live else lose_section(section_packet(seq), 'inactive')
    value['cameraType'] = 'section'
    tracker.accept(value, t, t)
    sm['carState'].gasPressed = gas
    serv.update_navi('', sm, pm, 60., [], [], 300., 'gpsLocationExternal')  # Lower vision-curve target.
    return sent['carrotMan'].carrotMan

  assert (frame(2, 10.5, False).desiredSource, serv.navdy_section_hold is None) == ('vturn', live)
  pressed, released = frame(3, 11.0, True), frame(4, 11.5, False)
  for result in (pressed, released):
    assert result.desiredSpeed <= 80
    assert any(c.source == 'section' for c in result.decelerationCandidates)
