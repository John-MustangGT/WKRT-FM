"""
wkrt/annotator.py — MusicBrainz track annotation cache.

Fetches recording metadata (album, label, release year, genre tags) from
MusicBrainz and caches it per-track under config/annotations/.

MusicBrainz policy: 1 request/second, descriptive User-Agent required.
No API key needed.
"""
import json
import logging
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import quote
from urllib.request import Request, urlopen

log = logging.getLogger(__name__)

_MB_BASE   = "https://musicbrainz.org/ws/2"
_USER_AGENT = "WKRT-FM/1.0 radio-automation"

_rate_lock    = threading.Lock()
_last_request = 0.0


def _mb_get(path: str) -> dict:
    """One rate-limited, authenticated MusicBrainz GET. Returns parsed JSON."""
    global _last_request
    with _rate_lock:
        gap = time.monotonic() - _last_request
        if gap < 1.05:
            time.sleep(1.05 - gap)
        req = Request(
            f"{_MB_BASE}{path}",
            headers={"User-Agent": _USER_AGENT, "Accept": "application/json"},
        )
        with urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        _last_request = time.monotonic()
    return data


def _norm_filename(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "_", s.lower())[:40]


class Annotator:
    def __init__(self, config_dir: Path):
        self.cache_dir = config_dir / "annotations"
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _cache_path(self, artist: str, title: str) -> Path:
        return self.cache_dir / f"{_norm_filename(artist)}_{_norm_filename(title)}.json"

    def load(self, artist: str, title: str) -> Optional[dict]:
        """Return cached annotation, or None if not cached / not found on MB."""
        path = self._cache_path(artist, title)
        if path.exists():
            try:
                data = json.loads(path.read_text())
                return None if data.get("found") is False else data
            except Exception:
                pass
        return None

    def _save(self, artist: str, title: str, data: dict):
        self._cache_path(artist, title).write_text(json.dumps(data, indent=2))

    def fetch(self, artist: str, title: str) -> Optional[dict]:
        """Fetch from MusicBrainz, cache result, return annotation or None."""
        if self._cache_path(artist, title).exists():
            return self.load(artist, title)

        now = datetime.now(timezone.utc).isoformat()
        try:
            q = f'recording:"{quote(title)}" AND artist:"{quote(artist)}"'
            data = _mb_get(f"/recording?query={q}&fmt=json&limit=5")
            recordings = data.get("recordings", [])

            if not recordings or recordings[0].get("score", 0) < 60:
                self._save(artist, title, {"found": False, "fetched_at": now})
                log.debug(f"MusicBrainz: no confident match for {artist} — {title}")
                return None

            rec = recordings[0]

            # Album + label from first release
            album = label = release_year = None
            releases = rec.get("releases", [])
            if releases:
                r = releases[0]
                album = r.get("title")
                raw_date = r.get("date", "")
                if raw_date:
                    release_year = raw_date[:4]
                li = r.get("label-info", [])
                if li and li[0].get("label"):
                    label = li[0]["label"].get("name")

            if not release_year:
                release_year = rec.get("first-release-date", "")[:4] or None

            # Genre tags with non-zero votes
            tags = [
                t["name"] for t in rec.get("tags", [])
                if t.get("count", 0) > 0
            ][:5]

            annotation = {
                "found":        True,
                "artist":       artist,
                "title":        title,
                "album":        album,
                "label":        label,
                "release_year": release_year,
                "tags":         tags,
                "mbid":         rec.get("id"),
                "fetched_at":   now,
            }
            self._save(artist, title, annotation)
            log.debug(f"MusicBrainz: annotated {artist} — {title} (score {rec['score']})")
            return annotation

        except Exception as e:
            log.debug(f"MusicBrainz fetch failed for {artist} — {title}: {e}")
            return None

    def fetch_library(self, library: dict):
        """Background sweep — annotate any un-cached track in the library."""
        tracks = [t for tracks in library.values() for t in tracks]
        missing = [t for t in tracks if not self._cache_path(t.artist, t.title).exists()]
        if not missing:
            log.info("Annotation cache: all tracks already annotated")
            return
        log.info(f"Annotation: fetching MusicBrainz data for {len(missing)} tracks…")
        ok = 0
        for t in missing:
            if self.fetch(t.artist, t.title) is not None:
                ok += 1
        log.info(f"Annotation sweep complete: {ok}/{len(missing)} matched on MusicBrainz")

    @staticmethod
    def format_for_prompt(annotation: Optional[dict], label: str) -> list[str]:
        """Return a list of fact lines suitable for injecting into a DJ prompt."""
        if not annotation:
            return []
        lines = []
        if annotation.get("album"):
            lines.append(f'{label} album: {annotation["album"]}')
        if annotation.get("release_year"):
            lines.append(f'{label} released: {annotation["release_year"]}')
        if annotation.get("tags"):
            lines.append(f'{label} style: {", ".join(annotation["tags"][:3])}')
        return lines
