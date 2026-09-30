import socket
import json

import pytest
from websocket import ABNF, WebSocketTimeoutException

from openpilot.system.hylink.policy import UploadBackoff
from openpilot.system.hylink.relay import ObservedWebSocket, Relay, RelayRetry, TARGET


class WebSocket:
  def __init__(self, frames):
    self.frames = iter(frames)
  def settimeout(self, timeout):
    assert timeout <= 0.5
  def recv_data(self, **kwargs):
    return next(self.frames)


def test_relay_only_targets_loopback_video():
  assert TARGET == ("127.0.0.1", 8765)


def test_stale_forward_thread_cannot_close_new_viewer():
  relay = Relay(lambda: True)
  old, old_peer = socket.socketpair()
  new, new_peer = socket.socketpair()
  try:
    relay.local = new
    relay.close_local(old)
    assert relay.local is new
    relay.close_local(new)
    assert relay.local is None
  finally:
    old.close()
    old_peer.close()
    new.close()
    new_peer.close()


def test_onroad_prevents_local_connection(monkeypatch):
  relay = Relay(lambda: False)
  def forbidden(*a, **k):
    pytest.fail("Must not connect onroad")
  monkeypatch.setattr(socket, "create_connection", forbidden)
  relay.open_local(None)
  assert relay.local is None


def test_no_arbitrary_ssh_or_vehicle_command(monkeypatch):
  relay = Relay(lambda: True)
  monkeypatch.setattr(relay, "open_local", lambda ws: pytest.fail("Unknown command must not connect"))
  relay.connected(WebSocket([(ABNF.OPCODE_TEXT, b"wayon-ssh-authorize-v1.untrusted"),
                             (ABNF.OPCODE_TEXT, b"remote-control"), (ABNF.OPCODE_CLOSE, b"")]))


def test_peer_close_and_ignition_closes_local():
  allowed = [True]
  relay = Relay(lambda: allowed[0])
  local, peer = socket.socketpair()
  relay.local = local
  class WS(WebSocket):
    def recv_data(self, **kwargs):
      allowed[0] = False
      raise WebSocketTimeoutException()
  try:
    relay.connected(WS([]))
    assert relay.local is None
    assert peer.recv(1) == b""
  finally:
    peer.close()


def test_forward_failure_diagnostics_identify_stage_without_payload(monkeypatch, capsys):
  class Local:
    def settimeout(self, value):
      pass
    def recv(self, size):
      return b"private-camera-content"
    def shutdown(self, how):
      pass
    def close(self):
      pass

  class InlineThread:
    def __init__(self, target, **kwargs):
      self.target = target
    def start(self):
      self.target()

  class FailedSend:
    closed = False
    def send(self, data, opcode):
      raise WebSocketTimeoutException("private-auth-or-host-text")
    def close(self, **kwargs):
      self.closed = True

  monkeypatch.setattr(socket, "create_connection", lambda *args, **kwargs: Local())
  monkeypatch.setattr("openpilot.system.hylink.relay.threading.Thread", InlineThread)
  ws = FailedSend()
  relay = Relay(lambda: True)
  relay.open_local(ws)
  output = capsys.readouterr().out
  events = [json.loads(line.split(": ", 1)[1]) for line in output.splitlines()]
  assert events[-1]["event"] == "peer_end"
  assert events[-1]["phase"] == "cloud_send"
  assert events[-1]["reason"] == "WebSocketTimeoutException"
  assert events[-1]["unexpected"] is True
  assert "private-" not in output
  assert ws.closed and relay.local is None


def test_live_recovery_is_fast_but_bounded_during_an_outage():
  retry = RelayRetry(UploadBackoff(jitter=lambda: 0))
  assert retry.delay(True, 10) == 1
  # Subsequent failed handshakes have no local peer, but retain the budget.
  assert retry.delay(False, 0) == 2
  assert retry.delay(False, 0) == 4
  assert [retry.delay(True, 1) for _ in range(5)] == [30, 60, 120, 240, 300]


def test_idle_and_ssh_retries_keep_low_frequency_backoff():
  retry = RelayRetry(UploadBackoff(jitter=lambda: 0))
  assert [retry.delay(False, 0) for _ in range(4)] == [30, 60, 120, 240]
  assert retry.delay(False, 61) == 30


def test_only_a_stable_connection_rearms_quick_live_recovery():
  retry = RelayRetry(UploadBackoff(jitter=lambda: 0))
  assert [retry.delay(True, 1) for _ in range(3)] == [1, 2, 4]
  assert retry.delay(True, 60) == 30
  assert retry.delay(True, 61) == 1


def test_close_frame_code_logged_without_reason_text(monkeypatch, capsys):
  frame = ABNF.create_frame(b"\x03\xe8private-close-reason", ABNF.OPCODE_CLOSE)
  monkeypatch.setattr("websocket.WebSocket.recv_frame", lambda self: frame)
  assert ObservedWebSocket().recv_frame() is frame
  output = capsys.readouterr().out
  assert '"code":1000' in output and "private-close-reason" not in output


def test_key_replacement_interrupts_old_relay_backoff(monkeypatch):
  from types import SimpleNamespace
  from openpilot.system.hylink import relay as module
  active, clock, tokens, sleeps = [True], [0.0], [], []
  config = {"token": "old-synthetic"}
  monkeypatch.setattr(module, "read_config", lambda _: config.copy())
  monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
  def sleep(seconds):
    sleeps.append(seconds)
    clock[0] += seconds
    config["token"] = "new-synthetic"
  monkeypatch.setattr(module.time, "sleep", sleep)
  def connect(*args, **kwargs):
    tokens.append(kwargs["header"][0])
    if len(tokens) == 2:
      active[0] = False
    return SimpleNamespace(close=lambda **kwargs: None)
  monkeypatch.setattr(module, "create_connection", connect)
  relay = Relay(lambda: active[0])
  monkeypatch.setattr(relay, "connected", lambda ws: None)
  module.run_relay(relay, object())
  assert tokens == ["Authorization: Bearer old-synthetic", "Authorization: Bearer new-synthetic"]
  assert sleeps == [0.5]
