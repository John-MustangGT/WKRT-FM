# WKRT-FM TODO

---

## DJ Shift Handoffs

Natural on-air handoff when the active DJ changes — outgoing signs off and
introduces the incoming, incoming responds to what was just said.

### Two prompts, sequential (outgoing text feeds incoming)

**Prompt 1 — outgoing DJ** (uses their persona + voice):
```
You're wrapping up your shift and handing the console over to {incoming_name}
for the {time_slot} show. Give a warm sign-off, mention something about what
you just played, and set up {incoming_name} — make it feel like a real FM handoff.
```

Capture `handoff_text`. Synthesize with outgoing DJ's TTS voice. Play clip.

**Prompt 2 — incoming DJ** (uses their persona + voice):
```
You're starting your {time_slot} show on WKRT 104.7. {outgoing_name} just
handed the console over to you and said: "{handoff_text}". Pick it up from
there — acknowledge the intro, make it yours, and kick off your set.
```

Synthesize with incoming DJ's TTS voice. Play clip. Normal show resumes.

### Where to hook in

`_maybe_announce_dj_change()` in `engine.py` already detects the flip.
Currently it just logs + updates ICY metadata. Extend it to:

1. Set `self._pending_handoff = (outgoing_cfg, incoming_cfg)`
2. In the main loop, after finishing the current track and before
   `_build_segment()` runs for the next one, check `_pending_handoff`
   and call `_do_handoff()` which generates + plays both clips sequentially

### New `ClipType` entries (in `dj.py`)

```python
ClipType.HANDOFF_OUT   # outgoing sign-off
ClipType.HANDOFF_IN    # incoming pickup
```

Or handle entirely in `engine.py` as one-off logic — simpler since it only
ever fires at shift boundaries.

### Edge cases

- DJ override active: suppress handoff (no natural shift change)
- Same DJ back-to-back (e.g. only one DJ configured): skip
- TTS failure: fall back to a generic station ID clip, don't block the show

---

## GPT-4o "Themed Hour" DJ

A third DJ personality powered by OpenAI instead of Claude — does curated
one-hour themed shows rather than the rolling block format.

### Concept

At the top of its shift the DJ picks a theme it can actually execute against
the real library, then owns that hour end-to-end with themed banter throughout.

Example themes (generated, not hardcoded):
- "Summer of '83 — big hair, bigger riffs"
- "FM Gold: the songs that owned the late-night drive"
- "Hair Metal Happy Hour"
- "One-Hit Wonders of the 80s"
- "The Miami Vice Soundtrack (without actually being Miami Vice)"

### API — near-identical to Anthropic

```python
# pip install openai
from openai import OpenAI
client = OpenAI(api_key=...)
resp = client.chat.completions.create(
    model="gpt-4o",
    messages=[
        {"role": "system",  "content": persona},
        {"role": "user",    "content": prompt},
    ]
)
text = resp.choices[0].message.content
```

Add `api_backend = "openai"` and `openai_api_key = ""` to `[[djs]]` config.
Default backend stays `"anthropic"` for Roxanne and Neon — no existing behavior
changes.

### Different programming model: theme-first, not block-first

Current DJs: pick 6 tracks → break → 6 tracks → break → repeat.

GPT DJ: at top of hour —
1. Send full library summary to GPT-4o
2. Ask it to propose a theme it can execute with the available tracks
3. Ask it to select 12-15 tracks for the full hour, in order, with an energy arc
4. Store theme + tracklist as `_themed_hour` state on the engine
5. Each DJ break references the theme ("that was track 4 of our journey through…")
6. Final break: themed outro before handing off to next DJ

One API call to plan the hour, then normal per-break calls for banter.

### Key constraint: library-aware theme selection

GPT must see the library BEFORE picking a theme, not after. Prompt structure:

```
Here is what's actually in the crate: {library_summary}

Pick a theme for a one-hour show that you can execute with at least
10 of these tracks. Return JSON:
{"theme": "...", "tagline": "...", "tracks": [{artist, title, year}, ...]}
```

Fuzzy-match the returned tracks back to real files (same `fuzzy_match()`
already in `programmer.py`).

### New engine fields needed

```python
self._themed_hour: Optional[dict] = None   # {theme, tagline, tracks: [Track]}
self._themed_hour_index: int = 0           # which track we're on
self._themed_hour_expiry: float = 0        # epoch seconds, reset each hour
```

