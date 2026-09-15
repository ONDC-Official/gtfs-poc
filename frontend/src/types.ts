export interface Vehicle {
  vehicle_id: string
  ts: number
  trip_id: string | null
  route_id: string | null
  lat: number | null
  lon: number | null
  bearing: number | null
  speed: number | null
  stop_id: string | null
  current_status: number | null
  occupancy_status: number | null
  ingested_at: number
  route_name: string | null
  route_desc: string | null
  agency_id: string | null
}

export interface FeedStatus {
  source: 'feed' | 'mock'
  configured: boolean
  poll_seconds: number
  last_poll_at: number | null
  last_success_at: number | null
  last_error: string | null
  consecutive_failures: number
  polls: number
  vehicles: number
  feed_timestamp: number | null
  latency_ms: number | null
}

export interface Overview {
  active_vehicles: number
  active_routes: number
  moving: number
  stale_vehicles: number
  avg_speed_kmh: number | null
  newest_ts: number | null
  poll_success_rate: number | null
  avg_poll_latency_ms: number | null
}

export interface RouteSummary {
  route_id: string
  agency_id: string | null
  route_short_name: string | null
  route_long_name: string | null
  route_desc: string | null
  route_type: number | null
  trip_count: number
}

export interface TopRoute {
  route_id: string
  route_name: string
  route_desc: string | null
  vehicles: number
  avg_speed_kmh: number | null
}

export interface IngestPoint {
  bucket: number
  observations: number
  vehicles: number
  avg_speed_kmh: number | null
}

export interface SpeedBin {
  bin: number
  label: string
  count: number
}

export interface PollLog {
  id: number
  polled_at: number
  ok: number
  source: string
  http_status: number | null
  entity_count: number | null
  new_rows: number | null
  latency_ms: number | null
  error: string | null
}

export interface FeedSummary {
  agencies: number
  routes: number
  stops: number
  trips: number
  stop_times: number
  shape_points: number
  bbox: { min_lat: number; min_lon: number; max_lat: number; max_lon: number } | null
  static_loaded_at: number | null
  history_rows: number
}

export interface RouteStop {
  stop_id: string
  stop_name: string
  stop_lat: number
  stop_lon: number
  stop_sequence: number
  arrival_time: string
  departure_time: string
}

export interface RouteStat {
  route_id: string
  route_name: string
  route_desc: string | null
  vehicles: number
  moving: number
  avg_speed_kmh: number | null
  moving_speed_kmh: number | null
  bunched: number
}

export interface LiveAnalytics {
  generated_at: number
  fleet: { total: number; fresh: number; moving: number; stopped: number; stale: number; no_trip: number }
  speed: { avg_kmh: number | null; p50_kmh: number | null; p85_kmh: number | null; sample: number }
  coverage: { routes_scheduled: number; routes_live: number; routes_dark: number; pct_live: number | null }
  bunching: { bunched_vehicles: number; pct: number | null; threshold_m: number; routes: RouteStat[] }
  quality: {
    implausible_speed: number; implausible_pct: number | null; missing_trip_id: number
    missing_position: number; stale: number; unpopulated_fields: string[]
  }
  dispatch_hours: { hour: number; vehicles: number }[]
  busiest_routes: RouteStat[]
  slowest_routes: RouteStat[]
}

export interface DarkRoute {
  route_id: string
  route_short_name: string | null
  route_desc: string | null
  trip_count: number
}

// ---- analytics tier --------------------------------------------------------

/** Compact columnar grid. `cells` rows are indexed by `columns`; rectangles
 *  are reconstituted client-side from the grid origin. */
export interface GridResponse {
  grid: { lat_deg: number; lon_deg: number; factor: number; cell_m: number }
  window: { since: number; hours: number }
  columns: string[]
  cells: number[][]
  count: number
  truncated: boolean
  scales: {
    speed_kmh: { min: number; max: number; p10: number; p90: number } | null
    obs: { min: number; max: number; p90: number } | null
  }
}

export interface Hotspot {
  gy: number
  gx: number
  lat: number
  lon: number
  observations: number
  peak_vehicles: number
  speed_kmh: number
  dwell_pct: number
  place: string | null
  stop_id: string | null
}

