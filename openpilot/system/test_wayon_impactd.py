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


def door_sample(now, opened=False):
  # Actual Traverse BCMDoorBeltStatus from route 1aa, rear-right door.
  return now, 0, 0x12A, bytes.fromhex("30e0f06e00001085" if opened else "30e0706e00001085")


def guarded_monitor(tmp_path, arm_delay_s=0, settle_s=0):
  return impactd.OffroadImpactMonitor(
    ImpactDetector(arm_delay_s=arm_delay_s, warmup_s=0),
    DoorLockTracker(required=True, require_doors_closed=True, arm_delay_s=settle_s,
                    state_path=tmp_path / "guard-lock", max_age_s=1),
    ParkingUnlockReminder(),
  )


def test_traverse_defaults_enable_both_guards_without_affecting_other_vehicles(tmp_path):
  config = {"impact_lock_state_path": str(tmp_path / "lock")}
  tracker = impactd.lock_tracker_from_config(config, "CHEVROLET_TRAVERSE")
  assert tracker.required and tracker.require_doors_closed
  for fingerprint in ("", "CHEVROLET_BOLT_EUV", "HYUNDAI_IONIQ_5"):
    tracker = impactd.lock_tracker_from_config(config, fingerprint)
    assert not tracker.required and not tracker.require_doors_closed
  config.update(impact_require_door_locked="false", impact_require_doors_closed=False)
  tracker = impactd.lock_tracker_from_config(config, "CHEVROLET_TRAVERSE")
  assert not tracker.required and not tracker.require_doors_closed


def test_drain_includes_real_door_frames_only_on_receive_bus(tmp_path):
  tracker = guarded_monitor(tmp_path).tracker
  door = door_sample(10, True)[3]
  frames = [lock_frame(4), {"src": 0, "address": 0x12A, "dat": door},
            {"src": 130, "address": 0x12A, "dat": door}]
  samples, complete = impactd.drain_lock_can(Socket([can_bytes(10, *frames)]), tracker, 10)
  assert complete
  assert samples == [lock_sample(10, 4), door_sample(10, True)]


@pytest.mark.parametrize("frames,state", [
  ([], "waitingForDoorState"),
  ([lock_sample(10, 4)], "waitingForDoorState"),
  ([door_sample(10)], "waitingForLockState"),
  ([lock_sample(10, 2), door_sample(10)], "pausedUnlocked"),
  ([lock_sample(10, 4), door_sample(10, True)], "pausedDoorOpen"),
])
def test_door_jolt_blocked_when_unlocked_open_or_unknown(tmp_path, frames, state):
  monitor = guarded_monitor(tmp_path)
  update(monitor, 10, frames)
  update(monitor, 10.01)
  assert update(monitor, 10.02, imu=imu_snapshot(10.02, accel=(8, 0, 9.8)))[1] is None
  assert monitor.pending_impact is None
  assert not monitor.detector.armed
  assert impactd.operating_state(monitor.detector, monitor.tracker, False, 10.02) == state


@pytest.mark.parametrize("byte,mask", [(1, 1), (1, 2), (1, 4), (2, 128)])
def test_each_of_four_door_switches_blocks_even_if_locked(tmp_path, byte, mask):
  monitor = guarded_monitor(tmp_path)
  update(monitor, 10, [lock_sample(10, 4), door_sample(10)])
  update(monitor, 10.01)
  data = bytearray(door_sample(10)[3]); data[byte] |= mask
  events, impact = update(monitor, 10.02, [(10.02, 0, 0x12A, bytes(data))],
                          imu=imu_snapshot(10.02, accel=(8, 0, 9.8)))
  assert not events and impact is None
  assert monitor.tracker.door_open is True
  assert not monitor.detector.armed


@pytest.mark.parametrize("edge", ["unlock", "open", "invalid_door"])
def test_late_door_edge_cancels_pending_impact_before_upload(tmp_path, edge):
  monitor = guarded_monitor(tmp_path)
  update(monitor, 10, [lock_sample(10, 4), door_sample(10)])
  update(monitor, 10.01)
  assert update(monitor, 10.02, imu=imu_snapshot(10.02, accel=(2.05, 0, 9.80665)))[1] is None
  assert monitor.pending_impact is not None
  for n in range(3, 45):
    assert update(monitor, 10 + n * 0.01)[1] is None
  frames = {"unlock": [lock_sample(10.45, 2)], "open": [door_sample(10.45, True)],
            "invalid_door": [(10.45, 0, 0x12A, b"\x00")]}[edge]
  update(monitor, 10.45, frames)
  assert monitor.pending_impact is None
  for n in range(46, 70):
    assert update(monitor, 10 + n * 0.01)[1] is None


