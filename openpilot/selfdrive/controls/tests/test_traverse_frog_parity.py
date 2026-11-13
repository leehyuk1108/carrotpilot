"""FrogPilot parity of the Traverse-family lateral path (user request 2026-11-14: match FrogPilot's logic, keep the
settings): integrator handling at the torque limit, lateral delay inputs of the NNFF and the lateral acceleration
limit of the desired curvature."""
import pytest

from openpilot.common.params import Params
from openpilot.common.pid import PIDController
from openpilot.selfdrive.controls.lib.drive_helpers import clip_curvature
from openpilot.selfdrive.controls.lib.latcontrol_torque import FrogPilotWindupPID, NNFF_FROGPILOT_PARITY_CARS


def lat_accel_after(v_ego, target_lat_accel, **kwargs):
  curvature = 0.0
  for _ in range(2000):   # the jerk limit only lets it grow step by step
    curvature, limited = clip_curvature(v_ego, curvature, target_lat_accel / v_ego ** 2, 0.0, **kwargs)
  return curvature * v_ego ** 2, limited


def test_frogpilot_limit_keeps_3_0_lateral_accel_at_low_speed():
  v = 50 / 3.6
  carrot, _ = lat_accel_after(v, 4.2)
  frog, limited = lat_accel_after(v, 4.2, low_speed_allowance=False)
  assert carrot == pytest.approx(4.2, abs=1e-6)           # carrot's own path allows up to 4.5 below 80 km/h
  assert frog == pytest.approx(3.0, abs=1e-6) and limited  # FrogPilot / stock: 3.0 at every speed
  assert lat_accel_after(v, 2.5, low_speed_allowance=False)[0] == pytest.approx(2.5, abs=1e-6)



@pytest.mark.parametrize("roll", [-0.05, 0.0, 0.05])
def test_frogpilot_limit_moves_with_roll_and_clamps_an_outside_target_at_once(roll):
  """The band is roll*g +- 3.0 (FrogPilot/stock), so a 2.8 target on a -0.05 rad road is cut to 2.5095, and a previous
  target outside the band (e.g. carrot's 4.5 allowance just before) is clamped in one step, as in FrogPilot."""
  from opendbc.car.vehicle_model import ACCELERATION_DUE_TO_GRAVITY as G
  v = 15.0
  for target in (2.8, -2.8, 3.8, -3.8):
    k = target / v ** 2
    frog, limited = clip_curvature(v, k, k, roll, low_speed_allowance=False)
    expected = min(max(target, roll * G - 3.0), roll * G + 3.0)
    assert frog * v ** 2 == pytest.approx(expected, abs=1e-9)
    assert limited == (abs(expected - target) > 1e-9)
    carrot, _ = clip_curvature(v, k, k, roll)   # carrot's own path: 4.5 below 80 km/h, so none of these is cut
    assert carrot * v ** 2 == pytest.approx(target, abs=1e-9)

def test_frogpilot_integrator_is_not_dumped_at_the_limit():
  frog = FrogPilotWindupPID(1.0, 0.3, k_f=1.0, pos_limit=1.0, neg_limit=-1.0)
  carrot = PIDController(1.0, 0.3, k_f=1.0, pos_limit=1.0, neg_limit=-1.0)
  for pid in (frog, carrot):
    pid.i = 0.30
  # a short P/FF spike to the limit: carrot cuts the integrator to the room left, FrogPilot only stops its growth
  frog.update(0.05, feedforward=0.95)
  carrot.update(0.05, feedforward=0.95)
  assert frog.i == pytest.approx(0.30)
  assert carrot.i == pytest.approx(0.0, abs=1e-9)
  # when the spike passes, FrogPilot's output keeps the integrator; carrot's drops by it
  assert frog.update(0.0, feedforward=0.6) == pytest.approx(0.9)
  assert carrot.update(0.0, feedforward=0.6) == pytest.approx(0.6, abs=1e-3)


