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

# Ingest new tracks into a running station (notifies engine via HTTP)
python wkrt_ingest.py /path/to/new/track.mp3 [...]
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
| `WKRT_ADMIN_PASSWORD` | `[web].admin_password` | Yes (for admin UI + ingest) |

## Architecture

### Data flow

```
music/<year>/*.mp3  →  PlaylistQueue  →  WKRTEngine (main loop)
                                                 │
                              ┌──────────────────┴──────────────────┐
                              │  every dj_every_n_tracks:            │
                       active DJEngine                        (track only)
                       (Claude API)                                  │
                         + Annotator                                 │
                         + DJStats                                   │
                              │                                      │
                           TTSEngine                                 │
                     (Piper or Google TTS)                           │
                              │                                      │
                           Mixer (ffmpeg) ──────────────────────────┘
                              │
                     spool/*.mp3  →  ffplay (local) or Icecast
                              │
                        PlayHistory  ←  records each play
```

### Module responsibilities

| Module | Role |
|---|---|
| `wkrt/config.py` | Loads `settings.toml`, merges env var overrides, resolves relative paths |
| `wkrt/playlist.py` | Scans `music/<year>/` dirs, reads ID3 tags (mutagen), weighted-shuffle `PlaylistQueue` |
| `wkrt/dj.py` | Builds prompts for 7 clip types, calls Claude API, tracks consecutive failures, falls back to pre-baked TTS clip when API is unhealthy |
| `wkrt/tts.py` | Dispatches to Piper or Google Cloud TTS per `dj_cfg`; converts WAV→MP3; caches by SHA-256 of `(voice_id, text)` |
| `wkrt/mixer.py` | ffmpeg filtergraphs: talkover stitch, crossfade, silence trim; returns `(path, dj_starts_at)` |
| `wkrt/engine.py` | Main loop; DJ rotation by hour; pre-generates next segment in background thread; pushes ICY metadata to Icecast; manages fallback clips |
| `wkrt/cache.py` | `StartupCache` (COLD→WARMING→WARM→RUNNING→COOLING state machine) + `TopOfHourScheduler` |
| `wkrt/hooks.py` | Tiny HTTP server on port 8765 for Icecast `on-connect`/`on-disconnect` webhooks (GET and POST) |
| `wkrt/context.py` | Background thread: fetches Boston weather + sports scores; injects into DJ prompts |
| `wkrt/state.py` | Thread-safe station state (now-playing, listener count, active DJ, cache state); consumed by web UI |
| `wkrt/web.py` | Station web UI at `/` and admin UI at `/admin`; JSON API; Prometheus metrics at `/metrics` (stdlib `http.server`, port 8080) |
| `wkrt/annotator.py` | Fetches per-track metadata (album, label, release year, genre) from MusicBrainz; caches under `config/annotations/`; injects verified facts into DJ prompts |
| `wkrt/history.py` | Records per-track play history (last 5 plays, total plays, per-DJ slot breakdown) under `config/history/` |
| `wkrt/dj_stats.py` | Tracks per-DJ API call counts, token usage, latency, TTS timing, and fallback events; persists to `config/dj_stats.json` |
| `wkrt/programmer.py` | DJ programming: MD Picks (user favorites), DJ Crate Picks (AI-generated per-slot favorites), library state tracking |

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
- `new_arrival` — announces a freshly ingested track; triggered automatically when a crate track is next

### API health and automatic fallback mode

`DJEngine` tracks consecutive Claude API failures. After 3 in a row it marks itself unhealthy and stops calling the API. The engine switches to a per-DJ pre-baked TTS fallback clip ("station on automatic") generated at startup. The engine retries the real API every 5 minutes; on success it restores healthy status automatically.

The admin page shows a live `API HEALTHY` / `API DEGRADED` badge per DJ. `/api/dj-stats` and `/metrics` expose the health flag for external monitoring.

### MusicBrainz annotation

`Annotator` fetches album, label, release year, and genre tags from the MusicBrainz REST API (1 req/sec, no key required). Results are cached as JSON under `config/annotations/`. Negative results (no confident match) are also cached so tracks aren't re-queried on every restart.

At startup a background thread sweeps the library and annotates any un-cached tracks. New tracks ingested via `wkrt_ingest.py` trigger individual annotation fetches immediately.

Verified facts are injected into DJ prompts for `between_tracks`, `trivia`, `dedication`, and `new_arrival` clip types with the instruction "don't invent details not listed here."

### Play history

`PlayHistory` records every track play under `config/history/<artist>_<title>.json`. Each file stores:

- `total_plays` — lifetime play count
- `djs` — per-DJ breakdown with `total`, `by_slot` (morning/midday/afternoon/evening/night), and `last_played` (capped at last 5, with timestamp and slot)

The track detail panel in both the listener page and admin page displays this history.

### DJ programming

`DJProgrammer` manages two layers of curated content:

- **MD Picks** (`config/user_favorites.json`) — tracks hand-picked by the music director; always eligible for the programmed block regardless of other weights.
- **DJ Crate Picks** (`config/dj_<name>_favorites.json`) — AI-generated per-slot favorites created by asking each DJ to pick tracks from the library they'd want to play in each daypart.

