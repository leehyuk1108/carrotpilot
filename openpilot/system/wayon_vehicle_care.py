"""Per-trip fuel figures and last-known vehicle care readings for Wayon Cloud.

Inputs are carState.vehicleCare (decoded in selfdrive/car/vehicle_care.py from Traverse CAN).
Everything here is display/report data; nothing feeds back into control.
"""
import json
import math
import os
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from openpilot.selfdrive.car.vehicle_care import FUEL_COUNTER_MODULO, FUEL_ML_PER_COUNT, TANK_LITERS

TRIP_FUEL_SCHEMA = "wayon-trip-fuel-v1"
# Full-throttle flow peaks near 1,000 counts/s (000001f2: 991 over a 0.25 s frame); anything
# faster between two samples is a counter restart (the ECM resets it to 0 when it wakes), not fuel.
MAX_COUNTS_PER_S = 1500.0
COUNT_SLACK = 50.0
COUNTER_HOLD_S = 0.5         # the ECM refreshes the counter at 4 Hz; allow one missed refresh
MAX_GAP_S = 40.0             # keeps the widest bound (1500 x 40 s) below one 65536 wrap
IDLE_SPEED_MPS = 0.5
LEVEL_WINDOW = 30            # samples (~3 s at the qlog carState rate) for start/end fuel level medians
MIN_OBSERVED_S = 30.0        # less engine data than this: no fuel block
MIN_ECONOMY_KM = 1.0
MIN_ECONOMY_L = 0.05

VEHICLE_CARE_SAVE_INTERVAL_S = 60.0
MIN_VALID_EPOCH = datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp()
CARE_NUMBER_KEYS = ("fuelLevelPercent", "fuelLiters", "oilLifePercent", "odometerKm", "batteryVoltage",
                    "batteryCurrentA", "batterySocPercent")


def _median(values):
  ordered = sorted(values)
  if not ordered:
    return None
  middle = len(ordered) // 2
  return ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2.0


def _finite(value):
  try:
    value = float(value)
  except (TypeError, ValueError):
    return None
  return value if math.isfinite(value) else None