def test_genuine_locked_closed_impact_survives_confirmation_with_original_timestamp(tmp_path):
  monitor = guarded_monitor(tmp_path)
  update(monitor, 10, [lock_sample(10, 4), door_sample(10)])
  update(monitor, 10.01)
  assert update(monitor, 10.02, imu=imu_snapshot(10.02, accel=(2.05, 0, 9.80665)))[1] is None
  expected = monitor.pending_impact[1].copy()
  emitted = []
  for n in range(3, 80):
    _, impact = update(monitor, 10 + n * 0.01)
    if impact: emitted.append(impact)
  assert emitted == [expected]
  assert expected["captureRequested"]


@pytest.mark.parametrize("failure", ["stale_lock", "stale_door", "can_overload", "ignition"])
def test_pending_impact_never_survives_lost_guard(tmp_path, failure):
  monitor = guarded_monitor(tmp_path)
  update(monitor, 10, [lock_sample(10, 4), door_sample(10)])
  update(monitor, 10.01)
  update(monitor, 10.02, imu=imu_snapshot(10.02, accel=(2.05, 0, 9.80665)))
  assert monitor.pending_impact
  if failure == "stale_lock": monitor.tracker.last_sample_at = 8
  if failure == "stale_door": monitor.tracker.last_door_sample_at = 8
  assert update(monitor, 10.03, can_complete=failure != "can_overload", ignition_can=failure == "ignition")[1] is None
  assert monitor.pending_impact is None
  assert not monitor.detector.armed


def test_door_open_close_in_same_batch_requires_new_warmup(tmp_path):
  monitor = guarded_monitor(tmp_path, arm_delay_s=0.2, settle_s=0.1)
  update(monitor, 10, [lock_sample(10, 4), door_sample(10)])
  for n in range(1, 50): update(monitor, 10 + n * 0.01)
  assert monitor.detector.armed
  frames = [door_sample(10.5, True), door_sample(10.51)]
  update(monitor, 10.51, frames)
  assert not monitor.detector.armed
  for n in range(52, 80):
    assert update(monitor, 10 + n * 0.01)[1] is None
    assert not monitor.detector.armed
  for n in range(80, 90): update(monitor, 10 + n * 0.01)
  assert monitor.detector.armed


def test_guard_reset_happens_even_without_new_accel_sample(tmp_path):
  monitor = guarded_monitor(tmp_path)
  update(monitor, 10, [lock_sample(10, 4), door_sample(10)])
  update(monitor, 10.01)
  assert monitor.detector.armed
  imu = imu_snapshot(10.02); imu.updated["accelerometer"] = False
  update(monitor, 10.02, [door_sample(10.02, True)], imu=imu)
  assert not monitor.detector.armed


def test_old_lock_and_door_files_are_not_reused_after_process_restart(tmp_path):
  monitor = guarded_monitor(tmp_path)
  update(monitor, 10, [lock_sample(10, 4), door_sample(10)])
  restarted = guarded_monitor(tmp_path)
  assert restarted.tracker.locked is None and restarted.tracker.door_open is None
  assert not restarted.tracker.detection_allowed(11)


@pytest.mark.parametrize("failure", ["duplicate", "gap", "invalid"])
def test_pending_impact_is_discarded_when_imu_continuity_breaks(tmp_path, failure):
  monitor = guarded_monitor(tmp_path)
  update(monitor, 10, [lock_sample(10, 4), door_sample(10)])
  update(monitor, 10.01)
  update(monitor, 10.02, imu=imu_snapshot(10.02, accel=(2.05, 0, 9.80665)))
  assert monitor.pending_impact
  now = 10.53 if failure == "gap" else 10.03
  imu = imu_snapshot(now)
  if failure == "duplicate": imu["accelerometer"].timestamp = 10_020_000_000
  if failure == "invalid": imu.valid["gyroscope"] = False
  assert update(monitor, now, imu=imu)[1] is None
  assert monitor.pending_impact is None


@pytest.mark.parametrize("edge", ["unlock", "open", "expire"])
def test_queue_rechecks_door_guard_without_blocking_lock_notifications(tmp_path, monkeypatch, edge):
  monitor = guarded_monitor(tmp_path)
  update(monitor, 10, [lock_sample(10, 4), door_sample(10)])
  now = 12 if edge == "expire" else 10.1
  safety = Snapshot(safety_messages(now), now)
  if edge == "unlock": monitor.tracker.update(0, 0x130, b"\x02", now)
  if edge == "open": monitor.tracker.update(*door_sample(now, True)[1:], now)
  monkeypatch.setattr(impactd.time, "monotonic", lambda: now)
  queued = []
  assert not impactd.queue_if_offroad(safety, monitor, {"id": "impact"}, queued.append, impact=True)
  assert queued == []
  assert impactd.queue_if_offroad(safety, monitor, {"eventType": "door_lock"}, queued.append)
  assert queued == [{"eventType": "door_lock"}]
