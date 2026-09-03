import {
  Bar, BarChart, CartesianGrid, Cell, LabelList, Line, LineChart,
  ResponsiveContainer, Tooltip, XAxis, YAxis,
} from 'recharts'
import type { IngestPoint, RouteStat, SpeedBin } from '../types'
import type { ReactElement } from 'react'
import { Empty } from './ui'

/** ResponsiveContainer measures its parent, so give it a parent with a real
 *  height - otherwise tall charts get clipped by the panel. */
function ChartBox({ height, children }: { height: number; children: ReactElement }) {
  return (
    <div style={{ width: '100%', height }}>
      <ResponsiveContainer width="100%" height="100%">{children}</ResponsiveContainer>
    </div>
  )
}

const AXIS = { stroke: 'var(--text-muted)', fontSize: 10 }
const GRID = 'var(--grid)'

/** Shared tooltip so every chart in the console reads identically. */
function Box({ title, rows }: { title: string; rows: [string, string][] }) {
  return (
    <div style={{
      background: 'var(--surface-2)', border: '1px solid var(--border-strong)',
      borderRadius: 'var(--radius-sm)', padding: '8px 10px', fontSize: 12,
      boxShadow: '0 8px 24px rgba(0,0,0,0.5)',
    }}>
      <div style={{ fontWeight: 600, marginBottom: 4 }}>{title}</div>
      {rows.map(([k, v]) => (
        <div key={k} style={{ display: 'flex', gap: 12, justifyContent: 'space-between' }}>
          <span style={{ color: 'var(--text-muted)' }}>{k}</span>
          <span className="num" style={{ color: 'var(--text-primary)' }}>{v}</span>
        </div>
      ))}
    </div>
  )
}

const hhmm = (s: number) =>
  new Date(s * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })

// ---------------------------------------------------------------------------

/** Vehicles reporting over time. One series - the title names it, so no legend.
 *  Observations and vehicles live on different scales, so observations is a
 *  tooltip row rather than a second axis. */
export function IngestChart({ data }: { data: IngestPoint[] }) {
  if (data.length < 2) return <Empty>Collecting — the trend appears after a few polls.</Empty>
  return (
    <ChartBox height={140}>
      <LineChart data={data} margin={{ top: 6, right: 10, bottom: 0, left: 0 }}>
        <CartesianGrid stroke={GRID} vertical={false} />
        <XAxis dataKey="bucket" tickFormatter={hhmm} tick={AXIS} tickLine={false}
               axisLine={{ stroke: GRID }} minTickGap={40} />
        <YAxis tick={AXIS} tickLine={false} axisLine={false} width={34} allowDecimals={false} />
        <Tooltip
          cursor={{ stroke: 'var(--border-strong)', strokeWidth: 1 }}
          content={({ active, payload }) => {
            if (!active || !payload?.length) return null
            const p = payload[0].payload as IngestPoint
            return <Box title={hhmm(p.bucket)} rows={[
              ['Vehicles reporting', String(p.vehicles)],
              ['Observations', String(p.observations)],
              ['Avg speed', p.avg_speed_kmh != null ? `${p.avg_speed_kmh} km/h` : '—'],
            ]} />
          }} />
        <Line type="monotone" dataKey="vehicles" stroke="var(--series-1)" strokeWidth={2}
              dot={false} activeDot={{ r: 4, strokeWidth: 2, stroke: 'var(--surface-1)' }}
              isAnimationActive={false} />
      </LineChart>
    </ChartBox>
  )
}

/** Speed histogram over the recent observation window. */
export function SpeedChart({ data }: { data: SpeedBin[] }) {
  if (!data.some((d) => d.count > 0)) return <Empty>No speed samples yet.</Empty>
  return (
    <ChartBox height={148}>
      <BarChart data={data} margin={{ top: 6, right: 10, bottom: 0, left: 0 }} barCategoryGap={2}>
        <CartesianGrid stroke={GRID} vertical={false} />
        <XAxis dataKey="label" tick={AXIS} tickLine={false} axisLine={{ stroke: GRID }}
               interval="preserveStartEnd" minTickGap={2} height={18} />
        <YAxis tick={AXIS} tickLine={false} axisLine={false} width={34} allowDecimals={false} />
        <Tooltip
          cursor={{ fill: 'rgba(255,255,255,0.04)' }}
          content={({ active, payload }) => {
            if (!active || !payload?.length) return null
            const p = payload[0].payload as SpeedBin
            return <Box title={`${p.label} km/h`}
                        rows={[['Observations', p.count.toLocaleString()]]} />
          }} />
        {/* 4px rounded ends, anchored to the baseline */}
        <Bar dataKey="count" fill="var(--series-1)" radius={[4, 4, 0, 0]}
             isAnimationActive={false} />
      </BarChart>
    </ChartBox>
  )
}

/**
 * Horizontal bars over routes. Busiest, slowest and most-bunched are the same
 * form with a different measure, so they share one implementation.
 * Direct-labelled: with <=10 bars the value reads better at the bar end than
 * off a shared axis.
 */
