"""
Tests for wkrt/config.py — config loading, path resolution, and validation.
"""
import sys
import os
import tempfile
import textwrap
from pathlib import Path

import pytest

# Ensure the repo root is importable even when run from the tests/ directory.
sys.path.insert(0, str(Path(__file__).parent.parent))

from wkrt.config import load, resolve_paths, validate


# ── Minimal valid TOML used across tests ──────────────────────────────────────

_VALID_TOML = textwrap.dedent("""
    [station]
    call_sign = "WKRT"
    frequency = "104.7"
    city = "Boston"
    timezone = "America/New_York"

    [paths]
    music_dir     = "music"
    spool_dir     = "spool"
    dj_clips_dir  = "dj_clips"
    voices_dir    = "voices"
    log_dir       = "logs"

    [playlist]
    dj_every_n_tracks = 3

    [[djs]]
    name         = "Roxanne"
    shift_hours  = 3
    tts_backend  = "piper"
    persona      = "Dry Boston native."

    [[djs]]
    name         = "Neon"
    shift_hours  = 3
    tts_backend  = "google"
    persona      = "Miami-meets-Back-Bay energy."
""")


def _write_toml(content: str) -> Path:
    """Write *content* to a temp file and return its Path."""
    f = tempfile.NamedTemporaryFile(
        mode="w", suffix=".toml", delete=False, encoding="utf-8"
    )
    f.write(content)
    f.flush()
    f.close()
    return Path(f.name)


# ── load() tests ──────────────────────────────────────────────────────────────

class TestLoad:
    def test_load_custom_path(self):
        p = _write_toml(_VALID_TOML)
        try:
            cfg = load(p)
            assert cfg["station"]["call_sign"] == "WKRT"
            assert len(cfg["djs"]) == 2
        finally:
            p.unlink(missing_ok=True)

    def test_env_override_music_dir(self, monkeypatch):
        p = _write_toml(_VALID_TOML)
        monkeypatch.setenv("WKRT_MUSIC_DIR", "/tmp/custom_music")
        try:
            cfg = load(p)
            assert cfg["paths"]["music_dir"] == "/tmp/custom_music"
        finally:
            p.unlink(missing_ok=True)

    def test_env_override_api_key(self, monkeypatch):
        p = _write_toml(_VALID_TOML)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-12345")
        try:
            cfg = load(p)
            assert cfg["api"]["api_key"] == "test-key-12345"
        finally:
            p.unlink(missing_ok=True)

    def test_missing_file_raises(self):
        with pytest.raises(FileNotFoundError):
            load(Path("/nonexistent/path/settings.toml"))


# ── resolve_paths() tests ─────────────────────────────────────────────────────

class TestResolvePaths:
    def test_relative_paths_resolved(self):
        p = _write_toml(_VALID_TOML)
        try:
            cfg = load(p)
            base = Path("/srv/wkrt")
            cfg = resolve_paths(cfg, base)
            assert cfg["paths"]["music_dir"] == "/srv/wkrt/music"
            assert cfg["paths"]["spool_dir"] == "/srv/wkrt/spool"
        finally:
            p.unlink(missing_ok=True)

    def test_absolute_paths_unchanged(self):
        toml = _VALID_TOML.replace('music_dir     = "music"',
                                   'music_dir     = "/absolute/music"')
        p = _write_toml(toml)
        try:
            cfg = load(p)
            base = Path("/srv/wkrt")
            cfg = resolve_paths(cfg, base)
            assert cfg["paths"]["music_dir"] == "/absolute/music"
        finally:
            p.unlink(missing_ok=True)


# ── validate() tests ──────────────────────────────────────────────────────────

class TestValidate:
    def _valid_cfg(self) -> dict:
        p = _write_toml(_VALID_TOML)
        try:
            return load(p)
        finally:
            p.unlink(missing_ok=True)

    def test_valid_config_passes(self):
        validate(self._valid_cfg())   # should not raise

    def test_missing_station_section(self):
        cfg = self._valid_cfg()
        del cfg["station"]
        with pytest.raises(ValueError, match="station"):
            validate(cfg)

    def test_missing_paths_key(self):
        cfg = self._valid_cfg()
        del cfg["paths"]["music_dir"]
        with pytest.raises(ValueError, match="music_dir"):
            validate(cfg)

    def test_missing_dj_every_n_tracks(self):
        cfg = self._valid_cfg()
        del cfg["playlist"]["dj_every_n_tracks"]
        with pytest.raises(ValueError, match="dj_every_n_tracks"):
            validate(cfg)

    def test_no_djs(self):
        cfg = self._valid_cfg()
        cfg["djs"] = []
        with pytest.raises(ValueError, match="No \\[\\[djs\\]\\]"):
            validate(cfg)

    def test_dj_missing_name(self):
        cfg = self._valid_cfg()
        del cfg["djs"][0]["name"]
        with pytest.raises(ValueError, match="name"):
            validate(cfg)

    def test_dj_missing_shift_hours(self):
        cfg = self._valid_cfg()
        del cfg["djs"][1]["shift_hours"]
        with pytest.raises(ValueError, match="shift_hours"):
            validate(cfg)

    def test_error_message_lists_all_problems(self):
        cfg = self._valid_cfg()
        del cfg["station"]
        del cfg["paths"]["spool_dir"]
        with pytest.raises(ValueError) as exc_info:
            validate(cfg)
        msg = str(exc_info.value)
        assert "station" in msg
        assert "spool_dir" in msg
