import asyncio
import hashlib
import json
import stat
import sys
from pathlib import Path
from types import ModuleType

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

OWNER_KEY = "wayon_" + "A1b2C3d4" * 6
ENDPOINT = "https://cloud.example.com"
DONGLE = "a1b2c3d4e5f60718"


@pytest.fixture
def api(monkeypatch):
  root = Path(__file__).resolve().parents[4]
  package = ModuleType("openpilot.selfdrive.carrot.server.features")
  package.__path__ = [str(root / "selfdrive/carrot/server/features")]
  monkeypatch.setitem(sys.modules, package.__name__, package)
  from openpilot.selfdrive.carrot.server.features import wayon_key
  monkeypatch.setattr(wayon_key, "ACTION_INTERVAL_S", 0.0)
  return wayon_key


class Cloud:
  """Fake My Traverse cloud: records calls; replies can be queued."""
  def __init__(self):
    self.calls = []
    self.replies = []
    self.gate = None
    self.registered = {}

  async def post(self, url, payload, token=None):
    self.calls.append((url, payload, token))
    if self.gate is not None:
      await self.gate.wait()
    if self.replies:
      reply = self.replies.pop(0)
      if isinstance(reply, Exception):
        raise reply
      return reply
    if url != ENDPOINT + "/api/devices/self-register":
      return 404, {}
    device, key = payload["deviceId"], payload["key"]
    if device in self.registered and self.registered[device] != key:
      return 409, {"error": "device_already_registered"}
    self.registered[device] = key
    return 200, {"ok": True, "deviceId": device}


def make_app(api, monkeypatch, tmp_path, cloud, config=None, dongle=DONGLE, proofs=None):
  path = tmp_path / "wayon_cloud" / "config.json"
  if config is not None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(config if isinstance(config, str) else json.dumps(config))
  monkeypatch.setattr(api, "CONFIG_PATH", path)
  monkeypatch.setattr(api, "cloud_post", cloud.post)
  monkeypatch.setattr(api, "dongle_id", lambda: dongle)
  monkeypatch.setattr(api, "cloud_endpoint", lambda: ENDPOINT)
  made = proofs if proofs is not None else []
  def proof(device, request_id, key_hash):
    made.append((device, request_id, key_hash))
    return f"proof-for-{device}-{request_id}-{key_hash[:8]}"
  monkeypatch.setattr(api, "device_proof", proof)
  app = web.Application()
  api.register(app)
  return app, path


def same_origin(client, csrf):
  return {"Origin": str(client.make_url("/")).rstrip("/"), "Sec-Fetch-Site": "same-origin", "X-Carrot-CSRF": csrf}


async def csrf(client):
  return (await (await client.get("/api/wayon_key")).json())["csrf"]


async def press(client, headers=None):
  r = await client.post("/api/wayon_key/register", json={"confirmed": True}, headers=headers or same_origin(client, await csrf(client)))
  return r.status, await r.json()


def run(coro):
  return asyncio.run(coro)


def test_status_offers_a_key_only_on_a_comma_without_any_key_file(api, monkeypatch, tmp_path):
  async def body(config, dongle):
    app, _ = make_app(api, monkeypatch, tmp_path, Cloud(), config=config, dongle=dongle)
    async with TestClient(TestServer(app)) as client:
      return await (await client.get("/api/wayon_key")).json()
  missing = run(body(None, DONGLE))
  assert missing["configured"] is False and missing["can_register"] is True and missing["local_device_id"] == DONGLE
  assert run(body(None, ""))["can_register"] is False
  broken = run(body("{not json", DONGLE))
  assert broken["reason"] == "unreadable" and broken["can_register"] is False
  configured = run(body({"device_id": "own", "endpoint": ENDPOINT, "token": OWNER_KEY}, DONGLE))
  assert configured["configured"] is True and "can_register" not in configured and "local_device_id" not in configured


