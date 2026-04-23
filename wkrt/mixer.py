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
    ) -> Path:
        """
        Produce a finished MP3 segment: track (with fade-out) + optional DJ clip.
        Returns path to the segment in the spool directory.
        """
        out_path = self.spool_dir / f"{segment_name}.mp3"

        if dj_clip_path and dj_clip_path.exists():
            self._stitch_with_dj(track_path, dj_clip_path, out_path)
        else:
            self._process_track_only(track_path, out_path)

        log.info(f"Segment ready: {out_path.name}")
        return out_path

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

    def _stitch_with_dj(self, track_path: Path, dj_path: Path, out_path: Path):
        """
        track → fade out last N seconds → dj clip → output
        The next track will be a separate segment (fade-in handled there).
        """
        dur = self._get_duration(track_path)
        fade_start = max(0, dur - self.fade_out_s)

        # Build filtergraph:
        # 1. Apply fade-out to the track
        # 2. Concatenate with DJ clip
        # 3. Add a short silence pad between them for breathing room
        silence_pad = 0.4  # seconds

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
