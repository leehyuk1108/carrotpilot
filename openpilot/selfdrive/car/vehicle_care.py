"""Receive-only Traverse maintenance readings for display and per-trip fuel reports.

All frames are ones the comma already receives on bus0. Grades come from the Traverse CAN
reverse-engineering record (2026-11-05/06, TRAVERSE_CAN_MAP.md sections 6 and 19-21):

- ECM 0x4D1 byte5: fuel level, raw/255 of a full tank (confirmed: three refuels; 4 -> 100 % took
  79 L of the 82 L AWD tank). byte1 is engine oil temperature, raw - 40 C (likely, not used here).
- ECM 0x3F9 bytes1-2: fuel-used accumulator, big-endian, wraps at 65536 and restarts at 0 when the
  ECM wakes from sleep (likely; about 33.6 uL/count fitted against the fuel level over 1.5 days,
  r=-0.964; warm idle 13 counts/s = 1.57 L/h; zero while the fuel is cut).
- ECM 0x3F9 byte6: engine oil life, raw*100/255 % (confirmed: cluster 25 % = raw 65, reset to 255).
- 0x120 bytes1-3: odometer, 1/64 km (confirmed); byte0 and byte4 are always 0.
- Battery sensor 0x3F3: bytes1-3 low 22 bits current (raw - 0x200000) mA, positive = charging
  (confirmed: cranking -305..-320 A); bytes4-5 terminal voltage in mV (likely: reads about 1.1 V
  above the panda's OBD input). byte0 bits7-6 are 11, except one 00 frame every 64 (a raw sample).
- 0x4EF byte1: battery state of charge, raw*100/255 % (confirmed by coulomb counting against 0x4EB).
"""

FUEL_LEVEL_KEY = "fuel_level"
ENGINE_KEY = "engine"
ODOMETER_KEY = "odometer"
BATTERY_KEY = "battery"
BATTERY_SOC_KEY = "battery_soc"

TANK_LITERS = 82.0  # domestic AWD Traverse
FUEL_ML_PER_COUNT = 0.0336
FUEL_COUNTER_MODULO = 65536


def decode_fuel_level(data: bytes) -> float | None:
  """Fraction of a full tank, 0..1."""
  if len(data) != 8:
    return None
  return data[5] / 255.0


def decode_engine(data: bytes) -> tuple[int, float] | None:
  """(fuel-used counter, oil life %)."""
  if len(data) != 8:
    return None
  return (data[1] << 8) | data[2], data[6] * 100.0 / 255.0


def decode_odometer(data: bytes) -> float | None:
  if len(data) != 5 or data[0] != 0 or data[4] != 0:
    return None
  raw = (data[1] << 16) | (data[2] << 8) | data[3]
  return raw / 64.0 if raw > 0 else None


def decode_battery(data: bytes) -> tuple[float, float] | None:
  """(terminal voltage V, current A with + charging); None for the raw-sample frame or nonsense."""
  if len(data) != 6 or data[0] & 0xC0 != 0xC0:
    return None
  current_a = ((((data[1] & 0x3F) << 16) | (data[2] << 8) | data[3]) - 0x200000) / 1000.0
  voltage = ((data[4] << 8) | data[5]) / 1000.0
  if not 6.0 <= voltage <= 18.0 or abs(current_a) > 1000.0:
    return None
  return voltage, current_a


def decode_battery_soc(data: bytes) -> float | None:
  if len(data) != 4:
    return None
  return data[1] * 100.0 / 255.0


ENGINE_MAX_AGE_NS = 2_000_000_000  # 0x3F9 is 4 Hz; its last value stays valid this long

