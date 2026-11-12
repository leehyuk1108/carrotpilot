import asyncio
import base64
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
CODE = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"
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
  """Fake My Traverse cloud: records calls; replies are queued per path suffix."""
  def __init__(self):
    self.calls = []
    self.replies = {}
    self.gate = None
    self.registered = {}

  async def post(self, url, payload, token=None):
    self.calls.append((url, payload, token))
    if self.gate is not None:
      await self.gate.wait()
    path = url.split("://", 1)[1].split("/", 1)[1] if "://" in url else url
    if "//" in path or not path.startswith("api/devices/"):
      return 404, {}  # the Worker routes only exact paths
    suffix = "/" + path
    queued = self.replies.get(suffix)
    if queued:
      reply = queued.pop(0)
      if isinstance(reply, Exception):
        raise reply
      return reply
    if suffix == "/api/devices/invites":
      return 200, {"ok": True, "code": CODE, "expiresAt": "2026-11-12T03:30:00.000Z"}
    device, key = payload["deviceId"], payload["key"]
    if device in self.registered and self.registered[device] != key:
      return 409, {"error": "device_already_registered"}
    self.registered[device] = key
    return 200, {"ok": True, "deviceId": device}


def make_app(api, monkeypatch, tmp_path, cloud, config=None, dongle=DONGLE):
  path = tmp_path / "wayon_cloud" / "config.json"
  if config is not None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(config if isinstance(config, str) else json.dumps(config))
  monkeypatch.setattr(api, "CONFIG_PATH", path)
  monkeypatch.setattr(api, "cloud_post", cloud.post)
  monkeypatch.setattr(api, "dongle_id", lambda: dongle)
  app = web.Application()
  api.register(app)
  return app, path


def same_origin(client, csrf):
  return {"Origin": str(client.make_url("/")).rstrip("/"), "Sec-Fetch-Site": "same-origin", "X-Carrot-CSRF": csrf}


async def csrf(client):
  return (await (await client.get("/api/wayon_key")).json())["csrf"]


def invite_text(endpoint=ENDPOINT, code=CODE, version=1, extra=None):
  data = {"v": version, "e": endpoint, "c": code, "x": "2026-11-12T03:30:00.000Z", **(extra or {})}
  return "mtinv1." + base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")


def run(coro):
  return asyncio.run(coro)


def test_status_offers_registration_only_on_a_comma_without_any_key_file(api, monkeypatch, tmp_path):
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


def test_owner_creates_an_invite_that_carries_its_cloud_address_but_never_its_key(api, monkeypatch, tmp_path):
  cloud = Cloud()
  app, _ = make_app(api, monkeypatch, tmp_path, cloud, config={"device_id": "own", "endpoint": ENDPOINT + "/", "token": OWNER_KEY})
  async def body():
    async with TestClient(TestServer(app)) as client:
      reply = await client.post("/api/wayon_key/invite", json={"confirmed": True}, headers=same_origin(client, await csrf(client)))
      return reply.status, await reply.json(), reply.headers
  status, body_, headers = run(body())
  assert status == 200 and headers["Cache-Control"] == "no-store"
  # The trailing slash of the stored endpoint is not doubled into the request path.
  assert cloud.calls == [(ENDPOINT + "/api/devices/invites", {}, OWNER_KEY)]
  assert api.decode_invite(body_["invite"]) == (ENDPOINT, CODE)
  assert body_["endpoint_host"] == "cloud.example.com" and body_["expires_at"] == "2026-11-12T03:30:00.000Z"
  assert OWNER_KEY not in json.dumps(body_)


