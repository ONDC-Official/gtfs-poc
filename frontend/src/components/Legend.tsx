import type { GridResponse } from '../types'
import type { MetricSpec } from '../metrics'
import { InfoTip } from './InfoTip'
import type { TermId } from '../glossary'

/** Continuous ramp legend. Labelled at both ends and told in words what a
 *  bright cell means, so the encoding never has to be inferred. */
export function Legend({ spec, grid }: { spec: MetricSpec; grid: GridResponse | null }) {
  if (!grid) return null
  const stops = spec.stops(grid)
  const lo = stops[0]
  const hi = stops[stops.length - 1]
  // The congestion ramp runs bright->dark as speed rises, so the axis labels
  // are ordered by value while the swatches follow the ramp.
  const gradient = stops.map((s) => s.color).join(', ')

  return (
    <div style={{
      background: 'rgba(26,26,25,0.92)', border: '1px solid var(--border)',
      borderRadius: 'var(--radius-sm)', padding: '9px 11px',
      backdropFilter: 'blur(6px)', minWidth: 208,
    }}>
      <div style={{ fontSize: 11, fontWeight: 600, marginBottom: 6 }}>
        <InfoTip term={spec.id as TermId}>{spec.label}</InfoTip>
      </div>
      <div style={{
        height: 9, borderRadius: 3, marginBottom: 5,
        background: `linear-gradient(to right, ${gradient})`,
      }} />
      <div className="num" style={{
        display: 'flex', justifyContent: 'space-between',
        fontSize: 10, color: 'var(--text-muted)',
      }}>
        <span>{spec.format(lo.value)}</span>
        <span>{spec.format(hi.value)}</span>
      </div>
      <div style={{ fontSize: 10.5, color: 'var(--text-secondary)', marginTop: 5 }}>
        {spec.meaning}
      </div>
      <div className="num" style={{ fontSize: 10, color: 'var(--text-muted)', marginTop: 3 }}>
        {grid.count.toLocaleString()} cells · {grid.grid.cell_m} m
        {grid.truncated && ' · truncated'}
      </div>
    </div>
  )
}
