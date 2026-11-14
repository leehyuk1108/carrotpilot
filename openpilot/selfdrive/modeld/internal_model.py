"""Selectable internal (comma GPU) driving model.

The stock model stays the built-in artifact (models/driving_tinygrad.pkl). The user can pick another driving
model from the pinned catalog below in the carrot web. internal_model_manager downloads its ONNX (pinned size and
sha256), checks that its inputs and outputs match the stock model exactly, and compiles it on the device while
parked. modeld uses it at the next start only when the compiled artifact matches the current modeld/tinygrad
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
class InternalModel:
  id: str
  name: str
  size: int
  sha256: str
  url: str | None  # None: the built-in stock artifact
  checkpoint: str
  recommended: bool = False
  experimental: bool = False

  @property
  def stock(self) -> bool:
    return self.url is None


# Only models with exactly the stock input/output contract (see contract_errors). Others, e.g. RDFv6/TSFDO with a
# 32-frame history, need modeld changes first.
CATALOG: tuple[InternalModel, ...] = (
  InternalModel(
    id="CD210", name="CD210", size=60_792_584,
    sha256="f73a9e535523d5e9acb9e642c64e33d631825dc8ba74123757d107cedd047bb5", url=None,
    checkpoint="6a7d09ad-bcc9-43bc-916d-29287e60cee2/200+a27b3122-733e-4a65-938b-acfebebbe5e8/100",
  ),
  InternalModel(
    id="RDF", name="RDF", size=96_618_974,
    sha256="d0caa7d771cf9b905fca9db471d3413eed1b8b0611413f84c0da964e5992126e",
    url="https://raw.githubusercontent.com/happymaj11r/openpilot-models/main/models/RDF/driving_supercombo.onnx",
    checkpoint="5c5d862b-d4dd-4087-aa8b-232ff9ac943f/100/1c13f8c7-a35d-4796-880a-902f600130db/400",
    recommended=True, experimental=True,
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

  It is pending (a stop now counts as unhealthy) only while it produces the outputs: from its first inference until
  HEALTHY_PUBLISHES of its outputs, and not while an eGPU or Jetlink model it handed over to produces them.
  """

  def __init__(self, selected: Selected | None):
    self.selected = selected
    self.published = 0
    self.pending = False

  def _write(self, pending: bool, used: bool, healthy: bool = False) -> None:
    assert self.selected is not None
    try:
      runtime = read_json(runtime_path())
      unhealthy = 0 if healthy else int(runtime.get("unhealthy_starts") or 0)
      _write_runtime(self.selected.model, self.selected.model.sha256, pending, unhealthy, None, used=used)
    except Exception:
      pass

  def attempt(self, internal: bool = True) -> None:
    """Called before an inference: the selected internal model is about to produce the output."""
    if self.selected is None or self.pending or not internal:
      return
    self.pending = True
    self._write(pending=True, used=True)

  def publish(self, produced_by_internal: bool = True) -> None:
    if self.selected is None:
      return
    if not produced_by_internal:
      if self.pending:  # handed over cleanly to an eGPU/Jetlink model; it is not running or failing now
        self.pending = False
        self._write(pending=False, used=False)
      return
    self.published += 1
    if self.published >= HEALTHY_PUBLISHES:
      self._write(pending=False, used=True, healthy=True)
      self.selected = None


def reset_runtime_block(model: InternalModel) -> None:
  """A new selection by the user retries a model that was blocked after crashing."""
  runtime = read_json(runtime_path())
  if runtime.get("sha256") == model.sha256 and (runtime.get("pending") or runtime.get("unhealthy_starts")):
    runtime.update({"pending": False, "unhealthy_starts": 0})
    write_json(runtime_path(), runtime)
