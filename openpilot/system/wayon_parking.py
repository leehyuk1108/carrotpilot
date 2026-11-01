"""Best-effort, bounded exterior photos of a low-speed parking approach.

This consumer never requests cameras, publishes vehicle data or runs inference.
Only the offroad uploader may transmit the retained JPEGs.
"""
from collections import deque
from dataclasses import dataclass
import io
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

from openpilot.system.wayon_camera import sample_fresh, snapshot_frame_fresh, snapshot_capture_time

INTERVAL_S = 2.0
HISTORY_S = 60.0
MAX_FRAMES = 24
MAX_BYTES = 8 * 1024 * 1024
MAX_FRAME_BYTES = 1024 * 1024
MAX_SELECTED = 6
MAX_UPLOAD_AGE_S = 120.0


@dataclass(frozen=True)
class ApproachFrame:
  monotonic: float
  captured_at: float
  jpeg: bytes


def approach_allowed(sm, now):
  if not all(sample_fresh(sm, name, now, max_age=1.5) for name in ("deviceState", "carState")):
    return False
  device, car = sm["deviceState"], sm["carState"]
  speed, memory = float(car.vEgo), float(device.memoryUsagePercent)
  return (bool(device.started) and str(device.thermalStatus) == "green"
          and math.isfinite(memory) and 0 <= memory < 80
          and math.isfinite(speed) and 0.2 <= abs(speed) <= 25 / 3.6
          and str(car.gearShifter) in ("drive", "reverse", "low", "sport"))