@pytest.mark.parametrize("reply,status,text", [
  ((403, {"error": "invite_not_allowed"}), 403, "처음 등록한 콤마"),
  ((429, {"error": "invite_limit"}), 429, "3개"),
  ((429, {"error": "invite_daily_limit"}), 429, "24시간"),
  ((401, {"error": "unauthorized"}), 502, "받아들이지"),
  ((404, {}), 502, "지원하지"),
  ((200, {"ok": True, "code": "short", "expiresAt": "x"}), 502, "HTTP 200"),
  ((500, {}), 502, "HTTP 500"),
])
def test_invite_failures_are_explained(api, monkeypatch, tmp_path, reply, status, text):
  cloud = Cloud()
  cloud.replies["/api/devices/invites"] = [reply]
  app, _ = make_app(api, monkeypatch, tmp_path, cloud, config={"device_id": "own", "endpoint": ENDPOINT, "token": OWNER_KEY})
  async def body():
    async with TestClient(TestServer(app)) as client:
      r = await client.post("/api/wayon_key/invite", json={"confirmed": True}, headers=same_origin(client, await csrf(client)))
      return r.status, await r.json()
  got_status, got = run(body())
  assert got_status == status and got["ok"] is False and text in got["error"] and "invite" not in got


def test_invite_needs_a_key_a_public_cloud_address_and_a_reachable_cloud(api, monkeypatch, tmp_path):
  async def body(config, replies=None):
    cloud = Cloud()
    if replies:
      cloud.replies["/api/devices/invites"] = replies
    app, _ = make_app(api, monkeypatch, tmp_path, cloud, config=config)
    async with TestClient(TestServer(app)) as client:
      r = await client.post("/api/wayon_key/invite", json={"confirmed": True}, headers=same_origin(client, await csrf(client)))
      return r.status, cloud.calls
  assert run(body(None)) == (404, [])
  assert run(body({"device_id": "own", "endpoint": "http://10.0.0.5:8787", "token": OWNER_KEY})) == (409, [])
  status, calls = run(body({"device_id": "own", "endpoint": ENDPOINT, "token": OWNER_KEY}, [api.CloudUnavailable()]))
  assert status == 502 and len(calls) == 1


@pytest.mark.parametrize("path", ["/api/wayon_key/invite", "/api/wayon_key/register"])
@pytest.mark.parametrize("case", ["no_csrf", "foreign_origin", "cross_site", "forwarded", "not_confirmed", "rebind"])
def test_new_actions_keep_the_same_origin_and_confirmation_guards(api, monkeypatch, tmp_path, path, case):
  cloud = Cloud()
  config = {"device_id": "own", "endpoint": ENDPOINT, "token": OWNER_KEY} if path.endswith("invite") else None
  app, _ = make_app(api, monkeypatch, tmp_path, cloud, config=config)
  async def body():
    async with TestClient(TestServer(app)) as client:
      headers = same_origin(client, await csrf(client))
      payload = {"confirmed": True, "invite": invite_text()}
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
      r = await client.post(path, json=payload, headers=headers)
      return r.status
  assert run(body()) in (400, 403)
  assert cloud.calls == []


def test_a_new_comma_registers_with_its_own_key_and_writes_a_private_config(api, monkeypatch, tmp_path):
  cloud = Cloud()
  app, path = make_app(api, monkeypatch, tmp_path, cloud)
  async def body():
    async with TestClient(TestServer(app)) as client:
      token = await csrf(client)
      # Copying across lines may add whitespace; it is ignored.
      text = invite_text()
      pasted = "\n  " + text[:20] + "\n" + text[20:] + "  \n"
      r = await client.post("/api/wayon_key/register", json={"confirmed": True, "invite": pasted}, headers=same_origin(client, token))
      after = await (await client.get("/api/wayon_key")).json()
      return r.status, await r.json(), after
  status, reply, after = run(body())
  assert status == 200 and reply == {"ok": True, "device_id": DONGLE, "endpoint_host": "cloud.example.com"}
  [(url, payload, auth)] = cloud.calls
  assert url == ENDPOINT + "/api/devices/register" and auth is None
  assert payload["deviceId"] == DONGLE and payload["invite"] == CODE
  assert api.KEY_PATTERN.fullmatch(payload["key"]) and payload["key"] != OWNER_KEY
  config = json.loads(path.read_text())
  # Only this comma's own cloud, key and ID; the owner car's external status feed is switched off.
  assert config == {"endpoint": ENDPOINT, "token": payload["key"], "device_id": DONGLE, "vehicle_status_url": ""}
  assert stat.S_IMODE(path.stat().st_mode) == 0o600
  assert not api.pending_path().exists()
  assert not list(path.parent.glob(".*.tmp"))
  assert after["configured"] is True and after["device_id"] == DONGLE
  assert payload["key"] not in json.dumps(reply)


