import { useEffect, useMemo, useRef, useState } from 'react'
import {
  GeoJSONSource, LngLatBounds, MapLibreMap, NavigationControl, Popup,
  ScaleControl, setWorkerUrl, type MapLayerMouseEvent, type MapOptions,
} from 'maplibre-gl'
import 'maplibre-gl/dist/maplibre-gl.css'
import maplibreWorkerUrl from 'maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url'
import type { CorridorResponse, GridResponse } from '../types'
import { METRICS, type MetricId, rampColor, valueOf } from '../metrics'

setWorkerUrl(maplibreWorkerUrl)

const DELHI: [number, number] = [77.216, 28.62]
const EMPTY: GeoJSON.FeatureCollection = { type: 'FeatureCollection', features: [] }

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
      id: 'basemap', type: 'raster', source: 'osm',
      paint: {
        // A touch dimmer than the live map, since the metric layer sits on
        // top - but still legible as a map underneath it.
        'raster-saturation': -0.85,
        'raster-brightness-min': 0.02,
        'raster-brightness-max': 0.30,
        'raster-contrast': -0.12,
        'raster-opacity': 0.9,
      },
    },
  ],
}

interface Props {
  grid: GridResponse | null
  metric: MetricId
  corridor: CorridorResponse | null
  routeShape: GeoJSON.Feature<GeoJSON.LineString> | null
  focus: { lat: number; lon: number } | null
}

