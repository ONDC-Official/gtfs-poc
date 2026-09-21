"""Regression coverage for the single-flight caching added to
AnalyticsService, LiveAnalytics and QualityService.

These run against the real `repo` fixture (both adapters), not a hand-rolled
fake - a fake risks drifting from the real Repository contract (as the first
draft of this file did: it was missing `poll_series` and several other
methods `QualityService.summary()` actually calls). Using the same fixture
as test_quality_service.py exercises the real SQL path under concurrency,
which is exactly where the production bug this fixes was found.
"""
import threading
import time

from app.services.analytics import AnalyticsService
from app.services.live_analytics import LiveAnalytics
from app.services.quality import QualityService
from app.services.singleflight import SingleFlightCache


def test_analytics_service_caches_and_distinguishes_params(repo):
    svc = AnalyticsService(repo)
    a = svc.overview(300)
    b = svc.overview(300)
    assert a == b, "repeated identical calls within the TTL should be cached"

    # A different window_s must not reuse the 300s entry.
    c = svc.overview(600)
    assert c["window_s"] == 600


def test_live_analytics_force_bypasses_a_fresh_cache(repo):
    live = LiveAnalytics(repo)
    first = live.compute()
    second = live.compute()
    assert first == second, "second call within the 5s TTL should be cached"

    # force=True must not just return the cached dict - it should actually
    # recompute. generated_at is int(time.time()), which can tie at 1s
    # resolution, so assert on identity/behavior we can pin without sleeping:
    # calling force twice in a row must both go through _compute(), not
    # short-circuit on the cache like a plain (non-force) call would.
    calls = {"n": 0}
    real_compute = live._compute

    def counting_compute(now):
        calls["n"] += 1
        return real_compute(now)

    live._compute = counting_compute
    live.compute(force=True)
    live.compute(force=True)
    assert calls["n"] == 2, "force=True must recompute every time, not reuse the cache"


def test_quality_summary_keys_by_minutes_independently(repo):
    q = QualityService(repo)
    default_window = q.summary()
    window_60 = q.summary(minutes=60)
    assert default_window["window_minutes"] is None
    assert window_60["window_minutes"] == 60

    # Wrap coverage() (called by summary()) to count real invocations per
    # distinct `minutes` key.
    calls = []
    real_coverage = q.coverage

    def counting_coverage(*args, **kwargs):
        calls.append((args, kwargs))
        return real_coverage(*args, **kwargs)

    q.coverage = counting_coverage
    q.summary()          # cached - no new coverage() call
    q.summary(minutes=60)  # cached - no new coverage() call
    assert len(calls) == 0, "both windows should still be served from cache"

    q.summary(force=True)  # only the default window should recompute
    assert len(calls) == 1
    q.summary(minutes=60)  # untouched by the force above
    assert len(calls) == 1, "force on one window must not invalidate another window's cache"


def test_quality_summary_concurrent_calls_collapse_to_one_computation(repo, monkeypatch):
    """The actual production bug: several tabs polling summary() at once,
    right as the cache is cold, each independently running the full 6-layer
    computation. Reproduced here by making vehicles_in_window artificially
    slow and firing many real threads at the real QualityService."""
    q = QualityService(repo)
    real_vehicles_in_window = repo.vehicles_in_window
    call_count = {"n": 0}
    lock = threading.Lock()

    def slow_vehicles_in_window(*args, **kwargs):
        with lock:
            call_count["n"] += 1
        time.sleep(0.05)
        return real_vehicles_in_window(*args, **kwargs)

    monkeypatch.setattr(repo, "vehicles_in_window", slow_vehicles_in_window)

    results = []
    def worker():
        results.append(q.summary())

    threads = [threading.Thread(target=worker) for _ in range(8)]
    start = time.time()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.time() - start

    # vehicles_in_window is called from 3 layers inside one summary() pass
    # (coverage, freshness, correctness) - 8 concurrent summary() calls
    # sharing one computation should total 3 calls, not 8 x 3 = 24.
    assert call_count["n"] == 3, (
        f"expected exactly 3 real vehicles_in_window calls (one summary "
        f"computation's worth), got {call_count['n']} - stampede not collapsed")
    assert elapsed < 1.0, f"took {elapsed:.2f}s - looks serialized/duplicated, not shared"
    assert all(r == results[0] for r in results), "all callers must get the same result"


def test_singleflight_cache_error_reaches_every_waiter():
    """Every concurrent caller for a failing key must see the error - not
    just the leader, with the rest crashing on a missing cache entry (the
    bug the first draft of singleflight.py had, caught by this test)."""
    cache = SingleFlightCache(ttl_s=10.0)
    calls = {"n": 0}
    lock = threading.Lock()

    def failing():
        with lock:
            calls["n"] += 1
        time.sleep(0.05)
        raise RuntimeError("boom")

    errors = []
    def worker():
        try:
            cache.get_or_compute("k", failing)
        except RuntimeError as e:
            errors.append(str(e))

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert calls["n"] == 1, "only the leader should actually run compute()"
    assert len(errors) == 6, "every waiter must receive the error, not a KeyError"
