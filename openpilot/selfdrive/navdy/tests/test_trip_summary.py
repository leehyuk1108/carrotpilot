from datetime import datetime, timedelta
import math
from types import SimpleNamespace as NS

import pytest

from openpilot.selfdrive.navdy import bridge
from openpilot.selfdrive.navdy.trip_summary import FUEL_ML_PER_COUNT, GAP_MAX_S, RESTORE_MAX_AGE_S, SHOW_S, TripSummary
from openpilot.selfdrive.navdy.tests.test_bridge import Messages, sm, no_hardware_door_thread, dark_ambient_off  # noqa: F401

START = datetime(2026, 11, 12, 20, 43, 29)


class Clock:
  def __init__(self):
    self.t = START

  def __call__(self):
    return self.t


def care(odometer=None, counter=None):
  return NS(odometerValid=odometer is not None, odometerKm=odometer or 0.0,
            engineValid=counter is not None, fuelUsedCounter=counter or 0)


def drive(trip, clock, *, seconds, step=0.2, start_now=0.0, speed=10.0, engaged=lambda t: True,
          odometer=lambda t: None, counter=lambda t: None):
  now = start_now
  for i in range(int(round(seconds / step)) + 1):
    t = i * step
    now = start_now + t
    clock.t = START + timedelta(seconds=now)
    trip.observe(now=now, onroad=True, car_state=NS(vEgo=speed, vehicleCare=care(odometer(t), counter(t))),
                 selfdrive_state=NS(enabled=engaged(t)))
  return now


def test_normal_trip_summary_and_display_window():
  clock = Clock(); trip = TripSummary(clock)
  # 831 s, odometer 62695.172 -> 62698.906 km, 24355 counts (0.818 L), engaged 62 % of the time
  now = drive(trip, clock, seconds=831, odometer=lambda t: 62695.172 + 3.734 * t / 831,
              counter=lambda t: int(24355 * t / 831) % 65536, engaged=lambda t: t < 0.62 * 831)
  assert trip.payload(now) is None  # still driving
  clock.t = START + timedelta(seconds=now + 1)
  trip.observe(now=now + 1, onroad=False)
  summary = trip.payload(now + 1)
  assert summary == {"id": int(START.timestamp()), "distanceKm": 3.7, "durationS": 831,
                     "fuelL": round(24355 * FUEL_ML_PER_COUNT / 1000, 2), "kmPerL": round(3.734 / (24355 * FUEL_ML_PER_COUNT / 1000), 1),
                     "opPercent": 62, "startedAt": "20:43", "endedAt": "20:57"}
  assert trip.payload(now + 1 + SHOW_S - 0.01) is not None
  assert trip.payload(now + 1 + SHOW_S) is None


@pytest.mark.parametrize("seconds,speed,shown", [(30, 5.0, False), (59, 5.0, False), (61, 0.0, True), (30, 12.0, True)])
def test_very_short_trips_are_not_shown(seconds, speed, shown):
  clock = Clock(); trip = TripSummary(clock)
  now = drive(trip, clock, seconds=seconds, speed=speed)  # 30 s at 12 m/s = 0.36 km
  trip.observe(now=now + 0.2, onroad=False)
  assert (trip.payload(now + 0.2) is not None) is shown


def test_without_odometer_distance_comes_from_speed():
  clock = Clock(); trip = TripSummary(clock)
  now = drive(trip, clock, seconds=100, speed=20.0)
  trip.observe(now=now + 0.2, onroad=False)
  summary = trip.payload(now + 0.2)
  assert summary["distanceKm"] == 2.0
  assert summary["fuelL"] is None and summary["kmPerL"] is None


def test_fuel_counter_wrap_and_ecm_restart():
  trip = TripSummary(Clock())
  for now, counter in ((0, 65530), (0.2, 65535), (0.4, 4), (0.6, 10), (0.8, 30000), (1.0, 30010)):
    trip.observe(now=now, onroad=True, car_state=NS(vEgo=0, vehicleCare=care(None, counter)), selfdrive_state=None)
  assert trip.fuel_counts == 5 + 5 + 6 + 10  # the 10 -> 30000 jump is an ECM restart, not fuel


def test_small_fuel_has_no_economy():
  clock = Clock(); trip = TripSummary(clock)
  now = drive(trip, clock, seconds=70, odometer=lambda t: 100.0 + t / 70, counter=lambda t: int(t))
  trip.observe(now=now + 0.2, onroad=False)
  summary = trip.payload(now + 0.2)
  assert summary["fuelL"] == 0.0 and summary["kmPerL"] is None