export function GridMap({ grid, metric, corridor, routeShape, focus }: Props) {
  const container = useRef<HTMLDivElement>(null)
  const map = useRef<MapLibreMap | null>(null)
  const popup = useRef<Popup | null>(null)
  const [ready, setReady] = useState(false)
  const [failed, setFailed] = useState<string | null>(null)

  const spec = METRICS[metric]

  // Cells become points, not rectangles. A grid of hard squares reads as
  // blocks rather than a phenomenon; soft overlapping circles sized to the
  // cell merge into a continuous field, which is what the data actually is.
  // Switching metric only restyles these features - it never rebuilds them.
  const cellFeatures = useMemo<GeoJSON.FeatureCollection>(() => {
    if (!grid) return EMPTY
    const { lat_deg, lon_deg, factor } = grid.grid
    const idx = Object.fromEntries(grid.columns.map((c, i) => [c, i]))
    const dy = lat_deg * factor
    const dx = lon_deg * factor
    return {
      type: 'FeatureCollection',
      features: grid.cells.map((row) => ({
        type: 'Feature' as const,
        geometry: {
          type: 'Point' as const,
          // centre of the cell
          coordinates: [(row[idx.gx] + 0.5) * dx, (row[idx.gy] + 0.5) * dy],
        },
        properties: {
          obs: row[idx.obs],
          peak: row[idx.peak_vehicles],
          moving: row[idx.moving],
          stopped: row[idx.stopped],
          speed_kmh: row[idx.speed_kmh],
          dwell_pct: row[idx.dwell_pct],
        },
      })),
    }
  }, [grid])

  // ---- construct ---------------------------------------------------------
  useEffect(() => {
    if (!container.current || map.current) return
    let m: MapLibreMap
    try {
      m = new MapLibreMap({
        container: container.current, style: STYLE,
        center: DELHI, zoom: 9.6, attributionControl: { compact: true },
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
      m.addSource('cells', { type: 'geojson', data: EMPTY })
      m.addSource('corridor', { type: 'geojson', data: EMPTY })
      m.addSource('route', { type: 'geojson', data: EMPTY })

      m.addLayer({
        id: 'cells-fill', type: 'circle', source: 'cells',
        paint: {
          'circle-color': '#3987e5',
          'circle-opacity': 0.6,
          'circle-radius': 6,
          // Feathered edges let neighbouring cells blend into one surface
          // instead of tiling as visible discs.
          'circle-blur': 0.5,
          'circle-pitch-alignment': 'map',
        },
      })

      m.addLayer({
        id: 'route-line', type: 'line', source: 'route',
        layout: { 'line-cap': 'round', 'line-join': 'round' },
        paint: { 'line-color': '#c3c2b7', 'line-width': 1.5, 'line-opacity': 0.5 },
      })
      m.addLayer({
        id: 'corridor-dot', type: 'circle', source: 'corridor',
        paint: {
          'circle-radius': ['interpolate', ['linear'], ['zoom'], 9, 3, 14, 7],
          'circle-color': ['get', 'color'],
          'circle-stroke-color': '#121211',
          'circle-stroke-width': 1,
        },
      })

      setReady(true)
    })

    const show = (e: MapLayerMouseEvent) => {
      const f = e.features?.[0]
      if (!f) return
      const p = f.properties as Record<string, number>
      popup.current?.remove()
      popup.current = new Popup({ closeButton: true, offset: 6 })
        .setLngLat(e.lngLat)
        .setHTML(
          `<div style="font-weight:600;margin-bottom:5px">Grid cell</div>` +
          `<div style="color:#c3c2b7;line-height:1.6;font-variant-numeric:tabular-nums">` +
          `Observations <b style="color:#fff">${Number(p.obs).toLocaleString()}</b><br/>` +
          `Peak vehicles <b style="color:#fff">${p.peak}</b><br/>` +
          `Moving speed <b style="color:#fff">${p.speed_kmh ?? '—'}</b> km/h<br/>` +
          `Stopped <b style="color:#fff">${p.dwell_pct ?? '—'}</b>% of observations` +
          `</div>`)
        .addTo(m)
    }
    m.on('click', 'cells-fill', show)
    m.on('mouseenter', 'cells-fill', () => { m.getCanvas().style.cursor = 'pointer' })
    m.on('mouseleave', 'cells-fill', () => { m.getCanvas().style.cursor = '' })

    return () => { m.remove(); map.current = null; setReady(false) }
  }, [])


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
  }, [ready])

  // ---- data --------------------------------------------------------------
  useEffect(() => {
    const m = map.current
    if (!m || !ready) return
    ;(m.getSource('cells') as GeoJSONSource | undefined)?.setData(cellFeatures)
  }, [cellFeatures, ready])

  // ---- restyle on metric change (no refetch, no rebuild) -----------------
  useEffect(() => {
    const m = map.current
    if (!m || !ready || !grid) return
    const stops = spec.stops(grid)

    // Size each circle to the ground area its cell covers, so the surface
    // stays continuous at every zoom instead of dissolving into dots when you
    // zoom in or clumping into a blob when you zoom out.
    // metres-per-pixel at Delhi's latitude is 156543 * cos(28.6) / 2^zoom.
    // A 500 m cell is barely 2 px across at city zoom, so geographic accuracy
    // alone leaves a scatter of dots. The floor keeps neighbours overlapping
    // into one surface when zoomed out; the geographic term takes over once
    // zoomed in far enough for cells to have real size.
    const M_PER_PX_Z0 = 156543.03 * Math.cos((28.6 * Math.PI) / 180)
    // Diagonal neighbours sit sqrt(2) cell-widths apart, and circle-blur eats
    // into the solid core, so the radius has to exceed half a cell by a fair
    // margin before the marks actually merge into a surface.
    const MIN_PX = 7
    const OVERLAP = 0.85
    const radiusAt = (z: number) =>
      Math.max(MIN_PX, ((grid.grid.cell_m * 2 ** z) / M_PER_PX_Z0) * OVERLAP)
    m.setPaintProperty('cells-fill', 'circle-radius', [
      'interpolate', ['exponential', 2], ['zoom'],
      8, radiusAt(8), 11, radiusAt(11), 14, radiusAt(14), 17, radiusAt(17),
    ])

    m.setPaintProperty('cells-fill', 'circle-color', [
      'interpolate', ['linear'], spec.expression,
      ...stops.flatMap((s) => [s.value, s.color]),
    ])
    // Opacity tracks the ramp's brightness, not the raw value: the end that
    // gets the bright colour also gets the solid fill, so the metric's
    // extreme comes forward and the uninteresting end recedes into the
    // basemap. Reversing one without the other makes them cancel out.
    // Against a legible (rather than near-black) basemap the layer needs more
    // weight, or the metric reads as a faint haze over the streets.
    const solid = 0.92
    const faint = 0.38
    m.setPaintProperty('cells-fill', 'circle-opacity', [
      'interpolate', ['linear'], spec.expression,
      stops[0].value, spec.reversed ? solid : faint,
      stops[stops.length - 1].value, spec.reversed ? faint : solid,
    ])
  }, [metric, grid, ready, spec])

  // ---- corridor overlay --------------------------------------------------
  useEffect(() => {
    const m = map.current
    if (!m || !ready) return
    const corridorSrc = m.getSource('corridor') as GeoJSONSource | undefined
    const routeSrc = m.getSource('route') as GeoJSONSource | undefined
    routeSrc?.setData(routeShape ?? EMPTY)

    if (!corridor) { corridorSrc?.setData(EMPTY); return }
    corridorSrc?.setData({
      type: 'FeatureCollection',
      features: corridor.points.map((p) => ({
        type: 'Feature' as const,
        geometry: { type: 'Point' as const, coordinates: [p.lon, p.lat] },
        properties: {
          // Corridor points always encode speed, whatever the grid shows.
          color: rampColor(METRICS.congestion, p.speed_kmh ?? 0),
          speed: p.speed_kmh,
        },
      })),
    })
    const pts = corridor.points
    if (pts.length) {
      const b = pts.reduce(
        (acc: LngLatBounds, p) => acc.extend([p.lon, p.lat]),
        new LngLatBounds([pts[0].lon, pts[0].lat], [pts[0].lon, pts[0].lat]))
      m.fitBounds(b, { padding: 90, duration: 800, maxZoom: 13 })
    }
  }, [corridor, routeShape, ready])

  // ---- fly to a hotspot --------------------------------------------------
  useEffect(() => {
    const m = map.current
    if (!m || !ready || !focus) return
    m.flyTo({ center: [focus.lon, focus.lat], zoom: 14.5, duration: 900 })
  }, [focus, ready])

  if (failed) {
    return (
      <div style={{
        position: 'absolute', inset: 0, display: 'grid', placeItems: 'center',
        padding: 24, textAlign: 'center', background: 'var(--surface-1)',
      }}>
        <div style={{ maxWidth: 420 }}>
          <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 6 }}>Map unavailable</div>
          <div style={{ fontSize: 12, color: 'var(--text-secondary)', lineHeight: 1.6 }}>
            MapLibre needs WebGL2, which this browser did not provide. The
            rankings and trends in the sidebar keep working.
          </div>
        </div>
      </div>
    )
  }

  return <div ref={container} style={{ position: 'absolute', inset: 0 }} />
}

export { valueOf }
