import { useCallback, useEffect, useMemo, useState } from 'react'
import { api } from '../api'
import { usePolled } from '../useLiveVehicles'
import {
  applyFilters, EMPTY_FILTERS, findBunched, isActive, type Filters,
} from '../filters'
import type { FeedStatus, RouteStop, RouteSummary, Vehicle } from '../types'
import { MapView } from '../components/MapView'
import { DispatchChart, IngestChart, RouteBarChart, SpeedChart } from '../components/Charts'
import { RoutePanel } from '../components/RoutePanel'
import { FeedHealth } from '../components/FeedHealth'
import { FilterBar } from '../components/FilterBar'
import { Dot, Empty, Meter, Panel, Rows, StatTile, Tabs } from '../components/ui'
import { InfoTip } from '../components/InfoTip'
import { ago } from '../format'

type TabId = 'fleet' | 'routes' | 'health'

interface Props {
  vehicles: Map<string, Vehicle>
  status: FeedStatus | null
}

export function LiveView({ vehicles, status }: Props) {
  const [tab, setTab] = useState<TabId>('fleet')
  const [filters, setFilters] = useState<Filters>(EMPTY_FILTERS)
  const [routeShape, setRouteShape] = useState<GeoJSON.Feature<GeoJSON.LineString> | null>(null)
  const [routeStops, setRouteStops] = useState<RouteStop[]>([])
  const [routeMeta, setRouteMeta] = useState<RouteSummary | null>(null)
  const [, setTick] = useState(0)

  useEffect(() => {
    const id = setInterval(() => setTick((t) => t + 1), 1000)
    return () => clearInterval(id)
  }, [])

  const live = usePolled(() => api.live(), 15_000)
  const feed = usePolled(() => api.feed(), 300_000)
  const ingest = usePolled(() => api.ingest(60), 30_000)
  const speed = usePolled(() => api.speed(60), 30_000)
  const health = usePolled(() => api.feedHealth(30), 15_000)
  const dark = usePolled(() => api.darkRoutes(12), 60_000)

  const all = useMemo(() => Array.from(vehicles.values()), [vehicles])
  const bunched = useMemo(() => findBunched(all), [all])
  const shown = useMemo(
    () => applyFilters(all, filters, bunched, Date.now() / 1000),
    [all, filters, bunched])

  const liveCounts = useMemo(() => {
    const m = new Map<string, number>()
    for (const v of all) if (v.route_id) m.set(v.route_id, (m.get(v.route_id) ?? 0) + 1)
    return m
  }, [all])

  const selectRoute = useCallback((routeId: string | null) => {
    setFilters((f) => ({ ...f, routeId: f.routeId === routeId ? null : routeId }))
  }, [])

  useEffect(() => {
    const id = filters.routeId
    if (!id) { setRouteShape(null); setRouteStops([]); setRouteMeta(null); return }
    let alive = true
    api.route(id).then((r) => { if (alive) setRouteMeta(r) }).catch(() => {})
    api.routeShape(id).then((f) => { if (alive) setRouteShape(f) })
      .catch(() => { if (alive) setRouteShape(null) })
    api.routeStops(id).then((r) => { if (alive) setRouteStops(r.items) })
      .catch(() => { if (alive) setRouteStops([]) })
    return () => { alive = false }
  }, [filters.routeId])

  const onSelectVehicle = useCallback((_v: Vehicle | null) => {}, [])

  const isMock = status?.source === 'mock'
  const a = live.data
  const bunchedShown = shown.filter((v) => bunched.has(v.vehicle_id)).length
  const filtering = isActive(filters)

  return (
    <div style={{
      display: 'grid', gridTemplateRows: 'auto 1fr', minHeight: 0, overflow: 'hidden',
    }}>
      <FilterBar filters={filters} onChange={setFilters} shown={shown.length}
                 total={all.length} routeLabel={routeMeta?.route_short_name ?? null} />

      <div style={{
        display: 'grid', gridTemplateColumns: 'minmax(0,1fr) 384px',
        minHeight: 0, overflow: 'hidden',
      }}>
        <main style={{ position: 'relative', minWidth: 0 }}>
          <MapView
            vehicles={shown}
            selectedRoute={filters.routeId}
            routeShape={routeShape}
            routeStops={routeStops}
            bunched={bunched}
            onSelectVehicle={onSelectVehicle}
          />

          <div style={{
            position: 'absolute', left: 12, top: 12, display: 'flex', gap: 13,
            alignItems: 'center', background: 'rgba(26,26,25,0.9)',
            border: '1px solid var(--border)', borderRadius: 'var(--radius-sm)',
            padding: '6px 11px', fontSize: 11, color: 'var(--text-secondary)',
            backdropFilter: 'blur(6px)',
          }}>
            <span style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
              <Dot color="var(--series-1)" /><InfoTip term="moving">In transit</InfoTip></span>
            <span style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
              <Dot color="var(--text-muted)" /><InfoTip term="stopped">Stopped</InfoTip></span>
            <span style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
              <Dot color="var(--series-1)" ring="var(--warning)" />
              <InfoTip term="bunched">Bunched</InfoTip></span>
            {filters.routeId && (
              <span style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                <Dot color="var(--series-2)" /> Selected route</span>
            )}
          </div>

          {filters.routeId && (
            <div style={{
              position: 'absolute', left: 12, bottom: 28, maxWidth: 350,
              background: 'rgba(26,26,25,0.94)', border: '1px solid var(--border)',
              borderRadius: 'var(--radius-sm)', padding: '9px 12px',
              backdropFilter: 'blur(6px)',
            }}>
              <div style={{ fontSize: 12.5, fontWeight: 600, marginBottom: 3 }}>
                Route {routeMeta?.route_short_name ?? filters.routeId}
              </div>
              {routeMeta?.route_desc && (
                <div style={{ fontSize: 11, color: 'var(--text-secondary)', marginBottom: 3 }}>
                  {routeMeta.route_desc}
                </div>
              )}
              <div className="num" style={{ fontSize: 11, color: 'var(--text-muted)' }}>
                {routeStops.length} stops · {shown.length} shown · {bunchedShown} bunched
              </div>
            </div>
          )}
        </main>

        <aside style={{
          borderLeft: '1px solid var(--border)', background: 'var(--surface-0)',
          overflowY: 'auto', padding: 12, display: 'flex', flexDirection: 'column', gap: 12,
        }}>
          <Tabs value={tab} onChange={setTab} options={[
            { id: 'fleet', label: 'Fleet' },
            { id: 'routes', label: 'Routes' },
            { id: 'health', label: 'Health' },
          ]} />

          {tab === 'fleet' && (
            <>
              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8 }}>
                <StatTile term="vehiclesTracked"
                          label={filtering ? 'Vehicles shown' : 'Vehicles tracked'}
                          value={shown.length.toLocaleString()}
                          hint={filtering ? `of ${all.length.toLocaleString()} tracked`
                                          : `${a?.fleet.moving.toLocaleString() ?? '—'} moving`} />
                <StatTile term="routesCovered" label="Routes covered"
                          value={a ? `${a.coverage.pct_live}%` : '—'}
                          hint={a ? `${a.coverage.routes_live.toLocaleString()} of ${a.coverage.routes_scheduled.toLocaleString()}`
                                  : undefined} />
                <StatTile term="medianSpeed" label="Median speed"
                          value={a?.speed.p50_kmh ?? '—'} unit="km/h"
                          hint={a?.speed.avg_kmh != null ? `mean ${a.speed.avg_kmh}` : undefined} />
                <StatTile term="bunched" label="Bunched"
                          value={filtering ? bunchedShown.toLocaleString()
                                           : (a?.bunching.bunched_vehicles.toLocaleString() ?? '—')}
                          tone={(a?.bunching.pct ?? 0) > 25 ? 'warning' : 'default'}
                          hint={a?.bunching.pct != null ? `${a.bunching.pct}% within 300 m` : undefined} />
              </div>

              <Panel title="Fleet state">
                {a ? (
                  <Meter total={a.fleet.total} segments={[
                    { label: 'Moving', value: a.fleet.moving,
                      color: 'var(--series-1)', term: 'moving' },
                    { label: 'Stopped', value: a.fleet.stopped,
                      color: 'var(--text-muted)', term: 'stopped' },
                    { label: 'Stale (>5 min)', value: a.fleet.stale,
                      color: 'var(--warning)', term: 'stale' },
                  ]} />
                ) : <Empty>Waiting for the first snapshot…</Empty>}
              </Panel>

              <Panel title="Find a route">
                <RoutePanel selected={filters.routeId} onSelect={selectRoute}
                            liveCounts={liveCounts} />
              </Panel>

              <Panel title="Busiest routes now">
                <RouteBarChart data={a?.busiest_routes ?? []} metric={(r) => r.vehicles}
                               selected={filters.routeId} onSelect={selectRoute}
                               emptyText="No vehicles reporting a route yet." />
              </Panel>
            </>
          )}

          {tab === 'routes' && (
            <>
              <Panel title="Slowest routes · congestion">
                <div style={{ fontSize: 11, color: 'var(--text-muted)', marginBottom: 8 }}>
                  Slowest first. Average speed of buses actually in motion, on
                  routes with at least 3 moving — parked fleets are excluded so
                  this reads as congestion, not depots.
                </div>
                <RouteBarChart data={a?.slowest_routes ?? []}
                               metric={(r) => r.moving_speed_kmh} unit=" km/h"
                               color="var(--series-2)"
                               selected={filters.routeId} onSelect={selectRoute}
                               emptyText="Not enough moving vehicles yet." />
              </Panel>

              <Panel title="Most bunched routes">
                <div style={{ fontSize: 11, color: 'var(--text-muted)', marginBottom: 8 }}>
                  Buses on one route within {a?.bunching.threshold_m ?? 300} m of another —
                  the classic sign of headway collapse.
                </div>
                <RouteBarChart data={a?.bunching.routes ?? []} metric={(r) => r.bunched}
                               color="var(--series-2)"
                               selected={filters.routeId} onSelect={selectRoute}
                               emptyText="No bunching detected." />
              </Panel>

              <Panel title="Network coverage">
                {a ? (
                  <Meter total={a.coverage.routes_scheduled} segments={[
                    { label: 'Routes reporting', value: a.coverage.routes_live,
                      color: 'var(--series-1)', term: 'routesCovered' },
                    { label: 'Dark (no vehicle)', value: a.coverage.routes_dark,
                      color: 'var(--text-muted)', term: 'darkRoutes' },
                  ]} />
                ) : <Empty>Waiting for the first snapshot…</Empty>}
              </Panel>

              <Panel title="Speed distribution · last hour">
                <SpeedChart data={speed.data?.items ?? []} />
              </Panel>

              <Panel title="Fleet by dispatch hour">
                <div style={{ fontSize: 11, color: 'var(--text-muted)', marginBottom: 8 }}>
                  When each reporting bus began its run, read from the trip id.
                </div>
                <DispatchChart data={a?.dispatch_hours ?? []} />
              </Panel>

              <Panel title="Vehicles reporting · last hour">
                <IngestChart data={ingest.data?.items ?? []} />
              </Panel>
            </>
          )}

          {tab === 'health' && (
            <>
              <Panel title="Feed">
                <Rows terms={{ 'Poll latency': 'pollLatency' }} items={[
                  ['Source', isMock ? 'Simulated' : 'GTFS-RT live'],
                  ['Poll interval', `${status?.poll_seconds ?? '—'}s`],
                  ['Last poll', ago(status?.last_poll_at)],
                  ['Poll latency', status?.latency_ms != null ? `${status.latency_ms} ms` : '—'],
                  ['Feed timestamp', ago(status?.feed_timestamp)],
                  ['History rows', (feed.data?.history_rows ?? 0).toLocaleString()],
                ]} />
                {status?.last_error && (
                  <div style={{
                    marginTop: 10, fontSize: 11, color: 'var(--critical)',
                    background: 'rgba(208,59,59,0.1)', border: '1px solid rgba(208,59,59,0.35)',
                    borderRadius: 'var(--radius-sm)', padding: '6px 8px',
                  }}>
                    <strong>▲ Feed error</strong><br />{status.last_error}
                  </div>
                )}
              </Panel>

              <Panel title="Poll health">
                <FeedHealth polls={health.data?.items ?? []} />
              </Panel>

              <Panel title="Data quality">
                {a ? (
                  <>
                    <Rows terms={{
                      [`Implausible speed (>${Math.round(20 * 3.6)} km/h)`]: 'implausible',
                      'Stale (>5 min)': 'stale',
                    }} items={[
                      [`Implausible speed (>${Math.round(20 * 3.6)} km/h)`,
                       <span style={{ color: (a.quality.implausible_pct ?? 0) > 5
                         ? 'var(--warning)' : undefined }}>
                         {a.quality.implausible_speed.toLocaleString()} ({a.quality.implausible_pct}%)
                       </span>],
                      ['Stale (>5 min)', a.quality.stale.toLocaleString()],
                      ['Missing trip id', a.quality.missing_trip_id.toLocaleString()],
                      ['Missing position', a.quality.missing_position.toLocaleString()],
                    ]} />
                    <div style={{
                      marginTop: 10, paddingTop: 10, borderTop: '1px solid var(--border)',
                      fontSize: 11, color: 'var(--text-muted)', lineHeight: 1.6,
                    }}>
                      <strong style={{ color: 'var(--text-secondary)' }}>
                        Never sent by this feed
                      </strong>
                      <div style={{ marginTop: 4, fontFamily: 'ui-monospace, monospace' }}>
                        {a.quality.unpopulated_fields.join(', ')}
                      </div>
                      <div style={{ marginTop: 6 }}>
                        No stop-arrival, crowding or schedule-adherence metrics are
                        possible while these are empty.
                      </div>
                    </div>
                  </>
                ) : <Empty>Waiting for the first snapshot…</Empty>}
              </Panel>

              <Panel title="Dark routes">
                <div style={{ fontSize: 11, color: 'var(--text-muted)', marginBottom: 8 }}>
                  Scheduled routes with nothing reporting, busiest schedule first.
                </div>
                {dark.data?.items.length ? (
                  <div style={{ display: 'grid', gap: 6, maxHeight: 260, overflowY: 'auto' }}>
                    {dark.data.items.map((r) => (
                      <div key={r.route_id} style={{
                        display: 'flex', gap: 8, alignItems: 'baseline', fontSize: 12,
                      }}>
                        <span style={{ fontWeight: 600 }}>{r.route_short_name ?? r.route_id}</span>
                        <span className="num" style={{
                          marginLeft: 'auto', color: 'var(--text-muted)', fontSize: 10,
                          whiteSpace: 'nowrap',
                        }}>{r.trip_count} trips</span>
                      </div>
                    ))}
                  </div>
                ) : <Empty>Every scheduled route is reporting.</Empty>}
              </Panel>

              <Panel title="Static feed">
                <Rows items={[
                  ['Routes', (feed.data?.routes ?? 0).toLocaleString()],
                  ['Stops', (feed.data?.stops ?? 0).toLocaleString()],
                  ['Trips', (feed.data?.trips ?? 0).toLocaleString()],
                  ['Stop times', (feed.data?.stop_times ?? 0).toLocaleString()],
                ]} />
              </Panel>
            </>
          )}
        </aside>
      </div>
    </div>
  )
}
