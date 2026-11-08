"""Opt-in: switch the Navdy ambient light off while the car reports darkness.

The HUD already obeys a manual ambient override carried in the payload (the Wayon
override path). While NavdyAmbientDarkOff is on, the car is onroad, the doors are
closed and the car's light sensor has read dark for DARK_HOLD_S, the bridge sends
a manual override with both zones disabled. When that ends it sends mode 'auto'
for RESTORE_S so the HUD drops the override and returns to its normal lighting.

HUD behavior this relies on (AmbientLightController.setManualOverride):
- A repeat of the active override id is ignored, so the id rotates every ROTATE_S
  and each new id re-arms the HUD expiry.
- expiresAt must be ISO-8601 UTC and in the future by the HUD clock (capped there at
  20 minutes). An override the HUD considers stale is dropped, leaving lights on.
- 'auto' only clears a manual override. A user override sent afterwards is re-applied.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

PARAM = 'NavdyAmbientDarkOff'
DARK_HOLD_S = 5.0       # dark this long before switching off (tunnel entries, flicker)
LIT_HOLD_S = 10.0       # bright or unknown this long before switching back on
RESTORE_S = 3.0         # keep sending 'auto' this long after switching back on
ROTATE_S = 300          # new override id every 5 minutes
LEASE_ROTATIONS = 3     # expiresAt lands 10-15 minutes ahead
PARAM_REFRESH_S = 2.0
USER_OVERRIDE_MODES = ('manual', 'auto')


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
    self.active = False
    self.restore_until: float | None = None

  def _refresh(self, now: float) -> None:
    if self._checked_at is not None and now - self._checked_at < PARAM_REFRESH_S:
      return
    self._checked_at = now
    try:
      self.enabled = bool(self._read())
    except Exception:
      self.enabled = False

  def update(self, *, now: float, wall: float, dark: bool | None, onroad: bool | None, door_open: bool,
             user_override=None) -> dict | None:
    """Override to send instead of the user's, or None to leave the payload alone."""
    self._refresh(now)
    if not self._started:
      self._started = True
      if self.enabled:
        # A previous bridge may have left the HUD dark; clear it unless conditions hold.
        self.restore_until = now + RESTORE_S
    if dark is True:
      self.lit_since = None
      self.dark_since = now if self.dark_since is None else self.dark_since
    else:
      self.dark_since = None
      self.lit_since = now if self.lit_since is None else self.lit_since
    user_blocks = isinstance(user_override, dict) and user_override.get('mode') in USER_OVERRIDE_MODES
    gated = self.enabled and onroad is True and not door_open and not user_blocks
    if self.active:
      if not gated or (dark is not True and now - self.lit_since >= LIT_HOLD_S):
        self.active = False
        self.restore_until = now + RESTORE_S
    elif gated and dark is True and now - self.dark_since >= DARK_HOLD_S:
      self.active = True
      self.restore_until = None
    if self.active:
      return off_override(wall)
    if self.restore_until is not None:
      if now < self.restore_until and not user_blocks:
        return {'mode': 'auto'}
      self.restore_until = None
    return None

  def state(self) -> dict:
    return {'enabled': self.enabled, 'active': self.active, 'restoring': self.restore_until is not None}
