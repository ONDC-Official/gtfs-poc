import { useEffect, useRef, useState } from 'react'
import {
  GeoJSONSource, LngLatBounds, MapLibreMap, NavigationControl, Popup, ScaleControl,
  setWorkerUrl, type MapLayerMouseEvent, type MapOptions,
} from 'maplibre-gl'
import 'maplibre-gl/dist/maplibre-gl.css'
// Point MapLibre at a worker the bundler actually emits. Without this the
// production build 404s on the worker and every GeoJSON layer silently stays
// empty while the basemap renders normally. See vite.config.ts.
import maplibreWorkerUrl from 'maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url'

setWorkerUrl(maplibreWorkerUrl)
import type { RouteStop, Vehicle } from '../types'

const DELHI: [number, number] = [77.216, 28.62]

// OpenStreetMap raster, darkened in the paint layer. Keyless by design: CARTO's
// dark tiles now watermark unauthenticated requests, and a key is one more thing
// to provision before the console works.
const STYLE: MapOptions['style'] = {
  version: 8,
  sources: {
    osm: {
      type: 'raster',
      tiles: [
        'https://a.tile.openstreetmap.org/{z}/{x}/{y}.png',
        'https://b.tile.openstreetmap.org/{z}/{x}/{y}.png',
        'https://c.tile.openstreetmap.org/{z}/{x}/{y}.png',
      ],
      tileSize: 256,
      maxzoom: 19,
      attribution: '© OpenStreetMap contributors',
    },
  },
  layers: [
    { id: 'bg', type: 'background', paint: { 'background-color': '#121211' } },
    {
      id: 'basemap',
      type: 'raster',
      source: 'osm',
      paint: {
        // Desaturated and dimmed so the buses stay the brightest thing on
        // screen - but light enough that streets and place names still read
        // as a map rather than a black field.
        'raster-saturation': -0.85,
        'raster-brightness-min': 0.02,
        'raster-brightness-max': 0.42,
        'raster-contrast': -0.12,
        'raster-opacity': 0.92,
      },
    },
  ],
}

const EMPTY: GeoJSON.FeatureCollection = { type: 'FeatureCollection', features: [] }

interface Props {
  vehicles: Vehicle[]
  selectedRoute: string | null
  routeShape: GeoJSON.Feature<GeoJSON.LineString> | null
  routeStops: RouteStop[]
  bunched: Set<string>
  onSelectVehicle: (v: Vehicle | null) => void
}

