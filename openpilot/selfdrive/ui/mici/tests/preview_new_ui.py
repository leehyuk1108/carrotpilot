"""Offline native Wayon HUD preview. Synthetic inputs, no device or publishers."""
import argparse
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace as NS

import pyray as rl


def main():
  parser = argparse.ArgumentParser()
  parser.add_argument('--output', type=Path, required=True)
  args = parser.parse_args()
  args.output.mkdir(parents=True, exist_ok=True)

  class SM(dict):
    valid = dict.fromkeys(('carState', 'selfdriveState', 'carControl'), True)
    alive = valid.copy()
    recv_frame = dict.fromkeys(valid, 101)
  car = NS(leftBlindspot=False, rightBlindspot=False, steeringAngleDeg=4)
  sm = SM(carState=car, carControl=NS(latActive=False), controlsState=NS(lateralControlState=NS(which=lambda: 'torqueState')),
          carOutput=NS(actuatorsOutput=NS(torque=-.45)))
  ui = NS(started=True, started_frame=100, status='engaged', is_metric=True, sm=sm)
  # Inject only UI inputs; keep real widgets, fonts, textures, filters and GPU draw calls.
  state_module = ModuleType('openpilot.selfdrive.ui.ui_state')
  state_module.ui_state = ui
  state_module.UIStatus = NS(DISENGAGED='disengaged', ENGAGED='engaged')
  sys.modules[state_module.__name__] = state_module
  from openpilot.system.ui.lib.application import FontWeight, gui_app
  from openpilot.selfdrive.ui.mici.onroad.wayon_hud import WayonHud
  from openpilot.selfdrive.ui.mici.onroad.status_timer_renderer import StatusTimerRenderer
  from openpilot.selfdrive.ui.mici.onroad.status_timers import TimerDisplay

  gui_app.init_window('C4 new ui native preview', font_weights=[FontWeight.DISPLAY, FontWeight.SEMI_BOLD])
  try:
    renderer = WayonHud()
    timer = StatusTimerRenderer()
    hud = NS(_can_draw_top_icons=True, _status_timer_visible=False, _show_wheel_critical=False,
             is_cruise_set=True, _engaged=True, set_speed=100, _set_speed_changed_time=rl.get_time(),
             _font_display=gui_app.font(FontWeight.DISPLAY), _font_semi_bold=gui_app.font(FontWeight.SEMI_BOLD),
             _turn_intent=NS(render=lambda _: None))
    rect = rl.Rectangle(0, 0, 476, 240)
    cases = ('set-speed', 'steady', 'disengaged', 'bsd', 'critical', 'parking', 'hold', 'three-digits', 'lateral-only')
    for case in cases:
      renderer.reset()
      ui.status = 'disengaged' if case in ('disengaged', 'lateral-only') else 'engaged'
      hud._engaged = case not in ('disengaged', 'lateral-only')
      sm['carControl'].latActive = case == 'lateral-only'
      hud._status_timer_visible = case in ('parking', 'hold')
      hud._show_wheel_critical = case == 'critical'
      hud._can_draw_top_icons = case != 'critical'
      hud.set_speed = 254 if case == 'three-digits' else 100
      hud._set_speed_changed_time = rl.get_time() if case in ('set-speed', 'three-digits') else -10
      car.leftBlindspot = car.rightBlindspot = case == 'bsd'
      for _ in range(40):
        rl.begin_drawing()
        rl.clear_background(rl.Color(22, 24, 26, 255))
        renderer.draw(rect, hud)
        if hud._status_timer_visible:
          timer.draw(rect, TimerDisplay(case, 83), 1.)
        if case == 'bsd':
          for x in (6, 464):
            rl.draw_rectangle_rounded(rl.Rectangle(x, 78, 6, 56), .8, 6, rl.Color(255, 215, 0, 255))
        rl.end_drawing()
      image = rl.load_image_from_screen()
      try:
        assert image.width == 536 and image.height == 240
        assert rl.export_image(image, str(args.output / f'{case}.png'))
      finally:
        rl.unload_image(image)
    print(f'PASS: {len(cases)} native 536x240 HUD frames; isolated synthetic inputs, not a vehicle capture')
  finally:
    gui_app.close()


if __name__ == '__main__':
  main()
