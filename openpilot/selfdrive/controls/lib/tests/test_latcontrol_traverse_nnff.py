"""Traverse full-NNFF behaviour that follows FrogPilot's NNFF. Real controller, donor NN model and
vehicle model; Params are in memory, no device I/O."""
import math
from types import SimpleNamespace as NS

import pytest

from openpilot.cereal import car, log
from opendbc.car.gm.values import CAR
from opendbc.car.interfaces import get_nn_model
from opendbc.car.vehicle_model import VehicleModel
from openpilot.selfdrive.modeld.constants import ModelConstants
from opendbc.car.gm.tests.test_display_speed import MemoryParams, runtime, build_cp  # noqa: F401


@pytest.fixture
def make_controller(runtime, monkeypatch):
  import openpilot.selfdrive.controls.lib.latcontrol_torque as latcontrol_torque
  monkeypatch.setattr(latcontrol_torque, "Params", MemoryParams)

  def make(fingerprint=CAR.CHEVROLET_TRAVERSE, use_nnff=True):
    CP = build_cp(runtime)
    CP.carFingerprint = str(fingerprint)
    CI = runtime.interface.CarInterface.__new__(runtime.interface.CarInterface)
    CI.CP = CP
    CI.lat_torque_nn_model = get_nn_model(str(CAR.CHEVROLET_TRAVERSE), "", allow_substitute=True)
    CI.use_nnff, CI.use_nnff_lite = use_nnff, False
    assert CI.lat_torque_nn_model is not None
    return latcontrol_torque.LatControlTorque(CP.as_reader(), CI), CP
  return make


def model(lat_accel, jerk):
  md = log.ModelDataV2.new_message()
  t = ModelConstants.T_IDXS
  md.init("acceleration")
  md.acceleration.y = [lat_accel + jerk * x for x in t]
  md.init("orientation")
  md.orientation.x = [0.0] * len(t)
  md.orientation.y = [0.0] * len(t)
  return md


def step(controller, CP, rate, plan_jerk, lat_accel=2.0, v=20.0, frames=1, actual_lat_accel=None):
  VM = VehicleModel(CP)
  VM.update_params(1.0, 18.7)
  desired_curvature = lat_accel / v ** 2
  actual_curvature = desired_curvature if actual_lat_accel is None else actual_lat_accel / v ** 2
  CS = car.CarState.new_message(vEgo=v, aEgo=0.0, steeringPressed=False, steeringRateDeg=rate,
                                steeringAngleDeg=math.degrees(VM.get_steer_from_curvature(-actual_curvature, v, 0.0)))
  params = log.LiveParametersData.new_message()
  cc = NS(angularVelocity=[], orientationNED=[])
  md = model(lat_accel, plan_jerk)
  for _ in range(frames):
    torque, _, pid_log = controller.update(True, CS, VM, params, False, desired_curvature, cc, False, md)
  return torque, pid_log


@pytest.mark.parametrize("fingerprint", [CAR.CHEVROLET_TRAVERSE, CAR.CHEVROLET_TRAILBLAZER, CAR.CHEVROLET_TRAILBLAZER_CC,
                                         CAR.CHEVROLET_TRAX])
def test_parity_for_traverse_trailblazer_trax_full_nnff(make_controller, fingerprint):
  assert make_controller(fingerprint=fingerprint)[0].nnff_frogpilot_parity
  assert not make_controller(fingerprint=fingerprint, use_nnff=False)[0].nnff_frogpilot_parity


def test_parity_not_for_other_cars(make_controller):
  assert not make_controller(fingerprint=CAR.CHEVROLET_EQUINOX)[0].nnff_frogpilot_parity


def test_parity_takes_desired_jerk_over_lateral_delay(make_controller):
  controller, CP = make_controller()
  assert controller.desired_lat_jerk_time == pytest.approx(CP.steerActuatorDelay + 0.1)
  other, _ = make_controller(fingerprint=CAR.CHEVROLET_EQUINOX)
  assert other.desired_lat_jerk_time == pytest.approx(0.3)  # SteerActuatorDelay Param 0 + 0.3 s


def test_measured_jerk_reaches_nn_when_plan_has_jerk(make_controller):
  requests = []
  for rate in (-20.0, 20.0):
    controller, CP = make_controller()
    torque, _ = step(controller, CP, rate, plan_jerk=0.5)
    requests.append(torque)
  assert abs(requests[0] - requests[1]) > 0.02
  assert all(math.isfinite(x) for x in requests)


def test_other_cars_still_drop_steering_angle_jerk(make_controller):
  requests = []
  for rate in (-20.0, 20.0):
    controller, CP = make_controller(fingerprint=CAR.CHEVROLET_EQUINOX)
    torque, _ = step(controller, CP, rate, plan_jerk=0.5)
    requests.append(torque)
  assert requests[0] == pytest.approx(requests[1], abs=1e-9)


