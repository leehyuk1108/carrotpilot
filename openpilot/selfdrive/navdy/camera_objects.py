"""GM front-camera object kinds and lead lamps for the Navdy overlay (display only, never control).

The stock front camera (Traverse SDGM object bus, panda bus 1) reports up to 12 tracked objects in
F_Vision_Obj_Track_1..12 (0x421-0x426, 0x441-0x446) and the lamps of track 1 in F_Vision_Obj_Track_1_B
(0x427), see gm_global_a_object.dbc. Field meanings were checked against road video (Nov 2026 CAN map):
object type 1 car, 2 truck/bus, 3 motorcycle, 4 pedestrian; track-1 brake lamp value 2 = lit; turn code
2 left, 3 right (4 hazard is unreliable and ignored). The same decoding drives the phone live view.
"""
from __future__ import annotations

import math

OBJECT_BUS = 1
TRACK_ADDRESSES = (0x421, 0x422, 0x423, 0x424, 0x425, 0x426, 0x441, 0x442, 0x443, 0x444, 0x445, 0x446)
LAMP_ADDRESS = 0x427
WANTED = frozenset(TRACK_ADDRESSES + (LAMP_ADDRESS,))
KINDS = {1: 'car', 2: 'truck', 3: 'moto', 4: 'ped'}
OBJECT_MAX_AGE = 0.25  # tracks arrive at 25 Hz
TURN_HOLD = 1.2  # the code drops between flashes; keep the side while it keeps coming back
CAMERA_AHEAD_OF_RADAR = -1.6  # camera ranges are ~1.6 m longer than front-radar dRel
PARSE_EVENTS = 8  # newest 'can' events (10 ms each) parsed per display tick


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
  def __init__(self):
    self.frames: dict[int, tuple[float, bytes]] = {}
    self.turn_seen = (0, -math.inf)

  def clear(self):
    self.frames.clear()
    self.turn_seen = (0, -math.inf)

  def feed(self, frames, now: float) -> None:
    """frames: iterable of (address, data, src) as in can_capnp_to_list."""
    for address, data, src in frames:
      if src != OBJECT_BUS or address not in WANTED or len(data) != 8:
        continue
      data = bytes(data)
      self.frames[address] = (now, data)
      if address == LAMP_ADDRESS:
        lamps = decode_lamps(data)
        if lamps is not None and lamps[1] in (2, 3):
          self.turn_seen = (lamps[1], now)

  def _fresh(self, address: int, now: float) -> bytes | None:
    entry = self.frames.get(address)
    return entry[1] if entry is not None and 0 <= now - entry[0] <= OBJECT_MAX_AGE else None

  def objects(self, now: float) -> list[dict]:
    out = []
    for address in TRACK_ADDRESSES:
      data = self._fresh(address, now)
      track = decode_track(data) if data is not None else None
      if track is not None:
        track['track1'] = address == TRACK_ADDRESSES[0]
        out.append(track)
    return out

  def lead_lamps(self, now: float) -> dict | None:
    """Lamps of camera track 1, or None when that frame is stale."""
    data = self._fresh(LAMP_ADDRESS, now)
    lamps = decode_lamps(data) if data is not None else None
    if lamps is None:
      return None
    code, seen = self.turn_seen
    turn = {2: 'left', 3: 'right'}.get(code, '') if 0 <= now - seen < TURN_HOLD else ''
    return {'brake': lamps[0], 'turn': turn}


def camera_frames(raw_events, log_module):
  """Raw 'can' events -> [(seconds, [(address, data, src)])] keeping only the camera addresses.

  Reads one field per frame and the rest only for the 13 wanted addresses, so a 200 ms drain
  (~1000 frames) stays far cheaper than converting every frame.
  """
  out = []
  for raw in raw_events:
    with log_module.Event.from_bytes(raw) as event:
      frames = [(frame.address, frame.dat, frame.src) for frame in event.can if frame.address in WANTED]
      out.append((event.logMonoTime / 1e9, frames))
  return out


class CameraFeed:
  """Drains raw 'can' once per display tick (GM, onroad) and keeps the latest camera frames."""
  def __init__(self, messaging=None, parse=None):
    self.messaging = messaging
    self.parse = parse
    self.sock = None
    self.objects = CameraObjects()
    self.error = ''

  def close(self):
    self.sock = None
    self.objects.clear()

  def poll(self, enabled: bool) -> None:
    if not enabled:
      if self.sock is not None:
        self.close()
      return
    try:
      if self.messaging is None:
        from openpilot.cereal import messaging
        self.messaging = messaging
      if self.parse is None:
        from openpilot.cereal import log
        self.parse = lambda raw: camera_frames(raw, log)
      if self.sock is None:
        self.sock = self.messaging.sub_sock('can', conflate=False)
      raw = self.messaging.drain_sock_raw(self.sock)
      if raw:
        # Tracks and lamps repeat every 40 ms: the newest ~80 ms hold all of them, so skip parsing older events.
        for seconds, frames in self.parse(raw[-PARSE_EVENTS:]):
          self.objects.feed(frames, seconds)
      self.error = ''
    except Exception as error:
      # The camera overlay must never take down telemetry or navigation forwarding.
      self.error = f'{type(error).__name__}: {error}'[:160]
      self.close()

  def status(self, now: float) -> dict:
    return {'objects': len(self.objects.objects(now)), 'lamps': self.objects.lead_lamps(now) is not None,
            'error': self.error}
