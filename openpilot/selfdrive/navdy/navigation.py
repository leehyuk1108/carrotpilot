"""Bounded Navdy feedback, separate from vehicle control and USB ownership."""
from __future__ import annotations

import json
import math
from pathlib import Path
import re
import threading
import time

from openpilot.selfdrive.carrot.carrot_navi_control import (
  CarrotNaviControl, NaviGuidanceControl, NaviRouteControl, NaviSpeedControl,
  NaviTrafficControl, NaviVehicleControl,
)

FEEDBACK_PATH = Path('/dev/shm/navdy-feedback.json')
STATUS_PATH = Path('/dev/shm/navdy-status.json')
MAX_FRAME_BYTES = 32768
LINK_TTL = 2.0
SOURCE_TTL = 4.0


def number(value, minimum, maximum):
  if isinstance(value, bool) or not isinstance(value, (int, float)):
    return None
  return value if math.isfinite(value) and minimum <= value <= maximum else None


def camera_distance(value):
  if not isinstance(value, str):
    return None
  match = re.fullmatch(r'\s*(\d+(?:\.\d+)?)\s*(m|km)\s*', value.lower())
  if match is None:
    return None
  distance = float(match[1]) * (1000 if match[2] == 'km' else 1)
  return int(distance) if 0 < distance <= 10000 else None


class FeedbackTracker:
  def __init__(self):
    self.received_at = -math.inf
    self.components = {}
    self.sequences = {}
    self.revision = 0
    self.revisions = {'road': 0, 'guidance': 0, 'camera': 0}
    self.camera = None
    self.extended = False
    self.speed_signature = None
    self.speed_sequence = 0

  def _component(self, name, value, received_at):
    if value is None:
      if self.components.pop(name, None) is not None:
        self.revision += 1
        self.revisions[name] += 1
      return
    if not isinstance(value, dict) or type(value.get('version')) is not int or value['version'] != 1:
      return
    session, sequence = value.get('session'), value.get('sequence')
    age = number(value.get('ageMs'), 0, SOURCE_TTL * 1000)
    if (not isinstance(session, str) or not 1 <= len(session) <= 64
        or type(sequence) is not int or not 0 <= sequence <= 2**53 or age is None):
      return
    valid_for = value.get('validForMs')
    if valid_for is not None and (type(valid_for) is not int or not 0 <= valid_for <= SOURCE_TTL * 1000):
      return
    deadline = received_at + valid_for / 1000 if valid_for is not None else received_at - age / 1000 + SOURCE_TTL
    previous = self.sequences.get(name)
    if previous is not None and previous[0] == session and sequence <= previous[1]:
      if sequence == previous[1] and name in self.components:
        old_value, captured, old_deadline = self.components[name]
        self.components[name] = old_value, captured, min(old_deadline, deadline)
      return
    self.sequences[name] = (session, sequence)
    self.components[name] = (value, received_at - age / 1000, deadline)
    self.revision += 1
    self.revisions[name] += 1

  def accept(self, packet, received_at, now):
    if (not isinstance(packet, dict) or number(received_at, 0, now) is None
        or now - received_at > LINK_TTL or received_at <= self.received_at):
      return False
    self.received_at = received_at
    self.extended = 'navigationFeedback' in packet
    self._component('road', packet.get('roadPreview'), received_at)
    self._component('guidance', packet.get('navigationFeedback'), received_at)
    speed = packet.get('cameraSpeedKph')
    distance = camera_distance(packet.get('cameraDistance'))
    kind_value = packet.get('cameraType')
    kind = {'fixed': 1, 'mobile': 7, 'section': 4}.get(kind_value) if isinstance(kind_value, str) else None
    camera = None
    if (packet.get('cameraSource') == 'trafficNotification' and kind is not None
        and type(speed) is int and 20 <= speed <= 140 and distance is not None):
      camera = (kind, speed, distance)
    if self.camera != camera:
      self.camera = camera
      self.revision += 1
      self.revisions['camera'] += 1
    return True

  def _fresh(self, name, now):
    value, captured_at, deadline = self.components.get(name, (None, -math.inf, -math.inf))
    if 0 <= now - captured_at < SOURCE_TTL and now < deadline:
      return value, now - captured_at
    if self.components.pop(name, None) is not None:
      self.revision += 1
      self.revisions[name] += 1
    return None, 0

  def snapshot(self, now):
    if not 0 <= now - self.received_at < LINK_TTL:
      return None
    road, _ = self._fresh('road', now)
    raw_guidance, age = self._fresh('guidance', now)
    guidance = raw_guidance.get('guidance') if raw_guidance else None
    if not isinstance(guidance, dict) or guidance.get('active') is not True:
      guidance = {}
    road_limit = road.get('roadLimitKph') if road else None
    if type(road_limit) is not int or not 10 <= road_limit <= 200 or road_limit % 10:
      road_limit = None
    camera = self.camera
    revision = self.revisions['guidance']
    sdi = guidance.get('sdi')
    bump_distance = None
    if isinstance(sdi, dict) and type(sdi.get('type')) is int and sdi['type'] == 22:
      bump_distance = number(sdi.get('distanceMeters'), 1, 10000)
    # A route/road heartbeat must not rewind Carrot's ego-motion distance estimate.
    signature = (road_limit, camera, bump_distance)
    if signature != self.speed_signature:
      self.speed_signature = signature
      self.speed_sequence += 1

    def turn(prefix):
      kind = guidance.get(prefix + 'Type')
      distance = number(guidance.get(prefix + 'DistanceMeters'), 0, 100000)
      present = type(kind) is int and 1 <= kind <= 2000 and distance is not None
      title = guidance.get('roadName', '') if prefix == 'primary' else ''
      return NaviGuidanceControl(present, revision, int(distance or 0), kind if present else -1,
                                 title[:256] if isinstance(title, str) else '',
                                 received_mono_time_ns=max(0, int((now - age) * 1e9)) if present else 0)

    signal = guidance.get('trafficSignal')
    traffic = NaviTrafficControl(False, revision)
    if isinstance(signal, dict) and signal.get('visible') is True and raw_guidance.get('validForMs') is not None:
      remaining = number(signal.get('remainingSeconds'), 0, 600)
      # The HUD exports its shortest signal/source deadline. Polling cannot
      # extend it; an older APK without that bound cannot supply a signal.
      signal_age = max(age, 2.5 - (self.components['guidance'][2] - now))
      remaining = max(0, int(remaining - signal_age)) if remaining is not None else 0
      lamps = [name for name in ('red', 'left', 'green') if signal.get(name) is True]
      lamp = lamps[0] if lamps else ''
      if lamp and remaining > 0:
        traffic = NaviTrafficControl(True, revision, True, int(number(guidance.get('primaryDistanceMeters'), 0, 100000) or 0),
                                     'navdy', lamp, remaining)
    speed = NaviSpeedControl(road_limit is not None or camera is not None or bump_distance is not None, self.speed_sequence, road_limit,
                            sdi_present=camera is not None, sdi_type=camera[0] if camera else -1,
                            sdi_speed_limit_kph=camera[1] if camera else 0, sdi_distance_m=camera[2] if camera else 0,
                            secondary_sdi_present=bump_distance is not None, secondary_sdi_type=22 if bump_distance is not None else -1,
                            secondary_sdi_distance_m=int(bump_distance or 0))
    current, upcoming = turn('primary'), turn('secondary')
    route = NaviRouteControl(bool(guidance), revision,
                             int(number(guidance.get('remainingDistanceMeters'), 0, 10000000) or 0),
                             int(number(guidance.get('remainingTimeSeconds'), 0, 604800) or 0))
    if not (speed.present or current.present or upcoming.present or route.present or traffic.present):
      return None
    # Preserve producer identity after component expiry, but reset Carrot's
    # distance history when either producer actually restarts.
    session = 'navdy:' + json.dumps([self.sequences.get(name, ('',))[0] for name in ('road', 'guidance')], separators=(',', ':'))
    return CarrotNaviControl(session, NaviVehicleControl(False, revision), speed, current, upcoming, route, traffic,
                             guidance_active=bool(guidance))


