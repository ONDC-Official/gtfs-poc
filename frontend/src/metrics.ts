import type { ExpressionSpecification } from 'maplibre-gl'
import type { GridResponse } from './types'

/**
 * The choropleth encoding.
 *
 * One sequential hue for every metric (the documented blue ramp), stepped for
 * a dark surface: low values recede toward the basemap, high values come
 * forward. Verified monotonic in lightness, 1.46:1 -> 13.16:1 against the
 * #1a1a19 surface.
 *
 * The invariant is that **brighter always means more of the thing the metric
 * is named for**. Congestion is therefore ramped against falling speed, not
 * rising - a bright cell is a slow cell.
 */
export const RAMP = [
  '#0d366b', '#184f95', '#256abf', '#3987e5', '#6da7ec', '#9ec5f4', '#cde2fb',
] as const

export type MetricId = 'congestion' | 'activity' | 'dwell'

export interface Stop { value: number; color: string }

export interface MetricSpec {
  id: MetricId
  label: string
  /** What a bright cell means. Shown under the legend. */
  meaning: string
  unit: string
  /** MapLibre expression yielding the raw value for a cell feature. */
  expression: ExpressionSpecification
  /** Ramp stops, scaled from the data in this response. */
  stops: (grid: GridResponse) => Stop[]
  /** True when the bright end of the ramp sits at the LOW value end, as it
   *  does for congestion. Opacity has to follow brightness, not the raw
   *  value, or the most important cells end up the faintest. */
  reversed: boolean
  /** The same value, read from a plain object (hotspots, corridor points). */
  read: (o: { speed_kmh?: number | null; obs?: number; dwell_pct?: number | null }) => number | null
  format: (v: number | null) => string
}

function evenStops(lo: number, hi: number, reversed = false): Stop[] {
  const colors = reversed ? [...RAMP].reverse() : [...RAMP]
  const span = hi - lo || 1
  return colors.map((color, i) => ({ value: lo + (span * i) / (colors.length - 1), color }))
}

export const METRICS: Record<MetricId, MetricSpec> = {
  congestion: {
    id: 'congestion',
    label: 'Congestion',
    meaning: 'Brighter = slower moving traffic',
    unit: 'km/h',
    expression: ['coalesce', ['get', 'speed_kmh'], 60] as ExpressionSpecification,
    // Reversed: the low-speed end gets the bright colours. Clamped to the
    // p10-p90 range so a couple of outlier cells do not flatten the scale.
    stops: (g) => evenStops(g.scales.speed_kmh?.p10 ?? 8,
                            g.scales.speed_kmh?.p90 ?? 45, true),
    reversed: true,
    read: (o) => o.speed_kmh ?? null,
    format: (v) => (v == null ? '—' : `${v.toFixed(1)} km/h`),
  },
  activity: {
    id: 'activity',
    label: 'Activity',
    meaning: 'Brighter = more bus observations',
    unit: 'observations',
    expression: ['get', 'obs'] as ExpressionSpecification,
    // p90 rather than max: a handful of terminal cells carry an order of
    // magnitude more traffic and would otherwise own the whole ramp.
    stops: (g) => evenStops(g.scales.obs?.min ?? 0, g.scales.obs?.p90 ?? 200),
    reversed: false,
    read: (o) => o.obs ?? null,
    format: (v) => (v == null ? '—' : v.toLocaleString()),
  },
  dwell: {
    id: 'dwell',
    label: 'Dwell',
    meaning: 'Brighter = buses more often stationary',
    unit: '% stopped',
    expression: ['coalesce', ['get', 'dwell_pct'], 0] as ExpressionSpecification,
    stops: () => evenStops(0, 100),
    reversed: false,
    read: (o) => o.dwell_pct ?? null,
    format: (v) => (v == null ? '—' : `${v.toFixed(1)}%`),
  },
}

export const METRIC_LIST = [METRICS.congestion, METRICS.activity, METRICS.dwell]

/** Ramp colour for a value, used where MapLibre expressions are not available
 *  (corridor dots, legend swatches, table cells). */
export function rampColor(spec: MetricSpec, value: number, grid?: GridResponse): string {
  const stops = spec.stops(grid ?? ({ scales: { speed_kmh: null, obs: null } } as GridResponse))
  if (value <= stops[0].value) return stops[0].color
  const last = stops[stops.length - 1]
  if (value >= last.value) return last.color
  for (let i = 1; i < stops.length; i++) {
    if (value <= stops[i].value) return stops[i].color
  }
  return last.color
}

export function valueOf(spec: MetricSpec, o: Parameters<MetricSpec['read']>[0]) {
  return spec.read(o)
}
