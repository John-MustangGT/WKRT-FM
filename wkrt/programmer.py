"""
wkrt/programmer.py — DJ block programmer.

Each DJ maintains six curated track lists:
  favorites   — all-time personal picks, used every block
  morning     — 5 am – 10 am
  midday      — 10 am – 2 pm
  afternoon   — 2 pm – 7 pm
  evening     — 7 pm – 10 pm
  night       — 10 pm – 5 am

When programming a block the active DJ's favorites + current time-slot list
(40 tracks total) are combined with the user's saved favorites (up to 20)
and a random fill (20) to form a curated pool of ~80 candidates.
Claude then picks BLOCK_SIZE tracks from that pool.
"""
import datetime
import json
import logging
import random
import re
import threading
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import anthropic

from .playlist import Track

log = logging.getLogger(__name__)

SLOTS = ["favorites", "morning", "midday", "afternoon", "evening", "night"]

_SLOT_DESC = {
    "favorites":  "all-time personal favorites — tracks you'd reach for first any time of day",
    "morning":    "morning shift (5 am–10 am) — upbeat, energizing, gets people moving",
    "midday":     "midday (10 am–2 pm) — solid reliable rock, steady energy, good variety",
    "afternoon":  "afternoon drive (2 pm–7 pm) — building energy, sing-along anthems",
    "evening":    "evening (7 pm–10 pm) — laid-back but still rocking, slightly reflective",
    "night":      "late night (10 pm–5 am) — deeper cuts, darker edge, more intense",
}


def current_time_slot(tz_name: str = "UTC") -> str:
    hour = datetime.datetime.now(ZoneInfo(tz_name)).hour
    if 5 <= hour < 10:  return "morning"
    elif hour < 14:     return "midday"
    elif hour < 19:     return "afternoon"
    elif hour < 22:     return "evening"
    else:               return "night"


# ── Fuzzy matching ────────────────────────────────────────────────────────────

def _norm(s: str) -> str:
    return re.sub(r'[^a-z0-9]', '', s.lower())


def fuzzy_match(artist: str, title: str, library: dict) -> Optional[Track]:
    """Return the best-matching Track from library, or None if score < 0.70."""
    na, nt = _norm(artist), _norm(title)
    best_score, best_track = 0.0, None
    for tracks in library.values():
        for t in tracks:
            a = SequenceMatcher(None, na, _norm(t.artist)).ratio()
            b = SequenceMatcher(None, nt, _norm(t.title)).ratio()
            score = (a + b) / 2
            if score > best_score:
                best_score, best_track = score, t
    return best_track if best_score >= 0.70 else None


# ── DJProgrammer ──────────────────────────────────────────────────────────────

