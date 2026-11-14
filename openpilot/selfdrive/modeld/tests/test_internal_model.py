"""Selectable internal driving model: catalog, contract check, modeld selection/fallback and the parked manager."""
import json
import os
from pathlib import Path

import pytest

from openpilot.selfdrive.modeld import internal_model as im
from openpilot.selfdrive.modeld import internal_model_manager as imm

RDF = im.find("RDF")
CAM = (1344, 760)


@pytest.fixture(autouse=True)
def model_dirs(monkeypatch, tmp_path):
  monkeypatch.setenv("CARROT_INTERNAL_MODEL_DIR", str(tmp_path / "internal"))
  models_dir = tmp_path / "models"
  models_dir.mkdir()
  (models_dir / ".build_stamp").write_text("stamp-1")
  monkeypatch.setattr(im, "MODELS_DIR", models_dir)
  return tmp_path


def stock_metadata(**changes):
  widths = {**im.REQUIRED_HEADS, **im.OPTIONAL_HEADS}
  slices, start = {}, 0
  for name, width in widths.items():
    slices[name] = slice(start, start + width)
    start += width
  slices["pad"] = slice(-2, None)
  meta = {"input_shapes": dict(im.REQUIRED_INPUTS), "output_shapes": {"outputs": (1, start + 2)}, "output_slices": slices}
  meta.update(changes)
  return meta


def compile_ok(model, stamp="stamp-1", cameras=(CAM,)):
  im.model_dir(model).mkdir(parents=True, exist_ok=True)
  im.pkl_path(model).write_bytes(b"x" * 10)
  im.write_json(im.compiled_path(model), {"sha256": model.sha256, "stamp": stamp, "cameras": [list(c) for c in cameras], "pkl_size": 10})


def test_catalog_has_one_stock_model_and_pinned_https_downloads():
  assert im.stock_model().id == im.STOCK_ID and sum(m.stock for m in im.CATALOG) == 1
  assert len({m.id for m in im.CATALOG}) == len(im.CATALOG)
  for m in im.CATALOG:
    assert len(m.sha256) == 64 and int(m.sha256, 16) >= 0 and m.size > 0
    assert m.stock or m.url.startswith("https://")


def test_contract_accepts_stock_layout_with_or_without_action():
  assert im.contract_errors(stock_metadata()) == []
  meta = stock_metadata()
  del meta["output_slices"]["action"]
  assert im.contract_errors(meta) == []


@pytest.mark.parametrize("change,expected", [
  ({"input_shapes": {**im.REQUIRED_INPUTS, "features_buffer": (1, 32, 512)}}, "input features_buffer shape"),
  ({"input_shapes": {**im.REQUIRED_INPUTS, "lateral_control_params": (1, 2)}}, "unexpected input lateral_control_params"),
  ({"output_shapes": {"a": (1, 10), "b": (1, 10)}}, "expected one output"),
])
def test_contract_rejects_other_inputs_and_outputs(change, expected):
  assert any(expected in e for e in im.contract_errors(stock_metadata(**change)))


def test_contract_rejects_wrong_head_width_missing_head_and_unknown_head():
  meta = stock_metadata()
  meta["output_slices"]["meta"] = slice(0, 53)
  del meta["output_slices"]["plan"]
  meta["output_slices"]["mystery"] = slice(0, 1)
  errors = im.contract_errors(meta)
  assert any("head meta width 53" in e for e in errors)
  assert any("missing head plan" in e for e in errors)
  assert any("unexpected head mystery" in e for e in errors)


def test_stock_request_or_garbage_uses_stock():
  assert im.select_for_modeld(*CAM) is None
  im.request_path().write_text("{not json")
  assert im.select_for_modeld(*CAM) is None
  im.write_json(im.request_path(), {"id": "NOPE"})
  assert im.select_for_modeld(*CAM) is None
  assert im.read_json(im.runtime_path())["active_id"] == im.STOCK_ID


@pytest.mark.parametrize("prepare,reason", [
  (lambda: None, "not_compiled"),
  (lambda: compile_ok(RDF, stamp="old"), "stale_build"),
  (lambda: compile_ok(RDF, cameras=((1928, 1208),)), "camera"),
  (lambda: (compile_ok(RDF), im.pkl_path(RDF).write_bytes(b"short")), "missing"),
])
def test_unusable_artifact_falls_back_with_a_reason(prepare, reason):
  im.write_request(RDF)
  prepare()
  assert im.select_for_modeld(*CAM) is None
  runtime = im.read_json(im.runtime_path())
  assert runtime["active_id"] == im.STOCK_ID and runtime["fallback_reason"] == reason


