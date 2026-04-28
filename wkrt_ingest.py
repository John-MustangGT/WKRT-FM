#!/usr/bin/env python3
"""
wkrt_ingest.py — drop audio files into new_music/, run this to process them.

What it does:
  1. Waits for each file to finish writing (size-stability check)
  2. Reads ID3 tags; queries MusicBrainz to fill any missing fields
  3. Writes updated tags back to the file before moving it
  4. Moves the file into music/<year>/
  5. POSTs to the running station's /api/library/ingest to hot-add to the crate
     (if the station isn't running, files are in place for the next restart)

Usage:
  python wkrt_ingest.py                         # processes new_music/
  python wkrt_ingest.py /path/to/drop/dir       # custom drop directory
  python wkrt_ingest.py --api http://host:8080  # custom station URL
  python wkrt_ingest.py --music-dir /mnt/music  # custom music library root
  python wkrt_ingest.py --skip-mb               # skip MusicBrainz tag enrichment
"""
import argparse
import base64
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import URLError

try:
    from mutagen import File as MutagenFile
except ImportError:
    print("ERROR: mutagen not installed — run: pip install mutagen", file=sys.stderr)
    sys.exit(1)

AUDIO_EXTENSIONS = {".mp3", ".flac", ".m4a", ".ogg", ".wav"}
STABLE_SECS  = 2
STABLE_POLLS = 5

_MB_BASE       = "https://musicbrainz.org/ws/2"
_USER_AGENT    = "WKRT-FM/1.0 radio-automation"
_MB_MIN_SCORE  = 75   # higher than the annotator's 60 — we're writing to files


# ── Config ────────────────────────────────────────────────────────────────────

def load_admin_password() -> str:
    env = os.environ.get("WKRT_ADMIN_PASSWORD", "")
    if env:
        return env
    cfg_path = Path(__file__).parent / "config" / "settings.toml"
    if cfg_path.exists():
        try:
            try:
                import tomllib
            except ImportError:
                import tomli as tomllib  # type: ignore
            data = tomllib.loads(cfg_path.read_text())
            return data.get("web", {}).get("admin_password", "")
        except Exception:
            pass
    return ""


# ── File stability ────────────────────────────────────────────────────────────

def wait_stable(path: Path, timeout: int = 60) -> bool:
    prev_size = -1
    stable_count = 0
    for _ in range(timeout):
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            return False
        if size == prev_size and size > 0:
            stable_count += 1
            if stable_count >= STABLE_POLLS:
                return True
        else:
            stable_count = 0
            prev_size = size
        time.sleep(1)
    return False


# ── Tag reading ───────────────────────────────────────────────────────────────

def _parse_filename(stem: str) -> tuple[Optional[str], Optional[str]]:
    """Best-effort 'Artist - Title' parse from filename stem."""
    # Strip leading track numbers like "01 ", "01. ", "01 - "
    stem = re.sub(r'^\d{1,3}[\s.\-]+', '', stem)
    if ' - ' in stem:
        parts = stem.split(' - ', 1)
        return parts[0].replace('_', ' ').strip() or None, \
               parts[1].replace('_', ' ').strip() or None
    return None, stem.replace('_', ' ').strip() or None


def read_tags(path: Path) -> dict:
    """Return dict with artist/title/album/year, any of which may be None."""
    result: dict = {"artist": None, "title": None, "album": None, "year": None}
    try:
        audio = MutagenFile(path, easy=True)
        if audio and audio.tags:
            for field in ("artist", "title", "album"):
                val = str((audio.tags.get(field) or [""])[0]).strip()
                if val:
                    result[field] = val
            for key in ("date", "year"):
                val = str((audio.tags.get(key) or [""])[0])
                m = re.search(r"(19|20)\d{2}", val)
                if m:
                    result["year"] = int(m.group())
                    break
    except Exception:
        pass

    # Fill artist/title from filename if still missing
    fn_artist, fn_title = _parse_filename(path.stem)
    if not result["artist"] and fn_artist:
        result["artist"] = fn_artist
    if not result["title"] and fn_title:
        result["title"] = fn_title

    return result


