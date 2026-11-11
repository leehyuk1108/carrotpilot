import asyncio
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace as NS

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

KEY = "wyn_" + "A1b2C3d4" * 6


@pytest.fixture
def feature(monkeypatch, tmp_path):
  root = Path(__file__).resolve().parents[4]
  package = ModuleType("openpilot.selfdrive.carrot.server.features")
  package.__path__ = [str(root / "selfdrive/carrot/server/features")]
  monkeypatch.setitem(sys.modules, package.__name__, package)
  from openpilot.selfdrive.carrot.server.features import wayon_key as api
  config = tmp_path / "config.json"
  config.write_text(json.dumps({"device_id": "dev-123", "endpoint": "https://cloud.example.test/api", "token": KEY,
                                "parking_approach_enabled": True}))
  monkeypatch.setattr(api, "CONFIG_PATH", config)
  app = web.Application()
  api.register(app)
  return app, api, config


def same_origin(client, **extra):
  return {"Origin": str(client.make_url("/")).rstrip("/"), "Sec-Fetch-Site": "same-origin", **extra}


async def status(client):
  response = await client.get("/api/wayon_key")
  return response, await response.json()


def test_status_masks_the_key_and_never_returns_it(feature):
  app, _, _ = feature
  async def run():
    async with TestClient(TestServer(app)) as client:
      response, body = await status(client)
      assert response.status == 200
      assert response.headers["Cache-Control"] == "no-store"
      assert body["configured"] is True and body["device_id"] == "dev-123"
      assert body["endpoint_host"] == "cloud.example.test"
      assert body["key_masked"] == f"{KEY[:4]}…{KEY[-4:]}" and body["key_length"] == len(KEY)
      assert KEY not in json.dumps(body) and body["csrf"]
  asyncio.run(run())


@pytest.mark.parametrize("content,reason", [(None, "missing"), ("{not json", "unreadable"), ("[]", "invalid"),
                                            (json.dumps({"device_id": "x", "token": " "}), "no_key")])
def test_status_explains_a_missing_or_broken_config(feature, content, reason):
  app, _, config = feature
  if content is None:
    config.unlink()
  else:
    config.write_text(content)
  async def run():
    async with TestClient(TestServer(app)) as client:
      _, body = await status(client)
      assert body["configured"] is False and body["reason"] == reason and "key_masked" not in body
      response = await client.post("/api/wayon_key/reveal", json={"confirmed": True},
                                   headers=same_origin(client, **{"X-Carrot-CSRF": body["csrf"]}))
      assert response.status == 404 and "key" not in await response.json()
  asyncio.run(run())


def test_reveal_returns_the_key_once_and_then_rate_limits(feature):
  app, _, _ = feature
  async def run():
    async with TestClient(TestServer(app)) as client:
      _, body = await status(client)
      headers = same_origin(client, **{"X-Carrot-CSRF": body["csrf"]})
      response = await client.post("/api/wayon_key/reveal", json={"confirmed": True}, headers=headers)
      assert response.status == 200 and response.headers["Cache-Control"] == "no-store"
      assert (await response.json())["key"] == KEY
      again = await client.post("/api/wayon_key/reveal", json={"confirmed": True}, headers=headers)
      assert again.status == 429 and "key" not in await again.json()
  asyncio.run(run())


@pytest.mark.parametrize("case", ["no_csrf", "bad_csrf", "no_origin", "foreign_origin", "cross_site", "forwarded",
                                  "not_confirmed", "not_json", "too_large"])
def test_reveal_refuses_anything_but_a_confirmed_same_origin_request(feature, case):
  app, _, _ = feature
  async def run():
    async with TestClient(TestServer(app)) as client:
      _, body = await status(client)
      headers = same_origin(client, **{"X-Carrot-CSRF": body["csrf"]})
      payload = {"json": {"confirmed": True}}
      if case == "no_csrf":
        headers.pop("X-Carrot-CSRF")
      elif case == "bad_csrf":
        headers["X-Carrot-CSRF"] = "x" * len(body["csrf"])
      elif case == "no_origin":
        headers.pop("Origin")
      elif case == "foreign_origin":
        headers["Origin"] = "http://evil.example"
      elif case == "cross_site":
        headers["Sec-Fetch-Site"] = "cross-site"
      elif case == "forwarded":
        headers["X-Forwarded-For"] = "192.168.35.10"
      elif case == "not_confirmed":
        payload = {"json": {"confirmed": "yes"}}
      elif case == "not_json":
        payload = {"data": "confirmed=true"}
      elif case == "too_large":
        payload = {"json": {"confirmed": True, "pad": "x" * 2048}}
      response = await client.post("/api/wayon_key/reveal", headers=headers, **payload)
      assert response.status in (400, 403)
      assert KEY not in await response.text()
  asyncio.run(run())


def test_status_refuses_a_foreign_host_name(feature):
  app, _, _ = feature
  async def run():
    async with TestClient(TestServer(app)) as client:
      response = await client.get("/api/wayon_key", headers={"Host": f"rebind.example:{client.port}"})
      assert response.status == 403
  asyncio.run(run())


def fake_request(peer, local, host):
  return NS(remote=peer, transport=NS(get_extra_info=lambda name: (local, 7000)), host=host,
            headers={}, scheme="http")


@pytest.mark.parametrize("peer,local,host,allowed", [
  ("192.168.35.157", "192.168.35.175", "192.168.35.175:7000", True),
  ("100.115.146.86", "100.78.55.7", "100.78.55.7:7000", True),
  ("203.0.113.9", "192.168.35.175", "192.168.35.175:7000", False),
  ("192.168.35.157", "192.168.35.175", "192.168.35.99:7000", False),
  ("192.168.35.157", "192.168.35.175", "comma.local:7000", False),
])
def test_only_the_owner_network_reaching_the_comma_address_is_trusted(feature, peer, local, host, allowed):
  _, api, _ = feature
  request = fake_request(peer, local, host)
  if allowed:
    api.validate_request(request)
  else:
    with pytest.raises(web.HTTPForbidden):
      api.validate_request(request)
