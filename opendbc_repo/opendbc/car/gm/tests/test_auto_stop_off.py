import pytest

from opendbc.can.packer import CANPacker
from opendbc.car import Bus, structs
from opendbc.car.gm import gmcan
from opendbc.car.gm.auto_stop_off import AutoStopOff, CONFIRM, DONE, GAVE_UP, IDLE, PRESSING, DT_CTRL
from opendbc.car.gm.values import CAR, CruiseButtons

READY = int(AutoStopOff.START_DELAY_S / DT_CTRL)
RETRY = int(AutoStopOff.RETRY_DELAY_S / DT_CTRL)


class Sim:
  """Feeds AutoStopOff at 100 Hz with a real 0x1E1 frame every 3 control frames (33 Hz)."""

  def __init__(self, **kw):
    self.a = AutoStopOff()
    self.frame = 0
    self.counter = 0
    self.state = dict(enabled=True, latch_fresh=True, latch=0, standstill=True, cruise_engaged=False,
                      op_enabled=False, can_ok=True, buttons_idle=True, other_btn_recent=False)
    self.state.update(kw)
    self.presses = []

  def step(self, n=1, **kw):
    self.state.update(kw)
    for _ in range(n):
      if self.frame % 3 == 0:
        self.counter = (self.counter + 1) % 4
      if self.a.update(counter=self.counter, **self.state):
        self.presses.append(self.frame)
      self.frame += 1
    return self

  def until_pressing(self):
    for _ in range(READY + 100):
      self.step()
      if self.presses:
        return self
    raise AssertionError('never started pressing')


def test_disabled_never_presses():
  s = Sim(enabled=False).step(READY + 2000)
  assert s.presses == [] and s.a.state == IDLE


def test_latch_already_off_never_presses():
  s = Sim(latch=1).step(READY + 2000)
  assert s.presses == [] and s.a.state == DONE


def test_waits_for_fresh_latch_and_start_delay():
  s = Sim(latch_fresh=False).step(2000)
  assert s.presses == []
  s.step(READY - 5, latch_fresh=True)
  assert s.presses == []
  s.step(10)
  assert s.a.state == PRESSING and len(s.presses) >= 1


@pytest.mark.parametrize("kw", [dict(standstill=False), dict(cruise_engaged=True), dict(op_enabled=True),
                                dict(can_ok=False), dict(buttons_idle=False), dict(other_btn_recent=True)])
def test_preconditions_block_start(kw):
  s = Sim(**kw).step(READY + 500)
  assert s.presses == [] and s.a.state == IDLE and s.a.attempt == 0


def test_presses_once_per_real_frame_then_confirms():
  s = Sim().step(READY + 30)
  assert s.a.state == CONFIRM
  assert len(s.presses) == AutoStopOff.PRESS_FRAMES[0]
  assert {b - a for a, b in zip(s.presses, s.presses[1:])} == {3}  # one press frame per real 33 Hz frame
  s.step(20, latch=1)
  assert s.a.state == DONE
  s.step(2000, latch=0)  # driver turns auto stop back on: leave it alone
  assert len(s.presses) == AutoStopOff.PRESS_FRAMES[0] and s.a.state == DONE


def test_latch_one_mid_press_stops_immediately():
  s = Sim().until_pressing()
  s.step(1, latch=1)
  assert s.a.state == DONE
  sent = len(s.presses)
  s.step(500)
  assert len(s.presses) == sent == 1


def test_gives_up_after_all_attempts():
  s = Sim().step(READY + 3000)
  assert s.a.state == GAVE_UP
  assert len(s.presses) == sum(AutoStopOff.PRESS_FRAMES)
  s.step(3000)
  assert len(s.presses) == sum(AutoStopOff.PRESS_FRAMES)


@pytest.mark.parametrize("kw", [dict(op_enabled=True), dict(cruise_engaged=True), dict(standstill=False),
                                dict(can_ok=False), dict(latch_fresh=False), dict(buttons_idle=False),
                                dict(other_btn_recent=True), dict(enabled=False)])
def test_any_failing_condition_mid_press_aborts_and_uses_the_attempt(kw):
  s = Sim().until_pressing()
  s.step(1, **kw)
  assert s.a.state == IDLE and s.a.attempt == 1
  sent = len(s.presses)
  s.step(RETRY + 300)  # conditions still failing: nothing more is sent
  assert len(s.presses) == sent


def test_toggling_cannot_exceed_the_attempt_budget():
  s = Sim()
  for _ in range(20):
    s.step(READY + 10, enabled=True)
    s.step(1, enabled=False)
  s.step(READY + 5000, enabled=True)
  assert s.a.attempt == len(AutoStopOff.PRESS_FRAMES) and s.a.state == GAVE_UP
  assert len(s.presses) <= sum(AutoStopOff.PRESS_FRAMES)


def test_disable_while_confirming_uses_the_attempt():
  s = Sim().step(READY + 30)
  assert s.a.state == CONFIRM
  s.step(1, enabled=False)
  assert s.a.state == IDLE and s.a.attempt == 1


@pytest.fixture(scope="module")
def packer():
  return CANPacker('gm_global_a_powertrain_volt')


@pytest.mark.parametrize("idx,expected", [
  (0, "000000012010ff"), (1, "000000012115ee"), (2, "00000001221add"), (3, "00000001231fcc"),
])
def test_press_frame_matches_real_switch_press(packer, idx, expected):
  # real frames from 000001e6 13:12:43.9 (driver held the switch for 9 frames)
  addr, dat, bus = gmcan.create_buttons(packer, 0, idx, CruiseButtons.UNPRESS, park_brake=0, auto_stop_off=1)
  assert (addr, bus) == (0x1E1, 0) and dat.hex() == expected


def test_park_brake_bit_is_mirrored_outside_checksum(packer):
  _, dat, _ = gmcan.create_buttons(packer, 0, 1, CruiseButtons.UNPRESS, park_brake=1, auto_stop_off=1)
  assert dat.hex() == "000004012115ee"


@pytest.mark.parametrize("idx,button,expected", [
  (1, CruiseButtons.UNPRESS, "000000010115ee"), (1, CruiseButtons.DECEL_SET, "000000010135ce"),
])
def test_existing_button_frames_unchanged(packer, idx, button, expected):
  _, dat, _ = gmcan.create_buttons(packer, 0, idx, button)
  assert dat.hex() == expected


def _cp(fingerprint):
  cp = structs.CarParams()
  cp.carFingerprint = fingerprint
  cp.networkLocation = structs.CarParams.NetworkLocation.fwdCamera
  cp.transmissionType = structs.CarParams.TransmissionType.automatic
  return cp


def test_sport_mode_never_affects_can_valid():
  from opendbc.car.gm.carstate import CarState
  traverse = CarState.get_can_parsers(_cp(CAR.CHEVROLET_TRAVERSE))[Bus.pt]
  assert traverse.message_states[0x1F4].ignore_alive
  other = CarState.get_can_parsers(_cp(CAR.CHEVROLET_BOLT_EUV))[Bus.pt]
  assert 0x1F4 not in other.message_states
