# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

WKRT-FM 104.7 — a Python-based retro 80s radio station engine. It scans a music library, generates AI DJ banter via the Claude API (Anthropic), synthesizes voice via Piper TTS or Google Cloud TTS, and stitches everything together with ffmpeg into a continuous stream playable via ffplay or piped to Icecast.

Target platform: Raspberry Pi Zero 2W or any Linux box.

## Commands

```bash
# Install Python dependencies
pip install -r requirements.txt

# Download Piper voice model
bash setup_voices.sh

# Run the station
python main.py

# Scan library and print year/track stats
python main.py --scan

# Generate one DJ clip end-to-end (Claude → TTS → ffplay)
python main.py --test-dj

# Test TTS only (no Claude API call)
python main.py --test-tts "You're listening to WKRT 104.7"

# Analyze existing music collection for coverage vs. the master tracklist
python wkrt_analyze.py /path/to/mp3-listing.txt

# Copy matched tracks into the year-based layout (dry-run first)
python wkrt_organize.py --src /path/to/music --dst ./music --dry-run
python wkrt_organize.py --src /path/to/music --dst ./music
```

### System dependencies

`ffmpeg`, `ffprobe`, and `ffplay` must be on `PATH`. Piper binary (`piper` or `piper-tts`) is required for Piper TTS; without it the engine generates silent clips as fallback. Google Cloud TTS requires the `google-cloud-texttospeech` Python package and valid credentials (see Configuration below).

### Configuration

Edit `config/settings.toml` (gitignored — never committed). Copy `.env_example` to `.env` and fill in secrets. The systemd unit (`wkrt-fm.service`) is the canonical way to inject env vars in production.

| Env var | Overrides | Required |
|---|---|---|
| `ANTHROPIC_API_KEY` | `[api].api_key` | Yes |
| `WKRT_MUSIC_DIR` | `[paths].music_dir` | No |
| `WKRT_GOOGLE_CREDENTIALS` | `GOOGLE_APPLICATION_CREDENTIALS` | Only for Google TTS DJs |

## Architecture

### Data flow

```
music/<year>/*.mp3  →  PlaylistQueue  →  WKRTEngine (main loop)
                                                 │
                              ┌──────────────────┴──────────────────┐
                              │  every dj_every_n_tracks:            │
                       active DJEngine                        (track only)
                       (Claude API)                                  │
                              │                                      │
                           TTSEngine                                 │
                     (Piper or Google TTS)                           │
                              │                                      │
                           Mixer (ffmpeg) ──────────────────────────┘
                              │
                           spool/*.mp3  →  ffplay (local) or Icecast
```

### Module responsibilities

| Module | Role |
|---|---|
| `wkrt/config.py` | Loads `settings.toml`, merges env var overrides, resolves relative paths |
| `wkrt/playlist.py` | Scans `music/<year>/` dirs, reads ID3 tags (mutagen), weighted-shuffle `PlaylistQueue` |
| `wkrt/dj.py` | Builds prompts for 6 clip types, calls Claude API, falls back to canned lines if API unavailable |
| `wkrt/tts.py` | Dispatches to Piper or Google Cloud TTS per `dj_cfg`; converts WAV→MP3; caches by SHA-256 of `(voice_id, text)` |
| `wkrt/mixer.py` | ffmpeg filtergraphs: talkover stitch, crossfade, silence trim; returns `(path, dj_starts_at)` |
| `wkrt/engine.py` | Main loop; DJ rotation by hour; pre-generates next segment in background thread; pushes ICY metadata to Icecast |
| `wkrt/cache.py` | `StartupCache` (COLD→WARMING→WARM→RUNNING→COOLING state machine) + `TopOfHourScheduler` |
| `wkrt/hooks.py` | Tiny HTTP server on port 8765 for Icecast `on-connect`/`on-disconnect` webhooks (GET and POST) |
| `wkrt/context.py` | Background thread: fetches Boston weather + sports scores; injects into DJ prompts |
| `wkrt/state.py` | Thread-safe station state (now-playing, listener count, active DJ, cache state); consumed by web UI |
| `wkrt/web.py` | Station web UI at `/` and JSON status API at `/api/status` (stdlib `http.server`, port 8080) |

