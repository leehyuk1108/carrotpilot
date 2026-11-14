#!/usr/bin/env python3
"""Download, check and compile the selected internal driving model while parked (manager: only_offroad).

Nothing here touches the stock model. The compiled artifact is written next to the downloaded ONNX outside the
git checkout and only becomes usable (compiled.json) after the compiler validated it; modeld decides at its next
start (internal_model.select_for_modeld).
"""
from __future__ import annotations

import ctypes
import hashlib
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from openpilot.selfdrive.modeld import internal_model as im

POLL_SECONDS = 2.0
NETWORK_RETRY_SECONDS = 30.0
COMPILE_TIMEOUT_SECONDS = 45 * 60
STATUS_INTERVAL_SECONDS = 1.0
RETRYABLE = {"network", "waiting_offroad", "build"}
MODELD_DIR = Path(__file__).resolve().parent
QCOM_FLAGS = {"DEV": "QCOM", "IMAGE": "1", "FLOAT16": "1", "NOLOCALS": "1", "JIT_BATCH_SIZE": "0", "OPENPILOT_HACKS": "1"}


class Stop(Exception):
  pass


class StepError(Exception):
  def __init__(self, code: str, detail: str):
    super().__init__(detail)
    self.code, self.detail = code, detail


def camera_resolutions() -> list[tuple[int, int]]:
  """The road camera resolution of this device: mici (comma 4) or tici/tizi (comma 3/3X)."""
  from openpilot.common.transformations.camera import _ar_ox_fisheye, _os_fisheye
  from openpilot.system.hardware import HARDWARE
  device = HARDWARE.get_device_type()
  if device == "mici":
    return [(_os_fisheye.width, _os_fisheye.height)]
  if device in ("tici", "tizi"):
    return [(_ar_ox_fisheye.width, _ar_ox_fisheye.height)]
  return [(_ar_ox_fisheye.width, _ar_ox_fisheye.height), (_os_fisheye.width, _os_fisheye.height)]


COMPILE_CORE = 7


def compile_core_available() -> bool:
  """Whether the stock build's isolated core can be used. Parked, the comma takes cores 4-7 offline, and the
  manager's processes may not be allowed on it either; then the compiler runs on the allowed cores."""
  try:
    online = Path(f"/sys/devices/system/cpu/cpu{COMPILE_CORE}/online").read_text().strip() == "1"
    return online and COMPILE_CORE in os.sched_getaffinity(0) and Path("/usr/bin/taskset").is_file()
  except (OSError, AttributeError):
    return False


PART_NAME = "patch_part.onnx"


def build_patched(model: im.InternalModel, part: Path, target: Path) -> None:
  """Write the ONNX of a WeightPatch model: the stock ONNX with the copies from the verified part applied. The
  stock ONNX and the result must match their pinned size and sha256, so nothing else is ever compiled."""
  patch = model.patch
  assert patch is not None
  template = im.stock_onnx_path()
  try:
    with template.open("rb") as f:
      data = bytearray(f.read())
  except OSError as e:
    raise StepError("template", f"stock model unreadable: {e}") from e
  if len(data) != patch.template_size or hashlib.sha256(data).hexdigest() != patch.template_sha256:
    raise StepError("template", "the stock model differs from the one this model is built from")
  part_data = part.read_bytes()
  if len(part_data) != patch.part_size or hashlib.sha256(part_data).hexdigest() != patch.part_sha256:
    raise StepError("sha", "patch part sha256 mismatch")
  for dst, src, n in patch.copies:
    if n <= 0 or dst < 0 or src < 0 or dst + n > len(data) or src + n > len(part_data):
      raise StepError("contract", f"patch copy out of range: {dst} {src} {n}")
    data[dst:dst + n] = part_data[src:src + n]
  if len(data) != model.size or hashlib.sha256(data).hexdigest() != model.sha256:
    raise StepError("sha", "patched model sha256 mismatch")
  tmp = target.with_name(target.name + ".tmp")
  try:
    with tmp.open("wb") as f:
      f.write(data)
      f.flush()
      os.fsync(f.fileno())
    os.replace(tmp, target)
  except OSError as e:
    tmp.unlink(missing_ok=True)
    if getattr(e, "errno", None) == 28:
      raise StepError("storage", str(e)) from e
    raise


def _set_parent_death_signal() -> None:
  # The manager stops this process when the car turns on; the compiler must not outlive it onto the GPU.
  try:
    ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGKILL)  # PR_SET_PDEATHSIG
  except Exception:
    pass


