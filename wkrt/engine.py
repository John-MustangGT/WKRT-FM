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
import datetime
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
from .playlist import scan_library, PlaylistQueue, Track, AUDIO_EXTENSIONS, _read_tags
from .dj import DJEngine, ClipType
from .tts import TTSEngine
from .mixer import Mixer
from .state import StationState
from .web import WebServer
from .context import StationContext
from .programmer import DJProgrammer, current_time_slot
from .annotator import Annotator
from .history import PlayHistory
from .dj_stats import DJStats

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
        base = Path(__file__).parent.parent
        self._dj_stats = DJStats(base / "config")
        self._dj_engines: dict[str, DJEngine] = {
            dj_cfg["name"]: DJEngine(self.cfg, dj_cfg, stats=self._dj_stats)
            for dj_cfg in self._dj_configs
        }
        self._fallback_clips: dict[str, Path] = {}   # dj_name → pre-baked TTS path
        self._current_dj_name: Optional[str] = None
        self._dj_override: Optional[str] = None
        self._dj_override_lock = threading.Lock()

        # Library, queue ref (set in run()), and forced-next track (set by admin API)
        self._library: dict = {}
        self._queue = None          # PlaylistQueue — set once run() builds it
        self._forced_next: Optional[Track] = None
        self._forced_next_lock = threading.Lock()

        self.tts = TTSEngine(self.cfg)
        self.mixer = Mixer(self.cfg)

        self._ffplay = shutil.which("ffplay")
        self._stop = threading.Event()

        # One ffmpeg process per streaming target
        self._targets: list[dict] = self._load_targets()
        self._stream_procs: list[Optional[subprocess.Popen]] = [None] * len(self._targets)
        self._reconnecting: set[int] = set()
        self._target_enabled: list[bool] = [True] * len(self._targets)

        self._force_dj = threading.Event()

        # DJ block programmer + annotation cache
        self._programmer = DJProgrammer(self.cfg, base / "config")
        self._annotator  = Annotator(base / "config")
        self._history    = PlayHistory(base / "config")
        self._programmed_block: list[Track] = []
        self._block_lock = threading.Lock()
        self._refilling  = threading.Event()
        self._regen_triggered_for: Optional[str] = None

        self._listener_count = 0
        self._connect_id_pending = threading.Event()

        self.state = StationState()
        self.context = StationContext(self.cfg)

        # Top-of-hour scheduler
        self.toh = TopOfHourScheduler(self, None)  # cache set after init

        # Startup cache + listener hooks — hook_port lives on the primary (local) target
        self.cache = StartupCache(self)
        primary_target = self._targets[0] if self._targets else {}
        hook_port = primary_target.get("hook_port", 8765)
        self.hooks = HookServer(
            on_connect=self._on_listener_connect,
            on_disconnect=self._on_listener_disconnect,
            port=hook_port,
        )

        # Pre-generation thread — stores (segment_path, dj_starts_at, dj_text) tuple
        self._next_segment: Optional[tuple[Path, Optional[float], str]] = None
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

        self._library = library
        year_weights = self.cfg["playlist"].get("year_weights", {})
        queue = PlaylistQueue(library, year_weights)
        self._queue = queue
        self.state.set_dj_names([d["name"] for d in self._dj_configs])

        console.print(
            f"[cyan]Library:[/cyan] {queue.library_size} tracks across "
            f"{queue.year_count} years"
        )

        # Start context fetcher (weather + sports)
        self.context.start()

        # Watch for delayed DJ re-pick after crate updates
        threading.Thread(
            target=self._regen_watcher, daemon=True, name="regen-watcher"
        ).start()

        # Generate DJ favorites + first programmed block in background
        threading.Thread(
            target=self._initial_block_worker, daemon=True, name="initial-block"
        ).start()

        # Annotate library from MusicBrainz in background (1 req/sec, ~2 min for 137 tracks)
        threading.Thread(
            target=self._annotator.fetch_library, args=(self._library,),
            daemon=True, name="mb-annotate",
        ).start()

        # Pre-generate per-DJ "station on automatic" fallback clips (TTS only)
        threading.Thread(
            target=self._make_fallback_clips, daemon=True, name="fallback-clips",
        ).start()

        # Start web UI — listener panel uses the first target with admin_password
        web_cfg = self.cfg.get("web", {})
        web_port = web_cfg.get("port", 8080)
        primary = self._targets[0] if self._targets else {}
        self.state.stream_port = primary.get("port", 8000)
        self.state.stream_mount = primary.get("mount", "/wkrt")
        self.state.stream_url = (
            f"http://{primary.get('host', 'localhost')}:{self.state.stream_port}"
            f"{self.state.stream_mount}"
        )
        local_ice = next(
            (t for t in self._targets if t.get("admin_password")), primary
        )
        web_admin_pw = web_cfg.get("admin_password", "")
        WebServer(
            self.state, engine=self, port=web_port,
            admin_password=web_admin_pw,
            ice_cfg=local_ice,
        ).start()
        console.print(f"[cyan]Web UI →[/cyan] http://0.0.0.0:{web_port}/")

        # Start all Icecast streams
        self._start_all_streams()

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
        self.current_track = self._get_next_track()
        self.next_track    = self._get_next_track()
        seg_index = 0

        # Pre-generate first segment synchronously so we start immediately
        first_segment, first_dj_at, first_dj_text = self._build_segment(
            self.current_track, self.next_track, seg_index
        )
        seg_index += 1
        self.track_count += 1

        while not self._stop.is_set():
            # Start pre-generating next segment in background
            with self._forced_next_lock:
                if self._forced_next is not None:
                    future_track = self._forced_next
                    self._forced_next = None
                else:
                    future_track = self._get_next_track()
            pre_thread = threading.Thread(
                target=self._pregenerate,
                args=(self.next_track, future_track, seg_index),
                daemon=True,
            )
            pre_thread.start()

            # Play current segment — update DJ script text at play time so the
            # website reflects what's actually being heard, not what was pre-generated
            if first_dj_text:
                self.state.set_dj_script(first_dj_text)
            self._play(first_segment, self.current_track, first_dj_at)

            # Inject top-of-hour station ID at the first track boundary after :00
            toh_clip = self.toh.get_top_of_hour()
            if toh_clip:
                log.info("Injecting top-of-hour station ID")
                self._play_clip(toh_clip)

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
                    first_segment, first_dj_at, first_dj_text = self._next_segment
                else:
                    first_segment, first_dj_at, first_dj_text = self._build_segment(
                        self.current_track, self.next_track, seg_index
                    )
                self._next_segment = None

            # Periodic spool cleanup
            if self.track_count % 10 == 0:
                self.mixer.cleanup_spool(keep=15)

    def _pregenerate(self, current: Track, next_t: Track, idx: int):
        result = self._build_segment(current, next_t, idx)
        with self._next_segment_lock:
            self._next_segment = result

    def _build_segment(
        self, track: Track, next_track: Optional[Track], idx: int
    ) -> tuple[Path, Optional[float], str]:
        """Build a single playable segment: track + optional DJ clip.
        Returns (segment_path, dj_starts_at, dj_text) where dj_starts_at is seconds
        into the segment when the DJ begins speaking, or None if no DJ clip."""
        dj_cfg = self.active_dj_cfg()
        self._maybe_announce_dj_change(dj_cfg)

        segment_name = f"seg_{idx:06d}_{track.year}_{self._safe_name(track.artist)}"
        dj_clip_path: Optional[Path] = None
        dj_text = ""

        # Skip DJ when nobody is listening (saves API credits)
        from .cache import CacheState
        listeners_present = self.cache.state in (CacheState.WARM, CacheState.RUNNING)

        # Crate tracks always get a DJ announcement regardless of the normal cadence
        crate_incoming = bool(next_track and next_track.from_crate)
        force_dj = self._force_dj.is_set()
        if force_dj:
            self._force_dj.clear()
        insert_dj = listeners_present and (
            force_dj
            or crate_incoming
            or (self.dj_every > 0 and self.track_count % self.dj_every == 0)
        )

        seg_t0 = time.perf_counter()

        if insert_dj:
            try:
                active_engine = self._dj_engines[dj_cfg["name"]]
                force_type = ClipType.NEW_ARRIVAL if crate_incoming else None
                if crate_incoming:
                    next_track.from_crate = False  # consumed — won't re-trigger on replay

                # When API is degraded, use the pre-made fallback clip instead
                if not active_engine.is_api_healthy and not active_engine.should_retry_api():
                    fallback = self._fallback_clips.get(dj_cfg["name"])
                    if fallback and fallback.exists():
                        dj_clip_path = fallback
                        dj_text = "[automatic mode]"
                        log.info(f"API unhealthy — playing fallback clip for {dj_cfg['name']}")
                else:
                    ctx = dict(self.context.get() or {})
                    live = self.state.pop_live_context()
                    if live:
                        ctx["live_context"] = live
                    prev_ann = self._annotator.load(track.artist, track.title)
                    if prev_ann:
                        ctx["prev_annotation"] = prev_ann
                    if next_track:
                        next_ann = self._annotator.load(next_track.artist, next_track.title)
                        if next_ann:
                            ctx["next_annotation"] = next_ann

                    script = active_engine.generate(
                        prev_track=track,
                        next_track=next_track,
                        force_type=force_type,
                        context=ctx,
                    )
                    self._print_dj(script.text, dj_cfg["name"])
                    dj_text = script.text

                    tts_t0 = time.perf_counter()
                    dj_clip_path = self.tts.synthesize(script.text, dj_cfg)
                    self._dj_stats.record_tts(
                        dj_cfg["name"],
                        (time.perf_counter() - tts_t0) * 1000,
                    )

            except Exception as e:
                log.error(f"DJ generation failed: {e}")

        seg_path, dj_at = self.mixer.make_segment(track.path, dj_clip_path, segment_name)
        self._dj_stats.record_segment(
            dj_cfg["name"],
            (time.perf_counter() - seg_t0) * 1000,
        )
        return seg_path, dj_at, dj_text

    # ── Multi-target streaming ────────────────────────────────────────────────

    def _load_targets(self) -> list[dict]:
        """Return list of Icecast target dicts from config.
        Supports both [[icecast.targets]] (new) and flat [icecast] (legacy)."""
        ice = self.cfg.get("icecast", {})
        if "targets" in ice:
            return list(ice["targets"])
        if ice.get("host"):
            return [ice]
        return []

    def _target_url(self, target: dict) -> str:
        return (
            f"icecast://source:{target.get('source_password', 'hackme')}"
            f"@{target.get('host', 'localhost')}:{target.get('port', 8000)}"
            f"{target.get('mount', '/wkrt')}"
        )

    def _start_stream(self, target: dict) -> Optional[subprocess.Popen]:
        """Start one persistent ffmpeg process for the given target."""
        url = self._target_url(target)
        station = self.cfg["station"]
        codec = target.get("codec", "mp3").lower()
        bitrate = target.get("bitrate")

        if codec == "opus":
            audio_args = [
                "-c:a", "libopus", "-b:a", f"{bitrate or 96}k",
                "-f", "ogg", "-content_type", "audio/ogg",
            ]
        elif codec == "aac":
            audio_args = [
                "-c:a", "aac", "-b:a", f"{bitrate or 96}k",
                "-f", "adts", "-content_type", "audio/aac",
            ]
        elif bitrate:
            audio_args = ["-c:a", "libmp3lame", "-b:a", f"{bitrate}k", "-f", "mp3"]
        else:
            audio_args = ["-c:a", "copy", "-f", "mp3"]

        ice_name = target.get(
            "ice_name",
            f"{station['call_sign']}-FM {station['frequency']}",
        )
        cmd = [
            "ffmpeg", "-loglevel", "warning",
            "-re", "-f", "mp3", "-i", "pipe:0",
            *audio_args,
            "-ice_name", ice_name,
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
            log.info(f"Stream started → {target.get('name', url)}")
            return proc
        except Exception as e:
            log.error(f"Failed to start stream '{target.get('name')}': {e}")
            return None

    def _start_all_streams(self):
        for i, target in enumerate(self._targets):
            if not self._target_enabled[i]:
                continue
            proc = self._start_stream(target)
            self._stream_procs[i] = proc
            label = target.get("name", target.get("host", str(i)))
            url = self._target_url(target).split("@", 1)[-1]  # strip credentials
            if proc:
                console.print(f"[green]Streaming →[/green] {label}  [dim]({url})[/dim]")
            else:
                console.print(f"[red]Stream failed →[/red] {label}")

    def _ensure_all_streams(self) -> bool:
        """Return True if at least one stream is live. Kick off background reconnects
        for any dead enabled targets (without blocking the main loop)."""
        any_live = False
        for i, proc in enumerate(self._stream_procs):
            if not self._target_enabled[i]:
                continue
            if proc and proc.poll() is None:
                any_live = True
            elif i not in self._reconnecting:
                self._reconnecting.add(i)
                threading.Thread(
                    target=self._reconnect_worker,
                    args=(i,),
                    daemon=True,
                    name=f"reconnect-{self._targets[i].get('name', i)}",
                ).start()
        return any_live

    def _reconnect_worker(self, idx: int):
        target = self._targets[idx]
        name = target.get("name", target.get("host", str(idx)))
        for attempt in range(12):
            if self._stop.is_set() or not self._target_enabled[idx]:
                break
            wait = min(60, 5 * (attempt + 1))
            log.warning(f"Stream '{name}' down — reconnecting in {wait}s (attempt {attempt + 1})")
            time.sleep(wait)
            if not self._target_enabled[idx]:
                break
            proc = self._start_stream(target)
            if proc and proc.poll() is None:
                self._stream_procs[idx] = proc
                log.info(f"Stream '{name}' reconnected")
                break
        else:
            log.error(f"Stream '{name}' could not reconnect after retries")
        self._reconnecting.discard(idx)

    # ── Target runtime controls ───────────────────────────────────────────────

    def target_statuses(self) -> list[dict]:
        """Return a snapshot of each target's current state for the admin API."""
        result = []
        for i, target in enumerate(self._targets):
            proc = self._stream_procs[i]
            result.append({
                "idx": i,
                "name": target.get("name", f"target-{i}"),
                "host": target.get("host", ""),
                "port": target.get("port", 8000),
                "mount": target.get("mount", "/wkrt"),
                "codec": target.get("codec", "mp3"),
                "bitrate": target.get("bitrate"),
                "enabled": self._target_enabled[i],
                "connected": bool(proc and proc.poll() is None),
                "reconnecting": i in self._reconnecting,
            })
        return result

    def enable_target(self, idx: int):
        if 0 <= idx < len(self._targets):
            self._target_enabled[idx] = True
            proc = self._stream_procs[idx]
            if not (proc and proc.poll() is None) and idx not in self._reconnecting:
                proc = self._start_stream(self._targets[idx])
                self._stream_procs[idx] = proc
            log.info(f"Target {idx} enabled")

    def disable_target(self, idx: int):
        if 0 <= idx < len(self._targets):
            self._target_enabled[idx] = False
            self._reconnecting.discard(idx)
            proc = self._stream_procs[idx]
            if proc:
                try:
                    proc.stdin.close()
                    proc.wait(timeout=3)
                except Exception:
                    proc.kill()
                self._stream_procs[idx] = None
            log.info(f"Target {idx} disabled")

    def restart_target(self, idx: int):
        if 0 <= idx < len(self._targets):
            self.disable_target(idx)
            self._target_enabled[idx] = True
            proc = self._start_stream(self._targets[idx])
            self._stream_procs[idx] = proc
            log.info(f"Target {idx} restarted")

    # ── DJ block programming ──────────────────────────────────────────────────

    def _get_next_track(self) -> Track:
        """Next track: crate priority → programmed block → shuffle fallback."""
        if self._queue and self._queue.crate_size > 0:
            return next(self._queue)

        track = None
        remaining = 0
        with self._block_lock:
            if self._programmed_block:
                track     = self._programmed_block.pop(0)
                remaining = len(self._programmed_block)

        if track is not None:
            if remaining <= 2 and not self._refilling.is_set():
                self._refilling.set()
                threading.Thread(
                    target=self._refill_block_worker, daemon=True, name="block-refill"
                ).start()
            return track

        return next(self._queue)

    def _refill_block_worker(self):
        try:
            dj_cfg  = self.active_dj_cfg()
            tz      = self.cfg["station"].get("timezone", "UTC")
            slot    = current_time_slot(tz)
            ctx     = dict(self.context.get() or {})
            live    = self.state.live_context
            if live:
                ctx["live_context"] = live
            recent  = list(self.state.recent_tracks[:10])
            user_favs = self._programmer.load_user_favorites()
            block   = self._programmer.program_block(
                dj_cfg, self._library, slot, ctx, recent, user_favs
            )
            if block:
                with self._block_lock:
                    self._programmed_block.extend(block)
        except Exception as e:
            log.error(f"Block refill failed: {e}")
        finally:
            self._refilling.clear()

    def _initial_block_worker(self):
        """Generate favorites (if absent) then prime the first block. Runs at startup."""
        try:
            for dj_cfg in self._dj_configs:
                if not self._programmer.load_dj_favorites(dj_cfg["name"]):
                    log.info(f"No favorites for {dj_cfg['name']} — generating now (first run)…")
                    data = self._programmer.generate_all_slots(dj_cfg, self._library)
                    self._programmer.save_dj_favorites(dj_cfg["name"], data)

            # Prime block for the active DJ
            dj_cfg    = self.active_dj_cfg()
            tz        = self.cfg["station"].get("timezone", "UTC")
            slot      = current_time_slot(tz)
            user_favs = self._programmer.load_user_favorites()
            block     = self._programmer.program_block(
                dj_cfg, self._library, slot, self.context.get() or {}, [], user_favs
            )
            if block:
                with self._block_lock:
                    self._programmed_block = block + self._programmed_block
                log.info(f"Initial block ready ({dj_cfg['name']}, {slot})")
        except Exception as e:
            log.error(f"Initial block worker failed: {e}")

    def regenerate_dj_favorites(self, dj_name: str):
        """Trigger a background re-generation of all slots for one DJ."""
        dj_cfg = next((d for d in self._dj_configs if d["name"] == dj_name), None)
        if not dj_cfg:
            return
        def _worker():
            log.info(f"Regenerating favorites for {dj_name}…")
            data = self._programmer.generate_all_slots(dj_cfg, self._library)
            self._programmer.save_dj_favorites(dj_name, data)
            self._programmer.record_regen()
            log.info(f"Favorites regenerated for {dj_name}")
        threading.Thread(target=_worker, daemon=True, name=f"regen-{dj_name}").start()

    def _regen_watcher(self):
        """Wait 35 min after a crate update, then trigger DJ favorites regeneration."""
        DELAY = 35 * 60
        while not self._stop.wait(120):
            try:
                state = self._programmer.load_library_state()
                last_ingest_str = state.get("last_ingest")
                if not last_ingest_str:
                    continue
                if self._regen_triggered_for == last_ingest_str:
                    continue
                last_ingest = datetime.datetime.fromisoformat(last_ingest_str)
                now = datetime.datetime.now(datetime.timezone.utc)
                if (now - last_ingest).total_seconds() < DELAY:
                    continue
                last_regen_str = state.get("last_regen")
                if last_regen_str:
                    last_regen = datetime.datetime.fromisoformat(last_regen_str)
                    if last_regen >= last_ingest:
                        continue
                self._regen_triggered_for = last_ingest_str
                log.info("Regen watcher: 35 min since crate update — refreshing DJ favorites")
                for dj_cfg in self._dj_configs:
                    self.regenerate_dj_favorites(dj_cfg["name"])
            except Exception as e:
                log.error(f"Regen watcher error: {e}")

    def _make_fallback_clips(self):
        """Pre-synthesize one 'station on automatic' TTS clip per DJ at startup."""
        station = self.cfg["station"]
        cs   = station.get("call_sign", "WKRT")
        freq = station.get("frequency", "104.7")
        for dj_cfg in self._dj_configs:
            name = dj_cfg["name"]
            text = (
                f"Hey, this is {name} on {cs} {freq}. "
                f"We're running on automatic for a bit — technical issues in the booth. "
                f"The hits keep rolling. I'll be back before you know it."
            )
            try:
                path = self.tts.synthesize(text, dj_cfg)
                self._fallback_clips[name] = path
                log.info(f"Fallback clip ready for {name}: {path.name}")
            except Exception as e:
                log.warning(f"Could not pre-generate fallback clip for {name}: {e}")

    # ── DJ rotation ───────────────────────────────────────────────────────────

    def active_dj_cfg(self) -> dict:
        """Return the DJ config that should be on air right now."""
        with self._dj_override_lock:
            override = self._dj_override
        if override:
            for dj_cfg in self._dj_configs:
                if dj_cfg["name"] == override:
                    return dj_cfg
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

    def set_dj_override(self, name: Optional[str]):
        """Force a specific DJ on air, or pass None to restore time-based rotation."""
        with self._dj_override_lock:
            self._dj_override = name
        self.state.set_dj_override(name)
        log.info(f"DJ override → {name or '(auto)'}")

    def force_dj_break(self):
        """Force a DJ clip into the next segment regardless of normal cadence."""
        self._force_dj.set()
        log.info("DJ break forced by admin")

    def force_next_track(self, track: Track):
        """Inject a specific track to play after the current one finishes."""
        with self._forced_next_lock:
            self._forced_next = track
        log.info(f"Forced next track → {track.display}")

    def find_track(self, artist: str, title: str, year: int) -> Optional[Track]:
        """Look up a Track object from the in-memory library."""
        for tracks in self._library.values():
            for track in tracks:
                if track.artist == artist and track.title == title and track.year == year:
                    return track
        return None

    def ingest_tracks(self, paths: list) -> list[Track]:
        """Hot-add audio files to the library and crate. Returns successfully added tracks."""
        import re
        added = []
        for raw in paths:
            path = Path(raw)
            if not path.exists() or path.suffix.lower() not in AUDIO_EXTENSIONS:
                log.warning(f"Ingest skip (not found or bad extension): {path}")
                continue
            artist, title, duration, album = _read_tags(path)
            # Year: ID3 date tag → parent dir name → skip
            year = None
            try:
                from mutagen import File as _MFile
                audio = _MFile(path, easy=True)
                if audio and audio.tags:
                    for key in ("date", "year"):
                        val = str((audio.tags.get(key) or [""])[0])
                        m = re.search(r"(19|20)\d{2}", val)
                        if m:
                            year = int(m.group())
                            break
            except Exception:
                pass
            if not year:
                m = re.fullmatch(r"(19|20)\d{2}", path.parent.name)
                year = int(m.group()) if m else None
            if not year:
                log.warning(f"Ingest skip (no year): {path.name}")
                continue
            track = Track(
                path=path, year=year, artist=artist, title=title,
                duration_seconds=duration, album=album, from_crate=True,
            )
            if self._queue is not None:
                self._queue.add_track(track)
            else:
                self._library.setdefault(year, []).append(track)
            log.info(f"Ingested → {track.display}")
            console.print(
                f"[magenta]★ Crate:[/magenta] [white]{track.artist}[/white] — "
                f"[yellow]{track.title}[/yellow] [dim]({track.year})[/dim]"
            )
            added.append(track)
            threading.Thread(
                target=self._annotator.fetch,
                args=(track.artist, track.title),
                daemon=True, name=f"mb-{track.title[:20]}",
            ).start()

        if added:
            self._programmer.record_ingest()

        return added

    def get_library_for_api(self) -> list:
        """Return library grouped by artist, sorted, suitable for JSON serialisation."""
        artists: dict[str, list] = {}
        for tracks in self._library.values():
            for t in tracks:
                artists.setdefault(t.artist, []).append(
                    {"artist": t.artist, "title": t.title, "year": t.year}
                )
        return [
            {"name": a, "tracks": sorted(tl, key=lambda x: x["title"])}
            for a, tl in sorted(artists.items(), key=lambda x: x[0].lower())
        ]

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
        """Push a StreamTitle update to all targets that have admin creds."""
        for target in self._targets:
            if not target.get("source_password"):
                continue
            host = target.get("host", "localhost")
            port = target.get("port", 8000)
            mount = target.get("mount", "/wkrt")
            password = target["source_password"]
            params = urlencode({"mount": mount, "mode": "updinfo", "song": title})
            url = f"http://{host}:{port}/admin/metadata?{params}"
            creds = base64.b64encode(f"source:{password}".encode()).decode()
            req = Request(url, headers={"Authorization": f"Basic {creds}"})
            try:
                with urlopen(req, timeout=2):
                    pass
                log.debug(f"ICY metadata [{target.get('name', host)}] → {title!r}")
            except Exception as e:
                log.debug(f"ICY metadata [{target.get('name', host)}] failed: {e}")

    def _play(self, segment_path: Path, track: Track, dj_starts_at: Optional[float] = None):
        """Feed segment to all live Icecast streams (or ffplay as fallback).
        Writes are concurrent so multiple targets don't multiply the wall-clock time."""
        self._print_now_playing(track)
        self.state.set_now_playing(track, self.next_track)
        self.state.set_cache_state(self.cache.state.name)
        self._update_icy_metadata(f"{track.artist} - {track.title}")
        tz = self.cfg["station"].get("timezone", "UTC")
        self._history.record_play(
            track.artist, track.title,
            self.active_dj_cfg()["name"],
            current_time_slot(tz),
        )

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

        if self._targets:
            if not self._ensure_all_streams():
                if dj_timer:
                    dj_timer.cancel()
                return
            try:
                data = segment_path.read_bytes()
            except OSError as e:
                log.error(f"Could not read segment {segment_path.name}: {e}")
                if dj_timer:
                    dj_timer.cancel()
                return

            # Write to every live target concurrently so wall-clock time == one segment
            def _write(i: int, proc: subprocess.Popen):
                try:
                    proc.stdin.write(data)
                    proc.stdin.flush()
                except BrokenPipeError:
                    name = self._targets[i].get("name", i)
                    log.warning(f"Stream '{name}' pipe broke mid-segment")
                    self._stream_procs[i] = None
                except KeyboardInterrupt:
                    self._stop.set()

            writers = [
                threading.Thread(target=_write, args=(i, proc), daemon=True)
                for i, proc in enumerate(self._stream_procs)
                if proc and proc.poll() is None
            ]
            for t in writers:
                t.start()
            try:
                for t in writers:
                    t.join()
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
        """Sum listener counts across local targets (those with hook_port)."""
        total = 0
        polled = False
        for target in self._targets:
            if not target.get("hook_port"):
                continue  # skip external targets; we can't drive cache from them
            host = target.get("host", "localhost")
            port = target.get("port", 8000)
            mount = target.get("mount", "/wkrt")
            url = f"http://{host}:{port}/status-json.xsl"
            try:
                with urlopen(url, timeout=5) as resp:
                    data = json.loads(resp.read())
                sources = data.get("icestats", {}).get("source", [])
                if isinstance(sources, dict):
                    sources = [sources]
                for source in sources:
                    if source.get("listenurl", "").endswith(mount):
                        total += int(source.get("listeners", 0))
                        polled = True
            except Exception as e:
                log.debug(f"Icecast stats poll failed [{target.get('name', host)}]: {e}")
        return total if polled else self._listener_count

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
        """Write a standalone clip into all live streams. Blocks for clip duration."""
        dj_name = self.active_dj_cfg().get("name", "DJ")
        station = self.cfg.get("station", {})
        self._update_icy_metadata(
            f"{dj_name} — {station.get('call_sign', 'WKRT')}-FM {station.get('frequency', '104.7')}"
        )
        live = [(i, p) for i, p in enumerate(self._stream_procs) if p and p.poll() is None]
        if live:
            data = clip_path.read_bytes()
            def _write(i, proc):
                try:
                    proc.stdin.write(data)
                    proc.stdin.flush()
                except BrokenPipeError:
                    self._stream_procs[i] = None
            threads = [threading.Thread(target=_write, args=(i, p), daemon=True) for i, p in live]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        elif self._ffplay:
            subprocess.run(
                [self._ffplay, "-nodisp", "-autoexit", "-loglevel", "quiet",
                 str(clip_path)],
                timeout=30,
            )

    def stop(self):
        self._stop.set()
        for i, proc in enumerate(self._stream_procs):
            if proc:
                try:
                    proc.stdin.close()
                    proc.wait(timeout=5)
                except Exception:
                    proc.kill()
                self._stream_procs[i] = None

    def pause(self):
        """Called by cache when cooling timeout reached — no listeners."""
        log.info("Engine pausing — no listeners")
        # Currently just logs; future: stop ffmpeg pipe to Icecast

    def build_next_segment(self) -> Optional[Path]:
        """Called by cache warmup to pre-generate a segment."""
        if self.next_track is None:
            return None
        idx = self.track_count
        seg, _, _ = self._build_segment(self.current_track or self.next_track,
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
