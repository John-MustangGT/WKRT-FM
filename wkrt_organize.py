#!/usr/bin/env python3
"""
wkrt_organize.py — Copy matched tracks from existing music repo into WKRT layout.

Target layout:
    /wkrt/music/
        1980/
            AC_DC - You Shook Me All Night Long.m4a
            Queen - Crazy Little Thing Called Love.m4a
            ...
        1981/
            ...

Usage:
    # Dry run first — see what would be copied
    python3 wkrt_organize.py --src /path/to/music --dst /wkrt/music --dry-run

    # Actually copy
    python3 wkrt_organize.py --src /path/to/music --dst /wkrt/music

    # Verify after copy
    python3 wkrt_organize.py --src /path/to/music --dst /wkrt/music --verify
"""

import argparse
import re
import shutil
import sys
from collections import defaultdict
from pathlib import Path

# ── Master tracklist ─────────────────────────────────────────────────────────
# (year, artist, title, search_hints)
# search_hints: list of (artist_dir_fragment, title_fragment) tuples
# First hint that matches wins. Supports compilation sources.

TRACKLIST = [
    # ── 1980 ──────────────────────────────────────────────────────────────
    (1980, "AC_DC",             "You Shook Me All Night Long",  [("AC_DC", "You Shook Me")]),
    (1980, "Queen",             "Crazy Little Thing Called Love",[("Queen", "Crazy Little Thing")]),
    (1980, "Pink Floyd",        "Another Brick in the Wall",    [("Pink Floyd", "Another Brick")]),
    (1980, "Tom Petty",         "Refugee",                      [("Tom Petty", "Refugee")]),
    (1980, "Bob Seger",         "Against the Wind",             [("Bob Seger", "Against the Wind")]),
    (1980, "Rush",              "The Spirit of Radio",          [("Rush", "Spirit of Radio")]),
    (1980, "The Clash",         "Train in Vain",                [("The Clash", "Train in Vain")]),
    (1980, "Billy Joel",        "It's Still Rock and Roll to Me",[("Billy Joel", "Still Rock")]),
    (1980, "Heart",             "Even It Up",                   [("Heart", "Even It Up")]),
    (1980, "Van Halen",         "And the Cradle Will Rock",     [("Van Halen", "Cradle Will Rock")]),
    (1980, "Talking Heads",     "Once in a Lifetime",           [("Talking Heads", "Once in a Lifetime")]),
    (1980, "Devo",              "Whip It",                      [("Devo", "Whip It")]),
    (1980, "Buggles",           "Video Killed the Radio Star",  [("Compilations", "Video Killed"),
                                                                  ("Buggles", "Video Killed")]),
    (1980, "Joan Jett",         "Bad Reputation",               [("Joan Jett", "Bad Reputation")]),
    (1980, "Blondie",           "Call Me",                      [("Blondie", "Call Me")]),
    (1980, "Deep Purple",       "Black Night",                  [("Deep Purple", "Black Night")]),
    # ── 1981 ──────────────────────────────────────────────────────────────
    (1981, "Journey",           "Don't Stop Believin'",         [("Journey", "Don't Stop Believin")]),
    (1981, "Rush",              "Tom Sawyer",                   [("Rush", "Tom Sawyer")]),
    (1981, "AC_DC",             "Back in Black",                [("AC_DC", "Back in Black")]),
    (1981, "Van Halen",         "Unchained",                    [("Van Halen", "Unchained")]),
    (1981, "Blondie",           "The Tide Is High",             [("Blondie", "Tide Is High")]),
    (1981, "Styx",              "Too Much Time on My Hands",    [("Styx", "Too Much Time")]),
    (1981, "Dire Straits",      "Romeo and Juliet",             [("Dire Straits", "Romeo")]),
    (1981, "Soft Cell",         "Tainted Love",                 [("Soft Cell", "Tainted Love"),
                                                                  ("Compilations", "Tainted Love")]),
    (1981, "The GoGo's",        "Our Lips Are Sealed",          [("Compilations", "Our Lips Are Sealed"),
                                                                  ("GoGo", "Our Lips")]),
    (1981, "Billy Idol",        "Dancing with Myself",          [("Compilations", "Dancing With Myself"),
                                                                  ("Billy Idol", "Dancing")]),
    (1981, "The Rolling Stones","Start Me Up",                  [("The Rolling Stones", "Start Me Up")]),
    (1981, "Bob Seger",         "Feel Like a Number",           [("Bob Seger", "Feel Like")]),
    # ── 1982 ──────────────────────────────────────────────────────────────
    (1982, "Fleetwood Mac",     "Hold Me",                      [("Fleetwood Mac", "Hold Me")]),
    (1982, "Peter Gabriel",     "Shock the Monkey",             [("Peter Gabriel", "Shock the Monkey")]),
    (1982, "Rush",              "New World Man",                [("Rush", "New World Man")]),
    (1982, "Foreigner",         "Waiting for a Girl Like You",  [("Compilations", "Waiting For A Girl"),
                                                                  ("Foreigner", "Waiting")]),
    (1982, "REO Speedwagon",    "Can't Fight This Feeling",     [("Compilations", "Can't Fight This"),
                                                                  ("REO Speedwagon", "Can't Fight")]),
    (1982, "A Flock of Seagulls","I Ran (So Far Away)",         [("Compilations", "I Ran"),
                                                                  ("Flock of Seagulls", "I Ran")]),
    (1982, "Thomas Dolby",      "She Blinded Me With Science",  [("Compilations", "She Blinded"),
                                                                  ("Thomas Dolby", "Blinded")]),
    (1982, "Wall of Voodoo",    "Mexican Radio",                [("Compilations", "Mexican Radio"),
                                                                  ("Wall of Voodoo", "Mexican")]),
    (1982, "Tommy Tutone",      "867-5309/Jenny",               [("Compilations", "867"),
                                                                  ("Tommy Tutone", "867")]),
    (1982, "The J. Geils Band", "Love Stinks",                  [("Compilations", "Love Stinks"),
                                                                  ("J. Geils", "Love Stinks")]),
    (1982, "Greg Kihn Band",    "Jeopardy",                     [("Compilations", "Jeopardy"),
                                                                  ("Greg Kihn", "Jeopardy")]),
    (1982, "The Smiths",        "How Soon Is Now",              [("Compilations", "How Soon Is Now"),
                                                                  ("The Smiths", "How Soon")]),
    (1982, "38 Special",        "Hold On Loosely",              [("Compilations", "Hold On Loosely"),
                                                                  ("38 Special", "Hold On Loosely")]),
    (1982, "Toto",              "Africa",                       [("Toto", "Africa")]),
    (1982, "Def Leppard",       "Photograph",                   [("Def Leppard", "Photograph")]),
    # ── 1983 ──────────────────────────────────────────────────────────────
    (1983, "David Bowie",       "Let's Dance",                  [("David Bowie", "Let's Dance")]),
    (1983, "Bob Seger",         "Roll Me Away",                 [("Bob Seger", "Roll Me Away")]),
    (1983, "Billy Joel",        "Tell Her About It",            [("Billy Joel", "Tell Her About It")]),
    (1983, "Simple Minds",      "Waterfront",                   [("Simple Minds", "Waterfront")]),
    (1983, "Men at Work",       "Down Under",                   [("Compilations", "Down Under"),
                                                                  ("Men at Work", "Down Under")]),
    (1983, "New Order",         "Blue Monday",                  [("Compilations", "Blue Monday"),
                                                                  ("New Order", "Blue Monday")]),
    (1983, "Elvis Costello",    "Everyday I Write the Book",    [("Compilations", "Everyday I Write"),
                                                                  ("Elvis Costello", "Everyday")]),
    (1983, "Modern English",    "I Melt with You",              [("Compilations", "I Melt With You"),
                                                                  ("Modern English", "I Melt")]),
    (1983, "U2",                "Sunday Bloody Sunday",         [("U2", "Sunday Bloody Sunday")]),
    (1983, "The Clash",         "Should I Stay or Should I Go", [("The Clash", "Should I Stay")]),
    # ── 1984 ──────────────────────────────────────────────────────────────
    (1984, "Van Halen",         "Jump",                         [("Van Halen", "Jump")]),
    (1984, "Def Leppard",       "Rock of Ages",                 [("Def Leppard", "Rock of Ages")]),
    (1984, "Heart",             "Nothin' at All",               [("Heart", "Nothin")]),
    (1984, "Talking Heads",     "Burning Down the House",       [("Talking Heads", "Burning Down")]),
    (1984, "ZZ Top",            "Legs",                         [("ZZ Top", "Legs")]),
    (1984, "AC_DC",             "Guns for Hire",                [("AC_DC", "Guns for Hire")]),
    (1984, "Duran Duran",       "The Reflex",                   [("Compilations", "The Reflex"),
                                                                  ("Duran Duran", "Reflex")]),
    (1984, "The Cars",          "Drive",                        [("Compilations", "Drive"),
                                                                  ("The Cars", "Drive")]),
    (1984, "Kajagoogoo",        "Too Shy",                      [("Compilations", "Too Shy"),
                                                                  ("Kajagoogoo", "Too Shy")]),
    (1984, "The Thompson Twins","Hold Me Now",                  [("Compilations", "Hold Me Now"),
                                                                  ("Thompson Twins", "Hold Me Now")]),
    (1984, "Slade",             "Run Runaway",                  [("Slade", "Run Runaway")]),  # TO BUY
    (1984, "U2",                "Pride (In the Name of Love)",  [("U2", "Pride")]),
    (1984, "R.E.M.",            "Don't Go Back to Rockville",   [("R.E.M_", "Rockville")]),
    # ── 1985 ──────────────────────────────────────────────────────────────
    (1985, "Heart",             "These Dreams",                 [("Heart", "These Dreams")]),
    (1985, "Simple Minds",      "Don't You (Forget About Me)",  [("Simple Minds", "Don't You")]),
    (1985, "Tom Petty",         "Don't Come Around Here No More",[("Tom Petty", "Don't Come Around")]),
    (1985, "ZZ Top",            "Sleeping Bag",                 [("ZZ Top", "Sleeping Bag")]),
    (1985, "Billy Joel",        "You're Only Human",            [("Billy Joel", "Only Human")]),
    (1985, "Van Halen",         "Panama",                       [("Van Halen", "Panama")]),
    (1985, "Rush",              "The Big Money",                [("Rush", "Big Money")]),
    (1985, "Don Henley",        "The Boys of Summer",           [("Compilations", "Boys Of Summer"),
                                                                  ("Don Henley", "Boys of Summer")]),
    (1985, "The Cars",          "Drive",                        [("Compilations", "Drive"),
                                                                  ("The Cars", "Drive")]),
    (1985, "Billy Idol",        "White Wedding",                [("Compilations", "White Wedding"),
                                                                  ("Billy Idol", "White Wedding")]),
    (1985, "The Psychedelic Furs","Love My Way",                [("Compilations", "Love My Way"),
                                                                  ("Psychedelic Furs", "Love My Way")]),
    (1985, "Dire Straits",      "Money for Nothing",            [("Dire Straits", "Money for Nothing")]),
    # ── 1986 ──────────────────────────────────────────────────────────────
    (1986, "Van Halen",         "Why Can't This Be Love",       [("Van Halen", "Why Can't This Be Love")]),
    (1986, "Night Ranger",      "Four in the Morning",          [("Night Ranger", "Four in the Morning")]),
    (1986, "Rush",              "Time Stand Still",             [("Rush", "Time Stand Still")]),
    (1986, "Peter Gabriel",     "Sledgehammer",                 [("Peter Gabriel", "Sledgehammer")]),
    (1986, "Simple Minds",      "Alive and Kicking",            [("Compilations", "Alive And Kicking"),
                                                                  ("Simple Minds", "Alive and Kicking")]),
    (1986, "ZZ Top",            "Rough Boy",                    [("ZZ Top", "Rough Boy")]),
    (1986, "Dire Straits",      "Walk of Life",                 [("Dire Straits", "Walk of Life")]),
    (1986, "Depeche Mode",      "Stripped",                     [("Depeche Mode", "Stripped")]),
    (1986, "38 Special",        "If I'd Been the One",          [("Compilations", "If I'd Been The One"),
                                                                  ("38 Special", "If I'd Been")]),
    (1986, "U2",                "With or Without You",          [("U2", "With or Without You")]),
    (1986, "R.E.M.",            "Fall on Me",                   [("R.E.M_", "Fall on Me")]),
    # ── 1987 ──────────────────────────────────────────────────────────────
    (1987, "Def Leppard",       "Pour Some Sugar on Me",        [("Def Leppard", "Pour Some Sugar")]),
    (1987, "Van Halen",         "Dreams",                       [("Van Halen", "Dreams")]),
    (1987, "Night Ranger",      "The Secret of My Success",     [("Night Ranger", "Secret of My Success")]),
    (1987, "R.E.M.",            "The One I Love",               [("R.E.M_", "The One I Love")]),
    (1987, "Peter Gabriel",     "Big Time",                     [("Peter Gabriel", "Big Time")]),
    (1987, "Depeche Mode",      "Never Let Me Down Again",      [("Depeche Mode", "Never Let Me Down")]),
    (1987, "ZZ Top",            "Doubleback",                   [("ZZ Top", "Doubleback")]),
    (1987, "Rush",              "Force Ten",                    [("Rush", "Force Ten")]),
    (1987, "U2",                "Where the Streets Have No Name",[("U2", "Streets Have No Name")]),
    (1987, "INXS",              "Need You Tonight",             [("Compilations", "Need You Tonight"),
                                                                  ("INXS", "Need You Tonight")]),
    (1987, "Heart",             "Alone",                        [("Heart", "Alone")]),
    (1987, "Crowded House",     "Don't Dream It's Over",        [("Crowded House", "Don't Dream")]),
    # ── 1988 ──────────────────────────────────────────────────────────────
    (1988, "Def Leppard",       "Love Bites",                   [("Def Leppard", "Love Bites")]),
    (1988, "R.E.M.",            "Orange Crush",                 [("R.E.M_", "Orange Crush")]),
    (1988, "Rush",              "Superconductor",               [("Rush", "Superconductor")]),
    (1988, "Night Ranger",      "When You Close Your Eyes",     [("Night Ranger", "When You Close")]),
    (1988, "Lita Ford",         "Kiss Me Deadly",               [("Lita Ford", "Kiss Me Deadly")]),
    (1988, "Depeche Mode",      "Personal Jesus",               [("Depeche Mode", "Personal Jesus")]),
    (1988, "U2",                "Desire",                       [("U2", "Desire")]),
    (1988, "INXS",              "Devil Inside",                 [("INXS", "Devil Inside")]),
    (1988, "The Cult",          "Fire Woman",                   [("The Cult", "Fire Woman")]),
    # ── 1989 ──────────────────────────────────────────────────────────────
    (1989, "Def Leppard",       "Rocket",                       [("Def Leppard", "Rocket")]),
    (1989, "Van Halen",         "When It's Love",               [("Van Halen", "When It's Love")]),
    (1989, "Queensrÿche",       "Silent Lucidity",              [("Queensr", "Silent Lucidity")]),
    (1989, "R.E.M.",            "Stand",                        [("R.E.M_", "Stand")]),
    (1989, "Rush",              "Show Don't Tell",              [("Rush", "Show Don't Tell")]),
    (1989, "Heart",             "All I Wanna Do Is Make Love to You", [("Heart", "All I Wanna Do")]),
    (1989, "Aerosmith",         "Love in an Elevator",          [("Aerosmith", "Love in an Elevator")]),
    (1989, "Alice Cooper",      "Poison",                       [("Alice Cooper", "Poison")]),
    (1989, "Don Henley",        "The End of the Innocence",     [("Compilations", "End Of The Innocence"),
                                                                  ("Don Henley", "End of the Innocence")]),
    (1989, "Tom Petty",         "Free Fallin'",                 [("Tom Petty", "Free Fallin")]),
    (1989, "Living Colour",     "Cult of Personality",          [("Living Colour", "Cult of Personality")]),
]

