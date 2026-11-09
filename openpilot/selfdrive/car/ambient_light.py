"""Receive-only Traverse ambient light readings for display.

BCM 0x140 byte2 bits1-0 carry the auto-headlamp light sensor state: 1 dark, 2 bright.
On the 11/08 evening drive it read dark for the whole night (17:44-19:55, sun -5 to
-28 deg) with the low beams on, and bus1 0x310 byte0 bits7-6 carried the same value.
The frame repeats at 1 Hz and also on any lamp change. It also reads dark inside
daytime tunnels (the low beams come on with it).

Bus1 0x135 byte0 is the front camera's high-beam assist view at 10 Hz: 0 nothing,
1 street or city lights (also lit tunnels at night), 2 high beam advised. The real
high beam followed every 2 within 0.2 s on 11/08. It reads 0 all day, inside lit
daytime tunnels too, and can read 0 at night until the car first moves, so a 0 only
means "no lights" once the camera has reported 1 or 2 in the current darkness.
"""

TRAVERSE_AMBIENT_ADDRESS = 0x140
TRAVERSE_AMBIENT_BUS = 0
AMBIENT_MAX_AGE_NS = 3_000_000_000  # 1 Hz repeat; tolerate a couple of missed frames
AMBIENT_DARK = 1
AMBIENT_BRIGHT = 2

TRAVERSE_CAMERA_LIGHTS_ADDRESS = 0x135
TRAVERSE_CAMERA_LIGHTS_BUS = 1
CAMERA_LIGHTS_MAX_AGE_NS = 1_000_000_000  # 10 Hz
CAMERA_NONE = 0
CAMERA_CITY_LIGHTS = 1
CAMERA_HIGH_BEAM = 2


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


def decode_camera_lights(data: bytes) -> int | None:
  """0 nothing, 1 street/city lights, 2 high beam advised; None for empty or unknown."""
  if len(data) < 1:
    return None
  value = data[0]
  return value if value in (CAMERA_NONE, CAMERA_CITY_LIGHTS, CAMERA_HIGH_BEAM) else None


class _FreshCanValue:
  """Last decodable value of one address/bus, while it is no older than max_age_ns."""
  address = 0
  bus = 0
  max_age_ns = 0

  def __init__(self):
    self.sample_ns = -1
    self.value = None

  @staticmethod
  def decode(data: bytes):
    raise NotImplementedError

  def update(self, can_packets, now_ns: int):
    for sample_ns, frames in can_packets:
      if not self.sample_ns <= sample_ns <= now_ns or now_ns - sample_ns > self.max_age_ns:
        continue
      for address, data, bus in frames:
        if address == self.address and bus == self.bus:
          value = self.decode(data)
          if value is not None:
            self.value = value
            self.sample_ns = sample_ns
    if self.value is not None and 0 <= now_ns - self.sample_ns <= self.max_age_ns:
      return self.value
    return None


class TraverseAmbientLight(_FreshCanValue):
  address = TRAVERSE_AMBIENT_ADDRESS
  bus = TRAVERSE_AMBIENT_BUS
  max_age_ns = AMBIENT_MAX_AGE_NS
  decode = staticmethod(decode_ambient_dark)


class TraverseCameraLights(_FreshCanValue):
  address = TRAVERSE_CAMERA_LIGHTS_ADDRESS
  bus = TRAVERSE_CAMERA_LIGHTS_BUS
  max_age_ns = CAMERA_LIGHTS_MAX_AGE_NS
  decode = staticmethod(decode_camera_lights)


def _can_ok(car_state) -> bool:
  return bool(getattr(car_state, "canValid", True)) and not getattr(car_state, "canTimeout", False)


def ambient_dark(car_state) -> bool | None:
  """Dark/bright from carState, or None (unsupported, stale, or invalid CAN)."""
  light = getattr(car_state, "ambientLight", None)
  if not getattr(light, "valid", False) or not _can_ok(car_state):
    return None
  return bool(getattr(light, "dark", False))


def camera_lights(car_state) -> int | None:
  """Camera light view (0/1/2) from carState, or None (unsupported, stale, or invalid CAN)."""
  light = getattr(car_state, "ambientLight", None)
  if not getattr(light, "cameraValid", False) or not _can_ok(car_state):
    return None
  value = getattr(light, "cameraLights", None)
  return value if value in (CAMERA_NONE, CAMERA_CITY_LIGHTS, CAMERA_HIGH_BEAM) else None