def infer_year(path: Path, tags: dict) -> Optional[int]:
    """Year from tags (already read) → filename → parent dir name."""
    if tags.get("year"):
        return tags["year"]
    m = re.search(r"(19|20)\d{2}", path.stem)
    if m:
        return int(m.group())
    m = re.fullmatch(r"(19|20)\d{2}", path.parent.name)
    if m:
        return int(m.group())
    return None


# ── MusicBrainz lookup ────────────────────────────────────────────────────────

_mb_last_request = 0.0

def _mb_get(params: dict) -> dict:
    global _mb_last_request
    gap = time.monotonic() - _mb_last_request
    if gap < 1.1:
        time.sleep(1.1 - gap)
    url = f"{_MB_BASE}/recording?{urlencode(params)}"
    req = Request(url, headers={"User-Agent": _USER_AGENT, "Accept": "application/json"})
    try:
        with urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    finally:
        _mb_last_request = time.monotonic()


def mb_lookup(artist: str, title: str) -> Optional[dict]:
    """
    Query MusicBrainz for artist+title. Returns a dict with:
      artist, title, album, year (int), mbid
    or None if no confident match found.
    """
    q = f'recording:"{title}" AND artist:"{artist}"'
    try:
        data = _mb_get({"query": q, "fmt": "json", "limit": 3})
    except Exception as e:
        print(f"    MusicBrainz request failed: {e}")
        return None

    recordings = data.get("recordings", [])
    if not recordings:
        return None

    rec = recordings[0]
    score = rec.get("score", 0)
    if score < _MB_MIN_SCORE:
        print(f"    MusicBrainz: low confidence score ({score}) — skipping tag update")
        return None

    # Canonical artist name from MB
    mb_artist = artist
    credits = rec.get("artist-credit", [])
    if credits and isinstance(credits[0], dict):
        mb_artist = credits[0].get("artist", {}).get("name") or artist

    # Album + release year from first release
    album = release_year = None
    releases = rec.get("releases", [])
    if releases:
        r = releases[0]
        album = r.get("title")
        raw_date = r.get("date", "")
        m = re.search(r"(19|20)\d{2}", raw_date)
        if m:
            release_year = int(m.group())

    if not release_year:
        raw = rec.get("first-release-date", "")
        m = re.search(r"(19|20)\d{2}", raw)
        if m:
            release_year = int(m.group())

    return {
        "artist": mb_artist,
        "title":  rec.get("title") or title,
        "album":  album,
        "year":   release_year,
        "mbid":   rec.get("id"),
        "score":  score,
    }


# ── Tag writing ───────────────────────────────────────────────────────────────

def write_missing_tags(path: Path, current: dict, mb: dict) -> list[str]:
    """
    Write fields from mb into path's tags, but ONLY where current has no value.
    Returns list of field names that were written.
    """
    to_write = {}
    field_to_tag = {"artist": "artist", "title": "title", "album": "album", "year": "date"}

    for field, tag_key in field_to_tag.items():
        if not current.get(field) and mb.get(field):
            to_write[tag_key] = str(mb[field])

    if not to_write:
        return []

    try:
        audio = MutagenFile(path, easy=True)
        if audio is None:
            print(f"    mutagen could not open file for writing")
            return []
        if audio.tags is None:
            audio.add_tags()
        for tag_key, val in to_write.items():
            audio.tags[tag_key] = [val]
        audio.save()
    except Exception as e:
        print(f"    Tag write failed: {e}")
        return []

    return [f for f, tk in field_to_tag.items() if tk in to_write]


def enrich_tags(path: Path, tags: dict) -> dict:
    """
    Look up missing fields on MusicBrainz and write them back to the file.
    Returns the updated tags dict. Prints status to stdout.
    """
    missing = [f for f in ("artist", "title", "album", "year") if not tags.get(f)]
    if not missing:
        return tags  # nothing to do

    artist = tags.get("artist") or "Unknown"
    title  = tags.get("title")  or path.stem

    print(f"    Missing tags: {', '.join(missing)} — querying MusicBrainz...", end=" ", flush=True)

    mb = mb_lookup(artist, title)
    if not mb:
        print("no match found")
        return tags

    print(f"matched (score {mb['score']})")

    written = write_missing_tags(path, tags, mb)
    if written:
        print(f"    Tags written: {', '.join(written)}")
        for f in written:
            tags[f] = mb[f]
    else:
        print(f"    No new tags to write (all already set)")

    return tags


