import pytest

from openpilot.common.pid import PIDController
from openpilot.selfdrive.controls.lib.nnff_pid import NNFFPIDController


@pytest.mark.parametrize("direction", [-1, 1])
def test_saturation_preserves_existing_opposing_integral(direction):
  pid = NNFFPIDController(1.0, 0.3, k_f=1.0, neg_limit=-1.0, pos_limit=1.0)
  pid.i = -0.2 * direction
  assert pid.update(0.5 * direction, feedforward=0.8 * direction) == direction
  assert pid.i == pytest.approx(-0.2 * direction)


@pytest.mark.parametrize("direction", [-1, 1])
def test_integral_can_unwind_while_output_is_still_saturated(direction):
  pid = NNFFPIDController(0.0, 0.3, k_f=1.0, neg_limit=-1.0, pos_limit=1.0)
  pid.i = 0.2 * direction
  assert pid.update(-0.1 * direction, feedforward=direction) == direction
  assert abs(pid.i) < 0.2
  assert pid.i * direction > 0.0


def test_freeze_does_not_change_integral_but_still_updates_p_and_f():
  pid = NNFFPIDController(1.7, 1.0, k_f=1.2, neg_limit=-1.0, pos_limit=1.0)
  pid.i = 0.15
  pid.update(-0.4, feedforward=0.6, freeze_integrator=True)
  assert pid.i == 0.15
  assert pid.p == pytest.approx(-0.68)
  assert pid.f == pytest.approx(0.72)


def test_common_pid_keeps_its_existing_policy():
  # NNFF opting in must not change other controllers' saturation policy.
  pid = PIDController(1.0, 0.3, k_f=1.0, neg_limit=-1.0, pos_limit=1.0)
  pid.i = 0.2
  pid.update(0.5, feedforward=0.8)
  assert pid.i == 0.0
