import type {
  AggregatorStatus, CorridorResponse, DarkRoute, FeedStatus, FeedSummary,
  GridResponse, HotspotResponse, HourlyPoint, IngestPoint, LiveAnalytics,
  Overview, PollLog, RouteStop, RouteSummary, SpeedBin, TopRoute, Vehicle,
} from './types'

async function get<T>(path: string): Promise<T> {
  const res = await fetch(path)
  if (!res.ok) throw new Error(`${res.status} ${res.statusText} on ${path}`)
  return res.json() as Promise<T>
}

export const api = {
  feed: () => get<FeedSummary>('/api/feed'),
  live: () => get<LiveAnalytics>('/api/analytics/live'),
  darkRoutes: (limit = 12) => get<{ items: DarkRoute[] }>(`/api/analytics/dark-routes?limit=${limit}`),
  route: (routeId: string) => get<RouteSummary>(`/api/routes/${encodeURIComponent(routeId)}`),
  overview: () => get<Overview>('/api/analytics/overview'),
  topRoutes: (limit = 10) => get<{ items: TopRoute[] }>(`/api/analytics/routes/top?limit=${limit}`),
  ingest: (minutes = 60) => get<{ items: IngestPoint[] }>(`/api/analytics/ingest?minutes=${minutes}`),
  speed: (minutes = 60) => get<{ items: SpeedBin[] }>(`/api/analytics/speed?minutes=${minutes}`),
  feedHealth: (limit = 30) => get<{ items: PollLog[] }>(`/api/analytics/feed-health?limit=${limit}`),
  searchRoutes: (q: string, limit = 25) =>
    get<{ total: number; items: RouteSummary[] }>(
      `/api/routes?limit=${limit}&q=${encodeURIComponent(q)}`),
  routeShape: (routeId: string) =>
    get<GeoJSON.Feature<GeoJSON.LineString>>(`/api/routes/${encodeURIComponent(routeId)}/shape`),
  routeStops: (routeId: string) =>
    get<{ items: RouteStop[] }>(`/api/routes/${encodeURIComponent(routeId)}/stops`),
  vehicles: () => get<{ items: Vehicle[]; status: FeedStatus }>('/api/realtime/vehicles'),

  // ---- analytics tier ----
  grid: (hours: number, factor: number, minObs = 8) =>
    get<GridResponse>(
      `/api/analytics/grid?hours=${hours}&factor=${factor}&min_observations=${minObs}`),
  hotspots: (hours: number, limit = 10) =>
    get<HotspotResponse>(`/api/analytics/hotspots?hours=${hours}&limit=${limit}`),
  hourly: (hours = 24) =>
    get<{ items: HourlyPoint[] }>(`/api/analytics/hourly?hours=${hours}`),
  corridor: (routeId: string, hours: number) =>
    get<CorridorResponse>(
      `/api/analytics/corridor/${encodeURIComponent(routeId)}?hours=${hours}`),
  aggregator: () => get<AggregatorStatus>('/api/analytics/aggregator'),
  vehicleHistory: (id: string, minutes = 60) =>
    get<{ geometry: GeoJSON.LineString; count: number }>(
      `/api/realtime/vehicles/${encodeURIComponent(id)}/history?minutes=${minutes}`),
}
