"""
wkrt/cache.py — Startup segment cache

Pre-generates N segments before the first listener connects so there's
no dead air on connect. Also maintains a rolling lookahead buffer while
playing so the next segment is always ready.

Cache states:
    COLD  — nothing generated yet
    WARMING — generating initial segments  
    WARM  — ready for listeners
    RUNNING — listener connected, rolling buffer active
    COOLING — listener disconnected, engine pausing
"""

import logging
import threading
import time
from enum import Enum, auto
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


class CacheState(Enum):
    COLD = auto()
    WARMING = auto()
    WARM = auto()
    RUNNING = auto()
    COOLING = auto()


class StartupCache:
    """
    Manages pre-generation of audio segments.

    On init: pre-generates WARMUP_SEGMENTS before accepting listeners.
    While running: maintains LOOKAHEAD_SEGMENTS ahead of current playback.
    On disconnect: finishes current segment, enters COOLING state.
    On reconnect: resumes immediately from buffer, no gap.
    """

    WARMUP_SEGMENTS = 3      # segments to generate before first listener
    LOOKAHEAD_SEGMENTS = 2   # segments to keep ahead while playing
    COOLING_TIMEOUT = 300    # seconds to keep engine warm after disconnect
                             # before fully stopping (5 min)

    def __init__(self, engine):
        self.engine = engine
        self.state = CacheState.COLD
        self._buffer: list[Path] = []
        self._buffer_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._warmup_event = threading.Event()
        self._cooling_timer: Optional[threading.Timer] = None
        self._listener_count = 0

    # ── Public API ────────────────────────────────────────────────────────

    def start_warmup(self):
        """
        Begin pre-generating segments. Call this at startup.
        Returns immediately — warmup runs in background thread.
        """
        with self._state_lock:
            if self.state != CacheState.COLD:
                return
            self.state = CacheState.WARMING

        log.info(f"Cache warming — pre-generating {self.WARMUP_SEGMENTS} segments")
        t = threading.Thread(target=self._warmup_worker, daemon=True)
        t.start()

    def wait_until_warm(self, timeout: float = 120.0) -> bool:
        """Block until cache is warm. Returns True if warm, False if timeout."""
        return self._warmup_event.wait(timeout=timeout)

    def on_listener_connect(self):
        """Called when a listener connects (Icecast on-connect hook)."""
        with self._state_lock:
            self._listener_count += 1
            log.info(f"Listener connected (total: {self._listener_count})")

            # Cancel any pending cooling timer
            if self._cooling_timer:
                self._cooling_timer.cancel()
                self._cooling_timer = None

            if self.state in (CacheState.WARM, CacheState.COOLING):
                self.state = CacheState.RUNNING
                log.info("Cache → RUNNING")
            elif self.state == CacheState.COLD:
                # Late connect before warmup — start warming now
                self.start_warmup()

    def on_listener_disconnect(self):
        """Called when a listener disconnects (Icecast on-disconnect hook)."""
        with self._state_lock:
            self._listener_count = max(0, self._listener_count - 1)
            log.info(f"Listener disconnected (remaining: {self._listener_count})")

            if self._listener_count == 0:
                self.state = CacheState.COOLING
                log.info(
                    f"Cache → COOLING "
                    f"(will stop in {self.COOLING_TIMEOUT}s if no reconnect)"
                )
                self._cooling_timer = threading.Timer(
                    self.COOLING_TIMEOUT, self._on_cooling_timeout
                )
                self._cooling_timer.daemon = True
                self._cooling_timer.start()

    def get_next_segment(self) -> Optional[Path]:
        """
        Pop the next pre-generated segment from the buffer.
        Returns None if buffer is empty (shouldn't happen if warm).
        """
        with self._buffer_lock:
            if self._buffer:
                seg = self._buffer.pop(0)
                log.debug(f"Cache dequeue: {seg.name} ({len(self._buffer)} remaining)")
                return seg
            else:
                log.warning("Cache buffer empty — cold serving")
                return None

    def queue_segment(self, path: Path):
        """Add a pre-generated segment to the buffer."""
        with self._buffer_lock:
            self._buffer.append(path)
            log.debug(f"Cache enqueue: {path.name} ({len(self._buffer)} buffered)")

    @property
    def buffer_size(self) -> int:
        with self._buffer_lock:
            return len(self._buffer)

    @property
    def is_ready(self) -> bool:
        return self.state in (CacheState.WARM, CacheState.RUNNING)

    @property
    def should_generate(self) -> bool:
        """True if the engine should keep generating segments."""
        return self.state in (
            CacheState.WARMING,
            CacheState.WARM,
            CacheState.RUNNING,
            CacheState.COOLING,  # keep buffer warm during cooling
        )

    @property
    def needs_lookahead(self) -> bool:
        """True if buffer needs topping up."""
        return self.buffer_size < self.LOOKAHEAD_SEGMENTS

    # ── Internal ──────────────────────────────────────────────────────────

    def _warmup_worker(self):
        """Background thread: generates initial segments."""
        try:
            for i in range(self.WARMUP_SEGMENTS):
                if not self.should_generate:
                    break
                log.info(f"Warmup: generating segment {i+1}/{self.WARMUP_SEGMENTS}")
                seg = self.engine.build_next_segment()
                if seg:
                    self.queue_segment(seg)

            with self._state_lock:
                if self.state == CacheState.WARMING:
                    self.state = CacheState.WARM
                    log.info(
                        f"Cache WARM — {self.buffer_size} segments ready, "
                        f"accepting listeners"
                    )
            self._warmup_event.set()

        except Exception as e:
            log.error(f"Warmup failed: {e}")
            self._warmup_event.set()  # unblock any waiters

    def _on_cooling_timeout(self):
        """Called after COOLING_TIMEOUT with no reconnect — stop the engine."""
        with self._state_lock:
            if self.state == CacheState.COOLING and self._listener_count == 0:
                self.state = CacheState.COLD
                log.info("Cache → COLD (cooling timeout, engine stopping)")
                self.engine.pause()