class WideReader:
  def __init__(self):
    self.client = None

  def close(self):
    self.client = None

  def __call__(self, now):
    import cv2
    import numpy as np
    from PIL import Image
    from msgq.visionipc import VisionIpcClient, VisionStreamType

    cv2.setNumThreads(1)

    if self.client is None:
      client = VisionIpcClient("camerad", VisionStreamType.VISION_STREAM_WIDE_ROAD, True)
      if not client.connect(False):
        return None
      self.client = client
    frame = self.client.recv(timeout_ms=0)
    received = time.monotonic()
    if frame is None or not snapshot_frame_fresh(self.client, received):
      return None
    if not (64 <= frame.width <= 1928 and 64 <= frame.height <= 1208):
      return None
    y = np.asarray(frame.data[:frame.uv_offset]).reshape(-1, frame.stride)[:frame.height, :frame.width]
    uv = np.asarray(frame.data[frame.uv_offset:frame.uv_offset + frame.stride * (frame.height // 2)])
    uv = uv.reshape(-1, frame.stride)[:frame.height // 2, :frame.width]
    rgb = cv2.cvtColor(np.vstack((y, uv)), cv2.COLOR_YUV2RGB_NV12)
    image = Image.fromarray(rgb)
    output = io.BytesIO()
    image.save(output, "JPEG", quality=92, subsampling=0)
    raw = output.getvalue()
    captured = float(self.client.timestamp_eof) / 1e9
    return ApproachFrame(captured, snapshot_capture_time(self.client), raw)


class ApproachBuffer:
  def __init__(self, reader=None):
    self.reader = reader or WideReader()
    self.frames = deque()
    self.bytes = 0
    self.next_sample = 0.0
    self.last_elapsed = 0.0
    self.captured_count = 0
    self.skipped_count = 0

  def clear(self):
    self.frames.clear()
    self.bytes = 0
    self.reader.close()

  def expire(self, now):
    while self.frames and not 0 <= now - self.frames[0].monotonic <= HISTORY_S:
      self.bytes -= len(self.frames.popleft().jpeg)

  def add(self, frame, now, wall):
    if (not isinstance(frame, ApproachFrame) or not isinstance(frame.jpeg, bytes)
        or not 0 < len(frame.jpeg) <= MAX_FRAME_BYTES
        or not math.isfinite(frame.monotonic) or not math.isfinite(frame.captured_at)
        or not 0 <= now - frame.monotonic <= 0.75
        or abs((wall - frame.captured_at) - (now - frame.monotonic)) > 2):
      return False
    if self.frames and (frame.monotonic - self.frames[-1].monotonic < 0.5
                        or frame.captured_at <= self.frames[-1].captured_at):
      return False
    self.expire(now)
    self.frames.append(frame)
    self.bytes += len(frame.jpeg)
    while len(self.frames) > MAX_FRAMES or self.bytes > MAX_BYTES:
      self.bytes -= len(self.frames.popleft().jpeg)
    return True

  def sample(self, sm, now=None):
    now = time.monotonic() if now is None else now
    self.expire(now)
    if not approach_allowed(sm, now):
      self.reader.close()
      # A fast stretch breaks the parking approach, even if it was brief.
      if sample_fresh(sm, "carState", now) and abs(float(sm["carState"].vEgo)) > 25 / 3.6:
        self.clear()
      if sample_fresh(sm, "deviceState", now):
        device = sm["deviceState"]
        if str(device.thermalStatus) != "green" or float(device.memoryUsagePercent) >= 85:
          self.clear()
      return
    if now < self.next_sample:
      return
    self.next_sample = now + INTERVAL_S
    began = time.monotonic()
    try:
      frame = self.reader(now)
      if self.add(frame, time.monotonic(), time.time()):
        self.captured_count += 1
    except Exception:
      self.reader.close()
      self.skipped_count += 1
    self.last_elapsed = time.monotonic() - began
    if self.last_elapsed > 0.1:
      # A slow encoder is optional work: back off, never try to catch up.
      self.next_sample = time.monotonic() + 10.0

  def take(self, now=None, wall=None):
    now = time.monotonic() if now is None else now
    wall = time.time() if wall is None else wall
    self.expire(now)
    frames = list(self.frames)
    self.clear()
    if any(abs((wall - f.captured_at) - (now - f.monotonic)) > 2 for f in frames):
      return []
    if len(frames) <= MAX_SELECTED:
      return frames
    # Keep adjacent pairs at the beginning, middle and end of the approach.
    starts = (0, (len(frames) - 2) // 2, len(frames) - 2)
    return [frames[i] for i in sorted({j for i in starts for j in (i, i + 1)})]


def upload_frames(frames, parked_at, now=None, wall=None):
  now = time.monotonic() if now is None else now
  wall = time.time() if wall is None else wall
  selected = []
  for f in frames[:MAX_SELECTED]:
    if (not 0 <= now - f.monotonic <= MAX_UPLOAD_AGE_S
        or abs((wall - f.captured_at) - (now - f.monotonic)) > 2
        or not 0.5 <= parked_at - f.captured_at <= MAX_UPLOAD_AGE_S):
      continue
    selected.append((f.captured_at, f.jpeg, "approach"))
  return selected


class ApproachSession:
  """Keep optional camera IPC/JPEG work out of the telemetry uploader process."""
  def __init__(self):
    self.process = None
    self.directory = None
    self.retry_after = 0.0
    self.started_at = 0.0
    cleanup_abandoned()

  def stop(self):
    process = self.process
    if process is not None and process.poll() is None:
      process.terminate()
      try:
        process.wait(timeout=0.25)
      except subprocess.TimeoutExpired:
        process.kill()
        try:
          process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
          return False
    self.process = None
    return True

  def clear(self):
    if not self.stop():
      return
    if self.directory is not None:
      shutil.rmtree(self.directory, ignore_errors=True)
      self.directory = None

  def sample(self, sm):
    now = time.monotonic()
    if not all(sample_fresh(sm, name, now, max_age=1.5) for name in ("deviceState", "carState")):
      self.clear()
      return
    device, car = sm["deviceState"], sm["carState"]
    if (not device.started or str(device.thermalStatus) != "green"
        or not math.isfinite(float(device.memoryUsagePercent)) or device.memoryUsagePercent >= 85
        or not math.isfinite(float(car.vEgo)) or abs(car.vEgo) > 25 / 3.6):
      self.clear()
      return
    if self.process is not None and self.process.poll() is not None:
      self.clear()
      self.retry_after = now + 30
    if self.process is not None and now - self.started_at > 10:
      try:
        heartbeat = self.directory / "heartbeat"
        alive = heartbeat.stat().st_size < 64 and 0 <= now - float(heartbeat.read_text()) <= 5
      except (OSError, ValueError):
        alive = False
      if not alive:
        self.clear()
        self.retry_after = now + 30
    if self.process is not None or now < self.retry_after or not approach_allowed(sm, now):
      return
    try:
      self.directory = Path(tempfile.mkdtemp(prefix=f"wayon-parking-{os.getpid()}-", dir="/dev/shm"))
      env = {**os.environ, "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1"}
      self.process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--worker",
                                       str(self.directory), str(os.getpid())], env=env,
                                      stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL, start_new_session=True)
      self.started_at = now
    except (OSError, ValueError):
      self.clear()
      self.retry_after = now + 30

  def take(self):
    if self.process is not None and self.process.poll() is not None:
      try:
        clean_offroad = (self.directory / "finished").read_text() == "offroad"
      except (OSError, TypeError):
        clean_offroad = False
      if not clean_offroad:
        self.clear()
        return []
    if not self.stop():
      return []
    try:
      if self.directory is None:
        return []
      manifest = self.directory / "frames.json"
      if manifest.stat().st_size > 32_768:
        return []
      rows = json.loads(manifest.read_text())
      if not isinstance(rows, list) or len(rows) > MAX_FRAMES:
        return []
      ring = ApproachBuffer()
      for row in rows:
        name = row["file"]
        if not isinstance(name, str) or len(name) != 68 or not name.endswith(".jpg"):
          return []
        digest = name[:-4]
        if any(c not in "0123456789abcdef" for c in digest):
          return []
        path = self.directory / name
        if path.is_symlink() or not 0 < path.stat().st_size <= MAX_FRAME_BYTES:
          return []
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != digest:
          return []
        mono, wall = float(row["monotonic"]), float(row["captured_at"])
        if not math.isfinite(mono) or not math.isfinite(wall):
          return []
        if ring.frames and (mono <= ring.frames[-1].monotonic or wall <= ring.frames[-1].captured_at):
          return []
        ring.frames.append(ApproachFrame(mono, wall, raw))
        ring.bytes += len(raw)
        if ring.bytes > MAX_BYTES:
          return []
      return ring.take()
    except (OSError, ValueError, KeyError, TypeError):
      return []
    finally:
      self.clear()


def write_history(directory, frames):
  rows = []
  keep = set()
  for f in frames:
    name = hashlib.sha256(f.jpeg).hexdigest() + ".jpg"
    path = directory / name
    if not path.exists():
      temporary = directory / "frame.tmp"
      temporary.write_bytes(f.jpeg)
      temporary.replace(path)
    keep.add(name)
    rows.append({"file": name, "monotonic": f.monotonic, "captured_at": f.captured_at})
  temporary = directory / "frames.tmp"
  temporary.write_text(json.dumps(rows, separators=(",", ":")))
  temporary.replace(directory / "frames.json")
  for path in directory.glob("*.jpg"):
    if path.name not in keep:
      path.unlink()


def cleanup_abandoned(root=Path("/dev/shm")):
  for directory in list(root.glob("wayon-parking-*-*"))[:100]:
    try:
      parent = int(directory.name.split("-", 3)[2])
      if (parent <= 1 or directory.is_symlink() or not directory.is_dir()
          or directory.stat().st_uid != os.getuid() or directory.stat().st_mode & 0o077):
        continue
      try:
        os.kill(parent, 0)
      except ProcessLookupError:
        shutil.rmtree(directory)
    except (OSError, ValueError):
      continue


def finish_history(directory, frames, reason):
  write_history(directory, frames if reason == "offroad" else [])
  temporary = directory / "finished.tmp"
  temporary.write_text(reason)
  temporary.replace(directory / "finished")


def history_exit_reason(sm, now):
  device_fresh = sample_fresh(sm, "deviceState", now, max_age=1.5)
  car_fresh = sample_fresh(sm, "carState", now, max_age=1.5)
  if device_fresh:
    device = sm["deviceState"]
    memory = float(device.memoryUsagePercent)
    if str(device.thermalStatus) != "green" or not math.isfinite(memory) or not 0 <= memory < 85:
      return "ineligible"
  if car_fresh:
    speed = float(sm["carState"].vEgo)
    if not math.isfinite(speed) or abs(speed) > 25 / 3.6:
      return "ineligible"
  # card normally stops offroad. Fresh device state can close an existing
  # history without needing a new car sample; it never authorizes a capture.
  if device_fresh and not sm["deviceState"].started:
    return "offroad"
  return None


def worker(directory, parent):
  import ctypes
  import signal

  # Parent death must not leave a camera consumer behind.
  if ctypes.CDLL(None).prctl(1, signal.SIGTERM, 0, 0, 0) != 0 or os.getppid() != parent:
    return
  os.nice(19)
  os.sched_setaffinity(0, {0, 1, 2, 3})
  from openpilot.cereal import messaging
  sm = messaging.SubMaster(["deviceState", "carState"])
  ring = ApproachBuffer()
  last_count = 0
  deadline = time.monotonic() + 5
  reason = "parent_gone"
  while os.getppid() == parent:
    sm.update(200)
    now = time.monotonic()
    heartbeat = directory / "heartbeat.tmp"
    heartbeat.write_text(str(now))
    heartbeat.replace(directory / "heartbeat")
    terminal = history_exit_reason(sm, now)
    if terminal:
      reason = terminal
      break
    if not all(sample_fresh(sm, name, now, max_age=1.5) for name in ("deviceState", "carState")):
      if now > deadline:
        reason = "stale"
        break
      continue
    deadline = now + 5
    ring.sample(sm, now)
    if ring.captured_count != last_count:
      write_history(directory, ring.frames)
      last_count = ring.captured_count
    time.sleep(0.2)
  finish_history(directory, ring.frames, reason)


if __name__ == "__main__" and len(sys.argv) == 4 and sys.argv[1] == "--worker":
  worker(Path(sys.argv[2]), int(sys.argv[3]))
