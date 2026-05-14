# WKRT-FM — Design Document

## Overview

WKRT is a Python radio engine. It plays a weighted shuffle of a local music
library, inserts AI-generated DJ banter between tracks, synthesizes that banter
to speech, stitches audio segments with ffmpeg, and streams the result to one
or more Icecast servers. A small HTTP server serves a listener web UI and a
password-protected admin panel.

---

## Data Flow

```
music/<year>/*.mp3  ──►  PlaylistQueue  ──►  WKRTEngine (main loop)
                                                      │
                               ┌──────────────────────┴──────────────────────┐
                               │  every dj_every_n_tracks                     │
                               │  (or whenever a crate track is next):        │
                        active DJEngine                               (track only)
                        (Claude API)                                          │
                               │                                              │
                            TTSEngine                                         │
                      (Piper, Google, Kokoro)                                 │
                               │                                              │
                            Mixer (ffmpeg) ────────────────────────────────── ┘
                               │
                        spool/*.mp3 ──► ffmpeg stdin pipe(s) ──► Icecast target(s)
                                    └──► ffplay (local fallback)
```

---

## Module Responsibilities

| Module | Role |
|--------|------|
| `wkrt/config.py` | Loads `settings.toml`, merges env-var overrides, resolves relative paths, resolves metadata templates |
| `wkrt/playlist.py` | Scans `music/<year>/`, reads ID3 tags, weighted-shuffle `PlaylistQueue`, ingest crate |
| `wkrt/dj.py` | Builds prompts for 7 clip types, calls Claude API, falls back to canned lines |
| `wkrt/tts.py` | Dispatches to Piper, Google Cloud TTS, or Kokoro per DJ config; WAV→MP3; SHA-256 cache |
| `wkrt/mixer.py` | ffmpeg filtergraphs: talkover stitch, crossfade, silence trim |
| `wkrt/engine.py` | Main loop; multi-target streaming; DJ rotation; music ingest; ICY metadata |
| `wkrt/cache.py` | `StartupCache` (COLD→WARMING→WARM→RUNNING→COOLING) + `TopOfHourScheduler` |
| `wkrt/hooks.py` | Tiny HTTP server on `hook_port` for Icecast `on-connect`/`on-disconnect` |
| `wkrt/context.py` | Background thread: Boston weather + sports scores; injected into DJ prompts |
| `wkrt/state.py` | Thread-safe station state (now-playing, listener count, DJ, cache state) |
| `wkrt/web.py` | Web UI at `/`, admin at `/admin`, JSON API at `/api/*` |

---

## DJ Roster and Rotation

DJs are defined as `[[djs]]` entries in `settings.toml`. Each entry has:

- `name` — display name (e.g. `"Roxanne"`, `"Neon"`)
- `shift_hours` — hours per rotation block
- `tts_backend` — `"piper"`, `"google"`, or `"kokoro"`
- `persona` — multi-line system prompt injected into every Claude call
- `[djs.clip_types]` — per-DJ clip-type weights
- `[djs.tts]` — voice model, speed, and other backend-specific params

`WKRTEngine.active_dj_cfg()` returns whichever DJ owns the current hour
(`hour % total_shift_period`). An admin override bypasses time-based selection
until cleared.

---

## DJ Clip Types

| Type | When used | Needs |
|------|-----------|-------|
| `between_tracks` | Normal break — references prev and next song | both tracks |
| `trivia` | Fun fact about the just-played song/artist | prev track |
| `dedication` | Fake listener dedication for the next track | next track |
| `station_id` | Call sign and time-of-day | — |
| `top_of_hour` | Pre-generated at :55 for the :00 slot | — |
| `connect_id` | Played when first listener connects | — |
| `new_arrival` | **Forced** when a crate track is coming up next | next track |

`new_arrival` is never in the weighted random draw — the engine forces it
whenever `next_track.from_crate` is `True`.

---

## TTS Backends

`TTSEngine.synthesize(text, dj_cfg)` dispatches on `dj_cfg["tts_backend"]`:

- **`piper`** — local binary; requires `.onnx` model in `voices/`. Falls back
  to 4-second silence if the model file is missing.
- **`google`** — Google Cloud TTS; requires `GOOGLE_APPLICATION_CREDENTIALS`.
  Uses `en-US-Studio-*` voices. Falls back to silence on auth/network failure.
- **`kokoro`** — Kokoro ONNX neural TTS (local, no external binary or API key).
  Requires model files `voices/kokoro-v1.0.onnx` and `voices/kokoro-voices-v1.0.bin`.
  Download via `INSTALL_KOKORO=1 bash scripts/setup_voices.sh`. ONNX model is
  cached in memory after first load. Good choice for Raspberry Pi or
  bandwidth-constrained deployments. Falls back to silence if model files missing.

Cache key is SHA-256 of `"<voice_id>:<text>"`. Two DJs saying the same line
produce separate cached files.

---

## Metadata Mapping

ICY `StreamTitle` and other metadata sent to Icecast are template-driven.
Global defaults are defined in `[metadata_mapping]`; per-target overrides
are set in `[icecast.targets.metadata]`.

**Supported placeholders:**
- `%T` = Track title
- `%a` = Artist name
- `%A` = Album name
- `%Y` = Release year (from annotation or ID3)
- `%G` = Genre (from annotation)
- `%L` = Label (from annotation)

