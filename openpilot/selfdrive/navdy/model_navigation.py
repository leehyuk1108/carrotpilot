"""Bounded, independently timed model inputs. Never interprets display turn IDs."""
from dataclasses import dataclass
import hashlib
import math
import re

from openpilot.selfdrive.navigation.helpers import finite_number, valhalla_type

MAX_POINTS = 4096
MAX_ENCODED = 65536
CHUNK_SIZE = 1024
MAX_AGE = 2.
ASSEMBLY_AGE = 30.
HASH = re.compile(r'[0-9a-f]{64}\Z')


def integer(value, low, high):
  return type(value) is int and low <= value <= high


def bounded(value, low, high):
  return finite_number(value) and low <= value <= high


def identity(value):
  if not isinstance(value, dict) or value.get('source') not in ('tmap', 'naver'):
    return None
  parts = tuple(value.get(k) for k in ('source', 'session', 'routeId'))
  return parts if all(isinstance(p, str) and 1 <= len(p) <= 64 for p in parts) else None


def decode_route(encoded, count, digest):
  if (not isinstance(encoded, str) or not 2 <= len(encoded) <= MAX_ENCODED
      or not integer(count, 2, MAX_POINTS) or not isinstance(digest, str) or not HASH.fullmatch(digest)
      or any(not 63 <= ord(c) <= 126 for c in encoded)):
    raise ValueError('invalid route manifest')
  if hashlib.sha256(encoded.encode('ascii')).hexdigest() != digest:
    raise ValueError('route checksum mismatch')
  values, accumulator, shift = [], 0, 0
  for character in encoded:
    byte = ord(character) - 63
    accumulator |= (byte & 31) << shift
    if byte & 32:
      shift += 5
      if shift > 30:
        raise ValueError('polyline integer overflow')
    else:
      if accumulator > 0xffffffff:
        raise ValueError('polyline integer overflow')
      values.append(~(accumulator >> 1) if accumulator & 1 else accumulator >> 1)
      accumulator = shift = 0
      if len(values) > count * 2:
        raise ValueError('too many coordinates')
  if shift or len(values) != count * 2:
    raise ValueError('incomplete polyline')
  lat = lon = 0
  points = []
  for index in range(0, len(values), 2):
    lat += values[index]
    lon += values[index + 1]
    if abs(lat) > 85051128 or abs(lon) > 180000000:
      raise ValueError('invalid coordinates')
    points.append((lat / 1e6, lon / 1e6))
  if len(set(points)) < 2:
    raise ValueError('empty path')
  return tuple(points)


@dataclass(frozen=True)
class ModelNavigation:
  identity: tuple
  points: tuple
  observed: float
  maneuver_type: str
  modifier: str
  distance: float
  position: tuple | None

  @property
  def session(self):
    # Length-prefixed identity cannot collide through delimiters in a source ID.
    return ''.join(f'{len(part)}:{part}' for part in self.identity)