def test_unknown_road_state_pauses_without_ending_or_stretching():
  clock = Clock(); trip = TripSummary(clock)
  now = drive(trip, clock, seconds=60)
  for k in range(50):  # 10 s of unknown ignition (stale device/panda)
    trip.observe(now=now + 0.2 * (k + 1), onroad=None)
  assert trip.active and trip.payload(now + 10) is None
  resumed = now + GAP_MAX_S + 10.0
  trip.observe(now=resumed, onroad=True, car_state=NS(vEgo=10.0, vehicleCare=care()), selfdrive_state=NS(enabled=True))
  assert trip.duration_s == pytest.approx(60.0)  # a gap longer than GAP_MAX_S adds nothing
  trip.observe(now=resumed + 5.0, onroad=True, car_state=NS(vEgo=10.0, vehicleCare=care()), selfdrive_state=NS(enabled=True))
  assert trip.duration_s == pytest.approx(65.0)


def test_next_ignition_drops_the_previous_summary():
  clock = Clock(); trip = TripSummary(clock)
  now = drive(trip, clock, seconds=120)
  trip.observe(now=now + 0.2, onroad=False)
  assert trip.payload(now + 0.2) is not None
  trip.observe(now=now + 5, onroad=True, car_state=NS(vEgo=0.0, vehicleCare=care()), selfdrive_state=NS(enabled=False))
  assert trip.payload(now + 5) is None and trip.duration_s == 0.0
  trip.observe(now=now + 6, onroad=False)  # a 1 s ignition cycle: too short to show
  assert trip.payload(now + 6) is None


def test_missing_messages_count_time_but_not_distance_or_engagement():
  clock = Clock(); trip = TripSummary(clock)
  for k in range(306):
    trip.observe(now=k * 0.2, onroad=True, car_state=None, selfdrive_state=None)
  trip.observe(now=61.2, onroad=False)
  summary = trip.payload(61.2)
  assert summary["durationS"] == 61 and summary["distanceKm"] == 0.0 and summary["opPercent"] == 0


def test_bridge_inputs_and_payload(sm):  # noqa: F811
  inputs = bridge.trip_inputs(sm)
  assert inputs["onroad"] is True and inputs["car_state"] is sm.data["carState"] and inputs["selfdrive_state"] is sm.data["selfdriveState"]
  sm.delay("carState", 5)
  assert bridge.trip_inputs(sm)["car_state"] is None
  sm.delay("deviceState", 5)
  assert bridge.trip_inputs(sm)["onroad"] is None  # stale device and no panda: unknown, never offroad

  class Fixed:
    def payload(self, now):
      return {"id": 1, "distanceKm": 3.7}
  assert bridge.live_payload(sm, 1, trip=Fixed())["tripSummary"] == {"id": 1, "distanceKm": 3.7}
  assert "tripSummary" not in bridge.live_payload(sm, 2, trip=NS(payload=lambda now: None))
  assert "tripSummary" not in bridge.live_payload(sm, 3)


def test_partial_odometer_keeps_the_speed_distance_elsewhere():
  clock = Clock(); trip = TripSummary(clock)
  # 100 s at 20 m/s; the odometer (matching the speed) is valid only for the second half
  now = drive(trip, clock, seconds=100, speed=20.0, odometer=lambda t: 1000.0 + 0.02 * t if t >= 50 else None)
  trip.observe(now=now + 0.2, onroad=False)
  assert trip.payload(now + 0.2)["distanceKm"] == 2.0
  trip2 = TripSummary(Clock())
  now = drive(trip2, Clock(), seconds=100, speed=20.0, odometer=lambda t: 1000.0 + 0.02 * t if t < 50 else None)
  trip2.observe(now=now + 0.2, onroad=False)
  assert trip2.payload(now + 0.2)["distanceKm"] == 2.0


def test_odometer_glitch_falls_back_to_speed():
  trip = TripSummary(Clock())
  for now, odo in ((0.0, 100.0), (0.2, 100.004), (0.4, 150.0), (0.6, 150.004)):
    trip.observe(now=now, onroad=True, car_state=NS(vEgo=20.0, vehicleCare=care(odo)), selfdrive_state=None)
  assert trip.distance_km == pytest.approx(0.004 + 0.004 + 0.004)  # the 50 km jump counts as 0.2 s at 20 m/s


def test_blocked_loop_still_counts_the_real_time():
  clock = Clock(); trip = TripSummary(clock)
  now = drive(trip, clock, seconds=40, speed=20.0)
  # a Bluetooth connect holds the bridge loop for 15 s
  now = drive(trip, clock, seconds=60, start_now=now + 15.0, speed=20.0)
  trip.observe(now=now + 0.2, onroad=False)
  summary = trip.payload(now + 0.2)
  assert summary["durationS"] == 115 and summary["distanceKm"] == 2.3 and summary["opPercent"] == 100