**Example:**
```toml
[metadata_mapping]
StreamTitle = "%a - %T"
StreamUrl = "https://wkrt.local"
Comment = "WKRT-FM 104.7"

[[icecast.targets]]
name = "Primary"
host = "localhost"
[icecast.targets.metadata]
Comment = "WKRT-FM 104.7 Boston"  # override global default

[[icecast.targets]]
name = "CDN Backup"
host = "cdn.example.com"
[icecast.targets.metadata]
StreamTitle = "[BACKUP] %a - %T"   # prepend tag to title
```

Metadata resolution is handled by `wkrt/config.py`:
- `resolve_metadata_template()` replaces placeholders
- `get_metadata_for_target()` merges global defaults with per-target overrides

---

## Multi-Target Icecast Streaming

The engine maintains one persistent ffmpeg process per Icecast target. Targets
are declared as `[[icecast.targets]]` blocks in `settings.toml`.

**Segment write pattern** — each segment is read once from disk, then written
to all live target pipes concurrently via threads. Because each ffmpeg process
uses `-re` (read at native audio rate), all writes block for approximately one
segment duration in parallel — total wall-clock time equals one segment, not
`N × segment`.

**Dead-target recovery** — if a pipe breaks or a process exits, the engine
marks that slot dead and starts a background reconnect thread with exponential
backoff (5 s → 60 s, up to 12 attempts). Other targets continue unaffected.

**Listener polling and admin features** are scoped to targets that have
`admin_password` set (typically just the local server). External targets
(e.g. caster.fm) are stream-only.

---

## Music Ingest Pipeline

```
new_music/<file>
      │
      ▼  (systemd wkrt-ingest.path fires on inotify change)
wkrt_ingest.py
      │  1. wait for file size to stabilise
      │  2. read ID3 date/year tag (fallback: filename → parent dir)
      │  3. move to music/<year>/
      │  4. POST /api/library/ingest
      ▼
WKRTEngine.ingest_tracks()
      │  • reads tags, builds Track(from_crate=True)
      │  • PlaylistQueue.add_track() → appended to _crate list
      ▼
PlaylistQueue.__next__()
      │  • _crate drains before regular shuffle
      │  • track returned with from_crate=True
      ▼
_build_segment()
      │  • detects from_crate on next_track
      │  • forces ClipType.NEW_ARRIVAL regardless of dj_every_n_tracks cadence
      │  • clears from_crate so replays go through normal rotation
      ▼
DJ announces: "just dropped this into the crate — brand new addition"
```

If the station is offline when `wkrt_ingest.py` runs, the file is already in
`music/<year>/` and will be picked up by `scan_library()` on next start.

---

## Startup Cache State Machine

```
COLD ──► WARMING ──► WARM ──► RUNNING ──► COOLING ──► (engine pauses)
                               ▲    │          │
                               └────┘          └──► WARM (on reconnect)
                          (listener count > 0)
```

- **COLD** — nothing generated yet
- **WARMING** — pre-generating `WARMUP_SEGMENTS` (default 3) before accepting
  listeners; DJ segments are skipped (no API calls, saves credits)
- **WARM** — buffer ready; DJ segments resume
- **RUNNING** — at least one listener connected
- **COOLING** — all listeners gone; engine keeps running for
  `COOLING_TIMEOUT` seconds (default 5 min) in case someone reconnects

---

## Web API

All routes under `/api/` return JSON. Routes marked **[auth]** require HTTP
Basic Auth with the password set in `[web] admin_password`.

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/api/status` | — | Full station state snapshot |
| GET | `/api/library` | — | Track library grouped by artist |
| POST | `/api/dj/override` | ✓ | `{"name":"Neon"}` — force a DJ |
| DELETE | `/api/dj/override` | ✓ | Restore time-based rotation |
| POST | `/api/queue/next` | ✓ | `{"artist":…,"title":…,"year":…}` |
| GET | `/api/listeners` | ✓ | Connected Icecast clients (local target) |
| POST | `/api/listeners/kick` | ✓ | `{"id":"5"}` — disconnect a client |
| POST | `/api/library/ingest` | ✓ | `{"paths":[…]}` — hot-add tracks to crate |

---

## Spool and Caching

- **Spool** (`spool/`) — pre-stitched MP3 segments named
  `seg_<index>_<year>_<artist>.mp3`. Cleaned to the 15 most recent every
  10 tracks.
- **DJ clip cache** (`dj_clips/`) — TTS output cached as
  `dj_<sha256[:16]>.mp3`. Shared across restarts; never auto-purged.

---

## Pre-Generation Threading Model

The main loop pre-generates segment N+1 in a background thread while segment N
is playing. This keeps the stream gapless even when Claude API or TTS is slow.

```
main thread:   [play seg N] ─────────────────────► [play seg N+1] ──► …
bg thread:          [gen seg N+1] ─► done
```

If pre-generation finishes before playback ends, the result is stored in
`_next_segment` (a 3-tuple of `(path, dj_starts_at, dj_text)`). If it's still
running when needed, the main thread joins and waits.

`dj_text` is passed from `_build_segment` all the way through to `_play()`,
where it is written to `state.last_dj_script` — this ensures the website
displays what is currently being heard, not what was pre-generated ahead of
time.

---

## ICY Metadata

StreamTitle is pushed to each Icecast target on:
- Every track change (artist – title)
- The moment the DJ talkover begins (a `threading.Timer` fires at `dj_starts_at`
  seconds into the segment, switching the title to "DJ Name — WKRT-FM 104.7")
- Every DJ shift change

The Icecast admin metadata API requires source credentials and is attempted on
all targets that have `source_password` configured.
