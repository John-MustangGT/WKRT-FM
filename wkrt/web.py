"""
wkrt/web.py — Station web UI and status API.

Routes:
  GET  /              → index.html (listener view)
  GET  /admin         → admin.html (DJ/queue control)  [auth required]
  GET  /api/status    → JSON station state
  GET  /api/library   → JSON artist/track library
  GET  /api/library/state      → {last_ingest, last_regen} timestamps  [auth required]
  POST /api/dj/override        body: {"name": "Neon"}  [auth required]
  DELETE /api/dj/override      [auth required]
  POST /api/dj/restart         [auth required]
  POST /api/queue/next         body: {"artist":…, "title":…, "year":…}  [auth required]
  GET  /api/listeners          → JSON list of Icecast clients  [auth required]
  POST /api/listeners/kick     body: {"id": "5"}  [auth required]
  POST /api/library/ingest     body: {"paths": [...]}  [auth required]
  POST /api/context            body: {"text": "…", "one_shot": false}  [auth required]
  GET  /api/targets            → JSON list of streaming target statuses  [auth required]
  POST /api/targets/{idx}/enable    [auth required]
  POST /api/targets/{idx}/disable   [auth required]
  POST /api/targets/{idx}/restart   [auth required]
  GET  /api/favorites/user          → user favorites list  [auth required]
  POST /api/favorites/user/add      body: {artist, title, year}  [auth required]
  POST /api/favorites/user/remove   body: {artist, title}  [auth required]
  GET  /api/favorites/dj/{name}     → DJ favorites by slot  [auth required]
  POST /api/favorites/dj/{name}/regenerate  [auth required]
  GET  /api/track              → full track detail (id3, annotation, history, art)  [auth required]
"""
import base64
import json
import logging
import re
import threading
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

log = logging.getLogger(__name__)

_TEMPLATE_DIR = Path(__file__).parent.parent / "templates"


