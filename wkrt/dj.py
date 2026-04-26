"""
DJ script generator.
Calls Claude API to generate contextual radio DJ banter.
Returns plain text scripts ready for TTS.
"""
import datetime
import random
import logging
import time
from enum import Enum
from dataclasses import dataclass
from typing import Optional
from zoneinfo import ZoneInfo

import anthropic

from .playlist import Track

log = logging.getLogger(__name__)


class ClipType(Enum):
    BETWEEN_TRACKS = "between_tracks"
    TRIVIA = "trivia"
    DEDICATION = "dedication"
    STATION_ID = "station_id"
    TOP_OF_HOUR = "top_of_hour"
    CONNECT_ID = "connect_id"
    NEW_ARRIVAL = "new_arrival"


@dataclass
class DJScript:
    text: str
    clip_type: ClipType
    prev_track: Optional[Track]
    next_track: Optional[Track]


_PROMPTS = {
    ClipType.BETWEEN_TRACKS: """
Generate a short radio DJ break (3-5 sentences max).
Just played: "{prev_artist}" - "{prev_title}" ({prev_year})
Coming up next: "{next_artist}" - "{next_title}" ({next_year})

Rules:
- Mention the song that just played and/or what's coming up
- Include one real interesting fact about the artist or song if you know one
- Keep it natural and conversational, not hype-y
- Reference {city} or the commute occasionally (not every time)
- Do NOT say "stay tuned" or "don't go anywhere"
- Output ONLY the spoken text, no stage directions, no quotes around it
""",

    ClipType.TRIVIA: """
Generate a short rock trivia DJ drop (2-4 sentences).
Just played: "{prev_artist}" - "{prev_title}" ({prev_year})

Rules:
- Share one genuinely interesting fact about this song, album, or artist
- Could be recording history, chart performance, band drama, or cultural impact
- Sound like you're sharing something cool you know, not reading from Wikipedia
- Output ONLY the spoken text, no stage directions
""",

    ClipType.DEDICATION: """
Generate a fake listener dedication break (3-5 sentences).
Coming up next: "{next_artist}" - "{next_title}" ({next_year})

Rules:
- Invent a listener name and a simple dedication (birthday, anniversary, long commute, etc.)
- Keep the name normal — no joke names
- Briefly tease the song
- Keep it warm but not saccharine
- Output ONLY the spoken text
""",

    ClipType.TOP_OF_HOUR: """
Generate a top-of-the-hour station ID (2-4 sentences).
Station: {call_sign} {frequency} FM, {city}
Time: {hour} o'clock

Rules:
- State the time and call sign naturally
- Reference what's coming up — more classic rock, a block of a specific artist or era
- Could mention the day of the week if relevant
- Warm and authoritative, like a real FM jock
- Output ONLY the spoken text
""",

    ClipType.CONNECT_ID: """
Generate a station connect greeting (1-2 sentences max).
Station: {call_sign} {frequency} FM, {city}

Rules:
- Welcome the listener back to the station
- Keep it very short — this plays the moment someone tunes in
- Could reference time of day: {time_of_day}
- Tease that great music is coming
- Output ONLY the spoken text
""",

    ClipType.STATION_ID: """
Generate a short station ID/time break (1-3 sentences).
Station: {call_sign} {frequency} FM, {city}
Time of day hint: {time_of_day}

Rules:
- Give the call sign and frequency naturally
- Optionally mention time of day or a brief weather vibe
- Could tease that more rock is coming
- Output ONLY the spoken text
""",

    ClipType.NEW_ARRIVAL: """
Generate a DJ break announcing a fresh addition to the station's rotation (2-4 sentences).
Just dropped into the crate: "{next_artist}" - "{next_title}" ({next_year})

Rules:
- Make it clear this track just landed — "just added to the crate", "fresh drop", "just dug this one out"
- Sound genuinely excited, like you personally picked it
- Tease something interesting about the song or artist if you know it
- Do NOT say "stay tuned" or "don't go anywhere"
- Output ONLY the spoken text
""",
}

