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
from .state import StationState
from .web import WebServer

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
        self._stream_proc: Optional[subprocess.Popen] = None

        self._listener_count = 0
        self._connect_id_pending = threading.Event()

        self.state = StationState()

        # Top-of-hour scheduler
        self.toh = TopOfHourScheduler(self, None)  # cache set after init

        # Startup cache + listener hooks
        self.cache = StartupCache(self)
        hook_port = self.cfg.get("icecast", {}).get("hook_port", 8765)
        self.hooks = HookServer(
            on_connect=self._on_listener_connect,
            on_disconnect=self._on_listener_disconnect,
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

        # Start web UI
        web_cfg = self.cfg.get("web", {})
        web_port = web_cfg.get("port", 8080)
        ice = self.cfg.get("icecast", {})
        self.state.stream_port = ice.get("port", 8000)
        self.state.stream_mount = ice.get("mount", "/wkrt")
        self.state.stream_url = (
            f"http://{ice.get('host', 'localhost')}:{self.state.stream_port}"
            f"{self.state.stream_mount}"
        )
        WebServer(self.state, port=web_port).start()
        console.print(f"[cyan]Web UI →[/cyan] http://0.0.0.0:{web_port}/")

        # Start Icecast stream
        self._stream_proc = self._start_icecast_stream()
        if self._stream_proc:
            console.print(f"[green]Streaming →[/green] {self.state.stream_url}")

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

            # Inject connect ID at the next natural break if a listener just tuned in
            if self._connect_id_pending.is_set():
                self._connect_id_pending.clear()
                clip = self.toh.get_connect_id()
                if clip:
                    log.info("Injecting connect ID for new listener")
                    self._play_clip(clip)
                    # Regenerate for the next connect
                    threading.Thread(
                        target=self.toh.refresh_connect_id, daemon=True
                    ).start()

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

        # Skip DJ when nobody is listening (saves API credits)
        from .cache import CacheState
        listeners_present = self.cache.state in (CacheState.WARM, CacheState.RUNNING)

        # Decide if we insert a DJ clip after this track
        insert_dj = (
            self.dj_every > 0
            and (self.track_count % self.dj_every == 0)
            and listeners_present
        )

        if insert_dj:
            try:
                script = self.dj.generate(
                    prev_track=track,
                    next_track=next_track,
                )
                self._print_dj(script.text)
                self.state.set_dj_script(script.text)
                dj_clip_path = self.tts.synthesize(script.text)
            except Exception as e:
                log.error(f"DJ generation failed: {e}")

        return self.mixer.make_segment(track.path, dj_clip_path, segment_name)

    def _icecast_url(self) -> Optional[str]:
        ice = self.cfg.get("icecast", {})
        if not ice:
            return None
        return (
            f"icecast://source:{ice.get('source_password', 'hackme')}"
            f"@{ice.get('host', 'localhost')}:{ice.get('port', 8000)}"
            f"{ice.get('mount', '/wkrt')}"
        )

    def _start_icecast_stream(self) -> Optional[subprocess.Popen]:
        """Start a persistent ffmpeg process piping MP3 data to Icecast."""
        url = self._icecast_url()
        if not url:
            return None
        station = self.cfg["station"]
        cmd = [
            "ffmpeg", "-loglevel", "warning",
            "-re", "-f", "mp3", "-i", "pipe:0",
            "-c:a", "copy", "-f", "mp3",
            "-ice_name", f"{station['call_sign']}-FM {station['frequency']}",
            "-ice_description", station.get("tagline", ""),
            "-ice_genre", "Classic Rock",
            url,
        ]
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            log.info(f"Icecast stream started → {url}")
            return proc
        except Exception as e:
            log.error(f"Failed to start Icecast stream: {e}")
            return None

    def _ensure_stream(self) -> bool:
        """Ensure Icecast stream is live, reconnecting with backoff if needed."""
        if self._stream_proc and self._stream_proc.poll() is None:
            return True
        for attempt in range(12):  # retry up to ~2 minutes
            if self._stop.is_set():
                return False
            wait = min(60, 5 * (attempt + 1))
            log.warning(f"Icecast stream down — reconnecting in {wait}s (attempt {attempt + 1})")
            time.sleep(wait)
            self._stream_proc = self._start_icecast_stream()
            if self._stream_proc and self._stream_proc.poll() is None:
                return True
        log.error("Could not reconnect to Icecast after retries")
        return False

    def _play(self, segment_path: Path, track: Track):
        """Feed segment to Icecast stream (or ffplay as fallback). Blocks until done."""
        self._print_now_playing(track)
        self.state.set_now_playing(track, self.next_track)
        self.state.set_cache_state(self.cache.state.name)

        # Icecast path: write segment into persistent ffmpeg pipe.
        # The -re flag makes ffmpeg read at native rate, so stdin.write()
        # blocks for approximately the segment duration — no separate sleep needed.
        if self._icecast_url():
            if not self._ensure_stream():
                return
            try:
                with open(segment_path, "rb") as f:
                    self._stream_proc.stdin.write(f.read())
                    self._stream_proc.stdin.flush()
                return
            except BrokenPipeError:
                log.warning("Icecast pipe broke mid-segment — will reconnect next segment")
                self._stream_proc = None
            except KeyboardInterrupt:
                self._stop.set()
                return

        # Fallback: local playback via ffplay
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

    def _on_listener_connect(self):
        self._listener_count += 1
        log.info(f"Listener connected ({self._listener_count} total)")
        self.state.set_listener_count(self._listener_count)
        self._connect_id_pending.set()
        self.cache.on_listener_connect()

    def _on_listener_disconnect(self):
        self._listener_count = max(0, self._listener_count - 1)
        log.info(f"Listener disconnected ({self._listener_count} remaining)")
        self.state.set_listener_count(self._listener_count)
        self.cache.on_listener_disconnect()

    def _play_clip(self, clip_path: Path):
        """Write a standalone clip into the stream. Blocks for clip duration."""
        if self._stream_proc and self._stream_proc.poll() is None:
            try:
                with open(clip_path, "rb") as f:
                    self._stream_proc.stdin.write(f.read())
                    self._stream_proc.stdin.flush()
            except BrokenPipeError:
                log.warning("Icecast stream broken during clip injection")
        elif self._ffplay:
            subprocess.run(
                [self._ffplay, "-nodisp", "-autoexit", "-loglevel", "quiet",
                 str(clip_path)],
                timeout=30,
            )

    def stop(self):
        self._stop.set()
        if self._stream_proc:
            try:
                self._stream_proc.stdin.close()
                self._stream_proc.wait(timeout=5)
            except Exception:
                self._stream_proc.kill()

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
