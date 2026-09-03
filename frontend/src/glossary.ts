/**
 * Every derived term on screen, defined once.
 *
 * The dashboard invents most of these words — "stale", "bunched", "dwell" are
 * not in the GTFS spec, they are thresholds this app chose. So each entry
 * states the rule and its number, and the UI shows it on hover rather than
 * expecting the reader to guess. Thresholds mirror `filters.ts` and the
 * service tier's `live_analytics.py` / `spatial_analytics.py`.
 */
export interface Term {
  title: string
  body: string
  /** The rule, in the same terms the code uses. */
  rule?: string
}

export const GLOSSARY = {
  // ---- vehicle state ----
  moving: {
    title: 'Moving',
    body: 'The bus reported a speed above walking pace at its last update.',
    rule: 'speed > 0.5 m/s (1.8 km/h)',
  },
  stopped: {
    title: 'Stopped',
    body: 'The bus is reporting normally but is not moving — at a stop, in a '
        + 'queue, or parked. It is still live data, just a stationary vehicle.',
    rule: 'speed ≤ 0.5 m/s, and seen within the last 5 minutes',
  },
  stale: {
    title: 'Stale',
    body: 'No position update for over 5 minutes. The bus is still drawn at its '
        + 'last known location, but that location is old — it may have finished '
        + 'its run, lost GPS, or gone out of service.',
    rule: 'now − last report > 5 minutes',
  },
  fresh: {
    title: 'Fresh',
    body: 'Reported within the last minute. The feed updates each vehicle every '
        + '~45s on average, so most of the fleet is fresh at any moment.',
    rule: 'now − last report ≤ 60 seconds',
  },
  bunched: {
    title: 'Bunched',
    body: 'Two or more buses on the same route sitting within 300 m of each '
        + 'other. Bunching means the headway between them has collapsed: one is '
        + 'running late and picking up everyone, the one behind is catching up '
        + 'empty. Passengers experience it as a long wait then three at once.',
    rule: 'same route_id, straight-line distance < 300 m',
  },
  crawling: {
    title: 'Crawling',
    body: 'Moving, but slower than about a brisk cycle. Used to find buses '
        + 'stuck in traffic rather than buses parked at a depot.',
    rule: 'moving and speed < 10 km/h',
  },

  // ---- fleet-level ----
  vehiclesTracked: {
    title: 'Vehicles tracked',
    body: 'Distinct vehicles the poller has seen and is still holding a position '
        + 'for, including stale ones. This is the size of the map, not the size '
        + 'of the operating fleet.',
  },
  routesCovered: {
    title: 'Routes covered',
    body: 'Share of routes in the static GTFS schedule that currently have at '
        + 'least one vehicle reporting. The remainder are "dark" — scheduled, '
        + 'but nothing is broadcasting against them right now.',
    rule: 'routes with ≥ 1 live vehicle ÷ 2,554 scheduled routes',
  },
  medianSpeed: {
    title: 'Median speed',
    body: 'The middle speed across moving vehicles. The median is used rather '
        + 'than the mean because a minority of implausible GPS readings drags '
        + 'a mean upward.',
    rule: 'p50 of moving speeds, readings above 72 km/h excluded as noise',
  },

  // ---- analytics tier ----
  congestion: {
    title: 'Congestion',
    body: 'The average speed of buses that are actually in motion inside each '
        + '250 m cell. Cells that are stationary most of the time are excluded, '
        + 'because a depot full of parked buses averages a few km/h without any '
        + 'traffic being involved.',
    rule: 'mean speed of moving buses; cells > 35% stationary excluded; '
        + 'hotspot list requires < 15 km/h',
  },
  activity: {
    title: 'Activity',
    body: 'How many position reports landed in each cell over the window — a '
        + 'proxy for service density. Terminals and interchanges dominate, so '
        + 'the colour scale is capped at the 90th percentile to keep the rest '
        + 'of the network readable.',
    rule: 'count of observations per cell, scale capped at p90',
  },
  dwell: {
    title: 'Dwell',
    body: 'The share of observations in a cell where the bus was stationary. '
        + 'High dwell marks depots, terminals and layover points — the places '
        + 'buses wait rather than pass through.',
    rule: '(stationary observations ÷ all observations) per cell',
  },
  busiest: {
    title: 'Busiest',
    body: 'Cells with the most position reports in the window. These are the '
        + 'network’s load-bearing points: interchanges, terminals and trunk '
        + 'corridors.',
  },
  cellSize: {
    title: 'Cell size',
    body: 'The map is divided into a grid and every observation is folded into '
        + 'a cell. Smaller cells give more detail; larger ones smooth out noise '
        + 'and draw faster. Data is stored at 250 m and combined upward.',
  },
  window: {
    title: 'Window',
    body: 'How far back the rollup looks. A short window shows conditions now; '
        + 'a long one covers a peak period. Overnight there is almost no '
        + 'service, so a 6h window at 8am is mostly empty road.',
  },
  observations: {
    title: 'Observations',
    body: 'One row per vehicle per poll. Roughly 5,000–6,000 arrive every 30 '
        + 'seconds, so an hour of running is around 700,000 rows.',
  },

  // ---- feed health ----
  implausible: {
    title: 'Implausible speed',
    body: 'Readings above 72 km/h. Delhi city buses do not sustain that, and '
        + 'the feed clamps at exactly 50 m/s, so these are GPS artefacts. They '
        + 'are excluded from every speed statistic rather than averaged in.',
    rule: 'speed ≥ 20 m/s (72 km/h)',
  },
  pollLatency: {
    title: 'Poll latency',
    body: 'Round-trip time for one fetch of the GTFS-realtime feed, including '
        + 'protobuf parsing. Not the age of the data.',
  },
  darkRoutes: {
    title: 'Dark routes',
    body: 'Routes that exist in the published schedule but have no vehicle '
        + 'reporting. Some are genuinely unserved right now; others may be '
        + 'served by buses broadcasting a different route id.',
  },
} as const satisfies Record<string, Term>

export type TermId = keyof typeof GLOSSARY
