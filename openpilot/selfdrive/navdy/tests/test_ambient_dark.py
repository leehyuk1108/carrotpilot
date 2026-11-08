from datetime import datetime, timezone
from types import SimpleNamespace as NS

import pytest

from openpilot.selfdrive.navdy import ambient_dark as mod
from openpilot.selfdrive.navdy.ambient_dark import (
  DARK_HOLD_S, LIT_HOLD_S, RESTORE_DELIVERIES, RESTORE_S, ROTATE_S, DarkAmbient, off_override, read_enabled,
)

WALL = 1_794_180_000.0  # 2026-11-09 04:40 KST
AUTO = {'mode': 'auto'}


def driver(enabled=True):
  state = {'enabled': enabled}
  ambient = DarkAmbient(read=lambda: state['enabled'])
  return ambient, state


def step(ambient, now, *, dark=True, onroad=True, door_open=False, parked=False, user_override=None, wall=None,
         delivered=True):
  result = ambient.update(now=now, wall=WALL + now if wall is None else wall, dark=dark, onroad=onroad,
                          door_open=door_open, parked=parked, user_override=user_override)
  if delivered:
    # What the bridge would send: our command, else the user's override.
    ambient.delivered(result if result is not None else user_override)
  return result


def expires_in(command, wall):
  expires = datetime.strptime(command['expiresAt'], '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
  return expires.timestamp() - wall


def is_off(command):
  return (command is not None and command['mode'] == 'manual'
          and command['zone1']['enabled'] is False and command['zone2']['enabled'] is False)


def run(ambient, start, duration, **kwargs):
  """Distinct results of 5 Hz steps over [start, start + duration]."""
  seen = []
  for i in range(int(round(duration / 0.2)) + 1):
    result = step(ambient, start + i * 0.2, **kwargs)
    if result not in seen:
      seen.append(result)
  return seen


def settle(ambient, now=0.0, **kwargs):
  """Run past the startup restore window and the dark hold."""
  t = now
  while t <= now + DARK_HOLD_S + RESTORE_S:
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


def test_turns_off_only_after_dark_hold():
  ambient, _ = driver()
  assert step(ambient, 0.0) == AUTO  # startup clears anything a previous bridge left
  t = 0.2
  while t < DARK_HOLD_S - 0.01:
    assert not is_off(step(ambient, t))
    t += 0.2
  assert is_off(step(ambient, DARK_HOLD_S))


def test_disabled_never_touches_the_payload():
  ambient, _ = driver(enabled=False)
  for i in range(100):
    assert step(ambient, i * 0.2) is None


def test_bright_needs_lit_hold_then_restores_with_auto():
  ambient, _ = driver()
  result, t = settle(ambient)
  assert is_off(result)
  start = t
  while t < start + LIT_HOLD_S - 0.01:
    assert is_off(step(ambient, t, dark=False))
    t += 0.2
  assert run(ambient, start + LIT_HOLD_S, RESTORE_S - 0.1, dark=False) == [AUTO]
  assert step(ambient, start + LIT_HOLD_S + RESTORE_S + 0.1, dark=False) is None


def test_short_bright_blip_keeps_it_off():
  ambient, _ = driver()
  result, t = settle(ambient)
  assert is_off(step(ambient, t, dark=False))
  assert is_off(step(ambient, t + 2.0, dark=None))
  assert is_off(step(ambient, t + 4.0, dark=True))
  assert is_off(step(ambient, t + 4.0 + LIT_HOLD_S, dark=True))


def test_lost_signal_restores_after_lit_hold():
  ambient, _ = driver()
  result, t = settle(ambient)
  assert is_off(step(ambient, t, dark=None))
  assert step(ambient, t + LIT_HOLD_S, dark=None) == AUTO


@pytest.mark.parametrize("change", [
  {'door_open': True}, {'onroad': False}, {'onroad': None}, {'parked': True},
  {'user_override': {'mode': 'manual', 'id': 'phone-1'}}, {'user_override': {'mode': 'auto'}},
])
def test_door_offroad_and_user_override_restore_immediately(change):
  ambient, _ = driver()
  result, t = settle(ambient)
  assert is_off(result)
  result = step(ambient, t, **change)
  if 'user_override' in change:
    assert result is None  # the user's own command is sent instead of ours
  else:
    assert result == AUTO


def test_door_close_after_long_dark_turns_off_again_without_new_hold():
  ambient, _ = driver()
  result, t = settle(ambient)
  assert step(ambient, t, door_open=True) == AUTO
  assert is_off(step(ambient, t + 1.0, door_open=False))


def test_profile_override_does_not_block_dark_off():
  ambient, _ = driver()
  profile = {'mode': 'profile', 'profile': {'driving': {}}}
  result, _ = settle(ambient, user_override=profile)
  assert is_off(result)


def test_disabling_restores_within_param_refresh():
  ambient, state = driver()
  result, t = settle(ambient)
  state['enabled'] = False
  results = [step(ambient, t + i * 0.2) for i in range(int(mod.PARAM_REFRESH_S / 0.2) + 2)]
  assert AUTO in results
  assert not is_off(results[-1])


def test_startup_restore_only_when_enabled():
  ambient, _ = driver(enabled=False)
  assert step(ambient, 0.0, dark=False) is None
  ambient, _ = driver(enabled=True)
  assert run(ambient, 0.0, RESTORE_S - 0.1, dark=False) == [AUTO]
  assert step(ambient, RESTORE_S + 0.1, dark=False) is None


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


def test_parked_releases_before_ignition_off_so_exit_lighting_runs():
  ambient, _ = driver()
  result, t = settle(ambient)
  assert run(ambient, t, RESTORE_S - 0.1, parked=True) == [AUTO]
  t2 = t + RESTORE_S + 0.1
  assert step(ambient, t2, parked=True) is None  # delivered and held long enough; HUD back to normal
  assert step(ambient, t2 + 0.2, parked=True, onroad=False) is None
  # Never re-arms while parked, however long it stays dark.
  assert step(ambient, t2 + 60, parked=True) is None


def test_restore_waits_for_delivery_when_the_link_is_down():
  ambient, _ = driver()
  result, t = settle(ambient)
  assert step(ambient, t, door_open=True, delivered=False) == AUTO
  later = t + RESTORE_S + 30
  assert step(ambient, later, door_open=True, delivered=False) == AUTO  # link was down the whole time
  for i in range(RESTORE_DELIVERIES - 1):
    assert step(ambient, later + 0.2 * (i + 1), door_open=True) == AUTO
  assert step(ambient, later + 0.2 * RESTORE_DELIVERIES, door_open=True) == AUTO
  assert step(ambient, later + 0.2 * (RESTORE_DELIVERIES + 1), door_open=True) is None


def test_only_delivered_auto_counts_toward_restore():
  ambient, _ = driver()
  result, t = settle(ambient)
  assert step(ambient, t, door_open=True, delivered=False) == AUTO
  for _ in range(5):
    ambient.delivered({'mode': 'profile'})
    ambient.delivered(None)
  assert step(ambient, t + RESTORE_S + 1, door_open=True, delivered=False) == AUTO


def test_changed_profile_passes_once_then_off_resumes():
  ambient, _ = driver()
  first = {'mode': 'profile', 'profile': {'driving': {'zone1': {'brightness': 30}}}}
  result, t = settle(ambient, user_override=first)
  assert is_off(result)
  assert is_off(step(ambient, t, user_override=first))
  changed = {'mode': 'profile', 'profile': {'driving': {'zone1': {'brightness': 60}}}}
  assert step(ambient, t + 0.2, user_override=changed) is None  # the new profile reaches the HUD
  assert is_off(step(ambient, t + 0.4, user_override=changed))
  assert is_off(step(ambient, t + 0.6, user_override=changed))


def test_profile_set_for_the_first_time_while_dark_passes_once():
  ambient, _ = driver()
  result, t = settle(ambient)
  profile = {'mode': 'profile', 'profile': {}}
  assert step(ambient, t, user_override=profile) is None
  assert is_off(step(ambient, t + 0.2, user_override=profile))
