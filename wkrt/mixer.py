"""
Mixer — uses ffmpeg to:
  1. Trim silence from track start/end
  2. Fade out end of track
  3. Concatenate: [track_fadeout] + [dj_clip] + [fade_in_intro_of_next]
  4. Produce a single stitched MP3 segment for the spool

Each "segment" written to spool is:
  [current_track_body] → fade_out → [dj_clip] → fade_in → [next_track_start]

Or without a DJ clip:
  [current_track_body] → crossfade → [next_track_start]
"""
import logging
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


class Mixer:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.spool_dir = Path(cfg["paths"]["spool_dir"])
        self.output_cfg = cfg["output"]
        self.crossfade_s = cfg["playlist"]["crossfade_seconds"]
        self.fade_out_s = cfg["playlist"]["fade_out_seconds"]
        self.talkover_s = cfg["playlist"].get("dj_talkover_seconds", 8)
        self.trim_silence = cfg["playlist"].get("trim_silence", True)
        self.spool_dir.mkdir(parents=True, exist_ok=True)

        self._ffmpeg = shutil.which("ffmpeg")
        if not self._ffmpeg:
            raise RuntimeError("ffmpeg not found")

    def make_segment(
        self,
        track_path: Path,
        dj_clip_path: Optional[Path],
        segment_name: str,
    ) -> tuple[Path, Optional[float]]:
        """
        Produce a finished MP3 segment: track (with fade-out) + optional DJ clip.
        Returns (segment_path, dj_starts_at) where dj_starts_at is the number of
        seconds into the segment when Roxanne begins speaking, or None if no DJ clip.
        """
        out_path = self.spool_dir / f"{segment_name}.mp3"
        dj_starts_at: Optional[float] = None

        if dj_clip_path and dj_clip_path.exists():
            dj_starts_at = self._stitch_with_dj(track_path, dj_clip_path, out_path)
        else:
            self._process_track_only(track_path, out_path)

        log.info(f"Segment ready: {out_path.name}")
        return out_path, dj_starts_at

    def make_crossfade(
        self,
        track_a: Path,
        track_b: Path,
        segment_name: str,
    ) -> Path:
        """
        Crossfade the end of track_a into the start of track_b.
        Used when no DJ clip is inserted.
        """
        out_path = self.spool_dir / f"{segment_name}.mp3"
        cf = self.crossfade_s

        # Get duration of track_a
        dur_a = self._get_duration(track_a)
        trim_end = max(0, dur_a - cf)

        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
            tmp_path = Path(tmp.name)

        try:
            # acrossfade filter: overlap last N seconds of a with first N of b
            filter_complex = (
                f"[0:a]atrim=0:{trim_end + cf},asetpts=PTS-STARTPTS[a];"
                f"[1:a]atrim=0:{cf * 3},asetpts=PTS-STARTPTS[b];"
                f"[a][b]acrossfade=d={cf}:c1=exp:c2=exp[out]"
            )
            cmd = [
                self._ffmpeg, "-y",
                "-i", str(track_a),
                "-i", str(track_b),
                "-filter_complex", filter_complex,
                "-map", "[out]",
                "-b:a", self.output_cfg["bitrate"],
                "-ar", str(self.output_cfg["sample_rate"]),
                "-ac", str(self.output_cfg["channels"]),
                str(out_path),
            ]
            self._run(cmd)
        finally:
            if tmp_path.exists():
                tmp_path.unlink()

        return out_path

    def _stitch_with_dj(self, track_path: Path, dj_path: Path, out_path: Path) -> float:
        """
        Talkover mode: Roxanne starts speaking over the song's fade-out.

        Timeline:
          [---- track body ----][--- fade (talkover_s) ---]
                                 [--- Roxanne talking --------]

        Returns the number of seconds into the segment when Roxanne starts speaking.
        """
        if self.talkover_s <= 0:
            return self._stitch_sequential(track_path, dj_path, out_path)

        dur_track = self._get_duration(track_path)
        dur_dj    = self._get_duration(dj_path)

        # Always leave at least 3s of Roxanne's voice after the song ends,
        # and never start the talkover past the halfway point of the track.
        min_solo = 3.0
        talkover = min(self.talkover_s, max(0, dur_dj - min_solo), dur_track * 0.5)
        fade_start = max(0, dur_track - talkover)

        # Filtergraph:
        #   - Fade out the track over the talkover window
        #   - Prepend silence to the DJ clip so it starts at fade_start
        #   - Mix both streams (normalize=0 keeps natural levels)
        filter_complex = (
            f"[0:a]afade=t=out:st={fade_start:.3f}:d={talkover:.3f}[tfade];"
            f"aevalsrc=0:d={fade_start:.3f}:s=44100:c=stereo[silence];"
            f"[silence][1:a]concat=n=2:v=0:a=1[dj_delayed];"
            f"[tfade][dj_delayed]amix=inputs=2:duration=longest:normalize=0[out]"
        )

        cmd = [
            self._ffmpeg, "-y",
            "-i", str(track_path),
            "-i", str(dj_path),
            "-filter_complex", filter_complex,
            "-map", "[out]",
            "-b:a", self.output_cfg["bitrate"],
            "-ar", str(self.output_cfg["sample_rate"]),
            "-ac", str(self.output_cfg["channels"]),
            str(out_path),
        ]
        self._run(cmd)
        return fade_start

    def _stitch_sequential(self, track_path: Path, dj_path: Path, out_path: Path) -> float:
        """Fallback: track fades out, then DJ speaks (no overlap). Returns DJ start offset."""
        dur = self._get_duration(track_path)
        fade_start = max(0, dur - self.fade_out_s)
        silence_pad = 0.4

        filter_complex = (
            f"[0:a]afade=t=out:st={fade_start}:d={self.fade_out_s}[track_faded];"
            f"aevalsrc=0:d={silence_pad}:s=44100:c=stereo[pad];"
            f"[1:a]asetpts=PTS-STARTPTS[dj];"
            f"[track_faded][pad][dj]concat=n=3:v=0:a=1[out]"
        )

        cmd = [
            self._ffmpeg, "-y",
            "-i", str(track_path),
            "-i", str(dj_path),
            "-filter_complex", filter_complex,
            "-map", "[out]",
            "-b:a", self.output_cfg["bitrate"],
            "-ar", str(self.output_cfg["sample_rate"]),
            "-ac", str(self.output_cfg["channels"]),
            str(out_path),
        ]
        self._run(cmd)
        return dur + silence_pad

    def _process_track_only(self, track_path: Path, out_path: Path):
        """Normalize and copy track with no DJ clip."""
        cmd = [
            self._ffmpeg, "-y",
            "-i", str(track_path),
            "-b:a", self.output_cfg["bitrate"],
            "-ar", str(self.output_cfg["sample_rate"]),
            "-ac", str(self.output_cfg["channels"]),
            str(out_path),
        ]
        self._run(cmd)

    def make_fade_in(self, track_path: Path, segment_name: str) -> Path:
        """
        Apply a short fade-in to a track (used for first track after DJ clip).
        """
        out_path = self.spool_dir / f"{segment_name}_fadein.mp3"
        fi = self.crossfade_s

        cmd = [
            self._ffmpeg, "-y",
            "-i", str(track_path),
            "-af", f"afade=t=in:st=0:d={fi}",
            "-b:a", self.output_cfg["bitrate"],
            "-ar", str(self.output_cfg["sample_rate"]),
            "-ac", str(self.output_cfg["channels"]),
            str(out_path),
        ]
        self._run(cmd)
        return out_path

    def _get_duration(self, path: Path) -> float:
        cmd = [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        try:
            return float(result.stdout.strip())
        except ValueError:
            return 180.0  # fallback: assume 3 min

    def _run(self, cmd: list):
        result = subprocess.run(cmd, capture_output=True, timeout=300)
        if result.returncode != 0:
            raise RuntimeError(
                f"ffmpeg error (rc={result.returncode}):\n"
                f"{result.stderr.decode()[-800:]}"
            )

    def cleanup_spool(self, keep: int = 10):
        """Remove oldest segments from spool, keeping the N most recent."""
        segments = sorted(
            self.spool_dir.glob("*.mp3"),
            key=lambda p: p.stat().st_mtime,
        )
        for old in segments[:-keep]:
            old.unlink(missing_ok=True)
            log.debug(f"Spool cleanup: removed {old.name}")
