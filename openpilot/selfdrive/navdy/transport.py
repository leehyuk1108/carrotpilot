"""USB ADB socket transport. Also runnable on a paired Jetson over SSH stdio."""
from __future__ import annotations

import argparse
import json
import re
import shutil
import socket
import subprocess
import sys

MAX_FRAME_BYTES = 32768
ADB_PORT = 5038
SERVICE = 'com.navdy.hud.app/.openpilot.OpenpilotStateService'


def encode_frame(payload):
  encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8') + b'\n'
  if len(encoded) > MAX_FRAME_BYTES:
    raise ValueError('Navdy frame exceeds 32 KiB')
  return encoded


class LineReader:
  def __init__(self):
    self.buffer = b''

  def receive(self, conn):
    while b'\n' not in self.buffer:
      chunk = conn.recv(min(4096, MAX_FRAME_BYTES + 1 - len(self.buffer)))
      if not chunk:
        raise ConnectionError('Navdy closed its socket')
      self.buffer += chunk
      boundary = self.buffer.find(b'\n')
      if (boundary < 0 and len(self.buffer) > MAX_FRAME_BYTES) or boundary >= MAX_FRAME_BYTES:
        raise ValueError('Oversized Navdy response')
    line, self.buffer = self.buffer.split(b'\n', 1)
    try:
      value = json.loads(line)
    except RecursionError as error:
      raise ValueError('Navdy response nesting is too deep') from error
    if not isinstance(value, dict):
      raise ValueError('Navdy response is not an object')
    pending = [(value, 0)]
    while pending:
      item, depth = pending.pop()
      if depth > 32:
        raise ValueError('Navdy response nesting is too deep')
      if isinstance(item, dict):
        pending.extend((child, depth + 1) for child in item.values())
      elif isinstance(item, list):
        pending.extend((child, depth + 1) for child in item)
    if not any(key in value for key in ('roadPreview', 'navigationFeedback', 'cameraSpeedKph')):
      raise ValueError('Navdy service returned an unsupported response')
    return value


class AdbTransport:
  def __init__(self, adb=None, command=subprocess.run):
    self.adb = adb or shutil.which('adb')
    self.command = command
    self.serial = None
    self.port = None
    self.conn = None
    self.reader = LineReader()

  def run(self, args, *, serial=None):
    if not self.adb:
      raise FileNotFoundError('ADB is not installed')
    target = serial or self.serial
    cmd = [self.adb, '-P', str(ADB_PORT)]
    if target:
      cmd += ['-s', target]
    result = self.command(cmd + args, capture_output=True, text=True, timeout=3, check=True)
    return result.stdout.strip()

  def discover(self):
    candidates = []
    for line in self.run(['devices', '-l']).splitlines():
      fields = line.split()
      if len(fields) < 2 or fields[1] != 'device' or not re.fullmatch(r'[A-Za-z0-9_.-]{1,128}', fields[0]):
        continue
      # Verify the installed HUD package; never start services on an arbitrary phone.
      try:
        package = self.run(['shell', 'pm', 'path', 'com.navdy.hud.app'], serial=fields[0])
        if package.startswith('package:'):
          candidates.append(fields[0])
      except (OSError, subprocess.SubprocessError):
        continue
    if len(candidates) != 1:
      raise ConnectionError('Navdy USB not found' if not candidates else 'Multiple Navdy USB devices')
    return candidates[0]

  def connect(self):
    self.serial = self.discover()
    try:
      # Allocate our own loopback port, without replacing another bridge's mapping.
      port = self.run(['forward', 'tcp:0', 'tcp:8765'])
      if not port.isdigit() or not 1024 <= int(port) <= 65535:
        raise ConnectionError('ADB did not allocate a Navdy forward port')
      self.port = int(port)
      result = self.run(['shell', 'am', 'startservice', '-n', SERVICE])
      if 'Error' in result or 'Exception' in result:
        raise ConnectionError('Navdy service failed to start')
      self.conn = socket.create_connection(('127.0.0.1', self.port), timeout=0.8)
      self.conn.settimeout(0.8)
      self.reader = LineReader()
    except Exception:
      self.close()
      raise

  def exchange(self, payload):
    if self.conn is None:
      self.connect()
    self.conn.sendall(encode_frame(payload))
    return self.reader.receive(self.conn)

  def close(self):
    if self.conn is not None:
      self.conn.close()
      self.conn = None
    if self.port is not None:
      try:
        self.run(['forward', '--remove', f'tcp:{self.port}'])
      except (OSError, subprocess.SubprocessError):
        pass
    self.port = None
    self.serial = None


def relay_stdio():
  """No shell, privileged USB reconfiguration, Bluetooth reset or vehicle writes."""
  transport = AdbTransport()
  try:
    while True:
      line = sys.stdin.buffer.readline(MAX_FRAME_BYTES + 1)
      if not line:
        return
      if len(line) > MAX_FRAME_BYTES or not line.endswith(b'\n'):
        raise ValueError('Invalid relay frame')
      payload = json.loads(line)
      if not isinstance(payload, dict) or payload.get('schema') != 'navdy.openpilot.v1':
        raise ValueError('Unsupported display payload')
      sys.stdout.buffer.write(encode_frame(transport.exchange(payload)))
      sys.stdout.buffer.flush()
  finally:
    transport.close()


if __name__ == '__main__':
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument('--stdio', action='store_true')
  if parser.parse_args().stdio:
    relay_stdio()
