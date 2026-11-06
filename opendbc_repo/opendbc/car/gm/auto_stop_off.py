"""Turn off engine auto stop once per drive on the Traverse (opt-in through the AutoStopOff param).

Decoded from two drives where the driver pressed the auto-stop-off switch (11/4 12:39:51 and 11/6 13:12:43):
- 0x1E1 ASCMSteeringButton byte4 bit5 is 1 while the switch is held. A real press was 9 consecutive
  frames (0.27 s). The bit is outside the button checksum, and the rolling counter keeps running.
- 0x1F4 byte2 bit1 latches to 1 (auto stop off) about 10 ms after the release frame. It stays 1 until
  the next ignition cycle, and the engine never auto-stopped while it was 1.

The comma can only add frames next to the real button module's frames. It cannot replace them, so the
module sees our press frames interleaved with real "not pressed" frames. Every attempt therefore ends
in a confirmation window that reads the latch. The rules:
- never press while the latch already reads 1;
- use odd press lengths, so that a per-frame press/release toggle still nets one toggle;
- stop for the rest of the drive after a confirmed latch, or after the last attempt.
"""

DT_CTRL = 0.01

IDLE, PRESSING, CONFIRM, DONE, GAVE_UP = 'idle', 'pressing', 'confirm', 'done', 'gave_up'


class AutoStopOff:
  START_DELAY_S = 5.0     # wait after the latch is first seen, so the ECM and button module are settled
  CONFIRM_S = 1.5         # the real latch changed 10 ms after release; allow generous time
  RETRY_DELAY_S = 1.0
  PRESS_FRAMES = (7, 9, 1)  # button frames per attempt (odd counts), synced to the real 33 Hz frames

  def __init__(self):
    self.state = IDLE
    self.attempt = 0
    self.frames_left = 0
    self.timer = 0           # frames spent in CONFIRM
    self.ready_frames = 0    # frames since the latch was first seen
    self.retry_frames = 0
    self.last_counter = None
    self.event = None        # last transition, for logging by the caller

  def _goto(self, state, why):
    self.state = state
    self.event = f'{state}: {why}'

  def update(self, enabled: bool, latch_seen: bool, latch: int, counter: int, standstill: bool,
             cruise_engaged: bool, op_enabled: bool, bus_busy: bool) -> bool:
    """Call once per control frame (100 Hz). Returns True when one press frame should be sent now.

    counter is the rolling counter of the last real 0x1E1 frame. A press frame goes out only right after
    a new real frame arrives, so we add at most one frame per 30 ms slot.
    """
    self.event = None
    new_real_frame = counter != self.last_counter
    self.last_counter = counter

    if self.state in (DONE, GAVE_UP):
      return False

    if latch_seen:
      self.ready_frames += 1

    if not enabled:
      if self.state in (PRESSING, CONFIRM):
        self._goto(IDLE, 'disabled mid-attempt')
      return False

    if latch_seen and latch == 1 and self.state != PRESSING:
      self._goto(DONE, 'latch already 1' if self.state == IDLE else f'confirmed on attempt {self.attempt + 1}')
      return False

    blocked = cruise_engaged or op_enabled

    if self.state == IDLE:
      if self.retry_frames > 0:
        self.retry_frames -= 1
        return False
      if (latch_seen and latch == 0 and self.ready_frames * DT_CTRL >= self.START_DELAY_S
          and standstill and not blocked):
        self.frames_left = self.PRESS_FRAMES[self.attempt]
        self._goto(PRESSING, f'attempt {self.attempt + 1}, {self.frames_left} frames')
      else:
        return False

    if self.state == PRESSING:
      if blocked:
        # abandon this attempt; the module will see a short press at most
        self._end_attempt('cruise or openpilot engaged while pressing')
        return False
      if not new_real_frame or bus_busy:
        return False
      self.frames_left -= 1
      if self.frames_left <= 0:
        self.timer = 0
        self._goto(CONFIRM, f'attempt {self.attempt + 1} sent')
      return True

    if self.state == CONFIRM:
      self.timer += 1
      if self.timer * DT_CTRL >= self.CONFIRM_S:
        self._end_attempt(f'attempt {self.attempt + 1} not confirmed')
      return False

    return False

  def _end_attempt(self, why):
    self.attempt += 1
    if self.attempt >= len(self.PRESS_FRAMES):
      self._goto(GAVE_UP, why)
    else:
      self.retry_frames = int(self.RETRY_DELAY_S / DT_CTRL)
      self._goto(IDLE, why)
