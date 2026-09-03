import { useEffect, useRef, useState } from 'react'
import type { FeedStatus, Vehicle } from './types'

/**
 * Wire format. The service tier sends route metadata once per frame in
 * `routes` instead of repeating it on every vehicle, and omits the fields this
 * feed never populates - that is the difference between a ~2.0 MB and a
 * ~0.8 MB frame at ~5k vehicles. We rehydrate to the full `Vehicle` shape here
 * so the rest of the app sees one consistent type.
 */
interface WireVehicle {
  vehicle_id: string
  ts: number
  route_id: string | null
  trip_id: string | null
  lat: number | null
  lon: number | null
  bearing: number | null
  speed: number | null
}

interface RouteMeta { name: string | null; desc: string | null; agency: string | null }
type RouteTable = Record<string, RouteMeta>

type Frame =
  | { type: 'snapshot'; status: FeedStatus; routes: RouteTable; vehicles: WireVehicle[] }
  | { type: 'vehicles'; ts: number; count: number; status: FeedStatus
      routes: RouteTable; vehicles: WireVehicle[] }
  | { type: 'ping' }

function rehydrate(w: WireVehicle, routes: RouteTable): Vehicle {
  const meta = (w.route_id ? routes[w.route_id] : undefined) ?? null
  return {
    ...w,
    stop_id: null,
    current_status: null,
    occupancy_status: null,
    ingested_at: w.ts,
    route_name: meta?.name ?? null,
    route_desc: meta?.desc ?? null,
    agency_id: meta?.agency ?? null,
  }
}

export type Connection = 'connecting' | 'live' | 'reconnecting' | 'offline'

/**
 * Subscribes to the service tier's vehicle stream.
 *
 * The map needs whole-fleet state, and each poll pushes every vehicle it saw,
 * so frames are merged into a keyed map rather than replacing it - a vehicle
 * missing from one poll keeps its last known position until it ages out.
 */
export function useLiveVehicles() {
  const [vehicles, setVehicles] = useState<Map<string, Vehicle>>(new Map())
  const [status, setStatus] = useState<FeedStatus | null>(null)
  const [connection, setConnection] = useState<Connection>('connecting')
  const [lastFrameAt, setLastFrameAt] = useState<number | null>(null)
  const wsRef = useRef<WebSocket | null>(null)
  const retryRef = useRef(0)
  const closedRef = useRef(false)

  useEffect(() => {
    closedRef.current = false
    let timer: ReturnType<typeof setTimeout>

    const merge = (incoming: WireVehicle[], routes: RouteTable) => {
      if (!incoming.length) return
      setVehicles((prev) => {
        const next = new Map(prev)
        for (const w of incoming) {
          if (w.lat != null && w.lon != null) next.set(w.vehicle_id, rehydrate(w, routes))
        }
        return next
      })
      setLastFrameAt(Date.now())
    }

    const connect = () => {
      if (closedRef.current) return
      const proto = window.location.protocol === 'https:' ? 'wss' : 'ws'
      const ws = new WebSocket(`${proto}://${window.location.host}/ws/vehicles`)
      wsRef.current = ws

      ws.onopen = () => { retryRef.current = 0; setConnection('live') }

      ws.onmessage = (ev) => {
        const frame: Frame = JSON.parse(ev.data)
        if (frame.type === 'ping') return
        setStatus(frame.status)
        merge(frame.vehicles, frame.routes ?? {})
      }

      ws.onclose = () => {
        if (closedRef.current) return
        setConnection('reconnecting')
        // Back off to 15s so a stopped backend does not spin the browser.
        const delay = Math.min(15000, 1000 * 2 ** retryRef.current++)
        timer = setTimeout(connect, delay)
      }

      ws.onerror = () => ws.close()
    }

    connect()
    return () => {
      closedRef.current = true
      clearTimeout(timer)
      wsRef.current?.close()
    }
  }, [])

  return { vehicles, status, connection, lastFrameAt }
}

/** Poll a JSON endpoint on an interval. Used for the analytics panels, which
 *  do not need push-level freshness. */
export function usePolled<T>(fn: () => Promise<T>, intervalMs: number, deps: unknown[] = []) {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)
  const fnRef = useRef(fn)
  fnRef.current = fn

  useEffect(() => {
    let alive = true
    const run = () => {
      fnRef.current()
        .then((d) => { if (alive) { setData(d); setError(null) } })
        .catch((e) => { if (alive) setError(String(e)) })
    }
    run()
    const id = setInterval(run, intervalMs)
    return () => { alive = false; clearInterval(id) }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [intervalMs, ...deps])

  return { data, error }
}
