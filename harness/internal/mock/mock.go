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
// The same endpoints are served per named agency under /{agency_id}/...
// (e.g. /MOCK_01/GetGTFSVehicleUpdateData), each with its own token, data
// settings, faults, static version and stats. The root paths are the
// default agency (Config.Token), as used by the local stack.
//
// Control endpoints (what the harness calls; never exposed to the collector).
// Faults, bump and stats take ?agency=<id>; without it they act on the
// default agency.
//
//	GET|PUT|DELETE /_harness/faults        fault rules (JSON list of faults.Rule)
//	POST /_harness/static/bump             publish a new static schedule version
//	GET  /_harness/stats                   what was served, as JSON
//	GET  /_harness/agencies                named agencies (never their tokens)
//	PUT  /_harness/agencies/{agency_id}    add or replace one: {"token": "...", "vehicles": n, "shift_s": n}
//	DELETE /_harness/agencies/{agency_id}  remove one
//	DELETE /_harness/agencies              remove all
//	GET  /_harness/healthz
//	GET  /metrics                          the same counters for Prometheus (label "agency")
package mock

import (
	"crypto/subtle"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"math/rand/v2"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"sort"
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

// DefaultAgency labels the root-path agency in stats and metrics.
const DefaultAgency = "default"

// MaxAgencies bounds the named agencies (the plan ramps to 25).
const MaxAgencies = 100

var agencyIDPattern = regexp.MustCompile(`^[A-Z0-9_]{1,32}$`)

// AgencySpec is a named agency as set through the control API and saved in
// the state file.
type AgencySpec struct {
	ID       string `json:"agency_id"`
	Token    string `json:"token"`
	Vehicles int    `json:"vehicles,omitempty"` // 0: the server's default
	ShiftS   *int   `json:"shift_s,omitempty"`  // vehicle timestamp shift in seconds; nil: the server's default
}

// AgencyInfo is a named agency as listed by the control API (no token).
type AgencyInfo struct {
	ID            string    `json:"agency_id"`
	TokenSet      bool      `json:"token_set"`
	Vehicles      int       `json:"vehicles"`
	ShiftS        int       `json:"shift_s"`
	StaticVersion int64     `json:"static_version"`
	AddedAt       time.Time `json:"added_at"`
}

// agency is one feed: the default (root paths) or a named one.
type agency struct {
	id      string
	cfg     Config // the server's config with this agency's overrides
	faults  *faults.Set
	version atomic.Int64
	added   time.Time

	staticMu    sync.Mutex
	staticCache map[int64]map[string]string

	statsMu sync.Mutex
	stats   map[string]*EndpointStats
}

// Server serves the feed and control endpoints.
type Server struct {
	cfg Config
	log *slog.Logger
	seq atomic.Uint64

	def       *agency
	mu        sync.RWMutex
	agencies  map[string]*agency
	statePath string // "" keeps named agencies in memory only

	requests *prometheus.CounterVec // agency, endpoint, outcome
	bytes    *prometheus.CounterVec // agency, endpoint
	entities *prometheus.CounterVec // agency, endpoint
	registry *prometheus.Registry
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
	s := &Server{cfg: cfg, log: log, agencies: map[string]*agency{}, registry: prometheus.NewRegistry()}
	s.def = s.newAgency(DefaultAgency, cfg)
	s.requests = prometheus.NewCounterVec(prometheus.CounterOpts{Name: "mockbmtc_requests_total",
		Help: "Requests served by the mock BMTC, by agency, endpoint and outcome (ok or the fault action)"}, []string{"agency", "endpoint", "outcome"})
	s.bytes = prometheus.NewCounterVec(prometheus.CounterOpts{Name: "mockbmtc_bytes_total",
		Help: "Response body bytes written"}, []string{"agency", "endpoint"})
	s.entities = prometheus.NewCounterVec(prometheus.CounterOpts{Name: "mockbmtc_entities_total",
		Help: "GTFS-RT entities or CSV rows served"}, []string{"agency", "endpoint"})
	s.registry.MustRegister(s.requests, s.bytes, s.entities)
	return s
}

func (s *Server) newAgency(id string, cfg Config) *agency {
	a := &agency{id: id, cfg: cfg, faults: faults.NewSet(cfg.Seed), added: time.Now().UTC().Truncate(time.Second),
		staticCache: map[int64]map[string]string{}, stats: map[string]*EndpointStats{}}
	a.version.Store(int64(max(cfg.Static.Version, 1)))
	return a
}

// Faults is the default agency's fault rule set.
func (s *Server) Faults() *faults.Set { return s.def.faults }

// AddAgency adds or replaces a named agency. Replacing keeps its faults,
// stats and static version, so a token rotation doesn't reset a test.
func (s *Server) AddAgency(spec AgencySpec) error {
	if !agencyIDPattern.MatchString(spec.ID) || spec.ID == DefaultAgency {
		return fmt.Errorf("agency_id must be 1-32 characters of A-Z, 0-9 and _")
	}
	if spec.Token == "" || len(spec.Token) > 256 {
		return errors.New("token is required (at most 256 characters)")
	}
	if spec.Vehicles < 0 || spec.Vehicles > 50000 {
		return errors.New("vehicles must be between 0 (default) and 50000")
	}
	cfg := s.cfg
	cfg.Token = spec.Token
	if spec.Vehicles > 0 {
		cfg.Vehicles = spec.Vehicles
	}
	if spec.ShiftS != nil {
		cfg.Shift = time.Duration(*spec.ShiftS) * time.Second
	}
	s.mu.Lock()
	if old, ok := s.agencies[spec.ID]; ok {
		old.cfg = cfg
	} else {
		if len(s.agencies) >= MaxAgencies {
			s.mu.Unlock()
			return fmt.Errorf("at most %d agencies", MaxAgencies)
		}
		s.agencies[spec.ID] = s.newAgency(spec.ID, cfg)
	}
	err := s.saveLocked()
	s.mu.Unlock()
	s.log.Info("agency set", "agency_id", spec.ID, "vehicles", cfg.Vehicles, "shift_s", int(cfg.Shift.Seconds()))
	return err
}

// RemoveAgency removes a named agency; "" removes all. It reports whether
// anything was removed.
func (s *Server) RemoveAgency(id string) (bool, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	n := len(s.agencies)
	if id == "" {
		s.agencies = map[string]*agency{}
	} else {
		delete(s.agencies, id)
	}
	if len(s.agencies) == n {
		return false, nil
	}
	s.log.Info("agency removed", "agency_id", id, "remaining", len(s.agencies))
	return true, s.saveLocked()
}

// Agencies lists the named agencies, sorted by id.
func (s *Server) Agencies() []AgencyInfo {
	s.mu.RLock()
	defer s.mu.RUnlock()
	out := make([]AgencyInfo, 0, len(s.agencies))
	for _, a := range s.agencies {
		out = append(out, AgencyInfo{ID: a.id, TokenSet: a.cfg.Token != "", Vehicles: a.cfg.Vehicles,
			ShiftS: int(a.cfg.Shift.Seconds()), StaticVersion: a.version.Load(), AddedAt: a.added})
	}
	sort.Slice(out, func(i, j int) bool { return out[i].ID < out[j].ID })
	return out
}

func (s *Server) lookup(id string) *agency {
	if id == "" || id == DefaultAgency {
		return s.def
	}
	s.mu.RLock()
	defer s.mu.RUnlock()
	return s.agencies[id]
}

// UseStateFile loads named agencies from path (if it exists) and saves them
// there on every change, mode 0600 (it holds their tokens).
func (s *Server) UseStateFile(path string) error {
	b, err := os.ReadFile(path)
	if err != nil && !errors.Is(err, os.ErrNotExist) {
		return err
	}
	var specs []AgencySpec
	if len(b) > 0 {
		if err := json.Unmarshal(b, &specs); err != nil {
			return fmt.Errorf("state file %s: %w", path, err)
		}
	}
	for _, sp := range specs { // loaded before statePath is set: no rewrite per agency
		if err := s.AddAgency(sp); err != nil {
			return fmt.Errorf("state file %s: %s: %w", path, sp.ID, err)
		}
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	s.statePath = path
	return s.saveLocked()
}

func (s *Server) saveLocked() error {
	if s.statePath == "" {
		return nil
	}
	specs := make([]AgencySpec, 0, len(s.agencies))
	for _, a := range s.agencies {
		shift := int(a.cfg.Shift.Seconds())
		specs = append(specs, AgencySpec{ID: a.id, Token: a.cfg.Token, Vehicles: a.cfg.Vehicles, ShiftS: &shift})
	}
	sort.Slice(specs, func(i, j int) bool { return specs[i].ID < specs[j].ID })
	b, _ := json.MarshalIndent(specs, "", "  ")
	tmp := s.statePath + ".tmp"
	if err := os.MkdirAll(filepath.Dir(s.statePath), 0o700); err != nil {
		return err
	}
	if err := os.WriteFile(tmp, b, 0o600); err != nil {
		return err
	}
	return os.Rename(tmp, s.statePath)
}

// FeedHandler serves the three BMTC endpoints.
func (s *Server) FeedHandler() http.Handler {
	mux := http.NewServeMux()
	for _, prefix := range []string{"", "/{agency}"} {
		mux.HandleFunc("POST "+prefix+PathVehicles, s.withAgency(s.vehicles))
		mux.HandleFunc("POST "+prefix+PathTripUpdates, s.withAgency(s.trips))
		mux.HandleFunc("POST "+prefix+PathStatic, s.withAgency(s.static))
	}
	// Reachability check for the public path (host nginx -> web nginx ->
	// mock). No auth and no data, so it is safe to expose with the feed.
	mux.HandleFunc("GET /_ping", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "text/plain")
		_, _ = w.Write([]byte("mock-agency ok\n"))
	})
	return mux
}

