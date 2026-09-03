/// <reference types="vite/client" />

// Vite's `?worker&url` suffix yields the emitted worker's URL. Declared here
// because `vite/client` only ships the plain `?worker` and `?url` forms.
declare module '*?worker&url' {
  const src: string
  export default src
}
