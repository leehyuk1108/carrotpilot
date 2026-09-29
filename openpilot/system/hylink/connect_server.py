"""Offroad key page on port 1108; restricted to the same physical LAN subnet."""
import ipaddress
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import psutil

from openpilot.common.params import Params
from openpilot.system.hylink import health, pairing


def valid_host(host):
  try:
    parsed = urlsplit("http://" + host)
    return (parsed.port == 1108 and not parsed.username and not parsed.password
            and not parsed.path and ipaddress.ip_address(parsed.hostname).is_private)
  except (ValueError, TypeError):
    return False


def same_lan(host, peer, interfaces=None):
  """Require the actual Wi-Fi/Ethernet subnet; no proxy headers, VPN or DNS hosts."""
  if not valid_host(host):
    return False
  target = urlsplit("http://" + host).hostname
  try:
    client = ipaddress.ip_address(peer)
    for name, addresses in (interfaces if interfaces is not None else psutil.net_if_addrs()).items():
      if not name.startswith(("wlan", "eth", "en", "ap", "usb", "lo")):
        continue
      for addr in addresses:
        if addr.family == socket.AF_INET and addr.address == target and addr.netmask:
          network = ipaddress.ip_network(addr.address + "/" + addr.netmask, strict=False)
          if client in network and client.is_private:
            return True
  except (ValueError, TypeError):
    pass
  return False


class ConnectServer(ThreadingHTTPServer):
  daemon_threads = True
  request_queue_size = 4
  allow_reuse_address = True

  def __init__(self, address, params):
    super().__init__(address, Handler)
    self.params = params
    self.pair_lock = threading.Lock()
    self.slots = threading.BoundedSemaphore(6)

  def process_request(self, request, client_address):
    if not self.slots.acquire(blocking=False):
      self.shutdown_request(request)
      return
    try:
      super().process_request(request, client_address)
    except BaseException:
      self.slots.release()
      raise

  def process_request_thread(self, request, client_address):
    try:
      super().process_request_thread(request, client_address)
    finally:
      self.slots.release()


class Handler(BaseHTTPRequestHandler):
  server_version = "Hylink"
  sys_version = ""

  def setup(self):
    super().setup()
    self.connection.settimeout(3)

  def log_message(self, *args):
    pass  # Never log codes, keys or request bodies.

  def reply(self, code, body, kind="application/json; charset=utf-8"):
    data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
    self.send_response(code)
    self.send_header("Content-Type", kind)
    self.send_header("Content-Length", str(len(data)))
    self.send_header("Cache-Control", "no-store")
    self.send_header("Referrer-Policy", "no-referrer")
    self.send_header("X-Content-Type-Options", "nosniff")
    self.send_header("X-Frame-Options", "DENY")
    policy = "; ".join(("default-src 'none'", "script-src 'self'", "style-src 'self'", "connect-src 'self'",
                        "form-action 'none'", "frame-ancestors 'none'", "base-uri 'none'"))
    self.send_header("Content-Security-Policy", policy)
    self.end_headers()
    self.wfile.write(data)

  def do_GET(self):
    if not same_lan(self.headers.get("Host", ""), self.client_address[0]):
      return self.reply(403, {"error": "Use the comma local IP address and port 1108."})
    if not pairing.page_allowed(self.server.params):
      return self.reply(409, {"error": "키 페이지는 시동이 꺼진 오프로드 상태에서만 열립니다."})
    if self.path in ("/api/key", "/api/health"):
      if self.headers.get("X-Hylink-Request") != "1":
        return self.reply(403, {"error": "Open the local key page."})
      try:
        if self.path == "/api/health":
          return self.reply(200, health.snapshot(self.server.params))
        return self.reply(200, pairing.status(self.server.params))
      except ValueError as exc:
        return self.reply(409, {"error": str(exc)})
    assets = {"/": ("connect.html", "text/html"), "/connect.js": ("connect.js", "text/javascript"),
              "/connect.css": ("connect.css", "text/css")}
    if self.path not in assets:
      return self.reply(404, {"error": "Not found"})
    name, kind = assets[self.path]
    self.reply(200, Path(__file__).with_name(name).read_bytes(), kind + "; charset=utf-8")

  def do_POST(self):
    host = self.headers.get("Host", "")
    if (not same_lan(host, self.client_address[0]) or self.path != "/api/connect" or self.headers.get("Origin") != "http://" + host
        or self.headers.get("X-Hylink-Request") != "1" or self.headers.get("Content-Type") != "application/json"):
      return self.reply(403, {"error": "이 페이지에서 다시 연결해 주세요."})
    try:
      length = int(self.headers.get("Content-Length", "0"))
      if not 1 <= length <= 2048:
        return self.reply(413, {"error": "Invalid request size"})
      body = json.loads(self.rfile.read(length))
      if not isinstance(body, dict):
        raise ValueError("Invalid request")
      if not self.server.pair_lock.acquire(blocking=False):
        return self.reply(409, {"error": "연결 처리 중이에요. 잠시 후 다시 시도해 주세요."})
      try:
        result = pairing.connect(self.server.params, body)
      finally:
        self.server.pair_lock.release()
      self.reply(200, result)
    except ValueError as exc:
      self.reply(400, {"error": str(exc)})
    except Exception as exc:
      info = health.failure(exc, "enrollment")
      _, title, detail, action = health.MESSAGES[info["code"]]
      self.reply(503, {"error": f"{title}. {detail} {action} 기존 연결 키는 변경하지 않았어요.", "diagnostic": info})


def main():
  with ConnectServer(("0.0.0.0", 1108), Params()) as server:
    server.serve_forever(poll_interval=0.5)


if __name__ == "__main__":
  main()
