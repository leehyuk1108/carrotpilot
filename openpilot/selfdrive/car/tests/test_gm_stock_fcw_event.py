"""The Traverse camera's collision alert (GM stockFcw) shows the existing forward-collision alert; other brands unchanged."""
import pytest

from openpilot.cereal import car, log
from openpilot.selfdrive.car.car_specific import CarSpecificEvents

EventName = log.OnroadEvent.EventName


def event_names(brand, stock_fcw):
  CP = car.CarParams.new_message(brand=brand, networkLocation="fwdCamera", pcmCruise=False)
  events = CarSpecificEvents(CP)
  CS = car.CarState.new_message(stockFcw=stock_fcw, vEgo=20., gearShifter="drive")
  CS.cruiseState.available = True
  return events.update(CS, car.CarState.new_message(), car.CarControl.new_message()).names


def test_gm_stock_fcw_raises_the_forward_collision_alert():
  with_alert = event_names("gm", True)
  assert EventName.fcw in with_alert
  assert EventName.stockFcw in with_alert
  without = event_names("gm", False)
  assert EventName.fcw not in without
  assert EventName.stockFcw not in without
  assert set(with_alert) - set(without) == {EventName.fcw, EventName.stockFcw}


@pytest.mark.parametrize("brand", ["ford", "honda", "hyundai", "subaru", "tesla", "toyota", "volkswagen"])
def test_other_brands_keep_stock_fcw_without_alert(brand):
  names = event_names(brand, True)
  assert EventName.stockFcw in names
  assert EventName.fcw not in names
