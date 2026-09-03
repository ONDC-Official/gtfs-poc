import type { ReactNode } from 'react'
import { InfoTip } from './InfoTip'
import type { TermId } from '../glossary'

export function Panel({ title, action, children, pad = true }: {
  title?: string; action?: ReactNode; children: ReactNode; pad?: boolean
}) {
  return (
    <section style={{
      background: 'var(--surface-1)',
      border: '1px solid var(--border)',
      borderRadius: 'var(--radius)',
      overflow: 'hidden',
      // The sidebar is a flex column; without this, tall panels get squeezed
      // and their charts clip instead of the column scrolling.
      flexShrink: 0,
    }}>
      {title && (
        <header style={{
          display: 'flex', alignItems: 'center', justifyContent: 'space-between',
          gap: 8, padding: '10px 14px', borderBottom: '1px solid var(--border)',
        }}>
          <h2 style={{
            margin: 0, fontSize: 11, fontWeight: 600, letterSpacing: '0.07em',
            textTransform: 'uppercase', color: 'var(--text-secondary)',
          }}>{title}</h2>
          {action}
        </header>
      )}
      <div style={{ padding: pad ? 14 : 0 }}>{children}</div>
    </section>
  )
}

/** A hero number. No plot, so no hover layer - the value is the whole message. */
export function StatTile({ label, value, unit, hint, tone = 'default', term }: {
  label: string
  value: ReactNode
  unit?: string
  hint?: string
  tone?: 'default' | 'good' | 'warning' | 'critical'
  /** Glossary entry explaining what this number counts. */
  term?: TermId
}) {
  const toneColor = {
    default: 'var(--text-primary)',
    good: 'var(--good)',
    warning: 'var(--warning)',
    critical: 'var(--critical)',
  }[tone]

  return (
    <div style={{
      background: 'var(--surface-2)',
      border: '1px solid var(--border)',
      borderRadius: 'var(--radius-sm)',
      padding: '10px 12px',
      minWidth: 0,
    }}>
      <div style={{
        fontSize: 10, letterSpacing: '0.06em', textTransform: 'uppercase',
        color: 'var(--text-muted)', whiteSpace: 'nowrap',
      }}>
        {term ? <InfoTip term={term}>{label}</InfoTip> : label}
      </div>
      <div className="num" style={{
        fontSize: 24, fontWeight: 650, lineHeight: 1.15, marginTop: 4, color: toneColor,
      }}>
        {value}
        {unit && <span style={{
          fontSize: 12, fontWeight: 500, color: 'var(--text-muted)', marginLeft: 3,
        }}>{unit}</span>}
      </div>
      {hint && <div style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 2 }}>{hint}</div>}
    </div>
  )
}

export function Dot({ color, size = 8, ring }: {
  color: string; size?: number; ring?: string
}) {
  return <span style={{
    display: 'inline-block', width: size, height: size, borderRadius: '50%',
    background: color, flexShrink: 0,
    boxShadow: ring ? `0 0 0 2px ${ring}` : undefined,
    margin: ring ? 2 : undefined,
  }} />
}

export function Empty({ children }: { children: ReactNode }) {
  return <div style={{
    padding: '24px 12px', textAlign: 'center', fontSize: 12, color: 'var(--text-muted)',
  }}>{children}</div>
}

/**
 * A segmented meter: proportions of a whole, in one bar. Preferred over a pie
 * for two-to-four parts - easier to compare and it fits a sidebar. Segments
 * carry a 2px surface gap so adjacent fills stay separable, and every segment
 * is labelled, so colour never carries meaning alone.
 */
export function Meter({ segments, total }: {
  segments: { label: string; value: number; color: string; term?: TermId }[]
  total: number
}) {
  const safe = Math.max(total, 1)
  return (
    <div>
      <div style={{ display: 'flex', gap: 2, height: 10, marginBottom: 10 }}>
        {segments.filter((s) => s.value > 0).map((s) => (
          <div key={s.label} title={`${s.label}: ${s.value.toLocaleString()}`}
               style={{
                 width: `${(s.value / safe) * 100}%`, background: s.color,
                 borderRadius: 3, minWidth: 3,
               }} />
        ))}
      </div>
      <div style={{ display: 'grid', gap: 6 }}>
        {segments.map((s) => (
          <div key={s.label} style={{
            display: 'flex', alignItems: 'center', gap: 8, fontSize: 12,
          }}>
            <Dot color={s.color} size={7} />
            <span style={{ color: 'var(--text-secondary)' }}>
              {s.term ? <InfoTip term={s.term}>{s.label}</InfoTip> : s.label}
            </span>
            <span className="num" style={{ marginLeft: 'auto' }}>
              {s.value.toLocaleString()}
            </span>
            <span className="num" style={{
              color: 'var(--text-muted)', width: 42, textAlign: 'right',
            }}>{((s.value / safe) * 100).toFixed(1)}%</span>
          </div>
        ))}
      </div>
    </div>
  )
}

/** Label/value rows - the shape most of these panels reduce to. */
export function Rows({ items, terms }: {
  items: [string, ReactNode][]
  /** Optional label -> glossary entry, for rows worth explaining. */
  terms?: Partial<Record<string, TermId>>
}) {
  return (
    <div style={{ display: 'grid', gap: 7, fontSize: 12 }}>
      {items.map(([k, v]) => (
        <div key={k} style={{ display: 'flex', justifyContent: 'space-between', gap: 10 }}>
          <span style={{ color: 'var(--text-muted)' }}>
            {terms?.[k] ? <InfoTip term={terms[k]!}>{k}</InfoTip> : k}
          </span>
          <span className="num" style={{ textAlign: 'right' }}>{v}</span>
        </div>
      ))}
    </div>
  )
}

export function Tabs<T extends string>({ value, options, onChange }: {
  value: T
  options: { id: T; label: string; term?: TermId }[]
  onChange: (v: T) => void
}) {
  return (
    <div role="tablist" style={{
      display: 'flex', gap: 2, padding: 3, background: 'var(--surface-1)',
      border: '1px solid var(--border)', borderRadius: 'var(--radius-sm)',
      flexShrink: 0,
    }}>
      {options.map((o) => {
        const on = o.id === value
        return (
          <button key={o.id} role="tab" aria-selected={on} onClick={() => onChange(o.id)}
            style={{
              flex: 1, padding: '6px 8px', fontSize: 12,
              fontWeight: on ? 600 : 500, cursor: 'pointer',
              border: 'none', borderRadius: 4,
              background: on ? 'var(--surface-3)' : 'transparent',
              color: on ? 'var(--text-primary)' : 'var(--text-muted)',
            }}>{o.term ? <InfoTip term={o.term}>{o.label}</InfoTip> : o.label}</button>
        )
      })}
    </div>
  )
}
