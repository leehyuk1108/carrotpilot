import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock  # noqa: TID251 -- mocks only; tests use pytest

import pytest

from openpilot.selfdrive.carrot.tests.test_navigation_source import services as services
from openpilot.selfdrive.navdy.navigation import FeedbackTracker
from openpilot.selfdrive.navdy.tests.test_navigation import packet


@pytest.fixture
def navigation_publication(services):
  cls, values = services
  values.update(CarrotNaviSource=1, AutoNaviSpeedCtrlMode=2, AutoNaviSpeedSafetyFactor=100,
                AutoNaviSpeedBumpSpeed=35, AutoNaviSpeedDecelRate=135, AutoRoadSpeedLimitOffset=-1)
  svc = cls()
  svc.params_memory = Mock()
  svc.navdy_navigation = Mock()
  svc._update_gps = Mock(return_value=0.)
  svc._publish_model_route = Mock()
  svc._stamp_model_instruction = Mock()
  svc.update_nav_instruction = Mock()
  svc.update_auto_turn = Mock(return_value=(250., 'none', 250., 0.))
  scope = cls.update_navi.__globals__
  scope['messaging'] = SimpleNamespace(new_message=lambda service, **kw:
                                      SimpleNamespace(**kw, **{service: SimpleNamespace()}))
  sm = SimpleNamespace(alive={'carState': False, 'selfdriveState': False})
  sent = {}
  publisher = SimpleNamespace(send=lambda name, msg: sent.update({name: msg}))
  tracker = FeedbackTracker()

  def apply(value, now):
    assert tracker.accept(value, now, now)
    svc.navdy_navigation.read.return_value = tracker.snapshot(now)
    svc.update_navi('old-network-host', sm, publisher, 200., [], [], 200., 'gpsLocationExternal')
    return sent['carrotMan'].carrotMan, sent['navInstructionCarrot'].navInstructionCarrot

  return svc, values, apply


def test_live_camera_and_bump_remain_separate(navigation_publication):
  svc, _, apply = navigation_publication
  value = packet()
  value['cameraSpeedKph'], value['cameraDistance'] = 30, '112 m'
  value['navigationFeedback']['guidance']['sdi'] = {'type': 22, 'distanceMeters': 96}
  cm, _ = apply(value, 10)
  assert (svc.nSdiPlusType, svc.nSdiPlusDist) == (22, 96)
  assert (cm.xSpdType, cm.xSpdDist, cm.xSpdLimit, cm.desiredSource) == (1, 112, 30, 'cam')


def test_bump_only_has_correct_description(navigation_publication):
  svc, _, apply = navigation_publication
  value = packet()
  value['cameraSpeedKph'] = 0
  value['navigationFeedback']['guidance']['sdi'] = {'type': 22, 'distanceMeters': 96}
  cm, _ = apply(value, 10)
  assert (cm.xSpdType, cm.xSpdDist, cm.xSpdLimit) == (22, 96, 35)
  assert cm.szSdiDescr == svc._get_sdi_descr(22)
  assert cm.szSdiDescr


@pytest.mark.parametrize('clear', ['null', 'invalid', 'expired', 'disconnected'])
def test_navdy_expired_limit_is_not_published_to_cruise_or_ui(navigation_publication, clear):
  svc, _, apply = navigation_publication
  assert apply(packet(), 10)[0].nRoadLimitSpeed == 60
  value = packet(sequence=2)
  now = 11
  if clear == 'null':
    value['roadPreview'] = None
  elif clear == 'invalid':
    value['roadPreview']['roadLimitKph'] = 0
  elif clear == 'expired':
    value['roadPreview']['sequence'] = 1
    now = 14
  else:
    value = {'navigationFeedback': None, 'roadPreview': None}
  cm, inst = apply(value, now)
  assert not svc.carrot_navi_road_limit_valid
  assert cm.nRoadLimitSpeed == 0
  if hasattr(inst, 'speedLimit'):
    assert inst.speedLimit == 0


