import { useEffect, useMemo, useState } from 'react'
import { api } from '../api'
import { usePolled } from '../useLiveVehicles'
import { METRICS, METRIC_LIST, rampColor, type MetricId } from '../metrics'
import type {
  CorridorResponse, GridResponse, Hotspot, RouteSummary,
} from '../types'
import { GridMap } from '../components/GridMap'
import { Legend } from '../components/Legend'
import { TrendChart } from '../components/Charts'
import { RoutePanel } from '../components/RoutePanel'
import { Empty, Panel, Rows, StatTile, Tabs } from '../components/ui'
import { InfoTip } from '../components/InfoTip'
import type { TermId } from '../glossary'
import { ago } from '../format'

type HotspotKind = 'congestion' | 'dwell' | 'busiest'

const WINDOWS = [
  { hours: 1, label: '1h' },
  { hours: 6, label: '6h' },
  { hours: 12, label: '12h' },
  { hours: 24, label: '24h' },
]

// Base cell is 250 m; the API rolls it up by integer factors.
const RESOLUTIONS = [
  { factor: 1, label: '250 m' },
  { factor: 2, label: '500 m' },
  { factor: 4, label: '1 km' },
]

function Segmented<T extends string | number>({ value, options, onChange, label, term }: {
  value: T
  options: { id: T; label: string; term?: TermId }[]
  onChange: (v: T) => void
  label: string
  term?: TermId
}) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
      <span style={{
        fontSize: 10, textTransform: 'uppercase', letterSpacing: '0.06em',
        color: 'var(--text-muted)', whiteSpace: 'nowrap',
      }}>{term ? <InfoTip term={term}>{label}</InfoTip> : label}</span>
      <div style={{
        display: 'flex', gap: 2, padding: 2, background: 'var(--surface-2)',
        border: '1px solid var(--border)', borderRadius: 999,
      }}>
        {options.map((o) => {
          const on = o.id === value
          return (
            <button key={String(o.id)} type="button" onClick={() => onChange(o.id)}
              aria-pressed={on}
              style={{
                border: 'none', cursor: 'pointer', borderRadius: 999,
                padding: '3px 10px', fontSize: 11.5, whiteSpace: 'nowrap',
                fontWeight: on ? 600 : 400,
                background: on ? 'var(--series-1)' : 'transparent',
                color: on ? '#fff' : 'var(--text-secondary)',
              }}>{o.term ? <InfoTip term={o.term}>{o.label}</InfoTip> : o.label}</button>
          )
        })}
      </div>
    </div>
  )
}