class NavigationReader:
  def __init__(self, path=FEEDBACK_PATH):
    self.path = Path(path)
    self.tracker = FeedbackTracker()
    self.last_poll = -math.inf
    self.last_stat = None
    self.lock = threading.Lock()

  def read(self, now=None):
    now = time.monotonic() if now is None else now
    with self.lock:
      return self._read(now)

  def _read(self, now):
    if now - self.last_poll >= 0.1:
      self.last_poll = now
      try:
        info = self.path.stat()
        stamp = (info.st_mtime_ns, info.st_ino)
        if stamp != self.last_stat:
          self.last_stat = stamp
          with self.path.open('rb') as stream:
            data = stream.read(MAX_FRAME_BYTES + 1025)
          if len(data) <= MAX_FRAME_BYTES + 1024:
            value = json.loads(data)
            self.tracker.accept(value.get('feedback'), value.get('receivedAt'), now)
      except (OSError, ValueError, TypeError, AttributeError):
        pass
    return self.tracker.snapshot(now)

  def status(self):
    now = time.monotonic()
    with self.lock:
      state = self._read(now)
      connected = 0 <= now - self.tracker.received_at < LINK_TTL
      return {'connected': connected, 'extendedFeedback': self.tracker.extended,
              'navigationFresh': state is not None,
              'reason': ('navdy_disconnected' if not connected else 'navdy_apk_feedback_update_required'
                         if not self.tracker.extended else 'navdy_navigation_stale' if state is None else '')}
