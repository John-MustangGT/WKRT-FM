#!/usr/bin/env python3
"""
wkrt_analyze.py — Library analyzer for WKRT-FM

Usage:
    # From your music root:
    find . -name "*.mp3" > mp3.out
    python3 wkrt_analyze.py mp3.out

    # Or point at a listing file:
    python3 wkrt_analyze.py /path/to/mp3.out --music-root /path/to/music

Outputs:
    - Coverage report by year
    - List of matched tracks (with source paths)
    - Shopping list of missing artists/albums
    - wkrt_library.json  — used by the engine to find files
"""

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

# ── WKRT Master Tracklist ────────────────────────────────────────────────────
# Format: (year, artist, title)
# Artist names normalized for fuzzy matching

TRACKLIST = [
    # 1980
    (1980, "AC/DC", "You Shook Me All Night Long"),
    (1980, "Pat Benatar", "Hit Me with Your Best Shot"),
    (1980, "Blondie", "Call Me"),
    (1980, "Queen", "Crazy Little Thing Called Love"),
    (1980, "Pink Floyd", "Another Brick in the Wall"),
    (1980, "Tom Petty", "Refugee"),
    (1980, "Bob Seger", "Against the Wind"),
    (1980, "Rush", "The Spirit of Radio"),
    (1980, "The Clash", "Train in Vain"),
    (1980, "Joan Jett", "Bad Reputation"),
    (1980, "Ozzy Osbourne", "Crazy Train"),
    (1980, "Billy Joel", "It's Still Rock and Roll to Me"),
    (1980, "Bruce Springsteen", "Hungry Heart"),
    (1980, "Heart", "Even It Up"),
    (1980, "Foreigner", "Dirty White Boy"),
    (1980, "Journey", "Any Way You Want It"),
    (1980, "Van Halen", "And the Cradle Will Rock"),
    (1980, "Scorpions", "No One Like You"),
    (1980, "Iron Maiden", "Sanctuary"),
    (1980, "Motörhead", "Ace of Spades"),
    (1980, "Judas Priest", "Breaking the Law"),
    (1980, "David Bowie", "Fashion"),
    (1980, "Talking Heads", "Once in a Lifetime"),
    (1980, "The Pretenders", "Brass in Pocket"),
    (1980, "ZZ Top", "I Thank You"),
    (1980, "Devo", "Whip It"),
    (1980, "Black Sabbath", "Neon Knights"),
    (1980, "Deep Purple", "Black Night"),
    # 1981
    (1981, "The Rolling Stones", "Start Me Up"),
    (1981, "REO Speedwagon", "Keep on Loving You"),
    (1981, "Foreigner", "Waiting for a Girl Like You"),
    (1981, "Journey", "Don't Stop Believin'"),
    (1981, "Tom Petty", "The Waiting"),
    (1981, "The Police", "Every Little Thing She Does Is Magic"),
    (1981, "Rush", "Tom Sawyer"),
    (1981, "Ozzy Osbourne", "Flying High Again"),
    (1981, "AC/DC", "Back in Black"),
    (1981, "Van Halen", "Unchained"),
    (1981, "Def Leppard", "Bringin' On the Heartbreak"),
    (1981, "Stevie Nicks", "Edge of Seventeen"),
    (1981, "Rick Springfield", "Jessie's Girl"),
    (1981, "Blondie", "The Tide Is High"),
    (1981, "Billy Squier", "The Stroke"),
    (1981, "Styx", "Too Much Time on My Hands"),
    (1981, "Bob Seger", "Feel Like a Number"),
    (1981, "Dire Straits", "Romeo and Juliet"),
    (1981, "Iron Maiden", "The Trooper"),
    (1981, "Judas Priest", "Heading Out to the Highway"),
    (1981, "Joan Jett", "I Love Rock 'n' Roll"),
    (1981, "David Bowie", "Up the Hill Backwards"),
    (1981, "The Pretenders", "Message of Love"),
    (1981, "The Cars", "Shake It Up"),
    (1981, "Loverboy", "Working for the Weekend"),
    (1981, "Sammy Hagar", "There's Only One Way to Rock"),
    (1981, "ZZ Top", "Leila"),
    (1981, "U2", "Gloria"),
    (1981, "The GoGo's", "Our Lips Are Sealed"),
    # 1982
    (1982, "Joan Jett", "I Love Rock 'n' Roll"),
    (1982, "Survivor", "Eye of the Tiger"),
    (1982, "Toto", "Rosanna"),
    (1982, "John Mellencamp", "Jack & Diane"),
    (1982, "The Police", "Every Breath You Take"),
    (1982, "Pat Benatar", "Shadows of the Night"),
    (1982, "Loverboy", "Working for the Weekend"),
    (1982, "Fleetwood Mac", "Hold Me"),
    (1982, "Steve Miller Band", "Abracadabra"),
    (1982, "The Cars", "You Might Think"),
    (1982, "Hall & Oates", "Maneater"),
    (1982, "Men at Work", "Who Can It Be Now?"),
    (1982, "Billy Squier", "Everybody Wants You"),
    (1982, "Peter Gabriel", "Shock the Monkey"),
    (1982, "Iron Maiden", "Run to the Hills"),
    (1982, "Judas Priest", "You've Got Another Thing Comin'"),
    (1982, "Scorpions", "No One Like You"),
    (1982, "Rush", "New World Man"),
    (1982, "Heart", "This Man Is Mine"),
    (1982, "REO Speedwagon", "Keep the Fire Burnin'"),
    (1982, "Foreigner", "Juke Box Hero"),
    (1982, "Sammy Hagar", "I Can't Drive 55"),
    (1982, "Don Henley", "Dirty Laundry"),
    (1982, "Tom Petty", "You Got Lucky"),
    (1982, "Quiet Riot", "Cum on Feel the Noize"),
    (1982, "Dio", "Rainbow in the Dark"),
    (1982, "U2", "New Year's Day"),
    (1982, "The Clash", "Should I Stay or Should I Go"),
    (1982, "Toto", "Africa"),
    (1982, "Def Leppard", "Photograph"),
    (1982, "ZZ Top", "Sharp Dressed Man"),
    # 1983
    (1983, "Def Leppard", "Photograph"),
    (1983, "The Police", "Every Breath You Take"),
    (1983, "Toto", "Africa"),
    (1983, "David Bowie", "Let's Dance"),
    (1983, "ZZ Top", "Sharp Dressed Man"),
    (1983, "Men at Work", "Down Under"),
    (1983, "Quiet Riot", "Metal Health"),
    (1983, "Dio", "Holy Diver"),
    (1983, "Mötley Crüe", "Looks That Kill"),
    (1983, "Ozzy Osbourne", "Bark at the Moon"),
    (1983, "Iron Maiden", "The Trooper"),
    (1983, "Rush", "Distant Early Warning"),
    (1983, "Pat Benatar", "Love Is a Battlefield"),
    (1983, "Night Ranger", "Sister Christian"),
    (1983, "Whitesnake", "Fool for Your Loving"),
    (1983, "Scorpions", "Rock You Like a Hurricane"),
    (1983, "Heart", "How Can I Refuse"),
    (1983, "Don Henley", "The Boys of Summer"),
    (1983, "Stevie Nicks", "Stand Back"),
    (1983, "Tom Petty", "Change of Heart"),
    (1983, "Bob Seger", "Roll Me Away"),
    (1983, "Billy Joel", "Tell Her About It"),
    (1983, "U2", "Sunday Bloody Sunday"),
    (1983, "The Clash", "Should I Stay or Should I Go"),
    (1983, "Duran Duran", "Hungry Like the Wolf"),
    (1983, "R.E.M.", "Radio Free Europe"),
    (1983, "Big Country", "In a Big Country"),
    (1983, "Simple Minds", "Waterfront"),
    (1983, "New Order", "Blue Monday"),
    # 1984
    (1984, "Van Halen", "Jump"),
    (1984, "Def Leppard", "Rock of Ages"),
    (1984, "Bruce Springsteen", "Born in the U.S.A."),
    (1984, "Twisted Sister", "We're Not Gonna Take It"),
    (1984, "Scorpions", "Rock You Like a Hurricane"),
    (1984, "Mötley Crüe", "Shout at the Devil"),
    (1984, "Quiet Riot", "Metal Health"),
    (1984, "Dio", "The Last in Line"),
    (1984, "Iron Maiden", "2 Minutes to Midnight"),
    (1984, "Judas Priest", "Some Heads Are Gonna Roll"),
    (1984, "Night Ranger", "Sister Christian"),
    (1984, "Don Henley", "The Boys of Summer"),
    (1984, "Pat Benatar", "We Belong"),
    (1984, "Bryan Adams", "Run to You"),
    (1984, "John Mellencamp", "Pink Houses"),
    (1984, "Heart", "Nothin' at All"),
    (1984, "Rush", "The Enemy Within"),
    (1984, "Sammy Hagar", "I Can't Drive 55"),
    (1984, "Kenny Loggins", "Footloose"),
    (1984, "Duran Duran", "The Reflex"),
    (1984, "The Cars", "Drive"),
    (1984, "U2", "Pride (In the Name of Love)"),
    (1984, "R.E.M.", "Don't Go Back to Rockville"),
    (1984, "Talking Heads", "Burning Down the House"),
    (1984, "Billy Idol", "Eyes Without a Face"),
    (1984, "ZZ Top", "Legs"),
    (1984, "Ratt", "Round and Round"),
    (1984, "AC/DC", "Guns for Hire"),
    (1984, "Autograph", "Turn Up the Radio"),
    (1984, "Queensrÿche", "Queen of the Reich"),
    # 1985
    (1985, "Bryan Adams", "Summer of '69"),
    (1985, "Dire Straits", "Money for Nothing"),
    (1985, "John Mellencamp", "Lonely Ol' Night"),
    (1985, "Tears for Fears", "Shout"),
    (1985, "Simple Minds", "Don't You (Forget About Me)"),
    (1985, "Mötley Crüe", "Home Sweet Home"),
    (1985, "Ratt", "Lay It Down"),
    (1985, "Whitesnake", "Love Ain't No Stranger"),
    (1985, "Dokken", "Alone Again"),
    (1985, "Iron Maiden", "Aces High"),
    (1985, "Ozzy Osbourne", "Shot in the Dark"),
    (1985, "Heart", "What About Love?"),
    (1985, "Pat Benatar", "Invincible"),
    (1985, "Don Henley", "All She Wants to Do Is Dance"),
    (1985, "Tom Petty", "Don't Come Around Here No More"),
    (1985, "Bruce Springsteen", "I'm on Fire"),
    (1985, "ZZ Top", "Sleeping Bag"),
    (1985, "Billy Joel", "You're Only Human"),
    (1985, "Van Halen", "Panama"),
    (1985, "Rush", "The Big Money"),
    (1985, "U2", "The Unforgettable Fire"),
    (1985, "R.E.M.", "Can't Get There from Here"),
    (1985, "Duran Duran", "A View to a Kill"),
    (1985, "Tears for Fears", "Everybody Wants to Rule the World"),
    (1985, "Billy Idol", "Rebel Yell"),
    (1985, "Talking Heads", "Road to Nowhere"),
    (1985, "John Mellencamp", "Small Town"),
    (1985, "Scorpions", "Still Loving You"),
    (1985, "AC/DC", "Fly on the Wall"),
    (1985, "Cinderella", "Shake Me"),
    # 1986
    (1986, "Van Halen", "Why Can't This Be Love"),
    (1986, "Bon Jovi", "You Give Love a Bad Name"),
    (1986, "Whitesnake", "Still of the Night"),
    (1986, "Guns N' Roses", "Welcome to the Jungle"),
    (1986, "Mötley Crüe", "Dr. Feelgood"),
    (1986, "Ratt", "Dance"),
    (1986, "Dokken", "Dream Warriors"),
    (1986, "Iron Maiden", "Wasted Years"),
    (1986, "Judas Priest", "Turbo Lover"),
    (1986, "Ozzy Osbourne", "The Ultimate Sin"),
    (1986, "AC/DC", "Who Made Who"),
    (1986, "Heart", "Alone"),
    (1986, "Pat Benatar", "Sex as a Weapon"),
    (1986, "Tom Petty", "Jammin' Me"),
    (1986, "Cinderella", "Nobody's Fool"),
    (1986, "Poison", "Talk Dirty to Me"),
    (1986, "Night Ranger", "Four in the Morning"),
    (1986, "Queensrÿche", "Walk in the Shadows"),
    (1986, "Tesla", "Modern Day Cowboy"),
    (1986, "Rush", "Time Stand Still"),
    (1986, "U2", "With or Without You"),
    (1986, "R.E.M.", "Fall on Me"),
    (1986, "Peter Gabriel", "Sledgehammer"),
    (1986, "Simple Minds", "Alive and Kicking"),
    (1986, "John Mellencamp", "R.O.C.K. in the U.S.A."),
    (1986, "Billy Idol", "To Be a Lover"),
    (1986, "ZZ Top", "Rough Boy"),
    (1986, "Bob Seger", "Miami"),
    (1986, "Depeche Mode", "Stripped"),
    (1986, "Dire Straits", "Walk of Life"),
    # 1987
    (1987, "Bon Jovi", "Livin' on a Prayer"),
    (1987, "Guns N' Roses", "Sweet Child O' Mine"),
    (1987, "Whitesnake", "Here I Go Again"),
    (1987, "Def Leppard", "Pour Some Sugar on Me"),
    (1987, "Mötley Crüe", "Girls, Girls, Girls"),
    (1987, "Poison", "Talk Dirty to Me"),
    (1987, "Heart", "Alone"),
    (1987, "Van Halen", "Dreams"),
    (1987, "Tesla", "Modern Day Cowboy"),
    (1987, "Cinderella", "Don't Know What You Got"),
    (1987, "Warrant", "Down Boys"),
    (1987, "Winger", "Seventeen"),
    (1987, "Iron Maiden", "Can I Play with Madness"),
    (1987, "Ozzy Osbourne", "Crazy Babies"),
    (1987, "AC/DC", "Heatseeker"),
    (1987, "Scorpions", "Rhythm of Love"),
    (1987, "Night Ranger", "The Secret of My Success"),
    (1987, "Pat Benatar", "All Fired Up"),
    (1987, "Tom Petty", "Running Down a Dream"),
    (1987, "Bruce Springsteen", "Tunnel of Love"),
    (1987, "John Mellencamp", "Check It Out"),
    (1987, "Bryan Adams", "Heat of the Night"),
    (1987, "U2", "Where the Streets Have No Name"),
    (1987, "R.E.M.", "The One I Love"),
    (1987, "Peter Gabriel", "Big Time"),
    (1987, "Crowded House", "Don't Dream It's Over"),
    (1987, "INXS", "Need You Tonight"),
    (1987, "Depeche Mode", "Never Let Me Down Again"),
    (1987, "Billy Idol", "Sweet Sixteen"),
    (1987, "ZZ Top", "Doubleback"),
    (1987, "Bob Seger", "Shakedown"),
    (1987, "Rush", "Force Ten"),
    (1987, "Dire Straits", "Money for Nothing"),
    # 1988
    (1988, "Guns N' Roses", "Paradise City"),
    (1988, "Bon Jovi", "Bad Medicine"),
    (1988, "Def Leppard", "Love Bites"),
    (1988, "Van Halen", "Hot for Teacher"),
    (1988, "Mötley Crüe", "Dr. Feelgood"),
    (1988, "Poison", "Every Rose Has Its Thorn"),
    (1988, "Whitesnake", "Is This Love"),
    (1988, "Cinderella", "Gypsy Road"),
    (1988, "Warrant", "Heaven"),
    (1988, "Winger", "Seventeen"),
    (1988, "Skid Row", "18 and Life"),
    (1988, "Tesla", "Love Song"),
    (1988, "Iron Maiden", "The Evil That Men Do"),
    (1988, "Ozzy Osbourne", "No More Tears"),
    (1988, "Judas Priest", "Ram It Down"),
    (1988, "AC/DC", "Heatseeker"),
    (1988, "Scorpions", "Passion Rules the Game"),
    (1988, "Tom Petty", "Running Down a Dream"),
    (1988, "U2", "Desire"),
    (1988, "R.E.M.", "Orange Crush"),
    (1988, "INXS", "Devil Inside"),
    (1988, "The Cult", "Fire Woman"),
    (1988, "Depeche Mode", "Personal Jesus"),
    (1988, "New Order", "True Faith"),
    (1988, "Billy Idol", "Catch My Fall"),
    (1988, "Don Henley", "The End of the Innocence"),
    (1988, "ZZ Top", "Velcro Fly"),
    (1988, "Midnight Oil", "Beds Are Burning"),
    (1988, "Rush", "Superconductor"),
    (1988, "Night Ranger", "When You Close Your Eyes"),
    # 1989
    (1989, "Bon Jovi", "I'll Be There for You"),
    (1989, "Guns N' Roses", "Patience"),
    (1989, "Mötley Crüe", "Dr. Feelgood"),
    (1989, "Poison", "Every Rose Has Its Thorn"),
    (1989, "Skid Row", "Youth Gone Wild"),
    (1989, "Warrant", "Heaven"),
    (1989, "Tesla", "Love Song"),
    (1989, "Cinderella", "Don't Know What You Got (Till It's Gone)"),
    (1989, "Def Leppard", "Rocket"),
    (1989, "Van Halen", "When It's Love"),
    (1989, "Queensrÿche", "Silent Lucidity"),
    (1989, "Iron Maiden", "Infinite Dreams"),
    (1989, "Ozzy Osbourne", "Mama, I'm Coming Home"),
    (1989, "AC/DC", "Thunderstruck"),
    (1989, "Scorpions", "Wind of Change"),
    (1989, "Judas Priest", "A Touch of Evil"),
    (1989, "Tom Petty", "Free Fallin'"),
    (1989, "John Mellencamp", "Pop Singer"),
    (1989, "Don Henley", "The End of the Innocence"),
    (1989, "Bryan Adams", "Do I Have to Say the Words?"),
    (1989, "U2", "Angel of Harlem"),
    (1989, "R.E.M.", "Stand"),
    (1989, "INXS", "Never Tear Us Apart"),
    (1989, "The Cult", "Fire Woman"),
    (1989, "Depeche Mode", "Policy of Truth"),
    (1989, "Billy Idol", "Cradle of Love"),
    (1989, "Bob Seger", "Understanding"),
    (1989, "Rush", "Show Don't Tell"),
    (1989, "Heart", "All I Wanna Do Is Make Love to You"),
    (1989, "Aerosmith", "Love in an Elevator"),
    (1989, "Alice Cooper", "Poison"),
    (1989, "Living Colour", "Cult of Personality"),
    (1989, "Faith No More", "Epic"),
]

