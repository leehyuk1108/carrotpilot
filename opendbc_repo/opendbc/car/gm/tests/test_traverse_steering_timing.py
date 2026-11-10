"""Traverse LKA command timing and torque ceiling. Real GM controller and limiter; no device I/O."""
from types import SimpleNamespace as NS

import pytest

from opendbc.car import DT_CTRL, apply_driver_steer_torque_limits, structs
from opendbc.car.gm.values import CAR, DBC, CarControllerParams
from opendbc.car.gm.tests.test_display_speed import MemoryParams, runtime, build_cp, controller_state  # noqa: F401


def ramp_commands(params, target, start=0, limit=500):
  last, commands = start, 0
  while last != target and commands < limit:
    last = apply_driver_steer_torque_limits(target, last, 0.0, params)
    commands += 1
  return commands


def test_traverse_uses_upstream_frogpilot_command_timing():
  p = CarControllerParams(NS(carFingerprint=CAR.CHEVROLET_TRAVERSE))
  assert (p.STEER_STEP, p.STEER_DELTA_UP, p.STEER_DELTA_DOWN, p.STEER_MAX) == (3, 10, 15, 300)


@pytest.mark.parametrize("candidate", [c for c in CAR if c != CAR.CHEVROLET_TRAVERSE])
def test_other_gm_cars_keep_existing_timing(candidate):
  p = CarControllerParams(NS(carFingerprint=candidate))
  assert (p.STEER_STEP, p.STEER_DELTA_UP, p.STEER_DELTA_DOWN, p.STEER_MAX) == (4, 5, 7, 300)


def test_traverse_full_torque_ramp_time():
  p = CarControllerParams(NS(carFingerprint=CAR.CHEVROLET_TRAVERSE))
  assert ramp_commands(p, 300) * p.STEER_STEP * DT_CTRL == pytest.approx(0.9)
  assert ramp_commands(p, 0, start=300) * p.STEER_STEP * DT_CTRL == pytest.approx(0.6)
  assert ramp_commands(p, -300, start=300) * p.STEER_STEP * DT_CTRL == pytest.approx(1.5)


@pytest.mark.parametrize("custom,expected", [(0, 300), (250, 250), (300, 300), (400, 300), (1000, 300)])
def test_custom_steer_max_is_capped_at_eps_limit(runtime, monkeypatch, custom, expected):
  class CustomParams(MemoryParams):
    def __init__(self, *args):
      super().__init__(*args)
      self.values.update({"CustomSteerMax": custom, "CustomSteerDeltaUp": 10, "CustomSteerDeltaDown": 15})

  monkeypatch.setattr(runtime.controller, "Params", CustomParams)
  controller = runtime.controller.CarController(DBC[CAR.CHEVROLET_TRAVERSE], build_cp(runtime))
  cc = structs.CarControl(enabled=True, latActive=True)
  cc.actuators.torque = 1.0
  state = controller_state()
  sent = []
  for tick in range(200):
    output, _ = controller.update(cc.as_reader(), state, 1_000_000_000 + tick * 10_000_000)
    sent.append(output.torqueOutputCan)
  assert controller.params.STEER_MAX == expected
  assert max(abs(x) for x in sent) == expected
  assert controller.params.STEER_STEP == 3
