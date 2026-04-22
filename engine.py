"""
WKRT Engine — main loop.

Workflow:
  1. Scan music library
  2. Build infinite playlist queue
  3. For each track:
     a. If DJ clip due: call Claude API → Piper TTS → stitch segment
     b. Otherwise: crossfade into next track
     c. Write segment to spool
     d. Play segment via ffplay (or stdout pipe for USB gadget phase)
  4. Pre-generate next segment while current plays
"""
import logging
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from .config import load, resolve_paths
from .cache import StartupCache, TopOfHourScheduler
from .hooks import HookServer
from .playlist import scan_library, PlaylistQueue, Track
from .dj import DJEngine
from .tts import TTSEngine
from .mixer import Mixer

console = Console()
log = logging.getLogger(__name__)


def setup_logging(log_dir: str):
    log_path = Path(log_dir) / "wkrt.log"
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(log_path),
            logging.StreamHandler(),
        ],
    )


class WKRTEngine:
    def __init__(self, config_path: Optional[str] = None):
        from pathlib import Path as _Path
        base = _Path(__file__).parent.parent
        self.cfg = load()
        self.cfg = resolve_paths(self.cfg, base)

        setup_logging(self.cfg["paths"]["log_dir"])

        self.dj_every = self.cfg["playlist"]["dj_every_n_tracks"]
        self.track_count = 0
        self.current_track: Optional[Track] = None
        self.next_track: Optional[Track] = None

        # Components
        self.dj = DJEngine(self.cfg)
        self.tts = TTSEngine(self.cfg)
        self.mixer = Mixer(self.cfg)

        self._ffplay = shutil.which("ffplay")
        self._stop = threading.Event()

        # Top-of-hour scheduler
        self.toh = TopOfHourScheduler(self, None)  # cache set after init

        # Startup cache + listener hooks
        self.cache = StartupCache(self)
        hook_port = self.cfg.get("icecast", {}).get("hook_port", 8765)
        self.hooks = HookServer(
            on_connect=self.cache.on_listener_connect,
            on_disconnect=self.cache.on_listener_disconnect,
            port=hook_port,
        )

        # Pre-generation thread
        self._next_segment: Optional[Path] = None
        self._next_segment_ready = threading.Event()
        self._next_segment_lock = threading.Lock()

    def run(self):
        self._print_banner()

        # Scan library
        library = scan_library(self.cfg["paths"]["music_dir"])
        if not library:
            console.print(
                f"[red]No music found in {self.cfg['paths']['music_dir']}[/red]\n"
                f"Create year subdirectories (e.g. music/1984/) and add MP3s."
            )
            return

        year_weights = self.cfg["playlist"].get("year_weights", {})
        queue = PlaylistQueue(library, year_weights)

        console.print(
            f"[cyan]Library:[/cyan] {queue.library_size} tracks across "
            f"{queue.year_count} years"
        )

        # Start hook server and warm the cache
        self.hooks.start()
        self.cache.start_warmup()
        console.print("[yellow]Warming cache...[/yellow]")
        self.cache.wait_until_warm(timeout=120)
        console.print(f"[green]Cache warm — {self.cache.buffer_size} segments ready[/green]")
        self.toh.cache = self.cache
        self.toh.start()
        console.print("[cyan]Top-of-hour scheduler started[/cyan]")

        # Prime the first two tracks
        self.current_track = next(queue)
        self.next_track = next(queue)
        seg_index = 0

        # Pre-generate first segment synchronously so we start immediately
        first_segment = self._build_segment(
            self.current_track, self.next_track, seg_index
        )
        seg_index += 1
        self.track_count += 1

        while not self._stop.is_set():
            # Start pre-generating next segment in background
            future_track = next(queue)
            pre_thread = threading.Thread(
                target=self._pregenerate,
                args=(self.next_track, future_track, seg_index),
                daemon=True,
            )
            pre_thread.start()

            # Play current segment
            self._play(first_segment, self.current_track)

            # Advance
            self.current_track = self.next_track
            self.next_track = future_track
            seg_index += 1
            self.track_count += 1

            # Wait for pre-generated segment
            pre_thread.join(timeout=120)
            with self._next_segment_lock:
                first_segment = self._next_segment or self._build_segment(
                    self.current_track, self.next_track, seg_index
                )
                self._next_segment = None

            # Periodic spool cleanup
            if self.track_count % 10 == 0:
                self.mixer.cleanup_spool(keep=15)

    def _pregenerate(self, current: Track, next_t: Track, idx: int):
        seg = self._build_segment(current, next_t, idx)
        with self._next_segment_lock:
            self._next_segment = seg

    def _build_segment(
        self, track: Track, next_track: Optional[Track], idx: int
    ) -> Path:
        """Build a single playable segment: track + optional DJ clip."""
        segment_name = f"seg_{idx:06d}_{track.year}_{self._safe_name(track.artist)}"
        dj_clip_path: Optional[Path] = None

        # Decide if we insert a DJ clip after this track
        insert_dj = self.dj_every > 0 and (self.track_count % self.dj_every == 0)

        if insert_dj:
            try:
                script = self.dj.generate(
                    prev_track=track,
                    next_track=next_track,
                )
                self._print_dj(script.text)
                dj_clip_path = self.tts.synthesize(script.text)
            except Exception as e:
                log.error(f"DJ generation failed: {e}")

        return self.mixer.make_segment(track.path, dj_clip_path, segment_name)

    def _play(self, segment_path: Path, track: Track):
        """Play a segment via ffplay. Blocks until done."""
        self._print_now_playing(track)

        if not self._ffplay:
            log.warning("ffplay not found — cannot play audio")
            time.sleep(5)
            return

        cmd = [
            self._ffplay, "-nodisp", "-autoexit", "-loglevel", "quiet",
            str(segment_path),
        ]
        try:
            subprocess.run(cmd, timeout=600)
        except subprocess.TimeoutExpired:
            log.warning(f"Playback timeout on {segment_path.name}")
        except KeyboardInterrupt:
            self._stop.set()

    def stop(self):
        self._stop.set()

    def pause(self):
        """Called by cache when cooling timeout reached — no listeners."""
        log.info("Engine pausing — no listeners")
        # Currently just logs; future: stop ffmpeg pipe to Icecast

    def build_next_segment(self) -> Optional[Path]:
        """Called by cache warmup to pre-generate a segment."""
        if self.next_track is None:
            return None
        idx = self.track_count
        seg = self._build_segment(self.current_track or self.next_track,
                                   self.next_track, idx)
        return seg

    # ── Display helpers ──────────────────────────────────────────────────────

    def _print_banner(self):
        station = self.cfg["station"]
        console.print(Panel(
            Text.from_markup(
                f"[bold cyan]{station['call_sign']}-FM {station['frequency']}[/bold cyan]\n"
                f"[dim]{station['tagline']}[/dim]"
            ),
            title="[bold magenta]WKRT ENGINE[/bold magenta]",
            border_style="cyan",
        ))

    def _print_now_playing(self, track: Track):
        console.print(
            f"[green]▶[/green] [white]{track.artist}[/white] — "
            f"[yellow]{track.title}[/yellow] "
            f"[dim]({track.year})[/dim]"
        )

    def _print_dj(self, text: str):
        console.print(
            Panel(
                f"[italic cyan]{text}[/italic cyan]",
                title="[magenta]DJ ROXANNE[/magenta]",
                border_style="magenta",
                padding=(0, 1),
            )
        )

    @staticmethod
    def _safe_name(s: str) -> str:
        return "".join(c if c.isalnum() else "_" for c in s)[:20]
