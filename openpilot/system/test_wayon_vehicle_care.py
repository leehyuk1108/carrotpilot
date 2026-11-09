import json
from datetime import datetime, timezone
from types import SimpleNamespace as NS

import pytest

from openpilot.system.wayon_vehicle_care import (
  MAX_GAP_S, MIN_OBSERVED_S, TRIP_FUEL_SCHEMA, VEHICLE_CARE_SAVE_INTERVAL_S, TripFuelTracker, VehicleCareStore,
)

ML_PER_COUNT = 0.0336


def car(counter=None, speed=10.0, level=None, odometer=None, oil=25.1, can_valid=True):
  care = NS(engineValid=counter is not None, fuelUsedCounter=counter or 0, oilLifePercent=oil,
            fuelLevelValid=level is not None, fuelLevel=level or 0.0,
            odometerValid=odometer is not None, odometerKm=odometer or 0.0)
  return NS(vehicleCare=care, vEgo=speed, canValid=can_valid, canTimeout=False)


def drive(tracker, start_mono, seconds, counts_per_s, *, counter, speed=10.0, level=None, odometer=None,
          km_per_s=0.0, rate_hz=10):
  """Feed a steady stretch; returns the counter and odometer at the end."""
  steps = int(seconds * rate_hz)
  for step in range(1, steps + 1):
    t = step / rate_hz
    value = int(counter + counts_per_s * t) % 65536
    odo = odometer + km_per_s * t if odometer is not None else None
    tracker.update(start_mono + t, car(value, speed, level, odo))
  end_odo = odometer + km_per_s * seconds if odometer is not None else None
  return int(counter + counts_per_s * seconds) % 65536, end_odo


def test_steady_drive_economy_idle_share_levels_and_odometer():
  tracker = TripFuelTracker()
  tracker.update(100.0, car(1000, 10.0, 0.80, 61000.0))
  # 10 minutes moving at 200 counts/s and 10 km, then 2 minutes idle at 13 counts/s (warm idle).
  counter, odo = drive(tracker, 100.0, 600, 200.0, counter=1000, level=0.80, odometer=61000.0, km_per_s=10 / 600)
  drive(tracker, 700.0, 120, 13.0, counter=counter, speed=0.0, level=0.78, odometer=odo)
  fuel = tracker.summary()
  moving_l = 600 * 200 * ML_PER_COUNT / 1000
  idle_l = 120 * 13 * ML_PER_COUNT / 1000
  assert fuel["schemaVersion"] == TRIP_FUEL_SCHEMA
  assert fuel["usedL"] == pytest.approx(moving_l + idle_l, abs=0.02)
  assert fuel["idleL"] == pytest.approx(idle_l, abs=0.01)
  assert fuel["distanceKm"] == pytest.approx(10.0, abs=0.01)
  assert fuel["kmPerL"] == pytest.approx(10.0 / (moving_l + idle_l), rel=0.01)
  assert fuel["lPer100Km"] == pytest.approx((moving_l + idle_l) / 10 * 100, rel=0.01)
  assert fuel["levelStartPercent"] == 80.0 and fuel["levelEndPercent"] == 78.0
  assert fuel["levelStartL"] == pytest.approx(65.6) and fuel["oilLifePercent"] == 25
  assert fuel["counterRestarts"] == 0 and fuel["longGaps"] == 0
  assert fuel["observedS"] == pytest.approx(720.0, abs=0.2)


def test_4hz_counter_sampled_at_10hz_keeps_heavy_fuel_flow():
  # The ECM refreshes 0x3F9 every 0.25 s; qlog carState repeats the last value at 10 Hz.
  tracker = TripFuelTracker()
  for step in range(600):  # 60 s at 600 counts/s (full throttle)
    t = step / 10
    counter = int(600 * (int(t * 4) / 4)) % 65536
    tracker.update(t, car(counter))
  assert tracker.counter_restarts == 0
  assert tracker.used_counts == pytest.approx(600 * 59.75, abs=1)


def test_real_full_throttle_peak_is_fuel():
  # 000001f2 hard acceleration: 0x3F9 rose 219-248 counts per 0.25 s frame, first seen 0.2 s apart in qlog.
  tracker = TripFuelTracker()
  counter = 65022
  tracker.update(0.0, car(counter))
  for step, delta in enumerate((219, 231, 239, 248, 247, 241), start=1):
    counter = (counter + delta) % 65536
    tracker.update(step * 0.2, car(counter))
  assert tracker.counter_restarts == 0 and tracker.used_counts == 219 + 231 + 239 + 248 + 247 + 241
  assert 1500 * MAX_GAP_S < 65536


def test_reset_after_a_long_still_counter_is_not_fuel():
  # Counter held at 50000 for 30 s (fuel cut / auto stop) while samples keep coming, then the ECM resets it.
  tracker = TripFuelTracker()
  for step in range(301):
    tracker.update(step / 10, car(50000, 0.0))
  tracker.update(30.1, car(0, 0.0))
  tracker.update(30.35, car(13, 0.0))
  assert tracker.counter_restarts == 1
  assert tracker.used_counts == 13 and tracker.idle_counts == 13


