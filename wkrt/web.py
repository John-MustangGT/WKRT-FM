"""
wkrt/web.py — Station web UI and status API.

Routes:
  GET  /              → index.html (listener view)
  GET  /admin         → admin.html (DJ/queue control)
  GET  /api/status    → JSON station state
  GET  /api/library   → JSON artist/track library
  POST /api/dj/override        body: {"name": "Neon"}  — force a DJ
  DELETE /api/dj/override      — restore time-based rotation
  POST /api/queue/next         body: {"artist":…, "title":…, "year":…}
"""
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

log = logging.getLogger(__name__)

_TEMPLATE_DIR = Path(__file__).parent.parent / "templates"


class _Handler(BaseHTTPRequestHandler):
    state = None
    engine = None

    # ── GET ──────────────────────────────────────────────────────────────────

    def do_GET(self):
        if self.path == "/":
            self._serve_file(_TEMPLATE_DIR / "index.html", "text/html; charset=utf-8")
        elif self.path == "/admin":
            self._serve_file(_TEMPLATE_DIR / "admin.html", "text/html; charset=utf-8")
        elif self.path == "/api/status":
            data = json.dumps(self.state.to_dict() if self.state else {})
            self._respond(200, "application/json", data.encode())
        elif self.path == "/api/library":
            if self.engine:
                data = json.dumps(self.engine.get_library_for_api())
            else:
                data = "[]"
            self._respond(200, "application/json", data.encode())
        else:
            self._respond(404, "text/plain", b"Not found")

    # ── POST ─────────────────────────────────────────────────────────────────

    def do_POST(self):
        body = self._read_body()

        if self.path == "/api/dj/override":
            if not self.engine:
                return self._respond(503, "text/plain", b"Engine not available")
            try:
                name = json.loads(body).get("name") if body else None
            except (ValueError, AttributeError):
                return self._respond(400, "text/plain", b"Invalid JSON")
            if name and name not in [d["name"] for d in self.engine._dj_configs]:
                return self._respond(400, "text/plain", b"Unknown DJ name")
            self.engine.set_dj_override(name or None)
            self._respond(200, "application/json", b'{"ok":true}')

        elif self.path == "/api/queue/next":
            if not self.engine:
                return self._respond(503, "text/plain", b"Engine not available")
            try:
                req = json.loads(body)
                artist = req["artist"]
                title = req["title"]
                year = int(req["year"])
            except (ValueError, KeyError, TypeError):
                return self._respond(400, "text/plain", b"Invalid JSON - need artist, title, year")
            track = self.engine.find_track(artist, title, year)
            if track is None:
                return self._respond(404, "text/plain", b"Track not found in library")
            self.engine.force_next_track(track)
            self._respond(200, "application/json", b'{"ok":true}')

        else:
            self._respond(404, "text/plain", b"Not found")

    # ── DELETE ────────────────────────────────────────────────────────────────

    def do_DELETE(self):
        if self.path == "/api/dj/override":
            if not self.engine:
                return self._respond(503, "text/plain", b"Engine not available")
            self.engine.set_dj_override(None)
            self._respond(200, "application/json", b'{"ok":true}')
        else:
            self._respond(404, "text/plain", b"Not found")

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(length) if length else b""

    def _serve_file(self, path: Path, content_type: str):
        try:
            body = path.read_bytes()
            self._respond(200, content_type, body)
        except FileNotFoundError:
            self._respond(404, "text/plain", f"Template not found: {path.name}".encode())

    def _respond(self, code: int, content_type: str, body: bytes):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass  # suppress per-request logging


class WebServer:
    def __init__(self, state, engine=None, host: str = "0.0.0.0", port: int = 8080):
        _Handler.state = state
        _Handler.engine = engine
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
