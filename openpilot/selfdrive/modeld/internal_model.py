"""Selectable internal (comma GPU) driving model.

The stock model stays the built-in artifact (models/driving_tinygrad.pkl). The user can pick another driving
model from the pinned catalog below in the carrot web. internal_model_manager downloads its ONNX (pinned size and
sha256) or builds it from the stock ONNX and a downloaded policy (WeightPatch), checks that its inputs and outputs
match the stock model exactly, and compiles it on the device while parked. modeld uses it at the next start only when the compiled artifact matches the current modeld/tinygrad
build and camera, and falls back to the stock model on any mismatch, load failure or repeated crash.

Kept light on purpose: modeld imports this at startup.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openpilot.selfdrive.modeld.internal_model_patches import WMIV11_COPIES

STOCK_ID = "CD210"
# Starts that did not reach HEALTHY_PUBLISHES before modeld stopped. Two in a row block the model until the user
# selects it again, so a model that crashes modeld cannot keep taking it down.
MAX_UNHEALTHY_STARTS = 2
BLOCK_REASONS = ("load_failed", "runtime_failed")  # kept as the reason while the model stays blocked
HEALTHY_PUBLISHES = 100  # 5 s of published modelV2 at 20 Hz
MODELS_DIR = Path(__file__).resolve().parent / "models"
ONNX_NAME = "driving_supercombo.onnx"
PKL_NAME = "driving_tinygrad.pkl"
COMPILED_NAME = "compiled.json"


@dataclass(frozen=True)
class WeightPatch:
  """Build the ONNX from the stock ONNX (template) and a downloaded file (part, at InternalModel.url): for models
  upstream published only as a separate policy whose graph equals the stock policy. Each copy writes
  part[src:src + n] over template[dst:dst + n]; see make_internal_model_patch.py."""
  template_sha256: str
  template_size: int
  part_sha256: str
  part_size: int
  copies: tuple[tuple[int, int, int], ...]


@dataclass(frozen=True)
class InternalModel:
  id: str
  name: str
  size: int  # the ONNX that is compiled
  sha256: str
  url: str | None  # None: the built-in stock artifact; else the ONNX, or the part of a patch
  checkpoint: str
  recommended: bool = False
  experimental: bool = False
  patch: WeightPatch | None = None

  @property
  def stock(self) -> bool:
    return self.url is None


STOCK_SHA256 = "f73a9e535523d5e9acb9e642c64e33d631825dc8ba74123757d107cedd047bb5"
STOCK_SIZE = 60_792_584

# Only models with exactly the stock input/output contract (see contract_errors). Others, e.g. RDFv6/TSFDO with a
# 32-frame history, need modeld changes first.
CATALOG: tuple[InternalModel, ...] = (
  # Upstream CD210 (#37050) re-exported the weights of the WMI model (#36798, sunnypilot "WMI V12") unchanged.
  InternalModel(
    id="CD210", name="CD210 = WMI V12", size=STOCK_SIZE, sha256=STOCK_SHA256, url=None,
    checkpoint="6a7d09ad-bcc9-43bc-916d-29287e60cee2/200+a27b3122-733e-4a65-938b-acfebebbe5e8/100",
  ),
  InternalModel(
    id="RDF", name="RDF", size=96_618_974,
    sha256="d0caa7d771cf9b905fca9db471d3413eed1b8b0611413f84c0da964e5992126e",
    url="https://raw.githubusercontent.com/happymaj11r/openpilot-models/main/models/RDF/driving_supercombo.onnx",
    checkpoint="5c5d862b-d4dd-4087-aa8b-232ff9ac943f/100/1c13f8c7-a35d-4796-880a-902f600130db/400",
    recommended=True, experimental=True,
  ),
  # sunnypilot "WMI V11": the wmi_driving branch after reverting V10, i.e. the V9 policy (upstream 8950897d,
  # never merged) with the stock vision. Upstream published it only as a separate opset-17 policy.
  InternalModel(
    id="WMIV11", name="WMI V11", size=STOCK_SIZE,
    sha256="f347eef41776f1a1ea6a469a440aeb2ad92f3e3ce4b16a89c98afef95a79063d",
    url="https://raw.githubusercontent.com/happymaj11r/openpilot-models/main/models/WMIv9/driving_policy.onnx",
    checkpoint="6a7d09ad-bcc9-43bc-916d-29287e60cee2/200+887f923b-7e79-43c6-8f1f-053e1490f859/100",
    experimental=True,
    patch=WeightPatch(
      template_sha256=STOCK_SHA256, template_size=STOCK_SIZE,
      part_sha256="18a71853311aabf4e30384d40f2eee562b9880a7a3f5cbf99e600bcb7c68fa18", part_size=13_926_324,
      copies=WMIV11_COPIES,
    ),
  ),
)

# The stock model's contract (driving_supercombo.onnx, CD210). modeld, the compiler and the parser assume it.
REQUIRED_INPUTS: dict[str, tuple[int, ...]] = {
  "img": (1, 12, 128, 256),
  "big_img": (1, 12, 128, 256),
  "desire_pulse": (1, 25, 8),
  "traffic_convention": (1, 2),
  "action_t": (1, 2),
  "features_buffer": (1, 24, 512),
}
REQUIRED_HEADS: dict[str, int] = {
  "meta": 55, "desire_pred": 32, "pose": 12, "wide_from_device_euler": 6, "road_transform": 12,
  "lane_lines": 528, "lane_lines_prob": 8, "road_edges": 264, "lead": 144, "lead_prob": 3,
  "hidden_state": 512, "plan": 990, "desire_state": 8,
}
# 'action': [lateral accel, accel, ...], used by modeld.get_action_from_model when present
OPTIONAL_HEADS: dict[str, int] = {"action": 4}


def find(model_id: Any) -> InternalModel | None:
  return next((m for m in CATALOG if m.id == model_id), None)


def stock_model() -> InternalModel:
  model = find(STOCK_ID)
  assert model is not None and model.stock
  return model


def base_dir() -> Path:
  if override := os.getenv("CARROT_INTERNAL_MODEL_DIR"):
    return Path(override)
  if Path("/TICI").is_file():
    return Path("/data/media/0/carrot/models/internal")
  return Path.home() / ".comma" / "models" / "internal"


def model_dir(model: InternalModel) -> Path:
  return base_dir() / f"{model.id}-{model.sha256[:16]}"


def stock_onnx_path() -> Path:
  return MODELS_DIR / ONNX_NAME


def onnx_path(model: InternalModel) -> Path:
  return model_dir(model) / ONNX_NAME


def pkl_path(model: InternalModel) -> Path:
  return model_dir(model) / PKL_NAME


def compiled_path(model: InternalModel) -> Path:
  return model_dir(model) / COMPILED_NAME


def request_path() -> Path:
  return base_dir() / "request.json"


def status_path() -> Path:
  return base_dir() / "status.json"


def runtime_path() -> Path:
  return base_dir() / "runtime.json"


def read_json(path: Path) -> dict[str, Any]:
  try:
    with path.open(encoding="utf-8") as f:
      value = json.load(f)
    return value if isinstance(value, dict) else {}
  except (OSError, ValueError):
    return {}


def write_json(path: Path, value: dict[str, Any]) -> None:
  path.parent.mkdir(parents=True, exist_ok=True)
  fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
  try:
    with os.fdopen(fd, "w", encoding="utf-8") as f:
      json.dump(value, f, sort_keys=True)
      f.flush()
      os.fsync(f.fileno())
    os.replace(tmp, path)
  except BaseException:
    try:
      os.unlink(tmp)
    except OSError:
      pass
    raise


def requested_model() -> InternalModel:
  """The model the user selected; stock for a missing, unknown or malformed request."""
  return find(read_json(request_path()).get("id")) or stock_model()


def write_request(model: InternalModel, now: float | None = None) -> dict[str, Any]:
  request = {"id": model.id, "token": int((now if now is not None else time.time()) * 1000)}
  write_json(request_path(), request)
  return request


def current_build_stamp() -> str:
  """The stamp launch_chffrplus.sh writes after building modeld (modeld dir + tinygrad tree hashes)."""
  try:
    return (MODELS_DIR / ".build_stamp").read_text().strip()
  except OSError:
    return ""


def contract_errors(metadata: dict[str, Any]) -> list[str]:
  """Differences from the stock model's input/output contract; empty when the model can replace it."""
  errors: list[str] = []
  inputs = {str(k): tuple(int(x) for x in v) for k, v in (metadata.get("input_shapes") or {}).items()}
  for name, shape in REQUIRED_INPUTS.items():
    if name not in inputs:
      errors.append(f"missing input {name}")
    elif inputs[name] != shape:
      errors.append(f"input {name} shape {inputs[name]} != {shape}")
  for name in sorted(set(inputs) - set(REQUIRED_INPUTS)):
    errors.append(f"unexpected input {name}")

  outputs = {str(k): tuple(int(x) for x in v) for k, v in (metadata.get("output_shapes") or {}).items()}
  if len(outputs) != 1:
    errors.append(f"expected one output, got {len(outputs)}")
    return errors
  out_shape = next(iter(outputs.values()))
  if len(out_shape) != 2 or out_shape[0] != 1 or out_shape[1] <= 0:
    errors.append(f"output shape {out_shape} is not (1, N)")
    return errors
  width = out_shape[1]

  slices = metadata.get("output_slices") or {}
  for name, size in {**REQUIRED_HEADS, **OPTIONAL_HEADS}.items():
    if name not in slices:
      if name in REQUIRED_HEADS:
        errors.append(f"missing head {name}")
      continue
    s = slices[name]
    start, stop = getattr(s, "start", None), getattr(s, "stop", None)
    if not isinstance(start, int) or not isinstance(stop, int) or not 0 <= start < stop <= width:
      errors.append(f"head {name} slice {start}:{stop} outside the output")
    elif stop - start != size:
      errors.append(f"head {name} width {stop - start} != {size}")
  for name in sorted(set(slices) - set(REQUIRED_HEADS) - set(OPTIONAL_HEADS) - {"pad"}):
    errors.append(f"unexpected head {name}")
  return errors


