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
import base64
import json
import logging
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode
from urllib.request import Request, urlopen

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
from .context import StationContext

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

        # Build one DJEngine per DJ listed in config
        self._dj_configs: list[dict] = self.cfg.get("djs", [])
        if not self._dj_configs:
            raise RuntimeError("No [[djs]] entries found in settings.toml")
        self._dj_engines: dict[str, DJEngine] = {
            dj_cfg["name"]: DJEngine(self.cfg, dj_cfg)
            for dj_cfg in self._dj_configs
        }
        self._current_dj_name: Optional[str] = None  # tracks last-seen DJ for change detection

        self.tts = TTSEngine(self.cfg)
        self.mixer = Mixer(self.cfg)

        self._ffplay = shutil.which("ffplay")
        self._stop = threading.Event()
        self._stream_proc: Optional[subprocess.Popen] = None

        self._listener_count = 0
        self._connect_id_pending = threading.Event()

        self.state = StationState()
        self.context = StationContext(self.cfg)

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

        # Pre-generation thread — stores (segment_path, dj_starts_at) tuple
        self._next_segment: Optional[tuple[Path, Optional[float]]] = None
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

        # Start context fetcher (weather + sports)
        self.context.start()

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

        # Poll Icecast stats to keep listener count accurate even without webhooks
        threading.Thread(
            target=self._listener_poll_worker, daemon=True, name="listener-poll"
        ).start()

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
        first_segment, first_dj_at = self._build_segment(
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
            self._play(first_segment, self.current_track, first_dj_at)

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
                if self._next_segment is not None:
                    first_segment, first_dj_at = self._next_segment
                else:
                    first_segment, first_dj_at = self._build_segment(
                        self.current_track, self.next_track, seg_index
                    )
                self._next_segment = None

            # Periodic spool cleanup
            if self.track_count % 10 == 0:
                self.mixer.cleanup_spool(keep=15)

    def _pregenerate(self, current: Track, next_t: Track, idx: int):
        seg, dj_at = self._build_segment(current, next_t, idx)
        with self._next_segment_lock:
            self._next_segment = (seg, dj_at)

    def _build_segment(
        self, track: Track, next_track: Optional[Track], idx: int
    ) -> tuple[Path, Optional[float]]:
        """Build a single playable segment: track + optional DJ clip.
        Returns (segment_path, dj_starts_at) where dj_starts_at is seconds into
        the segment when the DJ begins speaking, or None if no DJ clip."""
        dj_cfg = self.active_dj_cfg()
        self._maybe_announce_dj_change(dj_cfg)

        segment_name = f"seg_{idx:06d}_{track.year}_{self._safe_name(track.artist)}"
        dj_clip_path: Optional[Path] = None

        # Skip DJ when nobody is listening (saves API credits)
        from .cache import CacheState
        listeners_present = self.cache.state in (CacheState.WARM, CacheState.RUNNING)

        insert_dj = (
            self.dj_every > 0
            and (self.track_count % self.dj_every == 0)
            and listeners_present
        )

        if insert_dj:
            try:
                active_engine = self._dj_engines[dj_cfg["name"]]
                script = active_engine.generate(
                    prev_track=track,
                    next_track=next_track,
                    context=self.context.get(),
                )
                self._print_dj(script.text, dj_cfg["name"])
                self.state.set_dj_script(script.text)
                dj_clip_path = self.tts.synthesize(script.text, dj_cfg)
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

    # ── DJ rotation ───────────────────────────────────────────────────────────

    def active_dj_cfg(self) -> dict:
        """Return the DJ config that should be on air right now."""
        import datetime as _dt
        from zoneinfo import ZoneInfo
        hour = _dt.datetime.now(ZoneInfo(self.cfg["station"].get("timezone", "UTC"))).hour
        period = sum(d["shift_hours"] for d in self._dj_configs)
        block = hour % period
        cumulative = 0
        for dj_cfg in self._dj_configs:
            cumulative += dj_cfg["shift_hours"]
            if block < cumulative:
                return dj_cfg
        return self._dj_configs[0]

    @property
    def dj(self) -> DJEngine:
        """Active DJ engine — used by TopOfHourScheduler and StartupCache."""
        return self._dj_engines[self.active_dj_cfg()["name"]]

    def _maybe_announce_dj_change(self, dj_cfg: dict):
        """Update ICY metadata and state when the on-air DJ changes."""
        if dj_cfg["name"] == self._current_dj_name:
            return
        self._current_dj_name = dj_cfg["name"]
        self.state.set_active_dj(dj_cfg["name"])
        station = self.cfg["station"]
        self._update_icy_metadata(
            f"{dj_cfg['name']} — {station['call_sign']}-FM {station['frequency']}"
        )
        log.info(f"DJ shift → {dj_cfg['name']}")

    # ── ICY metadata ──────────────────────────────────────────────────────────

    def _update_icy_metadata(self, title: str):
        """Push a StreamTitle update to Icecast via the admin metadata API."""
        ice = self.cfg.get("icecast", {})
        if not ice:
            return
        host = ice.get("host", "localhost")
        port = ice.get("port", 8000)
        mount = ice.get("mount", "/wkrt")
        password = ice.get("source_password", "hackme")
        params = urlencode({"mount": mount, "mode": "updinfo", "song": title})
        url = f"http://{host}:{port}/admin/metadata?{params}"
        credentials = base64.b64encode(f"source:{password}".encode()).decode()
        req = Request(url, headers={"Authorization": f"Basic {credentials}"})
        try:
            with urlopen(req, timeout=2):
                pass
            log.debug(f"ICY metadata → {title!r}")
        except Exception as e:
            log.debug(f"ICY metadata update failed: {e}")

    def _play(self, segment_path: Path, track: Track, dj_starts_at: Optional[float] = None):
        """Feed segment to Icecast stream (or ffplay as fallback). Blocks until done."""
        self._print_now_playing(track)
        self.state.set_now_playing(track, self.next_track)
        self.state.set_cache_state(self.cache.state.name)
        self._update_icy_metadata(f"{track.artist} - {track.title}")

        # Schedule a metadata flip to the active DJ when the talkover begins
        if dj_starts_at is not None and dj_starts_at > 0:
            dj_name = self.active_dj_cfg().get("name", "DJ")
            station = self.cfg.get("station", {})
            dj_label = f"{dj_name} — {station.get('call_sign', 'WKRT')}-FM {station.get('frequency', '104.7')}"
            dj_timer = threading.Timer(dj_starts_at, self._update_icy_metadata, args=(dj_label,))
            dj_timer.daemon = True
            dj_timer.start()
        else:
            dj_timer = None

        # Icecast path: write segment into persistent ffmpeg pipe.
        # The -re flag makes ffmpeg read at native rate, so stdin.write()
        # blocks for approximately the segment duration — no separate sleep needed.
        if self._icecast_url():
            if not self._ensure_stream():
                if dj_timer:
                    dj_timer.cancel()
                return
            try:
                with open(segment_path, "rb") as f:
                    self._stream_proc.stdin.write(f.read())
                    self._stream_proc.stdin.flush()
                return
            except BrokenPipeError:
                log.warning("Icecast pipe broke mid-segment — will reconnect next segment")
                self._stream_proc = None
                if dj_timer:
                    dj_timer.cancel()
            except KeyboardInterrupt:
                self._stop.set()
                if dj_timer:
                    dj_timer.cancel()
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
        log.info(f"Listener connected via webhook ({self._listener_count} total)")
        self.state.set_listener_count(self._listener_count)
        self._connect_id_pending.set()
        self.cache.on_listener_connect()

    def _on_listener_disconnect(self):
        self._listener_count = max(0, self._listener_count - 1)
        log.info(f"Listener disconnected via webhook ({self._listener_count} remaining)")
        self.state.set_listener_count(self._listener_count)
        self.cache.on_listener_disconnect()

    def _poll_icecast_listeners(self) -> int:
        """Fetch current listener count from Icecast public stats JSON endpoint."""
        ice = self.cfg.get("icecast", {})
        host = ice.get("host", "localhost")
        port = ice.get("port", 8000)
        mount = ice.get("mount", "/wkrt")
        url = f"http://{host}:{port}/status-json.xsl"
        try:
            with urlopen(url, timeout=5) as resp:
                data = json.loads(resp.read())
            sources = data.get("icestats", {}).get("source", [])
            if isinstance(sources, dict):
                sources = [sources]
            for source in sources:
                if source.get("listenurl", "").endswith(mount):
                    return int(source.get("listeners", 0))
        except Exception as e:
            log.debug(f"Icecast stats poll failed: {e}")
        return self._listener_count  # keep last known on error

    def _listener_poll_worker(self):
        """Background thread: reconciles listener count against Icecast stats every 15s."""
        # Brief initial delay so the stream has time to connect before first poll
        time.sleep(10)
        while not self._stop.is_set():
            new_count = self._poll_icecast_listeners()
            old_count = self._listener_count
            if new_count != old_count:
                self._listener_count = new_count
                self.state.set_listener_count(new_count)
                log.info(f"Listener count (poll): {old_count} → {new_count}")
                # Drive cache state transitions on boundary changes
                if old_count == 0 and new_count > 0:
                    self._connect_id_pending.set()
                    self.cache.on_listener_connect()
                elif old_count > 0 and new_count == 0:
                    self.cache.on_listener_disconnect()
            self._stop.wait(15)

    def _play_clip(self, clip_path: Path):
        """Write a standalone clip into the stream. Blocks for clip duration."""
        dj_name = self.active_dj_cfg().get("name", "DJ")
        station = self.cfg.get("station", {})
        self._update_icy_metadata(
            f"{dj_name} — {station.get('call_sign', 'WKRT')}-FM {station.get('frequency', '104.7')}"
        )
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
        seg, _ = self._build_segment(self.current_track or self.next_track,
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

    def _print_dj(self, text: str, name: str = ""):
        label = f"DJ {name.upper()}" if name else "DJ"
        console.print(
            Panel(
                f"[italic cyan]{text}[/italic cyan]",
                title=f"[magenta]{label}[/magenta]",
                border_style="magenta",
                padding=(0, 1),
            )
        )

    @staticmethod
    def _safe_name(s: str) -> str:
        return "".join(c if c.isalnum() else "_" for c in s)[:20]