# ── Artist name normalization ────────────────────────────────────────────────

# Maps library directory names → canonical artist names used in tracklist
ARTIST_ALIASES = {
    "AC_DC":                         "AC/DC",
    "Tom Petty & the Heartbreakers": "Tom Petty",
    "Tom Petty And The Heartbreakers":"Tom Petty",
    "R.E.M_":                        "R.E.M.",
    "John Cougar Mellencamp":        "John Mellencamp",
    "John Cougar":                   "John Mellencamp",
    "Motley Crue":                   "Mötley Crüe",
    "Motorhead":                     "Motörhead",
    "Queensryche":                   "Queensrÿche",
    "The Rolling Stones":            "The Rolling Stones",
    "Fleetwood Mac":                 "Fleetwood Mac",
    "Simple Minds":                  "Simple Minds",
    "Talking Heads":                 "Talking Heads",
    "The Police":                    "The Police",
    "The Clash":                     "The Clash",
    "Depeche Mode":                  "Depeche Mode",
    "Styx":                          "Styx",
    "Journey":                       "Journey",
    "ZZ Top":                        "ZZ Top",
    "U2":                            "U2",
    "Aerosmith":                     "Aerosmith",
    "Alice Cooper":                  "Alice Cooper",
    "Tina Turner":                   "Tina Turner",
    "Lynyrd Skynyrd":                "Lynyrd Skynyrd",
}

