"""Read-only vehicle telemetry to Navdy; bounded navigation feedback to Carrot."""
from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
from pathlib import Path
import select
import shlex
import signal
import subprocess
import time

from openpilot.selfdrive.navdy.navigation import FEEDBACK_PATH, STATUS_PATH
from openpilot.selfdrive.navdy.transport import AdbTransport, LineReader, encode_frame
from openpilot.selfdrive.navdy.bluetooth import BluetoothTransport, CONFIG_PATH, configured_connection
from openpilot.selfdrive.navdy.connection import UsbProbe
from openpilot.selfdrive.navdy.ambient import DoorMonitor, read_override, road_state
from openpilot.selfdrive.navdy.model_navigation import ModelNavigationTracker, ModelRouteCache, age_model_feedback

MAINTENANCE_STATE = Path('/data/hl-jetson')
JETSON_PYTHON = '/opt/carrot-jetlink/venv/bin/python'
JETSON_RELAY = '/opt/carrot-jetlink/current/openpilot/selfdrive/navdy/transport.py'
SERVICES = ['deviceState', 'pandaStates', 'carState', 'selfdriveState', 'controlsState', 'carControl',
            'carOutput', 'modelV2', 'radarState', 'longitudinalPlan', 'carrotMan']
DISPLAY_INTERVAL = 0.2
DISPLAY_MAX_AGE = 0.5
DEVICE_MAX_AGE = 1.5  # deviceState is published at 2 Hz.


def atomic_json(path, value):
  temporary = path.with_suffix('.tmp')
  temporary.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':')))
  temporary.replace(path)