def artifact_problem(model: InternalModel, cam_w: int, cam_h: int, stamp: str | None = None) -> str | None:
  """Why the compiled artifact of a downloaded model cannot be used now; None when it can."""
  compiled = read_json(compiled_path(model))
  if not compiled:
    return "not_compiled"
  if compiled.get("sha256") != model.sha256:
    return "not_compiled"
  stamp = current_build_stamp() if stamp is None else stamp
  if not stamp or compiled.get("stamp") != stamp:
    return "stale_build"
  if [cam_w, cam_h] not in compiled.get("cameras", []):
    return "camera"
  path = pkl_path(model)
  try:
    if path.stat().st_size != compiled.get("pkl_size"):
      return "missing"
  except OSError:
    return "missing"
  return None


@dataclass(frozen=True)
class Selected:
  model: InternalModel
  pkl_path: Path


def _write_runtime(active: InternalModel, sha256: str | None, pending: bool, unhealthy: int, reason: str | None,
                   now: float | None = None, loading: bool = False, used: bool = False) -> None:
  # loading: modeld is loading the selected artifact (a native crash there counts as an unhealthy start)
  # pending: the selected model has started producing outputs but is not healthy yet
  # used: the selected model produced outputs in this modeld start (an eGPU/Jetlink model may run instead)
  write_json(runtime_path(), {
    "active_id": active.id, "sha256": sha256, "pending": pending, "loading": loading, "used": used,
    "unhealthy_starts": unhealthy, "fallback_reason": reason, "updated_at": now if now is not None else time.time(),
  })


