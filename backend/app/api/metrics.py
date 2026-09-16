"""Prometheus exposition for the quality tier.

A custom Collector rather than module-level Gauge objects: QualityService
already computes everything in one coherent pass (`summary()`, the same
"one call, one snapshot" shape as `/api/analytics/live`), so `collect()`
just reshapes that one dict into Prometheus's wire format on every scrape,
instead of maintaining ~50 stateful gauges across the app's lifetime that
could otherwise go stale if a background updater ever stalled.

Mounted at plain GET /metrics (Prometheus convention), not under /api/, and
not proxied through nginx - Prometheus scrapes the api container directly
over the docker-compose network, the same way the healthcheck already does.
"""
from typing import Any, Dict, Iterable, Optional

from fastapi import APIRouter, Response
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, generate_latest
from prometheus_client.core import GaugeMetricFamily

from . import deps

router = APIRouter(tags=["metrics"])


def _gauge(name: str, doc: str, value: Optional[float],
          labels: Optional[Dict[str, str]] = None) -> Optional[GaugeMetricFamily]:
    """None means "not computable right now" (e.g. no EXPECTED_FLEET_SIZE) -
    Prometheus has no null; the correct representation is no sample at all,
    not a fabricated 0."""
    if value is None:
        return None
    label_names = list(labels or {})
    fam = GaugeMetricFamily(name, doc, labels=label_names)
    fam.add_metric([labels[k] for k in label_names] if labels else [], float(value))
    return fam


class QualityCollector:
    def __init__(self):
        pass

    def collect(self) -> Iterable[GaugeMetricFamily]:
        data = deps.quality.summary()
        yield from _quality_metrics(data)