class DJProgrammer:
    TRACKS_PER_SLOT = 20
    BLOCK_SIZE      = 6
    RANDOM_FILL     = 20

    def __init__(self, cfg: dict, config_dir: Path):
        self.cfg        = cfg
        self.config_dir = config_dir
        api_key         = cfg["api"].get("api_key", "")
        self.client     = anthropic.Anthropic(api_key=api_key) if api_key else None
        self._regen_lock = threading.Lock()  # one regen at a time per process

    # ── Paths ─────────────────────────────────────────────────────────────────

    def dj_favorites_path(self, dj_name: str) -> Path:
        return self.config_dir / f"favorites_{dj_name.lower()}.json"

    def user_favorites_path(self) -> Path:
        return self.config_dir / "favorites_user.json"

    # ── Load / save ───────────────────────────────────────────────────────────

    def load_dj_favorites(self, dj_name: str) -> dict:
        path = self.dj_favorites_path(dj_name)
        if path.exists():
            try:
                return json.loads(path.read_text())
            except Exception:
                pass
        return {}

    def save_dj_favorites(self, dj_name: str, data: dict):
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.dj_favorites_path(dj_name).write_text(json.dumps(data, indent=2))
        log.info(f"DJ favorites saved: {dj_name}")

    def load_user_favorites(self) -> list:
        path = self.user_favorites_path()
        if path.exists():
            try:
                return json.loads(path.read_text())
            except Exception:
                pass
        return []

    def save_user_favorites(self, tracks: list):
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.user_favorites_path().write_text(json.dumps(tracks, indent=2))

    def add_user_favorite(self, artist: str, title: str, year: int):
        favs = self.load_user_favorites()
        key = (_norm(artist), _norm(title))
        if not any((_norm(f["artist"]), _norm(f["title"])) == key for f in favs):
            favs.append({"artist": artist, "title": title, "year": year})
            self.save_user_favorites(favs)

    def remove_user_favorite(self, artist: str, title: str):
        key = (_norm(artist), _norm(title))
        favs = [f for f in self.load_user_favorites()
                if (_norm(f["artist"]), _norm(f["title"])) != key]
        self.save_user_favorites(favs)

    # ── Library summary ───────────────────────────────────────────────────────

    def _library_summary(self, library: dict) -> str:
        artists: dict[str, list[str]] = {}
        for tracks in library.values():
            for t in tracks:
                artists.setdefault(t.artist, []).append(f'"{t.title}" ({t.year})')
        return "\n".join(
            f"{a}: {', '.join(sorted(ts))}"
            for a, ts in sorted(artists.items())
        )

    # ── Slot generation ───────────────────────────────────────────────────────

    def generate_slot(self, dj_cfg: dict, library: dict, slot: str) -> list[dict]:
        """Ask Claude to pick TRACKS_PER_SLOT tracks for one slot. Returns raw dicts."""
        if not self.client:
            return []
        n    = self.TRACKS_PER_SLOT
        desc = _SLOT_DESC[slot]
        lib  = self._library_summary(library)
        prompt = (
            f"Here is the complete music library for WKRT 104.7:\n\n{lib}\n\n"
            f'Pick exactly {n} tracks for your "{slot}" playlist — {desc}.\n'
            f"Choose based on your DJ personality and what genuinely fits this slot.\n"
            f"Return ONLY a JSON array, no other text:\n"
            f'[{{"artist": "...", "title": "...", "year": ...}}, ...]'
        )
        try:
            resp = self.client.messages.create(
                model=self.cfg["api"]["model"],
                max_tokens=1200,
                system=dj_cfg["persona"].strip(),
                messages=[{"role": "user", "content": prompt}],
            )
            text = resp.content[0].text.strip()
            m = re.search(r'\[.*?\]', text, re.DOTALL)
            if not m:
                log.error(f"No JSON in slot response for {dj_cfg['name']}/{slot}")
                return []
            return json.loads(m.group())
        except Exception as e:
            log.error(f"Slot generation failed {dj_cfg['name']}/{slot}: {e}")
            return []

    def generate_all_slots(self, dj_cfg: dict, library: dict) -> dict:
        """Generate all 6 slots for one DJ. Blocks — call from a background thread."""
        result = {}
        for slot in SLOTS:
            log.info(f"Generating '{slot}' favorites for {dj_cfg['name']}…")
            picks = self.generate_slot(dj_cfg, library, slot)
            result[slot] = picks
            log.info(f"  {dj_cfg['name']}/{slot}: {len(picks)} tracks selected")
        return result

    # ── Candidate pool ────────────────────────────────────────────────────────

    def build_candidate_pool(
        self,
        dj_cfg:         dict,
        library:        dict,
        time_slot:      str,
        user_favorites: list,
    ) -> list[Track]:
        """DJ favorites + time-slot picks + user favorites + random fill."""
        dj_favs = self.load_dj_favorites(dj_cfg["name"])
        seen:  set[str]    = set()
        pool: list[Track]  = []

        def _add(raw: list):
            for item in raw:
                t = fuzzy_match(item.get("artist", ""), item.get("title", ""), library)
                if t:
                    key = f"{_norm(t.artist)}:{_norm(t.title)}"
                    if key not in seen:
                        seen.add(key)
                        pool.append(t)

        _add(dj_favs.get("favorites", []))
        _add(dj_favs.get(time_slot, []))
        _add(user_favorites)

        # Random fill from the rest of the library
        all_tracks = [t for ts in library.values() for t in ts]
        remaining  = [t for t in all_tracks
                      if f"{_norm(t.artist)}:{_norm(t.title)}" not in seen]
        random.shuffle(remaining)
        pool.extend(remaining[:self.RANDOM_FILL])

        return pool

    # ── Block programming ─────────────────────────────────────────────────────

    def program_block(
        self,
        dj_cfg:         dict,
        library:        dict,
        time_slot:      str,
        context:        Optional[dict],
        recent:         list,          # list of {artist, title} dicts
        user_favorites: list,
    ) -> list[Track]:
        """Ask Claude to program the next BLOCK_SIZE tracks from the candidate pool."""
        pool = self.build_candidate_pool(dj_cfg, library, time_slot, user_favorites)
        if not pool:
            pool = [t for ts in library.values() for t in ts]

        if not self.client:
            random.shuffle(pool)
            return pool[:self.BLOCK_SIZE]

        station  = self.cfg["station"]
        n        = self.BLOCK_SIZE
        cand_str = "\n".join(f'- {t.artist}: "{t.title}" ({t.year})' for t in pool)

        recent_str = ""
        if recent:
            recent_str = "\nRecently played (avoid repeating these soon):\n" + "\n".join(
                f'- {r["artist"]}: "{r["title"]}"' for r in recent[-8:]
            )

        ctx_parts = []
        if context:
            w = context.get("weather", {})
            if w:
                ctx_parts.append(f"Weather: {w.get('temp_f')}°F, {w.get('conditions')}")
            if context.get("sports"):
                ctx_parts.append(f"Sports: {context['sports']}")
            if context.get("live_context"):
                ctx_parts.append(f"Breaking: {context['live_context']}")
        ctx_str = ("\n" + "\n".join(ctx_parts)) if ctx_parts else ""

        prompt = (
            f"You're programming the next {n} tracks for "
            f"{station['call_sign']}-FM {station['frequency']} during {time_slot}.\n\n"
            f"Available tracks:\n{cand_str}"
            f"{recent_str}{ctx_str}\n\n"
            f"Pick {n} tracks that flow well together and suit your personality "
            f"and the {time_slot} vibe. Think about energy arc and variety.\n"
            f"Return ONLY a JSON array:\n"
            f'[{{"artist": "...", "title": "...", "year": ...}}, ...]'
        )

        try:
            resp = self.client.messages.create(
                model=self.cfg["api"]["model"],
                max_tokens=600,
                system=dj_cfg["persona"].strip(),
                messages=[{"role": "user", "content": prompt}],
            )
            text = resp.content[0].text.strip()
            m = re.search(r'\[.*?\]', text, re.DOTALL)
            if not m:
                raise ValueError("No JSON array in block response")
            picks = json.loads(m.group())

            result: list[Track] = []
            used: set[str] = set()
            for pick in picks:
                t = fuzzy_match(pick.get("artist", ""), pick.get("title", ""), library)
                if t:
                    key = f"{_norm(t.artist)}:{_norm(t.title)}"
                    if key not in used:
                        used.add(key)
                        result.append(t)

            if result:
                log.info(
                    f"{dj_cfg['name']} programmed {len(result)} tracks ({time_slot}): "
                    + " → ".join(f"{t.artist} — {t.title}" for t in result)
                )
                return result

        except Exception as e:
            log.error(f"Block programming failed for {dj_cfg['name']}: {e}")

        # Fallback: shuffled pool
        random.shuffle(pool)
        return pool[:self.BLOCK_SIZE]