`_get_next_track()` checks `_themed_hour` first when GPT DJ is active.

### `settings.toml` additions

```toml
[[djs]]
name        = "Chase"          # or whatever name fits
shift_hours = 1                # one-hour block, rotates in with Roxanne/Neon
tts_backend = "google"
api_backend = "openai"
openai_api_key = ""            # or pull from env OPENAI_API_KEY
persona     = """
You are Chase, afternoon drive DJ on WKRT 104.7. You do themed hours —
each show has a concept that connects the music. You're the curator,
not just the jock. Warm, smart, a little cinematic.
"""

[djs.clip_types]
between_tracks = 50
station_id     = 20
top_of_hour    = 20
trivia         = 10
```

### What GPT-4o brings that's genuinely different

Strong pop-culture pattern matching baked into training — ask for "Miami Vice
influence" and it knows Jan Hammer, Phil Collins, Glenn Frey without being told.
Claude would too, but different training data = different flavor = more variety
across the three DJs.

---

## Discord Listener Theme Voting

Before the GPT DJ's shift starts, post a poll to Discord so listeners pick
the themed hour.

### Flow

- ~10 min before Chase's shift: bot posts to #requests channel
  "🎙️ Chase goes on in 10 — vote for the theme!" with 3-4 options
- Use Discord's native poll API (added 2024) or emoji reaction voting
  (reactions are simpler, wider client support)
- At shift start, tally votes, pass winning theme to GPT as a seed
  ("Listeners voted for: Hair Metal Happy Hour — execute it")
- If no votes cast: GPT picks freely as normal

### Write-in themes

Allow `!theme [idea]` command in the channel during the voting window.
Collect write-ins, let GPT judge if it's executable against the library
("Can I do a 'Yacht Rock' hour? Let me check the crate…"), then add it
to the poll or auto-accept if it's the only suggestion.

### Config

```toml
[discord]
theme_voting_minutes_before = 10   # how early to post the poll
theme_options_count = 4            # how many GPT-generated options to offer
```

---

## Station History Context

Inject "what was happening in {year}" into DJ prompts so breaks feel grounded
in the era of the song, not just the song itself.

### Approach

Let the AI generate it from training knowledge — no external API needed.
Add a `year_context` field to the DJ prompt when a song has a year tag:

```
Song context: "{title}" by {artist} came out in {year}.
In {year}: [AI-generated 1-2 sentence snapshot of what was happening —
pop culture, news, sports, TV — that a radio DJ would naturally reference]
```

Could be pre-generated and cached per year at startup (one Claude call per
year in the library = ~10-15 calls, cheap, stored in `config/year_context.json`).
Then injected into `between_tracks` and `trivia` prompts automatically.

### What good looks like

> "That was Bonnie Tyler in '83 — the same year Return of the Jedi hit theaters
> and everyone had a Rubik's Cube they still couldn't solve. Some things age
> better than others."

---

## Song Annotation (richer DJ facts)

Pre-fetch real metadata per track so DJs have accurate facts, not hallucinated ones.

### Data sources (all free)

| Source | What it gives | API |
|---|---|---|
| MusicBrainz | recording date, album, genre tags, MBID | REST, no key needed |
| Last.fm | listener count, tags, wiki summary | free API key |
| Discogs | label, catalog #, pressing info | free API key |

### Storage

Sidecar JSON: `config/annotations/{artist_norm}_{title_norm}.json`

```json
{
  "album": "Pyromania",
  "recorded": "1982",
  "label": "Mercury Records",
  "tags": ["hard rock", "glam metal", "arena rock"],
  "wiki_summary": "First single from Pyromania, reached #12 on Billboard Hot 100...",
  "fetched_at": "2026-04-26T10:00:00Z"
}
```

### Injection

Add annotation to the `between_tracks` and `trivia` prompts when available:

```
Known facts about this track (use these — don't invent others):
{annotation}
```

The "don't invent others" instruction reduces hallucination on specific claims
(chart positions, recording locations, etc.) while still letting the DJ be
conversational around verified facts.

### Fetch strategy

- Lazy: fetch on first play, cache forever (annotations don't change)
- Background thread: annotate the whole library once at startup if cache is cold
- Rate limit: MusicBrainz asks for 1 req/sec, Last.fm is generous

---

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