def test_frogpilot_integrator_still_grows_below_the_limit_and_is_bounded():
  pid = FrogPilotWindupPID(1.0, 0.3, k_f=1.2, pos_limit=1.0, neg_limit=-1.0)
  for _ in range(100):
    out = pid.update(0.1, feedforward=0.2)
  assert pid.i == pytest.approx(0.03, rel=1e-6)            # 0.3 * 0.1 * 100 steps * 0.01 s
  assert pid.f == pytest.approx(0.24)                       # k_f (the LateralTorqueKf setting) is kept
  assert out == pytest.approx(0.1 + 0.03 + 0.24, rel=1e-6)
  for _ in range(10000):
    pid.update(1.0, feedforward=0.0)
  assert pid.control == pytest.approx(1.0)                  # the output never passes the limit
  assert pid.i == pytest.approx(0.03, rel=1e-6)             # and the integrator does not wind up while clipping


@pytest.fixture
def traverse_controller():
  from opendbc.car.car_helpers import interfaces
  from openpilot.selfdrive.controls.lib.latcontrol_torque import LatControlTorque
  if not Params().get_bool("NNFF"):   # read only: the Traverse runs with NNFF on
    pytest.skip("NNFF is off here")
  name = "CHEVROLET_TRAVERSE"
  CarInterface = interfaces[name]
  CP = CarInterface.get_non_essential_params(name)
  CI = CarInterface(CP)
  if not CI.use_nnff:
    pytest.skip("no NNFF model for the Traverse in this build")
  return LatControlTorque(CP.as_reader(), CI), CP


def test_traverse_uses_frogpilot_delay_inputs_and_integrator(traverse_controller):
  lc, CP = traverse_controller
  assert "CHEVROLET_TRAVERSE" in NNFF_FROGPILOT_PARITY_CARS and lc.nnff_frogpilot_parity
  assert isinstance(lc.pid, FrogPilotWindupPID)
  # before liveDelay arrives: FrogPilot adds the car's steer delay alone (no extra 0.2 s)
  assert lc.nn_future_times == pytest.approx([t + CP.steerActuatorDelay for t in (0.3, 0.6, 1.0, 1.5)])
  lc.update_live_delay(0.35)
  assert lc.nn_future_times == pytest.approx([0.65, 0.95, 1.35, 1.85])
  assert lc.desired_lat_jerk_time == pytest.approx(0.35)
  lc.update_live_delay(float('nan'))
  lc.update_live_delay(0.0)
  assert lc.desired_lat_jerk_time == pytest.approx(0.35)   # an invalid delay keeps the last good one


def test_traverse_adds_no_friction_override_like_frogpilot(traverse_controller):
  import inspect
  from openpilot.selfdrive.controls.lib import latcontrol_torque
  lc, _ = traverse_controller
  src = inspect.getsource(latcontrol_torque.LatControlTorque.update)
  assert "if self.nn_friction_override and not self.nnff_frogpilot_parity:" in src
  assert lc.nnff_frogpilot_parity   # so the carrot-only friction error is skipped for the Traverse


def test_traverse_fallback_feedforward_has_no_friction_like_frogpilot(traverse_controller):
  """Model too short for the NN (model_data=None): FrogPilot's fallback FF is the plain linear torque."""
  from types import SimpleNamespace as NS
  from openpilot.cereal import car, log
  from opendbc.car.vehicle_model import VehicleModel
  from openpilot.selfdrive.controls.lib import latcontrol_torque as lt
  lc, CP = traverse_controller
  carrot = lt.LatControlTorque(CP.as_reader(), lc_ci(CP))
  carrot.nnff_frogpilot_parity = False   # the carrot path for the same car, for comparison
  VM = VehicleModel(CP.as_reader())
  params = log.LiveParametersData.new_message()
  CS = car.CarState.new_message(vEgo=20.0, steeringAngleDeg=4.0)
  outs = []
  for ctl in (lc, carrot):
    ctl.torque_params.friction = 0.18   # a friction to show the term (frame 1: the custom settings are not re-read)
    ctl.update(True, CS, VM, params, False, 0.002, NS(angularVelocity=[], orientationNED=[]), False, model_data=None)
    outs.append(ctl.pid.f)
  assert outs[0] != pytest.approx(outs[1])   # carrot adds the friction torque to the fallback FF, FrogPilot does not


def lc_ci(CP):
  from opendbc.car.car_helpers import interfaces
  return interfaces[CP.carFingerprint](CP)
