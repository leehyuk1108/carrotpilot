import pytest

from opendbc.can.packer import CANPacker
from opendbc.car.gm import gmcan
from opendbc.car.gm.auto_stop_off import AutoStopOff, CONFIRM, DONE, GAVE_UP, IDLE, PRESSING, DT_CTRL
from opendbc.car.gm.values import CruiseButtons

READY = int(AutoStopOff.START_DELAY_S / DT_CTRL)


class Sim:
  """Feeds AutoStopOff at 100 Hz with a real 0x1E1 frame every 3 control frames (33 Hz)."""

  def __init__(self, **kw):
    self.a = AutoStopOff()
    self.frame = 0
    self.counter = 0
    self.state = dict(enabled=True, latch_seen=True, latch=0, standstill=True, cruise_engaged=False,
                      op_enabled=False, bus_busy=False)
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


def test_disabled_never_presses():
  s = Sim(enabled=False).step(READY + 2000)
  assert s.presses == [] and s.a.state == IDLE


def test_latch_already_off_never_presses():
  s = Sim(latch=1).step(READY + 2000)
  assert s.presses == [] and s.a.state == DONE


def test_waits_for_start_delay_and_latch():
  s = Sim(latch_seen=False).step(2000)
  assert s.presses == []
  s.step(READY - 5, latch_seen=True)
  assert s.presses == []
  s.step(10)
  assert s.a.state == PRESSING and len(s.presses) >= 1


@pytest.mark.parametrize("kw", [dict(standstill=False), dict(cruise_engaged=True), dict(op_enabled=True)])
def test_preconditions_block_start(kw):
  s = Sim(**kw).step(READY + 500)
  assert s.presses == [] and s.a.state == IDLE


def test_presses_once_per_real_frame_then_confirms():
  s = Sim().step(READY + 30)
  assert s.a.state == CONFIRM
  assert len(s.presses) == AutoStopOff.PRESS_FRAMES[0]
  gaps = {b - a for a, b in zip(s.presses, s.presses[1:])}
  assert gaps == {3}  # one press frame per real 33 Hz frame
  s.step(20, latch=1)
  assert s.a.state == DONE
  s.step(2000, latch=0)  # driver turns auto stop back on: leave it alone
  assert len(s.presses) == AutoStopOff.PRESS_FRAMES[0] and s.a.state == DONE


def test_gives_up_after_all_attempts():
  s = Sim().step(READY + 3000)
  assert s.a.state == GAVE_UP
  assert len(s.presses) == sum(AutoStopOff.PRESS_FRAMES)
  assert all(n % 2 == 1 for n in AutoStopOff.PRESS_FRAMES)
  s.step(3000)
  assert len(s.presses) == sum(AutoStopOff.PRESS_FRAMES)


def test_engaging_mid_press_ends_attempt():
  s = Sim().step(READY + 6)
  assert s.a.state == PRESSING
  sent = len(s.presses)
  s.step(1, op_enabled=True)
  assert s.a.state == IDLE and s.a.attempt == 1
  s.step(500)
  assert len(s.presses) == sent


def test_disable_mid_press_stops():
  s = Sim().step(READY + 6)
  sent = len(s.presses)
  s.step(500, enabled=False)
  assert len(s.presses) == sent and s.a.state == IDLE


def test_busy_bus_delays_without_losing_frames():
  s = Sim().step(READY + 3)
  before = len(s.presses)
  s.step(30, bus_busy=True)
  assert len(s.presses) == before and s.a.state == PRESSING
  s.step(40, bus_busy=False)
  assert len(s.presses) == AutoStopOff.PRESS_FRAMES[0] and s.a.state == CONFIRM


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
