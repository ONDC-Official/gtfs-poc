// Package mock is the harness's BMTC stand-in: the three BMTC GTFS endpoints
// with BMTC's auth contract and data quirks (from internal/fakefeed), plus
// runtime faults and a plain-HTTP control API for the scenario runner.
//
// Feed endpoints (what the collector calls):
//
//	POST /GetGTFSVehicleUpdateData   {}
//	POST /GetGTFSTripsUpdateData     {"zoneid": 1..7}
//	POST /GetGTFSStaticData          {"check": 0..7[, "zoneid": 1..7]}
//
// Control endpoints (what the harness calls; never exposed to the collector):
//
//	GET|PUT|DELETE /_harness/faults   fault rules (JSON list of faults.Rule)
//	POST /_harness/static/bump        publish a new static schedule version
//	GET  /_harness/stats              what was served, as JSON
//	GET  /_harness/healthz
//	GET  /metrics                     the same counters for Prometheus
package mock

import (
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"math/rand/v2"
	"net/http"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/MobilityData/gtfs-realtime-bindings/golang/gtfs"
	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promhttp"
	"google.golang.org/protobuf/encoding/protojson"

	"github.com/ONDC-Official/gtfs-poc/harness/internal/fakefeed"
	"github.com/ONDC-Official/gtfs-poc/harness/internal/faults"
)

// BMTC API paths.
const (
	PathVehicles    = "/GetGTFSVehicleUpdateData"
	PathTripUpdates = "/GetGTFSTripsUpdateData"
	PathStatic      = "/GetGTFSStaticData"
)

// Response formats.
const (
	FormatBMTC     = "bmtc"     // JSON in BMTC's envelopes (the real API)
	FormatJSON     = "json"     // unwrapped GTFS-RT JSON / plain CSV
	FormatProtobuf = "protobuf" // unwrapped GTFS-RT protobuf / plain CSV
)

// Config is the mock's data shape. Zero values are replaced by Defaults.
type Config struct {
	Token  string
	Format string
	Seed   uint64

	Vehicles int // fresh vehicles per response (real BMTC: ~7,000, ~2.4 MB)
	Dupes    int
	Stale    int
	Invalid  int
	Shift    time.Duration // vehicle timestamp shift (real BMTC: +5 h 30 m)

	TripsPerZone int
	StopsPerTrip int // drives the trip-zone payload size

	Static      fakefeed.StaticOptions // drives the static zip size
	StaticZoned bool                   // stop_times per zone, like the real API
}

// Defaults match the real BMTC API as measured on 2026-11-18.
func Defaults() Config {
	return Config{
		Token: "perf-token", Format: FormatBMTC, Seed: 1,
		Vehicles: 7000, Dupes: 50, Stale: 30, Invalid: 5, Shift: 5*time.Hour + 30*time.Minute,
		TripsPerZone: 1500, StopsPerTrip: 40,
		Static: fakefeed.DefaultStatic, StaticZoned: true,
	}
}

// Server serves the feed and control endpoints.
type Server struct {
	cfg    Config
	faults *faults.Set
	log    *slog.Logger

	seq     atomic.Uint64
	version atomic.Int64

	staticMu    sync.Mutex
	staticCache map[int64]map[string]string

	requests *prometheus.CounterVec // endpoint, outcome
	bytes    *prometheus.CounterVec // endpoint
	entities *prometheus.CounterVec // endpoint
	registry *prometheus.Registry

	statsMu sync.Mutex
	stats   map[string]*EndpointStats
}

// EndpointStats is what one endpoint has served.
type EndpointStats struct {
	Requests int64            `json:"requests"`
	Outcomes map[string]int64 `json:"outcomes"`
	Bytes    int64            `json:"bytes"`
	Entities int64            `json:"entities"`
}

