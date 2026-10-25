"""Display the selected speed cap, not a claim that the brake is actuating."""
import math

import pyray as rl

from openpilot.selfdrive.carrot.carrot_man_input import get_carrot_man
from openpilot.selfdrive.carrot.deceleration_source import DECELERATION_SOURCE_LABELS
from openpilot.system.ui.lib.text_measure import measure_text_cached


def selected_deceleration(sm, started_frame: int) -> tuple[str, bool] | None:
  def fresh(service):
    return sm.valid[service] and sm.alive[service] and sm.recv_frame[service] > 0 and sm.recv_frame[service] >= started_frame

  if not all(fresh(service) for service in ('carState', 'selfdriveState', 'carrotMan')):
    return None
  carrot = get_carrot_man(sm)
  if carrot is None or not 0 < carrot.desiredSpeed < 200:
    return None
  source = str(carrot.desiredSource or '').strip().lower()
  for suffix in (':n', ':v', ':c'):
    source = source.removesuffix(suffix)
  label = DECELERATION_SOURCE_LABELS.get(source)
  if not label or source == 'gas':
    return None

  # Match the planner's kph cap to the selected source. Cluster speed can be
  # corrected independently, so it must not decide whether this cap is applied.
  car = sm['carState']
  active = False
  if fresh('carControl') and fresh('longitudinalPlan'):
    target = float(sm['longitudinalPlan'].cruiseTarget)
    selected = float(car.vCruise)
    active = (sm['selfdriveState'].enabled and sm['carControl'].longActive and
              not car.gasPressed and not car.brakePressed and
              math.isfinite(selected) and 0 < selected < 255 and carrot.desiredSpeed < selected and
              math.isfinite(target) and abs(target - carrot.desiredSpeed) <= 1.0)
  return label.upper(), bool(active)


class DecelerationBadge:
  WIDTH = 76
  HEIGHT = 26
  PERIOD = 1.0
  GREEN = (32, 216, 106, 255)
  IDLE = (48, 52, 58, 255)

  def __init__(self):
    self.reset()

  def reset(self):
    self._active_label = None
    self._active_since = 0.0

  def draw(self, rect: rl.Rectangle, font, state: tuple[str, bool] | None, now: float):
    if state is None:
      self.reset()
      return
    label, active = state
    active_label = label if active else None
    if active_label != self._active_label:
      self._active_label = active_label
      self._active_since = now
    bright = active and (now - self._active_since) % self.PERIOD < 0.6
    box = rl.Rectangle(rect.x + 136, rect.y + rect.height - 78, self.WIDTH, self.HEIGHT)
    rl.draw_rectangle_rounded(box, 0.3, 6, rl.Color(*(self.GREEN if bright else self.IDLE)))
    size = 18
    measured = measure_text_cached(font, label, size)
    size *= min(1, (self.WIDTH - 16) / max(1, measured.x))
    measured = measure_text_cached(font, label, size)
    color = rl.Color(8, 24, 14, 255) if bright else rl.WHITE
    rl.draw_text_ex(font, label, rl.Vector2(box.x + (box.width - measured.x) / 2,
                                          box.y + (box.height - measured.y) / 2), size, 0, color)
