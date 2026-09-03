import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The service tier runs on :8000. Proxying keeps the browser same-origin, so
// there is no CORS dance and the websocket URL is just /ws/vehicles.
export default defineConfig({
  plugins: [react()],
  // MapLibre ships its own web worker and builds the URL at runtime, so no
  // bundler can see it statically. Two separate problems follow:
  //   dev  - the dep pre-bundler rewrites the URL into .vite/deps, where it
  //          404s. Excluding the package keeps the real path resolvable.
  //   prod - the worker is simply never emitted. MapView imports it with
  //          `?worker&url` and hands it to setWorkerUrl(); this ES format
  //          matches MapLibre's `new Worker(url, {type: 'module'})`.
  // Either way the failure is silent: raster tiles still draw, so the map
  // looks fine while every GeoJSON layer stays empty.
  optimizeDeps: { exclude: ['maplibre-gl'] },
  worker: { format: 'es' },
  server: {
    port: 5173,
    proxy: {
      '/api': { target: 'http://127.0.0.1:8000', changeOrigin: true },
      '/ws': { target: 'ws://127.0.0.1:8000', ws: true },
    },
  },
})