def test_missing_build_stamp_never_uses_a_custom_model():
  im.write_request(RDF)
  compile_ok(RDF)
  (im.MODELS_DIR / ".build_stamp").unlink()
  assert im.select_for_modeld(*CAM) is None


def start(internal_runs=True, loaded=True):
  """One modeld start that stops (or crashes) without reaching healthy."""
  selected = im.select_for_modeld(*CAM)
  if selected is not None and loaded:
    im.mark_loaded(selected)
    im.HealthTracker(selected).attempt(internal_runs)
  return selected


def test_ready_model_is_selected_and_tracked_from_its_first_inference_until_healthy():
  im.write_request(RDF)
  compile_ok(RDF)
  selected = im.select_for_modeld(*CAM)
  assert selected is not None and selected.pkl_path == im.pkl_path(RDF)
  assert im.read_json(im.runtime_path())["loading"] is True
  im.mark_loaded(selected)
  runtime = im.read_json(im.runtime_path())
  assert runtime["loading"] is False and runtime["pending"] is False and runtime["used"] is False
  health = im.HealthTracker(selected)
  health.attempt()
  assert im.read_json(im.runtime_path())["pending"] is True and im.read_json(im.runtime_path())["used"] is True
  for _ in range(im.HEALTHY_PUBLISHES):
    health.publish()
  runtime = im.read_json(im.runtime_path())
  assert runtime["pending"] is False and runtime["unhealthy_starts"] == 0 and runtime["active_id"] == "RDF"


def test_two_unhealthy_starts_block_the_model_until_selected_again():
  im.write_request(RDF)
  compile_ok(RDF)
  assert start() is not None        # start 1, crashes before healthy
  assert start() is not None        # start 2 (1 unhealthy so far), crashes again
  assert im.select_for_modeld(*CAM) is None       # 2 unhealthy starts: stock
  assert im.read_json(im.runtime_path())["fallback_reason"] == "crashed"
  assert im.select_for_modeld(*CAM) is None       # stays blocked
  im.reset_runtime_block(RDF)
  assert im.select_for_modeld(*CAM) is not None


def test_native_crash_while_loading_counts_as_unhealthy():
  im.write_request(RDF)
  compile_ok(RDF)
  assert start(loaded=False) is not None
  assert start(loaded=False) is not None
  assert im.select_for_modeld(*CAM) is None
  assert im.read_json(im.runtime_path())["fallback_reason"] == "crashed"


def test_starts_where_only_an_external_model_ran_never_block_the_selected_model():
  im.write_request(RDF)
  compile_ok(RDF)
  for _ in range(4):
    assert start(internal_runs=False) is not None
  runtime = im.read_json(im.runtime_path())
  assert runtime["unhealthy_starts"] == 0 and runtime["used"] is False


def test_load_failure_blocks_immediately():
  im.write_request(RDF)
  compile_ok(RDF)
  selected = im.select_for_modeld(*CAM)
  im.record_load_failure(selected)
  assert im.select_for_modeld(*CAM) is None
  assert im.read_json(im.runtime_path())["fallback_reason"] == "load_failed"


def test_select_for_modeld_never_raises(monkeypatch):
  monkeypatch.setattr(im, "requested_model", lambda: 1 / 0)
  assert im.select_for_modeld(*CAM) is None


# --- parked manager ------------------------------------------------------------------------------------------
class FakeParams:
  def __init__(self, onroad=False):
    self.onroad = onroad

  def get_bool(self, key):
    return key == "IsOnroad" and self.onroad


def manager(onroad=False):
  return imm.Manager(FakeParams(onroad), cameras=[CAM])


def test_manager_pipeline_downloads_validates_compiles_then_reports_ready(monkeypatch):
  im.write_request(RDF)
  calls = []
  m = manager()
  monkeypatch.setattr(m, "download", lambda model, token: calls.append("download") or im.onnx_path(model))
  monkeypatch.setattr(m, "validate", lambda model, token, path: calls.append("validate"))
  monkeypatch.setattr(m, "compile", lambda model, token, path, stamp: (calls.append(("compile", stamp)), compile_ok(model)))
  m.step()
  assert calls == ["download", "validate", ("compile", "stamp-1")]
  assert im.read_json(im.status_path())["phase"] == "ready"
  m.step()   # already compiled for this build: nothing to do
  assert len(calls) == 3


