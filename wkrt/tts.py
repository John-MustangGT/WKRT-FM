"""
Piper TTS wrapper + Google Cloud TTS backend.

synthesize(text, dj_cfg) dispatches to the right backend based on
dj_cfg["tts_backend"] ("piper" or "google") and caches the result by
a hash of (voice_id, text) so two DJs saying the same line stay separate.
"""
import hashlib
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


class TTSEngine:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.voices_dir = Path(cfg["paths"]["voices_dir"])
        self.dj_clips_dir = Path(cfg["paths"]["dj_clips_dir"])
        self.output_cfg = cfg["output"]

        self.dj_clips_dir.mkdir(parents=True, exist_ok=True)

        self._piper_bin = shutil.which("piper") or shutil.which("piper-tts")
        self._ffmpeg_bin = shutil.which("ffmpeg")

        if not self._piper_bin:
            log.warning("piper binary not found — Piper TTS will produce silent clips")
        if not self._ffmpeg_bin:
            raise RuntimeError("ffmpeg not found — required for audio processing")

    def synthesize(self, text: str, dj_cfg: dict) -> Path:
        """
        Synthesize text to MP3 using the backend specified in dj_cfg.
        Returns path to the cached MP3 file.
        """
        tts_cfg = dj_cfg.get("tts", {})
        backend = dj_cfg.get("tts_backend", "piper")
        voice_id = tts_cfg.get("voice_model") or tts_cfg.get("google_voice", "default")

        cache_key = hashlib.sha256(f"{voice_id}:{text}".encode()).hexdigest()[:16]
        out_path = self.dj_clips_dir / f"dj_{cache_key}.mp3"

        if out_path.exists():
            log.debug(f"TTS cache hit: {cache_key} ({dj_cfg.get('name', backend)})")
            return out_path

        wav_path: Optional[Path] = None
        try:
            if backend == "google":
                wav_path = self._google_tts(text, tts_cfg)
            else:
                wav_path = self._piper_tts(text, tts_cfg)
            self._wav_to_mp3(wav_path, out_path)
        finally:
            if wav_path and wav_path.exists():
                wav_path.unlink()

        log.info(f"TTS synthesized [{dj_cfg.get('name', backend)}]: {out_path.name} ({len(text)} chars)")
        return out_path

    # ── Piper backend ─────────────────────────────────────────────────────────

    def _piper_tts(self, text: str, tts_cfg: dict) -> Path:
        voice_model = tts_cfg.get("voice_model", "en_US-lessac-high")
        model_path = self.voices_dir / f"{voice_model}.onnx"
        model_config = self.voices_dir / f"{voice_model}.onnx.json"

        if not self._piper_bin or not model_path.exists():
            log.warning(f"Piper model {voice_model!r} unavailable — generating silence")
            return self._silence_wav()

        speed = tts_cfg.get("speed", 0.92)
        noise_scale = tts_cfg.get("noise_scale", 0.667)
        noise_w = tts_cfg.get("noise_w", 0.8)

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            wav_path = Path(tmp.name)

        cmd = [
            self._piper_bin,
            "--model", str(model_path),
            "--config", str(model_config),
            "--output_file", str(wav_path),
            "--length_scale", str(round(1.0 / speed, 3)),
            "--noise_scale", str(noise_scale),
            "--noise_w", str(noise_w),
        ]
        env = os.environ.copy()
        if "ESPEAK_DATA_PATH" not in env:
            for candidate in (
                "/usr/share/espeak-ng-data",
                "/usr/lib/x86_64-linux-gnu/espeak-ng-data",
                "/usr/lib/aarch64-linux-gnu/espeak-ng-data",
            ):
                if Path(candidate).exists():
                    env["ESPEAK_DATA_PATH"] = candidate
                    break

        result = subprocess.run(cmd, input=text.encode(), capture_output=True, timeout=60, env=env)
        if result.returncode != 0:
            raise RuntimeError(f"Piper failed (rc={result.returncode}): {result.stderr.decode()}")

        return wav_path

    # ── Google Cloud TTS backend ──────────────────────────────────────────────

    def _google_tts(self, text: str, tts_cfg: dict) -> Path:
        try:
            from google.cloud import texttospeech
        except ImportError:
            log.error("google-cloud-texttospeech not installed — pip install google-cloud-texttospeech")
            return self._silence_wav()

        voice_name = tts_cfg.get("google_voice", "en-US-Studio-O")
        speaking_rate = tts_cfg.get("speaking_rate", 1.0)

        try:
            client = texttospeech.TextToSpeechClient()
            response = client.synthesize_speech(
                input=texttospeech.SynthesisInput(text=text),
                voice=texttospeech.VoiceSelectionParams(
                    language_code="en-US",
                    name=voice_name,
                ),
                audio_config=texttospeech.AudioConfig(
                    audio_encoding=texttospeech.AudioEncoding.LINEAR16,
                    speaking_rate=speaking_rate,
                ),
            )
        except Exception as e:
            log.error(f"Google TTS failed: {e} — falling back to silence")
            return self._silence_wav()

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp.write(response.audio_content)
            return Path(tmp.name)

    # ── Shared helpers ────────────────────────────────────────────────────────

    def _wav_to_mp3(self, wav_path: Path, mp3_path: Path):
        sr = self.output_cfg["sample_rate"]
        br = self.output_cfg["bitrate"]
        cmd = [
            self._ffmpeg_bin, "-y",
            "-i", str(wav_path),
            "-ar", str(sr),
            "-ac", "2",
            "-b:a", br,
            str(mp3_path),
        ]
        result = subprocess.run(cmd, capture_output=True, timeout=60)
        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg wav→mp3 failed: {result.stderr.decode()[-500:]}")

    def _silence_wav(self, duration: float = 4.0) -> Path:
        """Generate a short silent WAV as a fallback clip."""
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            wav_path = Path(tmp.name)
        cmd = [
            self._ffmpeg_bin, "-y",
            "-f", "lavfi",
            "-i", f"anullsrc=channel_layout=stereo:sample_rate=44100",
            "-t", str(duration),
            wav_path,
        ]
        subprocess.run(cmd, capture_output=True, timeout=30)
        return wav_path