def test_a_comma_gets_its_own_key_proven_by_its_device_key_and_writes_a_private_config(api, monkeypatch, tmp_path):
  cloud, proofs = Cloud(), []
  app, path = make_app(api, monkeypatch, tmp_path, cloud, proofs=proofs)
  async def body():
    async with TestClient(TestServer(app)) as client:
      status, reply = await press(client)
      after = await (await client.get("/api/wayon_key")).json()
      return status, reply, after
  status, reply, after = run(body())
  assert status == 200 and reply == {"ok": True, "device_id": DONGLE, "endpoint_host": "cloud.example.com"}
  [(url, payload, auth)] = cloud.calls
  assert url == ENDPOINT + "/api/devices/self-register" and auth is None
  assert payload["deviceId"] == DONGLE and api.KEY_PATTERN.fullmatch(payload["key"])
  # The proof is bound to this comma, this request and the new key's hash.
  [(device, request_id, key_hash)] = proofs
  assert device == DONGLE and request_id == payload["requestId"] and 16 <= len(request_id) <= 64
  assert key_hash == hashlib.sha256(payload["key"].encode()).hexdigest()
  assert payload["proof"] == f"proof-for-{DONGLE}-{request_id}-{key_hash[:8]}"
  config = json.loads(path.read_text())
  assert config == {"endpoint": ENDPOINT, "token": payload["key"], "device_id": DONGLE, "vehicle_status_url": ""}
  assert stat.S_IMODE(path.stat().st_mode) == 0o600
  assert not api.pending_path().exists() and not list(path.parent.glob(".*.tmp"))
  assert after["configured"] is True and after["device_id"] == DONGLE
  assert payload["key"] not in json.dumps(reply)


@pytest.mark.parametrize("case", ["no_csrf", "foreign_origin", "cross_site", "forwarded", "not_confirmed", "rebind"])
def test_getting_a_key_keeps_the_same_origin_and_confirmation_guards(api, monkeypatch, tmp_path, case):
  cloud = Cloud()
  app, path = make_app(api, monkeypatch, tmp_path, cloud)
  async def body():
    async with TestClient(TestServer(app)) as client:
      headers = same_origin(client, await csrf(client))
      payload = {"confirmed": True}
      if case == "no_csrf":
        headers.pop("X-Carrot-CSRF")
      elif case == "foreign_origin":
        headers["Origin"] = "http://evil.example"
      elif case == "cross_site":
        headers["Sec-Fetch-Site"] = "cross-site"
      elif case == "forwarded":
        headers["X-Forwarded-For"] = "192.168.35.10"
      elif case == "not_confirmed":
        payload["confirmed"] = "yes"
      elif case == "rebind":
        headers["Host"] = f"rebind.example:{client.port}"
      r = await client.post("/api/wayon_key/register", json=payload, headers=headers)
      return r.status
  assert run(body()) in (400, 403)
  assert cloud.calls == [] and not path.exists()


def test_refuses_without_a_dongle_id_or_over_an_existing_or_broken_key_file(api, monkeypatch, tmp_path):
  async def body(config, dongle):
    cloud = Cloud()
    app, path = make_app(api, monkeypatch, tmp_path, cloud, config=config, dongle=dongle)
    before = path.read_text() if path.exists() else None
    async with TestClient(TestServer(app)) as client:
      status, _ = await press(client)
      return status, cloud.calls, before, (path.read_text() if path.exists() else None)
  status, calls, _, after = run(body(None, ""))
  assert status == 409 and calls == [] and after is None
  for config in ({"device_id": "own", "endpoint": ENDPOINT, "token": OWNER_KEY}, "{not json", {"device_id": "x", "token": " "}):
    status, calls, before, after = run(body(config, DONGLE))
    assert status == 409 and calls == [] and before == after


