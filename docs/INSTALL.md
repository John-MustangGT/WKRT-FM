# WKRT-FM — Installation Guide

## System Requirements

| Dependency | Notes |
|------------|-------|
| Python 3.11+ | 3.12 recommended |
| ffmpeg / ffprobe / ffplay | `apt install ffmpeg` |
| Icecast2 | `apt install icecast2` (optional — also streams locally via ffplay) |
| Piper binary | Required for Piper TTS DJ voice |
| Google Cloud credentials | Required only for Google TTS DJs |

---

## 1. Python Dependencies

```bash
pip install -r requirements.txt
```

Key packages: `anthropic`, `mutagen`, `piper-tts` (or local binary), `rich`,
`python-dotenv`, `google-cloud-texttospeech` (for Neon/Google TTS DJs).

---

## 2. Voice Models

### Piper (default — Roxanne)

```bash
bash setup_voices.sh
```

This downloads the `.onnx` model file into `voices/`. The voice used is
configured per-DJ in `settings.toml` under `[djs.tts]`.

### Google Cloud TTS (Neon)

1. Create a Google Cloud project and enable the Text-to-Speech API.
2. Download a service account JSON key.
3. Set the path in your environment or `.env`:

```bash
WKRT_GOOGLE_CREDENTIALS=/path/to/google-creds.json
```

The engine sets `GOOGLE_APPLICATION_CREDENTIALS` automatically from this alias.

---

## 3. Configuration

`config/settings.toml` is gitignored and never committed. Edit it directly:

### Secrets via `.env`

Copy `.env_example` to `.env` and fill in:

```bash
ANTHROPIC_API_KEY=sk-ant-...
WKRT_GOOGLE_CREDENTIALS=/home/roxanne/wkrt/config/google-creds.json
```

### Key settings

```toml
[api]
model      = "claude-sonnet-4-6"
max_tokens = 300

[station]
call_sign = "WKRT"
frequency = "104.7"
city      = "Boston"
timezone  = "America/New_York"

[playlist]
dj_every_n_tracks = 3          # insert a DJ break every N tracks

[web]
port           = 8080
admin_password = "wkrt104"     # password for /admin (empty = open)
```

### Icecast streaming targets

The engine supports streaming to multiple Icecast servers simultaneously.
Each target is an `[[icecast.targets]]` block:

```toml
# Local home server (full admin access)
[[icecast.targets]]
name            = "local"
host            = "localhost"
port            = 8000
mount           = "/wkrt"
source_password = "your-source-password"
admin_password  = "your-admin-password"   # for listener list + kick
hook_port       = 8765                     # webhook listener port

# External service (no admin access needed)
# [[icecast.targets]]
# name            = "caster-fm"
# host            = "streaming.caster.fm"
# port            = 8000
# mount           = "/your-mount"
# source_password = "their-password"
```

Dead targets reconnect automatically in the background without affecting
local streaming.

---

## 4. Music Library

Tracks must live in year subdirectories:

```
music/
  1980/
    AC_DC - You Shook Me All Night Long.mp3
  1984/
    ...
```

### Bulk organise an existing collection

```bash
# Dry run first
python wkrt_organize.py --src /mnt/music --dst ./music --dry-run

# Then for real
python wkrt_organize.py --src /mnt/music --dst ./music
```

### Analyse coverage against a master tracklist

```bash
python wkrt_analyze.py /path/to/mp3-listing.txt
```

---

## 5. Icecast Setup

Install and configure Icecast2:

```bash
sudo apt install icecast2
sudo nano /etc/icecast2/icecast.xml
```

Minimal `icecast.xml` additions:

```xml
<source-password>your-source-password</source-password>
<admin-password>your-admin-password</admin-password>

<!-- Listener webhooks — let WKRT know when someone connects -->
<mount>
  <mount-name>/wkrt</mount-name>
  <on-connect>http://127.0.0.1:8765/connect</on-connect>
  <on-disconnect>http://127.0.0.1:8765/disconnect</on-disconnect>
</mount>
```

The `8765` port matches `hook_port` in your target config. Without the hooks,
listener count is reconciled by polling `/status-json.xsl` every 15 seconds.

---

## 6. systemd — Station Service

```bash
# Edit User= and WorkingDirectory= if needed, then:
sudo cp wkrt-fm.service /etc/systemd/system/
sudo systemctl enable --now wkrt-fm
sudo journalctl -u wkrt-fm -f
```

The `.env` file at the project root is loaded automatically by the service.
For secrets you can also use a systemd drop-in:

```bash
sudo systemctl edit wkrt-fm
# Add:
# [Service]
# Environment=ANTHROPIC_API_KEY=sk-ant-...
```

---

## 7. systemd — Music Ingest Watcher

The path unit watches `new_music/` and triggers `wkrt_ingest.py` whenever
files appear there.

```bash
sudo cp wkrt-ingest.path wkrt-ingest.service /etc/systemd/system/
sudo systemctl enable --now wkrt-ingest.path
```

To test manually:

```bash
cp "some song.mp3" new_music/
python wkrt_ingest.py           # or let systemd fire it automatically
sudo journalctl -u wkrt-ingest.service -f
```

The script:
1. Waits for the file size to stabilise (safe for SCP / slow transfers)
2. Reads the ID3 `date`/`year` tag; falls back to filename or parent dir
3. Moves the file to `music/<year>/`
4. POSTs to `/api/library/ingest` to hot-add it to the running station
5. The DJ announces it on-air with a "just added to the crate" break

If the station is not running, the file is in place and will load on the
next restart.

---

## 8. Admin Web UI

Navigate to `http://host:8080/admin`. The browser will prompt for the
password set in `[web] admin_password`.

| Panel | What it does |
|-------|-------------|
| DJ On Air | Override the time-based DJ rotation or restore auto |
| Queue Next Song | Browse the library and force a specific track next |
| Connected Listeners | Shows IPs and connection times; Kick button per client |

The listener panel only shows clients on targets that have `admin_password`
configured (i.e., your local Icecast — not external services).

---

## 9. Verifying the Setup

```bash
# Check the station is alive
curl http://localhost:8080/api/status | python3 -m json.tool

# Test DJ generation (Claude API → TTS → play)
python main.py --test-dj

# Test TTS only
python main.py --test-tts "You're listening to WKRT 104.7"

# Check Icecast stream
curl -s http://localhost:8000/status-json.xsl | python3 -m json.tool
```
