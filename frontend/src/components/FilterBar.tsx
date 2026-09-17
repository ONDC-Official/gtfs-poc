import type { ReactNode } from 'react'
import type { Agency, Filters, Freshness, Motion } from '../filters'
import { activeCount, EMPTY_FILTERS } from '../filters'
import { InfoTip } from './InfoTip'
import type { TermId } from '../glossary'

const chipBase: React.CSSProperties = {
  border: '1px solid var(--border)',
  background: 'var(--surface-2)',
  color: 'var(--text-secondary)',
  borderRadius: 999,
  padding: '4px 10px',
  fontSize: 11.5,
  cursor: 'pointer',
  whiteSpace: 'nowrap',
  lineHeight: 1.4,
  transition: 'background 120ms, border-color 120ms, color 120ms',
}

function Chip({ on, onClick, children, tone = 'default', term }: {
  on: boolean; onClick: () => void; children: ReactNode
  tone?: 'default' | 'warn'
  /** Glossary entry, shown on hover so the rule behind the chip is visible. */
  term?: TermId
}) {
  const accent = tone === 'warn' ? 'var(--warning)' : 'var(--series-1)'
  const body = term ? <InfoTip term={term}>{children}</InfoTip> : children
  return (
    <button
      type="button" onClick={onClick} aria-pressed={on}
      style={{
        ...chipBase,
        borderColor: on ? accent : 'var(--border)',
        background: on
          ? (tone === 'warn' ? 'rgba(250,178,25,0.14)' : 'rgba(57,135,229,0.16)')
          : 'var(--surface-2)',
        color: on ? accent : 'var(--text-secondary)',
        fontWeight: on ? 600 : 400,
      }}>
      {body}
    </button>
  )
}

function Group({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
      <span style={{
        fontSize: 10, textTransform: 'uppercase', letterSpacing: '0.06em',
        color: 'var(--text-muted)', whiteSpace: 'nowrap',
      }}>{label}</span>
      <div style={{ display: 'flex', gap: 4 }}>{children}</div>
    </div>
  )
}

interface Props {
  filters: Filters
  onChange: (f: Filters) => void
  shown: number
  total: number
  routeLabel: string | null
}

export function FilterBar({ filters, onChange, shown, total, routeLabel }: Props) {
  const set = <K extends keyof Filters>(k: K, v: Filters[K]) =>
    onChange({ ...filters, [k]: v })
  // Clicking the active option in a group turns that group back off.
  const cycle = <K extends keyof Filters>(k: K, v: Filters[K]) =>
    set(k, (filters[k] === v ? 'all' : v) as Filters[K])

  const n = activeCount(filters)

  return (
    <div style={{
      display: 'flex', alignItems: 'center', gap: 14, flexWrap: 'wrap',
      padding: '8px 14px', borderBottom: '1px solid var(--border)',
      background: 'var(--surface-1)',
    }}>
      <Group label="Operator">
        {(['DTC', 'DOT'] as Agency[]).map((a) => (
          <Chip key={a} on={filters.agency === a} onClick={() => cycle('agency', a)}>{a}</Chip>
        ))}
      </Group>

      <Group label="Motion">
        {(['moving', 'stopped'] as Motion[]).map((m) => (
          <Chip key={m} on={filters.motion === m} onClick={() => cycle('motion', m)}
                term={m === 'moving' ? 'moving' : 'stopped'}>
            {m === 'moving' ? 'Moving' : 'Stopped'}
          </Chip>
        ))}
      </Group>

      <Group label="Reporting">
        {(['fresh', 'stale'] as Freshness[]).map((fr) => (
          <Chip key={fr} on={filters.freshness === fr} tone={fr === 'stale' ? 'warn' : 'default'}
                onClick={() => cycle('freshness', fr)}
                term={fr === 'fresh' ? 'fresh' : 'stale'}>
            {fr === 'fresh' ? 'Fresh' : 'Stale'}
          </Chip>
        ))}
      </Group>

      <Group label="Issues">
        <Chip on={filters.bunchedOnly} tone="warn" term="bunched"
              onClick={() => set('bunchedOnly', !filters.bunchedOnly)}>
          Bunched
        </Chip>
        <Chip on={filters.slowOnly} tone="warn" term="crawling"
              onClick={() => set('slowOnly', !filters.slowOnly)}>
          Crawling
        </Chip>
      </Group>

      {filters.routeId && (
        <Chip on onClick={() => set('routeId', null)}>
          Route {routeLabel ?? filters.routeId} ✕
        </Chip>
      )}

      <div style={{
        marginLeft: 'auto', display: 'flex', alignItems: 'center', gap: 10,
        fontSize: 12, color: 'var(--text-secondary)',
      }}>
        <span className="num">
          {n > 0
            ? <><strong style={{ color: 'var(--text-primary)' }}>{shown.toLocaleString()}</strong>
                {' '}of {total.toLocaleString()} shown</>
            : <><strong style={{ color: 'var(--text-primary)' }}>{total.toLocaleString()}</strong>
                {' '}vehicles</>}
        </span>
        {n > 0 && (
          <button
            type="button" onClick={() => onChange({ ...EMPTY_FILTERS, q: filters.q })}
            style={{ ...chipBase, background: 'transparent', color: 'var(--text-muted)' }}>
            Reset {n} filter{n === 1 ? '' : 's'}
          </button>
        )}
      </div>
    </div>
  )
}