class SshTransport:
  def __init__(self, host, state=MAINTENANCE_STATE):
    from tools.jetlink.manage_jetson import address, paired_config, ssh_options  # noqa: TID251 -- operational tools live at repository root
    config = paired_config(state)
    relay = (["sudo", "-n", "/usr/bin/python3", "-u", "/run/carrot-data/hl-navdy/navdy_management.py", "relay"]
             if config.get('navdy_relay') == 'protected-tools-v1' else [JETSON_PYTHON, '-u', JETSON_RELAY, '--stdio'])
    remote = shlex.join(relay)
    self.process = subprocess.Popen(['ssh', *ssh_options(state), '-T', 'jetlink@' + address(host), remote],
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
    os.set_blocking(self.process.stdin.fileno(), False)
    os.set_blocking(self.process.stdout.fileno(), False)
    self.reader = LineReader()
    self.deadline = 0.0

  def connect(self):
    from openpilot.selfdrive.navdy.display import payload_from_messages
    # Old relays have no readiness handshake. An inactive bootstrap is compatible
    # with them and cannot forward active telemetry captured before SSH/ADB setup.
    self.exchange(payload_from_messages(seq=0, onroad=False))

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


def display_sources(sm):
  now = time.monotonic()
  sources = {}
  for service in SERVICES:
    received_age = now - sm.recv_time[service]
    published_age = now - sm.logMonoTime[service] / 1e9
    clock_ok = (sm.logMonoTime[service] > 0 and math.isfinite(received_age) and math.isfinite(published_age)
                and received_age >= 0 and published_age >= 0)
    age = max(received_age, published_age) if clock_ok else None
    limit = DEVICE_MAX_AGE if service in ('deviceState', 'pandaStates') else DISPLAY_MAX_AGE
    if not sm.seen[service]:
      reason = 'not_seen'
    elif not sm.valid[service]:
      reason = 'invalid'
    elif age is None:
      reason = 'invalid_timestamp'
    elif age >= limit:
      reason = 'stale'
    else:
      reason = ''
    sources[service] = {'usable': not reason, 'reason': reason, 'alive': bool(sm.alive[service]),
                        'valid': bool(sm.valid[service]), 'ageSeconds': round(age, 4) if age is not None else None}
  return sources


def live_payload(sm, sequence, *, sources=None, door_open=False, ambient_override=None):
  from openpilot.selfdrive.navdy.display import payload_from_messages, resolve_operating_state
  sources = display_sources(sm) if sources is None else sources

  def recent(service):
    # SubMaster's 100 ms liveness cutoff is shorter than this display's 200 ms
    # tick. Tolerate a missed tick, not an invalid message or an old queued frame.
    return sm[service] if sources[service]['usable'] else None

  device = recent('deviceState')
  onroad = device is not None and bool(device.started)
  car_state = recent('carState')
  known_road = road_state(device, recent('pandaStates'))
  if known_road is True and car_state is not None and car_state.canValid and not car_state.canTimeout:
    door_open = bool(car_state.doorOpen)
  payload = payload_from_messages(
    seq=sequence, onroad=onroad, car_state=car_state, selfdrive_state=recent('selfdriveState'),
    controls_state=recent('controlsState'), car_control=recent('carControl'), car_output=recent('carOutput'),
    model=recent('modelV2'), radar_state=recent('radarState'), longitudinal_plan=recent('longitudinalPlan'),
    carrot_man=recent('carrotMan'), door_open=door_open,
  )
  # Offroad has no card/selfdrived messages. Ambient/power state instead comes
  # from fresh device/panda state; never grant active telemetry from this path.
  payload['displayPower'] = 'unknown' if known_road is None else 'onroad' if known_road else 'offroad'
  if known_road is not None:
    payload['onroad'] = known_road
    payload['ambientState'] = resolve_operating_state(connected=True, onroad=known_road,
                                                     door_open=door_open, gear=payload['gear'] if known_road else 'unknown')
  else:
    payload['ambientState'] = 'disconnected'
  if ambient_override is not None:
    payload['ambientOverride'] = ambient_override
  if payload['active'] and 'navPathLeft' not in payload:
    # The HUD retains geometry when keys are omitted; empty arrays explicitly clear it.
    payload.update(navPathLeft=[], navPathRight=[], navLaneLeft=[], navLaneRight=[])
  return payload


def run(*, feedback=FEEDBACK_PATH, status=STATUS_PATH, duration=None, bluetooth_config=CONFIG_PATH):
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
    failures = 0
    peer = None
    bluetooth_only = False
    config_error = ''
    config_checked = -math.inf
    usb_probe = UsbProbe(AdbTransport)
    last_handover = None
    last_connected_transport = None
    previous_available = False
    extended_feedback = False
    last_display_dropout = None
    model_navigation = ModelNavigationTracker()
    model_route_path = feedback.with_name(feedback.stem + '-model-route.json')
    model_route_cache = ModelRouteCache(model_route_path, atomic_json)
    doors = DoorMonitor()
    doors.start()
    try:
      while duration is None or time.monotonic() - start < duration:
        tick = time.monotonic()
        sm.update(0)
        sequence += 1
        if tick - config_checked >= 5:
          config_checked = tick
          old_policy = bluetooth_only
          try:
            peer, bluetooth_only = configured_connection(bluetooth_config)
            config_error = ''
          except (OSError, ValueError) as error:
            peer, config_error = None, str(error)[:160]
          if transport is not None and (old_policy != bluetooth_only or
                                        (transport_name == 'bluetooth' and peer != transport.peer)):
            transport.close()
            transport = None
            usb_probe.close()
            usb_probe = UsbProbe(AdbTransport)
            feedback.unlink(missing_ok=True)
            extended_feedback = False
            model_navigation.clear('model_link_changed')
            model_route_cache.update(model_navigation)
            next_attempt, failures, candidate_index = 0.0, 0, 0
        if not bluetooth_only and transport_name in ('bluetooth', 'jetson') and transport is not None:
          ready = usb_probe.poll(tick)
          if ready is not None:
            transport.close()
            usb_probe.close()
            usb_probe = UsbProbe(AdbTransport)
            transport = ready
            feedback.unlink(missing_ok=True)
            extended_feedback = False
            model_navigation.clear('model_link_changed')
            model_route_cache.update(model_navigation)
            if ready is not None:
              transport_name, connected_at = 'usb', time.monotonic()
            next_attempt, failures = 0.0, 0
            sm.update(0)
        if transport is None and tick < next_attempt:
          time.sleep(0.1)
          continue
        try:
          if transport is None:
            transport_name = 'bluetooth' if bluetooth_only else 'usb'
            try:
              if bluetooth_only:
                if peer is None:
                  raise ConnectionError(config_error or 'Bluetooth-only mode requires an enabled paired peer')
                transport = BluetoothTransport(peer)
              else:
                transport = AdbTransport()
              transport.connect()
            except (OSError, ValueError, subprocess.SubprocessError):
              if transport is not None:
                transport.close()
              transport = None
              if bluetooth_only:
                raise
              hosts = jetson_candidates()
              options = ([('bluetooth', peer)] if peer else [])
              options.extend(('jetson', host) for host in hosts)
              if not options:
                raise ConnectionError(config_error or 'Navdy USB not found; no paired Jetson or enabled Bluetooth peer') from None
              transport_name, target = options[candidate_index % len(options)]
              candidate_index += 1
              if transport_name == 'bluetooth':
                transport = BluetoothTransport(target)
                transport.connect()
              else:
                transport = SshTransport(target)
                transport.connect()
            connected_at = time.monotonic()
            # Discovery/service startup can block. Drain fresh messages before sending.
            sm.update(0)
          captured_ns = time.monotonic_ns()
          sources = display_sources(sm)
          payload = live_payload(sm, sequence, sources=sources, door_open=doors.door_open, ambient_override=read_override())
          payload['modelRoute'] = model_navigation.request(time.monotonic())
          exchange_started = time.monotonic()
          if transport_name == 'bluetooth':
            replies = transport.exchange_batch(payload, captured_ns=captured_ns)
          else:
            response = transport.exchange(payload)
            received = time.monotonic()
            replies = [(response, received, (received - exchange_started) * 1000.)]
          received = time.monotonic()
          for response, reply_received, elapsed_ms in replies:
            age_model_feedback(response, elapsed_ms)
            model_navigation.accept(response.get('modelNavigation'), reply_received, received)
          cache_token = model_route_cache.update(model_navigation)
          if transport_name == 'usb':
            # Every new cable loss starts with the configured wireless peer,
            # not the next failed relay left over from an earlier handover.
            candidate_index = 0
          if last_connected_transport is not None and last_connected_transport != transport_name:
            last_handover = {'at': received, 'from': last_connected_transport, 'to': transport_name}
          last_connected_transport = transport_name
          if received - connected_at >= 2:
            failures = 0
            if transport_name == 'bluetooth':
              # Retry a proven wireless link first after a transient loss. A
              # failed reconnect still advances to the paired Jetson relays.
              candidate_index = 0
          missing = {service: sources[service]['reason'] for service in ('deviceState', 'carState', 'selfdriveState')
                     if not sources[service]['usable']}
          car_state = sm['carState']
          if sources['carState']['usable'] and (not car_state.canValid or car_state.canTimeout):
            missing['carState'] = 'can_invalid' if not car_state.canValid else 'can_timeout'
          if previous_available and missing:
            last_display_dropout = {'at': received, 'sources': missing}
          previous_available = not missing
          # No reply means no new phone observation. Leave its original receipt
          # time intact so existing freshness rules expire it normally.
          if replies:
            response, reply_received, _ = replies[-1]
            extended_feedback = 'navigationFeedback' in response
            atomic_json(feedback, {'receivedAt': reply_received, 'feedback': response, 'modelRouteCacheToken': cache_token})
          atomic_json(status, {'updatedAt': received, 'connected': True, 'transport': transport_name,
                               'connectedAt': connected_at, 'extendedFeedback': extended_feedback,
                               'display': {'sequence': sequence, 'onroad': payload['onroad'], 'active': payload['active'],
                                           'ambientState': payload['ambientState'], 'sources': sources},
                               'power': payload['displayPower'],
                               'ambient': {'doorOpen': payload['doorOpen'], 'gmDoorMonitor': doors.gm, 'error': doors.error},
                               'lastDisplayDropout': last_display_dropout,
                               'lastHandover': last_handover, 'wirelessConfigured': peer is not None,
                               'transportPolicy': 'bluetooth_only' if bluetooth_only else 'usb_first',
                               'wireless': getattr(transport, 'last_exchange', None),
                               'modelNavigation': {'routeReady': bool(model_navigation.points),
                                                   'reason': model_navigation.reason},
                               'reason': ''})
        except (OSError, ValueError, subprocess.SubprocessError) as error:
          if transport_name in ('bluetooth', 'jetson'):
            usb_probe.close()
            usb_probe = UsbProbe(AdbTransport)
          if transport is not None:
            transport.close()
            transport = None
          feedback.unlink(missing_ok=True)
          extended_feedback = False
          model_navigation.clear('model_disconnected')
          model_route_cache.update(model_navigation)
          failures += 1
          retry_delay = min(10, 2 ** min(failures, 4))
          atomic_json(status, {'updatedAt': time.monotonic(), 'connected': False, 'transport': transport_name,
                               'transportPolicy': 'bluetooth_only' if bluetooth_only else 'usb_first',
                               'retryInSeconds': retry_delay, 'wirelessConfigured': peer is not None,
                               'reason': str(error)[:200]})
          next_attempt = time.monotonic() + retry_delay
        time.sleep(max(0, DISPLAY_INTERVAL - (time.monotonic() - tick)))
    finally:
      doors.close()
      usb_probe.close()
      if transport is not None:
        transport.close()
      feedback.unlink(missing_ok=True)
      model_route_path.unlink(missing_ok=True)
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
