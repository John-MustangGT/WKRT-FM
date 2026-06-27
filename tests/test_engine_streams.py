"""
Tests for stream target handling in wkrt/engine.py.
"""
import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from wkrt.engine import WKRTEngine


def _make_engine() -> WKRTEngine:
    engine = WKRTEngine.__new__(WKRTEngine)
    engine.cfg = {
        "station": {
            "call_sign": "WKRT",
            "frequency": "104.7",
            "tagline": "The Home of Classic Rock",
        }
    }
    engine._reconnecting = set()
    return engine


class _DummyProc:
    def __init__(self):
        self.stdin = io.BytesIO()

    def poll(self):
        return None


class TestStreamTargets:
    def test_icecast_target_uses_icecast_metadata_flags(self, monkeypatch):
        engine = _make_engine()
        target = {
            "name": "Primary",
            "host": "localhost",
            "port": 8000,
            "mount": "/wkrt",
            "source_password": "secret",
            "codec": "mp3",
        }

        captured = {}

        def fake_popen(cmd, **kwargs):
            captured["cmd"] = cmd
            return _DummyProc()

        monkeypatch.setattr("wkrt.engine.subprocess.Popen", fake_popen)

        proc = engine._start_stream(target)

        assert proc is not None
        cmd = captured["cmd"]
        assert cmd[-1] == "icecast://source:secret@localhost:8000/wkrt"
        assert "-ice_name" in cmd
        assert "-ice_description" in cmd
        assert "-ice_genre" in cmd

    def test_rtsp_target_uses_rtsp_url_without_icecast_flags(self, monkeypatch):
        engine = _make_engine()
        target = {
            "name": "MediaMTX",
            "url": "rtsp://localhost:8554/radio",
            "codec": "opus",
            "bitrate": 128,
            "format": "rtsp",
            "metadata_backend": "rtsp",
            "metadata_title": "WKRT-FM 104.7",
            "metadata_comment": "The Home of Classic Rock",
            "ffmpeg_audio_args": ["-rtsp_transport", "udp"],
        }

        captured = {}

        def fake_popen(cmd, **kwargs):
            captured["cmd"] = cmd
            return _DummyProc()

        monkeypatch.setattr("wkrt.engine.subprocess.Popen", fake_popen)

        proc = engine._start_stream(target)

        assert proc is not None
        cmd = captured["cmd"]
        assert cmd[-1] == "rtsp://localhost:8554/radio"
        assert "-ice_name" not in cmd
        assert any(cmd[i:i + 2] == ["-f", "rtsp"] for i in range(len(cmd) - 1))
        assert "-rtsp_transport" in cmd
        assert any(cmd[i:i + 2] == ["-metadata", "title=WKRT-FM 104.7"] for i in range(len(cmd) - 1))
        assert any(cmd[i:i + 2] == ["-metadata", "comment=The Home of Classic Rock"] for i in range(len(cmd) - 1))
        assert engine._target_public_url(target) == "rtsp://localhost:8554/radio"

    def test_rest_metadata_backend_posts_json_payload(self, monkeypatch):
        engine = _make_engine()
        engine._targets = [
            {
                "name": "MediaMTX",
                "url": "rtsp://localhost:8554/wkrt",
                "mount": "/wkrt",
                "metadata_backend": "mediamtx_rest",
                "metadata_rest_url": "http://127.0.0.1:9997/v3/metadata/{path}",
            }
        ]

        captured = {}

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        def fake_urlopen(req, timeout=0):
            captured["url"] = req.full_url
            captured["data"] = req.data
            captured["content_type"] = req.get_header("Content-type")
            return _Resp()

        monkeypatch.setattr("wkrt.engine.urlopen", fake_urlopen)

        engine._send_icy_metadata("Roxanne - WKRT-FM 104.7")

        assert captured["url"] == "http://127.0.0.1:9997/v3/metadata/wkrt"
        assert captured["content_type"] == "application/json"
        payload = json.loads(captured["data"].decode("utf-8"))
        assert payload["streamTitle"] == "Roxanne - WKRT-FM 104.7"
        assert payload["metadata"]["StreamTitle"] == "Roxanne - WKRT-FM 104.7"