// withAgency resolves {agency} (absent: the default agency). An unknown
// agency gets BMTC's auth error, so the mock doesn't reveal which IDs exist.
func (s *Server) withAgency(h func(*agency, http.ResponseWriter, *http.Request)) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		id := r.PathValue("agency")
		if id == DefaultAgency { // the default agency is only served at the root paths
			writeBMTCAuthError(w)
			return
		}
		a := s.lookup(id)
		if a == nil {
			writeBMTCAuthError(w)
			s.requests.WithLabelValues("unknown", "-", "unknown_agency").Inc()
			return
		}
		h(a, w, r)
	}
}

// agencyParam resolves ?agency= on a control request, writing 404 if unknown.
func (s *Server) agencyParam(w http.ResponseWriter, r *http.Request) *agency {
	a := s.lookup(r.URL.Query().Get("agency"))
	if a == nil {
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "no agency " + r.URL.Query().Get("agency")})
	}
	return a
}

// ControlHandler serves the control API and /metrics.
func (s *Server) ControlHandler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /_harness/healthz", func(w http.ResponseWriter, _ *http.Request) { _, _ = w.Write([]byte("ok\n")) })
	mux.HandleFunc("GET /_harness/faults", func(w http.ResponseWriter, r *http.Request) {
		if a := s.agencyParam(w, r); a != nil {
			writeJSON(w, http.StatusOK, a.faults.Rules())
		}
	})
	mux.HandleFunc("PUT /_harness/faults", func(w http.ResponseWriter, r *http.Request) {
		a := s.agencyParam(w, r)
		if a == nil {
			return
		}
		var rules []faults.Rule
		if err := json.NewDecoder(io.LimitReader(r.Body, 1<<20)).Decode(&rules); err != nil {
			writeJSON(w, http.StatusBadRequest, map[string]string{"error": "body must be a JSON list of rules: " + err.Error()})
			return
		}
		if err := a.faults.Replace(rules); err != nil {
			writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
			return
		}
		s.log.Info("faults set", "agency_id", a.id, "rules", len(rules))
		writeJSON(w, http.StatusOK, a.faults.Rules())
	})
	mux.HandleFunc("DELETE /_harness/faults", func(w http.ResponseWriter, r *http.Request) {
		a := s.agencyParam(w, r)
		if a == nil {
			return
		}
		_ = a.faults.Replace(nil)
		s.log.Info("faults cleared", "agency_id", a.id)
		writeJSON(w, http.StatusOK, []faults.Rule{})
	})
	mux.HandleFunc("POST /_harness/static/bump", func(w http.ResponseWriter, r *http.Request) {
		a := s.agencyParam(w, r)
		if a == nil {
			return
		}
		v := a.version.Add(1)
		s.log.Info("static version bumped", "agency_id", a.id, "version", v)
		writeJSON(w, http.StatusOK, map[string]int64{"version": v})
	})
	mux.HandleFunc("GET /_harness/stats", func(w http.ResponseWriter, r *http.Request) {
		a := s.agencyParam(w, r)
		if a == nil {
			return
		}
		a.statsMu.Lock()
		defer a.statsMu.Unlock()
		writeJSON(w, http.StatusOK, map[string]any{"agency_id": a.id, "static_version": a.version.Load(), "endpoints": a.stats})
	})
	mux.HandleFunc("GET /_harness/agencies", func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, http.StatusOK, map[string]any{"agencies": s.Agencies()})
	})
	mux.HandleFunc("PUT /_harness/agencies/{agency_id}", func(w http.ResponseWriter, r *http.Request) {
		var spec AgencySpec
		dec := json.NewDecoder(io.LimitReader(r.Body, 16<<10))
		dec.DisallowUnknownFields()
		if err := dec.Decode(&spec); err != nil {
			writeJSON(w, http.StatusBadRequest, map[string]string{"error": `body must be {"token": "...", "vehicles": n, "shift_s": n}: ` + err.Error()})
			return
		}
		if spec.ID != "" && spec.ID != r.PathValue("agency_id") {
			writeJSON(w, http.StatusBadRequest, map[string]string{"error": "agency_id in the body does not match the path"})
			return
		}
		spec.ID = r.PathValue("agency_id")
		if err := s.AddAgency(spec); err != nil {
			writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
			return
		}
		for _, a := range s.Agencies() {
			if a.ID == spec.ID {
				writeJSON(w, http.StatusOK, a)
				return
			}
		}
	})
	mux.HandleFunc("DELETE /_harness/agencies/{agency_id}", func(w http.ResponseWriter, r *http.Request) {
		ok, err := s.RemoveAgency(r.PathValue("agency_id"))
		switch {
		case err != nil:
			writeJSON(w, http.StatusInternalServerError, map[string]string{"error": err.Error()})
		case !ok:
			writeJSON(w, http.StatusNotFound, map[string]string{"error": "no agency " + r.PathValue("agency_id")})
		default:
			writeJSON(w, http.StatusOK, map[string]any{"removed": r.PathValue("agency_id")})
		}
	})
	mux.HandleFunc("DELETE /_harness/agencies", func(w http.ResponseWriter, _ *http.Request) {
		if _, err := s.RemoveAgency(""); err != nil {
			writeJSON(w, http.StatusInternalServerError, map[string]string{"error": err.Error()})
			return
		}
		writeJSON(w, http.StatusOK, map[string]any{"agencies": []AgencyInfo{}})
	})
	mux.Handle("GET /metrics", promhttp.HandlerFor(s.registry, promhttp.HandlerOpts{}))
	return mux
}