export interface HotspotResponse {
  window: { since: number; hours: number }
  min_observations: number
  max_dwell_for_congestion_pct: number
  congested_kmh: number
  congestion: Hotspot[]
  dwell: Hotspot[]
  busiest: Hotspot[]
}

export interface HourlyPoint {
  hour: number
  observations: number
  routes: number
  moving: number
  stopped: number
  avg_speed_kmh: number | null
  dwell_pct: number | null
}

export interface CorridorPoint {
  lat: number
  lon: number
  observations: number
  speed_kmh: number | null
  dwell_pct: number | null
}

export interface CorridorResponse {
  route_id: string
  window: { since: number; hours: number }
  cell_m: number
  points: CorridorPoint[]
  summary: {
    cells: number
    observations: number
    avg_speed_kmh: number | null
    slowest_kmh: number | null
    fastest_kmh: number | null
  }
}

export interface AggregatorStatus {
  grid_m: number
  interval_s: number
  watermark_ts: number | null
  retention_hours: number
  last_run: {
    rows_scanned: number; cells: number; routes: number
    watermark: number; elapsed_ms: number
  } | null
}

// ---- quality tier -----------------------------------------------------------

export interface QualityCoverage {
  generated_at: number
  fleet_coverage_ratio: { active: number; expected: number | null; pct: number | null }
  route_coverage: {
    routes_scheduled: number; routes_live: number; routes_dark: number
    pct_live: number | null
  }
  dark_route_load: DarkRoute[]
  spatial_coverage: {
    cells_observed: number; cells_total: number | null; pct: number | null
    window_days: number
  }
  temporal_coverage: {
    hour: number; weekend: boolean; avg_routes_live: number
    pct_live: number | null; samples: number
  }[]
}

export interface QualityFreshness {
  generated_at: number
  fresh_within_s: Record<string, { count: number; pct: number | null }>
  per_vehicle_staleness: {
    b_0_60: number; b_60_180: number; b_180_300: number; b_300_600: number; b_600_plus: number
  }
  feed_age_at_source_s: number | null
  native_publish_cadence_s: number | null
  end_to_end_latency_ms: {
    onboard_to_source_s: number | null; publish_delay_s: number | null; ingest_ms: number | null
  }
}

export interface QualityContinuity {
  generated_at: number
  window_hours: number
  report_continuity_pct: number | null
  vehicles: number
  gap_frequency: { gaps_over_threshold: number; threshold_s: number; pct_of_gaps: number | null }
  gap_duration_distribution: Record<string, number | null>
  trip_completeness: { trips: number; complete_trips: number; pct: number | null }
  session_churn: number
}

export interface QualityCorrectness {
  generated_at: number
  implausible_speed: number
  implausible_speed_pct: number | null
  off_route: {
    sampled: number; within_50m: number; pct_within_50m: number | null
    avg_distance_m: number | null; routes_sampled: number
  }
  coordinate_validity: { total: number; out_of_bounds: number; zero_coord: number }
  referential_integrity: { total: number; invalid_route_id: number; invalid_trip_id: number }
  duplicate_snapshot_rate: { polls: number; duplicates: number; pct: number | null }
}

export interface QualityFieldRichness {
  generated_at: number
  field_population: Record<string, { populated: number; total: number; pct: number | null }>
  static_feed: {
    routes: number; stops: number; trips: number; shape_points: number
    static_loaded_at: number | null; age_days: number | null
  }
}

export interface QualitySourceReliability {
  generated_at: number
  window_hours: number
  poll_success_rate: { polls: number; ok: number; pct: number | null }
  error_breakdown: Record<string, number>
  feed_volume_stability: {
    mean_entities: number | null; stdev_entities: number
    min_entities: number | null; max_entities: number | null; unstable: boolean
  }
  schema_stability: { fields: string[]; last_changed_at: number | null }
}

export interface QualitySummary {
  generated_at: number
  composite_qos_score: number | null
  coverage: QualityCoverage
  freshness: QualityFreshness
  continuity: QualityContinuity
  correctness: QualityCorrectness
  field_richness: QualityFieldRichness
  source_reliability: QualitySourceReliability
}