def test_manager_contract_failure_is_not_retried_until_selected_again(monkeypatch):
  im.write_request(RDF, now=1)
  m = manager()
  calls = []
  monkeypatch.setattr(m, "download", lambda model, token: im.onnx_path(model))
  def bad(model, token, path):
    calls.append(1)
    raise imm.StepError("contract", "missing head plan")
  monkeypatch.setattr(m, "validate", bad)
  m.step()
  m.step()
  status = im.read_json(im.status_path())
  assert status["phase"] == "error" and status["error_code"] == "contract" and calls == [1]
  im.write_request(RDF, now=2)   # a new selection retries
  m.step()
  assert calls == [1, 1]


def test_manager_stops_work_when_the_ignition_turns_on(monkeypatch):
  im.write_request(RDF)
  m = manager(onroad=True)
  monkeypatch.setattr(m, "download", lambda *a: pytest.fail("must not download onroad"))
  m.step()
  status = im.read_json(im.status_path())
  assert status["phase"] == "error" and status["error_code"] == "waiting_offroad"


def test_manager_recompiles_after_a_build_change(monkeypatch):
  im.write_request(RDF)
  compile_ok(RDF, stamp="stamp-0")
  m = manager()
  monkeypatch.setattr(m, "download", lambda model, token: im.onnx_path(model))
  monkeypatch.setattr(m, "validate", lambda *a: None)
  compiled = []
  monkeypatch.setattr(m, "compile", lambda model, token, path, stamp: (compiled.append(stamp), compile_ok(model, stamp=stamp)))
  m.step()
  assert compiled == ["stamp-1"] and im.artifact_problem(RDF, *CAM) is None


def test_manager_writes_stock_status_for_stock_selection():
  im.write_request(im.stock_model())
  manager().step()
  assert im.read_json(im.status_path())["phase"] == "stock"


def test_real_download_checks_the_pinned_size_and_hash(monkeypatch):
  im.write_request(RDF)
  m = manager()

  def fake_download(manifest, cache_dir, progress_callback=None, phase_callback=None):
    assert manifest.sha256 == RDF.sha256 and manifest.size == RDF.size and manifest.url == RDF.url
    raise OSError(f"model sha256 mismatch: expected {RDF.sha256}, got {'0' * 64}")

  import openpilot.selfdrive.modeld.big_model as big_model
  monkeypatch.setattr(big_model, "_download_model", fake_download)
  with pytest.raises(imm.StepError) as e:
    m.download(RDF, 1)
  assert e.value.code == "sha"


def test_compile_command_uses_the_stock_build_flags(monkeypatch, tmp_path):
  m = manager()
  seen = {}

  class FakeProc:
    returncode = 0
    def __init__(self, cmd, **kw):
      seen.update(cmd=cmd, env=kw["env"])
      Path(cmd[cmd.index("--output") + 1]).write_bytes(b"compiled-pkl")
    def poll(self):
      return 0
  monkeypatch.setattr(imm.subprocess, "Popen", FakeProc)
  monkeypatch.setattr(imm.Path, "is_file", lambda self: str(self) == "/TICI" or Path.exists(self))
  monkeypatch.setattr(imm, "compile_core_available", lambda: False)   # parked comma: cores 4-7 offline
  import openpilot.selfdrive.modeld.helpers as helpers
  monkeypatch.setattr(helpers, "validate_model_file", lambda path: pytest.fail("must not load the model (GPU) in the manager"))
  im.model_dir(RDF).mkdir(parents=True)
  m.compile(RDF, 1, im.onnx_path(RDF), "stamp-1")
  cmd = seen["cmd"]
  assert cmd[0] != "taskset"
  assert cmd[cmd.index("--camera-resolutions") + 1] == "1344x760" and cmd[cmd.index("--frame-skip") + 1] == "4"
  assert cmd[cmd.index("--model-size") + 1] == "512x256"
  assert all(seen["env"][k] == v for k, v in imm.QCOM_FLAGS.items())
  compiled = json.loads(im.compiled_path(RDF).read_text())
  assert compiled["stamp"] == "stamp-1" and compiled["cameras"] == [list(CAM)] and im.pkl_path(RDF).read_bytes() == b"compiled-pkl"


