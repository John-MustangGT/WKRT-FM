# WKRT-FM 104.7 — Retro 80s Radio Engine

Raspberry Pi Zero 2W (or Ubuntu) classic rock radio station.
Claude API generates DJ banter. Piper TTS synthesizes the voice.
ffmpeg handles crossfades and stitching.

## Project Structure

```
wkrt/
├── config/
│   └── settings.toml          # All configuration
├── music/                     # Your MP3s — organized by year
│   ├── 1980/
│   ├── 1981/
│   └── ...
├── spool/                     # Output segments (served to head unit)
├── dj_clips/                  # Generated TTS clips (cached)
├── voices/                    # Piper voice model files
├── logs/
├── wkrt/                      # Python package
│   ├── __init__.py
│   ├── playlist.py            # Queue/shuffle logic
│   ├── dj.py                  # Claude API script generation
│   ├── tts.py                 # Piper TTS wrapper
│   ├── mixer.py               # ffmpeg crossfade/stitching
│   └── engine.py              # Main loop
├── main.py                    # Entry point
├── setup_voices.sh            # Download Piper voice model
└── requirements.txt
```

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Download Piper voice
bash setup_voices.sh

# 3. Put MP3s in music/<year>/ folders

# 4. Set ANTHROPIC_API_KEY in environment or config/settings.toml

# 5. Run
python main.py
```

## Music Organization

```
music/
├── 1980/
│   ├── AC_DC - You Shook Me All Night Long.mp3
│   ├── Pat_Benatar - Hit Me with Your Best Shot.mp3
│   └── ...
├── 1981/
│   └── ...
```

Filenames don't need to match exactly — ID3 tags are read for metadata.
Falls back to filename parsing if tags are missing.

## Config

Edit `config/settings.toml` to set:
- Music directory
- API key
- DJ frequency (every N tracks)
- Station call sign and personality
- Year weighting (play more of certain years)
