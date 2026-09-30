"""Authenticated cloud relay for parking video only. No SSH or actuation endpoint."""
import json
import socket
import threading
import time

from websocket import ABNF, WebSocket, WebSocketException, WebSocketTimeoutException, create_connection

from openpilot.common.params import Params
from openpilot.system.hylink.policy import UploadBackoff
from openpilot.system.hylink.runtime import ENDPOINT, media_ready, read_config

TARGET = ("127.0.0.1", 8765)


def relay_event(target, event, **fields):
  # Metadata only: never include auth headers, commands, media or exception text.
  kind = "live" if target == TARGET else "ssh"
  print("Hylink relay " + kind + ": " + json.dumps({"event": event, "at_s": round(time.monotonic(), 3), **fields}, separators=(",", ":")), flush=True)


class RelayRetry:
  """Three quick attempts for a lost live viewer, then the usual quiet backoff."""
  def __init__(self, backoff=None):
    self.backoff = backoff or UploadBackoff()
    self.fast_attempt = 0
    self.recovering_viewer = False

  def delay(self, had_live_viewer, connected_s):
    if connected_s > 60:
      self.backoff.success()
      self.fast_attempt = 0
      self.recovering_viewer = False
    self.recovering_viewer = self.recovering_viewer or had_live_viewer
    if self.recovering_viewer and self.fast_attempt < 3:
      delay = (1, 2, 4)[self.fast_attempt]
      self.fast_attempt += 1
      return delay * (1 + 0.2 * self.backoff.jitter())
    return self.backoff.failure_delay()


class ObservedWebSocket(WebSocket):
  def __init__(self, *args, relay_target=TARGET, **kwargs):
    super().__init__(*args, **kwargs)
    self.relay_target = relay_target

  def recv_frame(self):
    frame = super().recv_frame()
    if frame is not None and frame.opcode == ABNF.OPCODE_CLOSE:
      # Observe before websocket-client's automatic close reply can itself fail.
      code = int.from_bytes(frame.data[:2], "big") if len(frame.data) >= 2 else None
      relay_event(self.relay_target, "cloud_close_frame", code=code)
    return frame


class Relay:
  def __init__(self, allowed, target=TARGET):
    self.allowed = allowed
    self.target = target
    self.local = None
    self.lock = threading.Lock()
    self.peer_opened = False

  def close_local(self, expected=None):
    with self.lock:
      if expected is not None and self.local is not expected:
        return
      local, self.local = self.local, None
    if local:
      try:
        local.shutdown(socket.SHUT_RDWR)
      except OSError:
        pass
      local.close()

  def open_local(self, ws):
    self.close_local()
    if not self.allowed():
      return
    local = socket.create_connection(self.target, timeout=1)
    local.settimeout(0.5)
    with self.lock:
      self.local = local
    self.peer_opened = True
    relay_event(self.target, "peer_open")

    def forward():
      started = time.monotonic()
      sent_bytes = 0
      phase = "guard"
      reason = "guard_closed"
      try:
        while self.allowed():
          phase = "local_read"
          try:
            data = local.recv(65536)
          except TimeoutError:
            continue
          if not data:
            reason = "local_eof"
            break
          phase = "cloud_send"
          ws.send(data, opcode=ABNF.OPCODE_BINARY)
          sent_bytes += len(data)
      except (OSError, WebSocketException) as exc:
        reason = type(exc).__name__
      finally:
        with self.lock:
          unexpected_end = self.local is local
        relay_event(self.target, "peer_end", phase=phase, reason=reason,
                    elapsed_s=round(time.monotonic() - started, 2), sent_bytes=sent_bytes,
                    unexpected=unexpected_end)
        self.close_local(local)
        if unexpected_end:
          # Propagate a camera/SSH EOF to the phone. Otherwise a five-minute
          # session can leave its WebSocket open forever with no further bytes.
          ws.close(timeout=0.5)
    threading.Thread(target=forward, name="hylink-live-forward", daemon=True).start()

  def tick(self):
    pass

  def command(self, command):
    pass

  def connected(self, ws):
    ws.settimeout(0.5)
    relay_event(self.target, "cloud_connected")
    next_ping = time.monotonic() + 20
    try:
      while self.allowed():
        self.tick()
        try:
          opcode, data = ws.recv_data(control_frame=True)
        except WebSocketTimeoutException:
          if time.monotonic() >= next_ping:
            ws.ping(b"wayon-live")
            next_ping = time.monotonic() + 20
          continue
        if opcode == ABNF.OPCODE_TEXT:
          command = data.decode() if isinstance(data, bytes) else data
          if command == "wayon-peer-open":
            self.open_local(ws)
          elif command == "wayon-peer-close":
            self.peer_opened = False
            relay_event(self.target, "peer_close_command")
            self.close_local()
          else:
            self.command(command)
        elif opcode == ABNF.OPCODE_BINARY:
          if len(data) > 8 * 1024 * 1024 + 9:
            raise ValueError("Oversized live control frame")
          with self.lock:
            local = self.local
          if local and self.allowed():
            local.sendall(data)
        elif opcode == ABNF.OPCODE_CLOSE:
          close_code = int.from_bytes(data[:2], "big") if len(data) >= 2 else None
          relay_event(self.target, "cloud_close", code=close_code)
          return
    finally:
      self.close_local()


def run_relay(relay, params, kind="live"):
  allowed = relay.allowed
  retry = RelayRetry()
  while allowed():
    ws = None
    config = {}
    relay.peer_opened = False
    connected_at = time.monotonic()
    try:
      config = read_config(params)
      ws = create_connection(ENDPOINT.replace("https://", "wss://") + "/api/device/relay/" + kind,
                             header=["Authorization: Bearer " + config["token"]],
                             timeout=5, enable_multithread=True, redirect_limit=0,
                             class_=ObservedWebSocket, relay_target=relay.target)
      relay.connected(ws)
    except (OSError, WebSocketException, ValueError) as exc:
      relay_event(relay.target, "connection_error", reason=type(exc).__name__,
                  elapsed_s=round(time.monotonic() - connected_at, 2))
    finally:
      relay.close_local()
      if ws:
        ws.close(timeout=0.5)
    delay = retry.delay(kind == "live" and relay.peer_opened, time.monotonic() - connected_at)
    relay_event(relay.target, "retry_scheduled", delay_s=round(delay, 2))
    retry_at = time.monotonic() + delay
    while allowed() and time.monotonic() < retry_at:
      if read_config(params).get("token") != config.get("token"):
        retry = RelayRetry()
        break  # Resume immediately after an explicit local key replacement.
      time.sleep(0.5)


def main():
  params = Params()
  run_relay(Relay(lambda: media_ready(False, params)), params)


if __name__ == "__main__":
  main()
