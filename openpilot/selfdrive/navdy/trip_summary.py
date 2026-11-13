"""Per-ignition trip summary for the Navdy post-drive screen. Display only, receive-only inputs.

While the car is onroad, distance, duration, fuel used (ECM fuel-used counter) and openpilot-engaged time are
accumulated. On the onroad-to-offroad edge the trip is frozen and sent as `tripSummary` for SHOW_S, a little
longer than the Navdy's 30 s offroad-before-sleep wait (v159), so it stays up until the HUD itself sleeps.
A new ignition starts a new trip and drops the old summary.

Time is the real elapsed time between observations, so a bridge loop held up by a blocking (re)connect does
not cut the trip short; a gap longer than GAP_MAX_S is not counted. Distance uses the odometer between two
consecutive valid samples and the speed (trapezoid, magnitude so reversing counts) everywhere else, so an
odometer that is valid for only part of the trip does not drop the rest. Unknown road state (stale device or
panda) neither ends nor restarts a trip; it is bridged like any other gap.
The state is saved to tmpfs every few seconds and picked up again by a restarted bridge.
"""
from __future__ import annotations

from datetime import datetime
import json
import math
import os
from pathlib import Path
import time

FUEL_ML_PER_COUNT = 0.0336   # ECM 0x3F9 fuel-used accumulator (vehicle_care.py)
FUEL_COUNTER_WRAP = 65536
FUEL_MAX_STEP = 2000         # larger jumps are an ECM restart (it restarts at 0), not fuel
ODOMETER_MAX_KMH = 300.0     # odometer steps faster than this are a glitch, not driving
MIN_DISTANCE_KM = 0.3        # shorter and shorter than MIN_DURATION_S: no summary
MIN_DURATION_S = 60.0
MIN_ECONOMY_FUEL_L = 0.05    # below this the km/L figure is noise
SHOW_S = 40.0
GAP_MAX_S = 20.0             # Bluetooth connect can block the loop up to 15 s; longer gaps are not counted
SAVE_PATH = Path('/dev/shm/navdy-trip-summary.json')
SAVE_EVERY_S = 5.0
RESTORE_MAX_AGE_S = 120.0    # a restarted bridge resumes only a recent save from this boot


def _finite(value) -> float | None:
  try:
    value = float(value)
  except (TypeError, ValueError):
    return None
  return value if math.isfinite(value) else None


