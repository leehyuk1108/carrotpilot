from collections import deque
from datetime import datetime

import pytest

from openpilot.cereal import log
from openpilot.system import wayon_impactd as impactd
from openpilot.system.wayon_impact import DoorLockTracker, ImpactDetector
from openpilot.system.wayon_vehicle_events import ParkingUnlockReminder


def message(service, now, valid=True, **fields):
  event = log.Event.new_message(logMonoTime=round(now * 1e9), valid=valid)
  event.init(service).from_dict(fields)
  return event


def safety_messages(now, started=False, ignition_line=False, ignition_can=False):
  device = message("deviceState", now, started=started)
  pandas = log.Event.new_message(logMonoTime=round(now * 1e9), valid=True)
  pandas.pandaStates = [{"pandaType": "tres", "ignitionLine": ignition_line, "ignitionCan": ignition_can}]
  return device, pandas


def sensor_message(service, now, vector, sample_at=None, valid=True):
  measurement = "acceleration" if service == "accelerometer" else "gyroUncalibrated"
  return message(service, now, valid, timestamp=round((now if sample_at is None else sample_at) * 1e9),
                 **{measurement: {"v": vector}})


class Snapshot:
  """SubMaster's consumed fields, backed by actual local Cap'n Proto messages."""

  def __init__(self, messages, now):
    self.data = {}
    self.seen = {}
    self.valid = {}
    self.updated = {}
    self.logMonoTime = {}
    self.recv_time = {}
    for event in messages:
      service = event.which()
      self.data[service] = getattr(event, service)
      self.seen[service] = True
      self.valid[service] = event.valid
      self.updated[service] = True
      self.logMonoTime[service] = event.logMonoTime
      self.recv_time[service] = now
    self.on_update = None

  def __getitem__(self, service):
    return self.data[service]

  def update(self, timeout):
    assert timeout == 0
    if self.on_update:
      self.on_update()


def imu_snapshot(now, accel=(0.0, 0.0, 9.80665), gyro=(0.0, 0.0, 0.0)):
  return Snapshot([sensor_message("accelerometer", now, accel), sensor_message("gyroscope", now, gyro)], now)


def lock_frame(value, bus=0, address=0x130):
  return {"src": bus, "address": address, "dat": bytes([value])}


def can_bytes(now, *frames, valid=True):
  event = log.Event.new_message(logMonoTime=round(now * 1e9), valid=valid)
  event.can = list(frames)
  return event.to_bytes()


class Socket:
  def __init__(self, messages):
    self.messages = deque(messages)
    self.receives = 0

  def receive(self, non_blocking=False):
    assert non_blocking
    self.receives += 1
    return self.messages.popleft() if self.messages else None


@pytest.fixture
def monitor(tmp_path):
  return impactd.OffroadImpactMonitor(
    ImpactDetector(arm_delay_s=0, warmup_s=0),
    DoorLockTracker(state_path=tmp_path / "lock", max_age_s=1),
    ParkingUnlockReminder(delay_s=0.2),
  )


def update(monitor, now, frames=(), imu=None, can_complete=True, **safety_flags):
  return monitor.update(imu or imu_snapshot(now), Snapshot(safety_messages(now, **safety_flags), now),
                        list(frames), can_complete, now)


def lock_sample(now, value):
  return now, 0, 0x130, bytes([value])


def test_real_sensor_schema_vector_and_timestamp():
  imu = imu_snapshot(10)
  assert impactd.sensor_sample(imu, "accelerometer", "acceleration", 10) == ((0, 0, pytest.approx(9.80665)), 10)
  assert impactd.sensor_sample(imu, "gyroscope", "gyroUncalibrated", 10) == ((0, 0, 0), 10)
  assert impactd.sensor_vector(imu["accelerometer"], "gyroUncalibrated") is None