class Manager:
  def __init__(self, params: Any | None = None, cameras: list[tuple[int, int]] | None = None):
    self.params = params
    self._cameras = cameras
    self.child: subprocess.Popen | None = None
    self.stopping = False
    self.last_status_write = 0.0
    self.retry_at: dict[int, float] = {}

  # --- state -----------------------------------------------------------------------------------------------
  @property
  def cameras(self) -> list[tuple[int, int]]:
    if self._cameras is None:
      self._cameras = camera_resolutions()
    return self._cameras

  def onroad(self) -> bool:
    if self.params is None:
      return False
    try:
      return bool(self.params.get_bool("IsOnroad"))
    except Exception:
      return False

  def check_continue(self) -> None:
    if self.stopping:
      raise Stop()
    if self.onroad():
      raise StepError("waiting_offroad", "ignition on")

  def status(self, model: im.InternalModel, token: int, phase: str, force: bool = True, **extra: Any) -> None:
    now = time.monotonic()
    if not force and now - self.last_status_write < STATUS_INTERVAL_SECONDS:
      return
    self.last_status_write = now
    previous = im.read_json(im.status_path())
    started = previous.get("started_at") if previous.get("token") == token and previous.get("phase") == phase else None
    im.write_json(im.status_path(), {
      "model_id": model.id, "sha256": model.sha256, "token": token, "phase": phase,
      "started_at": started or time.time(), "updated_at": time.time(), **extra,
    })

  # --- pipeline --------------------------------------------------------------------------------------------
  def download(self, model: im.InternalModel, token: int) -> Path:
    from openpilot.selfdrive.modeld.big_model import BigModelManifest, _download_model
    target = im.onnx_path(model)
    if target.is_file() and target.stat().st_size == model.size and im.read_json(im.model_dir(model) / "verified.json").get("sha256") == model.sha256:
      return target
    patch = model.patch
    if patch is None:
      manifest = BigModelManifest(model_id=model.id, filename=im.ONNX_NAME, size=model.size, sha256=model.sha256, url=str(model.url))
    else:
      manifest = BigModelManifest(model_id=model.id, filename=PART_NAME, size=patch.part_size, sha256=patch.part_sha256, url=str(model.url))

    def progress(_manifest, done: int, total: int) -> None:
      self.check_continue()
      self.status(model, token, "downloading", force=False, downloaded_bytes=done, total_bytes=total)

    def phase(name: str, _manifest) -> None:
      self.status(model, token, name, downloaded_bytes=manifest.size, total_bytes=manifest.size)

    try:
      path = _download_model(manifest, im.model_dir(model), progress_callback=progress, phase_callback=phase)
    except (Stop, StepError):
      raise
    except OSError as e:
      text = str(e)
      if "sha256 mismatch" in text or "size mismatch" in text:
        raise StepError("sha", text) from e
      if getattr(e, "errno", None) == 28 or "not enough free space" in text:
        raise StepError("storage", text) from e
      raise StepError("network", text) from e
    except Exception as e:
      raise StepError("network", str(e)) from e
    if patch is not None:
      self.check_continue()
      self.status(model, token, "verifying", downloaded_bytes=manifest.size, total_bytes=manifest.size)
      build_patched(model, path, target)
      path.unlink(missing_ok=True)
    else:
      os.replace(path, target)
    im.write_json(im.model_dir(model) / "verified.json", {"sha256": model.sha256, "size": model.size})
    return target

  def validate(self, model: im.InternalModel, token: int, path: Path) -> None:
    self.status(model, token, "validating")
    from openpilot.selfdrive.modeld.get_model_metadata import make_metadata_dict
    try:
      errors = im.contract_errors(make_metadata_dict(str(path)))
    except Exception as e:
      raise StepError("contract", f"metadata unreadable: {e}") from e
    if errors:
      raise StepError("contract", "; ".join(errors[:6]))

  def compile(self, model: im.InternalModel, token: int, path: Path, stamp: str) -> None:
    from openpilot.common.transformations.model import MEDMODEL_INPUT_SIZE
    from openpilot.selfdrive.modeld.constants import ModelConstants
    out = im.pkl_path(model)
    tmp = out.with_name(out.name + ".tmp")
    tmp.unlink(missing_ok=True)
    im.compiled_path(model).unlink(missing_ok=True)
    frame_skip = ModelConstants.MODEL_RUN_FREQ // ModelConstants.MODEL_CONTEXT_FREQ
    cmd = [sys.executable, str(MODELD_DIR / "compile_modeld.py"),
           "--model-size", "{}x{}".format(*MEDMODEL_INPUT_SIZE),
           "--camera-resolutions", *(f"{w}x{h}" for w, h in self.cameras),
           "--onnx", str(path), "--output", str(tmp), "--frame-skip", str(frame_skip)]
    if compile_core_available():
      cmd = ["taskset", "-c", str(COMPILE_CORE), *cmd]  # the isolated core the stock build also compiles on
    env = {**os.environ, **QCOM_FLAGS} if Path("/TICI").is_file() else dict(os.environ)
    self.status(model, token, "compiling")
    log_path = im.model_dir(model) / "compile.log"
    started = time.monotonic()
    with log_path.open("w") as log:
      # The loop owns this reference; the signal handler only kills it (self.child), it never clears it.
      proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, env=env, cwd=str(MODELD_DIR),
                              preexec_fn=_set_parent_death_signal)
      self.child = proc
      try:
        while proc.poll() is None:
          if time.monotonic() - started > COMPILE_TIMEOUT_SECONDS:
            raise StepError("compile", "compiler timed out")
          self.check_continue()
          time.sleep(1.0)
        code = proc.returncode
      finally:
        self.kill_child()
        self.child = None
    # Stopped or the ignition turned on while it ran: keep nothing half-finished.
    try:
      self.check_continue()
    except (Stop, StepError):
      tmp.unlink(missing_ok=True)
      raise
    if code != 0:
      tmp.unlink(missing_ok=True)
      tail = log_path.read_text(errors="replace")[-600:].strip().splitlines()
      raise StepError("compile", f"compiler exited {code}: {tail[-1] if tail else ''}")
    # compile_modeld.py already loaded the result back (validate_model_file) inside the child. Loading it again
    # here would allocate GPU buffers in this process, outside what stopping the child cancels.
    try:
      if tmp.stat().st_size <= 8:
        raise OSError("empty compiled model")
    except OSError as e:
      tmp.unlink(missing_ok=True)
      raise StepError("compile", f"compiled model missing: {e}") from e
    os.replace(tmp, out)
    im.write_json(im.compiled_path(model), {
      "sha256": model.sha256, "stamp": stamp, "cameras": [[w, h] for w, h in self.cameras],
      "pkl_size": out.stat().st_size, "compiled_at": time.time(), "seconds": round(time.monotonic() - started, 1),
    })

  def kill_child(self) -> None:
    child = self.child
    if child is not None and child.poll() is None:
      child.kill()
      try:
        child.wait(timeout=10)
      except subprocess.TimeoutExpired:
        pass

  # --- loop --------------------------------------------------------------------------------------------------
  def step(self) -> None:
    request = im.read_json(im.request_path())
    model = im.find(request.get("id")) or im.stock_model()
    token = int(request.get("token") or 0)
    if model.stock:
      if im.read_json(im.status_path()).get("phase") != "stock":
        im.write_json(im.status_path(), {"model_id": model.id, "sha256": model.sha256, "token": token, "phase": "stock",
                                         "updated_at": time.time()})
      return
    stamp = im.current_build_stamp()
    if stamp and all(im.artifact_problem(model, w, h, stamp) is None for w, h in self.cameras):
      if im.read_json(im.status_path()).get("phase") != "ready" or im.read_json(im.status_path()).get("token") != token:
        self.status(model, token, "ready")
      return
    previous = im.read_json(im.status_path())
    if previous.get("token") == token and previous.get("phase") == "error":
      if previous.get("error_code") not in RETRYABLE or time.monotonic() < self.retry_at.get(token, 0):
        return
    if not stamp:
      self.status(model, token, "error", error_code="build", detail="modeld build stamp missing")
      return
    try:
      self.check_continue()
      path = self.download(model, token)
      self.check_continue()
      self.validate(model, token, path)
      self.check_continue()
      self.compile(model, token, path, stamp)
      self.status(model, token, "ready")
    except StepError as e:
      self.retry_at[token] = time.monotonic() + NETWORK_RETRY_SECONDS
      self.status(model, token, "error", error_code=e.code, detail=e.detail[:500])

  def mark_paused(self) -> None:
    """Stopped mid-work (the manager stops this process when the car turns on): say it resumes when parked."""
    status = im.read_json(im.status_path())
    model = im.find(status.get("model_id"))
    if model is not None and status.get("phase") in ("downloading", "verifying", "validating", "compiling"):
      self.status(model, int(status.get("token") or 0), "error", error_code="waiting_offroad", detail="stopped")

  def run(self) -> None:
    while not self.stopping:
      try:
        self.step()
      except Stop:
        break
      time.sleep(POLL_SECONDS)
    self.mark_paused()


def main() -> None:
  from openpilot.common.params import Params
  manager = Manager(Params())

  def stop(_signum, _frame):
    manager.stopping = True
    manager.kill_child()
  signal.signal(signal.SIGTERM, stop)
  signal.signal(signal.SIGINT, stop)
  manager.run()
  manager.kill_child()


if __name__ == "__main__":
  main()
