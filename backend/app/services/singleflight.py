"""A small per-key cache that also protects against a cache-miss stampede.

Several services in this codebase (LiveAnalytics, QualityService,
AnalyticsService) each cache one expensive computation per key with a short
TTL, because the frontend polls a handful of dashboard endpoints from every
open tab. A plain "check cache, else compute and store" cache dedupes
*sequential* calls but not *concurrent* ones: several requests that land
during the same miss - right after a restart, or right at the instant a TTL
expires - each independently run the full expensive query instead of
sharing one result. That's what turned "several tabs open" into several
identical multi-second queries stacking up on Postgres at once.

FastAPI runs these synchronous service methods in a thread pool, so
"concurrent" here means genuinely different OS threads, not just
interleaved coroutines - hence a real thread Lock/Event below, not an
asyncio one.
"""
import threading
import time
from typing import Any, Callable, Dict, Optional


class SingleFlightCache:
    """Collapses concurrent misses on the same key into one computation.

    The first caller in for a key becomes the "leader" and runs `compute`;
    anyone else asking for that same key while it's in flight waits for the
    leader's result instead of starting a second, identical, expensive query.
    """

    def __init__(self, ttl_s: float, max_keys: Optional[int] = None):
        self.ttl_s = ttl_s
        self.max_keys = max_keys
        self._lock = threading.Lock()
        self._value: Dict[str, Any] = {}
        self._at: Dict[str, float] = {}
        self._inflight: Dict[str, threading.Event] = {}
        self._error: Dict[str, BaseException] = {}

    def get_or_compute(self, key: str, compute: Callable[[], Any],
                       force: bool = False) -> Any:
        now = time.time()
        with self._lock:
            if not force and key in self._value and now - self._at.get(key, 0.0) < self.ttl_s:
                return self._value[key]
            event = self._inflight.get(key)
            if event is not None:
                leader = False
            else:
                event = threading.Event()
                self._inflight[key] = event
                leader = True

        if not leader:
            # Someone else is already computing this key - wait for them
            # instead of duplicating their (possibly multi-second) query.
            # Every follower reads (not pops) `_error`/`_value` here, since
            # several followers can wake up for the same leader run - only
            # the *next* leader run clears a stale error, below.
            event.wait()
            with self._lock:
                if key in self._error:
                    raise self._error[key]
                return self._value[key]

        try:
            value = compute()
        except BaseException as exc:
            with self._lock:
                self._error[key] = exc
                self._inflight.pop(key, None)
            event.set()
            raise

        with self._lock:
            self._value[key] = value
            self._at[key] = time.time()
            self._error.pop(key, None)
            self._inflight.pop(key, None)
            if self.max_keys and len(self._value) > self.max_keys:
                oldest = min(self._at, key=self._at.get)
                self._value.pop(oldest, None)
                self._at.pop(oldest, None)
        event.set()
        return value