AUDIO_EXTENSIONS = {".mp3", ".MP3", ".m4a", ".m4p", ".flac", ".ogg", ".wav"}
VIDEO_EXTENSIONS = {".mp4", ".m4v"}
ALL_EXTENSIONS = AUDIO_EXTENSIONS | VIDEO_EXTENSIONS


import subprocess

def _extract_audio(src: Path, dst: Path):
    """Strip video stream, copy audio track to m4a."""
    cmd = [
        "ffmpeg", "-y", "-i", str(src),
        "-vn",           # no video
        "-acodec", "copy",  # copy audio stream without re-encoding
        str(dst)
    ]
    result = subprocess.run(cmd, capture_output=True, timeout=120)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg extract failed: {result.stderr.decode()[-300:]}")


def safe_filename(artist: str, title: str, ext: str) -> str:
    """Generate a clean Artist - Title.ext filename."""
    def clean(s):
        s = re.sub(r'[<>:"/\\|?*]', '', s)
        s = re.sub(r'\s+', ' ', s).strip()
        return s
    return f"{clean(artist)} - {clean(title)}{ext}"


def build_index(src_root: Path) -> list[dict]:
    """Walk src_root and build a searchable index of all audio files."""
    index = []
    for path in src_root.rglob("*"):
        if path.suffix.lower() not in ALL_EXTENSIONS:
            continue
        if path.name.startswith("."):
            continue
        rel = path.relative_to(src_root)
        parts = rel.parts
        index.append({
            "path": path,
            "rel": str(rel),
            "parts": parts,
            "title_norm": _norm(path.stem),
        })
    return index


