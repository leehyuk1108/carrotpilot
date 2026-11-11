"""Derivative on measurement in the torque controller (fix_pls_dterm). Real controller and vehicle model;
Params are in memory, no device I/O."""
import math
from types import SimpleNamespace as NS

import pytest

from openpilot.cereal import car, log
from opendbc.car.gm.values import CAR
from opendbc.car.interfaces import get_nn_model
from opendbc.car.vehicle_model import VehicleModel
from openpilot.selfdrive.modeld.constants import ModelConstants
from openpilot.selfdrive.controls.lib import latcontrol_torque
from opendbc.car.gm.tests.test_display_speed import MemoryParams, runtime, build_cp  # noqa: F401


def custom_params(kd):
  class CustomParams(MemoryParams):
    def __init__(self, *args):
      super().__init__(*args)
      self.values.update({"LateralTorqueCustom": 1, "LateralTorqueKpV": 100, "LateralTorqueKiV": 0,
                          "LateralTorqueKf": 100, "LateralTorqueAccelFactor": 2500, "LateralTorqueFriction": 0,
                          "LateralTorqueKd": kd})
  return CustomParams


@pytest.fixture
def make_controller(runtime, monkeypatch):
  def make(kd, use_nnff=False):
    monkeypatch.setattr(latcontrol_torque, "Params", custom_params(kd))
    CP = build_cp(runtime)
    CP.carFingerprint = str(CAR.CHEVROLET_TRAVERSE)
    CI = runtime.interface.CarInterface.__new__(runtime.interface.CarInterface)
    CI.CP = CP
    CI.lat_torque_nn_model = get_nn_model(str(CAR.CHEVROLET_TRAVERSE), "", allow_substitute=True)
    CI.use_nnff, CI.use_nnff_lite = use_nnff, False
    return latcontrol_torque.LatControlTorque(CP.as_reader(), CI), CP
  return make


def model(lat_accel):
  md = log.ModelDataV2.new_message()
  t = ModelConstants.T_IDXS
  md.init("acceleration")
  md.acceleration.y = [lat_accel] * len(t)
  md.init("orientation")
  md.orientation.x = [0.0] * len(t)
  md.orientation.y = [0.0] * len(t)
  return md


def run(controller, CP, angles, lat_accel=1.5, v=20.0, pressed=None, active=None):
  VM = VehicleModel(CP)
  VM.update_params(1.0, 18.7)
  desired_curvature = lat_accel / v ** 2
  params = log.LiveParametersData.new_message()
  cc = NS(angularVelocity=[], orientationNED=[])
  md = model(lat_accel)
  logs = []
  for k, angle in enumerate(angles):
    CS = car.CarState.new_message(vEgo=v, aEgo=0.0, steeringRateDeg=0.0, steeringAngleDeg=angle,
                                  steeringPressed=bool(pressed[k]) if pressed else False)
    is_active = bool(active[k]) if active else True
    _, _, pid_log = controller.update(is_active, CS, VM, params, False, desired_curvature, cc, False, md)
    logs.append(pid_log)
  return logs


SETTLE = [-30.0] * 20  # custom gains load on every 10th frame


def test_no_d_when_kd_is_zero(make_controller):
  controller, CP = make_controller(0)
  logs = run(controller, CP, SETTLE + [-30.0 - 0.5 * k for k in range(60)])
  assert all(entry.d == 0.0 for entry in logs)


def test_d_opposes_the_measured_change(make_controller):
  controller, CP = make_controller(5)
  winding = run(controller, CP, SETTLE + [-30.0 - 0.3 * k for k in range(60)])
  controller, CP = make_controller(5)
  unwinding = run(controller, CP, SETTLE + [-30.0 + 0.3 * k for k in range(60)])
  assert winding[-1].d * unwinding[-1].d < 0.0
  # The measured lateral acceleration grows while winding; D pulls the torque request back.
  assert winding[-1].actualLateralAccel > winding[len(SETTLE)].actualLateralAccel
  assert winding[-1].d < 0.0


def test_kd_is_capped_at_0_10_and_d_at_0_2(make_controller):
  controller, CP = make_controller(200)
  logs = run(controller, CP, SETTLE + [-30.0 - 3.0 * k for k in range(40)])
  assert controller.pid._k_d[1][0] == pytest.approx(latcontrol_torque.LAT_KD_MAX)
  assert max(abs(entry.d) for entry in logs) == pytest.approx(latcontrol_torque.LAT_D_MAX, rel=1e-6)


def test_no_d_while_the_driver_steers(make_controller):
  controller, CP = make_controller(5)
  angles = SETTLE + [-30.0 - 0.5 * k for k in range(40)]
  pressed = [0] * len(SETTLE) + [1] * 40
  logs = run(controller, CP, angles, pressed=pressed)
  assert all(entry.d == 0.0 for entry in logs[len(SETTLE):])


def test_no_kick_when_lateral_control_resumes(make_controller):
  controller, CP = make_controller(5)
  angles = SETTLE + [-60.0] * 10 + [-10.0] + [-10.0] * 5
  active = [1] * len(SETTLE) + [0] * 10 + [1] + [1] * 5
  logs = run(controller, CP, angles, active=active)
  assert logs[len(SETTLE) + 10].d == 0.0
  controller.reset()
  assert controller.previous_measurement is None and controller.measurement_rate.x == 0.0


def test_d_stays_finite_with_nnff_and_a_bad_angle(make_controller):
  controller, CP = make_controller(5, use_nnff=True)
  logs = run(controller, CP, SETTLE + [-30.0, float("nan"), -30.5, -31.0])
  assert all(math.isfinite(entry.d) for entry in logs)
