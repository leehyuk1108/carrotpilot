"""Turn off engine auto stop once per drive on the Traverse (opt-in through the AutoStopOff param).

Decoded from two drives where the driver pressed the auto-stop-off switch (11/4 12:39:51 and 11/6 13:12:43):
- 0x1E1 ASCMSteeringButton byte4 bit5 is 1 while the switch is held. A real press was 9 consecutive
  frames (0.27 s). The bit is outside the button checksum, and the rolling counter keeps running.
- 0x1F4 byte2 bit1 latches to 1 (auto stop off) about 10 ms after the release frame. It stays 1 until
  the next ignition cycle, and the engine never auto-stopped while it was 1.

The comma can only add frames next to the real button module's frames. It cannot replace them, so the
module sees our press frames interleaved with real "not pressed" frames. How the receiving ECU counts
such a pattern is unknown (one press, several presses, or none), and every attempt is checked on the
latch. The rules:
- start only when the latch is fresh and reads 0, the car is stopped, cruise and openpilot are off,
  CAN is healthy, no real button is held, and no other 0x1E1 frame was sent recently;
- re-check all of those before every press frame, and abort the attempt if any fails;
- stop pressing as soon as the latch reads 1;
- an attempt counts as soon as one frame is sent, so toggling the param cannot exceed the budget;
- stop for the rest of the drive after the latch is confirmed, or after the last attempt.
"""

DT_CTRL = 0.01

IDLE, PRESSING, CONFIRM, DONE, GAVE_UP = 'idle', 'pressing', 'confirm', 'done', 'gave_up'


class AutoStopOff:
  START_DELAY_S = 5.0     # wait after the latch is first seen fresh, so the ECM and button module are settled
  CONFIRM_S = 1.5         # the real latch changed 10 ms after release; allow generous time
  RETRY_DELAY_S = 1.0
  PRESS_FRAMES = (7, 9, 1)  # button frames per attempt, synced to the real 33 Hz frames

  def __init__(self):
    self.state = IDLE
    self.attempt = 0         # attempts used (an attempt is used once its first frame is sent)
    self.frames_left = 0
    self.sent = 0            # frames sent in the current attempt
    self.timer = 0           # frames spent in CONFIRM
    self.ready_frames = 0    # frames with a fresh latch
    self.retry_frames = 0
    self.last_counter = None
    self.event = None        # last transition, for logging by the caller

  def _goto(self, state, why):
    self.state = state
    self.event = f'{state}: {why}'

  def update(self, enabled: bool, latch_fresh: bool, latch: int, counter: int, standstill: bool,
             cruise_engaged: bool, op_enabled: bool, can_ok: bool, buttons_idle: bool,
             other_btn_recent: bool) -> bool:
    """Call once per control frame (100 Hz). Returns True when one press frame should be sent now.

    counter is the rolling counter of the last real 0x1E1 frame. A press frame goes out only right after
    a new real frame arrives, so we add at most one frame per 30 ms slot.
    """
    self.event = None
    new_real_frame = counter != self.last_counter
    self.last_counter = counter

    if self.state in (DONE, GAVE_UP):
      return False

    if latch_fresh:
      self.ready_frames += 1
      if latch == 1:
        self._goto(DONE, 'latch already 1' if self.state == IDLE and self.attempt == 0 else
                   f'latch 1 after attempt {self.attempt + (1 if self.sent else 0)}')
        return False

    ok = (enabled and latch_fresh and standstill and not cruise_engaged and not op_enabled and can_ok
          and buttons_idle and not other_btn_recent)

    if self.state == IDLE:
      if self.retry_frames > 0:
        self.retry_frames -= 1
        return False
      if not (ok and latch == 0 and self.ready_frames * DT_CTRL >= self.START_DELAY_S):
        return False
      self.frames_left = self.PRESS_FRAMES[self.attempt]
      self.sent = 0
      self._goto(PRESSING, f'attempt {self.attempt + 1}, {self.frames_left} frames')

    if self.state == PRESSING:
      if not ok:
        self._abort(self._why(enabled, latch_fresh, standstill, cruise_engaged, op_enabled, can_ok, buttons_idle,
                              other_btn_recent))
        return False
      if not new_real_frame:
        return False
      self.frames_left -= 1
      self.sent += 1
      if self.frames_left <= 0:
        self.timer = 0
        self._goto(CONFIRM, f'attempt {self.attempt + 1} sent')
      return True

    if self.state == CONFIRM:
      if not enabled:
        self._end_attempt('disabled while confirming')
        return False
      self.timer += 1
      if self.timer * DT_CTRL >= self.CONFIRM_S:
        self._end_attempt(f'attempt {self.attempt + 1} not confirmed')
      return False

    return False

  @staticmethod
  def _why(enabled, latch_fresh, standstill, cruise_engaged, op_enabled, can_ok, buttons_idle, other_btn_recent):
    reasons = [name for name, bad in (('disabled', not enabled), ('latch stale', not latch_fresh),
                                      ('moving', not standstill), ('cruise on', cruise_engaged),
                                      ('openpilot on', op_enabled), ('CAN not ok', not can_ok),
                                      ('real button held', not buttons_idle),
                                      ('other button sent', other_btn_recent)) if bad]
    return 'aborted: ' + ', '.join(reasons)

  def _abort(self, why):
    if self.sent > 0:
      self._end_attempt(why)
    else:
      self.retry_frames = int(self.RETRY_DELAY_S / DT_CTRL)
      self._goto(IDLE, why + ' (no frame sent)')

  def _end_attempt(self, why):
    self.attempt += 1
    self.sent = 0
    if self.attempt >= len(self.PRESS_FRAMES):
      self._goto(GAVE_UP, why)
    else:
      self.retry_frames = int(self.RETRY_DELAY_S / DT_CTRL)
      self._goto(IDLE, why)
