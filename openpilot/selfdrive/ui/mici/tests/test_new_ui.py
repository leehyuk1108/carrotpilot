import ast
import importlib.util
import sys
from collections import namedtuple
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

ONROAD = Path(__file__).parents[1] / 'onroad'


@pytest.fixture
def scene(monkeypatch):
  calls = []
  now = [10.]
  class Filter:
    def __init__(self, x, *_args):
      self.x = x
    def update(self, x):
      self.x = x
      return x
  class Torque:
    def __init__(self):
      self._torque_filter = Filter(0)
      self._torque_line_alpha_filter = Filter(0)
    def render(self, rect):
      calls.append(('torque', rect))
  rl = NS(Rectangle=namedtuple('Rectangle', 'x y width height'), Vector2=namedtuple('Vector2', 'x y'),
          Color=namedtuple('Color', 'r g b a'), WHITE='white', BLANK='blank', get_time=lambda: now[0])
  for name in ('draw_texture_pro', 'draw_texture_ex', 'draw_text_ex', 'draw_circle_gradient'):
    setattr(rl, name, lambda *args, name=name: calls.append((name, *args)))
  class SM(dict):
    valid = dict.fromkeys(('carState', 'selfdriveState', 'carControl'), True)
    alive = valid.copy()
    recv_frame = dict.fromkeys(valid, 101)
  cs = NS(leftBlindspot=False, rightBlindspot=False, steeringAngleDeg=5)
  sm = SM(carState=cs, carControl=NS(latActive=False))
  ui = NS(started=True, started_frame=100, status='engaged', is_metric=True, sm=sm, new_ui=True)
  gui = NS(target_fps=20, texture=lambda name, w, h: NS(name=name, width=w, height=h))
  for name, value in {
    'pyray': rl,
    'openpilot.common.filter_simple': NS(FirstOrderFilter=Filter),
    'openpilot.selfdrive.ui.ui_state': NS(ui_state=ui, UIStatus=NS(DISENGAGED='disengaged')),
    'openpilot.system.ui.lib.application': NS(gui_app=gui),
    'openpilot.system.ui.lib.multilang': NS(tr=lambda text: text),
    'openpilot.system.ui.lib.text_measure': NS(measure_text_cached=lambda _, text, size: NS(x=len(text) * size * .6)),
    'openpilot.selfdrive.ui.mici.onroad.torque_bar': NS(TorqueBar=Torque),
  }.items():
    monkeypatch.setitem(sys.modules, name, value)
  spec = importlib.util.spec_from_file_location('_new_ui_test', ONROAD / 'wayon_hud.py')
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  hud = NS(_can_draw_top_icons=True, _status_timer_visible=False, _show_wheel_critical=False,
           is_cruise_set=True, _engaged=True, set_speed=100, _set_speed_changed_time=10,
           _font_display='display', _font_semi_bold='semi', _turn_intent=NS(render=lambda rect: calls.append(('turn', rect))))
  return NS(renderer=module.WayonHud(), module=module, ui=ui, sm=sm, cs=cs, hud=hud, calls=calls, now=now,
            rect=rl.Rectangle(0, 0, 476, 240))


def texts(scene):
  return [call[2] for call in scene.calls if call[0] == 'draw_text_ex']


def test_max_is_temporary_and_uses_real_selected_speed(scene):
  s = scene
  s.renderer.draw(s.rect, s.hud)
  assert texts(s) == ['100', 'MAX']
  assert s.renderer.drawing_top_icons()
  s.now[0] += 2.6
  s.calls.clear()
  s.renderer.draw(s.rect, s.hud)
  assert not texts(s) and not s.renderer.drawing_top_icons()
  assert any(call[0] == 'torque' for call in s.calls)
  s.hud.set_speed = 120
  s.hud._set_speed_changed_time = s.now[0]
  s.ui.is_metric = False
  s.renderer.draw(s.rect, s.hud)
  assert texts(s) == ['75', 'MAX']


@pytest.mark.parametrize('condition', ['warning', 'timer', 'stale', 'old_session', 'offroad'])
def test_warning_timer_and_bad_inputs_clear_max_immediately(scene, condition):
  s = scene
  s.renderer.draw(s.rect, s.hud)
  s.calls.clear()
  if condition == 'warning':
    s.hud._can_draw_top_icons = False
  elif condition == 'timer':
    s.hud._status_timer_visible = True
  elif condition == 'stale':
    s.sm.alive = dict(s.sm.alive, carState=False)
  elif condition == 'old_session':
    s.ui.started_frame = 102
  else:
    s.ui.started = False
  s.renderer.draw(s.rect, s.hud)
  assert not texts(s) and not s.renderer.drawing_top_icons()
  assert any(call[0] == 'torque' for call in s.calls) == (condition == 'warning')


