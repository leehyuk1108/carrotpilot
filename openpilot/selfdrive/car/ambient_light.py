"""Receive-only Traverse ambient light level for display.

BCM 0x140 byte2 bits1-0 carry the auto-headlamp light sensor state: 1 dark, 2 bright.
On the 11/08 evening drive it read dark for the whole night (17:44-19:55, sun -5 to
-28 deg) with the low beams on, and bus1 0x310 byte0 bits7-6 carried the same value.
The frame repeats at 1 Hz and also on any lamp change.
"""

TRAVERSE_AMBIENT_ADDRESS = 0x140
TRAVERSE_AMBIENT_BUS = 0
AMBIENT_MAX_AGE_NS = 3_000_000_000  # 1 Hz repeat; tolerate a couple of missed frames
AMBIENT_DARK = 1
AMBIENT_BRIGHT = 2


def decode_ambient_dark(data: bytes) -> bool | None:
  """True dark, False bright, None for a short frame or the unused values 0 and 3."""
  if len(data) < 3:
    return None
  level = data[2] & 0x3
  if level == AMBIENT_DARK:
    return True
  if level == AMBIENT_BRIGHT:
    return False
  return None


class TraverseAmbientLight:
  def __init__(self):
    self.sample_ns = -1
    self.dark: bool | None = None

  def update(self, can_packets, now_ns: int) -> bool | None:
    for sample_ns, frames in can_packets:
      if not self.sample_ns <= sample_ns <= now_ns or now_ns - sample_ns > AMBIENT_MAX_AGE_NS:
        continue
      for address, data, bus in frames:
        if address == TRAVERSE_AMBIENT_ADDRESS and bus == TRAVERSE_AMBIENT_BUS:
          dark = decode_ambient_dark(data)
          if dark is not None:
            self.dark = dark
            self.sample_ns = sample_ns
    if self.dark is not None and 0 <= now_ns - self.sample_ns <= AMBIENT_MAX_AGE_NS:
      return self.dark
    return None


def ambient_dark(car_state) -> bool | None:
  """Dark/bright from carState, or None (unsupported, stale, or invalid CAN)."""
  light = getattr(car_state, "ambientLight", None)
  if not getattr(light, "valid", False):
    return None
  if not getattr(car_state, "canValid", True) or getattr(car_state, "canTimeout", False):
    return None
  return bool(getattr(light, "dark", False))