def test_rdf_onnx_metadata_matches_the_contract_when_available():
  path = Path(os.getenv("CARROT_TEST_RDF_ONNX", "/data/tmp/model-sel/rdf.onnx"))
  if not path.is_file():
    pytest.skip("RDF ONNX not downloaded here")
  from openpilot.selfdrive.modeld.get_model_metadata import make_metadata_dict
  assert im.contract_errors(make_metadata_dict(str(path))) == []


def test_runtime_failure_after_healthy_blocks_the_model_with_its_reason():
  im.write_request(RDF)
  compile_ok(RDF)
  selected = im.select_for_modeld(*CAM)
  health = im.HealthTracker(selected)
  for _ in range(im.HEALTHY_PUBLISHES):
    health.publish()
  im.record_runtime_failure(selected)   # modeld is about to stop on an inference error
  assert im.select_for_modeld(*CAM) is None
  assert im.read_json(im.runtime_path())["fallback_reason"] == "runtime_failed"
  assert im.select_for_modeld(*CAM) is None   # still blocked, same reason
  assert im.read_json(im.runtime_path())["fallback_reason"] == "runtime_failed"


def test_outputs_of_an_external_model_do_not_make_the_selected_model_healthy():
  im.write_request(RDF)
  compile_ok(RDF)
  im.write_json(im.runtime_path(), {"sha256": RDF.sha256, "unhealthy_starts": 1})   # one earlier bad start
  selected = im.select_for_modeld(*CAM)
  im.mark_loaded(selected)
  health = im.HealthTracker(selected)
  health.attempt()                      # it ran once, then an external model took over
  for _ in range(im.HEALTHY_PUBLISHES * 2):
    health.publish(produced_by_internal=False)
  runtime = im.read_json(im.runtime_path())
  assert runtime["unhealthy_starts"] == 1 and health.selected is selected   # not reset to healthy


class FakeChild:
  def __init__(self, polls_before_exit=1000, returncode=0, on_poll=None):
    self.polls, self.returncode, self.on_poll, self.killed = polls_before_exit, None, on_poll, False
    self._code = returncode

  def poll(self):
    if self.on_poll:
      self.on_poll(self)
    if self.killed:
      self.returncode = -9
    elif self.polls <= 0:
      self.returncode = self._code
    self.polls -= 1
    return self.returncode

  def kill(self):
    self.killed = True

  def wait(self, timeout=None):
    return self.returncode


def run_compile(monkeypatch, m, child):
  monkeypatch.setattr(imm.subprocess, "Popen", lambda *a, **k: child)
  monkeypatch.setattr(imm.time, "sleep", lambda s: None)
  im.model_dir(RDF).mkdir(parents=True, exist_ok=True)
  return m.compile(RDF, 1, im.onnx_path(RDF), "stamp-1")


def test_stop_signal_during_compile_kills_the_child_and_leaves_a_resumable_status(monkeypatch):
  im.write_request(RDF)
  m = manager()
  def signal_arrives(child):
    if child.polls == 998 and not m.stopping:
      m.stopping = True      # what the SIGTERM handler does
      m.kill_child()
  child = FakeChild(on_poll=signal_arrives)
  m.status(RDF, 1, "compiling")
  with pytest.raises(imm.Stop):
    run_compile(monkeypatch, m, child)
  assert child.killed and m.child is None
  assert not im.compiled_path(RDF).exists() and not im.pkl_path(RDF).exists()
  m.mark_paused()
  status = im.read_json(im.status_path())
  assert status["phase"] == "error" and status["error_code"] == "waiting_offroad"


def test_ignition_on_as_the_compiler_exits_does_not_finish_the_artifact(monkeypatch):
  im.write_request(RDF)
  params = FakeParams()
  m = imm.Manager(params, cameras=[CAM])
  out = im.pkl_path(RDF).with_name(im.PKL_NAME + ".tmp")
  def exits_while_ignition_turns_on(child):
    if child.polls == 0:
      out.write_bytes(b"compiled-pkl")
      params.onroad = True
  with pytest.raises(imm.StepError) as e:
    run_compile(monkeypatch, m, FakeChild(polls_before_exit=0, on_poll=exits_while_ignition_turns_on))
  assert e.value.code == "waiting_offroad"
  assert not out.exists() and not im.pkl_path(RDF).exists() and not im.compiled_path(RDF).exists()


