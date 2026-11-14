import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from openpilot.selfdrive.modeld import internal_model as im


@pytest.fixture
def feature(monkeypatch, tmp_path):
  monkeypatch.setenv("CARROT_INTERNAL_MODEL_DIR", str(tmp_path / "internal"))
  spec = importlib.util.spec_from_file_location(
    "openpilot.selfdrive.carrot.server.features.internal_model_test",
    Path(__file__).parents[1] / "features/internal_model.py",
  )
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  module.state = {"onroad": False, "engaged": False}
  params = SimpleNamespace(get_bool=lambda key: {"IsOnroad": module.state["onroad"], "IsEngaged": module.state["engaged"]}.get(key, False))
  monkeypatch.setattr(module, "_params", lambda: params)
  return module


class Request:
  def __init__(self, body):
    self.body = body

  async def json(self):
    if isinstance(self.body, Exception):
      raise self.body
    return self.body


def call(handler, body=None):
  response = asyncio.run(handler(Request(body)))
  import json
  return response.status, json.loads(response.body)


def test_status_lists_the_catalog_and_defaults_to_stock(feature):
  status, payload = call(feature.api_status)
  assert status == 200 and payload["requested"] == im.STOCK_ID and payload["phase"] == "stock"
  assert [m["id"] for m in payload["models"]] == [m.id for m in im.CATALOG]
  assert payload["can_select"] is True and payload["active"] == im.STOCK_ID


def test_select_writes_the_request_offroad(feature):
  status, payload = call(feature.api_select, {"id": "RDF"})
  assert status == 200 and payload["requested"] == "RDF" and payload["phase"] == "waiting"
  assert im.requested_model().id == "RDF"


@pytest.mark.parametrize("key", ["onroad", "engaged"])
def test_select_is_refused_while_driving(feature, key):
  feature.state[key] = True
  status, payload = call(feature.api_select, {"id": "RDF"})
  assert status == 409 and not payload["ok"]
  assert im.requested_model().id == im.STOCK_ID


@pytest.mark.parametrize("body", [{"id": "NOPE"}, {}, ["RDF"], ValueError("bad json")])
def test_select_rejects_unknown_models_and_bad_bodies(feature, body):
  status, payload = call(feature.api_select, body)
  assert status == 400 and not payload["ok"]


def test_status_reports_progress_and_fallback_for_the_requested_model(feature):
  rdf = im.find("RDF")
  im.write_request(rdf)
  im.write_json(im.status_path(), {"sha256": rdf.sha256, "phase": "downloading", "downloaded_bytes": 50, "total_bytes": 200})
  im.write_json(im.runtime_path(), {"active_id": im.STOCK_ID, "sha256": rdf.sha256, "fallback_reason": "crashed"})
  _, payload = call(feature.api_status)
  assert payload["phase"] == "downloading" and payload["progress"] == 25.0
  assert payload["fallback_reason"] == "crashed" and payload["active"] == im.STOCK_ID


def test_status_of_an_earlier_selection_is_ignored(feature):
  im.write_request(im.find("RDF"))
  im.write_json(im.status_path(), {"sha256": "0" * 64, "phase": "error", "error_code": "compile"})
  _, payload = call(feature.api_status)
  assert payload["phase"] == "waiting" and payload["error_code"] is None


def test_selecting_again_clears_a_crash_block(feature):
  rdf = im.find("RDF")
  im.write_json(im.runtime_path(), {"active_id": im.STOCK_ID, "sha256": rdf.sha256, "pending": False, "unhealthy_starts": 2})
  call(feature.api_select, {"id": "RDF"})
  assert im.read_json(im.runtime_path())["unhealthy_starts"] == 0


def test_selected_model_is_in_use_only_after_it_produced_outputs(feature):
  rdf = im.find("RDF")
  im.write_request(rdf)
  im.write_json(im.status_path(), {"sha256": rdf.sha256, "phase": "ready"})
  im.write_json(im.runtime_path(), {"active_id": "RDF", "sha256": rdf.sha256, "used": False})
  assert call(feature.api_status)[1]["in_use"] is False   # loaded as the fallback behind an eGPU/Jetlink model
  im.write_json(im.runtime_path(), {"active_id": "RDF", "sha256": rdf.sha256, "used": True})
  assert call(feature.api_status)[1]["in_use"] is True
