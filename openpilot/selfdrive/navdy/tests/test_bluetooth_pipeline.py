import json
import socket
import time
from types import SimpleNamespace as NS
import zlib

import pytest

from openpilot.selfdrive.navdy import bluetooth as bt
from openpilot.selfdrive.navdy import bluetooth_pipeline as pipe

PAYLOAD = {'schema': 'navdy.openpilot.v1', 'active': True}


class Wire:
  def __init__(self):
    self.input = bytearray()
    self.output = bytearray()
    self.write_limit = 100000
    self.read_limit = 100000
    self.closed = False

  def setblocking(self, value):
    assert value is False

  def recv(self, size):
    if not self.input:
      if self.closed:
        return b''
      raise BlockingIOError()
    count = min(size, len(self.input), self.read_limit)
    value = self.input[:count]
    del self.input[:count]
    return value

  def send(self, value):
    if not self.write_limit:
      raise BlockingIOError()
    count = min(len(value), self.write_limit)
    self.output.extend(value[:count])
    return count

  def close(self):
    self.closed = True

  def frames(self):
    data, result = self.output, []
    while data:
      head = bt.REQUEST.unpack(data[:bt.REQUEST.size])
      size = bt.REQUEST.size + head[5]
      raw = bt.unpack_payload(bytes(data[bt.REQUEST.size:size]), head[4], head[7], head[6])
      result.append((head, json.loads(raw)))
      data = data[size:]
    return result

  def ack(self, sequence, *, status=0, age=100000, lease=None, magic=b'NVA2', raw=None, crc=None, size=None):
    raw = raw or b'{"navigationFeedback":{"sequence":7,"ageMs":10,"validForMs":1000}}'
    wire = zlib.compress(raw)
    self.input.extend(bt.RESPONSE.pack(magic, sequence, status, sequence + 1 if lease is None else lease, age,
                                      len(raw), len(wire) if size is None else size,
                                      zlib.crc32(raw) if crc is None else crc, 1) + wire)


@pytest.fixture
def link(monkeypatch):
  clock = NS(ns=10_000_000_000)
  monkeypatch.setattr(pipe.time, 'monotonic_ns', lambda: clock.ns)
  wire = Wire()
  state = pipe.SnapshotPipeline(wire, clock.ns, reader_factory=lambda conn: pipe.ResponseReader(conn, threaded=False))
  return clock, wire, state


def tick(link, seconds=0., **payload):
  clock, _, state = link
  clock.ns += round(seconds * 1e9)
  return state.update({**PAYLOAD, **payload}, captured_ns=clock.ns)


def test_two_snapshots_do_not_wait_for_previous_ack_and_never_accumulate(link):
  _, wire, state = link
  assert tick(link, seq=0) == []
  assert tick(link, .2, seq=1) == []
  assert len(state.pending) == 2
  assert tick(link, .2, seq=2) == []
  assert len(wire.frames()) == 2
  wire.ack(0)
  wire.ack(1)
  replies = tick(link, .2, seq=3)
  assert len(replies) == 2
  assert [f[1]['seq'] for f in wire.frames()] == [0, 1, 3]
  assert all(f[0][0] == b'NVQ2' for f in wire.frames())
  assert state.skipped == 1 and len(state.pending) == 1
  assert tick(link, .1, seq=4) == []


def test_ack_age_charges_original_capture_and_preserves_source_sequence(link):
  tick(link)
  _, wire, _ = link
  wire.ack(0)
  response, received, elapsed = tick(link, .7)[0]
  assert elapsed == 700 and received == 10.7
  assert response['navigationFeedback'] == {'sequence': 7, 'ageMs': 710., 'validForMs': 300}


@pytest.mark.parametrize('status,age', [(1, 600000), (3, 100000)])
def test_expired_display_or_usb_owner_does_not_disconnect_healthy_link(link, status, age):
  _, wire, state = link
  tick(link)
  wire.ack(0, status=status, age=age)
  assert len(tick(link, .7)) == 1
  assert state.diagnostics()['displayStatus'] == status
  assert not wire.closed and state.sequence == 2


@pytest.mark.parametrize('fault', [{'magic': b'NVA1'}, {'status': 2}, {'status': 4}, {'age': -1},
                                  {'age': bt.TTL_US}, {'lease': 9}, {'crc': 0}, {'size': bt.MAX_BYTES + 1}])
def test_invalid_ack_is_not_tolerated(link, fault):
  _, wire, _ = link
  tick(link)
  wire.ack(0, **fault)
  with pytest.raises(ValueError):
    tick(link, .2)


def test_unsolicited_and_out_of_order_acks_fail_closed(link):
  _, wire, _ = link
  tick(link)
  tick(link, .1)
  wire.ack(1)
  with pytest.raises(ValueError, match='mismatch'):
    tick(link, .1)