def test_a_handover_to_an_external_model_then_a_normal_stop_is_not_a_failure():
  im.write_request(RDF)
  compile_ok(RDF)
  for _ in range(4):   # each start: 3 internal outputs, then Jetlink takes over, then the manager stops modeld
    selected = im.select_for_modeld(*CAM)
    assert selected is not None
    im.mark_loaded(selected)
    health = im.HealthTracker(selected)
    for _ in range(3):
      health.attempt(True)
      health.publish(True)
    health.attempt(False)
    health.publish(False)
    assert im.read_json(im.runtime_path())["used"] is False   # the card does not claim it is running
    im.mark_clean_stop()
  assert im.read_json(im.runtime_path())["unhealthy_starts"] == 0


def test_a_crash_when_the_internal_model_takes_back_over_is_counted():
  im.write_request(RDF)
  compile_ok(RDF)
  for _ in range(2):   # internal, Jetlink, then the internal model crashes modeld natively as it takes over again
    selected = im.select_for_modeld(*CAM)
    assert selected is not None
    im.mark_loaded(selected)
    health = im.HealthTracker(selected)
    health.attempt(True)
    health.publish(True)
    health.attempt(False)
    health.publish(False)
    health.attempt(True)   # JoiningModel may still look active here; pending was never cleared anyway
  assert im.select_for_modeld(*CAM) is None
  assert im.read_json(im.runtime_path())["fallback_reason"] == "crashed"


def test_a_normal_stop_before_healthy_is_not_counted():
  im.write_request(RDF)
  compile_ok(RDF)
  for _ in range(3):   # short drives
    selected = im.select_for_modeld(*CAM)
    im.mark_loaded(selected)
    health = im.HealthTracker(selected)
    for _ in range(10):
      health.attempt(True)
      health.publish(True)
    im.mark_clean_stop()
  assert im.select_for_modeld(*CAM) is not None
  assert im.read_json(im.runtime_path())["unhealthy_starts"] == 0


def test_in_use_follows_the_producing_model_after_healthy():
  im.write_request(RDF)
  compile_ok(RDF)
  selected = im.select_for_modeld(*CAM)
  im.mark_loaded(selected)
  health = im.HealthTracker(selected)
  for _ in range(im.HEALTHY_PUBLISHES):
    health.attempt(True)
    health.publish(True)
  assert im.read_json(im.runtime_path())["used"] is True and im.read_json(im.runtime_path())["pending"] is False
  health.publish(False)
  assert im.read_json(im.runtime_path())["used"] is False
  health.publish(True)
  runtime = im.read_json(im.runtime_path())
  assert runtime["used"] is True and runtime["unhealthy_starts"] == 0


def test_modeld_marks_the_managers_sigint_as_a_clean_stop_before_loading_the_model():
  import ast
  tree = ast.parse((Path(im.__file__).parent / "modeld.py").read_text())
  function = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "main")
  body = ast.unparse(function)
  assert body.index("signal.signal(signal.SIGINT, stop_cleanly)") < body.index("create_internal_model(vipc_client_main")
  block = next(n for n in ast.walk(function) if isinstance(n, ast.If) and "signal.signal" in ast.unparse(n.body))
  handlers, stops = {}, []
  def default_int_handler(signum, frame):
    raise KeyboardInterrupt
  fake_signal = type("S", (), {"SIGINT": 2, "signal": staticmethod(lambda sig, fn: handlers.__setitem__(sig, fn)),
                               "default_int_handler": staticmethod(default_int_handler)})
  for stock, installed in ((True, False), (False, True)):
    handlers.clear()
    internal = type("I", (), {"mark_clean_stop": staticmethod(lambda: stops.append(1)),
                              "requested_model": staticmethod(lambda stock=stock: type("M", (), {"stock": stock}))})
    scope = {"signal": fake_signal, "internal_model": internal}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[block], type_ignores=[])), "<sigint>", "exec"), scope)
    assert (2 in handlers) == installed   # the stock model keeps the default SIGINT behaviour
  with pytest.raises(KeyboardInterrupt):
    handlers[2](2, None)
  assert stops == [1]


