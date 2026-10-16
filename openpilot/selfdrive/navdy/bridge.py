"""Read-only vehicle telemetry to Navdy; bounded navigation feedback to Carrot."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import select
import shlex
import signal
import subprocess
import time

from openpilot.selfdrive.navdy.navigation import FEEDBACK_PATH, STATUS_PATH
from openpilot.selfdrive.navdy.transport import AdbTransport, LineReader, encode_frame

MAINTENANCE_STATE = Path('/data/hl-jetson')
JETSON_PYTHON = '/opt/carrot-jetlink/venv/bin/python'
JETSON_RELAY = '/opt/carrot-jetlink/current/openpilot/selfdrive/navdy/transport.py'
SERVICES = ['deviceState', 'carState', 'selfdriveState', 'controlsState', 'carControl',
            'carOutput', 'modelV2', 'radarState', 'longitudinalPlan', 'carrotMan']


def atomic_json(path, value):
  temporary = path.with_suffix('.tmp')
  temporary.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':')))
  temporary.replace(path)


class SshTransport:
  def __init__(self, host, state=MAINTENANCE_STATE):
    from tools.jetlink.manage_jetson import address, paired_config, ssh_options  # noqa: TID251 -- operational tools live at repository root
    paired_config(state)
    remote = shlex.join([JETSON_PYTHON, '-u', JETSON_RELAY, '--stdio'])
    self.process = subprocess.Popen(['ssh', *ssh_options(state), '-T', 'jetlink@' + address(host), remote],
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
    os.set_blocking(self.process.stdin.fileno(), False)
    os.set_blocking(self.process.stdout.fileno(), False)
    self.reader = LineReader()
    self.deadline = 0.0

  def recv(self, size):
    timeout = self.deadline - time.monotonic()
    if timeout <= 0 or not select.select([self.process.stdout], [], [], timeout)[0]:
      raise TimeoutError('Jetson Navdy relay response timed out')
    return os.read(self.process.stdout.fileno(), size)

  def exchange(self, payload):
    # First SSH setup may take longer; subsequent display/feedback exchanges are bounded.
    self.deadline = time.monotonic() + (6 if not getattr(self, 'connected', False) else 1)
    data = encode_frame(payload)
    while data:
      timeout = self.deadline - time.monotonic()
      if timeout <= 0 or not select.select([], [self.process.stdin], [], timeout)[1]:
        raise TimeoutError('Jetson Navdy relay write timed out')
      sent = os.write(self.process.stdin.fileno(), data)
      data = data[sent:]
    result = self.reader.receive(self)
    self.connected = True
    return result

  def close(self):
    self.process.terminate()
    try:
      self.process.wait(timeout=2)
    except subprocess.TimeoutExpired:
      self.process.kill()
      self.process.wait(timeout=2)
    self.process.stdin.close()
    self.process.stdout.close()


def jetson_candidates():
  try:
    from tools.jetlink.manage_jetson import candidates, discover, paired_config  # noqa: TID251 -- operational tools live at repository root
    config = paired_config(MAINTENANCE_STATE)
    return candidates(config, MAINTENANCE_STATE, discover())
  except (OSError, ValueError, ImportError, subprocess.SubprocessError):
    return []


def live_payload(sm, sequence):
  from openpilot.selfdrive.navdy.display import payload_from_messages
  now = time.monotonic()

  def recent(service):
    return sm[service] if sm.alive[service] and sm.valid[service] and 0 <= now - sm.recv_time[service] < 1.5 else None

  device = recent('deviceState')
  onroad = device is not None and bool(device.started)
  car_state = recent('carState')
  return payload_from_messages(
    seq=sequence, onroad=onroad, car_state=car_state, selfdrive_state=recent('selfdriveState'),
    controls_state=recent('controlsState'), car_control=recent('carControl'), car_output=recent('carOutput'),
    model=recent('modelV2'), radar_state=recent('radarState'), longitudinal_plan=recent('longitudinalPlan'),
    carrot_man=recent('carrotMan'),
  )


def run(*, feedback=FEEDBACK_PATH, status=STATUS_PATH, duration=None):
  from openpilot.cereal import messaging
  sm = messaging.SubMaster(SERVICES)
  lock_path = feedback.with_suffix('.lock')
  with lock_path.open('a') as lock:
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    transport = None
    transport_name = 'usb'
    connected_at = 0.0
    sequence = 0
    start = time.monotonic()
    next_attempt = 0.0
    candidate_index = 0
    try:
      while duration is None or time.monotonic() - start < duration:
        tick = time.monotonic()
        sm.update(0)
        sequence += 1
        if transport is None and tick < next_attempt:
          time.sleep(0.1)
          continue
        try:
          if transport is None:
            transport = AdbTransport()
            try:
              transport.connect()
              transport_name = 'usb'
            except (OSError, ValueError, subprocess.SubprocessError):
              transport.close()
              transport = None
              hosts = jetson_candidates()
              if not hosts:
                raise ConnectionError('Navdy USB not found; no paired Jetson management connection') from None
              host = hosts[candidate_index % len(hosts)]
              candidate_index += 1
              transport = SshTransport(host)
              transport_name = 'jetson'
            connected_at = tick
          response = transport.exchange(live_payload(sm, sequence))
          received = time.monotonic()
          atomic_json(feedback, {'receivedAt': received, 'feedback': response})
          atomic_json(status, {'updatedAt': received, 'connected': True, 'transport': transport_name,
                               'connectedAt': connected_at, 'extendedFeedback': 'navigationFeedback' in response,
                               'reason': ''})
        except (OSError, ValueError, subprocess.SubprocessError) as error:
          if transport is not None:
            transport.close()
            transport = None
          feedback.unlink(missing_ok=True)
          atomic_json(status, {'updatedAt': time.monotonic(), 'connected': False, 'transport': transport_name,
                               'reason': str(error)[:200]})
          next_attempt = time.monotonic() + 2
        time.sleep(max(0, 0.2 - (time.monotonic() - tick)))
    finally:
      if transport is not None:
        transport.close()
      feedback.unlink(missing_ok=True)
      atomic_json(status, {'updatedAt': time.monotonic(), 'connected': False, 'reason': 'bridge_stopped'})


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument('--feedback-file', type=Path, default=FEEDBACK_PATH)
  parser.add_argument('--status-file', type=Path, default=STATUS_PATH)
  parser.add_argument('--duration', type=float)
  args = parser.parse_args()

  def stop(*_args):
    raise SystemExit(0)

  signal.signal(signal.SIGTERM, stop)
  run(feedback=args.feedback_file, status=args.status_file, duration=args.duration)


if __name__ == '__main__':
  main()