class _Handler(BaseHTTPRequestHandler):
    state = None
    engine = None
    _admin_password = ""   # empty = no auth required
    _ice_cfg: dict = {}

    # ── Auth ─────────────────────────────────────────────────────────────────

    def _require_admin(self) -> bool:
        pw = self.__class__._admin_password
        if not pw:
            return True
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Basic "):
            try:
                creds = base64.b64decode(auth[6:]).decode()
                _, given = creds.split(":", 1)
                if given == pw:
                    return True
            except Exception:
                pass
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="WKRT Admin"')
        self.send_header("Content-Type", "text/plain")
        body = b"Unauthorized"
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)
        return False

    # ── GET ──────────────────────────────────────────────────────────────────

    def do_GET(self):
        if self.path == "/":
            self._serve_file(_TEMPLATE_DIR / "index.html", "text/html; charset=utf-8")
        elif self.path == "/admin":
            if not self._require_admin():
                return
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
        elif self.path == "/api/library/state":
            if not self._require_admin():
                return
            state = self.engine._programmer.load_library_state() if self.engine else {}
            self._respond(200, "application/json", json.dumps(state).encode())
        elif self.path == "/api/listeners":
            if not self._require_admin():
                return
            clients = self._icecast_list_clients()
            self._respond(200, "application/json", json.dumps(clients).encode())
        elif self.path == "/api/targets":
            if not self._require_admin():
                return
            statuses = self.engine.target_statuses() if self.engine else []
            self._respond(200, "application/json", json.dumps(statuses).encode())

        elif self.path == "/api/favorites/user":
            if not self._require_admin():
                return
            favs = self.engine._programmer.load_user_favorites() if self.engine else []
            self._respond(200, "application/json", json.dumps(favs).encode())

        elif self.path.startswith("/api/track"):
            qs = parse_qs(urlparse(self.path).query)
            artist = (qs.get("artist") or [""])[0]
            title  = (qs.get("title") or [""])[0]
            if not artist or not title:
                return self._respond(400, "text/plain", b"Need artist and title params")
            data = self._track_detail(artist, title)
            data.pop("file_path", None)   # never expose server path to clients
            self._respond(200, "application/json", json.dumps(data).encode())

        else:
            m = re.match(r'^/api/favorites/dj/([^/]+)$', self.path)
            if m and self.engine:
                if not self._require_admin():
                    return
                name = m.group(1)
                data = self.engine._programmer.load_dj_favorites(name)
                self._respond(200, "application/json", json.dumps(data).encode())
            else:
                self._respond(404, "text/plain", b"Not found")

    # ── POST ─────────────────────────────────────────────────────────────────

    def do_POST(self):
        if not self._require_admin():
            return
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

        elif self.path == "/api/listeners/kick":
            try:
                client_id = str(json.loads(body)["id"])
            except (ValueError, KeyError, TypeError):
                return self._respond(400, "text/plain", b"Invalid JSON - need id")
            ok = self._icecast_kick_client(client_id)
            if ok:
                self._respond(200, "application/json", b'{"ok":true}')
            else:
                self._respond(502, "text/plain", b"Icecast kick failed")

        elif self.path == "/api/library/ingest":
            if not self.engine:
                return self._respond(503, "text/plain", b"Engine not available")
            try:
                paths = json.loads(body).get("paths", [])
            except (ValueError, AttributeError):
                return self._respond(400, "text/plain", b"Invalid JSON - need paths array")
            added = self.engine.ingest_tracks(paths)
            data = json.dumps({"ok": True, "ingested": len(added),
                               "tracks": [t.display for t in added]}).encode()
            self._respond(200, "application/json", data)

        elif self.path == "/api/dj/restart":
            if not self.engine:
                return self._respond(503, "text/plain", b"Engine not available")
            self.engine.force_dj_break()
            self._respond(200, "application/json", b'{"ok":true}')

        elif self.path == "/api/context":
            try:
                req = json.loads(body)
                text = str(req.get("text", "")).strip()
                one_shot = bool(req.get("one_shot", False))
            except (ValueError, AttributeError):
                return self._respond(400, "text/plain", b"Invalid JSON - need text")
            self.state.set_live_context(text, one_shot)
            self._respond(200, "application/json", b'{"ok":true}')

        elif self.path == "/api/favorites/user/add":
            if not self.engine:
                return self._respond(503, "text/plain", b"Engine not available")
            try:
                req    = json.loads(body)
                artist = str(req["artist"])
                title  = str(req["title"])
                year   = int(req["year"])
            except (ValueError, KeyError, TypeError):
                return self._respond(400, "text/plain", b"Invalid JSON - need artist, title, year")
            self.engine._programmer.add_user_favorite(artist, title, year)
            self._respond(200, "application/json", b'{"ok":true}')

        elif self.path == "/api/favorites/user/remove":
            if not self.engine:
                return self._respond(503, "text/plain", b"Engine not available")
            try:
                req    = json.loads(body)
                artist = str(req["artist"])
                title  = str(req["title"])
            except (ValueError, KeyError, TypeError):
                return self._respond(400, "text/plain", b"Invalid JSON - need artist, title")
            self.engine._programmer.remove_user_favorite(artist, title)
            self._respond(200, "application/json", b'{"ok":true}')

        else:
            m = re.match(r'^/api/favorites/dj/([^/]+)/regenerate$', self.path)
            if m and self.engine:
                if not self._require_admin():
                    return
                self.engine.regenerate_dj_favorites(m.group(1))
                self._respond(200, "application/json", b'{"ok":true}')
                return

            m = re.match(r'^/api/targets/(\d+)/(enable|disable|restart)$', self.path)
            if m and self.engine:
                idx = int(m.group(1))
                action = m.group(2)
                if idx >= len(self.engine._targets):
                    return self._respond(404, "text/plain", b"Target index out of range")
                if action == "enable":
                    self.engine.enable_target(idx)
                elif action == "disable":
                    self.engine.disable_target(idx)
                elif action == "restart":
                    self.engine.restart_target(idx)
                self._respond(200, "application/json", b'{"ok":true}')
            else:
                self._respond(404, "text/plain", b"Not found")

    # ── DELETE ────────────────────────────────────────────────────────────────

    def do_DELETE(self):
        if not self._require_admin():
            return
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

    # ── Icecast admin helpers ─────────────────────────────────────────────────

    def _icecast_list_clients(self) -> list:
        ice = self.__class__._ice_cfg
        if not ice:
            return []
        host = ice.get("host", "localhost")
        port = ice.get("port", 8000)
        mount = ice.get("mount", "/wkrt")
        pw = ice.get("admin_password", "hackme")
        url = f"http://{host}:{port}/admin/listclients?mount={mount}"
        creds = base64.b64encode(f"admin:{pw}".encode()).decode()
        try:
            req = Request(url, headers={"Authorization": f"Basic {creds}"})
            with urlopen(req, timeout=3) as resp:
                xml_data = resp.read()
            root = ET.fromstring(xml_data)
            clients = []
            for listener in root.findall(".//listener"):
                secs = int(listener.findtext("Connected", "0") or 0)
                clients.append({
                    "id": listener.findtext("ID", ""),
                    "ip": listener.findtext("IP", ""),
                    "useragent": listener.findtext("UserAgent", ""),
                    "connected_seconds": secs,
                })
            return clients
        except Exception as e:
            log.debug(f"Icecast listclients failed: {e}")
            return []

    def _icecast_kick_client(self, client_id: str) -> bool:
        ice = self.__class__._ice_cfg
        if not ice:
            return False
        host = ice.get("host", "localhost")
        port = ice.get("port", 8000)
        mount = ice.get("mount", "/wkrt")
        pw = ice.get("admin_password", "hackme")
        url = f"http://{host}:{port}/admin/killclient?mount={mount}&id={client_id}"
        creds = base64.b64encode(f"admin:{pw}".encode()).decode()
        try:
            req = Request(url, headers={"Authorization": f"Basic {creds}"})
            with urlopen(req, timeout=3):
                return True
        except Exception as e:
            log.debug(f"Icecast killclient failed: {e}")
            return False

    # ── Track detail ──────────────────────────────────────────────────────────

    def _track_detail(self, artist: str, title: str) -> dict:
        engine = self.__class__.engine
        result: dict = {"artist": artist, "title": title}

        # Find track in library for path + year
        track = None
        for tracks in (engine._library if engine else {}).values():
            for t in tracks:
                if t.artist == artist and t.title == title:
                    track = t
                    break
            if track:
                break

        if track:
            result["year"] = track.year
            result["file_path"] = str(track.path)
            result["id3"]      = self._read_id3(track.path)
            result["album_art"] = self._extract_art(track.path)

        # MusicBrainz annotation
        ann = engine._annotator.load(artist, title) if engine else None
        result["annotation"] = ann or {}

        # Cover Art Archive fallback if no embedded art
        if not result.get("album_art") and ann and ann.get("release_mbid"):
            result["album_art"] = {
                "source": "coverartarchive",
                "url": f"https://coverartarchive.org/release/{ann['release_mbid']}/front",
            }

        # Play history
        result["history"] = engine._history.load(artist, title) if engine else {}

        return result

    def _read_id3(self, path) -> dict:
        try:
            from mutagen import File as MFile
            audio = MFile(path, easy=True)
            if not audio:
                return {}
            tags: dict = {}
            for key in ("title", "artist", "album", "date", "genre", "tracknumber"):
                val = (audio.tags or {}).get(key)
                if val:
                    tags[key] = str(val[0])
            if hasattr(audio, "info") and hasattr(audio.info, "length"):
                tags["duration_seconds"] = round(audio.info.length, 1)
            return tags
        except Exception as e:
            log.debug(f"ID3 read failed for {path}: {e}")
            return {}

    def _extract_art(self, path) -> dict:
        try:
            from mutagen import File as MFile
            audio = MFile(path)
            if not audio or not audio.tags:
                return {}
            tags = audio.tags
            # MP3 — APIC frame
            for key in list(tags.keys()):
                if str(key).startswith("APIC"):
                    apic = tags[key]
                    if len(apic.data) <= 300_000:
                        return {
                            "source": "id3",
                            "mime": apic.mime,
                            "data": base64.b64encode(apic.data).decode(),
                        }
                    break
            # M4A — covr atom
            if "covr" in tags:
                data = bytes(tags["covr"][0])
                if len(data) <= 300_000:
                    return {
                        "source": "id3",
                        "mime": "image/jpeg",
                        "data": base64.b64encode(data).decode(),
                    }
            # FLAC — picture block
            if hasattr(audio, "pictures") and audio.pictures:
                pic = audio.pictures[0]
                if len(pic.data) <= 300_000:
                    return {
                        "source": "id3",
                        "mime": pic.mime,
                        "data": base64.b64encode(pic.data).decode(),
                    }
        except Exception as e:
            log.debug(f"Album art extraction failed for {path}: {e}")
        return {}

    def log_message(self, fmt, *args):
        pass  # suppress per-request logging


class WebServer:
    def __init__(self, state, engine=None, host: str = "0.0.0.0", port: int = 8080,
                 admin_password: str = "", ice_cfg: dict = None):
        _Handler.state = state
        _Handler.engine = engine
        _Handler._admin_password = admin_password
        _Handler._ice_cfg = ice_cfg or {}
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
