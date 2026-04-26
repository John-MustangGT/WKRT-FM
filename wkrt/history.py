"""
wkrt/history.py — Per-track play history.

Tracks total plays + last 5 plays per DJ, broken down by time slot.
Stored as one JSON file per track under config/history/.
"""
import json
import logging
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


class PlayHistory:
    KEEP_LAST_N = 5

    def __init__(self, config_dir: Path):
        self.history_dir = config_dir / "history"
        self.history_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _path(self, artist: str, title: str) -> Path:
        def norm(s):
            return re.sub(r"[^a-z0-9]", "_", s.lower())[:30]
        return self.history_dir / f"{norm(artist)}_{norm(title)}.json"

    def load(self, artist: str, title: str) -> dict:
        path = self._path(artist, title)
        if path.exists():
            try:
                return json.loads(path.read_text())
            except Exception:
                pass
        return {"total_plays": 0, "djs": {}}

    def record_play(self, artist: str, title: str, dj_name: str, slot: str):
        with self._lock:
            data = self.load(artist, title)
            data["total_plays"] = data.get("total_plays", 0) + 1

            dj = data.setdefault("djs", {}).setdefault(
                dj_name, {"total": 0, "by_slot": {}, "last_played": []}
            )
            dj["total"] += 1
            dj["by_slot"][slot] = dj["by_slot"].get(slot, 0) + 1
            dj["last_played"].insert(0, {
                "at": datetime.now(timezone.utc).isoformat(),
                "slot": slot,
            })
            dj["last_played"] = dj["last_played"][: self.KEEP_LAST_N]

            self._path(artist, title).write_text(json.dumps(data, indent=2))
