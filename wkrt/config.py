"""
Configuration loader — reads settings.toml, merges env vars.
"""
import os
import sys
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

from dotenv import load_dotenv

if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib  # type: ignore


_DEFAULT_CONFIG_PATH = Path(__file__).parent.parent / "config" / "settings.toml"


def load(path: Optional[Path] = None) -> dict:
    # Load .env file from project root
    load_dotenv()

    config_path = path if path is not None else _DEFAULT_CONFIG_PATH
    with open(config_path, "rb") as f:
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

    validate(cfg)
    return cfg


def validate(cfg: dict) -> None:
    """Raise ValueError with a clear, actionable message for missing required config keys."""
    errors: list[str] = []

    if "station" not in cfg:
        errors.append("Missing required section: [station]")

    required_path_keys = ("music_dir", "spool_dir", "dj_clips_dir", "voices_dir", "log_dir")
    for key in required_path_keys:
        if key not in cfg.get("paths", {}):
            errors.append(f"Missing required key: paths.{key}")

    if "dj_every_n_tracks" not in cfg.get("playlist", {}):
        errors.append("Missing required key: playlist.dj_every_n_tracks")

    djs = cfg.get("djs", [])
    if not djs:
        errors.append("Missing required section: [[djs]] (no DJ entries found)")
    else:
        for i, dj in enumerate(djs):
            for key in ("name", "shift_hours"):
                if key not in dj:
                    errors.append(f"[[djs]] entry {i} is missing required key: {key}")

    if errors:
        raise ValueError(
            "Config validation failed:\n" + "\n".join(f"  - {e}" for e in errors)
        )


def resolve_paths(cfg: dict, base: Path) -> dict:
    """Resolve relative paths in config against base directory."""
    for key in ("music_dir", "spool_dir", "dj_clips_dir", "voices_dir", "log_dir"):
        p = Path(cfg["paths"][key])
        if not p.is_absolute():
            cfg["paths"][key] = str(base / p)
    return cfg
