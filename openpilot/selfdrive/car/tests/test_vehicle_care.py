from types import SimpleNamespace as NS

import pytest

from openpilot.selfdrive.car.vehicle_care import (
  BATTERY_KEY, BATTERY_SOC_KEY, ENGINE_KEY, FUEL_LEVEL_KEY, ODOMETER_KEY, TraverseVehicleCare,
  clear_vehicle_care, decode_battery, decode_battery_soc, decode_engine, decode_fuel_level, decode_odometer,
  fill_vehicle_care, vehicle_care_snapshot,
)

# Real frames from the 11/05 drive 000001d4--3 (bus0).
ENGINE = bytes.fromhex("0027b3786900401f")    # counter 10163, oil life raw 64
FUEL = bytes.fromhex("0055000291c90000")      # fuel level raw 201
BATTERY = bytes.fromhex("c0205ee7372c")       # +24.295 A, 14.124 V
SOC = bytes.fromhex("00d0feb3")               # SOC raw 208
ODOMETER = bytes([0x00, 0x3B, 0xE8, 0x80, 0x00])  # 3,926,144 / 64 = 61,346 km


def test_decoders_read_the_documented_fields():
  assert decode_engine(ENGINE) == (10163, pytest.approx(64 * 100 / 255))
  assert decode_fuel_level(FUEL) == pytest.approx(201 / 255)
  voltage, current = decode_battery(BATTERY)
  assert voltage == pytest.approx(14.124) and current == pytest.approx(24.295)
  assert decode_battery_soc(SOC) == pytest.approx(208 * 100 / 255)
  assert decode_odometer(ODOMETER) == pytest.approx(61346.0)


def test_battery_discharge_is_negative_and_raw_sample_frame_is_skipped():
  # -312 A while cranking: raw = 0x200000 - 312000, 10.6 V.
  raw = 0x200000 - 312_000
  frame = bytes([0xC0, (raw >> 16) & 0x3F, (raw >> 8) & 0xFF, raw & 0xFF, 0x29, 0x68])
  voltage, current = decode_battery(frame)
  assert current == pytest.approx(-312.0) and voltage == pytest.approx(10.6)
  assert decode_battery(bytes([0x00]) + BATTERY[1:]) is None  # 1-in-64 raw sample
  assert decode_battery(BATTERY[:4] + bytes([0x00, 0x10])) is None  # 0.016 V is nonsense


@pytest.mark.parametrize("decoder,data", [
  (decode_engine, ENGINE[:7]), (decode_fuel_level, FUEL + b"\x00"), (decode_battery, BATTERY[:5]),
  (decode_battery_soc, SOC[:3]), (decode_odometer, ODOMETER[:4]),
  (decode_odometer, bytes([0x01]) + ODOMETER[1:]), (decode_odometer, bytes(5)),
])
def test_decoders_reject_wrong_shapes(decoder, data):
  assert decoder(data) is None


def test_one_pass_picks_bus0_sources_and_ignores_copies_on_other_buses():
  care = TraverseVehicleCare()
  now = 5_000_000_000
  frames = [(0x3F9, ENGINE, 0), (0x4D1, FUEL, 0), (0x3F3, BATTERY, 0), (0x4EF, SOC, 0), (0x120, ODOMETER, 0),
            (0x3F9, bytes(8), 2), (0x4D1, bytes(8), 2), (0x140, bytes(4), 0)]
  values = care.update([(now, frames)], now)
  assert set(values) == {ENGINE_KEY, FUEL_LEVEL_KEY, BATTERY_KEY, BATTERY_SOC_KEY, ODOMETER_KEY}
  assert values[ENGINE_KEY][0] == 10163


