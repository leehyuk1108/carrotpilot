"""Bounded USB reattachment probe; it never writes display or navigation data."""
from __future__ import annotations

import subprocess
import threading


class UsbProbe:
  def __init__(self, factory):
    self.factory = factory
    self.lock = threading.Lock()
    self.thread = None
    self.ready = None
    self.stopped = False
    self.next_attempt = 0.0

  def poll(self, now):
    with self.lock:
      ready, self.ready = self.ready, None
      if ready is not None:
        return ready
      if self.stopped or (self.thread is not None and self.thread.is_alive()) or now < self.next_attempt:
        return None
      self.next_attempt = now + 2
      self.thread = threading.Thread(target=self._probe, name='navdy-usb-probe', daemon=True)
      self.thread.start()
    return None

  def _probe(self):
    candidate = self.factory()
    try:
      candidate.connect()
    except (OSError, ValueError, subprocess.SubprocessError):
      candidate.close()
      return
    with self.lock:
      if not self.stopped:
        self.ready = candidate
        return
    candidate.close()

  def close(self):
    with self.lock:
      self.stopped = True
      ready, self.ready = self.ready, None
    if ready is not None:
      ready.close()
    # An in-flight ADB discovery has bounded subprocess timeouts and closes its
    # own late result. Never block the display writer on USB discovery.