class ModelNavigationTracker:
  def __init__(self):
    self.high_water = {}
    self.source_sessions = {}
    self.retired_producers = set()
    self.current = None
    self.sequence = -1
    self.observed = self.deadline = self.received = -math.inf
    self.value_received = -math.inf
    self.value = None
    self.position_key = None
    self.position_stamp = -math.inf
    self.manifest = None
    self.chunks = {}
    self.points = ()
    self.encoded = None
    self.assembly_started = -math.inf
    self.reason = 'model_feedback_missing'

  def clear(self, reason='model_feedback_missing'):
    self.current = self.value = self.manifest = self.encoded = None
    self.chunks = {}
    self.points = ()
    self.position_key = None
    self.position_stamp = -math.inf
    self.sequence = -1
    self.reason = reason

  def accept(self, value, received, now):
    if not bounded(received, 0, now) or now - received >= MAX_AGE:
      self.clear('model_link_stale')
      return
    key = identity(value)
    if (key is None or type(value.get('version')) is not int or value['version'] != 1
        or not integer(value.get('sequence'), 1, 2**53 - 1)
        or not bounded(value.get('ageMs'), 0, 2000)
        or not bounded(value.get('validForMs'), 0, 2000)):
      self.clear()
      return
    sequence = value['sequence']
    producer = key[:2]
    if producer in self.retired_producers:
      return
    previous = self.high_water.get(producer)
    if previous and (sequence < previous[0] or (sequence == previous[0] and key != previous[1])):
      return
    # A cleared generation cannot be revived by replaying an old observation.
    if self.current != key and previous and sequence <= previous[0]:
      return
    if producer not in self.high_water and len(self.high_water) >= 16:
      self.clear('model_session_limit')
      return
    previous_session = self.source_sessions.get(key[0])
    if previous_session is not None and previous_session != key[1]:
      self.retired_producers.add((key[0], previous_session))
    self.source_sessions[key[0]] = key[1]
    self.high_water[producer] = sequence, key
    observed = received - value['ageMs'] / 1000.
    deadline = min(observed + MAX_AGE, received + value['validForMs'] / 1000.)
    if (value.get('active') is not True or value.get('offRoute') is not False
        or not 0 <= now - observed < MAX_AGE or deadline <= now):
      self.clear('model_navigation_inactive')
      return
    if key != self.current:
      self.clear('model_route_pending')
      self.current = key
      self.assembly_started = now
    if sequence == self.sequence:
      self.observed = min(self.observed, observed)
      self.deadline = min(self.deadline, deadline)
    else:
      self.sequence, self.observed, self.deadline = sequence, observed, deadline
      self.value = value
      self.value_received = received
    self.received = received
    if not self.points and now - self.assembly_started > ASSEMBLY_AGE:
      self.chunks = {}
      self.manifest = None
      self.assembly_started = now
    chunk = value.get('routeChunk')
    if chunk is not None:
      self._chunk(chunk)
    if self.current is not None:
      self.reason = '' if self.points else 'model_route_pending'

  def _chunk(self, chunk):
    if not isinstance(chunk, dict):
      return
    digest, size, count, point_count = (chunk.get(k) for k in ('sha256', 'encodedLength', 'count', 'pointCount'))
    index, data = chunk.get('index'), chunk.get('data')
    if (chunk.get('encoding') != 'polyline6' or not isinstance(digest, str) or not HASH.fullmatch(digest)
        or not integer(size, 2, MAX_ENCODED) or not integer(count, 1, 64)
        or count != (size + CHUNK_SIZE - 1) // CHUNK_SIZE or not integer(point_count, 2, MAX_POINTS)
        or not integer(index, 0, count - 1) or not isinstance(data, str)
        or len(data) != min(CHUNK_SIZE, size - index * CHUNK_SIZE)
        or any(not 63 <= ord(c) <= 126 for c in data)):
      self.clear('model_route_invalid')
      return
    manifest = digest, size, count, point_count
    if self.manifest is not None and self.manifest != manifest:
      self.clear('model_route_changed_without_id')
      return
    self.manifest = manifest
    if index in self.chunks and self.chunks[index] != data:
      self.clear('model_chunk_conflict')
      return
    self.chunks[index] = data
    if len(self.chunks) == count and not self.points:
      encoded = ''.join(self.chunks[i] for i in range(count))
      try:
        self.points = decode_route(encoded, point_count, digest)
      except ValueError:
        self.clear('model_route_invalid')
        return
      self.encoded = encoded

  def request(self, now):
    if not self._fresh(now):
      return {'routeId': '', 'index': 0}
    count = self.manifest[2] if self.manifest else 1
    return {'routeId': self.current[2], 'index': next((i for i in range(count) if i not in self.chunks), -1)}

  def route_cache(self):
    if not self.points:
      return None
    return dict(zip(('source', 'session', 'routeId'), self.current),
                sha256=self.manifest[0], pointCount=len(self.points), polyline6=self.encoded)

  def import_route(self, cache):
    if self.current is None or identity(cache) != self.current or self.points:
      return
    try:
      points = decode_route(cache.get('polyline6'), cache.get('pointCount'), cache.get('sha256'))
    except (ValueError, AttributeError):
      return
    encoded = cache['polyline6']
    manifest = cache['sha256'], len(encoded), (len(encoded) + CHUNK_SIZE - 1) // CHUNK_SIZE, len(points)
    if self.manifest is not None and self.manifest != manifest:
      return
    self.manifest, self.points = manifest, points
    self.reason = ''

  def _fresh(self, now):
    if (self.value is None or not 0 <= now - self.received < MAX_AGE
        or not 0 <= now - self.observed < MAX_AGE or now >= self.deadline):
      self.clear('model_navigation_stale')
      return False
    return True

  def snapshot(self, now):
    if not self._fresh(now) or not self.points:
      return None
    maneuver = self.value.get('maneuver')
    if not isinstance(maneuver, dict):
      return None
    kind, modifier, distance = (maneuver.get(k) for k in ('type', 'modifier', 'distanceMeters'))
    if not isinstance(kind, str) or not isinstance(modifier, str):
      return None
    command = valhalla_type(kind, modifier)
    if command is None or command <= 6 or not bounded(distance, 0, 1000000):
      return None
    position = self.value.get('position')
    fix = None
    if isinstance(position, dict):
      lat, lon, heading, speed, age, original = (position.get(k) for k in
          ('latitude', 'longitude', 'bearingDegrees', 'speedMps', 'ageMs', 'observedAtMs'))
      if (bounded(lat, -85.05112878, 85.05112878) and bounded(lon, -180, 180)
          and bounded(heading, 0, 359.999999) and bounded(speed, 0, 90)
          and bounded(age, 0, 2000) and integer(original, 1, 2**53 - 1)):
        stamp = self.value_received - age / 1000.
        if original == self.position_key:
          stamp = min(stamp, self.position_stamp)
        elif self.position_key is not None and original < self.position_key:
          return ModelNavigation(self.current, self.points, self.observed, kind, modifier, float(distance), None)
        self.position_key, self.position_stamp = original, stamp
        if 0 <= now - stamp < MAX_AGE:
          fix = lat, lon, heading, speed, stamp
    return ModelNavigation(self.current, self.points, self.observed, kind, modifier, float(distance), fix)


def age_model_feedback(value, elapsed_ms):
  """Conservatively charge the complete return exchange; no source re-dating."""
  component = value.get('modelNavigation')
  if not bounded(elapsed_ms, 0, 2000):
    value['modelNavigation'] = None
    return value
  if isinstance(component, dict):
    for item in (component, component.get('position')):
      if isinstance(item, dict) and bounded(item.get('ageMs'), 0, 2000):
        item['ageMs'] += elapsed_ms
    if bounded(component.get('validForMs'), 0, 2000):
      component['validForMs'] = max(0, component['validForMs'] - math.ceil(elapsed_ms))
  return value
