from datetime import datetime, timezone
from types import SimpleNamespace as NS

import pytest

from openpilot.selfdrive.navdy import ambient_dark as mod
from openpilot.selfdrive.navdy.ambient_dark import (
  CAMERA_RESET_S, DARK_HOLD_S, LIGHTS_HOLD_S, LIT_HOLD_S, NO_LIGHTS_HOLD_S, RELEASE_S, ROTATE_S, DarkAmbient,
  off_override, read_enabled,
)

WALL = 1_794_180_000.0
AUTO = {'mode': 'auto'}
PROFILE = {'mode': 'profile', 'profile': {'driving': {'zone1': {'brightness': 30}}}}
NONE, CITY, HIGH_BEAM = 0, 1, 2
OFF_HOLD_S = max(DARK_HOLD_S, NO_LIGHTS_HOLD_S)


def driver(enabled=True):
  state = {'enabled': enabled}
  ambient = DarkAmbient(read=lambda: state['enabled'])
  return ambient, state


def step(ambient, now, *, dark=True, onroad=True, door_open=False, parked=False, user_override=None, wall=None,
         camera=HIGH_BEAM):
  return ambient.update(now=now, wall=WALL + now if wall is None else wall, dark=dark, onroad=onroad,
                        door_open=door_open, parked=parked, user_override=user_override, camera=camera)


def run(ambient, start, duration, **kwargs):
  """Distinct results of 5 Hz steps over [start, start + duration]."""
  seen = []
  for i in range(int(round(duration / 0.2)) + 1):
    result = step(ambient, start + i * 0.2, **kwargs)
    if result not in seen:
      seen.append(result)
  return seen