class TripFuelTracker:
  """Integrates the ECM fuel-used counter over one route and keeps start/end gauge and odometer.

  Fuel and distance are counted over the same stretches: a gap in the engine record longer than
  MAX_GAP_S drops both, so km/L stays consistent even when some fuel could not be counted.
  """

  def __init__(self):
    self.prev_counter = None        # last counter value
    self.prev_change_mono = None    # when that value was first seen
    self.prev_sample_mono = None    # last engine-record sample
    self.prev_fuel_odometer = None  # odometer at the last engine-record sample
    self.used_counts = 0.0
    self.idle_counts = 0.0
    self.fuel_distance_km = 0.0
    self.counter_restarts = 0
    self.long_gaps = 0
    self.observed_s = 0.0
    self.level_start = []
    self.level_end = deque(maxlen=LEVEL_WINDOW)
    self.odometer_start = None
    self.odometer_end = None
    self.oil_life = None

  def update(self, mono, car_state):
    care = getattr(car_state, "vehicleCare", None)
    if care is None or not bool(getattr(car_state, "canValid", False)) or getattr(car_state, "canTimeout", False):
      # Skip the sample but keep the last counter: it is cumulative, so the next good sample
      # still accounts for the fuel burned meanwhile (or is caught as a restart or long gap).
      return
    odometer = None
    if care.odometerValid:
      odometer = _finite(care.odometerKm)
      if odometer is not None and odometer > 0:
        self.odometer_start = odometer if self.odometer_start is None else self.odometer_start
        self.odometer_end = odometer
      else:
        odometer = None
    if care.engineValid:
      self._update_fuel(mono, int(care.fuelUsedCounter) % FUEL_COUNTER_MODULO, odometer,
                        _finite(getattr(car_state, "vEgo", None)))
      oil = _finite(care.oilLifePercent)
      if oil is not None:
        self.oil_life = oil
    if care.fuelLevelValid:
      level = _finite(care.fuelLevel)
      if level is not None and 0.0 <= level <= 1.0:
        if len(self.level_start) < LEVEL_WINDOW:
          self.level_start.append(level)
        self.level_end.append(level)

  def _update_fuel(self, mono, counter, odometer, speed):
    if self.prev_sample_mono is None or mono - self.prev_sample_mono > MAX_GAP_S:
      if self.prev_sample_mono is not None:
        # The counter may have wrapped more than once or restarted: count neither the fuel
        # nor the distance across the gap.
        self.long_gaps += 1
      self.prev_counter, self.prev_change_mono = counter, mono
      self.prev_fuel_odometer = odometer
      self.prev_sample_mono = mono
      return
    if mono <= self.prev_sample_mono:
      return
    sample_dt = mono - self.prev_sample_mono
    self.observed_s += min(sample_dt, 1.0)
    self.prev_sample_mono = mono
    if counter != self.prev_counter:
      # Samples come at 10 Hz and repeat the 4 Hz counter, so a step may cover up to one refresh
      # period more than the sample gap; but a value that sat still for long (fuel cut, auto
      # stop) does not widen the bound, or a reset to 0 would pass as fuel.
      dt = min(mono - self.prev_change_mono, sample_dt + COUNTER_HOLD_S)
      limit = MAX_COUNTS_PER_S * dt + COUNT_SLACK
      delta = (counter - self.prev_counter) % FUEL_COUNTER_MODULO
      if delta > limit:
        # Counter restarted at 0 (ECM wake); what it shows now was burned since the restart.
        self.counter_restarts += 1
        delta = counter if counter <= limit else 0
      self.used_counts += delta
      if speed is not None and speed < IDLE_SPEED_MPS:
        self.idle_counts += delta
      self.prev_counter, self.prev_change_mono = counter, mono
    if odometer is not None:
      if self.prev_fuel_odometer is not None and odometer >= self.prev_fuel_odometer:
        self.fuel_distance_km += odometer - self.prev_fuel_odometer
      self.prev_fuel_odometer = odometer

  def summary(self):
    if self.observed_s < MIN_OBSERVED_S:
      return None
    used_l = self.used_counts * FUEL_ML_PER_COUNT / 1000.0
    idle_l = self.idle_counts * FUEL_ML_PER_COUNT / 1000.0
    fuel = {
      "schemaVersion": TRIP_FUEL_SCHEMA,
      "source": "traverseCan",
      "usedL": round(used_l, 2),
      "idleL": round(idle_l, 2),
      "idlePercent": round(idle_l / used_l * 100.0, 1) if used_l > 0 else 0.0,
      "observedS": round(self.observed_s, 1),
      "counterRestarts": self.counter_restarts,
      "longGaps": self.long_gaps,
      "tankLiters": TANK_LITERS,
      "scale": {"mlPerCount": FUEL_ML_PER_COUNT, "grade": "likely"},
    }
    if self.odometer_start is not None and self.odometer_end is not None and self.odometer_end >= self.odometer_start:
      fuel.update(odometerStartKm=round(self.odometer_start, 1), odometerEndKm=round(self.odometer_end, 1),
                  odometerDistanceKm=round(self.odometer_end - self.odometer_start, 2))
    if self.prev_fuel_odometer is not None:
      # Distance over exactly the stretches whose fuel was counted.
      fuel["distanceKm"] = round(self.fuel_distance_km, 2)
      if self.fuel_distance_km >= MIN_ECONOMY_KM and used_l >= MIN_ECONOMY_L:
        fuel["kmPerL"] = round(self.fuel_distance_km / used_l, 2)
        fuel["lPer100Km"] = round(used_l / self.fuel_distance_km * 100.0, 1)
    start, end = _median(self.level_start), _median(self.level_end)
    if start is not None and end is not None:
      fuel.update(levelStartPercent=round(start * 100.0, 1), levelEndPercent=round(end * 100.0, 1),
                  levelStartL=round(start * TANK_LITERS, 1), levelEndL=round(end * TANK_LITERS, 1))
    if self.oil_life is not None:
      fuel["oilLifePercent"] = round(self.oil_life)
    return fuel


def _iso(epoch):
  return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _epoch(value):
  try:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
  except (TypeError, ValueError):
    return None


class VehicleCareStore:
  """Last valid vehicle care readings, kept across drives so offroad heartbeats can show them."""

  def __init__(self, path, clock=time.time):
    self.path = Path(path)
    self.clock = clock
    self.values = None
    self.observed_at = None
    self.saved_at = None
    self.loaded = False

  def _load(self):
    self.loaded = True
    try:
      payload = json.loads(self.path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
      return
    if not isinstance(payload, dict):
      return
    observed_at = _epoch(payload.get("observedAt"))
    values = {key: payload[key] for key in CARE_NUMBER_KEYS
              if isinstance(payload.get(key), (int, float)) and not isinstance(payload.get(key), bool)
              and math.isfinite(payload[key])}
    if observed_at is not None and observed_at >= MIN_VALID_EPOCH and values:
      self.values, self.observed_at = values, observed_at

  def _save(self, now):
    payload = {"observedAt": _iso(self.observed_at), **self.values}
    try:
      self.path.parent.mkdir(parents=True, exist_ok=True)
      temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
      temporary.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
      os.replace(temporary, self.path)
      self.saved_at = now
    except OSError:
      pass

  def update(self, snapshot):
    """Record a fresh snapshot (or None) and return the telemetry block, or None if nothing is known."""
    if not self.loaded:
      self._load()
    now = self.clock()
    fresh = bool(snapshot) and now >= MIN_VALID_EPOCH
    if fresh:
      self.values = {**(self.values or {}), **snapshot}
      self.observed_at = now
      if self.saved_at is None or not 0 <= now - self.saved_at < VEHICLE_CARE_SAVE_INTERVAL_S:
        self._save(now)
    if not self.values or self.observed_at is None:
      return None
    # liveKeys: readings that are current right now; the others are last-known values from earlier.
    return {"fresh": fresh, "observedAt": _iso(self.observed_at), "source": "traverseCan", **self.values,
            "liveKeys": sorted(snapshot) if fresh else []}