def _time_of_day(tz_name: str) -> str:
    hour = datetime.datetime.now(ZoneInfo(tz_name)).hour
    if   5 <= hour < 10: return "morning drive"
    elif hour < 12:      return "mid-morning"
    elif hour < 15:      return "afternoon"
    elif hour < 19:      return "afternoon drive"
    elif hour < 22:      return "evening"
    else:                return "late night"


def _select_clip_type(weights: dict) -> ClipType:
    types = list(weights.keys())
    probs = list(weights.values())
    chosen = random.choices(types, weights=probs, k=1)[0]
    return ClipType(chosen)


_API_FAILURE_THRESHOLD = 3       # consecutive failures before marking unhealthy
_API_RETRY_INTERVAL   = 300.0   # seconds before retrying after going unhealthy


class DJEngine:
    def __init__(self, cfg: dict, dj_cfg: dict, stats=None):
        self.cfg = cfg
        self.dj_cfg = dj_cfg
        self.name = dj_cfg["name"]
        api_key = cfg["api"].get("api_key", "")
        self.client = anthropic.Anthropic(api_key=api_key) if api_key else None
        self.persona = dj_cfg["persona"].strip()
        self.station = cfg["station"]
        self.clip_weights = dj_cfg["clip_types"]
        self.timezone = cfg["station"].get("timezone", "UTC")
        self._stats = stats

        # API health tracking
        self._consecutive_failures = 0
        self._api_healthy = bool(api_key)
        self._unhealthy_since: Optional[float] = None   # monotonic timestamp

    @property
    def is_api_healthy(self) -> bool:
        return self._api_healthy

    def should_retry_api(self) -> bool:
        """True if we should attempt an API call even when marked unhealthy."""
        if self._api_healthy:
            return True
        if self._unhealthy_since is None:
            return True
        return time.monotonic() - self._unhealthy_since >= _API_RETRY_INTERVAL

    def generate(
        self,
        prev_track: Optional[Track] = None,
        next_track: Optional[Track] = None,
        force_type: Optional[ClipType] = None,
        context: Optional[dict] = None,
    ) -> DJScript:
        clip_type = force_type or _select_clip_type(self.clip_weights)

        # Fallback to station_id if we don't have the tracks we need
        if clip_type == ClipType.BETWEEN_TRACKS and (not prev_track or not next_track):
            clip_type = ClipType.STATION_ID
        if clip_type == ClipType.TRIVIA and not prev_track:
            clip_type = ClipType.STATION_ID
        if clip_type == ClipType.DEDICATION and not next_track:
            clip_type = ClipType.STATION_ID
        if clip_type == ClipType.NEW_ARRIVAL and not next_track:
            clip_type = ClipType.STATION_ID

        prompt = self._build_prompt(clip_type, prev_track, next_track, context)
        text = self._call_api(prompt, clip_type.value)

        return DJScript(
            text=text,
            clip_type=clip_type,
            prev_track=prev_track,
            next_track=next_track,
        )

    def _build_prompt(
        self,
        clip_type: ClipType,
        prev_track: Optional[Track],
        next_track: Optional[Track],
        context: Optional[dict] = None,
    ) -> str:
        template = _PROMPTS[clip_type]
        now = datetime.datetime.now(ZoneInfo(self.timezone))
        hour_12 = now.hour % 12 or 12
        ampm = "AM" if now.hour < 12 else "PM"
        kwargs = {
            "city": self.station.get("city", "Boston"),
            "call_sign": self.station.get("call_sign", "WKRT"),
            "frequency": self.station.get("frequency", "104.7"),
            "time_of_day": _time_of_day(self.timezone),
            "hour": f"{hour_12} {ampm}",
        }
        if prev_track:
            kwargs.update({
                "prev_artist": prev_track.artist,
                "prev_title": prev_track.title,
                "prev_year": prev_track.year,
            })
        if next_track:
            kwargs.update({
                "next_artist": next_track.artist,
                "next_title": next_track.title,
                "next_year": next_track.year,
            })
        prompt = template.format(**kwargs)

        # Append real-world Boston context when available
        ctx_lines = []
        if context:
            w = context.get("weather", {})
            if w:
                wind = f", wind at {w['wind_mph']} mph" if w.get("wind_mph", 0) > 15 else ""
                ctx_lines.append(
                    f"- Current Boston weather: {w['temp_f']}°F, {w['conditions']}{wind}"
                )
                if w.get("beacon"):
                    ctx_lines.append(
                        f"- Old Hancock building beacon is {w['beacon']} "
                        f"(Bostonians know: steady blue=clear, flashing blue=clouds, "
                        f"steady red=rain, flashing red=snow)"
                    )
            sports = context.get("sports")
            if sports:
                ctx_lines.append(f"- Boston sports update: {sports}")

        if ctx_lines:
            prompt += (
                "\n\nLive Boston context — weave in naturally if it fits the moment, "
                "don't force it every time:\n" + "\n".join(ctx_lines)
            )

        # Verified MusicBrainz facts — only for clip types that reference specific tracks
        _track_clips = {
            ClipType.BETWEEN_TRACKS, ClipType.TRIVIA,
            ClipType.DEDICATION, ClipType.NEW_ARRIVAL,
        }
        if clip_type in _track_clips and context:
            from .annotator import Annotator
            fact_lines = []
            fact_lines += Annotator.format_for_prompt(
                context.get("prev_annotation"), "Song just played"
            )
            fact_lines += Annotator.format_for_prompt(
                context.get("next_annotation"), "Song coming up"
            )
            if fact_lines:
                prompt += (
                    "\n\nVerified track facts (use these — don't invent details not listed here):\n"
                    + "\n".join(f"- {l}" for l in fact_lines)
                )

        if context and context.get("live_context"):
            prompt += (
                "\n\nBREAKING — work this into your next break, make it feel live and immediate:\n"
                + context["live_context"]
            )

        return prompt

    def _call_api(self, prompt: str, clip_type: str = "") -> str:
        if not self.client:
            log.warning("No API key set — using placeholder DJ script")
            if self._stats:
                self._stats.record_fallback(self.name)
            return self._fallback_script()

        t0 = time.perf_counter()
        try:
            response = self.client.messages.create(
                model=self.cfg["api"]["model"],
                max_tokens=self.cfg["api"]["max_tokens"],
                system=self.persona,
                messages=[{"role": "user", "content": prompt}],
            )
            latency_ms = (time.perf_counter() - t0) * 1000
            text = response.content[0].text.strip()

            if self._stats:
                self._stats.record_api_call(
                    self.name,
                    clip_type,
                    response.usage.input_tokens,
                    response.usage.output_tokens,
                    latency_ms,
                )

            # Restore health on success
            if not self._api_healthy:
                log.info(f"DJ {self.name}: Claude API recovered after outage")
            self._consecutive_failures = 0
            self._api_healthy = True
            self._unhealthy_since = None
            return text

        except Exception as e:
            log.error(f"Claude API error ({self.name}): {e}")
            self._consecutive_failures += 1
            if self._consecutive_failures >= _API_FAILURE_THRESHOLD:
                if self._api_healthy:
                    log.warning(
                        f"DJ {self.name}: API marked unhealthy after "
                        f"{self._consecutive_failures} consecutive failures"
                    )
                self._api_healthy = False
                if self._unhealthy_since is None:
                    self._unhealthy_since = time.monotonic()
            if self._stats:
                self._stats.record_fallback(self.name)
            return self._fallback_script()

    def _fallback_script(self) -> str:
        """Used when API is unavailable."""
        cs = self.station["call_sign"]
        freq = self.station["frequency"]
        fallbacks = [
            f"You're listening to {cs} {freq}, {self.station['tagline']}. More rock coming right up.",
            f"That's classic rock on {cs} {freq}. {self.name} back with more after this.",
            f"{cs} — {self.station['tagline']}. We'll be right back.",
        ]
        return random.choice(fallbacks)
