from __future__ import annotations

import struct

from openpilot.cereal import log
from openpilot.selfdrive.carrot.server.features.viz_can import WANTED, extract, pack_frames


def _unpack(payload: bytes):
  assert payload[:3] == b'VC\x01'
  count = struct.unpack_from('<H', payload, 3)[0]
  out, o = [], 5
  for _ in range(count):
    bus, addr, age, n = struct.unpack_from('<BHHB', payload, o)
    out.append((bus, addr, age, payload[o + 6:o + 6 + n]))
    o += 6 + n
  assert o == len(payload)
  return out


def test_keeps_only_wanted_frames_with_age():
  now = 10_000_000_000
  batches = [
    (now - 30_000_000, [(0x421, b'\x01' * 8, 1), (0x421, b'\x02' * 8, 0), (0x184, b'\x00' * 8, 1)]),
    (now - 5_000_000, [(0x510, b'\x03' * 8, 2), (0x3FC, b'\x04' * 8, 0), (0x461, b'\x05' * 8, 129)]),
  ]
  frames = _unpack(pack_frames(batches, now))
  assert frames == [(1, 0x421, 30, b'\x01' * 8), (2, 0x510, 5, b'\x03' * 8), (0, 0x3FC, 5, b'\x04' * 8)]


def test_nothing_wanted_returns_none():
  assert pack_frames([(0, [(0x184, b'\x00' * 8, 1), (0x421, b'\x00' * 8, 128)])], 0) is None
  assert pack_frames([], 0) is None


def test_age_is_clamped_and_lists_accepted():
  now = 100_000_000_000
  frames = _unpack(pack_frames([[now - 99_000_000_000, [[0x427, b'\x09' * 8, 1]]], [now + 5, [[0x427, b'\x0a' * 8, 1]]]], now))
  assert frames[0][2] == 65535 and frames[1][2] == 0


def test_wanted_sets_cover_live_view_inputs():
  assert {0x420, 0x421, 0x426, 0x427, 0x441, 0x446, 0x350, 0x352, 0x354, 0x355, 0x135, 0x461, 0x474} <= WANTED[1]
  assert 0x3FC in WANTED[0] and 0x510 in WANTED[2]


def test_extract_reads_real_can_events():
  msg = log.Event.new_message(logMonoTime=123, valid=True)
  msg.init('can', 3)
  for i, (addr, src, dat) in enumerate([(0x421, 1, b'\x11' * 8), (0x184, 1, b'\x22' * 8), (0x510, 2, b'\x33' * 8)]):
    msg.can[i].address = addr; msg.can[i].src = src; msg.can[i].dat = dat
  got = extract([msg.to_bytes()], log)
  assert got == [(123, [(0x421, b'\x11' * 8, 1), (0x510, b'\x33' * 8, 2)])]
  frames = _unpack(pack_frames(got, 123 + 7_000_000))
  assert frames == [(1, 0x421, 7, b'\x11' * 8), (2, 0x510, 7, b'\x33' * 8)]