// New returns a server for cfg.
func New(cfg Config, log *slog.Logger) *Server {
	d := Defaults()
	if cfg.Token == "" {
		cfg.Token = d.Token
	}
	if cfg.Format == "" {
		cfg.Format = d.Format
	}
	if cfg.Static.Stops == 0 {
		cfg.Static = d.Static
	}
	s := &Server{
		cfg: cfg, faults: faults.NewSet(cfg.Seed), log: log,
		staticCache: map[int64]map[string]string{},
		stats:       map[string]*EndpointStats{},
		registry:    prometheus.NewRegistry(),
	}
	s.version.Store(int64(max(cfg.Static.Version, 1)))
	s.requests = prometheus.NewCounterVec(prometheus.CounterOpts{Name: "mockbmtc_requests_total",
		Help: "Requests served by the mock BMTC, by endpoint and outcome (ok or the fault action)"}, []string{"endpoint", "outcome"})
	s.bytes = prometheus.NewCounterVec(prometheus.CounterOpts{Name: "mockbmtc_bytes_total",
		Help: "Response body bytes written"}, []string{"endpoint"})
	s.entities = prometheus.NewCounterVec(prometheus.CounterOpts{Name: "mockbmtc_entities_total",
		Help: "GTFS-RT entities or CSV rows served"}, []string{"endpoint"})
	s.registry.MustRegister(s.requests, s.bytes, s.entities)
	return s
}

// Faults is the server's fault rule set.
func (s *Server) Faults() *faults.Set { return s.faults }

// FeedHandler serves the three BMTC endpoints.
func (s *Server) FeedHandler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("POST "+PathVehicles, s.vehicles)
	mux.HandleFunc("POST "+PathTripUpdates, s.trips)
	mux.HandleFunc("POST "+PathStatic, s.static)
	// Reachability check for the public path (host nginx -> web nginx ->
	// mock). No auth and no data, so it is safe to expose with the feed.
	mux.HandleFunc("GET /_ping", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "text/plain")
		_, _ = w.Write([]byte("mock-agency ok\n"))
	})
	return mux
}

// ControlHandler serves the control API and /metrics.
func (s *Server) ControlHandler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /_harness/healthz", func(w http.ResponseWriter, _ *http.Request) { _, _ = w.Write([]byte("ok\n")) })
	mux.HandleFunc("GET /_harness/faults", func(w http.ResponseWriter, _ *http.Request) { writeJSON(w, http.StatusOK, s.faults.Rules()) })
	mux.HandleFunc("PUT /_harness/faults", func(w http.ResponseWriter, r *http.Request) {
		var rules []faults.Rule
		if err := json.NewDecoder(io.LimitReader(r.Body, 1<<20)).Decode(&rules); err != nil {
			writeJSON(w, http.StatusBadRequest, map[string]string{"error": "body must be a JSON list of rules: " + err.Error()})
			return
		}
		if err := s.faults.Replace(rules); err != nil {
			writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
			return
		}
		s.log.Info("faults set", "rules", len(rules))
		writeJSON(w, http.StatusOK, s.faults.Rules())
	})
	mux.HandleFunc("DELETE /_harness/faults", func(w http.ResponseWriter, _ *http.Request) {
		_ = s.faults.Replace(nil)
		s.log.Info("faults cleared")
		writeJSON(w, http.StatusOK, []faults.Rule{})
	})
	mux.HandleFunc("POST /_harness/static/bump", func(w http.ResponseWriter, _ *http.Request) {
		v := s.version.Add(1)
		s.log.Info("static version bumped", "version", v)
		writeJSON(w, http.StatusOK, map[string]int64{"version": v})
	})
	mux.HandleFunc("GET /_harness/stats", func(w http.ResponseWriter, _ *http.Request) {
		s.statsMu.Lock()
		defer s.statsMu.Unlock()
		writeJSON(w, http.StatusOK, map[string]any{"static_version": s.version.Load(), "endpoints": s.stats})
	})
	mux.Handle("GET /metrics", promhttp.HandlerFor(s.registry, promhttp.HandlerOpts{}))
	return mux
}

func (s *Server) authorized(w http.ResponseWriter, r *http.Request) bool {
	if r.Header.Get("authToken") == s.cfg.Token {
		return true
	}
	writeBMTCAuthError(w)
	return false
}

// Same status and body as the real BMTC API.
func writeBMTCAuthError(w http.ResponseWriter) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusUnauthorized)
	_, _ = w.Write([]byte(`{"status":"Error","errorMessage":"Invalid Token"}`))
}

