"""
wkrt/dj_stats.py — Per-DJ API and performance statistics.

Tracks API call counts, token usage, latency, TTS timing, and fallback
events. Persisted to config/dj_stats.json so stats survive restarts.
"""
import copy
import json
import threading
from datetime import datetime, timezone
from pathlib import Path


_BLANK_DJ: dict = {
    "api_calls": 0,
    "fallbacks": 0,
    "input_tokens": 0,
    "output_tokens": 0,
    "total_latency_ms": 0.0,
    "tts_calls": 0,
    "total_tts_ms": 0.0,
    "segment_calls": 0,
    "total_segment_ms": 0.0,
    "clip_types": {},
    "last_updated": None,
}


class DJStats:
    def __init__(self, config_dir: Path):
        self._path = config_dir / "dj_stats.json"
        self._lock = threading.Lock()
        self._data: dict = self._load()

    def _load(self) -> dict:
        try:
            if self._path.exists():
                return json.loads(self._path.read_text())
        except Exception:
            pass
        return {}

    def _save(self):
        self._path.write_text(json.dumps(self._data, indent=2))

    def _dj(self, name: str) -> dict:
        if name not in self._data:
            self._data[name] = copy.deepcopy(_BLANK_DJ)
        return self._data[name]

    def record_api_call(
        self,
        dj_name: str,
        clip_type: str,
        input_tokens: int,
        output_tokens: int,
        latency_ms: float,
    ):
        with self._lock:
            d = self._dj(dj_name)
            d["api_calls"] += 1
            d["input_tokens"] += input_tokens
            d["output_tokens"] += output_tokens
            d["total_latency_ms"] += latency_ms
            d["clip_types"][clip_type] = d["clip_types"].get(clip_type, 0) + 1
            d["last_updated"] = datetime.now(timezone.utc).isoformat()
            self._save()

    def record_fallback(self, dj_name: str):
        with self._lock:
            d = self._dj(dj_name)
            d["fallbacks"] += 1
            d["last_updated"] = datetime.now(timezone.utc).isoformat()
            self._save()

    def record_tts(self, dj_name: str, latency_ms: float):
        with self._lock:
            d = self._dj(dj_name)
            d["tts_calls"] += 1
            d["total_tts_ms"] += latency_ms
            d["last_updated"] = datetime.now(timezone.utc).isoformat()
            self._save()

    def record_segment(self, dj_name: str, total_ms: float):
        with self._lock:
            d = self._dj(dj_name)
            d["segment_calls"] += 1
            d["total_segment_ms"] += total_ms
            d["last_updated"] = datetime.now(timezone.utc).isoformat()
            self._save()

    def reset(self):
        with self._lock:
            self._data = {}
            self._save()

    def to_dict(self) -> dict:
        with self._lock:
            result = {}
            for name, s in self._data.items():
                result[name] = {
                    **s,
                    "avg_latency_ms": (
                        s["total_latency_ms"] / s["api_calls"] if s["api_calls"] else 0
                    ),
                    "avg_tts_ms": (
                        s["total_tts_ms"] / s["tts_calls"] if s["tts_calls"] else 0
                    ),
                    "avg_segment_ms": (
                        s["total_segment_ms"] / s["segment_calls"]
                        if s["segment_calls"] else 0
                    ),
                }
            return result