def test_navdy_connection_flag_uses_accepted_data_not_old_remote(navigation_publication):
  _, _, apply = navigation_publication
  cm, _ = apply(packet(), 10)
  assert (cm.externalNavigationSource, cm.externalNavigationActive) == ('navdy', True)
  cm, _ = apply({'navigationFeedback': None, 'roadPreview': None}, 11)
  assert (cm.externalNavigationSource, cm.externalNavigationActive) == ('navdy', False)


def test_traffic_segments_publish_then_clear_with_guidance(navigation_publication):
  _, _, apply = navigation_publication
  value = packet()
  value['navigationFeedback']['guidance'].update(
    trafficTotalMeters=1500, trafficSegments=[{'endM': 500, 'status': 'G'}, {'endM': 1500, 'status': 'R'}])
  cm, _ = apply(value, 10)
  assert cm.routeTrafficTotalMeters == 1500
  assert cm.routeTrafficSegments == [{'endM': 500, 'status': 'G'}, {'endM': 1500, 'status': 'R'}]
  cm, _ = apply(packet(sequence=2, active=False), 11)
  assert cm.routeTrafficSegments == [] and cm.routeTrafficTotalMeters == 0


def test_traffic_schema_roundtrip_and_web_clear():
  from openpilot.cereal import custom
  from openpilot.selfdrive.carrot.server.live_runtime.snapshot import _build_carrot

  msg = custom.CarrotMan.new_message(
    externalNavigationSource='navdy', externalNavigationActive=True,
    routeTrafficTotalMeters=1500,
    routeTrafficSegments=[{'endM': 500, 'status': 'G'}, {'endM': 1500, 'status': 'R'}])
  with custom.CarrotMan.from_bytes(msg.to_bytes()) as decoded:
    payload = _build_carrot(decoded)
  assert payload['externalNavigationSource'] == 'navdy'
  assert payload['externalNavigationActive'] is True
  assert payload['routeTrafficTotalMeters'] == 1500
  assert payload['routeTrafficSegments'] == [{'endM': 500, 'status': 'G'}, {'endM': 1500, 'status': 'R'}]
  cleared = _build_carrot(custom.CarrotMan.new_message(), previous=payload)
  assert cleared['routeTrafficSegments'] == [] and cleared['routeTrafficTotalMeters'] == 0
  assert cleared['externalNavigationActive'] is False


@pytest.fixture
def cruise_auto_speed():
  path = Path(__file__).resolve().parents[2] / 'car/cruise.py'
  tree = ast.parse(path.read_text())
  cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'VCruiseCarrot')
  method = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == '_auto_speed_up')
  scope = {}
  exec(compile(ast.Module(body=[method], type_ignores=[]), str(path), 'exec'), scope)
  return scope['_auto_speed_up']


@pytest.mark.parametrize('model_speed_factor', [-1., 0., 1.])
@pytest.mark.parametrize('road_adjust', [-1., 0., 0.5])
def test_unknown_road_limit_keeps_cruise_setting(cruise_auto_speed, model_speed_factor, road_adjust):
  state = SimpleNamespace(nRoadLimitSpeed=0, _pause_auto_speed_up=False,
                          applyModelSpeed=model_speed_factor, model_v_kph=80.,
                          autoSpeedUptoRoadSpeedLimit=1., autoRoadSpeedAdjust=road_adjust)
  assert cruise_auto_speed(state, 65.) == 65.


@pytest.mark.parametrize(('factor', 'expected'), [(-1., 66.), (0., 65.), (1., 66.)])
def test_valid_road_limit_keeps_existing_model_speed_behavior(cruise_auto_speed, factor, expected):
  state = SimpleNamespace(nRoadLimitSpeed=60, nRoadLimitSpeed_last=60, _pause_auto_speed_up=False,
                          applyModelSpeed=factor, model_v_kph=80., autoSpeedUptoRoadSpeedLimit=1.,
                          autoRoadSpeedAdjust=0., autoRoadSpeedLimitOffset=-1, v_lead_kph=0., d_rel=100.)
  assert cruise_auto_speed(state, 65.) == pytest.approx(expected)
