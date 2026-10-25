"""Offline native Wayon HUD preview. Synthetic inputs, no device or publishers."""
import argparse
from pathlib import Path
import sys
import time
from types import ModuleType, SimpleNamespace as NS

import pyray as rl


def main():
  cases = ('set-speed', 'steady', 'disengaged', 'bsd', 'critical', 'parking', 'hold', 'three-digits', 'lateral-only',
           'cam-active', 'cam-dim', 'cam-idle', 'turn-active', 'section-active', 'cap-max', 'cap-warning', 'cap-timer',
           'bsm-left', 'bsm-right', 'bsm-both', 'bsm-max', 'bsm-critical', 'bsm-timer',
           'bsm-fade-in', 'bsm-fade-out', 'bsm-cleared', 'bsm-stale')
  parser = argparse.ArgumentParser()
  parser.add_argument('--output', type=Path, required=True)
  parser.add_argument('--case', choices=cases, action='append', help='Render only these cases (repeatable).')
  args = parser.parse_args()
  args.output.mkdir(parents=True, exist_ok=True)

  class SM(dict):
    valid = dict.fromkeys(('carState', 'selfdriveState', 'carControl', 'carrotMan', 'longitudinalPlan'), True)
    alive = valid.copy()
    recv_frame = dict.fromkeys(valid, 101)
    seen = valid.copy()
    recv_time = dict.fromkeys(valid, time.monotonic())
  car = NS(leftBlindspot=False, rightBlindspot=False, steeringAngleDeg=4, vCruise=100, gasPressed=False, brakePressed=False)
  sm = SM(carState=car, carControl=NS(latActive=False, longActive=True), selfdriveState=NS(enabled=True),
          controlsState=NS(lateralControlState=NS(which=lambda: 'torqueState')), carOutput=NS(actuatorsOutput=NS(torque=-.45)),
          carrotMan=NS(desiredSpeed=250, desiredSource='none'), longitudinalPlan=NS(cruiseTarget=100))
  ui = NS(started=True, started_frame=100, status='engaged', is_metric=True, sm=sm, new_ui=True, share_data=False)
  # Inject only UI inputs; keep real widgets, fonts, textures, filters and GPU draw calls.
  state_module = ModuleType('openpilot.selfdrive.ui.ui_state')
  state_module.ui_state = ui
  state_module.UIStatus = NS(DISENGAGED='disengaged', ENGAGED='engaged')
  sys.modules[state_module.__name__] = state_module
  from openpilot.system.ui.lib.application import FontWeight, gui_app
  from openpilot.selfdrive.ui.mici.onroad.wayon_hud import WayonHud
  from openpilot.selfdrive.ui.mici.onroad.status_timer_renderer import StatusTimerRenderer
  from openpilot.selfdrive.ui.mici.onroad.status_timers import TimerDisplay
  from openpilot.selfdrive.ui.mici.onroad.vision_renderer import VisionRenderer

  gui_app.init_window('C4 new ui native preview', font_weights=[FontWeight.DISPLAY, FontWeight.SEMI_BOLD])
  real_clock = rl.get_time
  preview_time = [10.]
  rl.get_time = lambda: preview_time[0]
  try:
    renderer = WayonHud()
    timer = StatusTimerRenderer()
    vision = VisionRenderer()
    hud = NS(_can_draw_top_icons=True, _status_timer_visible=False, _show_wheel_critical=False,
             is_cruise_set=True, _engaged=True, set_speed=100, _set_speed_changed_time=rl.get_time(),
             _font_display=gui_app.font(FontWeight.DISPLAY), _font_semi_bold=gui_app.font(FontWeight.SEMI_BOLD),
             _turn_intent=NS(render=lambda _: None))
    rect = rl.Rectangle(0, 0, 536, 240)
    selected_cases = args.case or cases
    for case in selected_cases:
      preview_time[0] = 10.
      renderer.reset()
      sm.alive['carState'] = True
      ui.status = 'disengaged' if case in ('disengaged', 'lateral-only') else 'engaged'
      hud._engaged = case not in ('disengaged', 'lateral-only')
      sm['carControl'].latActive = case == 'lateral-only' or case.endswith('active') or case.startswith('bsm-')
      sm['selfdriveState'].enabled = hud._engaged
      sm['carControl'].longActive = hud._engaged and case != 'cam-idle'
      hud._status_timer_visible = case in ('parking', 'hold', 'cap-timer', 'bsm-timer')
      hud._show_wheel_critical = case in ('critical', 'bsm-critical')
      hud._can_draw_top_icons = case not in ('critical', 'cap-warning', 'bsm-critical')
      hud.set_speed = 254 if case == 'three-digits' else 100
      hud._set_speed_changed_time = rl.get_time() if case in ('set-speed', 'three-digits', 'cap-max', 'bsm-max') else -10
      car.leftBlindspot = case == 'bsd' or case.startswith('bsm-') and case != 'bsm-right'
      car.rightBlindspot = case == 'bsd' or case.startswith('bsm-') and case != 'bsm-left'
      cap = case.startswith(('cam-', 'cap-', 'bsm-')) or case in ('turn-active', 'section-active')
      sm['carrotMan'].desiredSpeed = 60 if cap else 250
      sm['carrotMan'].desiredSource = 'atc' if case == 'turn-active' else 'section' if case == 'section-active' else 'cam'
      sm['longitudinalPlan'].cruiseTarget = 60 if cap else 100
      frames = 1 if case == 'bsm-fade-in' else 41 if case in ('bsm-fade-out', 'bsm-stale') else 60 if case == 'bsm-cleared' else 40
      for frame in range(frames):
        if frame >= 40:
          if case in ('bsm-fade-out', 'bsm-cleared'):
            car.leftBlindspot = car.rightBlindspot = False
          elif case == 'bsm-stale':
            sm.alive['carState'] = False
        sm.recv_time['carrotMan'] = time.monotonic()
        preview_time[0] = 10.7 if case == 'cam-dim' and frame > 0 else 10.
        rl.begin_drawing()
        rl.clear_background(rl.Color(22, 24, 26, 255))
        renderer.draw(rect, hud)
        vision.render(rect)
        if hud._status_timer_visible:
          timer.draw(rect, TimerDisplay('parking' if case in ('cap-timer', 'bsm-timer') else case, 83), 1.)
        if frame == frames - 1:
          # Read the frame just drawn, not the old back buffer after swapping.
          rl.rl_draw_render_batch_active()
          image = rl.load_image_from_screen()
          try:
            assert image.width == 536 and image.height == 240
            assert rl.export_image(image, str(args.output / f'{case}.png'))
          finally:
            rl.unload_image(image)
        rl.end_drawing()
    print(f'PASS: {len(selected_cases)} native 536x240 HUD frames; isolated synthetic inputs, not a vehicle capture')
  finally:
    rl.get_time = real_clock
    gui_app.close()


if __name__ == '__main__':
  main()
