"""
Configuration loader — reads settings.toml, merges env vars.
"""
import os
import sys
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib  # type: ignore


_DEFAULT_CONFIG_PATH = Path(__file__).parent.parent / "config" / "settings.toml"


def load(path: Path = _DEFAULT_CONFIG_PATH) -> dict:
    with open(path, "rb") as f:
        cfg = tomllib.load(f)

    # ENV overrides
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if api_key:
        cfg.setdefault("api", {})["api_key"] = api_key

    music_dir = os.environ.get("WKRT_MUSIC_DIR", "")
    if music_dir:
        cfg.setdefault("paths", {})["music_dir"] = music_dir

    # GOOGLE_APPLICATION_CREDENTIALS is read automatically by the Google SDK,
    # but propagate WKRT_GOOGLE_CREDENTIALS as an alias for the systemd unit.
    google_creds = os.environ.get("WKRT_GOOGLE_CREDENTIALS", "")
    if google_creds:
        os.environ.setdefault("GOOGLE_APPLICATION_CREDENTIALS", google_creds)

    return cfg


def resolve_paths(cfg: dict, base: Path) -> dict:
    """Resolve relative paths in config against base directory."""
    for key in ("music_dir", "spool_dir", "dj_clips_dir", "voices_dir", "log_dir"):
        p = Path(cfg["paths"][key])
        if not p.is_absolute():
            cfg["paths"][key] = str(base / p)
    return cfg