export function AnalyticsView() {
  const [metric, setMetricRaw] = useState<MetricId>('congestion')
  const [hours, setHours] = useState(6)
  const [factor, setFactor] = useState(2)
  const [kind, setKind] = useState<HotspotKind>('congestion')

  // The map metric is the primary control, so moving it brings the hotspot
  // list along; the list can still be switched independently afterwards.
  const setMetric = (m: MetricId) => {
    setMetricRaw(m)
    setKind(m === 'activity' ? 'busiest' : m)
  }
  const [focus, setFocus] = useState<{ lat: number; lon: number } | null>(null)

  const [routeId, setRouteId] = useState<string | null>(null)
  const [routeMeta, setRouteMeta] = useState<RouteSummary | null>(null)
  const [corridor, setCorridor] = useState<CorridorResponse | null>(null)
  const [corridorError, setCorridorError] = useState<string | null>(null)
  const [routeShape, setRouteShape] = useState<GeoJSON.Feature<GeoJSON.LineString> | null>(null)

  const [grid, setGrid] = useState<GridResponse | null>(null)
  const [gridLoading, setGridLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)

  // The backend rollup itself only runs once every few hours (see
  // AGGREGATOR_INTERVAL_MS below) - the raw GROUP BY it does over the
  // observation log is expensive, so recomputing it on a tight loop is what
  // was pegging the DB's CPU. Polling on the same cadence just re-fetches
  // identical rows in between; "refreshKey" is the only thing that forces an
  // earlier read, and it only advances from the button below.
  const AGGREGATOR_INTERVAL_MS = 6 * 3600_000
  const [refreshKey, setRefreshKey] = useState(0)

  useEffect(() => {
    let alive = true
    setGridLoading(true)
    const load = () => api.grid(hours, factor)
      .then((g) => { if (alive) { setGrid(g); setGridLoading(false) } })
      .catch(() => { if (alive) setGridLoading(false) })
    load()
    const id = setInterval(load, AGGREGATOR_INTERVAL_MS)
    return () => { alive = false; clearInterval(id) }
  }, [hours, factor, refreshKey])

  const hotspots = usePolled(() => api.hotspots(hours, 10), AGGREGATOR_INTERVAL_MS, [hours, refreshKey])
  const hourly = usePolled(() => api.hourly(24), AGGREGATOR_INTERVAL_MS, [refreshKey])
  const agg = usePolled(() => api.aggregator(), AGGREGATOR_INTERVAL_MS, [refreshKey])

  // Manual trigger: forces one rollup pass on the server right now, then
  // re-reads everything above once it's done. This is the only thing (other
  // than the server's own 6h timer) that recomputes the analytics tables.
  const handleRefreshNow = async () => {
    setRefreshing(true)
    try {
      await api.runAggregator(false)
    } catch {
      // Surfacing the rollup failure isn't critical here - the stale data
      // just stays on screen and the user can try again.
    } finally {
      setRefreshing(false)
      setRefreshKey((k) => k + 1)
    }
  }

  useEffect(() => {
    if (!routeId) { setCorridor(null); setRouteShape(null); setRouteMeta(null); return }
    let alive = true
    setCorridorError(null)
    api.route(routeId).then((r) => { if (alive) setRouteMeta(r) }).catch(() => {})
    api.routeShape(routeId).then((f) => { if (alive) setRouteShape(f) })
      .catch(() => { if (alive) setRouteShape(null) })
    api.corridor(routeId, hours)
      .then((c) => { if (alive) setCorridor(c) })
      .catch(() => {
        if (alive) {
          setCorridor(null)
          setCorridorError('No observations for this route in the selected window.')
        }
      })
    return () => { alive = false }
  }, [routeId, hours])

  const spec = METRICS[metric]
  const rows: Hotspot[] = hotspots.data?.[kind] ?? []

  const totals = useMemo(() => {
    if (!grid) return null
    const idx = Object.fromEntries(grid.columns.map((c, i) => [c, i]))
    let obs = 0
    for (const c of grid.cells) obs += c[idx.obs]
    return { cells: grid.count, obs }
  }, [grid])

  const liveCounts = useMemo(() => new Map<string, number>(), [])

  return (
    <div style={{
      display: 'grid', gridTemplateRows: 'auto 1fr', minHeight: 0, overflow: 'hidden',
    }}>
      <div style={{
        display: 'flex', alignItems: 'center', gap: 16, flexWrap: 'wrap',
        padding: '8px 14px', borderBottom: '1px solid var(--border)',
        background: 'var(--surface-1)',
      }}>
        <Segmented label="Metric" value={metric} onChange={setMetric}
                   options={METRIC_LIST.map((m) => ({
                     id: m.id, label: m.label, term: m.id as TermId }))} />
        <Segmented label="Window" term="window" value={hours} onChange={setHours}
                   options={WINDOWS.map((w) => ({ id: w.hours, label: w.label }))} />
        <Segmented label="Cell" term="cellSize" value={factor} onChange={setFactor}
                   options={RESOLUTIONS.map((r) => ({ id: r.factor, label: r.label }))} />
        <div className="num" style={{
          marginLeft: 'auto', fontSize: 12, color: 'var(--text-secondary)',
        }}>
          {gridLoading && !grid ? 'Loading rollup…'
            : totals
              ? <><strong style={{ color: 'var(--text-primary)' }}>
                  {totals.cells.toLocaleString()}</strong> cells ·{' '}
                  {totals.obs.toLocaleString()} observations</>
              : 'No rollup yet'}
        </div>
        <button type="button" onClick={handleRefreshNow} disabled={refreshing}
                title="Recompute the analytics rollup now. It also refreshes on its own every 6 hours."
                style={{
                  border: '1px solid var(--border)', borderRadius: 6,
                  padding: '4px 10px', fontSize: 12, fontWeight: 500,
                  background: refreshing ? 'var(--surface-2)' : 'var(--surface-1)',
                  color: 'var(--text-secondary)',
                  cursor: refreshing ? 'default' : 'pointer',
                }}>
          {refreshing ? 'Refreshing…' : 'Refresh now'}
        </button>
      </div>

      <div style={{
        display: 'grid', gridTemplateColumns: 'minmax(0,1fr) 384px',
        minHeight: 0, overflow: 'hidden',
      }}>
        <main style={{ position: 'relative', minWidth: 0 }}>
          <GridMap grid={grid} metric={metric} corridor={corridor}
                   routeShape={routeShape} focus={focus} />
          <div style={{ position: 'absolute', left: 12, top: 12 }}>
            <Legend spec={spec} grid={grid} />
          </div>

          {corridor && (
            <div style={{
              position: 'absolute', left: 12, bottom: 28, maxWidth: 360,
              background: 'rgba(26,26,25,0.94)', border: '1px solid var(--border)',
              borderRadius: 'var(--radius-sm)', padding: '10px 12px',
              backdropFilter: 'blur(6px)',
            }}>
              <div style={{ fontSize: 12.5, fontWeight: 600, marginBottom: 3 }}>
                {routeMeta?.route_short_name ?? corridor.route_id} corridor
              </div>
              {routeMeta?.route_desc && (
                <div style={{ fontSize: 11, color: 'var(--text-secondary)', marginBottom: 5 }}>
                  {routeMeta.route_desc}
                </div>
              )}
              <div className="num" style={{ fontSize: 11, color: 'var(--text-muted)' }}>
                {corridor.summary.cells} cells · {corridor.summary.observations.toLocaleString()} obs ·
                avg {corridor.summary.avg_speed_kmh} km/h
                (slowest {corridor.summary.slowest_kmh})
              </div>
              <div style={{ fontSize: 10.5, color: 'var(--text-muted)', marginTop: 5 }}>
                Dots are coloured by speed on the congestion ramp.
              </div>
            </div>
          )}
        </main>

        <aside style={{
          borderLeft: '1px solid var(--border)', background: 'var(--surface-0)',
          overflowY: 'auto', padding: 12, display: 'flex', flexDirection: 'column', gap: 12,
        }}>
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8 }}>
            <StatTile term="medianSpeed" label="Median moving speed"
                      value={grid?.scales.speed_kmh
                        ? Math.round((grid.scales.speed_kmh.p10 + grid.scales.speed_kmh.p90) / 2)
                        : '—'}
                      unit="km/h"
                      hint={grid?.scales.speed_kmh
                        ? `p10 ${grid.scales.speed_kmh.p10} · p90 ${grid.scales.speed_kmh.p90}`
                        : undefined} />
            <StatTile term="cellSize" label="Cells with data"
                      value={(grid?.count ?? 0).toLocaleString()}
                      hint={`${grid?.grid.cell_m ?? 250} m each`} />
          </div>

          <Panel title="Hotspots">
            <Tabs value={kind} onChange={setKind} options={[
              { id: 'congestion', label: 'Congestion', term: 'congestion' },
              { id: 'dwell', label: 'Dwell', term: 'dwell' },
              { id: 'busiest', label: 'Busiest', term: 'busiest' },
            ]} />
            <div style={{
              fontSize: 11, color: 'var(--text-muted)', margin: '8px 0 6px', lineHeight: 1.5,
            }}>
              {kind === 'congestion'
                ? `Cells running below ${hotspots.data?.congested_kmh ?? 15} km/h.
                   Ones that are stationary more than ${
                     hotspots.data?.max_dwell_for_congestion_pct ?? 35}% of the time are
                   excluded — those are depots, not jams.`
                : kind === 'dwell'
                  ? 'Where buses sit longest — terminals, depots and layover points.'
                  : 'Highest observation density — the busiest corridors and interchanges.'}
            </div>
            {rows.length ? (
              <div style={{ display: 'grid', gap: 2 }}>
                {rows.map((h) => {
                  const v = kind === 'busiest' ? h.observations
                    : kind === 'dwell' ? h.dwell_pct : h.speed_kmh
                  const swatch = rampColor(
                    kind === 'dwell' ? METRICS.dwell : METRICS.congestion,
                    kind === 'dwell' ? h.dwell_pct : h.speed_kmh, grid ?? undefined)
                  return (
                    <button key={`${h.gy}:${h.gx}`}
                      onClick={() => setFocus({ lat: h.lat, lon: h.lon })}
                      style={{
                        display: 'flex', alignItems: 'center', gap: 8, width: '100%',
                        textAlign: 'left', cursor: 'pointer', background: 'transparent',
                        border: '1px solid transparent', borderRadius: 'var(--radius-sm)',
                        padding: '6px 7px',
                      }}
                      onMouseEnter={(e) => { e.currentTarget.style.background = 'var(--surface-2)' }}
                      onMouseLeave={(e) => { e.currentTarget.style.background = 'transparent' }}>
                      <span style={{
                        width: 4, height: 20, borderRadius: 2, background: swatch,
                        flexShrink: 0,
                      }} />
                      <span style={{
                        fontSize: 12, overflow: 'hidden', textOverflow: 'ellipsis',
                        whiteSpace: 'nowrap',
                      }}>{h.place ?? 'Unnamed area'}</span>
                      <span className="num" style={{
                        marginLeft: 'auto', fontSize: 11.5, fontWeight: 600,
                        whiteSpace: 'nowrap',
                      }}>
                        {kind === 'busiest' ? v.toLocaleString()
                          : kind === 'dwell' ? `${v}%` : `${v} km/h`}
                      </span>
                    </button>
                  )
                })}
              </div>
            ) : (
              <Empty>
                {kind === 'congestion'
                  ? `Nothing below ${hotspots.data?.congested_kmh ?? 15} km/h in this
                     window — traffic is free-flowing. Widen the window to reach a
                     peak period.`
                  : 'No cells meet the minimum observation count yet.'}
              </Empty>
            )}
          </Panel>

          <Panel title="Network speed by hour">
            <TrendChart data={(hourly.data?.items ?? []) as never} dataKey="avg_speed_kmh"
                        label="Average moving speed" unit="km/h"
                        extra={(p) => [
                          ['Observations', Number(p.observations).toLocaleString()],
                          ['Routes active', String(p.routes)],
                        ]} />
          </Panel>

          <Panel title="Dwell by hour">
            <TrendChart data={(hourly.data?.items ?? []) as never} dataKey="dwell_pct"
                        label="Stationary share" unit="%" color="var(--series-3)"
                        extra={(p) => [['Stopped', Number(p.stopped).toLocaleString()]]} />
          </Panel>

          <Panel title="Route corridor">
            <div style={{ fontSize: 11, color: 'var(--text-muted)', marginBottom: 8 }}>
              Pick a route to plot its observed speed, cell by cell, over the
              selected window.
            </div>
            <RoutePanel selected={routeId} onSelect={setRouteId} liveCounts={liveCounts} />
            {corridorError && (
              <div style={{ fontSize: 11, color: 'var(--warning)', marginTop: 6 }}>
                {corridorError}
              </div>
            )}
          </Panel>

          <Panel title="Rollup">
            <Rows terms={{ 'Base cell': 'cellSize', 'Rows folded': 'observations' }} items={[
              ['Base cell', `${agg.data?.grid_m ?? 250} m`],
              ['Refresh', `${agg.data?.interval_s ?? 120}s`],
              ['Last rollup', ago(agg.data?.watermark_ts)],
              ['Rows folded', (agg.data?.last_run?.rows_scanned ?? 0).toLocaleString()],
              ['Cell-hours', (agg.data?.last_run?.cells ?? 0).toLocaleString()],
              ['Pass time', agg.data?.last_run ? `${agg.data.last_run.elapsed_ms} ms` : '—'],
              ['History kept', `${agg.data?.retention_hours ?? 12}h`],
            ]} />
          </Panel>
        </aside>
      </div>
    </div>
  )
}