# ── Station notification ──────────────────────────────────────────────────────

def notify_station(api_url: str, paths: list[str], password: str = "") -> int:
    payload = json.dumps({"paths": paths}).encode()
    headers = {"Content-Type": "application/json"}
    if password:
        creds = base64.b64encode(f"admin:{password}".encode()).decode()
        headers["Authorization"] = f"Basic {creds}"
    req = Request(
        f"{api_url.rstrip('/')}/api/library/ingest",
        data=payload,
        headers=headers,
        method="POST",
    )
    try:
        with urlopen(req, timeout=8) as resp:
            result = json.loads(resp.read())
            return result.get("ingested", 0)
    except URLError as e:
        print(f"  Station not reachable ({e}) — files are in place, will load on next restart.")
        return 0
    except Exception as e:
        print(f"  Station notification failed ({e}).")
        return 0


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Ingest new music into WKRT")
    parser.add_argument("drop_dir", nargs="?", default="new_music",
                        help="Directory to scan for new files (default: new_music)")
    parser.add_argument("--music-dir", default="music",
                        help="Music library root (default: music)")
    parser.add_argument("--api", default="http://localhost:8080",
                        help="Station API URL (default: http://localhost:8080)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would happen without moving files or notifying")
    parser.add_argument("--skip-mb", action="store_true",
                        help="Skip MusicBrainz tag enrichment")
    args = parser.parse_args()

    drop = Path(args.drop_dir)
    music_root = Path(args.music_dir)

    if not drop.exists():
        print(f"Drop directory not found: {drop}", file=sys.stderr)
        sys.exit(1)

    files = sorted(
        f for f in drop.iterdir()
        if f.is_file() and f.suffix.lower() in AUDIO_EXTENSIONS
    )

    if not files:
        print(f"No audio files found in {drop}/")
        sys.exit(0)

    print(f"Found {len(files)} file(s) in {drop}/\n")

    moved: list[str] = []
    skipped: list[str] = []

    for f in files:
        print(f"  {f.name}")

        if not args.dry_run:
            print("    Waiting for file to stabilise...", end=" ", flush=True)
            if not wait_stable(f):
                print("TIMEOUT or disappeared — skipping")
                skipped.append(f.name)
                continue
            print("ready")

        # Read current tags (with filename fallback for artist/title)
        tags = read_tags(f)

        # Enrich missing tags from MusicBrainz before moving
        if not args.dry_run and not args.skip_mb:
            tags = enrich_tags(f, tags)

        year = infer_year(f, tags)
        if not year:
            print(f"    SKIP — could not determine year "
                  f"(set ID3 date tag or rename to include year e.g. '1984')")
            skipped.append(f.name)
            continue

        dest_dir = music_root / str(year)
        dest = dest_dir / f.name

        if dest.exists():
            print(f"    SKIP — already in library: music/{year}/{f.name}")
            if not args.dry_run:
                f.unlink()
            continue

        print(f"    → music/{year}/{f.name}", end="")
        if args.dry_run:
            if args.skip_mb:
                pass
            else:
                # Show what MB would fill in without actually calling it
                missing = [field for field in ("artist", "title", "album", "year")
                           if not tags.get(field)]
                if missing:
                    print(f"  [would query MB for: {', '.join(missing)}]", end="")
            print("  [dry-run]")
        else:
            dest_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(f), dest)
            print()
            moved.append(str(dest.resolve()))

    print()

    if not moved:
        if skipped:
            print(f"Nothing ingested. {len(skipped)} file(s) skipped.")
        sys.exit(0)

    password = load_admin_password()
    print(f"Moved {len(moved)} file(s). Notifying station at {args.api}...")
    n = notify_station(args.api, moved, password)
    if n:
        print(f"Hot-added {n} track(s) to the DJ's crate — "
              f"they'll get a special on-air introduction.")
    else:
        print("(Station will pick up new tracks on next restart.)")

    if skipped:
        print(f"\n{len(skipped)} file(s) skipped: {', '.join(skipped)}")


if __name__ == "__main__":
    main()