def test_a_normal_stop_while_loading_is_not_counted():
  im.write_request(RDF)
  compile_ok(RDF)
  for _ in range(3):
    assert im.select_for_modeld(*CAM) is not None   # loading=True
    im.mark_clean_stop()                            # the manager stopped modeld during the load
  assert im.select_for_modeld(*CAM) is not None
  assert im.read_json(im.runtime_path())["unhealthy_starts"] == 0


def test_egpu_standby_warmup_failure_blocks_the_selected_model(monkeypatch):
  import ast
  import sys
  import types
  im.write_request(RDF)
  compile_ok(RDF)
  selected = im.select_for_modeld(*CAM)
  im.mark_loaded(selected)
  tree = ast.parse((Path(im.__file__).parent / "modeld.py").read_text())
  block = next(n for n in ast.walk(tree) if isinstance(n, ast.If) and isinstance(n.test, ast.Name)
               and n.test.id == "usbgpu_model_loaded" and any(isinstance(b, ast.ImportFrom) and b.module and
               b.module.endswith("internal_model_warmup") for b in n.body))
  def warm(model, client):
    assert im.read_json(im.runtime_path())["loading"] is True   # a native crash here would count
    raise RuntimeError("selected internal model output is not finite")
  monkeypatch.setitem(sys.modules, "openpilot.selfdrive.modeld.internal_model_warmup",
                      types.SimpleNamespace(warm_internal_model_from_camera=warm))
  scope = {"usbgpu_model_loaded": True, "internal_selected": selected, "internal_model": im,
           "small_model": object(), "vipc_client_main": object()}
  with pytest.raises(RuntimeError):
    exec(compile(ast.fix_missing_locations(ast.Module(body=[block], type_ignores=[])), "<warmup>", "exec"), scope)
  assert im.select_for_modeld(*CAM) is None
  assert im.read_json(im.runtime_path())["fallback_reason"] == "runtime_failed"


def test_compile_core_needs_the_core_online_and_allowed(monkeypatch, tmp_path):
  states = {"online": "0\n", "affinity": {0, 1, 2, 3, 7}}
  real_read_text = Path.read_text
  monkeypatch.setattr(imm.Path, "read_text", lambda self, *a, **k: states["online"] if "cpu7/online" in str(self) else real_read_text(self, *a, **k))
  monkeypatch.setattr(imm.os, "sched_getaffinity", lambda pid: states["affinity"], raising=False)
  real_is_file = Path.is_file
  monkeypatch.setattr(imm.Path, "is_file", lambda self: True if str(self) == "/usr/bin/taskset" else real_is_file(self))
  assert imm.compile_core_available() is False          # offline while parked
  states["online"] = "1\n"
  assert imm.compile_core_available() is True
  states["affinity"] = {0, 1, 2, 3, 4, 5}
  assert imm.compile_core_available() is False          # not allowed for the manager's processes


# --- weight patch models (WMI V11) -------------------------------------------------------------------------------
WMIV11 = im.find("WMIV11")


def test_patch_models_are_pinned_and_their_copies_stay_inside_both_files():
  for m in im.CATALOG:
    if m.patch is None:
      continue
    p = m.patch
    assert p.template_sha256 == im.stock_model().sha256 and p.template_size == im.stock_model().size
    assert len(p.part_sha256) == 64 and p.part_size > 0 and m.url.startswith("https://") and not m.stock
    spans = sorted((dst, dst + n) for dst, _src, n in p.copies)
    assert all(n > 0 and dst + n <= p.template_size and src + n <= p.part_size for dst, src, n in p.copies)
    assert all(a_end <= b_start for (_a, a_end), (b_start, _b) in zip(spans, spans[1:], strict=False))


def _patch_model(tmp_path, template: bytes, part: bytes, copies, result: bytes | None = None):
  import dataclasses
  import hashlib
  out = bytearray(template)
  for dst, src, n in copies:
    out[dst:dst + n] = part[src:src + n]
  result = bytes(out) if result is None else result
  stock_onnx = tmp_path / "models" / im.ONNX_NAME
  stock_onnx.write_bytes(template)
  sha = lambda b: hashlib.sha256(b).hexdigest()
  patch = im.WeightPatch(template_sha256=sha(template), template_size=len(template), part_sha256=sha(part),
                         part_size=len(part), copies=tuple(copies))
  return dataclasses.replace(WMIV11, size=len(result), sha256=sha(result), patch=patch), bytes(out)