@pytest.mark.parametrize("text", [
  "", "wayon_" + "x" * 48, "mtinv2.abc", "mtinv1.***", "mtinv1." + "A" * 700,
  invite_text(endpoint="http://cloud.example.com"),
  invite_text(endpoint="https://10.0.0.5"),
  invite_text(endpoint="https://user:pw@cloud.example.com"),
  invite_text(endpoint="https://cloud.example.com:8443"),
  invite_text(endpoint="https://comma.local"),
  invite_text(endpoint="https://localhost"),
  invite_text(endpoint="https://cloud.example.com/a/../b"),
  invite_text(endpoint="https://cloud.example.com?x=1"),
  invite_text(endpoint="https://cloud.example.com/"),
  invite_text(code=CODE.lower()),
  invite_text(code=CODE[:-1]),
  invite_text(version=2),
  "mtinv1." + base64.urlsafe_b64encode(b"[1,2]").decode(),
])
def test_bad_or_tampered_invites_are_refused_before_contacting_any_cloud(api, monkeypatch, tmp_path, text):
  cloud = Cloud()
  app, path = make_app(api, monkeypatch, tmp_path, cloud)
  async def body():
    async with TestClient(TestServer(app)) as client:
      r = await client.post("/api/wayon_key/register", json={"confirmed": True, "invite": text}, headers=same_origin(client, await csrf(client)))
      return r.status
  assert run(body()) == 400
  assert cloud.calls == [] and not path.exists() and not api.pending_path().exists()


def test_register_refuses_without_a_dongle_id_or_over_an_existing_or_broken_key_file(api, monkeypatch, tmp_path):
  async def body(config, dongle):
    cloud = Cloud()
    app, path = make_app(api, monkeypatch, tmp_path, cloud, config=config, dongle=dongle)
    before = path.read_text() if path.exists() else None
    async with TestClient(TestServer(app)) as client:
      r = await client.post("/api/wayon_key/register", json={"confirmed": True, "invite": invite_text()}, headers=same_origin(client, await csrf(client)))
      return r.status, cloud.calls, before, (path.read_text() if path.exists() else None)
  status, calls, _, after = run(body(None, ""))
  assert status == 409 and calls == [] and after is None
  for config in ({"device_id": "own", "endpoint": ENDPOINT, "token": OWNER_KEY}, "{not json", {"device_id": "x", "token": " "}):
    status, calls, before, after = run(body(config, DONGLE))
    assert status == 409 and calls == [] and before == after


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
  # 1st: the cloud registers the device but the reply is lost; 2nd: registered, config write fails; 3rd: done.
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
      statuses = []
      for _ in range(3):
        r = await client.post("/api/wayon_key/register", json={"confirmed": True, "invite": invite_text()}, headers=headers)
        statuses.append(r.status)
      return statuses
  assert run(body()) == [502, 500, 200]
  keys = {payload["key"] for _, payload, _ in cloud.calls}
  assert len(cloud.calls) == 3 and len(keys) == 1
  assert json.loads(path.read_text())["token"] == keys.pop()
  assert not api.pending_path().exists()