def _norm(s: str) -> str:
    s = re.sub(r"^\d+[-_\s]+", "", s)   # strip leading track number
    s = re.sub(r"[^\w\s]", "", s.lower())
    s = re.sub(r"\s+", " ", s).strip()
    return s


def find_track(index: list, hints: list[tuple]) -> dict | None:
    """
    Try each (dir_fragment, title_fragment) hint in order.
    Returns first matching index entry.
    """
    for dir_hint, title_hint in hints:
        dir_norm = dir_hint.lower()
        title_norm = _norm(title_hint)
        for entry in index:
            rel_lower = entry["rel"].lower()
            if dir_norm not in rel_lower:
                continue
            if title_norm in entry["title_norm"] or entry["title_norm"] in title_norm:
                return entry
    return None


def organize(src_root: Path, dst_root: Path, dry_run: bool = False) -> dict:
    print(f"Indexing {src_root} ...")
    index = build_index(src_root)
    print(f"Found {len(index)} audio files\n")

    results = {"copied": [], "missing": [], "skipped": []}

    for (year, artist, title, hints) in TRACKLIST:
        match = find_track(index, hints)
        dst_dir = dst_root / str(year)
        src_suffix = match["path"].suffix.lower() if match else ".mp3"
        dst_suffix = ".m4a" if src_suffix in VIDEO_EXTENSIONS else src_suffix
        dst_file = dst_dir / safe_filename(artist, title, dst_suffix if match else ".mp3")

        if match:
            if dst_file.exists():
                results["skipped"].append((year, artist, title, dst_file))
            else:
                results["copied"].append((year, artist, title, match["path"], dst_file))
                if not dry_run:
                    dst_dir.mkdir(parents=True, exist_ok=True)
                    if match["path"].suffix.lower() in VIDEO_EXTENSIONS:
                        _extract_audio(match["path"], dst_file)
                    else:
                        shutil.copy2(match["path"], dst_file)
        else:
            results["missing"].append((year, artist, title))

    return results