def normalize_artist(name: str) -> str:
    return ARTIST_ALIASES.get(name, name).lower().strip()

def normalize_title(title: str) -> str:
    """Lowercase, strip track numbers, strip punctuation for fuzzy compare."""
    t = title.lower()
    t = re.sub(r"^\d+\s+", "", t)          # leading track number
    t = re.sub(r"[^\w\s]", "", t)          # strip punctuation
    t = re.sub(r"\s+", " ", t).strip()
    return t


# ── Library parsing ──────────────────────────────────────────────────────────

def parse_library(listing_file: str) -> dict:
    """
    Parse find/ls output into:
    { normalized_artist: [ { artist, album, title, path } ] }
    """
    lib = defaultdict(list)
    with open(listing_file) as f:
        for line in f:
            path = line.strip().lstrip("./")
            if Path(path).suffix.lower() not in {".mp3", ".m4a", ".m4p", ".flac", ".ogg", ".wav"}:
                continue
            parts = path.split("/")
            if len(parts) < 3:
                continue
            artist_dir = parts[0]
            album = parts[1]
            filename = parts[-1]
            # Strip track number and extension from filename
            title = re.sub(r"^\d+\s+", "", Path(filename).stem).strip()
            norm = normalize_artist(artist_dir)
            lib[norm].append({
                "artist_dir": artist_dir,
                "artist": ARTIST_ALIASES.get(artist_dir, artist_dir),
                "album": album,
                "title": title,
                "norm_title": normalize_title(title),
                "path": path,
            })
    return lib