func (s *Server) vehicles(w http.ResponseWriter, r *http.Request) {
	if !s.authorized(w, r) {
		s.count(faults.EndpointVehicles, "unauthorized", 0, 0)
		return
	}
	s.respond(w, faults.EndpointVehicles, 0, func() ([]byte, string, int, error) {
		feed := fakefeed.Build(fakefeed.Options{
			Now: time.Now(), Vehicles: s.cfg.Vehicles, Shift: s.cfg.Shift,
			Dupes: s.cfg.Dupes, Stale: s.cfg.Stale, Invalid: s.cfg.Invalid, Seed: s.nextSeed(),
		})
		return s.encodeFeed(feed)
	})
}

func (s *Server) trips(w http.ResponseWriter, r *http.Request) {
	if !s.authorized(w, r) {
		s.count(faults.EndpointTrips, "unauthorized", 0, 0)
		return
	}
	var req struct {
		ZoneID *int `json:"zoneid"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil || req.ZoneID == nil || *req.ZoneID < 1 || *req.ZoneID > 7 {
		http.Error(w, `body must be {"zoneid": 1..7}`, http.StatusBadRequest)
		s.count(faults.EndpointTrips, "bad_request", 0, 0)
		return
	}
	zone := *req.ZoneID
	s.respond(w, faults.EndpointTrips, zone, func() ([]byte, string, int, error) {
		feed := fakefeed.TripUpdates(fakefeed.TripOptions{
			Now: time.Now(), Zone: zone, Trips: s.cfg.TripsPerZone, StopsPerTrip: s.cfg.StopsPerTrip,
			CrossZone: s.cfg.TripsPerZone / 20, Invalid: 3, Seed: s.nextSeed(),
		})
		return s.encodeFeed(feed)
	})
}

// Static: {"check": n} returns one GTFS file. The check → file mapping is
// shuffled on purpose (fakefeed.StaticCheckOrder): the collector must
// identify files by header, not by check number.
func (s *Server) static(w http.ResponseWriter, r *http.Request) {
	if !s.authorized(w, r) {
		s.count(faults.EndpointStatic, "unauthorized", 0, 0)
		return
	}
	var req struct {
		Check  *int `json:"check"`
		ZoneID *int `json:"zoneid"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil || req.Check == nil ||
		*req.Check < 0 || *req.Check >= len(fakefeed.StaticCheckOrder) {
		http.Error(w, `body must be {"check": 0..7}`, http.StatusBadRequest)
		s.count(faults.EndpointStatic, "bad_request", 0, 0)
		return
	}
	zone := 0
	if req.ZoneID != nil {
		zone = *req.ZoneID
	}
	s.respond(w, faults.EndpointStatic, zone, func() ([]byte, string, int, error) {
		name := fakefeed.StaticCheckOrder[*req.Check]
		csv := s.staticFiles(s.version.Load())[name]
		if name == "stop_times.txt" && s.cfg.StaticZoned {
			csv = fakefeed.StaticZone(csv, req.ZoneID)
		}
		lines := strings.Split(strings.TrimRight(csv, "\n"), "\n")
		if s.cfg.Format != FormatBMTC {
			return []byte(csv), "text/csv", len(lines) - 1, nil
		}
		data, err := json.Marshal(lines)
		if err != nil {
			return nil, "", 0, err
		}
		body := fmt.Appendf(nil, `{"data":%s,"message":null,"issuccess":true,"exception":null}`, data)
		return body, "application/json; charset=utf-8", len(lines) - 1, nil
	})
}

func (s *Server) staticFiles(v int64) map[string]string {
	s.staticMu.Lock()
	defer s.staticMu.Unlock()
	if f, ok := s.staticCache[v]; ok {
		return f
	}
	o := s.cfg.Static
	o.Version = int(v)
	f := fakefeed.StaticFiles(o)
	s.staticCache[v] = f
	return f
}

func (s *Server) nextSeed() uint64 { return s.cfg.Seed*1_000_003 + s.seq.Add(1) }

func (s *Server) encodeFeed(feed *gtfs.FeedMessage) ([]byte, string, int, error) {
	if s.cfg.Format == FormatProtobuf {
		return fakefeed.Marshal(feed), "application/x-protobuf", len(feed.Entity), nil
	}
	// Real BMTC uses snake_case names and string uint64s: protojson with
	// proto names produces exactly that.
	js, err := protojson.MarshalOptions{UseProtoNames: true}.Marshal(feed)
	if err != nil {
		return nil, "", 0, err
	}
	if s.cfg.Format == FormatBMTC {
		js = fmt.Appendf(nil, `{"Data":%s,"Message":null,"IsSuccess":true,"Exception":null,"RowCount":%d}`, js, len(feed.Entity))
	}
	return js, "application/json; charset=utf-8", len(feed.Entity), nil
}

// respond applies the first matching fault, else serves build()'s body.
func (s *Server) respond(w http.ResponseWriter, endpoint string, zone int, build func() ([]byte, string, int, error)) {
	rule := s.faults.Match(endpoint, zone)
	if rule != nil {
		switch rule.Action {
		case faults.ActionStatus:
			http.Error(w, fmt.Sprintf("mock fault: HTTP %d", rule.Status), rule.Status)
			s.count(endpoint, "status_"+fmt.Sprint(rule.Status), 0, 0)
			return
		case faults.ActionAuth:
			writeBMTCAuthError(w)
			s.count(endpoint, faults.ActionAuth, 0, 0)
			return
		case faults.ActionDrop:
			if hj, ok := w.(http.Hijacker); ok {
				if conn, _, err := hj.Hijack(); err == nil {
					_ = conn.Close()
				}
			}
			s.count(endpoint, faults.ActionDrop, 0, 0)
			return
		case faults.ActionDelay:
			time.Sleep(time.Duration(rule.Delay))
		}
	}

	body, contentType, entities, err := build()
	if err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
		s.count(endpoint, "error", 0, 0)
		return
	}
	outcome := "ok"
	if rule != nil && rule.Action == faults.ActionMalformed {
		body, outcome = malform(body, rule.Mode, s.nextSeed()), "malformed_"+rule.Mode
	}
	w.Header().Set("Content-Type", contentType)
	if rule != nil && rule.Action == faults.ActionTrickle {
		trickle(w, body, time.Duration(rule.Delay))
		outcome = faults.ActionTrickle
	} else {
		_, _ = w.Write(body)
	}
	if rule != nil && rule.Action == faults.ActionDelay {
		outcome = faults.ActionDelay
	}
	s.count(endpoint, outcome, len(body), entities)
}

