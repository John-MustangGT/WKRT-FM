#!/usr/bin/env python3
"""
wkrt_ls.py — list and audit tracks in the configured WKRT music library.

Usage:
  python3 wkrt_ls.py                   # Artist - Title, sorted
  python3 wkrt_ls.py --path            # add file path (tab-separated)
  python3 wkrt_ls.py --csv             # CSV: year,artist,title[,path]
  python3 wkrt_ls.py --dupes           # show probable duplicates
  python3 wkrt_ls.py --dupes --fuzzy   # also flag near-match titles
  python3 wkrt_ls.py --dir /path       # override music directory
"""
import argparse
import csv
import os
import re
import sys
from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from wkrt import config as wkrt_config
from wkrt.playlist import scan_library, Track


def _norm(s: str) -> str:
    return re.sub(r'[^a-z0-9]', '', s.lower())


def _fmt_size(n: int) -> str:
    for unit in ('B', 'KB', 'MB'):
        if n < 1024:
            return f"{n}{unit}"
        n //= 1024
    return f"{n}GB"


def _dupe_type(group: list[Track]) -> str:
    """Classify why these tracks are considered duplicates."""
    dirs = [t.path.parent for t in group]
    if len(set(dirs)) == 1:
        # All in the same directory
        suffixes = [t.path.suffix.lower() for t in group]
        if len(set(suffixes)) > 1:
            return "format-dupe"   # same dir, different extension
        return "same-dir"          # same dir, same extension (track-num collision?)
    years = [t.year for t in group]
    if len(set(years)) == 1:
        return "cross-dir"         # same year, different subdirs (shouldn't happen normally)
    return "cross-year"            # same song filed under multiple years


_DUPE_LABELS = {
    "format-dupe": "FORMAT",
    "same-dir":    "SAME-DIR",
    "cross-dir":   "CROSS-DIR",
    "cross-year":  "YEAR",
}

_DUPE_DESCRIPTIONS = {
    "format-dupe": "same directory, different file format — keep the higher-quality file",
    "same-dir":    "same directory and format — likely duplicate import",
    "cross-dir":   "different subdirectory, same year — check for accidental split",
    "cross-year":  "same song filed under different years — check which is correct",
}


def find_exact_dupes(tracks: list[Track]) -> list[list[Track]]:
    """Group tracks by normalized artist+title; return groups with 2+ members."""
    groups: dict[str, list[Track]] = defaultdict(list)
    for t in tracks:
        key = f"{_norm(t.artist)}:{_norm(t.title)}"
        groups[key].append(t)
    return [sorted(v, key=lambda t: (t.year, str(t.path))) for v in groups.values() if len(v) > 1]


def find_fuzzy_dupes(
    tracks: list[Track],
    exact_keys: set[str],
    threshold: float = 0.88,
) -> list[list[Track]]:
    """Find pairs whose artist+title are very similar but didn't match exactly."""
    # Group by artist first to limit O(n²) scope
    by_artist: dict[str, list[Track]] = defaultdict(list)
    for t in tracks:
        by_artist[_norm(t.artist)].append(t)

    seen: set[frozenset[int]] = set()
    fuzzy_groups: list[list[Track]] = []

    for artist_tracks in by_artist.values():
        if len(artist_tracks) < 2:
            continue
        for i, a in enumerate(artist_tracks):
            for b in artist_tracks[i + 1:]:
                pair_id = frozenset({id(a), id(b)})
                if pair_id in seen:
                    continue
                # Skip if they're already caught by exact matching
                ka = f"{_norm(a.artist)}:{_norm(a.title)}"
                kb = f"{_norm(b.artist)}:{_norm(b.title)}"
                if ka == kb or ka in exact_keys or kb in exact_keys:
                    continue
                score = SequenceMatcher(None, _norm(a.title), _norm(b.title)).ratio()
                if score >= threshold:
                    seen.add(pair_id)
                    fuzzy_groups.append(sorted([a, b], key=lambda t: (t.year, str(t.path))))

    return fuzzy_groups


def print_dupes(exact_groups: list[list[Track]], fuzzy_groups: list[list[Track]]):
    total = len(exact_groups) + len(fuzzy_groups)
    if total == 0:
        print("No duplicates found.")
        return

    if exact_groups:
        print(f"── Exact duplicates ({len(exact_groups)} groups) " + "─" * 40)
        for group in sorted(exact_groups, key=lambda g: (g[0].artist.lower(), g[0].title.lower())):
            dtype  = _dupe_type(group)
            label  = _DUPE_LABELS[dtype]
            desc   = _DUPE_DESCRIPTIONS[dtype]
            print(f"\n  [{label}] {group[0].artist} — {group[0].title}")
            print(f"  {desc}")
            for t in group:
                size = _fmt_size(os.path.getsize(t.path)) if t.path.exists() else "?"
                print(f"    {t.year}  {str(t.path):<60}  {size}")

    if fuzzy_groups:
        print(f"\n── Near-matches ({len(fuzzy_groups)} pairs) " + "─" * 42)
        print("  (high title similarity — may be alternate versions, not true dupes)")
        for group in sorted(fuzzy_groups, key=lambda g: (g[0].artist.lower(), g[0].title.lower())):
            print(f"\n  {group[0].artist}")
            for t in group:
                size = _fmt_size(os.path.getsize(t.path)) if t.path.exists() else "?"
                print(f"    {t.year}  {t.title:<45}  {str(t.path.name):<40}  {size}")

    print(f"\n{sum(len(g) for g in exact_groups)} files in {len(exact_groups)} exact-dupe groups", end="")
    if fuzzy_groups:
        print(f", {len(fuzzy_groups)} near-match pairs", end="")
    print()


def main():
    parser = argparse.ArgumentParser(
        description="List tracks in the WKRT music library"
    )
    parser.add_argument("--path",  action="store_true", help="include file path")
    parser.add_argument("--csv",   action="store_true", help="CSV output")
    parser.add_argument("--dupes", action="store_true", help="show probable duplicates")
    parser.add_argument("--fuzzy", action="store_true", help="with --dupes: also flag near-match titles")
    parser.add_argument("--dir",   metavar="DIR",       help="override music directory")
    args = parser.parse_args()

    cfg = wkrt_config.load()
    music_dir = args.dir or cfg["paths"]["music_dir"]

    library = scan_library(music_dir)
    if not library:
        print(f"No tracks found in {music_dir}", file=sys.stderr)
        sys.exit(1)

    tracks = sorted(
        (t for ts in library.values() for t in ts),
        key=lambda t: (t.artist.lower(), t.title.lower()),
    )

    if args.dupes:
        exact = find_exact_dupes(tracks)
        exact_keys = {f"{_norm(t.artist)}:{_norm(t.title)}" for g in exact for t in g}
        fuzzy = find_fuzzy_dupes(tracks, exact_keys) if args.fuzzy else []
        print_dupes(exact, fuzzy)
        return

    if args.csv:
        writer = csv.writer(sys.stdout)
        header = ["year", "artist", "title"]
        if args.path:
            header.append("path")
        writer.writerow(header)
        for t in tracks:
            row = [t.year, t.artist, t.title]
            if args.path:
                row.append(str(t.path))
            writer.writerow(row)
    else:
        for t in tracks:
            line = f"{t.artist} - {t.title}"
            if args.path:
                line += f"\t{t.path}"
            print(line)


if __name__ == "__main__":
    main()