# (address, bus) -> (key, decoder, max age ns). Ages allow a few missed frames at each rate.
_SOURCES = {
  (0x4D1, 0): (FUEL_LEVEL_KEY, decode_fuel_level, 3_000_000_000),  # 2 Hz
  (0x3F9, 0): (ENGINE_KEY, decode_engine, ENGINE_MAX_AGE_NS),      # 4 Hz
  (0x120, 0): (ODOMETER_KEY, decode_odometer, 3_000_000_000),      # 1 Hz
  (0x3F3, 0): (BATTERY_KEY, decode_battery, 2_000_000_000),        # 4 Hz
  (0x4EF, 0): (BATTERY_SOC_KEY, decode_battery_soc, 3_000_000_000),  # 1 Hz
}
_MAX_AGE_NS = {key: age for key, _, age in _SOURCES.values()}
_LONGEST_AGE_NS = max(_MAX_AGE_NS.values())


class TraverseVehicleCare:
  """Latest fresh decoded value per source, from one pass over each CAN batch."""

  def __init__(self):
    self._latest: dict[str, tuple[int, object]] = {}

  def update(self, can_packets, now_ns: int) -> dict:
    for sample_ns, frames in can_packets:
      if sample_ns > now_ns or now_ns - sample_ns > _LONGEST_AGE_NS:
        continue
      for address, data, bus in frames:
        source = _SOURCES.get((address, bus))
        if source is None:
          continue
        key, decode, _ = source
        previous = self._latest.get(key)
        if previous is not None and sample_ns < previous[0]:
          continue
        value = decode(data)
        if value is not None:
          self._latest[key] = (sample_ns, value)
    return {key: value for key, (sample_ns, value) in self._latest.items()
            if 0 <= now_ns - sample_ns <= _MAX_AGE_NS[key]}


def fill_vehicle_care(care, values: dict, can_ok: bool) -> None:
  """Write fresh values into carState.vehicleCare; every group is invalid without good CAN."""
  values = values if can_ok else {}
  fuel = values.get(FUEL_LEVEL_KEY)
  care.fuelLevelValid = fuel is not None
  care.fuelLevel = fuel if fuel is not None else 0.0
  engine = values.get(ENGINE_KEY)
  care.engineValid = engine is not None
  care.fuelUsedCounter, care.oilLifePercent = engine if engine is not None else (0, 0.0)
  odometer = values.get(ODOMETER_KEY)
  care.odometerValid = odometer is not None
  care.odometerKm = odometer if odometer is not None else 0.0
  battery = values.get(BATTERY_KEY)
  care.batteryValid = battery is not None
  care.batteryVoltage, care.batteryCurrent = battery if battery is not None else (0.0, 0.0)
  soc = values.get(BATTERY_SOC_KEY)
  care.batterySocValid = soc is not None
  care.batterySocPercent = soc if soc is not None else 0.0


def clear_vehicle_care(care) -> None:
  fill_vehicle_care(care, {}, False)


def vehicle_care_snapshot(car_state) -> dict | None:
  """JSON-ready valid readings from carState (rounded for display), or None when nothing is valid."""
  care = getattr(car_state, "vehicleCare", None)
  if care is None or not bool(getattr(car_state, "canValid", True)) or getattr(car_state, "canTimeout", False):
    return None
  snapshot = {}
  try:
    if care.fuelLevelValid:
      snapshot["fuelLevelPercent"] = round(float(care.fuelLevel) * 100.0, 1)
      snapshot["fuelLiters"] = round(float(care.fuelLevel) * TANK_LITERS, 1)
    if care.engineValid:
      snapshot["oilLifePercent"] = round(float(care.oilLifePercent))
    if care.odometerValid:
      snapshot["odometerKm"] = round(float(care.odometerKm), 1)
    if care.batteryValid:
      snapshot["batteryVoltage"] = round(float(care.batteryVoltage), 2)
      snapshot["batteryCurrentA"] = round(float(care.batteryCurrent), 1)
    if care.batterySocValid:
      snapshot["batterySocPercent"] = round(float(care.batterySocPercent))
  except (AttributeError, TypeError, ValueError):
    return None
  return snapshot or None