def expires_in(command, wall):
  expires = datetime.strptime(command['expiresAt'], '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
  return expires.timestamp() - wall


def is_off(command):
  return (command is not None and command['mode'] == 'manual'
          and command['zone1']['enabled'] is False and command['zone2']['enabled'] is False)


def settle(ambient, now=0.0, **kwargs):
  """Run past the dark and no-lights holds at 5 Hz; returns the last result and the next time."""
  t = now
  while t <= now + OFF_HOLD_S + 1.0:
    result = step(ambient, t, **kwargs)
    t += 0.2
  return result, t


def test_off_override_matches_hud_contract():
  command = off_override(WALL)
  assert command['mode'] == 'manual'
  assert command['id'] == f'carrot-dark-{int(WALL // ROTATE_S)}'
  assert command['zone1'] == {'enabled': False, 'brightness': 0} == command['zone2']
  # HUD parses yyyy-MM-dd'T'HH:mm:ss'Z' and caps at 20 minutes; keep 10-15 minutes of lease.
  assert 600 < expires_in(command, WALL) <= 900
  assert command['zone1'] is not command['zone2']


def test_id_rotates_and_rearms_expiry_before_it_lapses():
  first = off_override(WALL)
  later = off_override(WALL + ROTATE_S)
  assert first['id'] != later['id']
  assert expires_in(later, WALL + ROTATE_S) > 600
  # Same id inside one rotation: the HUD ignores repeats, the old lease still has >5 min.
  same = off_override(WALL + ROTATE_S - 1 - WALL % ROTATE_S)
  assert same['id'] == first['id']


def test_turns_off_only_after_the_no_lights_hold():
  ambient, _ = driver()
  assert step(ambient, 0.0) == AUTO  # startup releases anything a previous bridge left
  t = 0.2
  while t < NO_LIGHTS_HOLD_S - 0.01:
    assert step(ambient, t) == AUTO
    t += 0.2
  assert is_off(step(ambient, NO_LIGHTS_HOLD_S))


def test_turns_off_only_after_the_dark_hold():
  ambient, _ = driver()
  assert run(ambient, 0.0, 19.8, dark=False) == [AUTO]  # camera already says no lights
  assert run(ambient, 20.0, DARK_HOLD_S - 0.2) == [AUTO]
  assert is_off(step(ambient, 20.0 + DARK_HOLD_S))


def test_daytime_tunnel_camera_never_active_stays_on():
  # Daytime tunnels read dark, but the camera reports "nothing" the whole time.
  ambient, _ = driver()
  assert run(ambient, 0.0, 120, camera=NONE) == [AUTO]
  assert not ambient.state()['cameraSeen']


@pytest.mark.parametrize("camera", [CITY, None])
def test_city_lights_or_no_camera_keep_it_on(camera):
  ambient, _ = driver()
  assert run(ambient, 0.0, 120, camera=camera) == [AUTO]


def test_camera_nothing_counts_once_lights_were_seen():
  ambient, _ = driver()
  assert run(ambient, 0.0, 19.8, camera=CITY) == [AUTO]
  assert ambient.state()['cameraSeen']
  assert run(ambient, 20.0, NO_LIGHTS_HOLD_S - 0.2, camera=NONE) == [AUTO]
  assert is_off(step(ambient, 20.0 + NO_LIGHTS_HOLD_S, camera=NONE))


def test_city_lights_switch_it_back_on_after_the_lights_hold():
  ambient, _ = driver()
  result, t = settle(ambient)
  assert is_off(result)
  start = t
  while t < start + LIGHTS_HOLD_S - 0.01:
    assert is_off(step(ambient, t, camera=CITY))
    t += 0.2
  assert step(ambient, start + LIGHTS_HOLD_S + 0.01, camera=CITY) == AUTO


def test_short_lit_stretch_keeps_it_off():
  ambient, _ = driver()
  result, t = settle(ambient)
  assert all(is_off(r) for r in run(ambient, t, LIGHTS_HOLD_S - 2, camera=CITY))
  assert is_off(step(ambient, t + LIGHTS_HOLD_S - 1, camera=NONE))
  assert is_off(step(ambient, t + LIGHTS_HOLD_S + 5, camera=NONE))


def test_lost_camera_releases_after_the_lights_hold():
  ambient, _ = driver()
  result, t = settle(ambient)
  assert is_off(step(ambient, t, camera=None))
  assert is_off(step(ambient, t + LIGHTS_HOLD_S - 1, camera=None))
  assert step(ambient, t + LIGHTS_HOLD_S + 0.01, camera=None) == AUTO


def test_daylight_forgets_the_camera_but_a_short_bright_patch_does_not():
  ambient, _ = driver()
  result, t = settle(ambient, camera=CITY)
  assert ambient.state()['cameraSeen'] and result == AUTO
  # A bright patch shorter than CAMERA_RESET_S (a lit station): "nothing" still counts afterwards.
  run(ambient, t, CAMERA_RESET_S - 10, dark=False, camera=NONE)
  t += CAMERA_RESET_S - 9.8
  result, t = settle(ambient, t, camera=NONE)
  assert is_off(result)
  # Daylight: the camera idles at "nothing", so a later tunnel stays lit.
  run(ambient, t, CAMERA_RESET_S + 1, dark=False, camera=NONE)
  assert not ambient.state()['cameraSeen']
  t += CAMERA_RESET_S + 1.2
  assert not any(is_off(r) for r in run(ambient, t, 60, camera=NONE))


def test_new_drive_forgets_the_camera():
  ambient, _ = driver()
  settle(ambient, camera=CITY)
  assert ambient.state()['cameraSeen']
  step(ambient, 20.0, onroad=False, camera=None)
  assert not ambient.state()['cameraSeen']
  assert not any(is_off(r) for r in run(ambient, 20.2, 60, camera=NONE))


def test_disabled_never_touches_the_payload():
  ambient, _ = driver(enabled=False)
  for i in range(100):
    assert step(ambient, i * 0.2) is None


def test_bright_needs_lit_hold_then_releases_for_the_hud_hold_time():
  ambient, _ = driver()
  result, t = settle(ambient)
  assert is_off(result)
  start = t
  while t < start + LIT_HOLD_S - 0.01:
    assert is_off(step(ambient, t, dark=False))
    t += 0.2
  assert run(ambient, start + LIT_HOLD_S, 60, dark=False) == [AUTO]
  last_off = start + LIT_HOLD_S - 0.2
  assert step(ambient, last_off + RELEASE_S - 1, dark=False) == AUTO
  assert step(ambient, last_off + RELEASE_S + 1, dark=False) is None


def test_short_bright_blip_keeps_it_off():
  ambient, _ = driver()
  result, t = settle(ambient)
  assert is_off(step(ambient, t, dark=False))
  assert is_off(step(ambient, t + 2.0, dark=None))
  assert is_off(step(ambient, t + 4.0, dark=True))
  assert is_off(step(ambient, t + 4.0 + LIT_HOLD_S, dark=True))


def test_lost_signal_releases_after_lit_hold():
  ambient, _ = driver()
  result, t = settle(ambient)
  assert is_off(step(ambient, t, dark=None))
  assert step(ambient, t + LIT_HOLD_S, dark=None) == AUTO


@pytest.mark.parametrize("change", [{'door_open': True}, {'onroad': False}, {'onroad': None}, {'parked': True}])
def test_door_offroad_and_park_release_immediately(change):
  ambient, _ = driver()
  result, t = settle(ambient)
  assert is_off(result)
  assert step(ambient, t, **change) == AUTO


@pytest.mark.parametrize("command", [{'mode': 'manual', 'id': 'phone-1'}, {'mode': 'auto'}])
def test_user_manual_or_auto_goes_out_instead_but_keeps_the_release_window(command):
  ambient, _ = driver()
  result, t = settle(ambient)
  assert step(ambient, t, user_override=command) is None  # the user's command is sent instead
  # If that command never reached the HUD and has since expired, keep releasing ours.
  assert step(ambient, t + 90, parked=True) == AUTO
  ambient, _ = driver()
  result, t = settle(ambient)
  assert step(ambient, t, door_open=True) == AUTO
  assert step(ambient, t + 1, door_open=True, user_override=command) is None
  assert step(ambient, t + 2, door_open=True) == AUTO
  assert step(ambient, t + RELEASE_S + 1, door_open=True) is None


def test_link_drop_during_release_is_covered_by_repeating_it():
  ambient, _ = driver()
  result, t = settle(ambient)
  assert step(ambient, t, door_open=True) == AUTO
  # However long the link was down (up to the HUD's own hold), the next payload still releases.
  assert step(ambient, t + 600, door_open=True) == AUTO
  assert step(ambient, t + RELEASE_S - 1, door_open=True) == AUTO


def test_park_releases_before_ignition_off_and_never_rearms_while_parked():
  ambient, _ = driver()
  result, t = settle(ambient)
  assert step(ambient, t, parked=True) == AUTO
  assert step(ambient, t + 0.2, parked=True, onroad=False) == AUTO
  assert step(ambient, t + 60, parked=True) == AUTO
  assert step(ambient, t + RELEASE_S + 1, parked=True) is None


def test_door_close_after_long_dark_turns_off_again_without_new_hold():
  ambient, _ = driver()
  result, t = settle(ambient)
  assert step(ambient, t, door_open=True) == AUTO
  assert is_off(step(ambient, t + 1.0, door_open=False))


def test_profile_does_not_block_and_is_the_release():
  ambient, _ = driver()
  result, t = settle(ambient, user_override=PROFILE)
  assert is_off(result)
  # Releasing with a profile set: the profile itself goes out (the HUD drops manual on it).
  assert step(ambient, t, door_open=True, user_override=PROFILE) is None
  assert step(ambient, t + 0.2, door_open=True, user_override=PROFILE) is None


def test_changed_profile_passes_once_then_off_resumes():
  ambient, _ = driver()
  result, t = settle(ambient, user_override=PROFILE)
  assert is_off(result)
  changed = {'mode': 'profile', 'profile': {'driving': {'zone1': {'brightness': 60}}}}
  assert step(ambient, t, user_override=changed) is None  # the new profile reaches the HUD
  assert is_off(step(ambient, t + 0.2, user_override=changed))
  assert is_off(step(ambient, t + 0.4, user_override=changed))


def test_profile_set_for_the_first_time_while_dark_passes_once():
  ambient, _ = driver()
  result, t = settle(ambient)
  assert step(ambient, t, user_override=PROFILE) is None
  assert is_off(step(ambient, t + 0.2, user_override=PROFILE))


def test_disabling_releases_within_param_refresh():
  ambient, state = driver()
  result, t = settle(ambient)
  state['enabled'] = False
  results = [step(ambient, t + i * 0.2) for i in range(int(mod.PARAM_REFRESH_S / 0.2) + 2)]
  assert results[-1] == AUTO
  assert step(ambient, t + RELEASE_S + 5) is None


def test_startup_release_only_when_enabled():
  ambient, _ = driver(enabled=False)
  assert step(ambient, 0.0, dark=False) is None
  ambient, _ = driver(enabled=True)
  assert run(ambient, 0.0, 30, dark=False) == [AUTO]
  assert step(ambient, RELEASE_S + 1, dark=False) is None


def test_read_failure_counts_as_disabled():
  def broken():
    raise RuntimeError('params unavailable')
  ambient = DarkAmbient(read=broken)
  for i in range(50):
    assert step(ambient, i * 0.2) is None


@pytest.mark.parametrize("raw,expected", [(b'1', True), (b'1\n', True), (b'0', False), (b'', False), (b'2', False)])
def test_read_enabled_reads_param_file(tmp_path, raw, expected):
  path = tmp_path / mod.PARAM
  path.write_bytes(raw)
  assert read_enabled(NS(get_param_path=lambda key: str(tmp_path / key))) is expected


def test_read_enabled_missing_file_is_disabled(tmp_path):
  assert read_enabled(NS(get_param_path=lambda key: str(tmp_path / key))) is False