def test_a_comma_already_registered_with_another_key_gets_a_clear_answer(api, monkeypatch, tmp_path):
  cloud = Cloud()
  cloud.registered[DONGLE] = "wayon_" + "z" * 48
  app, path = make_app(api, monkeypatch, tmp_path, cloud)
  async def body():
    async with TestClient(TestServer(app)) as client:
      r = await client.post("/api/wayon_key/register", json={"confirmed": True, "invite": invite_text()}, headers=same_origin(client, await csrf(client)))
      return r.status, await r.json()
  status, reply = run(body())
  assert status == 409 and "재발급" in reply["error"]
  assert not path.exists() and not api.pending_path().exists()


def test_a_spent_or_wrong_code_keeps_the_pending_key_for_a_new_code(api, monkeypatch, tmp_path):
  cloud = Cloud()
  cloud.replies["/api/devices/register"] = [(403, {"error": "invalid_device_invite"})]
  app, path = make_app(api, monkeypatch, tmp_path, cloud)
  async def body():
    async with TestClient(TestServer(app)) as client:
      headers = same_origin(client, await csrf(client))
      first = await client.post("/api/wayon_key/register", json={"confirmed": True, "invite": invite_text()}, headers=headers)
      second = await client.post("/api/wayon_key/register", json={"confirmed": True, "invite": invite_text(code="B" * 32)}, headers=headers)
      return first.status, await first.json(), second.status
  status, reply, second = run(body())
  assert status == 403 and "새 코드" in reply["error"] and second == 200
  assert cloud.calls[0][1]["key"] == cloud.calls[1][1]["key"]
  assert cloud.calls[1][1]["invite"] == "B" * 32 and path.exists()


def test_a_second_register_while_one_is_running_is_turned_away(api, monkeypatch, tmp_path):
  cloud = Cloud()
  app, _ = make_app(api, monkeypatch, tmp_path, cloud)
  async def body():
    cloud.gate = asyncio.Event()
    async with TestClient(TestServer(app)) as client:
      headers = same_origin(client, await csrf(client))
      first = asyncio.ensure_future(client.post("/api/wayon_key/register", json={"confirmed": True, "invite": invite_text()}, headers=headers))
      while not cloud.calls:
        await asyncio.sleep(0.01)
      second = await client.post("/api/wayon_key/register", json={"confirmed": True, "invite": invite_text()}, headers=headers)
      cloud.gate.set()
      return (await first).status, second.status
  assert run(body()) == (200, 429)
  assert len(cloud.calls) == 1


def test_owner_invite_round_trips_into_a_new_comma_registration(api, monkeypatch, tmp_path):
  cloud = Cloud()
  owner_app, _ = make_app(api, monkeypatch, tmp_path / "owner", cloud, config={"device_id": "own", "endpoint": ENDPOINT, "token": OWNER_KEY})
  async def make_invite():
    async with TestClient(TestServer(owner_app)) as client:
      r = await client.post("/api/wayon_key/invite", json={"confirmed": True}, headers=same_origin(client, await csrf(client)))
      return (await r.json())["invite"]
  invite = run(make_invite())
  new_app, path = make_app(api, monkeypatch, tmp_path / "new", cloud)
  async def register():
    async with TestClient(TestServer(new_app)) as client:
      r = await client.post("/api/wayon_key/register", json={"confirmed": True, "invite": invite}, headers=same_origin(client, await csrf(client)))
      return r.status
  assert run(register()) == 200
  assert json.loads(path.read_text())["endpoint"] == ENDPOINT


