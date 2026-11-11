import pytest

from openpilot.selfdrive.controls.lib.latcontrol_torque import get_low_speed_factor


@pytest.mark.parametrize("v_ego,use_nnff,expected", [
  (0., True, 144.), (10., True, 9.), (20., True, 1.), (30., True, 0.), (40., True, 0.),
  (0., False, 225.), (10., False, 169.), (20., False, 100.), (30., False, 25.),
])
def test_low_speed_factor_matches_path(v_ego, use_nnff, expected):
  assert get_low_speed_factor(v_ego, use_nnff) == pytest.approx(expected)