export function MapView({
  vehicles, selectedRoute, routeShape, routeStops, bunched, onSelectVehicle,
}: Props) {
  const container = useRef<HTMLDivElement>(null)
  const map = useRef<MapLibreMap | null>(null)
  const ready = useRef(false)
  const popup = useRef<Popup | null>(null)
  // MapLibre needs WebGL2. If it is unavailable the map cannot render, but the
  // rest of the console still can - so failure is state, not an exception.
  const [failed, setFailed] = useState<string | null>(null)
  // State, not a ref: the data effects below must re-run once layers exist,
  // otherwise a frame that arrived before 'load' is dropped for good.
  const [mapReady, setMapReady] = useState(false)

  // ---- one-time map construction -----------------------------------------
  useEffect(() => {
    if (!container.current || map.current) return
    let m: MapLibreMap
    try {
      m = new MapLibreMap({
        container: container.current,
        style: STYLE,
        center: DELHI,
        zoom: 10.2,
        attributionControl: { compact: true },
      })
    } catch (err) {
      setFailed(err instanceof Error ? err.message : String(err))
      return
    }
    map.current = m
    m.on('error', (e) => console.error('[maplibre]', e.error?.message ?? e))
    m.addControl(new NavigationControl({ showCompass: false }), 'top-right')
    m.addControl(new ScaleControl({ unit: 'metric' }), 'bottom-left')

    m.on('load', () => {
      m.addSource('route', { type: 'geojson', data: EMPTY })
      m.addSource('stops', { type: 'geojson', data: EMPTY })
      m.addSource('vehicles', { type: 'geojson', data: EMPTY })

      // Selected corridor, drawn under everything else.
      m.addLayer({
        id: 'route-casing', type: 'line', source: 'route',
        layout: { 'line-cap': 'round', 'line-join': 'round' },
        paint: { 'line-color': '#0d0d0c', 'line-width': 7, 'line-opacity': 0.85 },
      })
      m.addLayer({
        id: 'route-line', type: 'line', source: 'route',
        layout: { 'line-cap': 'round', 'line-join': 'round' },
        paint: { 'line-color': '#199e70', 'line-width': 3 },
      })
      m.addLayer({
        id: 'stops-dot', type: 'circle', source: 'stops',
        paint: {
          'circle-radius': ['interpolate', ['linear'], ['zoom'], 10, 2.5, 14, 5],
          'circle-color': '#1a1a19',
          'circle-stroke-color': '#199e70',
          'circle-stroke-width': 1.5,
        },
      })

      // Vehicles. A 2px surface ring keeps overlapping buses separable, which
      // matters at city zoom where hundreds of markers pile up.
      m.addLayer({
        id: 'vehicles-dot', type: 'circle', source: 'vehicles',
        paint: {
          'circle-radius': ['interpolate', ['linear'], ['zoom'], 9, 3.5, 12, 5.5, 15, 9],
          // Fill carries motion state; the ring carries bunching. Two
          // channels for two dimensions, so neither hides the other.
          'circle-color': [
            'case',
            ['==', ['get', 'selected'], true], '#d95926',
            ['==', ['get', 'stopped'], true], '#8a8980',
            '#3987e5',
          ],
          'circle-stroke-color': [
            'case', ['==', ['get', 'bunched'], true], '#fab219', '#1a1a19',
          ],
          'circle-stroke-width': [
            'case', ['==', ['get', 'bunched'], true], 2.2, 2,
          ],
          'circle-opacity': 0.95,
        },
      })
      // Bearing arrow, only once zoomed in far enough to read it.
      m.addLayer({
        id: 'vehicles-bearing', type: 'symbol', source: 'vehicles',
        minzoom: 12.5,
        filter: ['!=', ['get', 'stopped'], true],
        layout: {
          'text-field': '▲',
          'text-size': 9,
          'text-rotate': ['get', 'bearing'],
          'text-rotation-alignment': 'map',
          'text-offset': [0, -1.35],
          'text-allow-overlap': true,
          'text-ignore-placement': true,
        },
        paint: { 'text-color': '#c3c2b7', 'text-halo-color': '#1a1a19', 'text-halo-width': 1 },
      })

      ready.current = true
      setMapReady(true)
      m.getCanvas().style.cursor = ''
    })

    const showPopup = (e: MapLayerMouseEvent) => {
      const f = e.features?.[0]
      if (!f) return
      const p = f.properties as Record<string, string>
      popup.current?.remove()
      popup.current = new Popup({ closeButton: true, offset: 12 })
        .setLngLat((f.geometry as GeoJSON.Point).coordinates as [number, number])
        .setHTML(
          `<div style="font-weight:600;margin-bottom:4px">${p.route_name || 'Unknown route'}</div>` +
          `<div style="color:#c3c2b7;line-height:1.5">` +
          `${p.route_desc ? `${p.route_desc}<br/>` : ''}` +
          `Vehicle <span style="font-variant-numeric:tabular-nums">${p.vehicle_id}</span><br/>` +
          `Speed <span style="font-variant-numeric:tabular-nums">${p.speed_kmh}</span> km/h<br/>` +
          `Operator ${p.agency_id || '—'}<br/>` +
          `Updated <span style="font-variant-numeric:tabular-nums">${p.age}</span>s ago` +
          `${p.bunched === 'true' ? '<br/><span style="color:#fab219">▲ Bunched with another bus on this route</span>' : ''}` +
          `</div>`)
        .addTo(m)
      onSelectVehicle({ vehicle_id: p.vehicle_id, route_id: p.route_id } as Vehicle)
    }

    m.on('click', 'vehicles-dot', showPopup)
    m.on('mouseenter', 'vehicles-dot', () => { m.getCanvas().style.cursor = 'pointer' })
    m.on('mouseleave', 'vehicles-dot', () => { m.getCanvas().style.cursor = '' })

    return () => {
      m.remove(); map.current = null; ready.current = false; setMapReady(false)
    }
  }, [onSelectVehicle])


  // A map built inside a hidden container initialises at 0x0 and stays that
  // size when revealed, so watch the container rather than the window - this
  // covers view switching and ordinary window resizing alike.
  useEffect(() => {
    const el = container.current
    const m = map.current
    if (!el || !m) return
    const ro = new ResizeObserver(() => {
      if (el.clientWidth > 0 && el.clientHeight > 0) m.resize()
    })
    ro.observe(el)
    return () => ro.disconnect()
  }, [mapReady])

  // ---- vehicle updates ----------------------------------------------------
  useEffect(() => {
    const m = map.current
    if (!m || !ready.current) return
    const src = m.getSource('vehicles') as GeoJSONSource | undefined
    if (!src) return
    const now = Date.now() / 1000
    src.setData({
      type: 'FeatureCollection',
      features: vehicles
        .filter((v) => v.lat != null && v.lon != null)
        .map((v) => ({
          type: 'Feature' as const,
          geometry: { type: 'Point' as const, coordinates: [v.lon as number, v.lat as number] },
          properties: {
            vehicle_id: v.vehicle_id,
            route_id: v.route_id ?? '',
            route_name: v.route_name ?? v.route_id ?? '',
            route_desc: v.route_desc ?? '',
            bearing: v.bearing ?? 0,
            stopped: (v.speed ?? 0) < 0.5,
            selected: selectedRoute != null && v.route_id === selectedRoute,
            bunched: bunched.has(v.vehicle_id),
            agency_id: v.agency_id ?? '',
            speed_kmh: ((v.speed ?? 0) * 3.6).toFixed(1),
            age: Math.max(0, Math.round(now - v.ts)),
          },
        })),
    })
  }, [vehicles, selectedRoute, bunched, mapReady])

  // ---- selected route shape + stops ---------------------------------------
  useEffect(() => {
    const m = map.current
    if (!m || !ready.current) return
    const routeSrc = m.getSource('route') as GeoJSONSource | undefined
    const stopSrc = m.getSource('stops') as GeoJSONSource | undefined
    if (!routeSrc || !stopSrc) return

    routeSrc.setData(routeShape ?? EMPTY)
    stopSrc.setData({
      type: 'FeatureCollection',
      features: routeStops.map((s) => ({
        type: 'Feature' as const,
        geometry: { type: 'Point' as const, coordinates: [s.stop_lon, s.stop_lat] },
        properties: { stop_name: s.stop_name },
      })),
    })

    if (routeShape?.geometry.coordinates.length) {
      const bounds = routeShape.geometry.coordinates.reduce(
        (b: LngLatBounds, c: GeoJSON.Position) => b.extend(c as [number, number]),
        new LngLatBounds(
          routeShape.geometry.coordinates[0] as [number, number],
          routeShape.geometry.coordinates[0] as [number, number]))
      m.fitBounds(bounds, { padding: 80, duration: 900, maxZoom: 13 })
    }
  }, [routeShape, routeStops, mapReady])

  if (failed) {
    return (
      <div style={{
        position: 'absolute', inset: 0, display: 'grid', placeItems: 'center',
        padding: 24, textAlign: 'center', background: 'var(--surface-1)',
      }}>
        <div style={{ maxWidth: 420 }}>
          <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 6 }}>
            Map unavailable
          </div>
          <div style={{ fontSize: 12, color: 'var(--text-secondary)', lineHeight: 1.6 }}>
            MapLibre needs WebGL2, which this browser did not provide. Live tracking
            and analytics in the sidebar keep working.
          </div>
          <div style={{
            fontSize: 11, color: 'var(--text-muted)', marginTop: 10,
            fontFamily: 'ui-monospace, monospace',
          }}>{failed}</div>
        </div>
      </div>
    )
  }

  return <div ref={container} style={{ position: 'absolute', inset: 0 }} />
}
