// Package scenario reads scenario manifests (scenarios/*.yaml): what the
// upstream mock serves, how the collector runs, which buyers call the
// Consumer API at what rate, and the thresholds the run must meet.
package scenario

import (
	"bytes"
	"fmt"
	"os"
	"slices"
	"time"

	"go.yaml.in/yaml/v3"

	"github.com/ONDC-Official/gtfs-poc/harness/internal/faults"
	"github.com/ONDC-Official/gtfs-poc/harness/internal/load"
)

// Scenario is one manifest.
// MaxAgencies is the most mock agencies a dev run creates (the test plan's
// ramp goes to 25; the dev consumer's ADMIN_AGENCY_MAX defaults to 30).
const MaxAgencies = 25

type Scenario struct {
	ID          string `yaml:"scenario_id"`
	Description string `yaml:"description"`
	Seed        uint64 `yaml:"seed"`
	AgencyCount int    `yaml:"agency_count"` // local stack: 1; dev target: 1..MaxAgencies (MOCK_01..)

	Upstream   Upstream   `yaml:"upstream"`
	Collector  Collector  `yaml:"collector"`
	Identities Identities `yaml:"identities"`

	// Readiness waits for the stack to serve real-time and static data
	// before any measured phase starts.
	ReadyTimeout Duration `yaml:"ready_timeout"`
	Phases       []Phase  `yaml:"phases"`

	FaultSchedule []FaultStep `yaml:"fault_schedule"`
	Thresholds    Thresholds  `yaml:"acceptance_thresholds"`
}

// Upstream sizes the mock BMTC's payloads.
type Upstream struct {
	VehicleEntities     int `yaml:"vehicle_entities"`
	TripsPerZone        int `yaml:"trips_per_zone"`
	StopsPerTrip        int `yaml:"stops_per_trip"`
	StaticStops         int `yaml:"static_stops"`
	StaticRoutes        int `yaml:"static_routes"`
	StaticTripsPerRoute int `yaml:"static_trips_per_route"`
	StaticStopsPerTrip  int `yaml:"static_stops_per_trip"`
}

// Collector settings applied through the collector's own env.
type Collector struct {
	IntervalS          int      `yaml:"collector_interval_s"`
	RetentionS         int      `yaml:"retention_s"`
	ProjectorInterval  Duration `yaml:"projector_interval"`
	TripUpdatesEnabled bool     `yaml:"trip_updates_enabled"`
}

// Identities are the synthetic buyers to create.
type Identities struct {
	Valid          int `yaml:"valid"`
	LowQuota       int `yaml:"low_quota"`
	LowQuotaPerMin int `yaml:"low_quota_per_min"`
	Malformed      int `yaml:"malformed"`
	UnknownAgency  int `yaml:"unknown_agency"`
	// QuotaTest buyers exist to be pushed past their quota on purpose; the
	// harness then checks the consumer enforces it exactly (TC-9).
	QuotaTest       int `yaml:"quota_test"`
	QuotaTestPerMin int `yaml:"quota_test_per_min"` // default and maximum 100
}

// Phase is a measured stretch of traffic.
type Phase struct {
	Name     string    `yaml:"name"`
	Duration Duration  `yaml:"duration"`
	Profiles []Profile `yaml:"profiles"`
	// StartAtSecond delays the phase until the wall clock reaches this
	// second of a minute (0-59), e.g. 55 to straddle a quota window.
	StartAtSecond *int `yaml:"start_at_second"`
}

// Profile is one traffic stream in a phase.
type Profile struct {
	Name        string   `yaml:"name"`
	Endpoint    string   `yaml:"endpoint"` // static | realtime
	RPS         float64  `yaml:"rps"`
	Concurrency int      `yaml:"concurrency"`
	Conditional bool     `yaml:"conditional"`
	Identities  []string `yaml:"identities"` // kinds: valid, low_quota, malformed, unknown_agency, quota_test
	// IdentityOffset skips the first n buyers of the pool, and
	// IdentityLimit then uses at most n of the rest (0: all).
	IdentityOffset int `yaml:"identity_offset"`
	IdentityLimit  int `yaml:"identity_limit"`
	// HonorRetryAfter: after a 429, a buyer waits Retry-After before its
	// next request, and that request must succeed.
	HonorRetryAfter bool `yaml:"honor_retry_after"`
}

// FaultStep installs fault rules at an offset into the measured phases.
type FaultStep struct {
	At    Duration      `yaml:"at"`
	Rules []faults.Rule `yaml:"rules"` // empty clears faults
}

