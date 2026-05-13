"""
wkrt/discord.py — Discord webhook notifications for WKRT-FM.

Sends a "Now Playing" embed to a Discord channel webhook each time a new
track starts.  Entirely optional — disabled when no webhook URL is configured.

Config (in settings.toml):

    [discord]
    webhook_url = "https://discord.com/api/webhooks/..."
    # Optional overrides:
    # username    = "WKRT-FM 104.7"   # bot display name
    # avatar_url  = "https://..."      # bot avatar
    # post_dj_script = true            # also post DJ break text when it changes
"""

import json
import logging
import threading
from typing import Optional
from urllib.error import URLError
from urllib.request import Request, urlopen

log = logging.getLogger(__name__)


class DiscordNotifier:
    """Fire-and-forget Discord webhook poster.

    All sends are dispatched on a daemon thread so they never block playback.
    """

    def __init__(self, cfg: dict):
        discord_cfg = cfg.get("discord", {})
        self._webhook_url: Optional[str] = discord_cfg.get("webhook_url") or None
        station = cfg.get("station", {})
        call = station.get("call_sign", "WKRT")
        freq = station.get("frequency", "104.7")
        self._username: str = discord_cfg.get("username", f"{call}-FM {freq}")
        self._avatar_url: Optional[str] = discord_cfg.get("avatar_url") or None
        self._post_dj_script: bool = discord_cfg.get("post_dj_script", False)
        self._color: int = 0xE8334A  # WKRT red

        if self._webhook_url:
            log.info(f"Discord notifier enabled (username={self._username!r})")
        else:
            log.debug("Discord notifier disabled — no webhook_url configured")

    @property
    def enabled(self) -> bool:
        return bool(self._webhook_url)

    # ── Public API ────────────────────────────────────────────────────────────

    def notify_track(self, artist: str, title: str, year: Optional[int] = None,
                     dj_name: Optional[str] = None) -> None:
        """Post a now-playing embed for a new track."""
        if not self.enabled:
            return

        fields = []
        if year:
            fields.append({"name": "Year", "value": str(year), "inline": True})
        if dj_name:
            fields.append({"name": "On Air", "value": dj_name, "inline": True})

        embed = {
            "color": self._color,
            "author": {"name": "Now Playing"},
            "title": title,
            "description": f"**{artist}**",
            "fields": fields,
        }
        self._send({"embeds": [embed]})

    def notify_dj_script(self, dj_name: str, text: str) -> None:
        """Post a DJ break quote, if post_dj_script is enabled."""
        if not self.enabled or not self._post_dj_script:
            return

        embed = {
            "color": 0x4A9EFF,
            "author": {"name": f"DJ {dj_name}"},
            "description": f"*\u201c{text}\u201d*",
        }
        self._send({"embeds": [embed]})

    # ── Internal ──────────────────────────────────────────────────────────────

    def _send(self, payload: dict) -> None:
        """Dispatch a webhook POST on a daemon thread."""
        payload["username"] = self._username
        if self._avatar_url:
            payload["avatar_url"] = self._avatar_url

        threading.Thread(
            target=self._post,
            args=(payload,),
            daemon=True,
            name="discord-webhook",
        ).start()

    def _post(self, payload: dict) -> None:
        """Blocking POST — runs on background thread."""
        body = json.dumps(payload).encode()
        req = Request(
            self._webhook_url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(req, timeout=5):
                pass
            log.debug("Discord webhook sent")
        except URLError as e:
            log.warning(f"Discord webhook failed: {e}")
        except Exception as e:
            log.debug(f"Discord webhook error: {e}")
