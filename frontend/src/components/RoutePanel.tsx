import { useEffect, useState } from 'react'
import { api } from '../api'
import type { RouteSummary } from '../types'
import { Empty } from './ui'

interface Props {
  selected: string | null
  onSelect: (routeId: string | null) => void
  liveCounts: Map<string, number>
}

export function RoutePanel({ selected, onSelect, liveCounts }: Props) {
  const [q, setQ] = useState('')
  const [results, setResults] = useState<RouteSummary[]>([])
  const [total, setTotal] = useState(0)
  const [busy, setBusy] = useState(false)

  // Debounced so typing a route number does not fire a query per keystroke.
  useEffect(() => {
    if (q.trim().length < 1) { setResults([]); setTotal(0); return }
    setBusy(true)
    const id = setTimeout(() => {
      api.searchRoutes(q.trim(), 40)
        .then((r) => { setResults(r.items); setTotal(r.total) })
        .catch(() => { setResults([]); setTotal(0) })
        .finally(() => setBusy(false))
    }, 250)
    return () => { clearTimeout(id); setBusy(false) }
  }, [q])

  return (
    <div>
      <div style={{ display: 'flex', gap: 6, marginBottom: 10 }}>
        <input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="Search route or destination…"
          style={{
            flex: 1, minWidth: 0, background: 'var(--surface-2)',
            border: '1px solid var(--border)', borderRadius: 'var(--radius-sm)',
            color: 'var(--text-primary)', padding: '7px 10px', fontSize: 13, outline: 'none',
          }}
          onFocus={(e) => { e.target.style.borderColor = 'var(--series-1)' }}
          onBlur={(e) => { e.target.style.borderColor = 'var(--border)' }}
        />
        {selected && (
          <button
            onClick={() => onSelect(null)}
            style={{
              background: 'var(--surface-2)', border: '1px solid var(--border)',
              borderRadius: 'var(--radius-sm)', padding: '0 10px', cursor: 'pointer',
              fontSize: 12, color: 'var(--text-secondary)', whiteSpace: 'nowrap',
            }}>Clear</button>
        )}
      </div>

      {q && (
        <div style={{ fontSize: 11, color: 'var(--text-muted)', marginBottom: 6 }}>
          {busy ? 'Searching…' : `${total} match${total === 1 ? '' : 'es'}${
            total > results.length ? ` — showing ${results.length}` : ''}`}
        </div>
      )}

      <div style={{ maxHeight: 260, overflowY: 'auto', margin: '0 -4px' }}>
        {!q && <Empty>Type a route number (e.g. 764) or a destination.</Empty>}
        {q && !busy && !results.length && <Empty>No routes matched “{q}”.</Empty>}

        {results.map((r) => {
          const live = liveCounts.get(r.route_id) ?? 0
          const isSel = r.route_id === selected
          return (
            <button
              key={r.route_id}
              onClick={() => onSelect(isSel ? null : r.route_id)}
              style={{
                display: 'block', width: '100%', textAlign: 'left', cursor: 'pointer',
                background: isSel ? 'var(--surface-3)' : 'transparent',
                border: '1px solid ' + (isSel ? 'var(--series-2)' : 'transparent'),
                borderRadius: 'var(--radius-sm)', padding: '7px 8px', marginBottom: 2,
              }}
              onMouseEnter={(e) => {
                if (!isSel) e.currentTarget.style.background = 'var(--surface-2)'
              }}
              onMouseLeave={(e) => {
                if (!isSel) e.currentTarget.style.background = 'transparent'
              }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                <span style={{ fontWeight: 600, fontSize: 12.5 }}>
                  {r.route_short_name || r.route_id}
                </span>
                {live > 0 && (
                  <span className="num" style={{
                    fontSize: 10, background: 'rgba(57,135,229,0.16)', color: 'var(--series-1)',
                    border: '1px solid rgba(57,135,229,0.4)', borderRadius: 10,
                    padding: '1px 6px', whiteSpace: 'nowrap',
                  }}>{live} live</span>
                )}
                <span className="num" style={{
                  marginLeft: 'auto', fontSize: 10, color: 'var(--text-muted)',
                }}>{r.trip_count} trips</span>
              </div>
              {r.route_desc && (
                <div style={{
                  fontSize: 11, color: 'var(--text-muted)', marginTop: 2,
                  overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                }}>{r.route_desc}</div>
              )}
            </button>
          )
        })}
      </div>
    </div>
  )
}