def test_a_comma_without_a_device_key_cannot_sign_and_nothing_is_sent(api, monkeypatch, tmp_path):
  cloud = Cloud()
  app, path = make_app(api, monkeypatch, tmp_path, cloud)
  def no_key(*_):
    raise OSError("no comma device key")
  monkeypatch.setattr(api, "device_proof", no_key)
  async def body():
    async with TestClient(TestServer(app)) as client:
      return await press(client)
  status, reply = run(body())
  assert status == 409 and "서명" in reply["error"] and cloud.calls == [] and not path.exists()


@pytest.mark.parametrize("reply,status,text", [
  ((403, {"error": "device_proof_rejected"}), 403, "comma 서버"),
  ((429, {"error": "self_register_limit"}), 429, "한도"),
  ((503, {"error": "device_proof_unavailable"}), 502, "잠시 뒤"),
  ((404, {}), 502, "지원하지"),
  ((200, {"ok": True, "deviceId": "ffffffffffffffff"}), 502, "HTTP 200"),
  ((200, {}), 502, "HTTP 200"),
  ((500, {}), 502, "HTTP 500"),
])
def test_cloud_refusals_are_explained_and_keep_the_pending_key(api, monkeypatch, tmp_path, reply, status, text):
  cloud = Cloud()
  cloud.replies = [reply]
  app, path = make_app(api, monkeypatch, tmp_path, cloud)
  async def body():
    async with TestClient(TestServer(app)) as client:
      return await press(client)
  got_status, got = run(body())
  assert got_status == status and got["ok"] is False and text in got["error"]
  assert not path.exists() and api.pending_path().exists()


def test_a_comma_already_registered_with_another_key_gets_a_clear_answer(api, monkeypatch, tmp_path):
  cloud = Cloud()
  cloud.registered[DONGLE] = "wayon_" + "z" * 48
  app, path = make_app(api, monkeypatch, tmp_path, cloud)
  async def body():
    async with TestClient(TestServer(app)) as client:
      return await press(client)
  status, reply = run(body())
  assert status == 409 and "재발급" in reply["error"]
  assert not path.exists() and not api.pending_path().exists()


def test_a_lost_reply_or_failed_write_is_finished_later_with_the_same_key(api, monkeypatch, tmp_path):
  cloud = Cloud()
  real_write = api.write_private_json
  fail = {"config": 1}
  def flaky_write(path, data, **kwargs):
    if path.name == "config.json" and fail["config"]:
      fail["config"] -= 1
      raise OSError("disk full")
    return real_write(path, data, **kwargs)
  monkeypatch.setattr(api, "write_private_json", flaky_write)
  app, path = make_app(api, monkeypatch, tmp_path, cloud)
  original = cloud.post
  lost = {"left": 1}
  async def lossy(url, payload, token=None):
    reply = await original(url, payload, token)
    if lost["left"]:
      lost["left"] -= 1
      raise api.CloudUnavailable()
    return reply
  monkeypatch.setattr(api, "cloud_post", lossy)
  async def body():
    async with TestClient(TestServer(app)) as client:
      headers = same_origin(client, await csrf(client))
      return [(await press(client, headers))[0] for _ in range(3)]
  assert run(body()) == [502, 500, 200]
  keys = {payload["key"] for _, payload, _ in cloud.calls}
  assert len(cloud.calls) == 3 and len(keys) == 1
  assert json.loads(path.read_text())["token"] == keys.pop()
  assert not api.pending_path().exists()


@pytest.mark.parametrize("pending", [
  {"endpoint": "https://other.example.com", "device_id": DONGLE, "token": "wayon_" + "p" * 48},
  {"endpoint": ENDPOINT, "device_id": "ffffffffffffffff", "token": "wayon_" + "p" * 48},
  {"endpoint": ENDPOINT, "device_id": DONGLE, "token": "short"},
  "{not json",
])
def test_an_earlier_attempt_for_another_cloud_or_comma_is_never_replaced(api, monkeypatch, tmp_path, pending):
  cloud = Cloud()
  app, path = make_app(api, monkeypatch, tmp_path, cloud)
  api.pending_path().parent.mkdir(parents=True, exist_ok=True)
  api.pending_path().write_text(pending if isinstance(pending, str) else json.dumps(pending))
  before = api.pending_path().read_text()
  async def body():
    async with TestClient(TestServer(app)) as client:
      return await press(client)
  status, reply = run(body())
  assert status == 409 and "이전 키 발급 시도" in reply["error"]
  assert cloud.calls == [] and not path.exists() and api.pending_path().read_text() == before


