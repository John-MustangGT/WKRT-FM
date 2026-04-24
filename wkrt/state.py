"""
Shared station state — updated by the engine, read by the web server.
"""
import threading
from datetime import datetime
from typing import Optional


class StationState:
    def __init__(self):
        self._lock = threading.Lock()
        self.current_track = None
        self.next_track = None
        self.listener_count = 0
        self.last_dj_script = ""
        self.current_dj = ""
        self.recent_tracks = []   # list of dicts, newest first
        self.cache_state = "COLD"
        self.stream_url = ""
        self.stream_port = 8000
        self.stream_mount = "/wkrt"
        self.started_at = datetime.now().isoformat()

    def set_now_playing(self, track, next_track=None):
        with self._lock:
            if self.current_track:
                self.recent_tracks.insert(0, {
                    "artist": self.current_track.artist,
                    "title": self.current_track.title,
                    "year": self.current_track.year,
                })
                self.recent_tracks = self.recent_tracks[:10]
            self.current_track = track
            self.next_track = next_track

    def set_dj_script(self, text: str):
        with self._lock:
            self.last_dj_script = text

    def set_active_dj(self, name: str):
        with self._lock:
            self.current_dj = name

    def set_listener_count(self, count: int):
        with self._lock:
            self.listener_count = count

    def set_cache_state(self, name: str):
        with self._lock:
            self.cache_state = name

    def to_dict(self) -> dict:
        with self._lock:
            t = self.current_track
            n = self.next_track
            return {
                "current_track": {
                    "artist": t.artist,
                    "title": t.title,
                    "year": t.year,
                    "album": getattr(t, "album", ""),
                } if t else None,
                "next_track": {
                    "artist": n.artist,
                    "title": n.title,
                    "year": n.year,
                } if n else None,
                "listener_count": self.listener_count,
                "current_dj": self.current_dj,
                "last_dj_script": self.last_dj_script,
                "recent_tracks": list(self.recent_tracks),
                "cache_state": self.cache_state,
                "stream_url": self.stream_url,
                "stream_port": self.stream_port,
                "stream_mount": self.stream_mount,
                "started_at": self.started_at,
            }
