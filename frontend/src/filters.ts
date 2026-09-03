import type { Vehicle } from './types'

export type Motion = 'all' | 'moving' | 'stopped'
export type Freshness = 'all' | 'fresh' | 'stale'
export type Agency = 'all' | 'DTC' | 'DOT'

export interface Filters {
  q: string
  routeId: string | null
  agency: Agency
  motion: Motion
  freshness: Freshness
  bunchedOnly: boolean
  slowOnly: boolean
}

export const EMPTY_FILTERS: Filters = {
  q: '', routeId: null, agency: 'all', motion: 'all',
  freshness: 'all', bunchedOnly: false, slowOnly: false,
}

// Thresholds mirror the service tier (app/services/live_analytics.py) so the
// filtered counts on screen agree with the analytics panels.
export const MOVING_MPS = 0.5
export const FRESH_S = 60
export const STALE_S = 300
export const IMPLAUSIBLE_MPS = 20
export const SLOW_KMH = 10
export const BUNCH_M = 300

export function isActive(f: Filters): boolean {
  return f.routeId !== null || f.agency !== 'all' || f.motion !== 'all' ||
    f.freshness !== 'all' || f.bunchedOnly || f.slowOnly
}

export function activeCount(f: Filters): number {
  return [f.routeId !== null, f.agency !== 'all', f.motion !== 'all',
    f.freshness !== 'all', f.bunchedOnly, f.slowOnly].filter(Boolean).length
}

/**
 * Vehicles on the same route sitting within BUNCH_M of each other.
 *
 * Grid-hashed into BUNCH_M cells, so this stays linear in fleet size rather
 * than comparing every pair on a busy route - it runs on every frame for ~6k
 * vehicles and must not block the map.
 */
export function findBunched(vehicles: Vehicle[]): Set<string> {
  const deg = BUNCH_M / 111000
  const byRoute = new Map<string, Vehicle[]>()
  for (const v of vehicles) {
    if (!v.route_id || v.lat == null || v.lon == null) continue
    const list = byRoute.get(v.route_id)
    if (list) list.push(v)
    else byRoute.set(v.route_id, [v])
  }

  const bunched = new Set<string>()
  for (const list of byRoute.values()) {
    if (list.length < 2) continue
    const cells = new Map<string, Vehicle[]>()
    for (const v of list) {
      const key = `${Math.floor((v.lat as number) / deg)}:${Math.floor((v.lon as number) / deg)}`
      const c = cells.get(key)
      if (c) c.push(v)
      else cells.set(key, [v])
    }
    for (const [key, members] of cells) {
      const [cy, cx] = key.split(':').map(Number)
      const near: Vehicle[] = []
      for (let dy = -1; dy <= 1; dy++) {
        for (let dx = -1; dx <= 1; dx++) {
          const n = cells.get(`${cy + dy}:${cx + dx}`)
          if (n) near.push(...n)
        }
      }
      for (const a of members) {
        for (const b of near) {
          if (a.vehicle_id === b.vehicle_id) continue
          if (bunched.has(a.vehicle_id) && bunched.has(b.vehicle_id)) continue
          const mlat = (((a.lat as number) + (b.lat as number)) / 2) * Math.PI / 180
          const y = ((b.lat as number) - (a.lat as number)) * 111320
          const x = ((b.lon as number) - (a.lon as number)) * 111320 * Math.cos(mlat)
          if (Math.hypot(x, y) < BUNCH_M) { bunched.add(a.vehicle_id); bunched.add(b.vehicle_id) }
        }
      }
    }
  }
  return bunched
}

export function applyFilters(
  vehicles: Vehicle[], f: Filters, bunched: Set<string>, nowS: number,
): Vehicle[] {
  if (!isActive(f)) return vehicles
  return vehicles.filter((v) => {
    if (f.routeId && v.route_id !== f.routeId) return false
    if (f.agency !== 'all' && v.agency_id !== f.agency) return false

    const age = nowS - v.ts
    if (f.freshness === 'fresh' && age > FRESH_S) return false
    if (f.freshness === 'stale' && age <= STALE_S) return false

    const speed = v.speed ?? 0
    const moving = speed > MOVING_MPS
    if (f.motion === 'moving' && !moving) return false
    if (f.motion === 'stopped' && moving) return false

    // "Slow" means running but crawling - a parked bus is not congestion.
    if (f.slowOnly && !(moving && speed * 3.6 < SLOW_KMH)) return false
    if (f.bunchedOnly && !bunched.has(v.vehicle_id)) return false
    return true
  })
}