class TopOfHourScheduler:
    """
    Watches the clock and pre-generates a top-of-hour ID segment
    before the hour turns so it's ready to insert seamlessly.

    Pre-generates at :55 for the :00 slot.
    Also generates a connect ID on startup for first-listener greeting.
    """

    PREGEN_MINUTES_BEFORE = 5  # generate at :55

    def __init__(self, engine, cache: StartupCache):
        self.engine = engine
        self.cache = cache
        self._pending_toh: Optional[Path] = None
        self._connect_id: Optional[Path] = None
        self._lock = threading.Lock()
        self._stop = threading.Event()

    def start(self):
        # Pre-generate connect ID immediately
        t = threading.Thread(target=self._generate_connect_id, daemon=True)
        t.start()

        # Start clock watcher
        t2 = threading.Thread(target=self._clock_watcher, daemon=True,
                               name="toh-scheduler")
        t2.start()

    def get_connect_id(self) -> Optional[Path]:
        """Returns pre-generated connect ID clip, or None if not ready."""
        with self._lock:
            path = self._connect_id
            self._connect_id = None  # consume it
            return path

    def get_top_of_hour(self) -> Optional[Path]:
        """Returns pre-generated top-of-hour clip if ready."""
        with self._lock:
            path = self._pending_toh
            self._pending_toh = None  # consume it
            return path

    def is_top_of_hour(self) -> bool:
        """True if we're within 30 seconds of the hour."""
        import datetime
        now = datetime.datetime.now()
        return now.minute == 0 and now.second < 30

    def refresh_connect_id(self):
        """Regenerate the connect ID clip (call after consuming it)."""
        self._generate_connect_id()

    def stop(self):
        self._stop.set()

    def _clock_watcher(self):
        import datetime
        last_pregen_hour = -1

        while not self._stop.is_set():
            now = datetime.datetime.now()
            # At :55, pre-generate for the coming :00
            if (now.minute == 60 - self.PREGEN_MINUTES_BEFORE
                    and now.hour != last_pregen_hour):
                last_pregen_hour = now.hour
                next_hour = (now.hour + 1) % 24
                log.info(f"Pre-generating top-of-hour ID for {next_hour}:00")
                threading.Thread(
                    target=self._generate_toh,
                    args=(next_hour,),
                    daemon=True,
                ).start()

            time.sleep(20)  # check every 20 seconds

    def _generate_toh(self, hour: int):
        from .dj import ClipType
        import datetime
        hour_str = datetime.time(hour, 0).strftime("%-I o'clock %p")
        try:
            script = self.engine.dj.generate(force_type=ClipType.TOP_OF_HOUR)
            clip = self.engine.tts.synthesize(script.text)
            with self._lock:
                self._pending_toh = clip
            log.info(f"Top-of-hour ID ready for {hour_str}")
        except Exception as e:
            log.error(f"Top-of-hour generation failed: {e}")

    def _generate_connect_id(self):
        from .dj import ClipType
        try:
            script = self.engine.dj.generate(force_type=ClipType.CONNECT_ID)
            clip = self.engine.tts.synthesize(script.text)
            with self._lock:
                self._connect_id = clip
            log.info("Connect ID ready")
        except Exception as e:
            log.error(f"Connect ID generation failed: {e}")
