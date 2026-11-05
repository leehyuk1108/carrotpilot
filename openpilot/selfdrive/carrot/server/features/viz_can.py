"""CAN subset stream for the phone live-view app (/ws/viz_can).

The phone renders everything; this endpoint only forwards the GM frames the live view needs that are not in any cereal
service (front-camera object tracks and types, lead brake/turn lamps, stock lane-marking types, raw radar targets,
ACC gap, outside temperature). It runs only while a client is connected: it then subscribes to `can` (not conflated),
drains it every 100 ms and sends one binary frame:

  b'VC' u8 version=2, u16 count (LE), then count x 14-byte records:
  u8 bus, u16 addr LE, u16 age_ms LE, u8 len, data[8] (zero padded; frames longer than 8 bytes are not sent)

CPU (measured on the comma with a stored drive log, 5,100 frames/s): touching every frame through pycapnp costs
3.2 % of one core at 50 ms batches. pack_raw() reads the drained `can` events' capnp bytes directly with a handful of
numpy calls per batch (bus/address of all frames at once, data only for wanted frames): 1.5 % at 50 ms, 0.9 % at the
100 ms used here. Any layout it does not expect falls back to pycapnp. Read-only: never publishes cereal messages and
never sends CAN.
"""
from __future__ import annotations

import asyncio
import json
import struct
import time
from typing import Any, Iterable

import numpy as np
from aiohttp import WSMsgType, web

VERSION = 2
INTERVAL_S = 0.1                 # 10 Hz batches: per-batch overhead dominates on the comma CPU; camera objects come at 25 Hz
MAX_FRAMES = 4000
WANTED: dict[int, frozenset[int]] = {
  # bus 1 (object/camera): lane info 0x350-0x356, camera objects 0x420-0x42C / 0x440-0x44C, radar 0x460-0x47F, LHT 0x135
  1: frozenset(set(range(0x350, 0x357)) | set(range(0x420, 0x42D)) | set(range(0x440, 0x44D)) | set(range(0x460, 0x480)) | {0x135}),
  0: frozenset({0x140, 0x142, 0x3FC, 0x1F5}),   # high beam, lamps, outside air temperature, gear
  2: frozenset({0x510}),                         # ACC time gap / distance
}
ALL_ADDRS = frozenset().union(*WANTED.values())
REC = np.dtype([('bus', 'u1'), ('addr', '<u2'), ('age', '<u2'), ('len', 'u1'), ('dat', 'u1', (8,))])
_WANTED_KEYS = np.array(sorted((src << 32) | a for src, addrs in WANTED.items() for a in addrs), dtype=np.uint64)
_LANE8 = np.arange(8)


def _payload(rec: np.ndarray) -> bytes | None:
  if len(rec) == 0:
    return None
  rec = rec[:MAX_FRAMES]
  return b'VC' + bytes([VERSION]) + struct.pack('<H', len(rec)) + rec.tobytes()


# ---------- reference path (pycapnp) ----------
_FIELDS: tuple[Any, Any, Any] | None = None


def extract(raws: Iterable[bytes], log_module: Any) -> list[tuple[int, list[tuple[int, bytes, int]]]]:
  """Raw `can` events -> [(logMonoTime, [(address, dat, src), ...])] for wanted (bus, address) frames."""
  global _FIELDS
  out = []
  for raw in raws:
    with log_module.Event.from_bytes(raw) as event:
      frames = event.can
      sel = []
      if len(frames):
        if _FIELDS is None:
          fields = frames[0].schema.fields
          _FIELDS = (fields['address'], fields['dat'], fields['src'])
        addr_f, dat_f, src_f = _FIELDS
        for frame in frames:
          address = frame._get_by_field(addr_f)
          if address in ALL_ADDRS:
            src = frame._get_by_field(src_f)
            if address in WANTED.get(src, ()):
              sel.append((address, bytes(frame._get_by_field(dat_f)), src))
      out.append((event.logMonoTime, sel))
  return out