class TripSummary:
  FIELDS = ('active', 'summary', 'summary_until', 'duration_s', 'engaged_s', 'distance_km', 'fuel_counts',
            'fuel_seen', 'last_counter', 'last_odometer', 'last_speed', 'last_enabled', 'last_now', 'started_at')

  def __init__(self, wall_clock=None, save_path: Path | None = None, monotonic=time.monotonic):
    self._wall = wall_clock or (lambda: datetime.now())
    self._monotonic = monotonic
    self._save_path = save_path
    self._saved_at = -math.inf
    self.active = False
    self.summary: dict | None = None
    self.summary_until = 0.0
    self._reset()
    if save_path is not None:
      self._restore()

  def _reset(self):
    self.duration_s = 0.0
    self.engaged_s = 0.0
    self.distance_km = 0.0
    self.fuel_counts = 0
    self.fuel_seen = False
    self.last_counter: int | None = None
    self.last_odometer: float | None = None
    self.last_speed: float | None = None
    self.last_enabled: bool | None = None
    self.last_now: float | None = None
    self.started_at: float | None = None  # wall-clock timestamp

  # ---------- persistence (tmpfs; survives a bridge restart, not a reboot) ----------

  def _restore(self):
    try:
      data = json.loads(self._save_path.read_text())
      if not 0 <= self._monotonic() - float(data['saved_mono']) <= RESTORE_MAX_AGE_S:
        return
      for name in self.FIELDS:
        setattr(self, name, data[name])
    except (OSError, ValueError, KeyError, TypeError):
      self.active = False
      self.summary = None
      self._reset()

  def _save(self, now: float, force: bool = False):
    if self._save_path is None or (not force and now - self._saved_at < SAVE_EVERY_S):
      return
    self._saved_at = now
    try:
      data = {name: getattr(self, name) for name in self.FIELDS}
      data['saved_mono'] = self._monotonic()
      temporary = self._save_path.with_suffix('.tmp')
      temporary.write_text(json.dumps(data, allow_nan=False))
      os.replace(temporary, self._save_path)
    except (OSError, ValueError, TypeError):
      pass

  # ---------- accumulation ----------

  def observe(self, *, now: float, onroad: bool | None, car_state=None, selfdrive_state=None) -> None:
    """car_state / selfdrive_state: fresh messages or None. Call once per bridge loop."""
    if onroad is True:
      if not self.active:
        self.active = True
        self.summary = None
        self._reset()
        self.started_at = self._wall().timestamp()
      self._accumulate(now, car_state, selfdrive_state)
      self._save(now)
    elif onroad is False and self.active:
      self.active = False
      self._finish(now)
      self._save(now, force=True)

  def _accumulate(self, now, car_state, selfdrive_state):
    dt = 0.0 if self.last_now is None else now - self.last_now
    if not 0.0 <= dt <= GAP_MAX_S:
      dt = 0.0  # clock went backwards or a long gap: count nothing for it, restart the baselines
      self.last_odometer = self.last_speed = self.last_enabled = None
    self.last_now = now
    self.duration_s += dt
    enabled = None if selfdrive_state is None else bool(getattr(selfdrive_state, "enabled", False))
    if enabled is not None and self.last_enabled is not None:
      self.engaged_s += dt * (int(enabled) + int(self.last_enabled)) / 2.0  # half a step when it changed
    elif enabled:
      self.engaged_s += dt
    self.last_enabled = enabled

    speed = None if car_state is None else _finite(getattr(car_state, "vEgo", None))
    speed = abs(speed) if speed is not None else None
    care = None if car_state is None else getattr(car_state, "vehicleCare", None)
    odometer = None
    if care is not None and getattr(care, "odometerValid", False):
      odometer = _finite(getattr(care, "odometerKm", None))
      odometer = odometer if odometer is not None and odometer > 0 else None
    step_km = None
    if odometer is not None and self.last_odometer is not None:
      delta = odometer - self.last_odometer
      if 0.0 <= delta <= max(dt, 0.2) * ODOMETER_MAX_KMH / 3600.0:
        step_km = delta
    if step_km is None and speed is not None:
      previous = self.last_speed if self.last_speed is not None else speed
      step_km = (speed + previous) / 2.0 * dt / 1000.0
    self.distance_km += step_km or 0.0
    self.last_odometer = odometer
    self.last_speed = speed

    if care is not None and getattr(care, "engineValid", False):
      counter = getattr(care, "fuelUsedCounter", None)
      if isinstance(counter, int) and not isinstance(counter, bool) and 0 <= counter < FUEL_COUNTER_WRAP:
        if self.last_counter is not None:
          step = (counter - self.last_counter) % FUEL_COUNTER_WRAP
          if step <= FUEL_MAX_STEP:
            self.fuel_counts += step
        self.fuel_seen = True
        self.last_counter = counter

  def _finish(self, now):
    distance_km = self.distance_km
    if distance_km < MIN_DISTANCE_KM and self.duration_s < MIN_DURATION_S:
      self.summary = None
      return
    fuel_l = self.fuel_counts * FUEL_ML_PER_COUNT / 1000.0 if self.fuel_seen else None
    economy = distance_km / fuel_l if fuel_l is not None and fuel_l >= MIN_ECONOMY_FUEL_L else None
    ended = self._wall()
    started = datetime.fromtimestamp(self.started_at) if self.started_at is not None else ended
    self.summary = {
      "id": int(started.timestamp()),
      "distanceKm": round(distance_km, 1),
      "durationS": int(round(self.duration_s)),
      "fuelL": round(fuel_l, 2) if fuel_l is not None else None,
      "kmPerL": round(economy, 1) if economy is not None else None,
      "opPercent": int(round(100.0 * self.engaged_s / self.duration_s)) if self.duration_s > 0 else 0,
      "startedAt": started.strftime("%H:%M"),
      "endedAt": ended.strftime("%H:%M"),
    }
    self.summary_until = now + SHOW_S

  def payload(self, now: float) -> dict | None:
    if self.summary is not None and not self.active and now < self.summary_until:
      return dict(self.summary)
    return None
