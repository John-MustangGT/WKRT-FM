"""
wkrt/web.py — Station web UI and status API.

Serves the control page at / and JSON status at /api/status.
No extra dependencies — uses stdlib http.server.
"""
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

log = logging.getLogger(__name__)

_HTML_PATH = Path(__file__).parent.parent / "templates" / "index.html"


class _Handler(BaseHTTPRequestHandler):
    state = None

    def do_GET(self):
        if self.path == "/":
            try:
                html = _HTML_PATH.read_text()
            except FileNotFoundError:
                html = "<h1>Template not found</h1>"
            self._respond(200, "text/html; charset=utf-8", html.encode())

        elif self.path == "/api/status":
            data = json.dumps(self.state.to_dict() if self.state else {})
            self._respond(200, "application/json", data.encode())

        else:
            self._respond(404, "text/plain", b"Not found")

    def _respond(self, code, content_type, body):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass  # suppress per-request logging


class WebServer:
    def __init__(self, state, host: str = "0.0.0.0", port: int = 8080):
        _Handler.state = state
        self._server = HTTPServer((host, port), _Handler)
        self._port = port

    def start(self):
        t = threading.Thread(
            target=self._server.serve_forever,
            daemon=True,
            name="web-server",
        )
        t.start()
        log.info(f"Web UI → http://0.0.0.0:{self._port}/")