def select_for_modeld(cam_w: int, cam_h: int, now: float | None = None) -> Selected | None:
  """The selected custom model for this modeld start, or None for the stock model. Never raises."""
  try:
    model = requested_model()
    stock = stock_model()
    if model.stock:
      _write_runtime(stock, None, False, 0, None, now)
      return None
    problem = artifact_problem(model, cam_w, cam_h)
    if problem is not None:
      _write_runtime(stock, model.sha256, False, 0, problem, now)
      return None
    previous = read_json(runtime_path())
    same = previous.get("sha256") == model.sha256
    unhealthy = int(previous.get("unhealthy_starts") or 0) if same else 0
    if same and (previous.get("pending") or previous.get("loading")):
      # the previous start stopped while loading this model or before its outputs were healthy; a start in which
      # only an eGPU/Jetlink model ran is not counted
      unhealthy += 1
    if unhealthy >= MAX_UNHEALTHY_STARTS:
      reason = previous.get("fallback_reason") if same and previous.get("fallback_reason") in BLOCK_REASONS else "crashed"
      _write_runtime(stock, model.sha256, False, unhealthy, reason, now)
      return None
    _write_runtime(model, model.sha256, False, unhealthy, None, now, loading=True)
    return Selected(model, pkl_path(model))
  except Exception:
    return None