def test_build_patched_applies_the_copies_and_checks_every_hash(tmp_path):
  model, expected = _patch_model(tmp_path, b"T" * 64, b"0123456789", [(4, 0, 3), (40, 7, 3)])
  part = tmp_path / "part.onnx"
  part.write_bytes(b"0123456789")
  target = tmp_path / "out.onnx"
  imm.build_patched(model, part, target)
  assert target.read_bytes() == expected == b"TTTT012" + b"T" * 33 + b"789" + b"T" * 21


@pytest.mark.parametrize("broken,code", [("template", "template"), ("part", "sha"), ("result", "sha"), ("range", "contract")])
def test_build_patched_rejects_a_changed_stock_model_part_or_result(tmp_path, broken, code):
  copies = [(4, 0, 3)] if broken != "range" else [(62, 0, 3)]
  model, _ = _patch_model(tmp_path, b"T" * 64, b"0123456789", copies,
                          result=b"x" * 64 if broken == "result" else None)
  if broken == "template":
    (tmp_path / "models" / im.ONNX_NAME).write_bytes(b"U" * 64)   # e.g. an update changed the stock model
  part = tmp_path / "part.onnx"
  part.write_bytes(b"0123456789" if broken != "part" else b"9123456789")
  target = tmp_path / "out.onnx"
  with pytest.raises(imm.StepError) as e:
    imm.build_patched(model, part, target)
  assert e.value.code == code and not target.exists() and not target.with_name(target.name + ".tmp").exists()


def test_download_of_a_patch_model_fetches_the_part_then_builds_and_verifies_the_onnx(monkeypatch, tmp_path):
  model, expected = _patch_model(tmp_path, b"T" * 64, b"0123456789", [(4, 0, 3)])
  monkeypatch.setattr(im, "CATALOG", (*[m for m in im.CATALOG if m.id != model.id], model))
  seen = []

  def fake_download(manifest, cache_dir, progress_callback=None, phase_callback=None):
    seen.append(manifest)
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / manifest.cache_filename
    path.write_bytes(b"0123456789")
    return path

  import openpilot.selfdrive.modeld.big_model as big_model
  monkeypatch.setattr(big_model, "_download_model", fake_download)
  m = manager()
  path = m.download(model, 1)
  assert seen[0].sha256 == model.patch.part_sha256 and seen[0].size == model.patch.part_size and seen[0].url == model.url
  assert path == im.onnx_path(model) and path.read_bytes() == expected
  assert im.read_json(im.model_dir(model) / "verified.json")["sha256"] == model.sha256
  assert not (im.model_dir(model) / seen[0].cache_filename).exists()   # the part is not kept
  m.download(model, 1)   # verified: no second download
  assert len(seen) == 1


def test_template_mismatch_is_not_retried_until_selected_again(monkeypatch, tmp_path):
  model, _ = _patch_model(tmp_path, b"T" * 64, b"0123456789", [(4, 0, 3)])
  monkeypatch.setattr(im, "CATALOG", (*[m for m in im.CATALOG if m.id != model.id], model))
  im.write_request(model, now=1)
  m = manager()
  calls = []
  def download(model, token):
    calls.append(1)
    raise imm.StepError("template", "the stock model differs")
  monkeypatch.setattr(m, "download", download)
  m.step()
  m.step()
  status = im.read_json(im.status_path())
  assert status["phase"] == "error" and status["error_code"] == "template" and calls == [1]


def test_wmiv11_patch_of_the_real_stock_model_gives_the_pinned_onnx_when_available(tmp_path):
  stock = Path(os.getenv("CARROT_TEST_STOCK_ONNX", str(Path(im.__file__).parent / "models" / im.ONNX_NAME)))
  part = Path(os.getenv("CARROT_TEST_WMIV9_POLICY", "/data/tmp/model-sel/wmiv9_policy.onnx"))
  if not stock.is_file() or stock.stat().st_size != im.STOCK_SIZE or not part.is_file():
    pytest.skip("stock ONNX or WMI V9 policy not here")
  (tmp_path / "models" / im.ONNX_NAME).write_bytes(stock.read_bytes())
  target = tmp_path / "wmiv11.onnx"
  imm.build_patched(WMIV11, part, target)
  from openpilot.selfdrive.modeld.get_model_metadata import make_metadata_dict
  meta = make_metadata_dict(str(target))
  assert im.contract_errors(meta) == [] and meta["model_checkpoint"] == WMIV11.checkpoint
