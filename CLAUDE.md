# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

WKRT-FM 104.7 — a Python-based retro 80s radio station engine. It scans a music library, generates AI DJ banter via the Claude API (Anthropic), synthesizes voice via Piper TTS, and stitches everything together with ffmpeg into a continuous stream playable via ffplay or piped to Icecast.

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

`ffmpeg`, `ffprobe`, and `ffplay` must be on `PATH`. Piper binary (`piper` or `piper-tts`) is required for TTS; without it, the engine generates silent clips as fallback.

### Configuration

Edit `config/settings.toml`. The `ANTHROPIC_API_KEY` environment variable overrides `[api].api_key` in the config. `WKRT_MUSIC_DIR` overrides `[paths].music_dir`.

**The API key in `config/settings.toml` must not be stored in plaintext — use the env var instead.** The systemd unit (`wkrt-fm.service`) is the canonical way to set it in production.

## Architecture

### Data flow

```
music/<year>/*.mp3  →  PlaylistQueue  →  WKRTEngine (main loop)
                                                 │
                              ┌──────────────────┴──────────────────┐
                              │  every dj_every_n_tracks:            │
                         DJEngine                             (track only)
                       (Claude API)                                  │
                              │                                      │
                           TTSEngine                                 │
                         (Piper TTS)                                 │
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
| `wkrt/dj.py` | Builds prompts for 5 clip types, calls Claude API, falls back to canned lines if API unavailable |
| `wkrt/tts.py` | Calls `piper` binary, converts WAV→MP3 via ffmpeg, caches by SHA-256 of script text |
| `wkrt/mixer.py` | ffmpeg filtergraphs: fade-out + DJ clip stitch, crossfade, silence trim |
| `wkrt/engine.py` | Main loop; pre-generates next segment in a background thread while current plays |
| `wkrt/cache.py` | `StartupCache` (COLD→WARMING→WARM→RUNNING→COOLING state machine) + `TopOfHourScheduler` |
| `wkrt/hooks.py` | Tiny HTTP server on port 8765 for Icecast `on-connect`/`on-disconnect` webhooks |

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

### Spool and caching

- **Spool** (`spool/`): pre-stitched MP3 segments named `seg_<index>_<year>_<artist>.mp3`. Cleaned to the 15 most recent every 10 tracks.
- **DJ clip cache** (`dj_clips/`): TTS output cached as `dj_<sha256[:16]>.mp3`. Same script text always reuses the cached clip.

### DJ clip types

`DJEngine` selects clip type by weighted random draw from `[dj.clip_types]` in config:

- `between_tracks` — banter referencing prev and next songs
- `trivia` — fact about the just-played artist/song
- `dedication` — fake listener dedication for the next track
- `station_id` — call sign / time of day
- `top_of_hour` — pre-generated at :55 for the :00 slot (via `TopOfHourScheduler`)
- `connect_id` — generated at startup, played when first listener connects (Icecast integration)

### Icecast integration

Configure Icecast with:
```xml
<on-connect>http://127.0.0.1:8765/connect</on-connect>
<on-disconnect>http://127.0.0.1:8765/disconnect</on-disconnect>
```

`StartupCache` tracks listener count and enters a 5-minute COOLING state after the last disconnect before stopping the engine. The hook port is configurable via `[icecast].hook_port`.

### Production deployment

```bash
# Adjust User= and WorkingDirectory= in wkrt-fm.service, then:
sudo cp wkrt-fm.service /etc/systemd/system/
sudo systemctl enable --now wkrt-fm
sudo journalctl -u wkrt-fm -f
```