def pack_records(batches: Iterable[Any], now_ns: int) -> bytes | None:
  """[(logMonoTime_ns, [(address, dat, src), ...]), ...] -> wire payload (or None if nothing to send)."""
  rows = []
  for nanos, frames in batches:
    age = max(0, min(65535, (now_ns - int(nanos)) // 1_000_000))
    for address, dat, src in frames:
      if address in WANTED.get(src, ()) and len(dat) <= 8:
        rows.append((src, address, age, len(dat), tuple(bytes(dat).ljust(8, b'\0'))))
  return _payload(np.array(rows, dtype=REC)) if rows else None


# ---------- fast path (direct capnp reads) ----------
_LUT = np.zeros(4 * 2048, dtype=bool)                # (bus << 11 | 11-bit address) -> wanted
for _src, _addrs in WANTED.items():
  _LUT[[(_src << 11) | a for a in _addrs]] = True
_Q = struct.Struct('<Q')


def _s30(v: int) -> int:
  return v - (1 << 30) if v & (1 << 29) else v


def _can_list(raw: bytes) -> tuple[int, int, int] | None:
  """Single-segment `can` Event whose List(CanData) has 1 data + 1 pointer word per element
  -> (logMonoTime, first element word, count), else None."""
  if len(raw) < 24 or len(raw) % 8 or raw[0:4] != b'\0\0\0\0':
    return None
  nwords = (len(raw) - 8) // 8
  if int.from_bytes(raw[4:8], 'little') != nwords:
    return None
  root = _Q.unpack_from(raw, 8)[0]
  if root & 3 != 0:
    return None
  ev = 1 + _s30((root & 0xFFFFFFFF) >> 2)
  ev_dw = (root >> 32) & 0xFFFF
  pi = ev + ev_dw                                   # union payload (List(CanData)) is pointer slot 0
  if ev_dw < 1 or pi >= nwords:
    return None
  log_mono = _Q.unpack_from(raw, 8 + 8 * ev)[0]
  lp = _Q.unpack_from(raw, 8 + 8 * pi)[0]
  if lp == 0:
    return log_mono, 0, 0
  if lp & 3 != 1 or ((lp >> 32) & 7) != 7:          # list of inline composite structs
    return None
  tag_i = pi + 1 + _s30((lp & 0xFFFFFFFF) >> 2)
  if not 0 <= tag_i < nwords:
    return None
  tag = _Q.unpack_from(raw, 8 + 8 * tag_i)[0]
  count = _s30((tag & 0xFFFFFFFF) >> 2)
  if count < 0 or (tag >> 32) != 0x0001_0001 or tag_i + 1 + 2 * count > nwords:
    return None
  return log_mono, tag_i + 1, count


def pack_raw_fast(raws: list[bytes], now_ns: int) -> tuple[bool, bytes | None]:
  """-> (ok, payload). ok=False means an unexpected layout: use the pycapnp path for this batch."""
  starts, counts, ages = [], [], []
  base = 0
  for raw in raws:
    head = _can_list(raw)
    if head is None:
      return False, None
    log_mono, first, count = head
    if count:
      starts.append(base + 1 + first)               # +1: the segment-table word in front of each message
      counts.append(count)
      ages.append(max(0, min(65535, (now_ns - log_mono) // 1_000_000)))
    base += len(raw) // 8
  if not counts:
    return True, None
  joined = b''.join(raws)
  words = np.frombuffer(joined, dtype='<u8')
  blk = np.concatenate([words[st:st + 2 * c] for st, c in zip(starts, counts)])
  w16 = blk.view('<u2').reshape(-1, 8)              # per frame: addr lo/hi, busTime, src | dat pointer (4 x u16)
  addr, src = w16[:, 0], w16[:, 3] & 0xFF
  hit = (w16[:, 1] == 0) & (addr < 2048) & (src < 4)
  hit[hit] = _LUT[(src[hit] << 11) | addr[hit]]
  hits = np.flatnonzero(hit)
  if len(hits) == 0:
    return True, None
  cum = np.cumsum(counts)
  msg = np.searchsorted(cum, hits, side='right')
  local = hits - (cum - counts)[msg]
  dp_i = np.asarray(starts)[msg] + 2 * local + 1     # absolute word index of each hit's dat pointer
  dp = blk[2 * hits + 1]
  null = dp == 0
  if not np.all(null | (((dp & np.uint64(3)) == 1) & (((dp >> np.uint64(32)) & np.uint64(7)) == 2))):
    return False, None
  off = ((dp & np.uint64(0xFFFFFFFF)) >> np.uint64(2)).astype(np.int64)
  off -= (off >= (1 << 29)) * (1 << 30)
  n = np.where(null, 0, (dp >> np.uint64(35)).astype(np.int64))
  start = (dp_i + 1 + off) * 8
  if np.any(~null & ((start < 0) | (start + n > len(joined)))):
    return False, None
  keep = n <= 8
  if not keep.all():
    hits, start, n, msg = hits[keep], start[keep], n[keep], msg[keep]
  buf = np.frombuffer(joined, dtype=np.uint8)
  dat = buf[np.minimum(start[:, None] + _LANE8, len(buf) - 1)]
  dat[_LANE8[None, :] >= n[:, None]] = 0
  rec = np.empty(len(n), dtype=REC)
  rec['bus'] = src[hits]; rec['addr'] = addr[hits]; rec['len'] = n
  rec['age'] = np.asarray(ages)[msg]
  rec['dat'] = dat
  return True, _payload(rec)


def pack_raw(raws: list[bytes], log_module: Any, now_ns: int) -> bytes | None:
  ok, payload = pack_raw_fast(raws, now_ns)
  return payload if ok else pack_records(extract(raws, log_module), now_ns)


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
        payload = pack_raw(raws, log, time.monotonic_ns()) if raws else None
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
