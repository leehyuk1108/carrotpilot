"""GM front-camera object kinds and lead lamps for the Navdy overlay (display only, never control).

The stock front camera (Traverse SDGM object bus, panda bus 1) reports up to 12 tracked objects in
F_Vision_Obj_Track_1..12 (0x421-0x426, 0x441-0x446) and the lamps of track 1 in F_Vision_Obj_Track_1_B
(0x427), see gm_global_a_object.dbc. Field meanings were checked against road video (Nov 2026 CAN map):
object type 1 car, 2 truck/bus, 3 motorcycle, 4 pedestrian; track-1 brake lamp value 2 = lit; turn code
2 left, 3 right (4 hazard is unreliable and ignored). The same decoding drives the phone live view.

Frames come from the bridge's existing door-monitor CAN subscription (no extra msgq reader: readers are
never released, and more than 40 resets every reader of the 'can' queue, card included). Track 1's lamp
frame (B) is used only when it carries the same burst id as the newest track-1 frame (A) and arrives
within 20 ms of it; real logs: 8,888/8,888 B frames matched the preceding A burst, at most 13 ms later.
"""
from __future__ import annotations

import math
import threading

OBJECT_BUS = 1
TRACK_ADDRESSES = (0x421, 0x422, 0x423, 0x424, 0x425, 0x426, 0x441, 0x442, 0x443, 0x444, 0x445, 0x446)
LAMP_ADDRESS = 0x427
WANTED = frozenset(TRACK_ADDRESSES + (LAMP_ADDRESS,))
KINDS = {1: 'car', 2: 'truck', 3: 'moto', 4: 'ped'}
OBJECT_MAX_AGE = 0.25  # tracks arrive at 25 Hz
TURN_HOLD = 1.2  # the code drops between flashes; keep the side while it keeps coming back
CAMERA_AHEAD_OF_RADAR = -1.6  # camera ranges are ~1.6 m longer than front-radar dRel
PARSE_EVENTS = 8  # newest 'can' events (10 ms each) fed per door-monitor loop (100 ms)
LAMP_PAIR_WINDOW = 0.02  # B must follow its A burst within this (seen: <= 13 ms)


def _signed(value: int, bits: int) -> int:
  return value - (1 << bits) if value & (1 << (bits - 1)) else value


def decode_track(data: bytes) -> dict | None:
  """One F_Vision_Obj_Track frame -> object in radar coordinates (distance, lateral right-positive)."""
  if len(data) != 8:
    return None
  ident = data[0] >> 2
  kind = KINDS.get((data[1] >> 3) & 0xF)
  if ident == 0 or kind is None:
    return None
  azimuth = math.radians(_signed(((data[1] & 7) << 7) | (data[2] >> 1), 10) * 0.1)
  rng = (((data[2] & 1) << 11) | (data[3] << 3) | (data[4] >> 5)) * 0.1
  if rng < 1.0:
    return None
  # Camera azimuth is left-positive like liveTracks yRel (checked on 1,026 matched pairs).
  return {'id': ident, 'kind': kind, 'distance': rng * math.cos(azimuth) + CAMERA_AHEAD_OF_RADAR,
          'lateral': -rng * math.sin(azimuth), 'width': (data[7] & 0x3F) * 0.25}


def decode_lamps(data: bytes) -> tuple[bool, int] | None:
  if len(data) != 8:
    return None
  return ((data[4] >> 5) & 3) == 2, ((data[3] & 3) << 1) | (data[4] >> 7)


class CameraObjects:
  """Latest camera frames; fed by the door-monitor thread, read by the bridge loop (locked)."""
  def __init__(self):
    self.lock = threading.Lock()
    self.frames: dict[int, tuple[float, bytes]] = {}
    self.track1 = (0, -1, -math.inf)       # (object id, burst id, time) of the newest track-1 frame A
    self.lamps = None                      # (time, brake, turn code, object id) from a B paired with A
    self.turn_seen = (0, -math.inf, 0)     # (code, time, object id) of the last left/right observation

  def clear(self):
    with self.lock:
      self.frames.clear()
      self.track1 = (0, -1, -math.inf)
      self.lamps = None
      self.turn_seen = (0, -math.inf, 0)

  def feed(self, frames, now: float) -> None:
    """frames: iterable of (address, data, src)."""
    with self.lock:
      for address, data, src in frames:
        if src != OBJECT_BUS or address not in WANTED or len(data) != 8:
          continue
        data = bytes(data)
        if address == LAMP_ADDRESS:
          self._lamp_frame(data, now)
          continue
        self.frames[address] = (now, data)
        if address == TRACK_ADDRESSES[0]:
          ident = data[0] >> 2
          if ident != self.track1[0]:
            # Track 1 now carries another object (or none): nothing seen for its predecessor carries over.
            self.lamps = None
            self.turn_seen = (0, -math.inf, 0)
          self.track1 = (ident, data[0] & 3, now)

  def _lamp_frame(self, data: bytes, now: float) -> None:
    ident, burst, seen = self.track1
    if ident == 0 or (data[0] >> 6) != burst or not 0 <= now - seen <= LAMP_PAIR_WINDOW:
      return  # not this observation's B (late, early or from another burst): never attach it
    brake, code = decode_lamps(data)
    self.lamps = (now, brake, code, ident)
    if code in (2, 3):
      self.turn_seen = (code, now, ident)

  def _fresh(self, address: int, now: float) -> bytes | None:
    entry = self.frames.get(address)
    return entry[1] if entry is not None and 0 <= now - entry[0] <= OBJECT_MAX_AGE else None

  def objects(self, now: float) -> list[dict]:
    with self.lock:
      out = []
      for address in TRACK_ADDRESSES:
        data = self._fresh(address, now)
        track = decode_track(data) if data is not None else None
        if track is not None:
          track['track1'] = address == TRACK_ADDRESSES[0]
          out.append(track)
      return out

  def lead_lamps(self, now: float) -> dict | None:
    """Lamps of the object now in camera track 1, or None when there is no fresh paired lamp frame."""
    with self.lock:
      track1 = self._fresh(TRACK_ADDRESSES[0], now)
      if track1 is None or self.lamps is None:
        return None
      seen, brake, _, ident = self.lamps
      if ident != (track1[0] >> 2) or not 0 <= now - seen <= OBJECT_MAX_AGE:
        return None
      code, turn_at, turn_ident = self.turn_seen
      turn = {2: 'left', 3: 'right'}.get(code, '') if turn_ident == ident and 0 <= now - turn_at < TURN_HOLD else ''
      return {'brake': brake, 'turn': turn}


def frames_from_events(events):
  """Parsed 'can' events -> [(seconds, [(address, data, src)])] for the camera addresses, valid events only."""
  out = []
  for event in events:
    if not event.valid:
      continue
    out.append((event.logMonoTime / 1e9, [(frame.address, frame.dat, frame.src) for frame in event.can if frame.address in WANTED]))
  return out
