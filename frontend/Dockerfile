# syntax=docker/dockerfile:1
# ---------------------------------------------------------------------------
# UI tier: build the React app, then serve the static bundle from nginx.
# nginx also fronts the API, so the browser stays same-origin and the
# websocket needs no CORS handling - the same arrangement the Vite dev proxy
# gives you in development.
# ---------------------------------------------------------------------------
FROM node:22-alpine AS build

WORKDIR /app

# Install from the lockfile first; source edits then reuse this layer.
COPY package.json package-lock.json ./
RUN npm ci

COPY tsconfig*.json vite.config.ts index.html ./
COPY src ./src

# `npm run build` runs `tsc -b` first, so a type error fails the image build.
RUN npm run build

# ---------------------------------------------------------------------------
FROM nginx:1.27-alpine AS runtime

COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD wget -qO- http://127.0.0.1/healthz >/dev/null || exit 1

CMD ["nginx", "-g", "daemon off;"]
