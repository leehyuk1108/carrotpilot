from datetime import datetime, timedelta
from types import SimpleNamespace as NS

import pytest

from openpilot.selfdrive.navdy import bridge
from openpilot.selfdrive.navdy.trip_summary import FUEL_ML_PER_COUNT, MAX_STEP_S, SHOW_S, TripSummary
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
  resumed = now + 30.0
  trip.observe(now=resumed, onroad=True, car_state=NS(vEgo=10.0, vehicleCare=care()), selfdrive_state=NS(enabled=True))
  assert trip.duration_s == pytest.approx(60.0)  # the gap adds nothing
  trip.observe(now=resumed + 5.0, onroad=True, car_state=NS(vEgo=10.0, vehicleCare=care()), selfdrive_state=NS(enabled=True))
  assert trip.duration_s == pytest.approx(60.0 + MAX_STEP_S)


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
