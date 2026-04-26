#!/usr/bin/env python3
"""
wkrt_ingest.py — drop audio files into new_music/, run this to process them.

What it does:
  1. Waits for each file to finish writing (size-stability check)
  2. Reads ID3 tags to determine year; falls back to filename/parent dir
  3. Moves the file into music/<year>/
  4. POSTs to the running station's /api/library/ingest to hot-add to the crate
     (if the station isn't running, files are in place for the next restart)

Usage:
  python wkrt_ingest.py                         # processes new_music/
  python wkrt_ingest.py /path/to/drop/dir       # custom drop directory
  python wkrt_ingest.py --api http://host:8080  # custom station URL
  python wkrt_ingest.py --music-dir /mnt/music  # custom music library root
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

try:
    from mutagen import File as MutagenFile
except ImportError:
    print("ERROR: mutagen not installed — run: pip install mutagen", file=sys.stderr)
    sys.exit(1)

try:
    from urllib.request import urlopen, Request
    from urllib.error import URLError
except ImportError:
    pass

AUDIO_EXTENSIONS = {".mp3", ".flac", ".m4a", ".ogg", ".wav"}
STABLE_SECS = 2      # seconds of unchanged size before we consider a file complete
STABLE_POLLS = 5     # number of size checks before declaring stability


def load_admin_password() -> str:
    """Read admin password: env var → settings.toml → empty (no auth)."""
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


def wait_stable(path: Path, timeout: int = 60) -> bool:
    """Block until the file size stops changing. Returns False if it disappears."""
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


def infer_year(path: Path) -> int | None:
    """Try ID3 tags → filename → parent directory name for a 4-digit year."""
    try:
        audio = MutagenFile(path, easy=True)
        if audio and audio.tags:
            for key in ("date", "year"):
                val = str((audio.tags.get(key) or [""])[0])
                m = re.search(r"(19|20)\d{2}", val)
                if m:
                    return int(m.group())
    except Exception:
        pass

    # Filename (e.g. "1984 - Some Song.mp3")
    m = re.search(r"(19|20)\d{2}", path.stem)
    if m:
        return int(m.group())

    # Parent dir already named as a year (e.g. already in music/1985/)
    m = re.fullmatch(r"(19|20)\d{2}", path.parent.name)
    if m:
        return int(m.group())

    return None


def notify_station(api_url: str, paths: list[str], password: str = "") -> int:
    """POST paths to the station API. Returns number of tracks the engine accepted."""
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

        year = infer_year(f)
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
