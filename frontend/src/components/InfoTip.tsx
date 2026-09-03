import { useCallback, useId, useLayoutEffect, useRef, useState, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { GLOSSARY, type Term, type TermId } from '../glossary'

const WIDTH = 280
const GAP = 8
const MARGIN = 8

interface Pos { left: number; top: number }

/**
 * Hover/focus explainer for a derived term.
 *
 * Wraps the label itself rather than adding a separate "?" icon, so the thing
 * you want explained is the thing you point at.
 *
 * Rendered through a portal with fixed positioning: panels use
 * `overflow: hidden` for their rounded headers and the sidebar scrolls, so an
 * absolutely-positioned tooltip gets clipped to a sliver by one or the other.
 * Escaping to the body is the only reliable way out of both.
 */
export function InfoTip({ term, children }: { term: TermId; children: ReactNode }) {
  const [pos, setPos] = useState<Pos | null>(null)
  const anchor = useRef<HTMLSpanElement>(null)
  const id = useId()
  // Widen from the `satisfies` literal type: not every entry has a rule.
  const t: Term = GLOSSARY[term]

  const place = useCallback(() => {
    const el = anchor.current
    if (!el) return
    const r = el.getBoundingClientRect()
    // Prefer below; flip above when there is not enough room.
    const below = r.bottom + GAP
    const wantsAbove = below + 150 > window.innerHeight && r.top > 150
    setPos({
      left: Math.min(Math.max(MARGIN, r.left), window.innerWidth - WIDTH - MARGIN),
      top: wantsAbove ? r.top - GAP : below,
    })
  }, [])

  const open = useCallback(() => { place() }, [place])
  const close = useCallback(() => setPos(null), [])

  // Reposition if the page scrolls or resizes while the tip is up.
  useLayoutEffect(() => {
    if (!pos) return
    const onMove = () => place()
    window.addEventListener('scroll', onMove, true)
    window.addEventListener('resize', onMove)
    return () => {
      window.removeEventListener('scroll', onMove, true)
      window.removeEventListener('resize', onMove)
    }
  }, [pos, place])

  const flipped = pos != null && anchor.current != null
    && pos.top < anchor.current.getBoundingClientRect().top

  return (
    <>
      <span
        ref={anchor}
        tabIndex={0}
        aria-describedby={pos ? id : undefined}
        onMouseEnter={open}
        onMouseLeave={close}
        onFocus={open}
        onBlur={close}
        style={{
          cursor: 'help',
          // A dotted underline is the long-standing convention for "there is a
          // definition here", and costs no layout space.
          borderBottom: '1px dotted var(--border-strong)',
          outlineOffset: 2,
        }}
      >{children}</span>

      {pos && createPortal(
        <div
          role="tooltip"
          id={id}
          style={{
            position: 'fixed',
            left: pos.left,
            top: pos.top,
            transform: flipped ? 'translateY(-100%)' : undefined,
            zIndex: 1000,
            width: WIDTH,
            background: 'var(--surface-2)',
            border: '1px solid var(--border-strong)',
            borderRadius: 'var(--radius-sm)',
            padding: '9px 11px',
            boxShadow: '0 10px 30px rgba(0,0,0,0.55)',
            pointerEvents: 'none',
            textAlign: 'left',
          }}
        >
          <div style={{
            fontSize: 12, fontWeight: 650, color: 'var(--text-primary)',
            marginBottom: 4, letterSpacing: 0, textTransform: 'none',
          }}>{t.title}</div>
          <div style={{
            fontSize: 11.5, lineHeight: 1.55, color: 'var(--text-secondary)',
            fontWeight: 400, letterSpacing: 0, textTransform: 'none',
          }}>{t.body}</div>
          {t.rule && (
            <div style={{
              marginTop: 6, paddingTop: 6, borderTop: '1px solid var(--border)',
              fontSize: 10.5, color: 'var(--text-muted)',
              fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
              letterSpacing: 0, textTransform: 'none',
            }}>{t.rule}</div>
          )}
        </div>,
        document.body)}
    </>
  )
}
