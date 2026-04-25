# WKRT-FM 104.7 — Retro 80s Radio Engine

A Python-based internet radio station that plays classic rock, generates AI DJ
banter via the Claude API, synthesizes voice via Piper TTS or Google Cloud TTS,
and streams continuously to one or more Icecast servers.

Designed to run on a Raspberry Pi Zero 2W or any Linux box.

---

## Features

- **AI DJ rotation** — multiple DJ personalities (Roxanne, Neon) take shifts
  based on the hour; each has their own voice, persona, and clip-type weights
- **Six DJ clip types** — between-tracks banter, trivia, dedications, station
  IDs, top-of-hour, and new-arrival announcements for freshly ingested tracks
- **Two TTS backends** — local Piper (offline, fast) or Google Cloud TTS
  (Studio voices, higher quality)
- **Multi-target Icecast streaming** — stream to your home server, a VPN
  endpoint, and an external service simultaneously; targets reconnect
  independently on failure
- **Live Boston context** — weather and sports scores injected into DJ prompts
- **Web UI** — listener view at `/`, password-protected admin at `/admin`
- **Admin controls** — DJ override, queue next song, view connected listeners,
  kick stale connections
- **Music ingest pipeline** — drop files into `new_music/`, systemd fires
  `wkrt_ingest.py`, files land in `music/<year>/`, and the DJ announces them
  on-air with a special "just added to the crate" break
- **Startup cache** — pre-generates segments before the first listener connects;
  engine pauses automatically after all listeners disconnect

---

## Project Structure

```
wkrt/
├── config/
│   └── settings.toml          # All configuration (gitignored — never committed)
├── music/                     # MP3s organised by year (gitignored)
│   ├── 1980/
│   └── ...
├── new_music/                 # Drop new tracks here for automatic ingestion
├── spool/                     # Pre-stitched playback segments (auto-cleaned)
├── dj_clips/                  # TTS output cache (keyed by SHA-256)
├── voices/                    # Piper voice model files (.onnx)
├── logs/
├── templates/                 # Web UI HTML (index.html, admin.html)
├── wkrt/                      # Python package
│   ├── config.py              # Settings loader + env-var overrides
│   ├── playlist.py            # Weighted shuffle queue + ingest crate
│   ├── dj.py                  # Claude API prompt builder + clip types
│   ├── tts.py                 # Piper / Google TTS dispatcher + cache
│   ├── mixer.py               # ffmpeg crossfade / talkover stitching
│   ├── engine.py              # Main loop, multi-target streaming, ingest API
│   ├── cache.py               # StartupCache state machine + top-of-hour scheduler
│   ├── hooks.py               # Icecast on-connect/on-disconnect webhook server
│   ├── context.py             # Boston weather + sports background fetcher
│   ├── state.py               # Thread-safe shared station state
│   └── web.py                 # HTTP server — UI + JSON API
├── main.py                    # Entry point
├── wkrt_ingest.py             # Music drop-folder ingest script
├── wkrt_analyze.py            # Analyse music collection coverage
├── wkrt_organize.py           # Bulk-organise tracks into year directories
├── wkrt-fm.service            # systemd unit for the station
├── wkrt-ingest.path           # systemd path unit — watches new_music/
├── wkrt-ingest.service        # systemd unit — runs wkrt_ingest.py on change
└── requirements.txt
```

---

## Quick Start

```bash
# 1. Install Python dependencies
pip install -r requirements.txt

# 2. Download Piper voice model
bash setup_voices.sh

# 3. Copy and edit config
#    (settings.toml is gitignored — fill in your API keys and paths)
cp config/settings.toml.example config/settings.toml   # if example exists
#    or edit config/settings.toml directly

# 4. Organise your music
python wkrt_organize.py --src /path/to/music --dst ./music --dry-run
python wkrt_organize.py --src /path/to/music --dst ./music

# 5. Set your Anthropic API key
export ANTHROPIC_API_KEY=sk-ant-...

# 6. Run
python main.py
```

See **[INSTALL.md](INSTALL.md)** for full setup including Icecast, systemd,
Google TTS, and the ingest pipeline.

---

## Adding New Music

Drop audio files into `new_music/` and run:

```bash
python wkrt_ingest.py
```

The script reads ID3 tags to find the year, moves the file to `music/<year>/`,
and hot-adds it to the running station. The DJ introduces the track on-air with
a special "just added to the crate" announcement.

With the systemd path unit installed this happens automatically whenever a file
lands in `new_music/`.

---

## Web UI

| URL | Description |
|-----|-------------|
| `http://host:8080/` | Listener view — now playing, recent tracks, stream link |
| `http://host:8080/admin` | Admin panel (password-protected) |
| `http://host:8080/api/status` | JSON station state |
| `http://host:8080/api/library` | JSON track library |

---

## CLI Flags

```bash
python main.py            # run the station
python main.py --scan     # scan library and print year/track stats
python main.py --test-dj  # generate one DJ clip end-to-end
python main.py --test-tts "Hello WKRT"  # test TTS only
```