export function RouteBarChart({ data, metric, unit, color = 'var(--series-1)',
                                selected, onSelect, emptyText }: {
  data: RouteStat[]
  metric: (r: RouteStat) => number | null
  unit?: string
  color?: string
  selected: string | null
  onSelect: (routeId: string) => void
  emptyText: string
}) {
  const rows = data.filter((d) => metric(d) != null)
  if (!rows.length) return <Empty>{emptyText}</Empty>
  const plotted = rows.map((r) => ({ ...r, value: metric(r) as number }))
  const height = Math.max(110, plotted.length * 26 + 14)

  return (
    <ChartBox height={height}>
      <BarChart data={plotted} layout="vertical" barCategoryGap={2}
                margin={{ top: 4, right: 46, bottom: 4, left: 4 }}>
        <CartesianGrid stroke={GRID} horizontal={false} />
        <XAxis type="number" hide />
        <YAxis type="category" dataKey="route_name" width={104} tick={AXIS}
               tickLine={false} axisLine={false}
               tickFormatter={(v: string) => (v.length > 14 ? v.slice(0, 13) + '…' : v)} />
        <Tooltip
          cursor={{ fill: 'rgba(255,255,255,0.04)' }}
          content={({ active, payload }) => {
            if (!active || !payload?.length) return null
            const p = payload[0].payload as RouteStat
            return <Box title={p.route_name} rows={[
              ['Vehicles', String(p.vehicles)],
              ['Moving', String(p.moving)],
              ['Speed (moving)', p.moving_speed_kmh != null ? `${p.moving_speed_kmh} km/h` : '—'],
              ['Bunched', String(p.bunched)],
              ...(p.route_desc ? [['Route', p.route_desc] as [string, string]] : []),
            ]} />
          }} />
        <Bar dataKey="value" radius={[0, 4, 4, 0]} isAnimationActive={false}
             onClick={(d: unknown) => onSelect((d as RouteStat).route_id)}
             style={{ cursor: 'pointer' }}>
          {plotted.map((d) => (
            <Cell key={d.route_id}
                  fill={d.route_id === selected ? 'var(--series-2)' : color} />
          ))}
          <LabelList dataKey="value" position="right"
                     formatter={(v: unknown) => (unit ? `${v}${unit}` : String(v))}
                     style={{ fill: 'var(--text-secondary)', fontSize: 10 }} />
        </Bar>
      </BarChart>
    </ChartBox>
  )
}

/** Fleet dispatched by scheduled hour, read out of the trip_id. Shows the
 *  service curve and how much of it is still on the road. */
export function DispatchChart({ data }: { data: { hour: number; vehicles: number }[] }) {
  const active = data.filter((d) => d.vehicles > 0)
  if (active.length < 2) return <Empty>Not enough dispatch data yet.</Empty>
  // Trim leading/trailing dead hours so the plotted range is the service day.
  const first = data.findIndex((d) => d.vehicles > 0)
  const last = data.length - 1 - [...data].reverse().findIndex((d) => d.vehicles > 0)
  const window = data.slice(first, last + 1)

  return (
    <ChartBox height={140}>
      <BarChart data={window} margin={{ top: 6, right: 10, bottom: 0, left: 0 }}
                barCategoryGap={2}>
        <CartesianGrid stroke={GRID} vertical={false} />
        <XAxis dataKey="hour" tick={AXIS} tickLine={false} axisLine={{ stroke: GRID }}
               tickFormatter={(h: number) => `${String(h).padStart(2, '0')}h`}
               interval="preserveStartEnd" minTickGap={2} height={18} />
        <YAxis tick={AXIS} tickLine={false} axisLine={false} width={34} allowDecimals={false} />
        <Tooltip
          cursor={{ fill: 'rgba(255,255,255,0.04)' }}
          content={({ active: a, payload }) => {
            if (!a || !payload?.length) return null
            const p = payload[0].payload as { hour: number; vehicles: number }
            return <Box title={`Dispatched ${String(p.hour).padStart(2, '0')}:00`}
                        rows={[['Vehicles still reporting', p.vehicles.toLocaleString()]]} />
          }} />
        <Bar dataKey="vehicles" fill="var(--series-3)" radius={[4, 4, 0, 0]}
             isAnimationActive={false} />
      </BarChart>
    </ChartBox>
  )
}

/** Single-measure trend over time. Two measures of different scale get two of
 *  these rather than a second y-axis. */
export function TrendChart({ data, dataKey, label, unit, color = 'var(--series-1)',
                            extra, height = 118 }: {
  data: Record<string, number | null>[]
  dataKey: string
  label: string
  unit?: string
  color?: string
  /** Additional tooltip rows, so context travels with the point. */
  extra?: (p: Record<string, number | null>) => [string, string][]
  height?: number
}) {
  const points = data.filter((d) => d[dataKey] != null)
  if (points.length < 2) return <Empty>Not enough history yet — the rollup needs a few buckets.</Empty>
  return (
    <ChartBox height={height}>
      <LineChart data={data} margin={{ top: 6, right: 10, bottom: 0, left: 0 }}>
        <CartesianGrid stroke={GRID} vertical={false} />
        <XAxis dataKey="hour" tickFormatter={hhmm} tick={AXIS} tickLine={false}
               axisLine={{ stroke: GRID }} minTickGap={40} />
        <YAxis tick={AXIS} tickLine={false} axisLine={false} width={34} />
        <Tooltip
          cursor={{ stroke: 'var(--border-strong)', strokeWidth: 1 }}
          content={({ active, payload }) => {
            if (!active || !payload?.length) return null
            const p = payload[0].payload as Record<string, number | null>
            const v = p[dataKey]
            return <Box title={hhmm(p.hour as number)} rows={[
              [label, v == null ? '—' : `${v}${unit ? ' ' + unit : ''}`],
              ...(extra ? extra(p) : []),
            ]} />
          }} />
        <Line type="monotone" dataKey={dataKey} stroke={color} strokeWidth={2}
              dot={false} activeDot={{ r: 4, strokeWidth: 2, stroke: 'var(--surface-1)' }}
              isAnimationActive={false} connectNulls />
      </LineChart>
    </ChartBox>
  )
}
