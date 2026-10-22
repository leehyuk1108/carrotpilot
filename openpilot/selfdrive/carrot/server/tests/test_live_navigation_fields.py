import ast
from pathlib import Path

from openpilot.cereal import custom
from openpilot.selfdrive.carrot.server.live_runtime.snapshot import _build_carrot


def select_live_services(snapshot):
  path = Path(__file__).resolve().parents[1] / 'features/system.py'
  tree = ast.parse(path.read_text())
  nodes = [node for node in tree.body if (
    isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == '_LIVE_RUNTIME_SERVICE_NAMES' for t in node.targets)
    or isinstance(node, ast.FunctionDef) and node.name == '_select_live_runtime_services')]
  scope = {'Any': object}
  exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), 'exec'), scope)
  return scope['_select_live_runtime_services'](snapshot)


def test_web_runtime_includes_navigation_status_and_traffic():
  msg = custom.CarrotMan.new_message(
    externalNavigationSource='navdy', externalNavigationActive=True,
    routeTrafficTotalMeters=500, routeTrafficSegments=[{'endM': 500, 'status': 'O'}])
  snapshot = {'services': {'carrotMan': _build_carrot(msg), 'navRoute': {},
                           'navInstructionCarrot': {}, 'carState': {'excluded': True}}}
  services = select_live_services(snapshot)
  assert set(services) == {'carrotMan', 'navRoute', 'navInstructionCarrot'}
  assert services['carrotMan']['externalNavigationActive'] is True
  assert services['carrotMan']['routeTrafficSegments'] == [{'endM': 500, 'status': 'O'}]


def test_web_runtime_omits_dead_service():
  assert select_live_services({'services': {'carrotMan': None}}) == {}
  assert select_live_services({}) == {}