// trickle writes body in 20 chunks spread over d.
func trickle(w http.ResponseWriter, body []byte, d time.Duration) {
	const chunks = 20
	step := max(len(body)/chunks, 1)
	flusher, _ := w.(http.Flusher)
	for i := 0; i < len(body); i += step {
		_, _ = w.Write(body[i:min(i+step, len(body))])
		if flusher != nil {
			flusher.Flush()
		}
		time.Sleep(d / chunks)
	}
}

func malform(body []byte, mode string, seed uint64) []byte {
	switch mode {
	case faults.ModeTruncate:
		return body[:len(body)/2]
	case faults.ModeGarbage:
		r := rand.New(rand.NewPCG(seed, seed))
		out := make([]byte, min(len(body), 4096))
		for i := range out {
			out[i] = byte(r.UintN(256))
		}
		return out
	default: // empty
		return nil
	}
}

func (s *Server) count(endpoint, outcome string, bytes, entities int) {
	s.requests.WithLabelValues(endpoint, outcome).Inc()
	s.bytes.WithLabelValues(endpoint).Add(float64(bytes))
	s.entities.WithLabelValues(endpoint).Add(float64(entities))
	s.statsMu.Lock()
	st := s.stats[endpoint]
	if st == nil {
		st = &EndpointStats{Outcomes: map[string]int64{}}
		s.stats[endpoint] = st
	}
	st.Requests++
	st.Outcomes[outcome]++
	st.Bytes += int64(bytes)
	st.Entities += int64(entities)
	s.statsMu.Unlock()
	if outcome != "ok" {
		s.log.Info("served with fault", "endpoint", endpoint, "outcome", outcome)
	}
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}
