"""
Piper TTS wrapper.
Calls the piper binary to synthesize DJ scripts to WAV, then converts to MP3.
Caches clips by content hash to avoid re-generating identical scripts.
"""
import hashlib
import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)


class TTSEngine:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.voices_dir = Path(cfg["paths"]["voices_dir"])
        self.dj_clips_dir = Path(cfg["paths"]["dj_clips_dir"])
        self.voice_model = cfg["tts"]["voice_model"]
        self.speed = cfg["tts"].get("speed", 0.92)
        self.noise_scale = cfg["tts"].get("noise_scale", 0.667)
        self.noise_w = cfg["tts"].get("noise_w", 0.8)
        self.output_cfg = cfg["output"]

        self.dj_clips_dir.mkdir(parents=True, exist_ok=True)

        self._piper_bin = shutil.which("piper") or shutil.which("piper-tts")
        self._ffmpeg_bin = shutil.which("ffmpeg")

        if not self._piper_bin:
            log.warning("piper binary not found — TTS will produce silent clips")
        if not self._ffmpeg_bin:
            raise RuntimeError("ffmpeg not found — required for audio processing")

    @property
    def model_path(self) -> Path:
        return self.voices_dir / f"{self.voice_model}.onnx"

    @property
    def model_config_path(self) -> Path:
        return self.voices_dir / f"{self.voice_model}.onnx.json"

    def synthesize(self, text: str) -> Path:
        """
        Synthesize text to MP3. Returns path to the MP3 file.
        Cached by text hash — same script returns the same file.
        """
        cache_key = hashlib.sha256(text.encode()).hexdigest()[:16]
        out_path = self.dj_clips_dir / f"dj_{cache_key}.mp3"

        if out_path.exists():
            log.debug(f"TTS cache hit: {cache_key}")
            return out_path

        if not self._piper_bin or not self.model_path.exists():
            log.warning(f"Piper unavailable — generating silence for clip")
            return self._generate_silence(out_path, duration=4.0)

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp_wav:
            wav_path = Path(tmp_wav.name)

        try:
            self._run_piper(text, wav_path)
            self._wav_to_mp3(wav_path, out_path)
        finally:
            if wav_path.exists():
                wav_path.unlink()

        log.info(f"TTS synthesized: {out_path.name} ({len(text)} chars)")
        return out_path

    def _run_piper(self, text: str, wav_path: Path):
        cmd = [
            self._piper_bin,
            "--model", str(self.model_path),
            "--config", str(self.model_config_path),
            "--output_file", str(wav_path),
            "--length_scale", str(round(1.0 / self.speed, 3)),
            "--noise_scale", str(self.noise_scale),
            "--noise_w", str(self.noise_w),
        ]
        import os
        env = os.environ.copy()
        # Piper looks for espeak-ng-data at /usr/share/espeak-ng-data; on some
        # distros it lands under /usr/lib/<arch>/espeak-ng-data instead.
        if "ESPEAK_DATA_PATH" not in env:
            candidates = [
                "/usr/share/espeak-ng-data",
                "/usr/lib/x86_64-linux-gnu/espeak-ng-data",
                "/usr/lib/aarch64-linux-gnu/espeak-ng-data",
            ]
            for p in candidates:
                if Path(p).exists():
                    env["ESPEAK_DATA_PATH"] = p
                    break
        result = subprocess.run(
            cmd,
            input=text.encode(),
            capture_output=True,
            timeout=60,
            env=env,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"Piper failed (rc={result.returncode}): {result.stderr.decode()}"
            )

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
            raise RuntimeError(
                f"ffmpeg wav→mp3 failed: {result.stderr.decode()[-500:]}"
            )

    def _generate_silence(self, out_path: Path, duration: float = 3.0) -> Path:
        """Generate a silent MP3 as fallback when Piper is unavailable."""
        cmd = [
            self._ffmpeg_bin, "-y",
            "-f", "lavfi",
            "-i", f"anullsrc=channel_layout=stereo:sample_rate=44100",
            "-t", str(duration),
            "-b:a", self.output_cfg["bitrate"],
            str(out_path),
        ]
        subprocess.run(cmd, capture_output=True, timeout=30)
        return out_path