def test_a_key_file_that_appears_during_registration_is_never_overwritten(api, monkeypatch, tmp_path):
  cloud = Cloud()
  app, path = make_app(api, monkeypatch, tmp_path, cloud)
  original = cloud.post
  async def racing(url, payload, token=None):
    reply = await original(url, payload, token)
    path.write_text('{"someone": "else"}')
    return reply
  monkeypatch.setattr(api, "cloud_post", racing)
  async def body():
    async with TestClient(TestServer(app)) as client:
      return (await press(client))[0]
  assert run(body()) == 409
  assert path.read_text() == '{"someone": "else"}'
  assert api.pending_path().exists() and not list(path.parent.glob(".*.tmp"))


def test_a_dangling_symlink_in_place_of_the_key_file_refuses_before_anything(api, monkeypatch, tmp_path):
  cloud = Cloud()
  app, path = make_app(api, monkeypatch, tmp_path, cloud)
  path.parent.mkdir(parents=True, exist_ok=True)
  target = tmp_path / "elsewhere.json"
  path.symlink_to(target)
  async def body():
    async with TestClient(TestServer(app)) as client:
      status, _ = await press(client)
      return status, await (await client.get("/api/wayon_key")).json()
  status, after = run(body())
  assert status == 409 and not target.exists() and path.is_symlink()
  assert cloud.calls == [] and not api.pending_path().exists() and after["can_register"] is False


def test_another_process_holding_the_register_lock_turns_requests_away(api, monkeypatch, tmp_path):
  import fcntl
  import os
  cloud = Cloud()
  app, path = make_app(api, monkeypatch, tmp_path, cloud)
  lock_path = path.with_name(".register.lock")
  lock_path.parent.mkdir(parents=True, exist_ok=True)
  fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
  fcntl.flock(fd, fcntl.LOCK_EX)
  try:
    async def body():
      async with TestClient(TestServer(app)) as client:
        return (await press(client))[0]
    assert run(body()) == 429 and cloud.calls == []
  finally:
    os.close(fd)


def test_a_second_press_while_one_is_running_is_turned_away(api, monkeypatch, tmp_path):
  cloud = Cloud()
  app, _ = make_app(api, monkeypatch, tmp_path, cloud)
  async def body():
    cloud.gate = asyncio.Event()
    async with TestClient(TestServer(app)) as client:
      headers = same_origin(client, await csrf(client))
      first = asyncio.ensure_future(client.post("/api/wayon_key/register", json={"confirmed": True}, headers=headers))
      while not cloud.calls:
        await asyncio.sleep(0.01)
      second = await client.post("/api/wayon_key/register", json={"confirmed": True}, headers=headers)
      cloud.gate.set()
      return (await first).status, second.status
  assert run(body()) == (200, 429) and len(cloud.calls) == 1


def test_an_unconfirmed_pending_write_stops_before_the_cloud_every_time(api, monkeypatch, tmp_path):
  cloud = Cloud()
  app, path = make_app(api, monkeypatch, tmp_path, cloud)
  real = api.fsync_directory
  state = {"broken": True}
  def maybe(directory):
    if state["broken"]:
      raise OSError("EIO")
    return real(directory)
  monkeypatch.setattr(api, "fsync_directory", maybe)
  async def body():
    async with TestClient(TestServer(app)) as client:
      headers = same_origin(client, await csrf(client))
      statuses = [(await press(client, headers))[0] for _ in range(3)]
      calls_while_broken = len(cloud.calls)
      state["broken"] = False
      return statuses, calls_while_broken, (await press(client, headers))[0]
  statuses, calls_while_broken, final = run(body())
  assert statuses == [500, 500, 500] and calls_while_broken == 0
  assert final == 200 and len(cloud.calls) == 1 and path.exists()


