"""
wkrt/hooks.py — Icecast listener event webhook handler

Icecast calls these URLs on listener connect/disconnect:

    <on-connect>http://127.0.0.1:8765/connect</on-connect>
    <on-disconnect>http://127.0.0.1:8765/disconnect</on-disconnect>

Runs as a tiny HTTP server alongside the main engine.
"""

import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Callable
from urllib.parse import urlparse

log = logging.getLogger(__name__)


class _HookHandler(BaseHTTPRequestHandler):

    on_connect: Callable = None
    on_disconnect: Callable = None

    def do_GET(self):
        path = urlparse(self.path).path  # strip any query params Icecast may append
        if path == "/connect":
            log.info("Icecast: listener connected (webhook)")
            if self.on_connect:
                self.on_connect()
            self._ok()
        elif path == "/disconnect":
            log.info("Icecast: listener disconnected (webhook)")
            if self.on_disconnect:
                self.on_disconnect()
            self._ok()
        elif path == "/health":
            self._ok()
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        self.do_GET()  # Icecast may POST in some configurations

    def _ok(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, fmt, *args):
        # Suppress default HTTP server logging — we handle it ourselves
        pass


class HookServer:
    """
    Tiny HTTP server that receives Icecast listener events.

    Usage:
        hooks = HookServer(
            on_connect=cache.on_listener_connect,
            on_disconnect=cache.on_listener_disconnect,
        )
        hooks.start()
    """

    def __init__(
        self,
        on_connect: Callable,
        on_disconnect: Callable,
        host: str = "127.0.0.1",
        port: int = 8765,
    ):
        self.host = host
        self.port = port

        # Inject callbacks into handler class
        _HookHandler.on_connect = staticmethod(on_connect)
        _HookHandler.on_disconnect = staticmethod(on_disconnect)

        self._server = HTTPServer((host, port), _HookHandler)
        self._thread: threading.Thread | None = None

    def start(self):
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            daemon=True,
            name="hook-server",
        )
        self._thread.start()
        log.info(f"Hook server listening on {self.host}:{self.port}")

    def stop(self):
        self._server.shutdown()
