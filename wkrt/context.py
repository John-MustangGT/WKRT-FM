"""
wkrt/context.py — Real-world context for DJ scripts.

Fetches Boston weather and sports on a background thread.
Passed to DJEngine.generate() so Roxanne can make natural references
to current conditions, the Hancock beacon, and local teams.
"""
import logging
import threading
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

import httpx

log = logging.getLogger(__name__)

_LAT = 42.3601
_LON = -71.0589

# WMO weather code → (human description, Hancock beacon state)
# Beacon rhyme: steady blue=clear view, flashing blue=clouds due,
#               steady red=rain ahead, flashing red=snow instead
_WMO = {
    0:  ("clear",               "steady blue"),
    1:  ("mostly clear",        "steady blue"),
    2:  ("partly cloudy",       "flashing blue"),
    3:  ("overcast",            "flashing blue"),
    45: ("foggy",               "flashing blue"),
    48: ("freezing fog",        "flashing blue"),
    51: ("light drizzle",       "steady red"),
    53: ("drizzle",             "steady red"),
    55: ("heavy drizzle",       "steady red"),
    61: ("light rain",          "steady red"),
    63: ("rain",                "steady red"),
    65: ("heavy rain",          "steady red"),
    71: ("light snow",          "flashing red"),
    73: ("snow",                "flashing red"),
    75: ("heavy snow",          "flashing red"),
    77: ("sleet",               "flashing red"),
    80: ("rain showers",        "steady red"),
    81: ("rain showers",        "steady red"),
    82: ("heavy showers",       "steady red"),
    85: ("snow showers",        "flashing red"),
    86: ("heavy snow showers",  "flashing red"),
    95: ("thunderstorms",       "steady red"),
    96: ("thunderstorms",       "steady red"),
    99: ("thunderstorms",       "steady red"),
}

# ESPN sport slug → Boston team abbreviations
_TEAMS = {
    "baseball/mlb":   ["BOS"],   # Red Sox
    "football/nfl":   ["NE"],    # Patriots
    "basketball/nba": ["BOS"],   # Celtics
    "hockey/nhl":     ["BOS"],   # Bruins
}

_TEAM_NAMES = {
    "baseball/mlb":   {"BOS": "Red Sox"},
    "football/nfl":   {"NE":  "Patriots"},
    "basketball/nba": {"BOS": "Celtics"},
    "hockey/nhl":     {"BOS": "Bruins"},
}


class StationContext:
    REFRESH_INTERVAL = 1800  # 30 minutes

    def __init__(self, cfg: dict):
        self._tz = ZoneInfo(cfg["station"].get("timezone", "UTC"))
        self._data: dict = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()

    def start(self):
        self._refresh()
        t = threading.Thread(target=self._loop, daemon=True, name="context")
        t.start()

    def get(self) -> dict:
        with self._lock:
            return dict(self._data)

    def stop(self):
        self._stop.set()

    # ── Internal ──────────────────────────────────────────────────────────

    def _loop(self):
        while not self._stop.wait(self.REFRESH_INTERVAL):
            self._refresh()

    def _refresh(self):
        data = {}
        try:
            data["weather"] = self._fetch_weather()
            log.info(
                f"Weather: {data['weather']['temp_f']}°F, "
                f"{data['weather']['conditions']}, "
                f"beacon {data['weather']['beacon']}"
            )
        except Exception as e:
            log.warning(f"Weather fetch failed: {e}")
        try:
            sports = self._fetch_sports()
            if sports:
                data["sports"] = sports
                log.info(f"Sports: {sports}")
        except Exception as e:
            log.warning(f"Sports fetch failed: {e}")
        with self._lock:
            self._data = data

    def _fetch_weather(self) -> dict:
        url = (
            "https://api.open-meteo.com/v1/forecast"
            f"?latitude={_LAT}&longitude={_LON}"
            "&current=temperature_2m,weather_code,wind_speed_10m"
            "&temperature_unit=fahrenheit&wind_speed_unit=mph"
            "&forecast_days=1"
        )
        r = httpx.get(url, timeout=10)
        r.raise_for_status()
        c = r.json()["current"]
        code = c["weather_code"]
        desc, beacon = _WMO.get(code, ("unknown conditions", "steady blue"))
        return {
            "temp_f":     round(c["temperature_2m"]),
            "conditions": desc,
            "wind_mph":   round(c["wind_speed_10m"]),
            "beacon":     beacon,
        }

    def _fetch_sports(self) -> Optional[str]:
        lines = []
        with httpx.Client(timeout=10) as client:
            for sport, team_abbrevs in _TEAMS.items():
                try:
                    url = (
                        f"https://site.api.espn.com/apis/site/v2/sports"
                        f"/{sport}/scoreboard"
                    )
                    events = client.get(url).json().get("events", [])
                    for event in events:
                        comp = event.get("competitions", [{}])[0]
                        competitors = comp.get("competitors", [])
                        abbrevs = {c["team"]["abbreviation"] for c in competitors}
                        if not (abbrevs & set(team_abbrevs)):
                            continue

                        names = _TEAM_NAMES[sport]
                        bos = next(
                            (c for c in competitors
                             if c["team"]["abbreviation"] in team_abbrevs), None
                        )
                        opp = next(
                            (c for c in competitors
                             if c["team"]["abbreviation"] not in team_abbrevs), None
                        )
                        if not bos or not opp:
                            continue

                        team = names.get(bos["team"]["abbreviation"],
                                         bos["team"]["displayName"])
                        opp_name = opp["team"]["displayName"]
                        status = comp.get("status", {}).get("type", {})
                        state = status.get("state", "")

                        if state == "post":
                            bs, os = bos.get("score", "?"), opp.get("score", "?")
                            result = "won" if _gt(bs, os) else "lost"
                            lines.append(f"{team} {result} {bs}-{os} against {opp_name}")
                        elif state == "in":
                            bs, os = bos.get("score", "?"), opp.get("score", "?")
                            detail = status.get("shortDetail", "in progress")
                            lines.append(
                                f"{team} vs {opp_name} {bs}-{os} ({detail})"
                            )
                        elif state == "pre":
                            try:
                                gt = datetime.fromisoformat(
                                    event["date"].replace("Z", "+00:00")
                                ).astimezone(self._tz)
                                time_str = gt.strftime("%-I:%M %p")
                                lines.append(f"{team} host {opp_name} tonight at {time_str}")
                            except Exception:
                                lines.append(f"{team} play {opp_name} tonight")
                except Exception as e:
                    log.debug(f"Sports fetch error ({sport}): {e}")

        return "; ".join(lines) if lines else None


def _gt(a, b) -> bool:
    try:
        return int(a) > int(b)
    except (TypeError, ValueError):
        return False
