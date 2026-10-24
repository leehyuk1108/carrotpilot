import json
import socket
import subprocess
from types import SimpleNamespace as NS

import pytest

from openpilot.selfdrive.navdy import bluetooth as bt
from openpilot.selfdrive.navdy.tests.test_bluetooth import PAYLOAD, PEER

OTHER_PEER = '02:00:00:00:00:02'


@pytest.fixture
def radio(monkeypatch):
  state = NS(now=100., paired=True, channel=3, command_fault=None, sdp_delay=0., hello_delay=0.,
             commands=[], sockets=[], faults=[], clients=[], sent=[], discoveries=[], before_hello=None)
  state.cache = bt.ServiceChannelCache()
  state.cache.configure(PEER, True)
  monkeypatch.setattr(bt.time, 'monotonic', lambda: state.now)
  monkeypatch.setattr(bt.time, 'monotonic_ns', lambda: round(state.now * 1e9))
  monkeypatch.setattr(socket, 'AF_BLUETOOTH', 31, raising=False)
  monkeypatch.setattr(socket, 'BTPROTO_RFCOMM', 3, raising=False)

  def command(args, **kwargs):
    state.commands.append(args)
    assert args[:2] in (['bluetoothctl', 'info'], ['sdptool', 'browse'])
    assert kwargs == {'capture_output': True, 'text': True, 'timeout': 3 if args[0] == 'bluetoothctl' else 5, 'check': True}
    if state.command_fault == args[0]:
      raise subprocess.TimeoutExpired(args, kwargs['timeout'], output='PRIVATE ' + PEER)
    if args[0] == 'bluetoothctl':
      return NS(stdout='Paired: yes' if state.paired else 'Paired: no')
    state.now += state.sdp_delay
    state.discoveries.append(state.now)
    return NS(stdout=f'Service Name: Navdy\n{bt.SERVICE_UUID}\nChannel: {state.channel}')

  class RadioSocket:
    def __init__(self, *args):
      assert args == (socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
      self.fault = state.faults.pop(0) if state.faults else {}
      self.hello = self.fault.get('hello', bt.HELLO.pack(b'NVH1', 0, bt.TTL_US, bt.MAX_BYTES))
      self.closed = False
      self.target = None
      self.security_requested = False
      self.security_checked = False
      self.timeouts = []
      state.sockets.append(self)

    def setsockopt(self, *args):
      assert args == (274, 4, bytes([2, 0]))
      if 'security_request' in self.fault:
        raise self.fault['security_request']
      self.security_requested = True

    def settimeout(self, value):
      self.timeouts.append(value)

    def connect(self, target):
      assert self.security_requested
      assert self.timeouts[-1] == 5
      self.target = target
      if 'connect' in self.fault:
        raise self.fault['connect']

    def getsockopt(self, *args):
      assert args == (274, 4, 2)
      self.security_checked = True
      return bytes([self.fault.get('security', 2), 0])

    def recv(self, size):
      assert self.security_checked and self.fault.get('security', 2) >= 2
      assert 0 < self.timeouts[-1] <= 2
      if state.before_hello is not None:
        state.before_hello()
      if 'recv' in self.fault:
        raise self.fault['recv']
      state.now += state.hello_delay
      result, self.hello = self.hello[:size], self.hello[size:]
      return result

    def sendall(self, data):
      state.sent.append(data)
      pytest.fail('Connect/reconnect must not send application data')

    def close(self):
      self.closed = True

  def client(peer=PEER):
    link = bt.BluetoothTransport(peer, command=command, socket_factory=RadioSocket, channel_cache=state.cache)
    state.clients.append(link)
    return link

  state.client = client
  yield state
  for link in state.clients:
    link.close()
  assert not state.sent


def connect_and_close(radio, peer=PEER):
  client = radio.client(peer)
  client.connect()
  client.close()
  return client


def command_names(radio):
  return [args[0] for args in radio.commands]


def test_cache_learned_after_uuid_and_encrypted_hello_only(radio):
  radio.sdp_delay, radio.hello_delay = 5., 1.

  def not_learned_yet():
    assert radio.cache.entry is None

  radio.before_hello = not_learned_yet
  initial = connect_and_close(radio)
  assert radio.cache.entry == (PEER, 3, 105.)
  assert next(step for step in initial.connect_diagnostics['steps'] if step['phase'] == 'sdp')['elapsedMs'] == 5000.
  radio.before_hello = None
  radio.now = 120.
  reconnect = connect_and_close(radio)
  assert reconnect.connect_diagnostics['cachedChannel'] is True
  assert radio.cache.entry == (PEER, 3, 105.)
  assert command_names(radio) == ['bluetoothctl', 'sdptool', 'bluetoothctl']
  assert all(sock.security_requested and sock.security_checked for sock in radio.sockets)
  assert all(sock.closed for sock in radio.sockets)


@pytest.mark.parametrize('age,cached', [(59.999, True), (60., False), (60.001, False), (-.001, False)])
def test_expiry_is_absolute_from_sdp_not_hello_or_reconnect(radio, age, cached):
  radio.hello_delay = 1.
  connect_and_close(radio)
  radio.now = 130.
  connect_and_close(radio)
  assert radio.cache.entry == (PEER, 3, 100.)
  radio.now = 100. + age
  result = connect_and_close(radio)
  assert result.connect_diagnostics['cachedChannel'] is cached
  assert command_names(radio).count('sdptool') == (1 if cached else 2)


def test_hello_finishing_after_cache_lifetime_does_not_seed_a_new_lifetime(radio):
  # Inject a delayed scheduler even though recv has its own two-second timeout.
  radio.hello_delay = 60.
  connect_and_close(radio)
  assert radio.cache.entry is None


@pytest.mark.parametrize('configuration', [(OTHER_PEER, True), (PEER, False), (None, True), (None, False)])
def test_config_rebinding_policy_and_disable_discard_previous_entry(radio, configuration):
  connect_and_close(radio)
  radio.cache.configure(*configuration)
  assert radio.cache.entry is None
  radio.cache.configure(PEER, True)
  connect_and_close(radio)
  assert command_names(radio).count('sdptool') == 2


def test_unchanged_configuration_preserves_hint_but_peer_lookup_cannot_reuse_it(radio):
  connect_and_close(radio)
  radio.cache.configure(PEER, True)
  assert radio.cache.get(PEER) == 3
  connect_and_close(radio, OTHER_PEER)
  assert radio.cache.entry == (OTHER_PEER, 3, 100.)
  connect_and_close(radio)
  assert command_names(radio).count('sdptool') == 3


@pytest.mark.parametrize('unavailable', [ConnectionRefusedError, TimeoutError, ConnectionResetError])
@pytest.mark.parametrize('new_channel', [3, 7])
def test_cached_unavailable_channel_rediscovers_once_after_pairing_recheck(radio, unavailable, new_channel):
  connect_and_close(radio)
  radio.now = 110.
  radio.channel = new_channel
  radio.faults.append({'connect': unavailable('PRIVATE ' + PEER)})
  radio.before_hello = lambda: pytest.fail('Cache was not discarded') if radio.cache.entry is not None else None
  link = connect_and_close(radio)
  assert command_names(radio) == ['bluetoothctl', 'sdptool', 'bluetoothctl', 'bluetoothctl', 'sdptool']
  assert [sock.target for sock in radio.sockets] == [(PEER, 3), (PEER, 3), (PEER, new_channel)]
  assert all(sock.closed for sock in radio.sockets)
  assert radio.cache.entry == (PEER, new_channel, 110.)
  failed = [step for step in link.connect_diagnostics['steps'] if not step['ok']]
  assert [step['phase'] for step in failed] == ['socket']


def test_cached_failure_then_rediscovered_failure_does_not_loop(radio):
  connect_and_close(radio)
  radio.faults = [{'connect': ConnectionRefusedError('unavailable')}] * 2
  link = radio.client()
  with pytest.raises(ConnectionRefusedError):
    link.connect()
  assert link.conn is None and radio.cache.entry is None
  assert command_names(radio).count('sdptool') == 2
  assert len(radio.sockets) == 3 and all(sock.closed for sock in radio.sockets)


@pytest.mark.parametrize('fault,phase', [
  ({'hello': bt.HELLO.pack(b'BAD!', 0, bt.TTL_US, bt.MAX_BYTES)}, 'hello'),
  ({'hello': bt.HELLO.pack(b'NVH1', 1, bt.TTL_US, bt.MAX_BYTES)}, 'hello'),
  ({'hello': bt.HELLO.pack(b'NVH1', 0, bt.TTL_US + 1, bt.MAX_BYTES)}, 'hello'),
  ({'hello': bt.HELLO.pack(b'NVH1', 0, bt.TTL_US, bt.MAX_BYTES + 1)}, 'hello'),
  ({'hello': b''}, 'hello'),
  ({'recv': TimeoutError('PRIVATE ' + PEER)}, 'hello'),
  ({'security': 1}, 'security'),
  ({'security_request': PermissionError('PRIVATE ' + PEER)}, 'security_request'),
  ({'connect': PermissionError('PRIVATE ' + PEER)}, 'socket'),
])
@pytest.mark.parametrize('seeded', [False, True])
def test_security_and_hello_fail_closed_without_immediate_rediscovery(radio, fault, phase, seeded):
  if seeded:
    connect_and_close(radio)
  radio.faults.append(fault)
  link = radio.client()
  with pytest.raises((OSError, ValueError)):
    link.connect()
  assert link.conn is None and radio.cache.entry is None
  assert command_names(radio).count('sdptool') == 1
  assert link.connect_diagnostics['steps'][-1]['phase'] == phase
  assert link.connect_diagnostics['steps'][-1]['ok'] is False
  with pytest.raises(ConnectionError, match='not connected'):
    link.exchange(PAYLOAD, captured_ns=round(radio.now * 1e9))
  assert all(sock.closed for sock in radio.sockets)
  assert PEER not in json.dumps(link.connect_diagnostics)
  connect_and_close(radio)
  assert command_names(radio).count('sdptool') == 2


def test_missing_pairing_invalidates_cache_without_socket_or_sdp(radio):
  connect_and_close(radio)
  radio.paired = False
  with pytest.raises(ConnectionError, match='not paired'):
    radio.client().connect()
  assert radio.cache.entry is None and len(radio.sockets) == 1
  assert command_names(radio) == ['bluetoothctl', 'sdptool', 'bluetoothctl']
  radio.paired = True
  connect_and_close(radio)
  assert command_names(radio).count('sdptool') == 2


@pytest.mark.parametrize('command,phase', [('bluetoothctl', 'bluetoothctl'), ('sdptool', 'sdp')])
@pytest.mark.parametrize('seeded', [False, True])
def test_command_failures_have_precise_safe_phase_and_no_cache(radio, command, phase, seeded):
  if seeded:
    connect_and_close(radio)
    if command == 'sdptool':
      radio.now += bt.CHANNEL_CACHE_SECONDS
  radio.command_fault = command
  link = radio.client()
  with pytest.raises(subprocess.TimeoutExpired):
    link.connect()
  assert radio.cache.entry is None and len(radio.sockets) == int(seeded)
  assert link.connect_diagnostics['steps'][-1] == {'phase': phase, 'ok': False, 'elapsedMs': 0.}
  assert PEER not in json.dumps(link.connect_diagnostics)


def test_wrong_service_uuid_cannot_seed_channel(radio, monkeypatch):
  monkeypatch.setattr(bt, 'SERVICE_UUID', 'unadvertised-service')
  link = radio.client()
  real_command = link.command

  def command(args, **kwargs):
    response = real_command(args, **kwargs)
    response.stdout = response.stdout.replace('unadvertised-service', 'other-uuid')
    return response

  link.command = command
  with pytest.raises(ConnectionError, match='not advertised'):
    link.connect()
  assert radio.cache.entry is None and not radio.sockets


def test_cache_is_not_shared_by_default_between_unrelated_transports(radio):
  first, second = bt.BluetoothTransport(PEER), bt.BluetoothTransport(PEER)
  assert first.channel_cache is not second.channel_cache
  assert first.channel_cache.entry is second.channel_cache.entry is None


def test_lost_pairing_during_cached_socket_failure_stops_rediscovery(radio):
  connect_and_close(radio)
  link = radio.client()
  original_connect = link._connect_socket
  radio.faults.append({'connect': ConnectionRefusedError('channel unavailable')})

  def connect(channel):
    radio.paired = False
    original_connect(channel)

  link._connect_socket = connect
  with pytest.raises(ConnectionError, match='not paired'):
    link.connect()
  assert radio.cache.entry is None and link.conn is None
  assert command_names(radio) == ['bluetoothctl', 'sdptool', 'bluetoothctl', 'bluetoothctl']


def test_v2_reconnect_revalidates_hello_before_creating_pipeline(radio, monkeypatch):
  from openpilot.selfdrive.navdy import bluetooth_pipeline
  pipelines = []

  def pipeline(conn, received_ns):
    assert not conn.hello and conn.security_checked
    assert received_ns == round(radio.now * 1e9)
    pipelines.append(conn)
    return NS(close=lambda: None)

  monkeypatch.setattr(bluetooth_pipeline, 'SnapshotPipeline', pipeline)
  radio.faults = [{'hello': bt.HELLO.pack(b'NVH2', 0, bt.TTL_US, bt.MAX_BYTES)}] * 2
  connect_and_close(radio)
  connect_and_close(radio)
  assert len(pipelines) == 2
  assert command_names(radio) == ['bluetoothctl', 'sdptool', 'bluetoothctl']


def run_real_wireless_bridge(radio, monkeypatch, tmp_path, *, config=None, exchange=None, duration=5.):
  from openpilot import cereal
  from openpilot.selfdrive.navdy import bridge
  from openpilot.selfdrive.navdy.tests.test_bridge import Messages

  sm = Messages()
  statuses, sent = [], []
  real_write = bridge.atomic_json

  def update(_):
    sm.recv_time = dict.fromkeys(bridge.SERVICES, radio.now)
    sm.logMonoTime = dict.fromkeys(bridge.SERVICES, round(radio.now * 1e9))

  def write(path, value):
    if path.name == 'status.json':
      statuses.append(value)
    real_write(path, value)

  def client(peer):
    link = radio.client(peer)
    # The bridge, not the test factory, must inject its shared process cache.
    link.channel_cache = bt.ServiceChannelCache()

    def exchange_batch(payload, *, captured_ns):
      assert link.conn is not None and not link.conn.hello and link.conn.security_checked
      assert captured_ns == round(radio.now * 1e9)
      sent.append(payload)
      if exchange is not None:
        exchange(link)
      return [({'navigationFeedback': None}, radio.now, 0.)]

    link.exchange_batch = exchange_batch
    return link

  class MissingUsb:
    def connect(self):
      raise ConnectionError('No USB')

    def close(self):
      pass

  sm.update = update
  monkeypatch.setattr(cereal, 'messaging', NS(SubMaster=lambda _: sm), raising=False)
  monkeypatch.setattr(bridge.time, 'sleep', lambda seconds: setattr(radio, 'now', round(radio.now + seconds, 9)))
  monkeypatch.setattr(bridge, 'DoorMonitor', lambda: NS(start=lambda: None, close=lambda: None,
                                                     door_open=False, gm=True, error=''))
  monkeypatch.setattr(bridge, 'read_override', lambda: None)
  monkeypatch.setattr(bridge, 'configured_connection', config or (lambda _: (PEER, True)))
  monkeypatch.setattr(bridge, 'BluetoothTransport', client)
  monkeypatch.setattr(bridge, 'jetson_candidates', list)
  monkeypatch.setattr(bridge, 'UsbProbe', lambda _: NS(poll=lambda _: None, close=lambda: None))
  monkeypatch.setattr(bridge, 'AdbTransport', MissingUsb)
  monkeypatch.setattr(bridge, 'atomic_json', write)
  bridge.run(feedback=tmp_path / 'feedback.json', status=tmp_path / 'status.json', duration=duration)
  assert all(sock.closed for sock in radio.sockets)
  return statuses, sent


@pytest.mark.parametrize('bluetooth_only', [False, True])
def test_bridge_reuses_cache_on_healthy_reconnect_in_both_transport_policies(radio, monkeypatch, tmp_path, bluetooth_only):
  def exchange(link):
    if link is radio.clients[0] and radio.now >= 102.6:
      raise ConnectionResetError('PRIVATE ' + PEER)

  statuses, sent = run_real_wireless_bridge(radio, monkeypatch, tmp_path, exchange=exchange,
                                          config=lambda _: (PEER, bluetooth_only))
  assert len(radio.clients) == 2
  assert radio.clients[0].channel_cache is radio.clients[1].channel_cache
  assert command_names(radio) == ['bluetoothctl', 'sdptool', 'bluetoothctl']
  failures = [row for row in statuses if 'retryInSeconds' in row]
  assert [row['retryInSeconds'] for row in failures] == [.2]
  assert statuses[-2]['bluetoothConnect']['cachedChannel'] is True
  assert sent and PEER not in json.dumps(statuses) and 'PRIVATE' not in json.dumps(statuses)


@pytest.mark.parametrize('change', ['peer', 'policy', 'disable', 'invalid'])
def test_bridge_config_change_drops_cache_before_next_transport(radio, monkeypatch, tmp_path, change):
  def config(_):
    if 105. <= radio.now < 110.:
      if change == 'invalid':
        raise ValueError('invalid config')
      return {'peer': (OTHER_PEER, True), 'policy': (PEER, False), 'disable': (None, True)}[change]
    return PEER, True

  statuses, _ = run_real_wireless_bridge(radio, monkeypatch, tmp_path, config=config, duration=14.)
  assert command_names(radio).count('sdptool') == (3 if change in ('peer', 'policy') else 2)
  assert all(not client.connect_diagnostics['cachedChannel'] for client in radio.clients)
  assert statuses[-2]['connected'] is True


@pytest.mark.parametrize('phase', ['bluetoothctl', 'sdp', 'socket', 'hello', 'security'])
def test_bridge_status_keeps_precise_failure_phase_without_private_command_data(radio, monkeypatch, tmp_path, phase):
  if phase in ('bluetoothctl', 'sdp'):
    radio.command_fault = 'sdptool' if phase == 'sdp' else phase
  else:
    radio.faults.append({'socket': {'connect': ConnectionRefusedError('PRIVATE ' + PEER)},
                         'hello': {'recv': TimeoutError('PRIVATE ' + PEER)},
                         'security': {'security': 1}}[phase])
  statuses, sent = run_real_wireless_bridge(radio, monkeypatch, tmp_path, duration=1.)
  failure = next(row for row in statuses if 'retryInSeconds' in row)
  assert failure['bluetoothConnect']['steps'][-1]['phase'] == phase
  assert failure['bluetoothConnect']['steps'][-1]['ok'] is False
  assert failure['retryInSeconds'] == 2.
  assert not sent
  assert PEER not in json.dumps(statuses) and 'PRIVATE' not in json.dumps(statuses)