def test_replayed_ack_cannot_renew_lease(link):
  _, wire, _ = link
  tick(link)
  wire.ack(0)
  tick(link, .1)
  wire.ack(0)
  with pytest.raises(ValueError, match='mismatch'):
    tick(link, .1)


def test_split_header_and_body_are_bounded(link):
  _, wire, state = link
  tick(link)
  wire.ack(0)
  remainder = bytes(wire.input[7:])
  del wire.input[7:]
  assert tick(link, .1) == [] and len(state.reader.rx) == 7
  wire.input.extend(remainder)
  wire.read_limit = 1
  assert len(tick(link, .1)) == 1 and not state.reader.rx


@pytest.mark.parametrize('with_ack', [False, True])
def test_silent_or_too_late_response_has_absolute_deadline(link, with_ack):
  _, wire, _ = link
  tick(link)
  tick(link, .2)
  if with_ack:
    wire.ack(0)
  with pytest.raises(TimeoutError, match='response deadline'):
    tick(link, 1.3)


def test_write_backpressure_has_one_buffer_and_original_expiry(link):
  _, wire, state = link
  wire.write_limit = 1
  tick(link)
  assert len(state.pending) == 1 and state.tx
  wire.write_limit = 0
  tick(link, .2)
  assert len(state.pending) == 1
  with pytest.raises(TimeoutError, match='transmit deadline'):
    tick(link, .3)


def test_partial_write_is_completed_without_duplicate_bytes(link):
  _, wire, _ = link
  wire.write_limit = 7
  tick(link, seq=1)
  wire.write_limit = 100000
  tick(link, .1, seq=2)
  assert [f[1]['seq'] for f in wire.frames()] == [1, 2]


def test_ack_arriving_after_capture_cannot_be_its_lease(link):
  clock, wire, state = link
  tick(link)
  wire.ack(0)
  captured = clock.ns + 100_000_000
  clock.ns = captured + 10_000_000
  state.update(PAYLOAD, captured_ns=captured)
  head, _ = wire.frames()[-1]
  assert head[2] == 0 and head[3] == 100000


def test_lease_history_and_inflight_memory_remain_bounded(link):
  _, wire, state = link
  tick(link)
  for seq in range(100):
    wire.ack(seq)
    tick(link, .2)
    wire.output.clear()
  assert len(state.leases) == pipe.LEASE_HISTORY
  assert len(state.pending) == 1 and not state.reader.rx and not state.tx


def test_wrapper_closes_and_discards_pipeline_after_protocol_error(link):
  _, wire, state = link
  tick(link)
  client = bt.BluetoothTransport('54:ED:A3:00:48:23')
  client.conn, client.pipeline = wire, state
  wire.ack(0, crc=0)
  with pytest.raises(ValueError):
    client.exchange_batch(PAYLOAD, captured_ns=link[0].ns)
  assert client.conn is client.pipeline is None and wire.closed


@pytest.mark.parametrize('value', [None, 1.2, True, '10'])
def test_capture_timestamp_type_required(link, value):
  with pytest.raises(ValueError):
    link[2].update(PAYLOAD, captured_ns=value)


def test_future_and_expired_snapshots_rejected(link):
  clock, _, state = link
  for stamp in (clock.ns + 1, clock.ns - 500_000_000):
    with pytest.raises(ConnectionError, match='expired'):
      state.update(PAYLOAD, captured_ns=stamp)


def test_real_reader_timestamps_arrival_not_next_display_tick(link):
  clock, wire, _ = link
  left, right = socket.socketpair()
  state = pipe.SnapshotPipeline(left, clock.ns)
  try:
    state.update(PAYLOAD, captured_ns=clock.ns)
    right.settimeout(1)
    assert right.recv(32768)
    clock.ns += 150_000_000
    wire.ack(0)
    right.sendall(bytes(wire.input))
    deadline = time.monotonic() + 1
    while state.reader.frames.empty() and time.monotonic() < deadline:
      time.sleep(.001)
    assert not state.reader.frames.empty()
    clock.ns += 150_000_000
    response, received, elapsed = state.update(PAYLOAD, captured_ns=clock.ns)[0]
    assert received == 10.15 and elapsed == 150.
    assert response['navigationFeedback']['ageMs'] == 160.
    assert state.leases[-1][1] == 10_150_000_000
  finally:
    state.close()
    left.close()
    right.close()
  assert not state.reader.thread.is_alive()


def test_receiver_eof_propagates_and_thread_exits(link):
  clock, _, _ = link
  left, right = socket.socketpair()
  state = pipe.SnapshotPipeline(left, clock.ns)
  try:
    right.close()
    state.reader.thread.join(1)
    with pytest.raises(ConnectionError):
      state.update(PAYLOAD, captured_ns=clock.ns)
  finally:
    state.close()
    left.close()
