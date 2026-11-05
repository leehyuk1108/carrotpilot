"""CAN subset stream for the phone live-view app (/ws/viz_can).

The phone renders everything; this endpoint only forwards the few dozen GM frames the live view needs that are not in
any cereal service (front-camera object tracks and types, lead brake/turn lamps, stock lane-marking types, raw radar
targets, ACC gap, outside temperature). It runs only while a client is connected: it then subscribes to `can`
(not conflated), drains it every 50 ms, reads only the address of each frame (dat/src only for wanted addresses) and
sends one small binary frame:

  b'VC' u8 version=1, u16 count (LE), then count x (u8 bus, u16 addr LE, u16 age_ms LE, u8 len, data[len])

Read-only: never publishes cereal messages and never sends CAN.
"""
from __future__ import annotations

import asyncio
import json
import struct
import time
from typing import Any, Iterable

from aiohttp import WSMsgType, web

VERSION = 1
INTERVAL_S = 0.05
MAX_FRAMES = 2000
WANTED: dict[int, frozenset[int]] = {
  # bus 1 (object/camera): lane info 0x350-0x356, camera objects 0x420-0x42C / 0x440-0x44C, radar 0x460-0x47F, LHT 0x135
  1: frozenset(set(range(0x350, 0x357)) | set(range(0x420, 0x42D)) | set(range(0x440, 0x44D)) | set(range(0x460, 0x480)) | {0x135}),
  0: frozenset({0x140, 0x142, 0x3FC, 0x1F5}),   # high beam, lamps, outside air temperature, gear
  2: frozenset({0x510}),                         # ACC time gap / distance
}
ALL_ADDRS = frozenset().union(*WANTED.values())


def extract(raws: Iterable[bytes], log_module: Any) -> list[tuple[int, list[tuple[int, bytes, int]]]]:
  """Raw `can` events -> [(logMonoTime, [(address, dat, src), ...])] keeping only candidate addresses."""
  out = []
  for raw in raws:
    with log_module.Event.from_bytes(raw) as event:
      sel = []
      for frame in event.can:
        address = frame.address
        if address in ALL_ADDRS:
          sel.append((address, bytes(frame.dat), frame.src))
      out.append((event.logMonoTime, sel))
  return out


def _frames_of(entry: Any) -> tuple[int, Iterable[Any]]:
  nanos, frames = entry[0], entry[1]
  return int(nanos), frames


def pack_frames(batches: Iterable[Any], now_ns: int) -> bytes | None:
  """batches: [(logMonoTime_ns, [(address, dat, src), ...]), ...] as produced by extract()."""
  body = bytearray(); count = 0
  for entry in batches:
    nanos, frames = _frames_of(entry)
    age_ms = max(0, min(65535, (now_ns - nanos) // 1_000_000))
    for frame in frames:
      address, dat, src = frame[0], frame[1], frame[2]
      wanted = WANTED.get(src)
      if wanted is None or address not in wanted or len(dat) > 64:
        continue
      body += struct.pack('<BHHB', src, address, age_ms, len(dat)) + bytes(dat)
      count += 1
      if count >= MAX_FRAMES:
        break
    if count >= MAX_FRAMES:
      break
  if count == 0:
    return None
  return b'VC' + bytes([VERSION]) + struct.pack('<H', count) + bytes(body)


class VizCanHub:
  def __init__(self) -> None:
    self.clients: set[web.WebSocketResponse] = set()
    self.task: asyncio.Task | None = None

  async def register(self, ws: web.WebSocketResponse) -> None:
    self.clients.add(ws)
    if self.task is None or self.task.done():
      self.task = asyncio.create_task(self._loop())

  def unregister(self, ws: web.WebSocketResponse) -> None:
    self.clients.discard(ws)

  async def _loop(self) -> None:
    import openpilot.cereal.messaging as messaging
    from openpilot.cereal import log
    sock = messaging.sub_sock('can', conflate=False)
    try:
      while self.clients:
        raws = messaging.drain_sock_raw(sock)
        payload = pack_frames(extract(raws, log), time.monotonic_ns()) if raws else None
        if payload:
          for ws in list(self.clients):
            if ws.closed:
              self.clients.discard(ws)
              continue
            try:
              await ws.send_bytes(payload)
            except Exception:
              self.clients.discard(ws)
        await asyncio.sleep(INTERVAL_S)
    finally:
      try:
        sock.close()
      except Exception:
        pass


async def ws_viz_can(request: web.Request) -> web.WebSocketResponse:
  hub: VizCanHub = request.app['viz_can_hub']
  ws = web.WebSocketResponse(heartbeat=20, compress=False)
  await ws.prepare(request)
  await ws.send_str(json.dumps({'type': 'hello', 'stream': 'viz_can', 'version': VERSION, 'intervalMs': int(INTERVAL_S * 1000)}))
  await hub.register(ws)
  try:
    async for msg in ws:
      if msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.ERROR):
        break
  finally:
    hub.unregister(ws)
  return ws


def register(app: web.Application) -> None:
  app['viz_can_hub'] = VizCanHub()
  app.router.add_get('/ws/viz_can', ws_viz_can)
