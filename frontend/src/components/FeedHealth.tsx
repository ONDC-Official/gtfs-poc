import type { PollLog } from '../types'
import { Empty } from './ui'

/** One tick per poll, newest at the right. Height encodes latency, color
 *  encodes outcome - so a run of failures or a latency creep is visible at a
 *  glance without reading numbers. */
export function FeedHealth({ polls }: { polls: PollLog[] }) {
  if (!polls.length) return <Empty>No polls recorded yet.</Empty>

  const maxLatency = Math.max(1, ...polls.map((p) => p.latency_ms ?? 0))
  const failures = polls.filter((p) => !p.ok).length

  return (
    <div>
      <div style={{
        display: 'flex', alignItems: 'flex-end', gap: 2, height: 46,
        marginBottom: 8, overflow: 'hidden',
      }}>
        {polls.map((p) => {
          const h = Math.max(5, Math.round(((p.latency_ms ?? 0) / maxLatency) * 46))
          return (
            <div
              key={p.id}
              title={`${new Date(p.polled_at * 1000).toLocaleTimeString()} · ` +
                     `${p.ok ? 'ok' : 'failed'} · ${p.latency_ms ?? '—'} ms · ` +
                     `${p.entity_count ?? 0} vehicles${p.error ? ` · ${p.error}` : ''}`}
              style={{
                flex: 1, minWidth: 2, height: h, borderRadius: 2,
                background: p.ok ? 'var(--series-1)' : 'var(--critical)',
              }} />
          )
        })}
      </div>
      <div style={{
        display: 'flex', justifyContent: 'space-between', fontSize: 11,
        color: 'var(--text-muted)',
      }}>
        <span>Last {polls.length} polls · peak {maxLatency} ms</span>
        <span className="num" style={{ color: failures ? 'var(--critical)' : 'var(--good)' }}>
          {failures ? `${failures} failed` : 'all ok'}
        </span>
      </div>
    </div>
  )
}