def test_each_source_expires_on_its_own_age_and_late_frames_do_not_win():
  care = TraverseVehicleCare()
  now = 10_000_000_000
  care.update([(now, [(0x3F9, ENGINE, 0), (0x4D1, FUEL, 0)])], now)
  newer = bytes.fromhex("0029f7746900402b")
  values = care.update([(now + 1, [(0x3F9, newer, 0)]), (now, [(0x3F9, ENGINE, 0)])], now + 1)
  assert values[ENGINE_KEY][0] == 0x29F7
  # 0x3F9 (4 Hz) expires after 2 s, 0x4D1 (2 Hz) after 3 s.
  values = care.update([], now + 2_500_000_000)
  assert ENGINE_KEY not in values and FUEL_LEVEL_KEY in values
  assert care.update([], now + 3_000_000_001) == {}
  # Future frames are ignored.
  assert care.update([(now + 9_000_000_000, [(0x4EF, SOC, 0)])], now + 4_000_000_000) == {}


def builder():
  return NS(fuelLevelValid=None, fuelLevel=None, engineValid=None, fuelUsedCounter=None, oilLifePercent=None,
            odometerValid=None, odometerKm=None, batteryValid=None, batteryVoltage=None, batteryCurrent=None,
            batterySocValid=None, batterySocPercent=None)


def all_values():
  return {FUEL_LEVEL_KEY: 0.5, ENGINE_KEY: (1234, 40.0), ODOMETER_KEY: 61000.5, BATTERY_KEY: (12.6, -1.5),
          BATTERY_SOC_KEY: 82.0}


def test_fill_writes_values_and_invalidates_everything_without_good_can():
  care = builder()
  fill_vehicle_care(care, all_values(), True)
  assert care.fuelLevelValid and care.fuelLevel == 0.5
  assert care.engineValid and care.fuelUsedCounter == 1234 and care.oilLifePercent == 40.0
  assert care.odometerValid and care.odometerKm == 61000.5
  assert care.batteryValid and care.batteryVoltage == 12.6 and care.batteryCurrent == -1.5
  assert care.batterySocValid and care.batterySocPercent == 82.0
  fill_vehicle_care(care, all_values(), False)
  assert not any([care.fuelLevelValid, care.engineValid, care.odometerValid, care.batteryValid, care.batterySocValid])
  assert care.fuelUsedCounter == 0 and care.odometerKm == 0.0
  fill_vehicle_care(care, {ODOMETER_KEY: 1.0}, True)
  assert care.odometerValid and not care.fuelLevelValid
  clear_vehicle_care(care)
  assert not care.odometerValid


def test_snapshot_rounds_valid_values_and_hides_invalid_ones():
  care = builder()
  fill_vehicle_care(care, all_values(), True)
  snapshot = vehicle_care_snapshot(NS(vehicleCare=care, canValid=True, canTimeout=False))
  assert snapshot == {"fuelLevelPercent": 50.0, "fuelLiters": 41.0, "oilLifePercent": 40, "odometerKm": 61000.5,
                      "batteryVoltage": 12.6, "batteryCurrentA": -1.5, "batterySocPercent": 82}
  assert vehicle_care_snapshot(NS(vehicleCare=care, canValid=False, canTimeout=False)) is None
  assert vehicle_care_snapshot(NS(vehicleCare=care, canValid=True, canTimeout=True)) is None
  clear_vehicle_care(care)
  assert vehicle_care_snapshot(NS(vehicleCare=care, canValid=True, canTimeout=False)) is None
  assert vehicle_care_snapshot(NS(canValid=True)) is None


def test_carstate_schema_has_vehicle_care():
  car = pytest.importorskip("openpilot.cereal").car
  cs = car.CarState.new_message()
  cs.canValid = True
  fill_vehicle_care(cs.vehicleCare, all_values(), True)
  with car.CarState.from_bytes(cs.to_bytes()) as restored:
    assert restored.vehicleCare.fuelUsedCounter == 1234
    assert restored.vehicleCare.odometerKm == 61000.5
    assert vehicle_care_snapshot(restored)["batterySocPercent"] == 82
