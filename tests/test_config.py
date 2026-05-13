"""
Focused tests for config path handling and config validation.
"""
import textwrap
import pytest
from pathlib import Path
from unittest.mock import patch

from wkrt.config import validate, load, resolve_paths


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _valid_cfg() -> dict:
    """Return a minimal config dict that passes validation."""
    return {
        "station": {"name": "WKRT-FM", "frequency": "104.7"},
        "paths": {
            "music_dir": "music",
            "spool_dir": "spool",
            "dj_clips_dir": "dj_clips",
            "voices_dir": "voices",
            "log_dir": "logs",
        },
        "playlist": {"dj_every_n_tracks": 3},
        "djs": [
            {"name": "Roxanne", "shift_hours": 3},
        ],
    }


# ---------------------------------------------------------------------------
# validate() — happy path
# ---------------------------------------------------------------------------

def test_validate_passes_on_valid_config():
    """validate() should not raise for a complete config dict."""
    validate(_valid_cfg())  # must not raise


# ---------------------------------------------------------------------------
# validate() — missing sections / keys
# ---------------------------------------------------------------------------

def test_validate_raises_missing_station():
    cfg = _valid_cfg()
    del cfg["station"]
    with pytest.raises(ValueError, match="station"):
        validate(cfg)


def test_validate_raises_missing_path_key():
    cfg = _valid_cfg()
    del cfg["paths"]["spool_dir"]
    with pytest.raises(ValueError, match="paths.spool_dir"):
        validate(cfg)


def test_validate_raises_missing_all_path_keys():
    cfg = _valid_cfg()
    del cfg["paths"]
    err = pytest.raises(ValueError, validate, cfg)
    msg = str(err.value)
    for key in ("music_dir", "spool_dir", "dj_clips_dir", "voices_dir", "log_dir"):
        assert f"paths.{key}" in msg


def test_validate_raises_missing_playlist_key():
    cfg = _valid_cfg()
    del cfg["playlist"]["dj_every_n_tracks"]
    with pytest.raises(ValueError, match="playlist.dj_every_n_tracks"):
        validate(cfg)


def test_validate_raises_no_djs():
    cfg = _valid_cfg()
    cfg["djs"] = []
    with pytest.raises(ValueError, match=r"\[\[djs\]\]"):
        validate(cfg)


def test_validate_raises_dj_missing_name():
    cfg = _valid_cfg()
    del cfg["djs"][0]["name"]
    with pytest.raises(ValueError, match="name"):
        validate(cfg)


def test_validate_raises_dj_missing_shift_hours():
    cfg = _valid_cfg()
    del cfg["djs"][0]["shift_hours"]
    with pytest.raises(ValueError, match="shift_hours"):
        validate(cfg)


def test_validate_collects_multiple_errors():
    """All missing keys should be reported in a single ValueError, not just the first."""
    cfg = _valid_cfg()
    del cfg["station"]
    del cfg["paths"]["music_dir"]
    del cfg["playlist"]["dj_every_n_tracks"]
    with pytest.raises(ValueError) as exc_info:
        validate(cfg)
    msg = str(exc_info.value)
    assert "station" in msg
    assert "paths.music_dir" in msg
    assert "playlist.dj_every_n_tracks" in msg


# ---------------------------------------------------------------------------
# load() — custom path
# ---------------------------------------------------------------------------

def test_load_uses_custom_path(tmp_path):
    """load(path) should read the config at the given path, not the default."""
    toml_content = textwrap.dedent("""\
        [station]
        name = "TestFM"
        frequency = "99.9"

        [api]
        api_key = ""

        [paths]
        music_dir = "music"
        spool_dir = "spool"
        dj_clips_dir = "dj_clips"
        voices_dir = "voices"
        log_dir = "logs"

        [playlist]
        dj_every_n_tracks = 4

        [[djs]]
        name = "TestDJ"
        shift_hours = 6
        tts_backend = "piper"
        persona = "A test DJ."
        [djs.clip_types]
        [djs.tts]
    """)
    cfg_file = tmp_path / "custom_settings.toml"
    cfg_file.write_text(toml_content)

    cfg = load(cfg_file)
    assert cfg["station"]["name"] == "TestFM"
    assert cfg["playlist"]["dj_every_n_tracks"] == 4
    assert cfg["djs"][0]["name"] == "TestDJ"


def test_load_raises_for_missing_file(tmp_path):
    """load() should raise FileNotFoundError for a non-existent config path."""
    with pytest.raises(FileNotFoundError):
        load(tmp_path / "does_not_exist.toml")


def test_load_raises_validation_error_for_bad_config(tmp_path):
    """load() should raise ValueError if required keys are absent."""
    toml_content = textwrap.dedent("""\
        [api]
        api_key = ""
    """)
    cfg_file = tmp_path / "bad_settings.toml"
    cfg_file.write_text(toml_content)

    with pytest.raises(ValueError, match="Config validation failed"):
        load(cfg_file)


# ---------------------------------------------------------------------------
# resolve_paths()
# ---------------------------------------------------------------------------

def test_resolve_paths_makes_relative_paths_absolute(tmp_path):
    cfg = _valid_cfg()
    resolved = resolve_paths(cfg, tmp_path)
    for key in ("music_dir", "spool_dir", "dj_clips_dir", "voices_dir", "log_dir"):
        assert Path(resolved["paths"][key]).is_absolute(), f"{key} should be absolute"


def test_resolve_paths_leaves_absolute_paths_unchanged(tmp_path):
    cfg = _valid_cfg()
    cfg["paths"]["music_dir"] = "/absolute/path/music"
    resolved = resolve_paths(cfg, tmp_path)
    assert resolved["paths"]["music_dir"] == "/absolute/path/music"
