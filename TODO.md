# WKRT-FM TODO

## Discord Request/Dedication Bot

Listeners post to a Discord channel; the bot wires their requests into the
live engine using hooks that already exist.

### New file: `wkrt/discord_bot.py`

```python
import discord
from discord.ext import commands
# intents: message_content = True, guilds = True
```

Bot reads from one channel (configurable `channel_id`). Two command prefixes:

| Command | Action |
|---|---|
| `!request Artist - Title` | fuzzy-match → `engine.find_track()` → `engine.force_next_track()` → confirm |
| `!dedicate Artist - Title to Name, reason` | fuzzy-match + build dedication string → `state.set_live_context(text, one_shot=True)` → `engine.force_dj_break()` |
| Plain messages (no prefix) | optionally collect and inject as a batch live_context every N minutes |

**Fuzzy match confirmation flow** (the tricky UX part):

1. Match with `programmer.fuzzy_match()` (already in `wkrt/programmer.py`)
2. If score < 0.85, reply "Did you mean **Artist — Title (year)**? React ✅ to confirm."
3. Use `bot.wait_for('reaction_add', timeout=30)` to gate the queue/inject call
4. On timeout or ❌, reply "No worries, try again with the full artist name."

**Dedication context string format** (feeds into DJ prompt via `live_context`):

```
Listener {discord_username} dedicated "{title}" by {artist} to {recipient}: {reason}
```

The DJ prompt already has a BREAKING block that works this in naturally — no
prompt changes needed.

### `wkrt/engine.py` changes

- Add `_discord_bot` field (Optional), start it in `run()` after `self.context.start()`
- Pass `engine=self` into `DiscordBot.__init__` so it can call `find_track`,
  `force_next_track`, `force_dj_break`, and `state.set_live_context`

```python
if self.cfg.get("discord", {}).get("token"):
    from .discord_bot import DiscordBot
    self._discord_bot = DiscordBot(self.cfg, self)
    threading.Thread(target=self._discord_bot.run_forever,
                     daemon=True, name="discord-bot").start()
```

### `config/settings.toml` additions

```toml
[discord]
token      = ""          # bot token from Discord Developer Portal
channel_id = 0           # channel ID to watch (right-click channel → Copy ID)
guild_id   = 0           # server ID (optional, for slash commands)
prefix     = "!"
```

### Dependencies

```
discord.py>=2.3
```

Add to `requirements.txt`.

### Discord Developer Portal setup (one-time, ~5 min)

1. https://discord.com/developers/applications → New Application
2. Bot tab → Add Bot → copy token into `settings.toml`
3. Privileged Gateway Intents: enable **Message Content Intent**
4. OAuth2 → URL Generator: scopes `bot`, permissions `Send Messages` + `Add Reactions`
5. Invite bot to server with generated URL

### Admin page additions (nice-to-have)

- Show last N Discord requests in a "Request Queue" card (store in-memory list on engine)
- Button to clear pending requests

### Edge cases to handle

- Rate limiting: ignore rapid-fire requests from the same user (30s cooldown per user)
- Unknown track: reply "Couldn't find that one in the crate — try `!request Artist - Title`"
- No engine yet (still warming): reply "We're just spinning up, try again in a minute"
- Duplicate request already queued: reply "Already in the stack!"