@pytest.mark.parametrize("sample_at", [0, 9, 10.1])
def test_sensor_rejects_old_zero_or_future_capture_even_with_fresh_envelope(sample_at):
  event = sensor_message("accelerometer", 10, [0, 0, 9.80665], sample_at=sample_at)
  imu = Snapshot([event], 10)
  assert impactd.sensor_sample(imu, "accelerometer", "acceleration", 10) is None


@pytest.mark.parametrize("vector", [[0, 0], [0, float("nan"), 9.8], [float("inf"), 0, 0]])
def test_sensor_rejects_bad_vectors(vector):
  event = sensor_message("accelerometer", 10, vector)
  assert impactd.sensor_vector(event.accelerometer, "acceleration") is None


@pytest.mark.parametrize("service", ["deviceState", "pandaStates"])
@pytest.mark.parametrize("bad_field", ["seen", "valid", "old_source", "old_receive", "future", "zero"])
def test_guard_fails_closed_for_stale_missing_invalid_state(service, bad_field):
  safety = Snapshot(safety_messages(10), 10)
  assert impactd.offroad_allowed(safety, 10)
  if bad_field in ("seen", "valid"):
    getattr(safety, bad_field)[service] = False
  elif bad_field == "old_source":
    safety.logMonoTime[service] = 1_000_000_000
  elif bad_field == "old_receive":
    safety.recv_time[service] = 1
  elif bad_field == "future":
    safety.logMonoTime[service] = 11_000_000_000
  else:
    safety.logMonoTime[service] = 0
  assert not impactd.offroad_allowed(safety, 10)


@pytest.mark.parametrize("flags", [{"started": True}, {"ignition_line": True}, {"ignition_can": True}])
def test_guard_blocks_either_onroad_or_ignition_including_transition_lag(flags):
  assert not impactd.offroad_allowed(Snapshot(safety_messages(10, **flags), 10), 10)


@pytest.mark.parametrize("pandas", [[], [{"pandaType": "unknown"}],
                                  [{"pandaType": "tres"}, {"pandaType": "tres", "ignitionCan": True}],
                                  [{"pandaType": "tres", "controlsAllowed": True}]])
def test_guard_requires_all_known_pandas_off(pandas):
  device, panda_message = safety_messages(10)
  panda_message.pandaStates = pandas
  assert not impactd.offroad_allowed(Snapshot([device, panda_message], 10), 10)


def test_nonconflated_drain_preserves_lock_edges_and_actual_can_fields(monitor):
  sock = Socket([
    can_bytes(10, lock_frame(2), lock_frame(4, bus=1)),
    can_bytes(10.1, lock_frame(4), lock_frame(0, address=0x123)),
    can_bytes(10.2, lock_frame(2)),
  ])
  samples, complete = impactd.drain_lock_can(sock, monitor.tracker, 10.3)
  assert complete
  assert samples == [lock_sample(10, 2), lock_sample(10.1, 4), lock_sample(10.2, 2)]
  assert sock.receives == 4


def test_can_drain_discards_stale_and_future_packets(monitor):
  sock = Socket([can_bytes(5, lock_frame(2)), can_bytes(11, lock_frame(2)), can_bytes(10, lock_frame(4))])
  assert impactd.drain_lock_can(sock, monitor.tracker, 10) == ([lock_sample(10, 4)], True)


def test_can_arriving_during_drain_uses_current_receipt_time(monitor, monkeypatch):
  times = iter((10, 10.01))
  monkeypatch.setattr(impactd.time, "monotonic", lambda: next(times))
  sock = Socket([can_bytes(10, lock_frame(2)), can_bytes(10.01, lock_frame(4))])
  assert impactd.drain_lock_can(sock, monitor.tracker) == ([lock_sample(10, 2), lock_sample(10.01, 4)], True)


@pytest.mark.parametrize("raw", [b"broken", can_bytes(10, lock_frame(2), valid=False),
                                b"x" * (impactd.MAX_CAN_MESSAGE_BYTES + 1)])