### DJ roster and rotation

DJs are defined as `[[djs]]` entries in `settings.toml`. Each has:

- `name` — display name (e.g. `"Roxanne"`, `"Neon"`)
- `shift_hours` — how many hours per rotation block
- `tts_backend` — `"piper"` or `"google"`
- `persona` — multi-line system prompt injected into every Claude call
- `[djs.clip_types]` — per-DJ clip type weights
- `[djs.tts]` — backend-specific TTS params (voice model, speed, etc.)

`WKRTEngine.active_dj_cfg()` returns the DJ that should be on air based on the current hour modulo the total shift period. DJ changes push an ICY metadata update and log a shift announcement.

### TTS backends

`TTSEngine.synthesize(text, dj_cfg)` dispatches based on `dj_cfg["tts_backend"]`:

- **`piper`** — local binary; voice model file must exist at `voices/<model>.onnx`. Falls back to 4-second silence if model is missing.
- **`google`** — Google Cloud TTS; requires `GOOGLE_APPLICATION_CREDENTIALS` (or `WKRT_GOOGLE_CREDENTIALS`). Uses `en-US-Studio-*` voices. Falls back to silence on auth/network failure.

Cache key is SHA-256 of `"<voice_id>:<text>"` — two DJs saying the same line produce separate cached files.

### DJ clip types

`DJEngine` selects clip type by weighted random draw from `[djs.clip_types]` in config:

- `between_tracks` — banter referencing prev and next songs
- `trivia` — fact about the just-played artist/song
- `dedication` — fake listener dedication for the next track
- `station_id` — call sign / time of day
- `top_of_hour` — pre-generated at :55 for the :00 slot (via `TopOfHourScheduler`)
- `connect_id` — generated at startup, played when first listener connects (Icecast integration)

### Spool and caching

- **Spool** (`spool/`): pre-stitched MP3 segments named `seg_<index>_<year>_<artist>.mp3`. Cleaned to the 15 most recent every 10 tracks.
- **DJ clip cache** (`dj_clips/`): TTS output cached as `dj_<sha256[:16]>.mp3`.

### Icecast integration

Configure Icecast with:
```xml
<on-connect>http://127.0.0.1:8765/connect</on-connect>
<on-disconnect>http://127.0.0.1:8765/disconnect</on-disconnect>
```

The engine also polls `http://<host>:<port>/status-json.xsl` every 15 seconds to reconcile listener count independently of webhooks. ICY `StreamTitle` metadata is pushed on every track change and at the moment the DJ talkover begins.

`StartupCache` tracks listener count and enters a 5-minute COOLING state after the last disconnect before stopping the engine. The hook port is configurable via `[icecast].hook_port`.

### Music library layout

Music **must** be organized into year subdirectories:

```
music/
  1980/
    AC_DC - You Shook Me All Night Long.mp3
  1984/
    ...
```

`scan_library()` only recognizes directories matching `(19|20)\d{2}`. ID3 tags take priority over filename for artist/title metadata; filename is parsed as `"Artist - Title"` when tags are absent. Supported formats: `.mp3`, `.flac`, `.m4a`, `.ogg`, `.wav`.

### Production deployment

```bash
# Adjust User= and WorkingDirectory= in wkrt-fm.service, then:
sudo cp wkrt-fm.service /etc/systemd/system/
sudo systemctl enable --now wkrt-fm
sudo journalctl -u wkrt-fm -f
```

Set `ANTHROPIC_API_KEY` (and `WKRT_GOOGLE_CREDENTIALS` if using Google TTS) via `Environment=` lines in the systemd unit or a drop-in override (`systemctl edit wkrt-fm`).
