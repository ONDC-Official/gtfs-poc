import { api } from '../api'
import { usePolled } from '../useLiveVehicles'
import { Empty, Meter, Panel, Rows, StatTile } from '../components/ui'
import { InfoTip } from '../components/InfoTip'
import { ago } from '../format'
import type { QualitySummary } from '../types'

const pct = (v: number | null | undefined) => (v == null ? '—' : `${Math.round(v * 10) / 10}%`)
const num = (v: number | null | undefined, unit = '') =>
  v == null ? '—' : `${v.toLocaleString()}${unit}`

const GAP_LABELS: [string, string][] = [
  ['b_0_60', '≤ 1 min'], ['b_60_180', '1–3 min'], ['b_180_300', '3–5 min'],
  ['b_300_600', '5–10 min'], ['b_600_plus', '> 10 min'],
]
const GAP_COLORS = ['var(--good)', 'var(--series-1)', 'var(--warning)',
                    'var(--series-3)', 'var(--critical)']

function GapMeter({ buckets }: { buckets: Record<string, number | null> }) {
  const total = GAP_LABELS.reduce((s, [k]) => s + (buckets[k] || 0), 0)
  if (!total) return <Empty>No reporting gaps in this window.</Empty>
  return <Meter total={total} segments={GAP_LABELS.map(([k, label], i) => ({
    label, value: buckets[k] || 0, color: GAP_COLORS[i],
  }))} />
}