def test_can_drain_bad_packets_fail_closed(monitor, raw):
  assert impactd.drain_lock_can(Socket([raw]), monitor.tracker, 10) == ([], False)


@pytest.mark.parametrize("extra", [0, 1, 1000])
def test_can_message_work_bound(monitor, extra):
  sock = Socket([can_bytes(10)] * (impactd.MAX_CAN_MESSAGES + extra))
  samples, complete = impactd.drain_lock_can(sock, monitor.tracker, 10)
  assert samples == []
  assert complete == (extra == 0)
  assert sock.receives == impactd.MAX_CAN_MESSAGES + 1


def test_can_frame_and_edge_bounds(monitor):
  for frames in ([lock_frame(0, address=0x123)] * (impactd.MAX_CAN_FRAMES + 1),
                 [lock_frame(4)] * (impactd.MAX_CAN_LOCK_SAMPLES + 1)):
    assert impactd.drain_lock_can(Socket([can_bytes(10, *frames)]), monitor.tracker, 10) == ([], False)


def test_first_lock_observation_is_baseline_then_every_edge_is_retained(monitor):
  update(monitor, 10)
  events, _ = update(monitor, 10.3, [lock_sample(10.1, 4), lock_sample(10.2, 2), lock_sample(10.3, 4)])
  assert [event["locked"] for event in events] == [False, True]
  elapsed = datetime.fromisoformat(events[1]["occurredAt"]) - datetime.fromisoformat(events[0]["occurredAt"])
  assert elapsed.total_seconds() == pytest.approx(0.1)
  assert monitor.tracker.locked is True


def test_multiple_frames_in_one_can_envelope_preserve_edges(monitor):
  update(monitor, 10)
  sock = Socket([can_bytes(10.1, lock_frame(4), lock_frame(2), lock_frame(4))])
  samples, complete = impactd.drain_lock_can(sock, monitor.tracker, 10.1)
  events, _ = update(monitor, 10.1, samples, can_complete=complete)
  assert [event["locked"] for event in events] == [False, True]


def test_pre_session_and_replayed_can_do_not_create_edges(monitor):
  update(monitor, 10, [lock_sample(9.9, 2)])
  assert monitor.tracker.locked is None
  update(monitor, 10.1, [lock_sample(10.1, 4)])
  assert update(monitor, 10.2, [lock_sample(10.1, 2)])[0] == []
  assert monitor.tracker.locked is True


def test_unlocked_reminder_needs_fresh_lock_state_and_only_fires_once(monitor):
  update(monitor, 10)
  assert update(monitor, 10.1, [lock_sample(10.1, 2)])[0] == []
  events, _ = update(monitor, 10.4)
  assert [event["eventType"] for event in events] == ["parking_unlocked"]
  assert update(monitor, 10.5)[0] == []
  update(monitor, 12)
  assert monitor.tracker.locked is None
  assert not monitor.reminder.alerted
  assert update(monitor, 12.1, [lock_sample(12.1, 2)])[0] == []
  assert update(monitor, 12.4)[0][0]["eventType"] == "parking_unlocked"


def test_lock_and_unlock_in_same_batch_resets_reminder(monitor):
  update(monitor, 10)
  update(monitor, 10.1, [lock_sample(10.1, 2)])
  events, _ = update(monitor, 10.3, [lock_sample(10.2, 4), lock_sample(10.3, 2)])
  assert [event["eventType"] for event in events] == ["door_lock", "door_lock"]
  assert update(monitor, 10.4)[0] == []


@pytest.mark.parametrize("flags", [{"started": True}, {"ignition_line": True}, {"ignition_can": True}])
def test_transition_clears_all_detector_lock_and_reminder_state(monitor, flags):
  update(monitor, 10)
  update(monitor, 10.1, [lock_sample(10.1, 2)])
  update(monitor, 10.2)
  assert monitor.detector.armed
  assert update(monitor, 10.3, [lock_sample(10.3, 4)], imu=imu_snapshot(10.3, accel=(8, 0, 9.8)), **flags) == ([], None)
  assert monitor.offroad_since is None
  assert monitor.tracker.locked is None
  assert monitor.reminder.unlocked_since is None
  assert not monitor.detector.armed
  assert update(monitor, 10.4, [lock_sample(10.3, 4)])[0] == []
  assert monitor.tracker.locked is None


