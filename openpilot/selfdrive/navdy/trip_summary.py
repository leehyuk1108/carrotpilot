"""Per-ignition trip summary for the Navdy post-drive screen. Display only, receive-only inputs.

While the car is onroad, distance (odometer, else integrated speed), duration, fuel used (ECM fuel-used
counter) and openpilot-engaged time are accumulated. On the onroad-to-offroad edge the trip is frozen and
sent as `tripSummary` for SHOW_S, a little longer than the Navdy's 30 s offroad-before-sleep wait (v159), so
it stays up until the HUD itself sleeps. A new ignition starts a new trip and drops the old summary.
Unknown road state (stale device/panda) neither ends nor restarts a trip.
"""
from __future__ import annotations

from datetime import datetime
import math

FUEL_ML_PER_COUNT = 0.0336   # ECM 0x3F9 fuel-used accumulator (vehicle_care.py)
FUEL_COUNTER_WRAP = 65536
FUEL_MAX_STEP = 2000         # larger jumps are an ECM restart (it restarts at 0), not fuel
MIN_DISTANCE_KM = 0.3        # shorter and shorter than MIN_DURATION_S: no summary
MIN_DURATION_S = 60.0
MIN_ECONOMY_FUEL_L = 0.05    # below this the km/L figure is noise
SHOW_S = 40.0
MAX_STEP_S = 1.0             # gaps in the bridge loop add at most this much time or distance


def _finite(value) -> float | None:
  try:
    value = float(value)
  except (TypeError, ValueError):
    return None
  return value if math.isfinite(value) else None


class TripSummary:
  def __init__(self, wall_clock=None):
    self._wall = wall_clock or (lambda: datetime.now())
    self.active = False
    self.summary: dict | None = None
    self.summary_until = 0.0
    self._reset()

  def _reset(self):
    self.duration_s = 0.0
    self.engaged_s = 0.0
    self.integrated_km = 0.0
    self.odometer_start: float | None = None
    self.odometer_end: float | None = None
    self.fuel_counts = 0
    self.fuel_seen = False
    self.last_counter: int | None = None
    self.last_now: float | None = None
    self.started_at: datetime | None = None

  def observe(self, *, now: float, onroad: bool | None, car_state=None, selfdrive_state=None) -> None:
    """car_state / selfdrive_state: fresh messages or None. Call once per bridge loop."""
    if onroad is True:
      if not self.active:
        self.active = True
        self.summary = None
        self._reset()
        self.started_at = self._wall()
      self._accumulate(now, car_state, selfdrive_state)
    elif onroad is False and self.active:
      self.active = False
      self._finish(now)
    elif onroad is None:
      self.last_now = None  # unknown: pause the clock, never stretch a step over the gap

  def _accumulate(self, now, car_state, selfdrive_state):
    dt = 0.0 if self.last_now is None else min(max(now - self.last_now, 0.0), MAX_STEP_S)
    self.last_now = now
    self.duration_s += dt
    if selfdrive_state is not None and bool(getattr(selfdrive_state, "enabled", False)):
      self.engaged_s += dt
    if car_state is None:
      return
    speed = _finite(getattr(car_state, "vEgo", None))
    if speed is not None and speed > 0:
      self.integrated_km += speed * dt / 1000.0
    care = getattr(car_state, "vehicleCare", None)
    if care is None:
      return
    if getattr(care, "odometerValid", False):
      odometer = _finite(getattr(care, "odometerKm", None))
      if odometer is not None and odometer > 0:
        if self.odometer_start is None:
          self.odometer_start = odometer
        self.odometer_end = odometer
    if getattr(care, "engineValid", False):
      counter = getattr(care, "fuelUsedCounter", None)
      if isinstance(counter, int) and not isinstance(counter, bool) and 0 <= counter < FUEL_COUNTER_WRAP:
        if self.last_counter is not None:
          step = (counter - self.last_counter) % FUEL_COUNTER_WRAP
          if step <= FUEL_MAX_STEP:
            self.fuel_counts += step
        self.fuel_seen = True
        self.last_counter = counter

  def _finish(self, now):
    odometer_km = None
    if self.odometer_start is not None and self.odometer_end is not None:
      odometer_km = self.odometer_end - self.odometer_start
    distance_km = odometer_km if odometer_km is not None and odometer_km >= 0 else self.integrated_km
    if distance_km < MIN_DISTANCE_KM and self.duration_s < MIN_DURATION_S:
      self.summary = None
      return
    fuel_l = self.fuel_counts * FUEL_ML_PER_COUNT / 1000.0 if self.fuel_seen else None
    economy = distance_km / fuel_l if fuel_l is not None and fuel_l >= MIN_ECONOMY_FUEL_L else None
    ended = self._wall()
    started = self.started_at or ended
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
