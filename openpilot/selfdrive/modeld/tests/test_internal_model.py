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


def test_ready_model_is_selected_and_marked_pending_until_healthy():
  im.write_request(RDF)
  compile_ok(RDF)
  selected = im.select_for_modeld(*CAM)
  assert selected is not None and selected.pkl_path == im.pkl_path(RDF)
  assert im.read_json(im.runtime_path())["pending"] is True
  health = im.HealthTracker(selected)
  for _ in range(im.HEALTHY_PUBLISHES):
    health.publish()
  runtime = im.read_json(im.runtime_path())
  assert runtime["pending"] is False and runtime["unhealthy_starts"] == 0 and runtime["active_id"] == "RDF"


def test_two_unhealthy_starts_block_the_model_until_selected_again():
  im.write_request(RDF)
  compile_ok(RDF)
  assert im.select_for_modeld(*CAM) is not None   # start 1, crashes before healthy
  assert im.select_for_modeld(*CAM) is not None   # start 2 (1 unhealthy so far), crashes again
  assert im.select_for_modeld(*CAM) is None       # 2 unhealthy starts: stock
  assert im.read_json(im.runtime_path())["fallback_reason"] == "crashed"
  assert im.select_for_modeld(*CAM) is None       # stays blocked
  im.reset_runtime_block(RDF)
  assert im.select_for_modeld(*CAM) is not None


def test_load_failure_blocks_immediately():
  im.write_request(RDF)
  compile_ok(RDF)
  selected = im.select_for_modeld(*CAM)
  im.record_load_failure(selected)
  assert im.select_for_modeld(*CAM) is None
  assert im.read_json(im.runtime_path())["fallback_reason"] == "crashed"


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
      Path(cmd[cmd.index("--output") + 1]).write_bytes(b"pkl")
    def poll(self):
      return 0
  monkeypatch.setattr(imm.subprocess, "Popen", FakeProc)
  monkeypatch.setattr(imm.Path, "is_file", lambda self: str(self) == "/TICI" or Path.exists(self))
  import openpilot.selfdrive.modeld.helpers as helpers
  monkeypatch.setattr(helpers, "validate_model_file", lambda path: None)
  im.model_dir(RDF).mkdir(parents=True)
  m.compile(RDF, 1, im.onnx_path(RDF), "stamp-1")
  cmd = seen["cmd"]
  assert cmd[cmd.index("--camera-resolutions") + 1] == "1344x760" and cmd[cmd.index("--frame-skip") + 1] == "4"
  assert cmd[cmd.index("--model-size") + 1] == "512x256"
  assert all(seen["env"][k] == v for k, v in imm.QCOM_FLAGS.items())
  compiled = json.loads(im.compiled_path(RDF).read_text())
  assert compiled["stamp"] == "stamp-1" and compiled["cameras"] == [list(CAM)] and im.pkl_path(RDF).read_bytes() == b"pkl"


def test_rdf_onnx_metadata_matches_the_contract_when_available():
  path = Path(os.getenv("CARROT_TEST_RDF_ONNX", "/data/tmp/model-sel/rdf.onnx"))
  if not path.is_file():
    pytest.skip("RDF ONNX not downloaded here")
  from openpilot.selfdrive.modeld.get_model_metadata import make_metadata_dict
  assert im.contract_errors(make_metadata_dict(str(path))) == []
