# Grafana Metrics Reference

## Composite score

| Metric | Formula |
|---|---|
| `gtfs_quality_composite_score` | Unweighted mean of 5 sub-scores (each already 0–100), skipping any that are `null`: `route_coverage.pct_live`, `freshness.fresh_within_s["60"].pct`, `continuity.report_continuity_pct`, `100 - correctness.implausible_speed_pct`, `source_reliability.poll_success_rate.pct`. |

## Layer 1 — Coverage

| Metric | Formula |
|---|---|
| `gtfs_quality_fleet_coverage_pct` | `active_vehicles / EXPECTED_FLEET_SIZE * 100` |
| `gtfs_quality_fleet_active_vehicles` | Count of vehicles with a position update within the "active" staleness window (`STALE_S`) |
| `gtfs_quality_fleet_expected_vehicles` | Configured `EXPECTED_FLEET_SIZE` |
| `gtfs_quality_route_coverage_pct` | `routes_live / routes_scheduled * 100` |
| `gtfs_quality_routes_scheduled` | Distinct `route_id` count in the static GTFS schedule |
| `gtfs_quality_routes_live` | Distinct `route_id`s with ≥1 vehicle observation in the last `days` |
| `gtfs_quality_routes_dark` | `routes_scheduled - routes_live` |
| `gtfs_quality_dark_route_top_trip_count` | Scheduled trip count of the single busiest dark route |
| `gtfs_quality_spatial_coverage_pct` | `cells_observed / cells_total * 100` |
| `gtfs_quality_spatial_cells_observed` | Distinct 250m×250m grid cells with ≥1 vehicle observation in the last `days` |
| `gtfs_quality_spatial_cells_total` | `(bbox_lat_span / 250m) * (bbox_lon_span / 250m)`, from the static feed's bounding box |
| `gtfs_quality_temporal_coverage_pct` | Per (`hour`, `weekend`) label pair: `avg_routes_live_in_slot / routes_scheduled * 100`, averaged over the last `days` |

## Layer 2 — Freshness & Latency

| Metric | Formula |
|---|---|
| `gtfs_quality_fresh_within_pct` | Per `threshold_s` label (30/60/120/300): `vehicles with (now - last_report_ts) <= threshold_s / total_vehicles * 100` |
| `gtfs_quality_staleness_bucket_count` | Vehicle count bucketed by `now - last_report_ts`: `0-60s`, `60-180s`, `180-300s`, `300-600s`, `600s+` |
| `gtfs_quality_feed_age_at_source_seconds` | `mean(poll.polled_at - poll.feed_timestamp)` over successful polls in the window |
| `gtfs_quality_native_publish_cadence_seconds` | `mean(gap between successive distinct feed_timestamp values)` in the window |
| `gtfs_quality_ingest_latency_ms` | `mean(poll.latency_ms)` over successful polls in the window |

## Layer 3 — Continuity

| Metric | Formula |
|---|---|
| `gtfs_quality_report_continuity_pct` | `min(100, avg_observations_per_vehicle / expected_slots * 100)`, where `expected_slots = (avg_span_s / rt_poll_seconds) + 1` |
| `gtfs_quality_vehicles_in_window` | Distinct vehicles observed in the window |
| `gtfs_quality_gaps_over_threshold` | Count of reporting gaps `> GAP_THRESHOLD_S` |
| `gtfs_quality_gap_pct_of_gaps` | `gaps_over_threshold / gaps_total * 100` |
| `gtfs_quality_gap_duration_bucket_count` | Gap count bucketed by duration |
| `gtfs_quality_trip_completeness_pct` | `complete_trips / total_trips * 100` |
| `gtfs_quality_trips_total` | Distinct (`vehicle_id`, `trip_id`) pairs observed in the window |
| `gtfs_quality_trips_complete` | Trips with no gap over the threshold |
| `gtfs_quality_session_churn` | Same count as `gaps_over_threshold` |

## Layer 4 — Correctness

| Metric | Formula |
|---|---|
| `gtfs_quality_implausible_speed_pct` | `vehicles with speed >= IMPLAUSIBLE_MPS / total_vehicles * 100` |
| `gtfs_quality_implausible_speed_count` | Count, same basis as the pct above |
| `gtfs_quality_off_route_pct_within_50m` | `vehicles within 50m of route shape / sampled_vehicles * 100`, sampled over the busiest ≤150 routes (`OFF_ROUTE_MAX_ROUTES`) |
| `gtfs_quality_off_route_avg_distance_m` | `mean(point_to_polyline_distance_m)` over sampled vehicles |
| `gtfs_quality_off_route_routes_sampled` | Number of routes included in the off-route pass (capped at 150) |
| `gtfs_quality_coordinate_out_of_bounds` | Positions outside the static feed's bounding box |
| `gtfs_quality_coordinate_zero` | Positions at exactly `(0, 0)` |
| `gtfs_quality_referential_invalid_route_id` | `route_id` values absent from the static schedule, in the window |
| `gtfs_quality_referential_invalid_trip_id` | `trip_id` values absent from the static schedule, in the window |
| `gtfs_quality_duplicate_snapshot_pct` | `polls where new_rows == 0 / total_ok_polls * 100`, in the window |

## Layer 5 — Field Richness

| Metric | Formula |
|---|---|
| `gtfs_quality_field_population_pct` | Per `field` label: `records with that optional field populated / total_records * 100`, over recent records in the window |
| `gtfs_quality_static_feed_age_days` | `(now - static_loaded_at) / 86400` |
| `gtfs_quality_static_routes` | Routes in the loaded static schedule |
| `gtfs_quality_static_stops` | Stops in the loaded static schedule |
| `gtfs_quality_static_trips` | Trips in the loaded static schedule |
| `gtfs_quality_static_shape_points` | Shape points in the loaded static schedule |

## Layer 6 — Source Reliability

| Metric | Formula |
|---|---|
| `gtfs_quality_poll_success_pct` | `successful_polls / total_polls * 100`, in the window |
| `gtfs_quality_error_breakdown_count` | Per `kind` label (`timeout`, `5xx`, `4xx`, `empty_body`, `other`): count of failed polls of that kind |
| `gtfs_quality_feed_volume_mean` | `mean(entity_count)` over successful polls in the window |
| `gtfs_quality_feed_volume_stdev` | Population stdev of `entity_count` over successful polls |
| `gtfs_quality_feed_volume_min` | `min(entity_count)` over successful polls in the window |
| `gtfs_quality_feed_volume_max` | `max(entity_count)` over successful polls in the window |
| `gtfs_quality_feed_volume_unstable` | `1` if the minimum poll's `entity_count` was under half the window's mean, else `0` |
| `gtfs_quality_schema_fields_count` | Count of distinct non-null fields seen in the latest poll batch |
| `gtfs_quality_schema_last_changed_timestamp` | Unix seconds the feed's field set last changed |
