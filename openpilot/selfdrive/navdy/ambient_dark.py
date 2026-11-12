"""Opt-in: switch the Navdy ambient light off on dark roads without city lights.

The HUD already obeys a manual ambient override carried in the payload (the Wayon
override path). While NavdyAmbientDarkOff is on, the car is onroad and not in
Park, the doors are closed, the car's light sensor has read dark for DARK_HOLD_S
and the high-beam camera has seen no street or city lights for NO_LIGHTS_HOLD_S,
the bridge sends a manual override with both zones disabled. The camera reads
"nothing" all day (inside daytime tunnels too) and sometimes at night before the
car first moves, so its "nothing" only counts once it has reported lights or a
high-beam advice since it got dark. City lights (or no camera reading) for
LIGHTS_HOLD_S, or brightness for LIT_HOLD_S, end it. When that ends it keeps
releasing the override for RELEASE_S, the HUD's longest possible hold, so a
release lost on a dropped link is repeated after reconnect or the HUD expires the
override itself. The release is mode 'auto', or the user's Wayon
profile when one is set, since the HUD drops a manual override on either.
Releasing in Park, before the ignition goes off, lets the HUD's own
onroad-to-offroad transition (exit courtesy lighting) run unmodified. If the link
is down from Park until the ignition is off, that transition can still be missed.

Tunnels are an exception. The camera reads most lit tunnels as city lights but not all
(11/12 20:50: a lit tunnel read "nothing" for 25 s and the light went off). While the front
radar reports a tunnel (bus1 0x460 byte3 bit3), "nothing" never counts, and a light that was
already off comes back on after TUNNEL_ON_HOLD_S in the tunnel. A missing or stale radar
reading only means "not known to be a tunnel", which keeps the camera rules above.

HUD behavior this relies on (AmbientLightController.setManualOverride):
- A repeat of the active override id is ignored, so the id rotates every ROTATE_S
  and each new id re-arms the HUD expiry.
- expiresAt must be ISO-8601 UTC and in the future by the HUD clock (capped there at
  20 minutes). An override the HUD considers stale is dropped, leaving lights on.
- 'auto' only clears a manual override. A user override sent afterwards is re-applied.
- A profile command also clears the manual override (and saves the profile), so a
  changed Wayon profile is let through for one payload and the off override is
  re-applied on the next. If that one payload is lost, the profile still reaches
  the HUD during the release.
- The HUD's overspeed warning blink does not check the manual override and can still
  light zone 1 while the override is active.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

PARAM = 'NavdyAmbientDarkOff'
DARK_HOLD_S = 5.0       # dark this long before switching off (tunnel entries, flicker)
LIT_HOLD_S = 10.0       # bright or unknown this long before switching back on
NO_LIGHTS_HOLD_S = 10.0  # camera sees no street/city lights this long before switching off
LIGHTS_HOLD_S = 15.0    # camera sees city lights (or nothing usable) this long before switching on
CAMERA_RESET_S = 60.0   # bright this long: forget that the camera was active (daytime)
TUNNEL_ON_HOLD_S = 2.0  # in a radar-reported tunnel this long: an off light comes back on
RELEASE_S = 20 * 60     # HUD caps a manual override at 20 minutes from receipt
ROTATE_S = 300          # new override id every 5 minutes
LEASE_ROTATIONS = 3     # expiresAt lands 10-15 minutes ahead
PARAM_REFRESH_S = 2.0
USER_OVERRIDE_MODES = ('manual', 'auto')
CAMERA_NONE, CAMERA_CITY_LIGHTS, CAMERA_HIGH_BEAM = 0, 1, 2


def read_enabled(params=None) -> bool:
  if params is None:
    from openpilot.common.params import Params
    params = Params()
  try:
    # A running native Params table can predate a source-only update; read the file.
    with Path(params.get_param_path(PARAM)).open('rb') as stream:
      raw = stream.read(17)
  except (OSError, ValueError, TypeError):
    return False
  return raw.strip() == b'1'


def off_override(wall: float) -> dict:
  rotation = int(wall // ROTATE_S)
  expires = datetime.fromtimestamp((rotation + LEASE_ROTATIONS) * ROTATE_S, timezone.utc)
  zone = {'enabled': False, 'brightness': 0}
  return {'mode': 'manual', 'id': f'carrot-dark-{rotation}', 'expiresAt': expires.strftime('%Y-%m-%dT%H:%M:%SZ'),
          'zone1': dict(zone), 'zone2': dict(zone)}


class DarkAmbient:
  def __init__(self, read=read_enabled):
    self._read = read
    self.enabled = False
    self._checked_at: float | None = None
    self._started = False
    self.dark_since: float | None = None   # start of the current dark run
    self.lit_since: float | None = None    # start of the current bright/unknown run
    self.bright_since: float | None = None  # start of the current bright (not unknown) run
    self.camera_seen = False               # camera reported lights or high beam in this darkness
    self.no_lights_since: float | None = None  # start of the current camera "no lights" run
    self.lights_since: float | None = None     # start of the current city lights/no camera run
    self.tunnel_since: float | None = None     # start of the current radar-reported tunnel run
    self.active = False
    self.release_until: float | None = None
    self.forwarded_profile: str | None = None  # last Wayon profile the payload carried

  def _refresh(self, now: float) -> None:
    if self._checked_at is not None and now - self._checked_at < PARAM_REFRESH_S:
      return
    self._checked_at = now
    try:
      self.enabled = bool(self._read())
    except Exception:
      self.enabled = False

  def observe(self, *, now: float, dark: bool | None, onroad: bool | None, camera: int | None,
              tunnel: bool | None = None) -> None:
    """Track the car on ticks without a payload (Navdy link down).

    Keeps the hold timers and the camera record current and applies every reason to
    switch back on, so a stop or city lights while the link was down are not missed.
    It never switches off by itself; update() decides that with the full inputs.
    """
    self._track(now, dark, onroad, camera, tunnel)

  def _track(self, now, dark, onroad, camera, tunnel=None) -> bool:
    """Shared per-tick tracking; returns whether the camera currently means "no lights".

    Idempotent per tick: run() observes every loop and update() repeats it when a payload goes out.
    """
    if dark is True:
      self.lit_since = None
      self.dark_since = now if self.dark_since is None else self.dark_since
    else:
      self.dark_since = None
      self.lit_since = now if self.lit_since is None else self.lit_since
    if dark is False:
      self.bright_since = now if self.bright_since is None else self.bright_since
    else:
      self.bright_since = None
    if onroad is not True or (self.bright_since is not None and now - self.bright_since >= CAMERA_RESET_S):
      self.camera_seen = False  # a new drive or daylight: the camera has to report again
    elif dark is True and camera in (CAMERA_CITY_LIGHTS, CAMERA_HIGH_BEAM):
      self.camera_seen = True
    in_tunnel = onroad is True and tunnel is True
    self.tunnel_since = (now if self.tunnel_since is None else self.tunnel_since) if in_tunnel else None
    no_lights = self.camera_seen and camera in (CAMERA_NONE, CAMERA_HIGH_BEAM) and not in_tunnel
    if no_lights:
      self.lights_since = None
      self.no_lights_since = now if self.no_lights_since is None else self.no_lights_since
    else:
      self.no_lights_since = None
      self.lights_since = now if self.lights_since is None else self.lights_since
    if self.active and (onroad is not True or (dark is not True and now - self.lit_since >= LIT_HOLD_S)
                        or (not no_lights and now - self.lights_since >= LIGHTS_HOLD_S)
                        or (in_tunnel and now - self.tunnel_since >= TUNNEL_ON_HOLD_S)):
      self.active = False
    return no_lights

  def update(self, *, now: float, wall: float, dark: bool | None, onroad: bool | None, door_open: bool,
             parked: bool = False, user_override=None, camera: int | None = None,
             tunnel: bool | None = None) -> dict | None:
    """Override to send instead of the user's, or None to leave the payload alone."""
    result = self._decide(now, wall, dark, onroad, door_open, parked, user_override, camera, tunnel)
    if result is None:
      self.forwarded_profile = profile_key(user_override)
    return result

  def _decide(self, now, wall, dark, onroad, door_open, parked, user_override, camera, tunnel=None):
    self._refresh(now)
    if not self._started:
      self._started = True
      if self.enabled:
        # A previous bridge may have left the HUD dark; clear it unless conditions hold.
        self.release_until = now + RELEASE_S
    no_lights = self._track(now, dark, onroad, camera, tunnel)
    user_blocks = isinstance(user_override, dict) and user_override.get('mode') in USER_OVERRIDE_MODES
    gated = self.enabled and onroad is True and not door_open and not parked and not user_blocks
    if self.active:
      if not gated:
        self.active = False
    elif (gated and dark is True and now - self.dark_since >= DARK_HOLD_S
          and no_lights and now - self.no_lights_since >= NO_LIGHTS_HOLD_S):
      self.active = True
    if self.active:
      # The HUD can hold the last off command for up to RELEASE_S after it arrives.
      self.release_until = now + RELEASE_S
      profile = profile_key(user_override)
      if profile is not None and profile != self.forwarded_profile:
        return None
      return off_override(wall)
    if self.release_until is not None:
      if now >= self.release_until:
        self.release_until = None
      elif not user_blocks and profile_key(user_override) is None:
        return {'mode': 'auto'}
      # else: the user's own command or profile goes out unchanged; the HUD drops our
      # override on either. The window is kept in case that payload never arrives.
    return None

  def state(self) -> dict:
    return {'enabled': self.enabled, 'active': self.active, 'releasing': self.release_until is not None,
            'cameraSeen': self.camera_seen, 'tunnel': self.tunnel_since is not None}


def profile_key(override) -> str | None:
  if not isinstance(override, dict) or override.get('mode') != 'profile':
    return None
  return json.dumps(override, sort_keys=True, allow_nan=False)