def _quality_metrics(data: Dict[str, Any]) -> Iterable[GaugeMetricFamily]:
    def g(name, doc, value, labels=None):
        fam = _gauge(name, doc, value, labels)
        if fam is not None:
            yield fam

    yield from g("gtfs_quality_composite_score",
                 "Unweighted mean of the available headline sub-scores (0-100).",
                 data.get("composite_qos_score"))

    # ---- Layer 1: Coverage -------------------------------------------------
    cov = data["coverage"]
    fcr = cov["fleet_coverage_ratio"]
    yield from g("gtfs_quality_fleet_coverage_pct",
                 "Active vehicles / EXPECTED_FLEET_SIZE * 100.", fcr["pct"])
    yield from g("gtfs_quality_fleet_active_vehicles",
                 "Vehicles reporting within the active window.", fcr["active"])
    yield from g("gtfs_quality_fleet_expected_vehicles",
                 "Configured EXPECTED_FLEET_SIZE.", fcr["expected"])

    rc = cov["route_coverage"]
    yield from g("gtfs_quality_route_coverage_pct",
                 "Routes with a live vehicle / scheduled routes * 100.", rc["pct_live"])
    yield from g("gtfs_quality_routes_scheduled", "Routes in the static schedule.",
                 rc["routes_scheduled"])
    yield from g("gtfs_quality_routes_live", "Scheduled routes with >=1 live vehicle.",
                 rc["routes_live"])
    yield from g("gtfs_quality_routes_dark", "Scheduled routes with no live vehicle.",
                 rc["routes_dark"])

    dark = cov["dark_route_load"]
    yield from g("gtfs_quality_dark_route_top_trip_count",
                 "Scheduled trip count of the single busiest dark route "
                 "(not all dark routes - see /api/quality/coverage for the ranked list).",
                 dark[0]["trip_count"] if dark else None)

    sc = cov["spatial_coverage"]
    yield from g("gtfs_quality_spatial_coverage_pct",
                 "Observed grid cells / total cells in the service-area bbox * 100.", sc["pct"])
    yield from g("gtfs_quality_spatial_cells_observed", "250m cells with >=1 observation.",
                 sc["cells_observed"])
    yield from g("gtfs_quality_spatial_cells_total", "250m cells across the static bbox.",
                 sc["cells_total"])

    for row in cov["temporal_coverage"]:
        yield from g("gtfs_quality_temporal_coverage_pct",
                     "Route coverage % by hour-of-day and weekend/weekday.",
                     row["pct_live"],
                     {"hour": str(row["hour"]), "weekend": "1" if row["weekend"] else "0"})

    # ---- Layer 2: Freshness & Latency --------------------------------------
    fr = data["freshness"]
    for threshold, bucket in fr["fresh_within_s"].items():
        yield from g("gtfs_quality_fresh_within_pct",
                     "Share of vehicles updated within N seconds.",
                     bucket["pct"], {"threshold_s": threshold})
    for bucket, count in fr["per_vehicle_staleness"].items():
        yield from g("gtfs_quality_staleness_bucket_count",
                     "Vehicle count by age-since-last-report bucket.",
                     count, {"bucket": bucket})
    yield from g("gtfs_quality_feed_age_at_source_seconds",
                 "poll.polled_at - poll.feed_timestamp, averaged over the window.",
                 fr["feed_age_at_source_s"])
    yield from g("gtfs_quality_native_publish_cadence_seconds",
                 "Gap between successive distinct feed_timestamp values.",
                 fr["native_publish_cadence_s"])
    yield from g("gtfs_quality_ingest_latency_ms",
                 "Our own fetch+parse time per poll.",
                 fr["end_to_end_latency_ms"]["ingest_ms"])

    # ---- Layer 3: Continuity -----------------------------------------------
    ct = data["continuity"]
    yield from g("gtfs_quality_report_continuity_pct",
                 "Observed vs. expected report slots per vehicle, fleet-averaged.",
                 ct["report_continuity_pct"])
    yield from g("gtfs_quality_vehicles_in_window", "Distinct vehicles seen in the window.",
                 ct["vehicles"])
    yield from g("gtfs_quality_gaps_over_threshold",
                 "Reporting gaps > GAP_THRESHOLD_S in the window.",
                 ct["gap_frequency"]["gaps_over_threshold"])
    yield from g("gtfs_quality_gap_pct_of_gaps",
                 "Share of all gaps that exceed the threshold.",
                 ct["gap_frequency"]["pct_of_gaps"])
    for bucket, count in ct["gap_duration_distribution"].items():
        yield from g("gtfs_quality_gap_duration_bucket_count",
                     "Reporting-gap count by duration bucket.", count, {"bucket": bucket})
    tc = ct["trip_completeness"]
    yield from g("gtfs_quality_trip_completeness_pct",
                 "Trips with no internal gap > threshold / total trips * 100.", tc["pct"])
    yield from g("gtfs_quality_trips_total", "Distinct (vehicle, trip) pairs in the window.",
                 tc["trips"])
    yield from g("gtfs_quality_trips_complete", "Trips with no gap over the threshold.",
                 tc["complete_trips"])
    yield from g("gtfs_quality_session_churn",
                 "Stale-then-recovered cycles (same count as gaps_over_threshold).",
                 ct["session_churn"])

    # ---- Layer 4: Correctness ----------------------------------------------
    cr = data["correctness"]
    yield from g("gtfs_quality_implausible_speed_pct",
                 "Vehicles reporting speed >= IMPLAUSIBLE_MPS.", cr["implausible_speed_pct"])
    yield from g("gtfs_quality_implausible_speed_count", "Count, same basis as the pct above.",
                 cr["implausible_speed"])
    orte = cr["off_route"]
    yield from g("gtfs_quality_off_route_pct_within_50m",
                 "Sampled vehicles within 50m of their route shape.", orte["pct_within_50m"])
    yield from g("gtfs_quality_off_route_avg_distance_m",
                 "Mean point-to-polyline distance over sampled vehicles.",
                 orte["avg_distance_m"])
    yield from g("gtfs_quality_off_route_routes_sampled",
                 "Routes included in the off-route pass (capped at 150).",
                 orte["routes_sampled"])
    cv = cr["coordinate_validity"]
    yield from g("gtfs_quality_coordinate_out_of_bounds",
                 "Live positions outside the static feed's bounding box.", cv["out_of_bounds"])
    yield from g("gtfs_quality_coordinate_zero",
                 "Live positions at exactly (0, 0).", cv["zero_coord"])
    ri = cr["referential_integrity"]
    yield from g("gtfs_quality_referential_invalid_route_id",
                 "Live route_id values absent from the static schedule.",
                 ri["invalid_route_id"])
    yield from g("gtfs_quality_referential_invalid_trip_id",
                 "Live trip_id values absent from the static schedule.",
                 ri["invalid_trip_id"])
    yield from g("gtfs_quality_duplicate_snapshot_pct",
                 "Polls where the feed had not actually refreshed (new_rows = 0).",
                 cr["duplicate_snapshot_rate"]["pct"])

    # ---- Layer 5: Field Richness -------------------------------------------
    fld = data["field_richness"]
    for field, stats in fld["field_population"].items():
        yield from g("gtfs_quality_field_population_pct",
                     "Share of recent records with this optional field populated.",
                     stats["pct"], {"field": field})
    sf = fld["static_feed"]
    yield from g("gtfs_quality_static_feed_age_days",
                 "Age of the loaded static GTFS feed.", sf["age_days"])
    yield from g("gtfs_quality_static_routes", "Routes in the static schedule.", sf["routes"])
    yield from g("gtfs_quality_static_stops", "Stops in the static schedule.", sf["stops"])
    yield from g("gtfs_quality_static_trips", "Trips in the static schedule.", sf["trips"])
    yield from g("gtfs_quality_static_shape_points", "Shape points in the static schedule.",
                 sf["shape_points"])

    # ---- Layer 6: Source Reliability ---------------------------------------
    sr = data["source_reliability"]
    yield from g("gtfs_quality_poll_success_pct",
                 "Successful polls / total polls in the window.",
                 sr["poll_success_rate"]["pct"])
    for kind, count in sr["error_breakdown"].items():
        yield from g("gtfs_quality_error_breakdown_count",
                     "Failed polls by error kind.", count, {"kind": kind})
    fv = sr["feed_volume_stability"]
    yield from g("gtfs_quality_feed_volume_mean", "Mean entity_count over successful polls.",
                 fv["mean_entities"])
    yield from g("gtfs_quality_feed_volume_stdev", "Population stdev of entity_count.",
                 fv["stdev_entities"])
    yield from g("gtfs_quality_feed_volume_min", "Minimum entity_count in the window.",
                 fv["min_entities"])
    yield from g("gtfs_quality_feed_volume_max", "Maximum entity_count in the window.",
                 fv["max_entities"])
    yield from g("gtfs_quality_feed_volume_unstable",
                 "1 if the minimum poll was under half the running mean, else 0.",
                 1 if fv["unstable"] else 0)
    ss = sr["schema_stability"]
    yield from g("gtfs_quality_schema_fields_count",
                 "Distinct non-null fields seen in the latest poll batch.",
                 len(ss["fields"]))
    yield from g("gtfs_quality_schema_last_changed_timestamp",
                 "Unix seconds the feed's field set last changed.", ss["last_changed_at"])


@router.get("/metrics")
def metrics() -> Response:
    registry = CollectorRegistry()
    registry.register(QualityCollector())
    return Response(generate_latest(registry), media_type=CONTENT_TYPE_LATEST)
