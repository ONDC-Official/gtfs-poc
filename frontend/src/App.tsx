import { useState } from 'react'
import { useLiveVehicles } from './useLiveVehicles'
import { LiveView } from './views/LiveView'
import { AnalyticsView } from './views/AnalyticsView'
import { QualityView } from './views/QualityView'
import { Dot } from './components/ui'
import { ago } from './format'

type ViewId = 'live' | 'analytics' | 'quality'

const VIEWS: { id: ViewId; label: string; hint: string }[] = [
  { id: 'live', label: 'Live', hint: 'Where every bus is right now' },
  { id: 'analytics', label: 'Analytics', hint: 'Patterns over the recorded history' },
  { id: 'quality', label: 'Quality', hint: 'Is this feed trustworthy?' },
]

export default function App() {
  const [view, setView] = useState<ViewId>('live')
  // The websocket lives in the shell, so switching views does not drop the
  // connection and re-download the fleet.
  const { vehicles, status, connection, lastFrameAt } = useLiveVehicles()

  const isLive = connection === 'live'
  const isMock = status?.source === 'mock'

  return (
    <div style={{
      display: 'grid', gridTemplateRows: 'auto 1fr', height: '100%', overflow: 'hidden',
    }}>
      <header style={{
        display: 'flex', alignItems: 'center', gap: 18, padding: '9px 16px',
        borderBottom: '1px solid var(--border)', background: 'var(--surface-1)',
      }}>
        <h1 style={{ margin: 0, fontSize: 15, fontWeight: 650, letterSpacing: '-0.01em' }}>
          Delhi Bus Network
        </h1>

        <nav role="tablist" style={{
          display: 'flex', gap: 2, padding: 3, background: 'var(--surface-2)',
          border: '1px solid var(--border)', borderRadius: 'var(--radius-sm)',
        }}>
          {VIEWS.map((v) => {
            const on = v.id === view
            return (
              <button key={v.id} role="tab" aria-selected={on} title={v.hint}
                onClick={() => setView(v.id)}
                style={{
                  border: 'none', cursor: 'pointer', borderRadius: 4,
                  padding: '5px 14px', fontSize: 12.5,
                  fontWeight: on ? 600 : 500,
                  background: on ? 'var(--surface-3)' : 'transparent',
                  color: on ? 'var(--text-primary)' : 'var(--text-muted)',
                }}>{v.label}</button>
            )
          })}
        </nav>

        <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginLeft: 'auto' }}>
          {isMock && (
            <span style={{
              fontSize: 10.5, fontWeight: 600, color: 'var(--warning)',
              background: 'rgba(250,178,25,0.12)', border: '1px solid rgba(250,178,25,0.4)',
              borderRadius: 12, padding: '3px 9px',
            }}>▲ SIMULATED FEED — no API key set</span>
          )}
          <span style={{
            display: 'flex', alignItems: 'center', gap: 7, fontSize: 11.5,
            color: 'var(--text-secondary)', background: 'var(--surface-2)',
            border: '1px solid var(--border)', borderRadius: 12, padding: '3px 10px',
          }}>
            <Dot color={isLive ? 'var(--good)' : 'var(--critical)'} />
            {isLive ? 'Connected'
              : connection === 'reconnecting' ? 'Reconnecting…' : 'Connecting…'}
            <span style={{ color: 'var(--border-strong)' }}>|</span>
            <span className="num">{ago(lastFrameAt ? lastFrameAt / 1000 : null)}</span>
          </span>
        </div>
      </header>

      {/* Both views stay mounted: the map is expensive to build, and the live
          view's filters should survive a trip to Analytics. */}
      <div style={{ display: view === 'live' ? 'grid' : 'none', minHeight: 0 }}>
        <LiveView vehicles={vehicles} status={status} />
      </div>
      <div style={{ display: view === 'analytics' ? 'grid' : 'none', minHeight: 0 }}>
        <AnalyticsView />
      </div>
      <div style={{ display: view === 'quality' ? 'grid' : 'none', minHeight: 0 }}>
        <QualityView />
      </div>
    </div>
  )
}
