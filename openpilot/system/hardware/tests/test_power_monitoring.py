import pytest

from openpilot.common.params import Params
from openpilot.system.hardware.power_monitoring import PowerMonitoring, CAR_BATTERY_CAPACITY_uWh, \
                                                CAR_CHARGING_RATE_W, VBATT_PAUSE_CHARGING

# Create fake time
ssb = 0.
def mock_time_monotonic():
  global ssb
  ssb += 1.
  return ssb

TEST_DURATION_S = 50
GOOD_VOLTAGE = 12 * 1e3
VOLTAGE_BELOW_PAUSE_CHARGING = (VBATT_PAUSE_CHARGING - 1) * 1e3

def pm_patch(mocker, name, value, constant=False):
  if constant:
    mocker.patch(f"openpilot.system.hardware.power_monitoring.{name}", value)
  else:
    mocker.patch(f"openpilot.system.hardware.power_monitoring.{name}", return_value=value)


@pytest.fixture(autouse=True)
def mock_time(mocker):
  global ssb
  ssb = 0.
  mocker.patch("time.monotonic", mock_time_monotonic)


class TestPowerMonitoring:
  def setup_method(self):
    self.params = Params()

  # Test to see that it doesn't do anything when pandaState is None
  def test_panda_state_present(self):
    pm = PowerMonitoring()
    for _ in range(10):
      pm.calculate(None, None)
    assert pm.get_power_used() == 0
    assert pm.get_car_battery_capacity() == (CAR_BATTERY_CAPACITY_uWh / 10)

  # Test to see that it doesn't integrate offroad when ignition is True
  def test_offroad_ignition(self):
    pm = PowerMonitoring()
    for _ in range(10):
      pm.calculate(GOOD_VOLTAGE, True)
    assert pm.get_power_used() == 0

  # Test to see that it integrates with discharging battery
  def test_offroad_integration_discharging(self, mocker):
    POWER_DRAW = 4
    pm_patch(mocker, "HARDWARE.get_current_power_draw", POWER_DRAW)
    pm = PowerMonitoring()
    for _ in range(TEST_DURATION_S + 1):
      pm.calculate(GOOD_VOLTAGE, False)
    expected_power_usage = ((TEST_DURATION_S/3600) * POWER_DRAW * 1e6)
    assert abs(pm.get_power_used() - expected_power_usage) < 10

  # Test to check positive integration of car_battery_capacity
  def test_car_battery_integration_onroad(self, mocker):
    POWER_DRAW = 4
    pm_patch(mocker, "HARDWARE.get_current_power_draw", POWER_DRAW)
    pm = PowerMonitoring()
    pm.car_battery_capacity_uWh = 0
    for _ in range(TEST_DURATION_S + 1):
      pm.calculate(GOOD_VOLTAGE, True)
    expected_capacity = ((TEST_DURATION_S/3600) * CAR_CHARGING_RATE_W * 1e6)
    assert abs(pm.get_car_battery_capacity() - expected_capacity) < 10

  # Test to check positive integration upper limit
  def test_car_battery_integration_upper_limit(self, mocker):
    POWER_DRAW = 4
    pm_patch(mocker, "HARDWARE.get_current_power_draw", POWER_DRAW)
    pm = PowerMonitoring()
    pm.car_battery_capacity_uWh = CAR_BATTERY_CAPACITY_uWh - 1000
    for _ in range(TEST_DURATION_S + 1):
      pm.calculate(GOOD_VOLTAGE, True)
    estimated_capacity = CAR_BATTERY_CAPACITY_uWh + (CAR_CHARGING_RATE_W / 3600 * 1e6)
    assert abs(pm.get_car_battery_capacity() - estimated_capacity) < 10

  # Test to check negative integration of car_battery_capacity
  def test_car_battery_integration_offroad(self, mocker):
    POWER_DRAW = 4
    pm_patch(mocker, "HARDWARE.get_current_power_draw", POWER_DRAW)
    pm = PowerMonitoring()
    pm.car_battery_capacity_uWh = CAR_BATTERY_CAPACITY_uWh
    for _ in range(TEST_DURATION_S + 1):
      pm.calculate(GOOD_VOLTAGE, False)
    expected_capacity = CAR_BATTERY_CAPACITY_uWh - ((TEST_DURATION_S/3600) * POWER_DRAW * 1e6)
    assert abs(pm.get_car_battery_capacity() - expected_capacity) < 10

  # Test to check negative integration lower limit
  def test_car_battery_integration_lower_limit(self, mocker):
    POWER_DRAW = 4
    pm_patch(mocker, "HARDWARE.get_current_power_draw", POWER_DRAW)
    pm = PowerMonitoring()
    pm.car_battery_capacity_uWh = 1000
    for _ in range(TEST_DURATION_S + 1):
      pm.calculate(GOOD_VOLTAGE, False)
    estimated_capacity = 0 - ((1/3600) * POWER_DRAW * 1e6)
    assert abs(pm.get_car_battery_capacity() - estimated_capacity) < 10

  @pytest.mark.parametrize("minutes", [0, 60, 4320])
  @pytest.mark.parametrize("offset, expected", [(0, False), (1, True)])
  def test_max_time_offroad(self, mocker, minutes, offset, expected):
    self.params.put_int("MaxTimeOffroadMin", minutes)
    pm = PowerMonitoring()
    start_time = 10.
    mocker.patch("time.monotonic", return_value=start_time + minutes * 60 + offset)
    assert pm.should_shutdown(False, False, start_time, False) == expected

  def test_car_voltage(self, mocker):
    POWER_DRAW = 0 # To stop shutting down for other reasons
    TEST_TIME = 350
    pm_patch(mocker, "HARDWARE.get_current_power_draw", POWER_DRAW)
    self.params.put_int("MaxTimeOffroadMin", 4320)
    pm = PowerMonitoring()
    pm.car_battery_capacity_uWh = CAR_BATTERY_CAPACITY_uWh
    ignition = False
    start_time = ssb
    for i in range(TEST_TIME):
      pm.calculate(VOLTAGE_BELOW_PAUSE_CHARGING, ignition)
      if i % 10 == 0:
        assert pm.should_shutdown(ignition, True, start_time, True) == \
                          (pm.car_voltage_mV < VBATT_PAUSE_CHARGING * 1e3)
    assert pm.should_shutdown(ignition, True, start_time, True)

  @pytest.mark.parametrize("voltage, expected", [(11299, True), (11300, False), (11301, False), (11700, False)])
  def test_voltage_threshold(self, voltage, expected):
    assert VBATT_PAUSE_CHARGING == 11.3
    self.params.put_int("MaxTimeOffroadMin", 4320)
    pm = PowerMonitoring()
    pm.car_voltage_mV = voltage
    assert pm.should_shutdown(False, True, 0., False) == expected

  @pytest.mark.parametrize("elapsed", [0, 1, 59, 60, 299, 300, 3599])
  def test_no_startup_or_offroad_delay(self, mocker, elapsed):
    self.params.put_int("MaxTimeOffroadMin", 4320)
    mocker.patch("time.monotonic", return_value=elapsed)
    pm = PowerMonitoring()
    pm.car_voltage_mV = 11200
    assert pm.should_shutdown(False, False, 0., False)

  @pytest.mark.parametrize("capacity", [0, -1])
  def test_empty_estimated_capacity_does_not_shutdown(self, capacity):
    self.params.put_int("MaxTimeOffroadMin", 4320)
    pm = PowerMonitoring()
    pm.car_battery_capacity_uWh = capacity
    assert not pm.should_shutdown(False, True, 0., True)

  @pytest.mark.parametrize("key", ["DisablePowerDown", "ForcePowerDown"])
  def test_legacy_override_does_not_change_policy(self, key):
    self.params.put_bool(key, True)
    self.params.put_int("MaxTimeOffroadMin", 4320)
    pm = PowerMonitoring()
    assert not pm.should_shutdown(False, True, 0., True)
    pm.car_voltage_mV = 11200
    assert pm.should_shutdown(False, True, 0., True)
    assert not pm.should_shutdown(True, True, 0., True)

  @pytest.mark.parametrize("voltage", [10000, 12000])
  @pytest.mark.parametrize("elapsed", [1, 4320 * 60 + 1])
  def test_ignition_always_blocks_shutdown(self, mocker, voltage, elapsed):
    self.params.put_int("MaxTimeOffroadMin", 4320)
    self.params.put_bool("ForcePowerDown", True)
    mocker.patch("time.monotonic", return_value=elapsed)
    pm = PowerMonitoring()
    pm.car_voltage_mV = voltage
    assert not pm.should_shutdown(True, True, 0., True)

  def test_unknown_offroad_timestamp(self):
    pm = PowerMonitoring()
    pm.car_voltage_mV = 10000
    assert not pm.should_shutdown(False, True, None, False)

  def test_timeout_setting_updates_without_restart(self, mocker):
    self.params.put_int("MaxTimeOffroadMin", 4320)
    mocker.patch("time.monotonic", return_value=61.)
    pm = PowerMonitoring()
    assert not pm.should_shutdown(False, True, 0., True)
    self.params.put_int("MaxTimeOffroadMin", 1)
    assert pm.should_shutdown(False, True, 0., True)
