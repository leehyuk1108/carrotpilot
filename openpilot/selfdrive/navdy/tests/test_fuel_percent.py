from types import SimpleNamespace as NS

import pytest

from openpilot.selfdrive.navdy.tests.test_display import car, payload
from openpilot.selfdrive.navdy.transport import encode_frame


def with_fuel(valid=True, level=0.437, **kwargs):
  return car(vehicleCare=NS(fuelLevelValid=valid, fuelLevel=level), **kwargs)


def test_valid_fuel_level_is_added_without_touching_other_fields():
  base = payload()
  result = payload(car_state=with_fuel())
  assert result["fuelPercent"] == 43.7
  assert {k: v for k, v in result.items() if k != "fuelPercent"} == base
  encode_frame(result)


@pytest.mark.parametrize("level, expected", [(0.0, 0.0), (0.199, 19.9), (1.0, 100.0), (1.04, 100.0)])
def test_fuel_level_range(level, expected):
  assert payload(car_state=with_fuel(level=level))["fuelPercent"] == expected


@pytest.mark.parametrize("state", [
  car(),
  with_fuel(valid=False),
  with_fuel(level=float("nan")),
  with_fuel(level=-0.01),
  with_fuel(level=1.2),
  with_fuel(canValid=False),
  with_fuel(canTimeout=True),
])
def test_missing_or_invalid_fuel_leaves_key_absent(state):
  assert "fuelPercent" not in payload(car_state=state)


def test_fuel_is_sent_offroad_and_while_disengaged():
  assert payload(car_state=with_fuel(), onroad=False)["fuelPercent"] == 43.7
  assert payload(car_state=with_fuel(level=0.08), selfdrive_state=None, onroad=False)["fuelPercent"] == 8.0