def test_an_unconfirmed_config_write_keeps_the_pending_key_until_a_second_press_confirms_it(api, monkeypatch, tmp_path):
  cloud = Cloud()
  app, path = make_app(api, monkeypatch, tmp_path, cloud)
  real = api.fsync_directory
  calls = {"n": 0}
  def flaky(directory):
    calls["n"] += 1
    if calls["n"] == 2:  # 1st: pending, 2nd: config
      raise OSError("EIO")
    return real(directory)
  monkeypatch.setattr(api, "fsync_directory", flaky)
  async def body():
    async with TestClient(TestServer(app)) as client:
      headers = same_origin(client, await csrf(client))
      first = (await press(client, headers))[0]
      kept = api.pending_path().exists()
      second, reply = await press(client, headers)
      return first, kept, second, reply
  first, kept, second, reply = run(body())
  assert first == 500 and kept and path.exists()
  assert second == 200 and reply["device_id"] == DONGLE
  assert not api.pending_path().exists() and len(cloud.calls) == 1


def test_cloud_post_collects_a_reply_that_arrives_in_pieces_and_caps_large_ones(api):
  async def body():
    async def split(request):
      reply = web.StreamResponse(status=200, headers={"Content-Type": "application/json"})
      await reply.prepare(request)
      await reply.write(b'{"ok": true, "device')
      await asyncio.sleep(0.2)
      await reply.write(b'Id": "' + DONGLE.encode() + b'"}')
      await reply.write_eof()
      return reply
    async def huge(request):
      return web.Response(body=b'{"ok": true, "pad": "' + b"x" * 10000 + b'"}', content_type="application/json")
    async def redirect(request):
      raise web.HTTPFound("/split")
    app = web.Application()
    app.router.add_post("/split", split)
    app.router.add_post("/huge", huge)
    app.router.add_post("/redirect", redirect)
    async with TestServer(app) as server:
      base = str(server.make_url("")).rstrip("/")
      return (await api.cloud_post(base + "/split", {}), await api.cloud_post(base + "/huge", {}),
              await api.cloud_post(base + "/redirect", {}))
  split, huge, redirect = run(body())
  assert split == (200, {"ok": True, "deviceId": DONGLE})
  assert huge == (200, {})
  assert redirect[0] == 302  # never followed


def test_the_cloud_address_is_the_fork_default_and_must_be_https(api, monkeypatch):
  from openpilot.system import wayon_identity
  assert api.cloud_endpoint() == api.clean_endpoint(wayon_identity.DEFAULT_ENDPOINT) is not None
  monkeypatch.setattr(wayon_identity, "DEFAULT_ENDPOINT", "http://insecure.example.com")
  assert api.cloud_endpoint() is None


@pytest.mark.parametrize("value,expected", [
  ("https://wayon-cloud.someone.workers.dev", "https://wayon-cloud.someone.workers.dev"),
  ("https://Cloud.Example.com/", "https://cloud.example.com"),
  ("https://cloud.example.com:443/base", "https://cloud.example.com/base"),
  ("http://cloud.example.com", None), ("https://192.168.0.1", None), ("https://[::1]", None), ("https://cloud", None),
  ("https://cloud.example.com#x", None), ("https://-bad.example.com", None), ("https://cloud.example.test", None),
  ("ftp://cloud.example.com", None), (None, None), ("https://" + "a" * 250 + ".com", None),
])
def test_clean_endpoint(api, value, expected):
  assert api.clean_endpoint(value) == expected
