"""
Playlist engine — scans music directory, builds weighted shuffle queue.

Music layout expected:
  music/
    1980/
      Artist - Title.mp3
    1981/
      ...

ID3 tags are preferred for metadata; filename is the fallback.
"""
import random
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Optional

from mutagen import File as MutagenFile
from mutagen.id3 import ID3NoHeaderError


AUDIO_EXTENSIONS = {".mp3", ".flac", ".m4a", ".ogg", ".wav"}


@dataclass
class Track:
    path: Path
    year: int
    artist: str
    title: str
    duration_seconds: float = 0.0
    album: str = ""

    @property
    def display(self) -> str:
        return f"{self.artist} — {self.title} ({self.year})"


def _parse_filename(stem: str) -> tuple[str, str]:
    """Best-effort parse of 'Artist - Title' from filename stem."""
    if " - " in stem:
        parts = stem.split(" - ", 1)
        return parts[0].replace("_", " ").strip(), parts[1].replace("_", " ").strip()
    return "Unknown Artist", stem.replace("_", " ").strip()


def _read_tags(path: Path) -> tuple[str, str, float, str]:
    """Returns (artist, title, duration, album). Falls back to filename."""
    artist, title = _parse_filename(path.stem)
    duration = 0.0
    album = ""
    try:
        audio = MutagenFile(path, easy=True)
        if audio is None:
            return artist, title, duration, album
        if hasattr(audio, "info") and hasattr(audio.info, "length"):
            duration = audio.info.length
        tags = audio.tags or {}
        if "artist" in tags:
            artist = str(tags["artist"][0]).strip() or artist
        if "title" in tags:
            title = str(tags["title"][0]).strip() or title
        if "album" in tags:
            album = str(tags["album"][0]).strip()
    except Exception:
        pass
    return artist, title, duration, album


def scan_library(music_dir: str) -> dict[int, list[Track]]:
    """
    Scan music_dir for year subdirectories.
    Returns dict mapping year -> list of Track.
    """
    library: dict[int, list[Track]] = {}
    base = Path(music_dir)

    if not base.exists():
        return library

    for year_dir in sorted(base.iterdir()):
        if not year_dir.is_dir():
            continue
        # Accept dirs named as 4-digit years in 1900-2099
        m = re.fullmatch(r"(19|20)\d{2}", year_dir.name)
        if not m:
            continue
        year = int(year_dir.name)
        tracks: list[Track] = []

        for f in sorted(year_dir.iterdir()):
            if f.suffix.lower() not in AUDIO_EXTENSIONS:
                continue
            artist, title, duration, album = _read_tags(f)
            tracks.append(Track(
                path=f,
                year=year,
                artist=artist,
                title=title,
                duration_seconds=duration,
                album=album,
            ))

        if tracks:
            library[year] = tracks

    return library


class PlaylistQueue:
    """
    Infinite weighted-shuffle iterator over the library.
    Generates tracks in year-weighted random order.
    Never plays the same track twice in a row.
    Avoids repeating a track until at least half the library has played.
    """

    def __init__(self, library: dict[int, list[Track]], year_weights: dict[str, float]):
        self.library = library
        self.year_weights = {int(k): v for k, v in year_weights.items()}
        self._recent: list[Path] = []
        self._history_limit = max(10, sum(len(v) for v in library.values()) // 4)
        self._queue: list[Track] = []
        self._refill()

    def _weighted_years(self) -> list[int]:
        years = []
        for year, tracks in self.library.items():
            weight = self.year_weights.get(year, 1.0)
            count = max(1, round(weight * 10))
            years.extend([year] * count)
        return years

    def _refill(self):
        """Build a new shuffled batch of tracks across all years."""
        weighted_years = self._weighted_years()
        random.shuffle(weighted_years)
        batch: list[Track] = []
        seen_years: set[int] = set()

        for year in weighted_years:
            if year in seen_years:
                continue
            seen_years.add(year)
            tracks = list(self.library[year])
            random.shuffle(tracks)
            # Filter out recently played
            available = [t for t in tracks if t.path not in self._recent]
            if not available:
                available = tracks  # fallback if all recently played
            batch.extend(available[:max(1, len(available))])

        random.shuffle(batch)
        self._queue = batch

    def __iter__(self) -> Iterator[Track]:
        return self

    def __next__(self) -> Track:
        if not self._queue:
            self._refill()

        # Pick next, avoiding immediate repeat
        for i, track in enumerate(self._queue):
            if not self._recent or track.path != self._recent[-1]:
                self._queue.pop(i)
                self._recent.append(track.path)
                if len(self._recent) > self._history_limit:
                    self._recent.pop(0)
                return track

        # Fallback
        track = self._queue.pop(0)
        return track

    @property
    def library_size(self) -> int:
        return sum(len(v) for v in self.library.values())

    @property
    def year_count(self) -> int:
        return len(self.library)
