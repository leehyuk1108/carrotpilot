"""Bounded, data-only link history; never feeds display or vehicle state."""
from collections import deque
import copy
import math
import os
import subprocess
import time
import uuid

PUBLIC_ERRORS = frozenset((
  'Navdy Bluetooth response deadline', 'Navdy Bluetooth disconnected',
  'Navdy Bluetooth disconnected during write', 'Navdy wireless v2 response deadline',
  'Navdy wireless v2 transmit deadline', 'Navdy wireless session lease expired',
  'Navdy snapshot expired before transmission', 'Navdy wireless snapshot predates its lease',
  'Navdy wireless session sequence exhausted', 'Navdy wireless link is not connected',
  'Navdy wireless v2 acknowledgement mismatch', 'Navdy wireless v2 invalid acknowledgement age',
  'Navdy wireless v2 response header/bound', 'Navdy wireless v2 unsolicited response overflow',
  'Navdy wireless acknowledgement mismatch', 'Navdy wireless size/checksum mismatch',
  'Invalid Navdy compressed response', 'Incomplete or trailing Navdy compressed response',
  'Unknown Navdy wireless encoding', 'Unsupported Navdy wireless handshake',
  'Navdy wireless v1 service not advertised', 'Ambiguous Navdy Bluetooth service',
  'Navdy requires an encrypted paired link', 'Configured Navdy is not paired; pairing is never automatic',
  'Native Bluetooth RFCOMM is unavailable on this OS', 'Navdy closed its socket',
  'Navdy USB not found', 'Multiple Navdy USB devices', 'Navdy service failed to start',
  'Jetson Navdy relay response timed out', 'Jetson Navdy relay write timed out',
))


def public_error(error):
  # Never persist subprocess command lines, paths, peer addresses or payloads.
  if isinstance(error, subprocess.TimeoutExpired):
    return 'subprocess_timeout'
  if isinstance(error, subprocess.SubprocessError):
    return 'subprocess_failed'
  if isinstance(error, OSError) and isinstance(error.errno, int):
    return os.strerror(error.errno)
  message = str(error)
  return message if message in PUBLIC_ERRORS else type(error).__name__


def selected(source, keys):
  if not isinstance(source, dict):
    return {}
  return {key: value for key in keys if (value := source.get(key)) is not None
          and (isinstance(value, (bool, int)) or isinstance(value, float) and math.isfinite(value)
               or isinstance(value, str) and len(value) <= 64)}


class LinkDiagnostics:
  def __init__(self, emit):
    self.emit = emit
    self.run_id = uuid.uuid4().hex
    self.events = deque(maxlen=16)
    self.sequence = 0
    self.last_failure = None
    self.receiver = None
    self.source_state = None

  def record(self, kind, **fields):
    self.sequence += 1
    event = {'runId': self.run_id, 'eventSeq': self.sequence, 'at': time.monotonic(), 'kind': kind, **fields}
    self.events.append(event)
    try:
      # Production uses swaglog's nonblocking IPC, not synchronous disk writes.
      self.emit('navdy_link_event', **event)
    except Exception:
      # Diagnostics must not tear down a healthy display link.
      pass
    return event

  def failure(self, *, phase, transport, error, connected_at, wireless):
    self.last_failure = self.record('failure', phase=phase, transport=transport,
                                    errorType=type(error).__name__, reason=public_error(error),
                                    connectedAt=connected_at, wireless=self.metrics(wireless),
                                    sourceState=copy.deepcopy(self.source_state),
                                    receiver=copy.deepcopy(self.receiver))

  @staticmethod
  def metrics(wireless):
    return selected(wireless, ('protocol', 'sequence', 'rttMs', 'receiverAgeMs', 'displayStatus',
                               'inFlight', 'skippedSnapshots', 'ackAgeMs'))

  def sources(self, sources, payload, *, can_valid=True, can_timeout=False):
    missing = {key: value['reason'] for key, value in sources.items()
               if key in ('deviceState', 'pandaStates', 'carState', 'selfdriveState') and not value['usable']}
    if sources['carState']['usable'] and (not can_valid or can_timeout):
      missing['carState'] = 'can_invalid' if not can_valid else 'can_timeout'
    state = {'missing': missing, 'power': payload['displayPower'], 'ambient': payload['ambientState']}
    if state != self.source_state:
      self.record('source_baseline' if self.source_state is None else 'source_changed', state=state)
      self.source_state = state

  def feedback(self, response, *, transport, received_at, elapsed_ms, wireless):
    current = {'wireless': selected(response.get('wireless'), ('session', 'owner', 'clears', 'usbUpdates')),
               'power': selected(response.get('displayPower'), ('state', 'fresh', 'generation', 'invalidations'))}
    if not any(current.values()):
      return
    def changes(state):
      if state is None:
        return None
      return selected(state['wireless'], ('session', 'owner', 'clears')), state['power']

    if changes(current) != changes(self.receiver):
      # First cumulative counters are a baseline, never newly observed failures.
      self.record('receiver_baseline' if self.receiver is None else 'receiver_changed',
                  transport=transport, receivedAt=received_at, previous=self.receiver,
                  current=current, roundTripMs=elapsed_ms, batchLatestWireless=self.metrics(wireless))
    self.receiver = current

  def snapshot(self):
    return copy.deepcopy({'runId': self.run_id, 'eventSeq': self.sequence,
                          'lastFailure': self.last_failure, 'events': list(self.events)})