// Thresholds are the pass/fail limits. A nil pointer is not checked.
type Thresholds struct {
	AvailabilityMin    *float64 `yaml:"availability_min"`
	RealtimeP95MsMax   *float64 `yaml:"realtime_p95_ms_max"`
	RealtimeP99MsMax   *float64 `yaml:"realtime_p99_ms_max"`
	StaticP95MsMax     *float64 `yaml:"static_p95_ms_max"`
	ServerErrorRateMax *float64 `yaml:"server_error_rate_max"`
	CheckFailuresMax   *int64   `yaml:"check_failures_max"`
	ClientSaturatedMax *int64   `yaml:"client_saturated_max"`
	// QuotaViolationsMax bounds early refusals, quota leaks, bad
	// Retry-After headers and refusals after waiting Retry-After.
	QuotaViolationsMax *int64   `yaml:"quota_violations_max"`
	ProjectorLagSMax   *float64 `yaml:"projector_lag_s_max"`
	DataAgeSMax        *float64 `yaml:"data_age_s_max"`
	VehicleSuccessMin  *float64 `yaml:"vehicle_collection_success_min"`
	NoFiringAlerts     bool     `yaml:"no_firing_alerts"`
}

// Duration reads "30s", "5m".
type Duration time.Duration

func (d *Duration) UnmarshalYAML(n *yaml.Node) error {
	v, err := time.ParseDuration(n.Value)
	if err != nil {
		return fmt.Errorf("line %d: %w", n.Line, err)
	}
	*d = Duration(v)
	return nil
}

// D is the time.Duration.
func (d Duration) D() time.Duration { return time.Duration(d) }

// Load reads and validates a manifest.
func Load(path string) (*Scenario, error) {
	b, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var s Scenario
	dec := yaml.NewDecoder(bytes.NewReader(b))
	dec.KnownFields(true)
	if err := dec.Decode(&s); err != nil {
		return nil, fmt.Errorf("%s: %w", path, err)
	}
	s.applyDefaults()
	if err := s.Validate(); err != nil {
		return nil, fmt.Errorf("%s: %w", path, err)
	}
	return &s, nil
}

func (s *Scenario) applyDefaults() {
	if s.Seed == 0 {
		s.Seed = 1
	}
	if s.AgencyCount == 0 {
		s.AgencyCount = 1
	}
	if s.ReadyTimeout == 0 {
		s.ReadyTimeout = Duration(5 * time.Minute)
	}
	if s.Collector.IntervalS == 0 {
		s.Collector.IntervalS = 30
	}
	if s.Collector.RetentionS == 0 {
		s.Collector.RetentionS = max(300, 2*s.Collector.IntervalS)
	}
	if s.Collector.ProjectorInterval == 0 {
		s.Collector.ProjectorInterval = Duration(5 * time.Second)
	}
	if s.Identities.LowQuotaPerMin == 0 {
		s.Identities.LowQuotaPerMin = 30
	}
	if s.Identities.QuotaTestPerMin == 0 {
		s.Identities.QuotaTestPerMin = load.MaxQuotaPerMin
	}
	for i := range s.Phases {
		for j := range s.Phases[i].Profiles {
			p := &s.Phases[i].Profiles[j]
			if p.Concurrency == 0 {
				p.Concurrency = max(1, int(p.RPS))
			}
			if len(p.Identities) == 0 {
				p.Identities = []string{load.KindValid}
			}
		}
	}
}