def test_long_engine_gap_drops_its_distance_too():
  tracker = TripFuelTracker()
  t, counter, odo = 0.0, 1000, 61000.0
  for step in range(2400):  # 240 s, 20 m/s, 0.0336 mL x 100 counts/s; engine record missing for 120 s
    t = step / 10
    counter = 1000 + int(100 * t)
    odo = 61000.0 + 0.02 * t
    engine = None if 60 <= t < 180 else counter
    tracker.update(t, car(engine, 20.0, odometer=odo))
  fuel = tracker.summary()
  assert fuel["longGaps"] == 1
  assert fuel["odometerDistanceKm"] == pytest.approx(4.8, abs=0.01)
  assert fuel["distanceKm"] == pytest.approx(2.4, abs=0.01)  # only the stretches whose fuel was counted
  assert fuel["usedL"] == pytest.approx(120 * 100 * ML_PER_COUNT / 1000, abs=0.01)
  assert fuel["kmPerL"] == pytest.approx(0.02 / (100 * ML_PER_COUNT / 1000), rel=0.02)


def test_wrap_is_fuel_but_a_restart_is_not_a_huge_jump():
  tracker = TripFuelTracker()
  tracker.update(0.0, car(65500))
  tracker.update(0.25, car(40))        # wrapped: 76 counts
  tracker.update(0.50, car(100))       # +60
  tracker.update(0.75, car(10))        # ECM restart at 0: 10 counts since
  tracker.update(1.00, car(70))        # +60
  assert tracker.used_counts == 76 + 60 + 10 + 60
  assert tracker.counter_restarts == 1


def test_bad_can_and_missing_frames_do_not_lose_cumulative_fuel():
  tracker = TripFuelTracker()
  tracker.update(0.0, car(1000))
  tracker.update(1.0, car(1200, can_valid=False))   # ignored
  tracker.update(2.0, car(None))                    # engine frame stale
  tracker.update(5.0, car(2000))                    # 1000 counts over 5 s is plausible
  assert tracker.used_counts == 1000 and tracker.observed_s == 1.0


def test_long_gap_resyncs_without_guessing():
  tracker = TripFuelTracker()
  tracker.update(0.0, car(1000))
  tracker.update(MAX_GAP_S + 1.0, car(50000))
  tracker.update(MAX_GAP_S + 1.25, car(50050))
  assert tracker.used_counts == 50 and tracker.long_gaps == 1


def test_out_of_order_samples_are_ignored():
  tracker = TripFuelTracker()
  tracker.update(10.0, car(1000))
  tracker.update(9.0, car(900))
  tracker.update(10.25, car(1050))
  assert tracker.used_counts == 50


def test_no_block_without_enough_engine_data_or_for_old_logs():
  tracker = TripFuelTracker()
  drive(tracker, 0.0, MIN_OBSERVED_S - 1, 100.0, counter=0)
  assert tracker.summary() is None
  old = TripFuelTracker()
  for i in range(600):
    old.update(i * 0.1, NS(vEgo=5.0, canValid=True, canTimeout=False))  # pre-vehicleCare carState
  assert old.summary() is None


def test_short_trip_has_no_economy_figure():
  tracker = TripFuelTracker()
  tracker.update(0.0, car(0, odometer=100.0))
  drive(tracker, 0.0, 60, 50.0, counter=0, odometer=100.0, km_per_s=0.5 / 60)
  fuel = tracker.summary()
  assert fuel["distanceKm"] == pytest.approx(0.5, abs=0.02)
  assert "kmPerL" not in fuel and "lPer100Km" not in fuel


class Clock:
  def __init__(self, epoch):
    self.now = epoch

  def __call__(self):
    return self.now


NOW = datetime(2026, 11, 9, 8, 0, tzinfo=timezone.utc).timestamp()


def test_store_reports_fresh_then_last_known_and_survives_restart(tmp_path):
  path = tmp_path / "vehicle_care.json"
  clock = Clock(NOW)
  store = VehicleCareStore(path, clock)
  assert store.update(None) is None
  block = store.update({"fuelLevelPercent": 50.0, "oilLifePercent": 25})
  assert block == {"fresh": True, "observedAt": "2026-11-09T08:00:00Z", "source": "traverseCan",
                   "fuelLevelPercent": 50.0, "oilLifePercent": 25, "liveKeys": ["fuelLevelPercent", "oilLifePercent"]}
  clock.now += 5
  block = store.update({"batterySocPercent": 80})  # merges with earlier groups, but only SOC is live
  assert block["fuelLevelPercent"] == 50.0 and block["batterySocPercent"] == 80
  assert block["liveKeys"] == ["batterySocPercent"]
  clock.now += 3600
  block = store.update(None)
  assert block["fresh"] is False and block["observedAt"] == "2026-11-09T08:00:05Z" and block["liveKeys"] == []
  # Saved at the first observation only (the next save waits for the interval).
  saved = json.loads(path.read_text())
  assert saved == {"observedAt": "2026-11-09T08:00:00Z", "fuelLevelPercent": 50.0, "oilLifePercent": 25}
  restarted = VehicleCareStore(path, clock)
  assert restarted.update(None)["oilLifePercent"] == 25


def test_store_saves_on_interval_and_ignores_bad_clock_and_bad_file(tmp_path):
  path = tmp_path / "vehicle_care.json"
  clock = Clock(NOW)
  store = VehicleCareStore(path, clock)
  store.update({"odometerKm": 61000.0})
  clock.now += VEHICLE_CARE_SAVE_INTERVAL_S
  store.update({"odometerKm": 61001.0})
  assert json.loads(path.read_text())["odometerKm"] == 61001.0
  # An unset device clock (1970) is not recorded.
  early = VehicleCareStore(tmp_path / "other.json", Clock(1000.0))
  assert early.update({"odometerKm": 1.0}) is None and not (tmp_path / "other.json").exists()
  path.write_text('{"observedAt": "2026-11-09T08:00:00Z", "odometerKm": "x", "oilLifePercent": true}')
  assert VehicleCareStore(path, clock).update(None) is None
  path.write_text("not json")
  assert VehicleCareStore(path, clock).update(None) is None
