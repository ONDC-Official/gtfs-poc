/** "12s ago" / "4m ago" / "2h ago". Shared so every panel phrases age the same. */
export function ago(ts: number | null | undefined): string {
  if (!ts) return '—'
  const s = Math.max(0, Math.round(Date.now() / 1000 - ts))
  if (s < 60) return `${s}s ago`
  if (s < 3600) return `${Math.round(s / 60)}m ago`
  return `${Math.round(s / 3600)}h ago`
}

/** Hour-of-day label for a unix timestamp, in the viewer's timezone. */
export function hourLabel(ts: number): string {
  return new Date(ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}
