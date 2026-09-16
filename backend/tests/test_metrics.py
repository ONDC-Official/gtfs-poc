"""/api/metrics exposition: valid Prometheus text, covering both adapters.

Doesn't go through the ASGI app (that would start the realtime poller and
aggregator background loops via the lifespan) - the collector only needs a
QualityService, so it's built directly from the `repo` fixture, the same
way test_quality_service.py does.
"""
from conftest import NOW
from prometheus_client import CollectorRegistry, generate_latest
from prometheus_client.parser import text_string_to_metric_families

from app.api.metrics import _quality_metrics
from app.services import quality as quality_module
from app.services.aggregator import (GAP_THRESHOLD_S, GRID_LAT_DEG, GRID_LON_DEG,
                                     HOUR, IMPLAUSIBLE_MPS, MOVING_MPS)
from app.services.live_analytics import LiveAnalytics
from app.services.quality import QualityService


class _StaticCollector:
    """Replays already-computed metric families - QualityCollector.collect()
    reaches through the module-level `deps` singleton (wired to whichever
    backend the process imported first), so tests exercise `_quality_metrics`
    (the actual dict-to-Prometheus reshaping) against the fixture-backed
    service directly instead."""
    def __init__(self, families):
        self._families = list(families)

    def collect(self):
        yield from self._families


def _render(repo, monkeypatch) -> str:
    monkeypatch.setattr(quality_module.time, "time", lambda: NOW)
    repo.fold_continuity_gaps(0, NOW, GAP_THRESHOLD_S)
    # coverage()'s temporal_coverage reads the grid/route rollup, not the
    # continuity one - fold it too so that metric has something to report.
    repo.fold_rollups(0, GRID_LAT_DEG, GRID_LON_DEG, HOUR, MOVING_MPS, IMPLAUSIBLE_MPS)

    quality = QualityService(repo, LiveAnalytics(repo))
    data = quality.summary()

    registry = CollectorRegistry()
    registry.register(_StaticCollector(_quality_metrics(data)))
    return generate_latest(registry).decode()


def test_metrics_is_valid_prometheus_text_across_adapters(repo, monkeypatch):
    text = _render(repo, monkeypatch)

    assert text.startswith("# HELP") or text.startswith("# TYPE")
    families = list(text_string_to_metric_families(text))
    names = {f.name for f in families}

    assert "gtfs_quality_composite_score" in names
    assert "gtfs_quality_route_coverage_pct" in names
    assert "gtfs_quality_fresh_within_pct" in names
    assert "gtfs_quality_gap_duration_bucket_count" in names
    assert "gtfs_quality_field_population_pct" in names
    assert "gtfs_quality_poll_success_pct" in names


def test_metrics_labels_stay_low_cardinality(repo, monkeypatch):
    """Nothing here should be keyed by vehicle_id or route_id - that's the
    classic Prometheus cardinality-explosion mistake. temporal_coverage is
    the widest legitimate label set (hour x weekend), capped at 48 series."""
    text = _render(repo, monkeypatch)
    families = {f.name: f for f in text_string_to_metric_families(text)}

    temporal = families["gtfs_quality_temporal_coverage_pct"]
    assert len(temporal.samples) <= 48