def test_measured_jerk_dropped_without_planned_jerk(make_controller):
  requests = []
  for rate in (-20.0, 20.0):
    controller, CP = make_controller()
    torque, _ = step(controller, CP, rate, plan_jerk=0.0)
    requests.append(torque)
  assert requests[0] == pytest.approx(requests[1], abs=1e-9)


def test_non_finite_steering_rate_keeps_controller_finite(make_controller):
  controller, CP = make_controller()
  torque, pid_log = step(controller, CP, float("nan"), plan_jerk=0.5, frames=5)
  assert math.isfinite(torque) and math.isfinite(pid_log.i)
  torque, pid_log = step(controller, CP, 0.0, plan_jerk=0.5, frames=5)
  assert math.isfinite(torque) and math.isfinite(pid_log.i)


@pytest.mark.parametrize("fingerprint", [CAR.CHEVROLET_TRAVERSE, CAR.CHEVROLET_TRAILBLAZER, CAR.CHEVROLET_TRAX])
def test_jerk_gate_does_not_change_later_curve_feedforward(make_controller, fingerprint):
  after_gate, CP = make_controller(fingerprint=fingerprint)
  fresh, _ = make_controller(fingerprint=fingerprint)
  step(after_gate, CP, 0.0, 0.0, lat_accel=0.5, actual_lat_accel=0.25)
  _, actual = step(after_gate, CP, 0.0, 0.5, lat_accel=0.5, actual_lat_accel=0.25, frames=40)
  _, expected = step(fresh, CP, 0.0, 0.5, lat_accel=0.5, actual_lat_accel=0.25, frames=40)
  assert actual.f == pytest.approx(expected.f, abs=1e-8)
  assert actual.p == pytest.approx(expected.p, abs=1e-8)


@pytest.mark.parametrize("invalid", [None, float("nan"), float("inf"), -0.2, 0.0, 1.1, True])
def test_bad_live_delay_preserves_last_valid_timing(make_controller, invalid):
  controller, _ = make_controller()
  controller.update_live_delay(0.32, 0.1)
  before = list(controller.nn_future_times)
  controller.update_live_delay(invalid)
  assert controller.nn_future_times == before
  assert controller.desired_lat_jerk_time == pytest.approx(0.42)


def test_live_delay_updates_future_and_jerk_horizons_separately(make_controller):
  controller, _ = make_controller()
  controller.update_live_delay(0.32, 0.15)
  assert controller.nn_future_times == pytest.approx([0.62, 0.92, 1.32, 1.82])
  assert controller.desired_lat_jerk_time == pytest.approx(0.47)


def test_non_parity_timing_and_pid_remain_unchanged(make_controller):
  from openpilot.common.pid import PIDController
  controller, _ = make_controller(fingerprint=CAR.CHEVROLET_EQUINOX)
  before = list(controller.nn_future_times)
  controller.update_live_delay(0.32)
  assert controller.nn_future_times == before
  assert type(controller.pid) is PIDController


def test_non_parity_keeps_existing_friction_gate(make_controller):
  controller, CP = make_controller(fingerprint=CAR.CHEVROLET_EQUINOX)
  step(controller, CP, 0.0, 0.0)
  assert controller.lat_accel_friction_factor == 1.0


def test_missing_model_does_not_add_friction_to_full_nnff_fallback(make_controller):
  controller, CP = make_controller()
  friction_calls = []
  original = controller.torque_from_lateral_accel

  def record(*args, **kwargs):
    friction_calls.append(kwargs["friction_compensation"])
    return original(*args, **kwargs)

  controller.torque_from_lateral_accel = record
  CS = car.CarState.new_message(vEgo=15.0, aEgo=0.0, steeringPressed=False,
                                steeringRateDeg=0.0, steeringAngleDeg=0.0)
  torque, _, pid_log = controller.update(True, CS, VehicleModel(CP), log.LiveParametersData.new_message(),
                                        False, 0.001, NS(orientationNED=[], angularVelocity=[]), False, model_data=None)
  assert math.isfinite(torque) and pid_log.active
  assert friction_calls == [False, False, False]


def test_reset_clears_nn_history_but_keeps_integral(make_controller):
  controller, CP = make_controller()
  step(controller, CP, 0.0, 0.5, frames=40)
  assert controller.lateral_accel_desired_deque
  controller.pid.i = 0.12
  controller.reset()
  assert not controller.lateral_accel_desired_deque
  assert not controller.roll_deque
  assert controller.pid.i == 0.12