Crate picks are regenerated automatically ~35 minutes after a crate update (detected via `config/library_state.json` `last_ingest` timestamp). Manual regeneration is available via the admin page.

### Spool and caching

- **Spool** (`spool/`): pre-stitched MP3 segments named `seg_<index>_<year>_<artist>.mp3`. Cleaned to the 15 most recent every 10 tracks.
- **DJ clip cache** (`dj_clips/`): TTS output cached as `dj_<sha256[:16]>.mp3`. Fallback clips are stored here too.
- **Annotation cache** (`config/annotations/`): MusicBrainz results as JSON, one file per track.
- **Play history** (`config/history/`): per-track play records as JSON.
- **DJ stats** (`config/dj_stats.json`): accumulated API/TTS performance counters.
- **Library state** (`config/library_state.json`): `last_ingest` and `last_regen` timestamps.

### Web API

All endpoints are on port 8080. Auth-required endpoints need HTTP Basic auth with `admin:<admin_password>`.

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/` | — | Listener page |
| GET | `/admin` | ✓ | Admin control panel |
| GET | `/metrics` | — | Prometheus text metrics |
| GET | `/api/status` | — | Full station state JSON |
| GET | `/api/library` | — | Artist/track library JSON |
| GET | `/api/track?artist=&title=` | — | Track detail (ID3, annotation, history, art) |
| GET | `/api/dj-stats` | — | Per-DJ API/TTS performance stats |
| GET | `/api/library/state` | ✓ | `{last_ingest, last_regen}` timestamps |
| GET | `/api/listeners` | ✓ | Icecast client list |
| GET | `/api/targets` | ✓ | Stream target statuses |
| GET | `/api/favorites/user` | ✓ | MD Picks list |
| GET | `/api/favorites/dj/{name}` | ✓ | DJ Crate Picks by slot |
| POST | `/api/dj/override` | ✓ | Force a specific DJ on air |
| DELETE | `/api/dj/override` | ✓ | Clear DJ override |
| POST | `/api/dj/restart` | ✓ | Force an immediate DJ break |
| POST | `/api/queue/next` | ✓ | Queue a specific track next |
| POST | `/api/library/ingest` | ✓ | Notify engine of new tracks |
| POST | `/api/context` | ✓ | Inject live context into next DJ break |
| POST | `/api/listeners/kick` | ✓ | Kick a listener by ID |
| POST | `/api/targets/{idx}/enable` | ✓ | Enable a stream target |
| POST | `/api/targets/{idx}/disable` | ✓ | Disable a stream target |
| POST | `/api/targets/{idx}/restart` | ✓ | Restart a stream target |
| POST | `/api/favorites/user/add` | ✓ | Add to MD Picks |
| POST | `/api/favorites/user/remove` | ✓ | Remove from MD Picks |
| POST | `/api/favorites/dj/{name}/regenerate` | ✓ | Regenerate DJ Crate Picks |
| POST | `/api/dj-stats/reset` | ✓ | Clear accumulated DJ stats |

### Prometheus metrics

`GET /metrics` returns Prometheus text format (v0.0.4). No authentication required. Scrape with:

```yaml
scrape_configs:
  - job_name: wkrt_fm
    static_configs:
      - targets: ['your-pi-ip:8080']
```

Metrics exposed (all prefixed `wkrt_`):

| Metric | Type | Labels | Description |
|---|---|---|---|
| `wkrt_info` | gauge | call_sign, frequency, city | Station identity (always 1) |
| `wkrt_listeners` | gauge | — | Current listener count |
| `wkrt_tracks_played_total` | counter | — | Tracks played since startup |
| `wkrt_cache_state` | gauge | — | 0=COLD 1=WARMING 2=WARM 3=RUNNING 4=COOLING |
| `wkrt_stream_targets_configured` | gauge | — | Number of configured targets |
| `wkrt_stream_target_connected` | gauge | name, host, mount, codec | Per-target connection state |
| `wkrt_stream_target_enabled` | gauge | name | Per-target enabled flag |
| `wkrt_dj_api_calls_total` | counter | dj | Claude API calls |
| `wkrt_dj_input_tokens_total` | counter | dj | Input tokens consumed |
| `wkrt_dj_output_tokens_total` | counter | dj | Output tokens generated |
| `wkrt_dj_api_latency_ms_total` | counter | dj | Cumulative API call latency |
| `wkrt_dj_fallbacks_total` | counter | dj | Times fell back to canned clip |
| `wkrt_dj_tts_calls_total` | counter | dj | TTS synthesis calls |
| `wkrt_dj_tts_latency_ms_total` | counter | dj | Cumulative TTS latency |
| `wkrt_dj_segment_calls_total` | counter | dj | Segment build calls |
| `wkrt_dj_segment_latency_ms_total` | counter | dj | Cumulative segment build latency |
| `wkrt_dj_clip_type_total` | counter | dj, clip_type | Clips generated by type |
| `wkrt_dj_api_healthy` | gauge | dj | Claude API health (1=ok 0=degraded) |

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

Set `ANTHROPIC_API_KEY`, `WKRT_ADMIN_PASSWORD`, and `WKRT_GOOGLE_CREDENTIALS` (if using Google TTS) via `Environment=` lines in the systemd unit or a drop-in override (`systemctl edit wkrt-fm`).
