import json
import socket
from types import SimpleNamespace

import pytest

from openpilot.selfdrive.navdy.transport import AdbTransport, LineReader, MAX_FRAME_BYTES, encode_frame


class FragmentSocket:
  def __init__(self, chunks):
    self.chunks = iter(chunks)

  def recv(self, size):
    return next(self.chunks, b'')


def test_fragmented_utf8_and_multiple_frames():
  frame = encode_frame({'navigationFeedback': {'title': '서울역'}})
  reader = LineReader()
  sock = FragmentSocket([frame[:4], frame[4:20], frame[20:] + b'{"roadPreview":null}\n'])
  assert reader.receive(sock)['navigationFeedback']['title'] == '서울역'
  assert reader.receive(sock) == {'roadPreview': None}


@pytest.mark.parametrize('frame', [b'[]\n', b'{"other":1}\n', b'{', b'x' * (MAX_FRAME_BYTES + 1)])
def test_invalid_response(frame):
  with pytest.raises((ValueError, ConnectionError)):
    LineReader().receive(FragmentSocket([frame]))


def test_socket_timeout_propagates():
  left, right = socket.socketpair()
  try:
    left.settimeout(0.01)
    right.sendall(b'{"roadPreview":')
    with pytest.raises(TimeoutError):
      LineReader().receive(left)
  finally:
    left.close()
    right.close()


def test_maximum_frame_followed_by_another_frame_is_not_oversized():
  frame = encode_frame({'roadPreview': 'x' * (MAX_FRAME_BYTES - len(encode_frame({'roadPreview': ''})))})
  assert len(frame) == MAX_FRAME_BYTES
  reader = LineReader()
  socket = FragmentSocket([frame[:-10], frame[-10:] + b'{"roadPreview":null}\n'])
  assert len(reader.receive(socket)['roadPreview']) > 32000
  assert reader.receive(socket) == {'roadPreview': None}


def test_extreme_nesting_is_a_recoverable_protocol_error():
  with pytest.raises(ValueError, match='nesting'):
    LineReader().receive(FragmentSocket([b'{"roadPreview":' + b'[' * 2000 + b'0' + b']' * 2000 + b'}\n']))


def test_frame_bound_and_nan():
  with pytest.raises(ValueError):
    encode_frame({'title': 'a' * MAX_FRAME_BYTES})
  with pytest.raises(ValueError):
    encode_frame({'speed': float('nan')})
  assert json.loads(encode_frame({'title': '이등병의 편지'}))['title'] == '이등병의 편지'


def test_discover_ignores_phones_unauthorized_and_network_adb():
  calls = []

  def run(cmd, **kwargs):
    calls.append(cmd)
    if cmd[-2:] == ['devices', '-l']:
      result = 'phone device\nnavdy device\nother unauthorized\n192.168.1.2:5555 device\n'
    else:
      result = 'package:/data/app/com.navdy.hud.app/base.apk' if 'navdy' in cmd else ''
    return SimpleNamespace(stdout=result)

  assert AdbTransport(adb='adb', command=run).discover() == 'navdy'
  assert all(cmd[1:3] == ['-P', '5038'] for cmd in calls)
  assert len(calls) == 3


def test_multiple_targets_fail_closed():
  def run(cmd, **kwargs):
    return SimpleNamespace(stdout='a device\nb device\n' if cmd[-2:] == ['devices', '-l'] else 'package:/base.apk')
  with pytest.raises(ConnectionError, match='Multiple'):
    AdbTransport(adb='adb', command=run).discover()