def test_can_overload_invalidates_lock_and_reminder(monitor):
  update(monitor, 10)
  update(monitor, 10.1, [lock_sample(10.1, 2)])
  assert update(monitor, 10.4, [lock_sample(10.4, 4)], can_complete=False) == ([], None)
  assert monitor.tracker.locked is None
  assert monitor.reminder.unlocked_since is None


def test_fresh_impact_uses_sensor_time_and_preserves_api_fields(monitor):
  update(monitor, 10)
  update(monitor, 10.01)
  _, impact = update(monitor, 10.02, imu=imu_snapshot(10.02, accel=(2.05, 0, 9.80665)))
  assert impact is not None
  assert impact["captureRequested"] is True
  assert impact["severity"] == "light"
  assert impact["sensorHz"] == 104
  assert impact["peakJerkGPerSec"] == pytest.approx(20.9, abs=0.01)
  assert set(impact) == {"id", "detectedAt", "severity", "peakDynamicG", "peakTotalG", "peakJerkGPerSec",
                         "peakGyroRadPerSec", "durationMs", "sampleCount", "sensorHz", "sensorClipped",
                         "detectorVersion", "captureRequested"}


@pytest.mark.parametrize("failure", ["accel_invalid", "gyro_invalid", "gyro_stale", "accel_capture_stale", "duplicate", "backward"])
def test_imu_invalidity_discards_impact_history(monitor, failure):
  update(monitor, 10)
  update(monitor, 10.01)
  imu = imu_snapshot(10.02, accel=(4, 0, 9.80665))
  if failure.endswith("invalid"):
    imu.valid["gyroscope" if failure.startswith("gyro") else "accelerometer"] = False
  elif failure == "gyro_stale":
    imu.logMonoTime["gyroscope"] = 9_000_000_000
  else:
    imu["accelerometer"].timestamp = round({"accel_capture_stale": 9, "duplicate": 10.01, "backward": 10.005}[failure] * 1e9)
  assert update(monitor, 10.02, imu=imu)[1] is None
  assert not monitor.detector.armed


def test_unsafe_interval_requires_full_new_warmup(monitor):
  monitor.detector.arm_delay_s = 0.2
  for index in range(5):
    update(monitor, 10 + index * 0.1)
  assert monitor.detector.armed
  update(monitor, 10.5, ignition_can=True)
  update(monitor, 10.6)
  assert not monitor.detector.armed
  assert update(monitor, 10.7, imu=imu_snapshot(10.7, accel=(4, 0, 9.8)))[1] is None
  assert not monitor.detector.armed


@pytest.mark.parametrize("kind", ["impact", "door_lock", "parking_unlocked"])
def test_queue_rechecks_ignition_immediately_before_every_write(monitor, monkeypatch, kind):
  safety = Snapshot(safety_messages(10), 10)
  update(monitor, 10)
  safety.on_update = lambda: setattr(safety["pandaStates"][0], "ignitionCan", True)
  monkeypatch.setattr(impactd.time, "monotonic", lambda: 10)
  queued = []
  assert not impactd.queue_if_offroad(safety, monitor, {"eventType": kind}, queued.append)
  assert queued == []
  assert monitor.offroad_since is None


def test_queue_allows_fresh_offroad_write(monitor, monkeypatch):
  safety = Snapshot(safety_messages(10), 10)
  update(monitor, 10)
  monkeypatch.setattr(impactd.time, "monotonic", lambda: 10)
  queued = []
  assert impactd.queue_if_offroad(safety, monitor, {"id": "event"}, queued.append)
  assert queued == [{"id": "event"}]