# ── Matching ─────────────────────────────────────────────────────────────────

def title_match(want: str, have: str) -> bool:
    """Fuzzy title match — normalized, checks if want is substring of have or vice versa."""
    w = normalize_title(want)
    h = normalize_title(have)
    return w == h or w in h or h in w


def find_match(year: int, artist: str, title: str, lib: dict) -> dict | None:
    norm_artist = normalize_artist(artist).lower()
    candidates = lib.get(norm_artist, [])
    for track in candidates:
        if title_match(title, track["title"]):
            return track
    return None


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="WKRT Library Analyzer")
    parser.add_argument("listing", help="Path to mp3.out (find . -name '*.mp3' output)")
    parser.add_argument("--music-root", default=".", help="Root path to prepend to matched paths")
    parser.add_argument("--out", default="wkrt_library.json", help="Output JSON file")
    parser.add_argument("--shopping-list", default="shopping_list.txt", help="Output shopping list")
    args = parser.parse_args()

    print(f"Parsing library from {args.listing}...")
    lib = parse_library(args.listing)
    print(f"Found {sum(len(v) for v in lib.values())} tracks across {len(lib)} artists\n")

    matched = []
    missing_by_artist = defaultdict(list)
    year_stats = defaultdict(lambda: {"have": 0, "missing": 0})

    for (year, artist, title) in TRACKLIST:
        m = find_match(year, artist, title, lib)
        if m:
            matched.append({
                "year": year,
                "artist": artist,
                "title": title,
                "path": str(Path(args.music_root) / m["path"]),
                "album": m["album"],
            })
            year_stats[year]["have"] += 1
        else:
            missing_by_artist[artist].append((year, title))
            year_stats[year]["missing"] += 1

    # ── Print coverage report ────────────────────────────────────────────────
    print("=" * 60)
    print("WKRT COVERAGE REPORT")
    print("=" * 60)
    total_want = len(TRACKLIST)
    total_have = len(matched)
    print(f"Overall: {total_have}/{total_want} tracks matched "
          f"({100*total_have//total_want}%)\n")

    print(f"{'Year':<6} {'Have':>5} {'Miss':>5} {'Coverage':>10}")
    print("-" * 30)
    for year in sorted(year_stats.keys()):
        s = year_stats[year]
        total = s["have"] + s["missing"]
        pct = 100 * s["have"] // total if total else 0
        bar = "█" * (s["have"] * 20 // total) if total else ""
        print(f"{year:<6} {s['have']:>5} {s['missing']:>5}   {pct:>3}%  {bar}")

    # ── Write wkrt_library.json ──────────────────────────────────────────────
    # Organized by year for the engine
    by_year = defaultdict(list)
    for t in matched:
        by_year[t["year"]].append(t)

    with open(args.out, "w") as f:
        json.dump({str(y): by_year[y] for y in sorted(by_year.keys())}, f, indent=2)
    print(f"\n✓ Library written to {args.out} ({len(matched)} tracks)")

    # ── Write shopping list ──────────────────────────────────────────────────
    with open(args.shopping_list, "w") as f:
        f.write("WKRT-FM APPLE MUSIC SHOPPING LIST\n")
        f.write("=" * 60 + "\n\n")
        f.write(f"Missing {total_want - total_have} of {total_want} tracks\n\n")

        # Group by artist, sort by how many tracks we're missing
        f.write("── BY ARTIST (most needed first) ──────────────────────\n\n")
        for artist, tracks in sorted(missing_by_artist.items(),
                                     key=lambda x: -len(x[1])):
            f.write(f"{artist} ({len(tracks)} tracks missing)\n")
            for year, title in sorted(tracks):
                f.write(f"  {year}  {title}\n")
            f.write("\n")

    print(f"✓ Shopping list written to {args.shopping_list}")

    # ── Quick artist summary to terminal ────────────────────────────────────
    print("\n── MISSING ARTISTS (buy these first) ──────────────────")
    top_missing = sorted(missing_by_artist.items(), key=lambda x: -len(x[1]))[:15]
    for artist, tracks in top_missing:
        print(f"  {artist:<30} {len(tracks):>3} tracks missing")


if __name__ == "__main__":
    main()