func (a *agency) authorized(w http.ResponseWriter, r *http.Request) bool {
	if subtleEqual(r.Header.Get("authToken"), a.cfg.Token) {
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

func (s *Server) vehicles(a *agency, w http.ResponseWriter, r *http.Request) {
	if !a.authorized(w, r) {
		s.count(a, faults.EndpointVehicles, "unauthorized", 0, 0)
		return
	}
	s.respond(a, w, faults.EndpointVehicles, 0, func() ([]byte, string, int, error) {
		feed := fakefeed.Build(fakefeed.Options{
			Now: time.Now(), Vehicles: a.cfg.Vehicles, Shift: a.cfg.Shift,
			Dupes: a.cfg.Dupes, Stale: a.cfg.Stale, Invalid: a.cfg.Invalid, Seed: s.nextSeed(),
		})
		return s.encodeFeed(a, feed)
	})
}

func (s *Server) trips(a *agency, w http.ResponseWriter, r *http.Request) {
	if !a.authorized(w, r) {
		s.count(a, faults.EndpointTrips, "unauthorized", 0, 0)
		return
	}
	var req struct {
		ZoneID *int `json:"zoneid"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil || req.ZoneID == nil || *req.ZoneID < 1 || *req.ZoneID > 7 {
		http.Error(w, `body must be {"zoneid": 1..7}`, http.StatusBadRequest)
		s.count(a, faults.EndpointTrips, "bad_request", 0, 0)
		return
	}
	zone := *req.ZoneID
	s.respond(a, w, faults.EndpointTrips, zone, func() ([]byte, string, int, error) {
		feed := fakefeed.TripUpdates(fakefeed.TripOptions{
			Now: time.Now(), Zone: zone, Trips: a.cfg.TripsPerZone, StopsPerTrip: a.cfg.StopsPerTrip,
			CrossZone: a.cfg.TripsPerZone / 20, Invalid: 3, Seed: s.nextSeed(),
		})
		return s.encodeFeed(a, feed)
	})
}

// Static: {"check": n} returns one GTFS file. The check → file mapping is
// shuffled on purpose (fakefeed.StaticCheckOrder): the collector must
// identify files by header, not by check number.
func (s *Server) static(a *agency, w http.ResponseWriter, r *http.Request) {
	if !a.authorized(w, r) {
		s.count(a, faults.EndpointStatic, "unauthorized", 0, 0)
		return
	}
	var req struct {
		Check  *int `json:"check"`
		ZoneID *int `json:"zoneid"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil || req.Check == nil ||
		*req.Check < 0 || *req.Check >= len(fakefeed.StaticCheckOrder) {
		http.Error(w, `body must be {"check": 0..7}`, http.StatusBadRequest)
		s.count(a, faults.EndpointStatic, "bad_request", 0, 0)
		return
	}
	zone := 0
	if req.ZoneID != nil {
		zone = *req.ZoneID
	}
	s.respond(a, w, faults.EndpointStatic, zone, func() ([]byte, string, int, error) {
		name := fakefeed.StaticCheckOrder[*req.Check]
		csv := a.staticFiles(a.version.Load())[name]
		if name == "stop_times.txt" && a.cfg.StaticZoned {
			csv = fakefeed.StaticZone(csv, req.ZoneID)
		}
		lines := strings.Split(strings.TrimRight(csv, "\n"), "\n")
		if a.cfg.Format != FormatBMTC {
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

func (a *agency) staticFiles(v int64) map[string]string {
	a.staticMu.Lock()
	defer a.staticMu.Unlock()
	if f, ok := a.staticCache[v]; ok {
		return f
	}
	o := a.cfg.Static
	o.Version = int(v)
	f := fakefeed.StaticFiles(o)
	clear(a.staticCache) // keep only the current version
	a.staticCache[v] = f
	return f
}

func (s *Server) nextSeed() uint64 { return s.cfg.Seed*1_000_003 + s.seq.Add(1) }

func (s *Server) encodeFeed(a *agency, feed *gtfs.FeedMessage) ([]byte, string, int, error) {
	if a.cfg.Format == FormatProtobuf {
		return fakefeed.Marshal(feed), "application/x-protobuf", len(feed.Entity), nil
	}
	// Real BMTC uses snake_case names and string uint64s: protojson with
	// proto names produces exactly that.
	js, err := protojson.MarshalOptions{UseProtoNames: true}.Marshal(feed)
	if err != nil {
		return nil, "", 0, err
	}
	if a.cfg.Format == FormatBMTC {
		js = fmt.Appendf(nil, `{"Data":%s,"Message":null,"IsSuccess":true,"Exception":null,"RowCount":%d}`, js, len(feed.Entity))
	}
	return js, "application/json; charset=utf-8", len(feed.Entity), nil
}

// respond applies the first matching fault, else serves build()'s body.
func (s *Server) respond(a *agency, w http.ResponseWriter, endpoint string, zone int, build func() ([]byte, string, int, error)) {
	rule := a.faults.Match(endpoint, zone)
	if rule != nil {
		switch rule.Action {
		case faults.ActionStatus:
			http.Error(w, fmt.Sprintf("mock fault: HTTP %d", rule.Status), rule.Status)
			s.count(a, endpoint, "status_"+fmt.Sprint(rule.Status), 0, 0)
			return
		case faults.ActionAuth:
			writeBMTCAuthError(w)
			s.count(a, endpoint, faults.ActionAuth, 0, 0)
			return
		case faults.ActionDrop:
			if hj, ok := w.(http.Hijacker); ok {
				if conn, _, err := hj.Hijack(); err == nil {
					_ = conn.Close()
				}
			}
			s.count(a, endpoint, faults.ActionDrop, 0, 0)
			return
		case faults.ActionDelay:
			time.Sleep(time.Duration(rule.Delay))
		}
	}

	body, contentType, entities, err := build()
	if err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
		s.count(a, endpoint, "error", 0, 0)
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
	s.count(a, endpoint, outcome, len(body), entities)
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

func (s *Server) count(a *agency, endpoint, outcome string, bytes, entities int) {
	s.requests.WithLabelValues(a.id, endpoint, outcome).Inc()
	s.bytes.WithLabelValues(a.id, endpoint).Add(float64(bytes))
	s.entities.WithLabelValues(a.id, endpoint).Add(float64(entities))
	a.statsMu.Lock()
	st := a.stats[endpoint]
	if st == nil {
		st = &EndpointStats{Outcomes: map[string]int64{}}
		a.stats[endpoint] = st
	}
	st.Requests++
	st.Outcomes[outcome]++
	st.Bytes += int64(bytes)
	st.Entities += int64(entities)
	a.statsMu.Unlock()
	if outcome != "ok" {
		s.log.Info("served with fault", "agency_id", a.id, "endpoint", endpoint, "outcome", outcome)
	}
}

func subtleEqual(got, want string) bool {
	return want != "" && subtle.ConstantTimeCompare([]byte(got), []byte(want)) == 1
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}
