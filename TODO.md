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

### Why Discord is the right layer for this

Discord does the hard auth/moderation work for free so we don't have to:

- **Auth** — Discord accounts = identity. No login system to build. Restrict
  the request channel to server members only (or a specific role like `@listener`).
- **Spam** — Enable **Slowmode** on the request channel (e.g. 60s per user).
  Discord enforces it server-side before the bot ever sees the message.
- **Content / PG filter** — Enable **AutoMod** on the server: built-in NSFW
  keyword filter + custom blocked words list. Messages that trip it are deleted
  before the bot processes them. Free, no API calls, no maintenance.
- **Banning** — Server mods can kick/ban abusers without touching the station.

### API credit protection

`!request` never touches Claude — it's just track lookup + queue. Safe.

`!dedicate` injects into `live_context` as one-shot, which fires on the next
**natural** DJ break (one that would have happened anyway). The risk is
`force_dj_break()` — if called on every dedication it could generate extra
Claude calls beyond the normal cadence.

**Rules to enforce in the bot:**

1. **One pending dedication at a time.** If one is already queued, reply
   "There's already a dedication in the booth — try again after the next break."
   Don't call `force_dj_break()` again.
2. **One pending request at a time.** Same deal — `force_next_track` replaces
   whatever was queued, so stacking them is pointless anyway.
3. **Per-user cooldown (bot-side, secondary net):** 5 min between commands per
   Discord user ID, regardless of Discord slowmode setting. Store in a simple
   `dict[user_id, last_timestamp]`.
4. **Never call `force_dj_break()` for a bare `!request`** — let it play at
   the next natural break. Only use it for dedications where timing matters.

With Slowmode=60s + one-pending-at-a-time, the absolute max extra Claude calls
from Discord is one per ~60 seconds, bounded by however fast the DJ breaks
already fire. In practice it's a non-issue.

### Edge cases to handle

- Rate limiting: per-user 5 min cooldown (dict lookup, no DB needed)
- Unknown track: reply "Couldn't find that one in the crate — try `!request Artist - Title`"
- No engine yet (still warming): reply "We're just spinning up, try again in a minute"
- Duplicate request already queued: reply "Already in the stack!"
- AutoMod-deleted message: Discord handles it silently, bot never sees it