def test_bsd_hides_ordinary_wheel_but_not_critical_wheel(scene):
  s = scene
  s.cs.leftBlindspot = True
  s.renderer.draw(s.rect, s.hud)
  assert next(call for call in s.calls if call[0] == 'draw_texture_pro')[-1].a == 0
  s.calls.clear()
  s.hud._show_wheel_critical = True
  s.renderer.draw(s.rect, s.hud)
  wheel = next(call for call in s.calls if call[0] == 'draw_texture_pro')
  assert wheel[1].name.endswith('wheel_critical.png') and wheel[-1].a == 255
  assert any(call[0] == 'draw_texture_ex' for call in s.calls)


def test_disengaged_and_reset_remove_old_presentation(scene):
  s = scene
  s.hud._engaged = False
  s.ui.status = 'disengaged'
  s.renderer.draw(s.rect, s.hud)
  assert not texts(s)
  s.renderer._speed_alpha.x = 1
  s.renderer._torque_bar._torque_filter.x = .8
  s.renderer.reset()
  assert not s.renderer.drawing_top_icons()
  assert s.renderer._torque_bar._torque_filter.x == 0


@pytest.mark.parametrize('blindspot', [False, True])
def test_lateral_only_keeps_green_wheel_and_torque_even_with_bsd(scene, blindspot):
  s = scene
  s.ui.status = 'disengaged'
  s.hud._engaged = False
  s.sm['carControl'].latActive = True
  s.cs.leftBlindspot = blindspot
  s.renderer.draw(s.rect, s.hud)
  color = next(call for call in s.calls if call[0] == 'draw_texture_pro')[-1]
  assert color.r == 0 and color.g == 255 and color.a > 0
  assert s.renderer._torque_bar.lateral_active
  assert not texts(s)
  s.sm.alive = dict(s.sm.alive, carControl=False)
  s.calls.clear()
  s.renderer.draw(s.rect, s.hud)
  assert not s.renderer._torque_bar.lateral_active
  assert next(call for call in s.calls if call[0] == 'draw_texture_pro')[-1].a == 0


def test_lane_change_alert_keeps_turn_intent_below_alert(scene):
  s = scene
  s.hud._can_draw_top_icons = False
  s.renderer.draw(s.rect, s.hud)
  assert any(call[0] == 'turn' for call in s.calls)
  assert not texts(s)


def test_real_hud_dispatch_reuses_one_wayon_instance_and_restores_legacy(scene, monkeypatch):
  s = scene
  tree = ast.parse((ONROAD / 'hud_renderer.py').read_text())
  klass = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'HudRenderer')
  method = next(node for node in klass.body if isinstance(node, ast.FunctionDef) and node.name == '_render')
  namespace = {'rl': s.module.rl, 'ui_state': s.ui}
  exec(compile(ast.Module(body=[method], type_ignores=[]), str(ONROAD / 'hud_renderer.py'), 'exec'), namespace)
  allocations, draws = [], []
  def create():
    allocations.append(True)
    return s.renderer
  monkeypatch.setitem(sys.modules, 'openpilot.selfdrive.ui.mici.onroad.wayon_hud', NS(WayonHud=create))
  h = s.hud
  h._new_ui_active, h._wayon_hud, h._overlay_state = False, None, None
  h._traffic_light_info = None
  h._overlay = NS(draw=lambda *a, **kw: draws.append('legacy'), invalidate=lambda: draws.append('invalidate'))
  h._paint = lambda rect: None
  h._draw_egpu_badge = lambda rect: draws.append('egpu')
  h._draw_traffic_light_info = lambda *args: None
  h._draw_cruise_speed_animation = lambda rect: draws.append('animation')
  for _ in range(20):
    s.ui.new_ui = True
    namespace['_render'](h, s.rect)
    assert draws[-1] == 'egpu'
    s.ui.new_ui = False
    namespace['_render'](h, s.rect)
    assert draws[-2:] == ['legacy', 'animation']
  assert len(allocations) == 1
  assert draws.count('invalidate') == 40


def test_new_ui_is_display_only_and_existing_overlay_owners_remain():
  text = (ONROAD / 'augmented_road_view.py').read_text()
  for fragment in ('self._model_renderer.render', 'self._alert_renderer.render', 'self._traffic_light.render',
                   'self._vision_renderer.render', 'self._hud_renderer.draw_jetlink_status'):
    assert fragment in text
  source = (ONROAD / 'wayon_hud.py').read_text()
  assert all(forbidden not in source for forbidden in ('PubMaster', 'Params(', 'put_bool', 'controls_allowed'))