export function QualityView() {
  const { data, error } = usePolled(() => api.qualitySummary(), 60_000)

  if (error) return <div style={{ padding: 16 }}><Empty>Failed to load quality metrics — {error}</Empty></div>
  if (!data) return <div style={{ padding: 16 }}><Empty>Loading quality metrics…</Empty></div>

  const { coverage, freshness, continuity, correctness, field_richness,
          source_reliability, composite_qos_score, generated_at } = data as QualitySummary

  const bestHour = coverage.temporal_coverage.length
    ? coverage.temporal_coverage.reduce((a, b) => (b.pct_live ?? -1) > (a.pct_live ?? -1) ? b : a)
    : null
  const worstHour = coverage.temporal_coverage.length
    ? coverage.temporal_coverage.reduce((a, b) => (b.pct_live ?? 101) < (a.pct_live ?? 101) ? b : a)
    : null

  return (
    <div style={{
      display: 'grid', gap: 14, padding: 14, overflowY: 'auto', alignContent: 'start',
      gridTemplateColumns: 'repeat(auto-fit, minmax(340px, 1fr))',
    }}>
      <div style={{ gridColumn: '1 / -1', display: 'flex', alignItems: 'center', gap: 14 }}>
        <StatTile label="Composite QoS score" value={num(composite_qos_score)} unit="/ 100"
                  term="compositeQos"
                  tone={composite_qos_score == null ? 'default'
                       : composite_qos_score >= 80 ? 'good'
                       : composite_qos_score >= 60 ? 'warning' : 'critical'} />
        <span style={{ fontSize: 11.5, color: 'var(--text-muted)' }}>
          updated {ago(generated_at)}
        </span>
      </div>

      {/* ---- Layer 1: Coverage ---- */}
      <Panel title="Coverage — is the data there at all?">
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(128px, 1fr))', gap: 8, marginBottom: 12 }}>
          <StatTile label="Fleet coverage" value={pct(coverage.fleet_coverage_ratio.pct)}
                    term="fleetCoverageRatio"
                    hint={coverage.fleet_coverage_ratio.expected == null ? 'set EXPECTED_FLEET_SIZE' : undefined} />
          <StatTile label="Route coverage" value={pct(coverage.route_coverage.pct_live)}
                    term="routesCovered" />
          <StatTile label="Spatial coverage" value={pct(coverage.spatial_coverage.pct)}
                    term="spatialCoverage" hint={`${coverage.spatial_coverage.window_days}d window`} />
        </div>
        <Rows terms={{ 'Best hour': 'temporalCoverage', 'Worst hour': 'temporalCoverage' }} items={[
          ['Routes scheduled', num(coverage.route_coverage.routes_scheduled)],
          ['Routes dark', num(coverage.route_coverage.routes_dark)],
          ...(bestHour ? [['Best hour', `${String(bestHour.hour).padStart(2, '0')}:00 — ${pct(bestHour.pct_live)}`] as [string, string]] : []),
          ...(worstHour ? [['Worst hour', `${String(worstHour.hour).padStart(2, '0')}:00 — ${pct(worstHour.pct_live)}`] as [string, string]] : []),
        ]} />
        {coverage.dark_route_load.length > 0 && (
          <div style={{ marginTop: 12 }}>
            <div style={{ fontSize: 10, textTransform: 'uppercase', letterSpacing: '0.06em',
                          color: 'var(--text-muted)', marginBottom: 6 }}>
              <InfoTip term="darkRoutes">Busiest dark routes</InfoTip>
            </div>
            <Rows items={coverage.dark_route_load.slice(0, 5).map((r) => (
              [r.route_short_name || r.route_id, `${r.trip_count} trips`] as [string, string]
            ))} />
          </div>
        )}
      </Panel>

      {/* ---- Layer 2: Freshness & Latency ---- */}
      <Panel title="Freshness & latency — how current is the data?">
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(128px, 1fr))', gap: 8, marginBottom: 12 }}>
          <StatTile label="Fresh ≤ 60s" value={pct(freshness.fresh_within_s['60']?.pct)} term="fresh" />
          <StatTile label="Feed age at source" value={num(freshness.feed_age_at_source_s, 's')} />
          <StatTile label="Publish cadence" value={num(freshness.native_publish_cadence_s, 's')} />
        </div>
        <div style={{ fontSize: 10, textTransform: 'uppercase', letterSpacing: '0.06em',
                      color: 'var(--text-muted)', marginBottom: 6 }}>Per-vehicle staleness</div>
        <GapMeter buckets={freshness.per_vehicle_staleness} />
        <div style={{ marginTop: 12 }}>
          <Rows items={[
            ['Publish delay', num(freshness.end_to_end_latency_ms.publish_delay_s, 's')],
            ['Ingest latency', num(freshness.end_to_end_latency_ms.ingest_ms, 'ms')],
          ]} />
        </div>
      </Panel>

      {/* ---- Layer 3: Continuity ---- */}
      <Panel title="Continuity — does a bus keep reporting without gaps?">
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(128px, 1fr))', gap: 8, marginBottom: 12 }}>
          <StatTile label="Report continuity" value={pct(continuity.report_continuity_pct)}
                    term="reportContinuity" />
          <StatTile label="Trip completeness" value={pct(continuity.trip_completeness.pct)}
                    term="tripCompleteness" />
          <StatTile label="Session churn" value={num(continuity.session_churn)} term="sessionChurn" />
        </div>
        <div style={{ fontSize: 10, textTransform: 'uppercase', letterSpacing: '0.06em',
                      color: 'var(--text-muted)', marginBottom: 6 }}>
          <InfoTip term="reportingGap">Gap duration distribution</InfoTip>
        </div>
        <GapMeter buckets={continuity.gap_duration_distribution} />
      </Panel>

      {/* ---- Layer 4: Correctness ---- */}
      <Panel title="Correctness — is the data believable?">
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(128px, 1fr))', gap: 8, marginBottom: 12 }}>
          <StatTile label="Implausible speed" value={pct(correctness.implausible_speed_pct)}
                    term="implausible" tone={correctness.implausible_speed_pct && correctness.implausible_speed_pct > 10 ? 'warning' : 'default'} />
          <StatTile label="Off-route (>50m)" value={pct(
            correctness.off_route.pct_within_50m == null ? null
              : 100 - correctness.off_route.pct_within_50m)} term="offRoute" />
          <StatTile label="Duplicate polls" value={pct(correctness.duplicate_snapshot_rate.pct)}
                    term="duplicateSnapshot" />
        </div>
        <Rows terms={{ 'Referential integrity': 'referentialIntegrity', 'Coordinate validity': 'coordinateValidity' }} items={[
          ['Out of bounds', num(correctness.coordinate_validity.out_of_bounds)],
          ['Zero coordinates', num(correctness.coordinate_validity.zero_coord)],
          ['Invalid route_id', num(correctness.referential_integrity.invalid_route_id)],
          ['Invalid trip_id', num(correctness.referential_integrity.invalid_trip_id)],
        ]} />
      </Panel>

      {/* ---- Layer 5: Field Richness ---- */}
      <Panel title="Field richness — how complete is each record?">
        <Rows terms={Object.fromEntries(
          Object.keys(field_richness.field_population).map((f) => [f, 'fieldPopulation'])
        )} items={Object.entries(field_richness.field_population).map(([field, s]) => (
          [field, pct(s.pct)] as [string, string]
        ))} />
        <div style={{ marginTop: 12 }}>
          <Rows items={[
            ['Static routes / stops / trips', `${num(field_richness.static_feed.routes)} / `
              + `${num(field_richness.static_feed.stops)} / ${num(field_richness.static_feed.trips)}`],
            ['Static feed age', num(field_richness.static_feed.age_days, 'd')],
          ]} />
        </div>
      </Panel>

      {/* ---- Layer 6: Source Reliability ---- */}
      <Panel title="Source reliability — is the feed endpoint itself healthy?">
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(2, 1fr)', gap: 8, marginBottom: 12 }}>
          <StatTile label="Poll success rate" value={pct(source_reliability.poll_success_rate.pct)}
                    term="pollSuccessRate" />
          <StatTile label="Mean vehicles / poll" value={num(source_reliability.feed_volume_stability.mean_entities)}
                    term="feedVolumeStability"
                    tone={source_reliability.feed_volume_stability.unstable ? 'critical' : 'default'} />
        </div>
        {Object.keys(source_reliability.error_breakdown).length > 0 && (
          <Rows items={Object.entries(source_reliability.error_breakdown).map(([kind, n]) => (
            [kind, String(n)] as [string, string]
          ))} />
        )}
        <div style={{ marginTop: 12 }}>
          <Rows terms={{ 'Schema last changed': 'schemaStability' }} items={[
            ['Fields sent', String(source_reliability.schema_stability.fields.length)],
            ['Schema last changed', ago(source_reliability.schema_stability.last_changed_at)],
          ]} />
        </div>
      </Panel>
    </div>
  )
}