// Validate reports a manifest the runner cannot execute.
func (s *Scenario) Validate() error {
	if s.ID == "" {
		return fmt.Errorf("scenario_id is required")
	}
	if s.AgencyCount < 1 || s.AgencyCount > MaxAgencies {
		return fmt.Errorf("agency_count %d: must be 1..%d", s.AgencyCount, MaxAgencies)
	}
	if s.Collector.IntervalS < 5 {
		return fmt.Errorf("collector_interval_s must be >= 5 (the collector's minimum)")
	}
	if len(s.Phases) == 0 {
		return fmt.Errorf("at least one phase is required")
	}
	if s.Identities.Valid < 1 {
		return fmt.Errorf("identities.valid must be >= 1")
	}
	counts := s.identityCounts()
	for _, ph := range s.Phases {
		if ph.Name == "" || ph.Duration <= 0 || len(ph.Profiles) == 0 {
			return fmt.Errorf("phase %q needs a name, a positive duration and at least one profile", ph.Name)
		}
		for _, p := range ph.Profiles {
			if p.Endpoint != load.EndpointStatic && p.Endpoint != load.EndpointRealtime {
				return fmt.Errorf("phase %q profile %q: endpoint must be static or realtime", ph.Name, p.Name)
			}
			if p.RPS <= 0 {
				return fmt.Errorf("phase %q profile %q: rps must be positive", ph.Name, p.Name)
			}
			for _, k := range p.Identities {
				if !slices.Contains([]string{load.KindValid, load.KindLowQuota, load.KindMalformed, load.KindUnknownAgency, load.KindQuotaTest}, k) {
					return fmt.Errorf("phase %q profile %q: unknown identity kind %q", ph.Name, p.Name, k)
				}
				if counts[k] == 0 {
					return fmt.Errorf("phase %q profile %q uses %q identities but identities.%s is 0", ph.Name, p.Name, k, k)
				}
			}
		}
	}
	for _, ph := range s.Phases {
		if ph.StartAtSecond != nil && (*ph.StartAtSecond < 0 || *ph.StartAtSecond > 59) {
			return fmt.Errorf("phase %q: start_at_second must be 0..59", ph.Name)
		}
		for _, p := range ph.Profiles {
			if p.IdentityLimit < 0 || p.IdentityOffset < 0 {
				return fmt.Errorf("phase %q profile %q: identity_offset and identity_limit must be >= 0", ph.Name, p.Name)
			}
			pool := 0
			for _, k := range p.Identities {
				pool += s.identityCounts()[k]
			}
			if p.IdentityOffset >= pool {
				return fmt.Errorf("phase %q profile %q: identity_offset %d leaves no buyers (pool %d)", ph.Name, p.Name, p.IdentityOffset, pool)
			}
			// Quota-test buyers are pushed past their quota on purpose;
			// keeping them apart from other kinds keeps the other buyers'
			// results (and the isolation check) clean.
			if slices.Contains(p.Identities, load.KindQuotaTest) && len(p.Identities) > 1 {
				return fmt.Errorf("phase %q profile %q: quota_test identities can't share a profile with other kinds", ph.Name, p.Name)
			}
		}
	}
	if s.Identities.QuotaTestPerMin < 1 || s.Identities.QuotaTestPerMin > load.MaxQuotaPerMin {
		return fmt.Errorf("identities.quota_test_per_min %d: must be 1..%d (test buyers never get more than %d requests per minute)",
			s.Identities.QuotaTestPerMin, load.MaxQuotaPerMin, load.MaxQuotaPerMin)
	}
	if s.Identities.LowQuotaPerMin < 1 || s.Identities.LowQuotaPerMin > load.MaxQuotaPerMin {
		return fmt.Errorf("identities.low_quota_per_min %d: must be 1..%d (test buyers never get more than %d requests per minute)",
			s.Identities.LowQuotaPerMin, load.MaxQuotaPerMin, load.MaxQuotaPerMin)
	}
	// Valid buyers get the harness's maximum quota (load.MaxQuotaPerMin);
	// each must stay under it with headroom, or valid buyers would see 429s.
	for _, ph := range s.Phases {
		if perMin := s.ValidPerBuyerPerMin(ph); perMin > MaxValidPerBuyerPerMin {
			return fmt.Errorf("phase %q: each valid buyer would send %.0f requests per minute, above %d (quota %d with headroom): add valid identities (need at least %d)",
				ph.Name, perMin, MaxValidPerBuyerPerMin, load.MaxQuotaPerMin, s.validNeeded(ph))
		}
	}
	for i, f := range s.FaultSchedule {
		for j := range f.Rules {
			if err := f.Rules[j].Validate(); err != nil {
				return fmt.Errorf("fault_schedule[%d] rule %d: %w", i, j, err)
			}
		}
	}
	return nil
}

// MaxValidPerBuyerPerMin is the most requests per minute a scenario may send
// with one valid buyer: the 100/min quota minus headroom for the limiter's
// fixed one-minute window and arrival jitter.
const MaxValidPerBuyerPerMin = 90

// ValidPerBuyerPerMin is the request rate each valid buyer gets in a phase:
// a profile spreads its rate evenly (round robin) over its identity pool,
// and a buyer used by several profiles adds up their shares.
func (s *Scenario) identityCounts() map[string]int {
	return map[string]int{load.KindValid: s.Identities.Valid, load.KindLowQuota: s.Identities.LowQuota,
		load.KindMalformed: s.Identities.Malformed, load.KindUnknownAgency: s.Identities.UnknownAgency,
		load.KindQuotaTest: s.Identities.QuotaTest}
}

func (s *Scenario) ValidPerBuyerPerMin(ph Phase) float64 {
	counts := s.identityCounts()
	var perMin float64
	for _, p := range ph.Profiles {
		if !slices.Contains(p.Identities, load.KindValid) {
			continue
		}
		pool := 0
		for _, k := range p.Identities {
			pool += counts[k]
		}
		pool -= min(p.IdentityOffset, pool)
		if p.IdentityLimit > 0 {
			pool = min(pool, p.IdentityLimit)
		}
		if pool > 0 {
			perMin += p.RPS * 60 / float64(pool)
		}
	}
	return perMin
}

// validNeeded is the fewest valid buyers that keep phase ph under the cap,
// assuming the other kinds stay as they are.
func (s *Scenario) validNeeded(ph Phase) int {
	c := *s
	for n := s.Identities.Valid; n < 10000; n++ {
		c.Identities.Valid = n
		if c.ValidPerBuyerPerMin(ph) <= MaxValidPerBuyerPerMin {
			return n
		}
	}
	return 10000
}

// TotalDuration is the sum of the phase durations.
func (s *Scenario) TotalDuration() time.Duration {
	var d time.Duration
	for _, p := range s.Phases {
		d += p.Duration.D()
	}
	return d
}