def mark_loading(selected: Selected, now: float | None = None) -> None:
  """modeld is running the selected model outside the main loop (eGPU standby warmup); a crash there counts."""
  try:
    runtime = read_json(runtime_path())
    _write_runtime(selected.model, selected.model.sha256, False, int(runtime.get("unhealthy_starts") or 0), None, now,
                   loading=True)
  except Exception:
    pass


def mark_loaded(selected: Selected, now: float | None = None) -> None:
  try:
    runtime = read_json(runtime_path())
    _write_runtime(selected.model, selected.model.sha256, False, int(runtime.get("unhealthy_starts") or 0), None, now)
  except Exception:
    pass


def record_load_failure(selected: Selected, now: float | None = None) -> None:
  _block(selected, "load_failed", now)


def record_runtime_failure(selected: Selected, now: float | None = None) -> None:
  """modeld is about to stop on an inference error or non-finite output of the selected model; its restart uses stock."""
  _block(selected, "runtime_failed", now)


def _block(selected: Selected, reason: str, now: float | None) -> None:
  try:
    _write_runtime(stock_model(), selected.model.sha256, False, MAX_UNHEALTHY_STARTS, reason, now)
  except Exception:
    pass


class HealthTracker:
  """Tracks the selected internal model in this modeld start.

  From its first inference until HEALTHY_PUBLISHES of its own outputs it is pending: if modeld then stops without
  the clean-stop mark (mark_clean_stop, written when the manager stops modeld), the next start counts it as
  unhealthy. Handing over to an eGPU/Jetlink model does not end pending, so a crash when it takes back over is
  still counted; a normal stop is not. "used" follows which model produces the outputs, for the carrot web card.
  """

  def __init__(self, selected: Selected | None):
    self.selected = selected
    self.published = 0
    self.pending = False
    self.healthy = False
    self.used: bool | None = None

  def _write(self) -> None:
    assert self.selected is not None
    try:
      runtime = read_json(runtime_path())
      unhealthy = 0 if self.healthy else int(runtime.get("unhealthy_starts") or 0)
      _write_runtime(self.selected.model, self.selected.model.sha256, self.pending, unhealthy, None, used=bool(self.used))
    except Exception:
      pass

  def attempt(self, internal: bool = True) -> None:
    """Called before an inference: whether the selected internal model is about to produce the output."""
    if self.selected is None or not internal or self.pending or self.healthy:
      return
    self.pending, self.used = True, True
    self._write()

  def publish(self, produced_by_internal: bool = True) -> None:
    if self.selected is None:
      return
    changed = self.used is not None and self.used != produced_by_internal
    self.used = produced_by_internal
    if produced_by_internal and not self.healthy:
      self.published += 1
      if self.published >= HEALTHY_PUBLISHES:
        self.healthy, self.pending, changed = True, False, True
    if changed:
      self._write()


def mark_clean_stop() -> None:
  """The manager is stopping modeld (SIGINT): this start ended normally, also while it was still loading.

  The runtime record was rewritten by this start's select_for_modeld, so its pending/loading flags are this start's.
  """
  try:
    runtime = read_json(runtime_path())
    if runtime.get("pending") or runtime.get("loading"):
      runtime.update({"pending": False, "loading": False, "stopped_cleanly": True})
      write_json(runtime_path(), runtime)
  except Exception:
    pass


def reset_runtime_block(model: InternalModel) -> None:
  """A new selection by the user retries a model that was blocked after crashing."""
  runtime = read_json(runtime_path())
  if runtime.get("sha256") == model.sha256 and (runtime.get("pending") or runtime.get("unhealthy_starts")):
    runtime.update({"pending": False, "unhealthy_starts": 0})
    write_json(runtime_path(), runtime)