def print_results(results: dict, dry_run: bool):
    tag = "[DRY RUN] " if dry_run else ""

    print(f"\n{'='*60}")
    print(f"WKRT ORGANIZE RESULTS {tag}")
    print(f"{'='*60}")
    print(f"  {'Would copy' if dry_run else 'Copied'}:  {len(results['copied'])}")
    print(f"  Skipped (exists): {len(results['skipped'])}")
    print(f"  Missing:          {len(results['missing'])}")

    if results["copied"]:
        print(f"\n── {'WOULD COPY' if dry_run else 'COPIED'} ──────────────────────────────")
        for year, artist, title, src, dst in sorted(results["copied"]):
            print(f"  {year}  {artist:<28} {title}")
            if dry_run:
                print(f"       src: {src}")
                print(f"       dst: {dst}")

    if results["missing"]:
        print(f"\n── MISSING (need to acquire) ──────────────────────────")
        by_artist = defaultdict(list)
        for year, artist, title in results["missing"]:
            by_artist[artist].append((year, title))
        for artist, tracks in sorted(by_artist.items(), key=lambda x: -len(x[1])):
            print(f"  {artist} ({len(tracks)} tracks)")
            for year, title in sorted(tracks):
                print(f"    {year}  {title}")


def verify(dst_root: Path):
    print(f"\n── VERIFY: {dst_root} ───────────────────────────────")
    by_year = defaultdict(list)
    for path in sorted(dst_root.rglob("*")):
        if path.suffix.lower() not in ALL_EXTENSIONS:
            continue
        by_year[path.parent.name].append(path.name)

    total = 0
    for year in sorted(by_year.keys()):
        count = len(by_year[year])
        total += count
        print(f"  {year}: {count:>3} tracks")
    print(f"  TOTAL: {total}")


def main():
    parser = argparse.ArgumentParser(description="WKRT Music Library Organizer")
    parser.add_argument("--src", required=True, help="Source music root directory")
    parser.add_argument("--dst", required=True, help="Destination WKRT music directory")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be copied, don't copy")
    parser.add_argument("--verify", action="store_true", help="Verify existing dst layout")
    args = parser.parse_args()

    src = Path(args.src)
    dst = Path(args.dst)

    if not src.exists():
        print(f"ERROR: Source directory not found: {src}")
        sys.exit(1)

    if args.verify:
        verify(dst)
        return

    results = organize(src, dst, dry_run=args.dry_run)
    print_results(results, dry_run=args.dry_run)

    if not args.dry_run and results["copied"]:
        print(f"\nDone. Run with --verify to confirm layout.")


if __name__ == "__main__":
    main()