@pytest.mark.parametrize("value,expected", [
  ("https://wayon-cloud.someone.workers.dev", "https://wayon-cloud.someone.workers.dev"),
  ("https://Cloud.Example.com/", "https://cloud.example.com"),
  ("https://cloud.example.com:443/base", "https://cloud.example.com/base"),
  ("https://cloud.example.com/base/", "https://cloud.example.com/base"),
  ("http://cloud.example.com", None), ("https://192.168.0.1", None), ("https://[::1]", None), ("https://cloud", None),
  ("https://cloud.example.com#x", None), ("https://-bad.example.com", None), ("https://cloud.example.test", None),
  ("ftp://cloud.example.com", None), (None, None), ("https://" + "a" * 250 + ".com", None),
])
def test_clean_endpoint(api, value, expected):
  assert api.clean_endpoint(value) == expected


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
      r = await client.post("/api/wayon_key/register", json={"confirmed": True, "invite": invite_text()}, headers=same_origin(client, await csrf(client)))
      return r.status, await r.json()
  status, reply = run(body())
  assert status == 409 and "이전 등록 시도" in reply["error"]
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
      r = await client.post("/api/wayon_key/register", json={"confirmed": True, "invite": invite_text()}, headers=same_origin(client, await csrf(client)))
      return r.status
  assert run(body()) == 409
  assert path.read_text() == '{"someone": "else"}'
  assert api.pending_path().exists()
  assert not list(path.parent.glob(".*.tmp"))


def test_a_dangling_symlink_in_place_of_the_key_file_is_not_followed(api, monkeypatch, tmp_path):
  cloud = Cloud()
  app, path = make_app(api, monkeypatch, tmp_path, cloud)
  path.parent.mkdir(parents=True, exist_ok=True)
  target = tmp_path / "elsewhere.json"
  path.symlink_to(target)
  async def body():
    async with TestClient(TestServer(app)) as client:
      r = await client.post("/api/wayon_key/register", json={"confirmed": True, "invite": invite_text()}, headers=same_origin(client, await csrf(client)))
      status = await (await client.get("/api/wayon_key")).json()
      return r.status, status
  code, status = run(body())
  assert code == 409
  assert not target.exists() and path.is_symlink()
  # Refused before any pending key or cloud call, and the page does not offer registration.
  assert cloud.calls == [] and not api.pending_path().exists()
  assert status["can_register"] is False


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
        r = await client.post("/api/wayon_key/register", json={"confirmed": True, "invite": invite_text()}, headers=same_origin(client, await csrf(client)))
        return r.status
    assert run(body()) == 429
    assert cloud.calls == []
  finally:
    os.close(fd)


def test_cloud_post_collects_a_reply_that_arrives_in_pieces_and_caps_large_ones(api):
  async def body():
    async def split(request):
      reply = web.StreamResponse(status=200, headers={"Content-Type": "application/json"})
      await reply.prepare(request)
      await reply.write(b'{"ok": true, "code": "')
      await asyncio.sleep(0.2)
      await reply.write(CODE.encode() + b'", "expiresAt": "2026-11-12T03:30:00.000Z"}')
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
  assert split == (200, {"ok": True, "code": CODE, "expiresAt": "2026-11-12T03:30:00.000Z"})
  assert huge == (200, {})
  assert redirect[0] == 302  # never followed


def test_an_unconfirmed_pending_write_stops_before_the_cloud(api, monkeypatch, tmp_path):
  cloud = Cloud()
  app, path = make_app(api, monkeypatch, tmp_path, cloud)
  def broken(directory):
    raise OSError("EIO")
  monkeypatch.setattr(api, "fsync_directory", broken)
  async def body():
    async with TestClient(TestServer(app)) as client:
      r = await client.post("/api/wayon_key/register", json={"confirmed": True, "invite": invite_text()}, headers=same_origin(client, await csrf(client)))
      return r.status
  assert run(body()) == 500
  assert cloud.calls == [] and not path.exists()


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
      first = await client.post("/api/wayon_key/register", json={"confirmed": True, "invite": invite_text()}, headers=headers)
      kept = api.pending_path().exists()
      second = await client.post("/api/wayon_key/register", json={"confirmed": True, "invite": invite_text()}, headers=headers)
      return first.status, kept, second.status, await second.json()
  first, kept, second, reply = run(body())
  assert first == 500 and kept and path.exists()
  assert second == 200 and reply["device_id"] == DONGLE
  assert not api.pending_path().exists() and len(cloud.calls) == 1
