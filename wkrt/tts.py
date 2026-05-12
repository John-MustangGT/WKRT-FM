"""
Piper TTS wrapper + Google Cloud TTS backend.

synthesize(text, dj_cfg) dispatches to the right backend based on
dj_cfg["tts_backend"] ("piper" or "google") and caches the result by
a hash of (voice_id, text) so two DJs saying the same line stay separate.

Pronunciation hints
-------------------
Piper uses espeak-ng for phonemization, so pronunciation can be guided by
substituting tricky words/names with phonetic spellings before the text
reaches Piper.  Two layers are applied (in order):

1. Built-in table (_PIPER_PRONOUNCE) — common rock-radio names/terms that
   espeak-ng mispronounces out of the box.
2. Per-DJ overrides — set [djs.tts] pronounce = {"word": "spelling"} in
   settings.toml to extend or override the built-in table for that DJ.

Substitutions are case-insensitive whole-word matches.  To keep the Google
TTS path clean, substitutions are only applied when backend == "piper".
"""
import hashlib
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# ── Built-in pronunciation table for Piper / espeak-ng ───────────────────────
# Keys are the words as they appear in DJ scripts (case-insensitive whole-word).
# Values are phonetic spellings that espeak-ng renders correctly.
# Extend per-DJ via [djs.tts] pronounce = {"word": "spelling"} in settings.toml.
_PIPER_PRONOUNCE: dict[str, str] = {
    # Artists
    "AC/DC":            "AC DC",
    "ACDC":             "AC DC",
    "Def Leppard":      "Def Lepperd",
    "Leppard":          "Lepperd",
    "Motley Crue":      "Motley Croo",
    "Mötley Crüe":     "Motley Croo",
    "Crüe":            "Croo",
    "Yngwie":           "Ing-vay",
    "Yngwie Malmsteen": "Ing-vay Malmsteene",
    "Malmsteen":        "Malmsteene",
    "Dio":              "Dee-oh",
    "Siouxsie":         "Soo-see",
    "Axl":              "Axel",
    "Ozzy":             "Ozzy",
    "Osbourne":         "Oz-born",
    "Ozzfest":          "Oz-fest",
    "Aerosmith":        "Air-oh-smith",
    "Whitesnake":       "White-snake",
    "Dokken":           "Dock-en",
    "Ratt":             "Rat",
    "Styx":             "Sticks",
    "REO Speedwagon":   "Ree-oh Speed-wagon",
    "Reo Speedwagon":   "Ree-oh Speed-wagon",
    "Springsteen":      "Spring-steen",
    "Mellencamp":       "Mellen-camp",
    "Seger":            "See-ger",
    "Fogerty":          "Foe-ger-tee",
    "Lynyrd Skynyrd":   "Lin-erd Skin-erd",
    "Lynyrd":           "Lin-erd",
    "Skynyrd":          "Skin-erd",
    "Zeppelin":         "Zepp-uh-lin",
    "Sabbath":          "Sab-uth",
    "Metallica":        "Meh-tal-ica",
    "Megadeth":         "Mega-death",
    "Anthrax":          "Ann-thrax",
    "Pantera":          "Pan-tare-uh",
    "Dimebag":          "Dime-bag",
    "Anselmo":          "An-sell-mo",
    "Vedder":           "Ved-er",
    "Cobain":           "Ko-bane",
    "Grohl":            "Grole",
    # Abbreviations / station IDs
    "WKRT":             "W-K-R-T",
    "FM":               "F-M",
    "LP":               "el-pee",
    "EP":               "ee-pee",
    "104.7":            "one oh four point seven",
}


def _apply_pronounce_table(text: str, extra: dict | None = None) -> str:
    """Replace words/phrases using the pronunciation table.

    Matches are whole-word, case-insensitive.  Multi-word keys (e.g.
    'Lynyrd Skynyrd') are matched before their single-word components
    because keys are sorted longest-first.
    """
    table = dict(_PIPER_PRONOUNCE)
    if extra:
        table.update(extra)
    for key in sorted(table, key=len, reverse=True):
        pattern = re.compile(r'(?<!\w)' + re.escape(key) + r'(?!\w)', re.IGNORECASE)
        text = pattern.sub(table[key], text)
    return text


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

        text = self._preprocess_text(text)

        # Apply pronunciation substitutions for Piper only — Google TTS handles
        # these names well on its own and the phonetic spellings would sound odd.
        if backend == "piper":
            extra_pronounce = tts_cfg.get("pronounce")  # per-DJ overrides
            text = _apply_pronounce_table(text, extra_pronounce)

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

    def _preprocess_text(self, text: str) -> str:
        """
        Clean up text for TTS.
        - Strip asterisks (often used by LLM for emphasis or stage directions)
        - Replace trailing 'in\'' with 'en' to avoid dropped-g artifacts in
          some Piper voices  (e.g. 'Smokin\'' → 'Smoken').
        """
        text = text.replace("*", "")
        text = re.sub(r"(\w+)in'(\W|$)", r"\1en\2", text)
        return text.strip()

    def cleanup_clips(self, max_age_days: int = 7, keep_paths: set | None = None) -> int:
        """Remove cached DJ clips older than max_age_days that are not pinned.

        Args:
            max_age_days: Files older than this many days are eligible for removal.
            keep_paths:   Set of absolute path strings that must never be deleted
                          (e.g. pre-baked fallback clips).

        Returns the number of files removed.
        """
        cutoff = time.time() - (max_age_days * 86_400)
        keep_paths = keep_paths or set()
        removed = 0
        for clip in list(self.dj_clips_dir.glob("dj_*.mp3")):
            if str(clip) in keep_paths:
                continue
            try:
                if clip.stat().st_mtime < cutoff:
                    clip.unlink()
                    removed += 1
                    log.debug(f"DJ clip purged: {clip.name}")
            except OSError:
                pass
        if removed:
            log.info(f"DJ clip cache: purged {removed} clip(s) older than {max_age_days}d")
        return removed