def test_engagement_changes_count_half_a_step():
  trip = TripSummary(Clock())
  for k, enabled in enumerate((True, True, False, False, True)):
    trip.observe(now=k * 1.0, onroad=True, car_state=None, selfdrive_state=NS(enabled=enabled))
  assert trip.engaged_s == pytest.approx(1.0 + 0.5 + 0.0 + 0.5)


def test_reversing_counts_as_distance():
  clock = Clock(); trip = TripSummary(clock)
  now = drive(trip, clock, seconds=120, speed=-2.0)
  trip.observe(now=now + 0.2, onroad=False)
  assert trip.payload(now + 0.2)["distanceKm"] == 0.2  # 0.24 km


def test_restarted_bridge_resumes_the_trip(tmp_path):
  path = tmp_path / "trip.json"
  mono = {"t": 1000.0}
  clock = Clock()
  first = TripSummary(clock, save_path=path, monotonic=lambda: mono["t"])
  now = drive(first, clock, seconds=600, speed=20.0)
  first._save(now, force=True)
  mono["t"] += 5.0  # the bridge restarts within seconds
  second = TripSummary(clock, save_path=path, monotonic=lambda: mono["t"])
  assert second.active and second.duration_s == pytest.approx(600.0)
  now = drive(second, clock, seconds=60, start_now=now + 3.0, speed=20.0)
  second.observe(now=now + 0.2, onroad=False)
  summary = second.payload(now + 0.2)
  assert summary["durationS"] == 663 and summary["distanceKm"] == 13.3 and summary["startedAt"] == "20:43"
  # and a summary being shown survives a restart too
  mono["t"] += 2.0
  third = TripSummary(clock, save_path=path, monotonic=lambda: mono["t"])
  assert third.payload(now + 5.0) == summary


def test_old_or_broken_saves_are_ignored(tmp_path):
  path = tmp_path / "trip.json"
  mono = {"t": 1000.0}
  clock = Clock()
  first = TripSummary(clock, save_path=path, monotonic=lambda: mono["t"])
  drive(first, clock, seconds=100)
  first._save(100.0, force=True)
  mono["t"] += RESTORE_MAX_AGE_S + 1
  assert not TripSummary(clock, save_path=path, monotonic=lambda: mono["t"]).active
  path.write_text("{not json")
  assert not TripSummary(clock, save_path=path).active
  path.write_text('{"saved_mono": 0}')
  broken = TripSummary(clock, save_path=path, monotonic=lambda: 10.0)
  assert not broken.active and broken.duration_s == 0.0


@pytest.mark.parametrize("kmh", [36, 72, 90, 108, 140])
def test_one_hertz_odometer_repeated_at_five_hertz(kmh):
  """The odometer updates once a second in 1/64 km steps and carState repeats it in between (vehicle_care.py)."""
  clock = Clock(); trip = TripSummary(clock)
  speed = kmh / 3.6
  odometer = lambda t: math.floor((62000.0 + speed * math.floor(t) / 1000.0) * 64) / 64
  now = drive(trip, clock, seconds=100, speed=speed, odometer=odometer)
  trip.observe(now=now + 0.2, onroad=False)
  assert trip.distance_km == pytest.approx(speed * 100 / 1000, abs=1 / 64 + 0.001)


def test_long_gap_excludes_fuel_too():
  clock = Clock(); trip = TripSummary(clock)
  now = drive(trip, clock, seconds=100, speed=20.0, counter=lambda t: int(20 * t))
  # the bridge is gone for 60 s (> GAP_MAX_S) while the engine keeps burning 20 counts/s
  now = drive(trip, clock, seconds=40, start_now=now + 60.0, speed=20.0, counter=lambda t: int(2000 + 1200 + 20 * t))
  trip.observe(now=now + 0.2, onroad=False)
  assert trip.fuel_counts == 2000 + 800 and trip.duration_s == pytest.approx(140.0)
  assert trip.payload(now + 0.2)["distanceKm"] == 2.8


def test_first_valid_odometer_after_a_delay_keeps_the_gap_distance():
  clock = Clock(); trip = TripSummary(clock)
  now = drive(trip, clock, seconds=40, speed=20.0)  # odometer not valid yet
  # the loop is held 15 s, then the odometer (1 Hz, 1/64 km) is valid from here on
  odometer = lambda t: math.floor((5000.0 + 20.0 * math.floor(t) / 1000.0) * 64) / 64
  now = drive(trip, clock, seconds=45, start_now=now + 15.0, speed=20.0, odometer=odometer)
  trip.observe(now=now + 0.2, onroad=False)
  assert trip.distance_km == pytest.approx(2.0, abs=1 / 64 + 0.001)
