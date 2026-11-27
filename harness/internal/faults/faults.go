// Package faults holds the mock BMTC's fault rules: what to do to a request
// instead of (or before) serving it normally. Rules are set at runtime
// through the control API (PUT /_harness/faults) or a scenario's
// fault_schedule.
package faults

import (
	"encoding/json"
	"fmt"
	"math/rand/v2"
	"slices"
	"sync"
	"time"
)

// Endpoints a rule can target.
const (
	EndpointVehicles = "vehicles"
	EndpointTrips    = "trips"
	EndpointStatic   = "static"
	EndpointAny      = "*"
)

// Actions.
const (
	ActionStatus    = "status"    // reply with Status (429, 4xx, 5xx) and a short body
	ActionDelay     = "delay"     // wait Delay, then serve normally
	ActionTrickle   = "trickle"   // serve normally, but write the body slowly over Delay
	ActionDrop      = "drop"      // close the connection without a response
	ActionMalformed = "malformed" // serve a broken body (Mode: truncate, garbage, empty)
	ActionAuth      = "auth"      // reply like BMTC does to a bad token (401)
)

// Malformed modes.
const (
	ModeTruncate = "truncate"
	ModeGarbage  = "garbage"
	ModeEmpty    = "empty"
)

// Duration is a time.Duration that reads and writes "2s" in JSON and YAML.
type Duration time.Duration

func (d Duration) MarshalJSON() ([]byte, error) { return json.Marshal(time.Duration(d).String()) }

func (d *Duration) UnmarshalJSON(b []byte) error {
	var s string
	if err := json.Unmarshal(b, &s); err != nil {
		return fmt.Errorf("duration must be a string like \"2s\": %w", err)
	}
	v, err := time.ParseDuration(s)
	*d = Duration(v)
	return err
}

func (d *Duration) UnmarshalText(b []byte) error {
	v, err := time.ParseDuration(string(b))
	*d = Duration(v)
	return err
}

// Rule is one fault. Endpoint and Zone select requests; Rate is the share of
// matching requests it applies to; Count, when positive, stops it after that
// many hits.
type Rule struct {
	Endpoint string   `json:"endpoint" yaml:"endpoint"`
	Zone     int      `json:"zone,omitempty" yaml:"zone,omitempty"` // trips only; 0 = every zone
	Action   string   `json:"action" yaml:"action"`
	Status   int      `json:"status,omitempty" yaml:"status,omitempty"`
	Delay    Duration `json:"delay,omitempty" yaml:"delay,omitempty"`
	Mode     string   `json:"mode,omitempty" yaml:"mode,omitempty"`
	Rate     float64  `json:"rate,omitempty" yaml:"rate,omitempty"` // 0 means 1 (every request)
	Count    int      `json:"count,omitempty" yaml:"count,omitempty"`

	hits int
}

// Validate reports a rule that cannot be applied.
func (r *Rule) Validate() error {
	if !slices.Contains([]string{EndpointVehicles, EndpointTrips, EndpointStatic, EndpointAny}, r.Endpoint) {
		return fmt.Errorf("endpoint %q: want vehicles, trips, static or *", r.Endpoint)
	}
	switch r.Action {
	case ActionStatus:
		if r.Status < 400 || r.Status > 599 {
			return fmt.Errorf("status action needs a 4xx or 5xx status, got %d", r.Status)
		}
	case ActionDelay, ActionTrickle:
		if r.Delay <= 0 {
			return fmt.Errorf("%s action needs a positive delay", r.Action)
		}
	case ActionMalformed:
		if !slices.Contains([]string{ModeTruncate, ModeGarbage, ModeEmpty}, r.Mode) {
			return fmt.Errorf("malformed action needs mode truncate, garbage or empty, got %q", r.Mode)
		}
	case ActionDrop, ActionAuth:
	default:
		return fmt.Errorf("unknown action %q", r.Action)
	}
	if r.Rate < 0 || r.Rate > 1 {
		return fmt.Errorf("rate must be between 0 and 1, got %v", r.Rate)
	}
	if r.Count < 0 {
		return fmt.Errorf("count must not be negative")
	}
	return nil
}

// Set is the active rule list, safe for concurrent use.
type Set struct {
	mu    sync.Mutex
	rules []*Rule
	rnd   *rand.Rand
}

// NewSet returns an empty set whose sampling is deterministic for seed.
func NewSet(seed uint64) *Set {
	return &Set{rnd: rand.New(rand.NewPCG(seed, seed^0x2545f4914f6cdd1d))}
}

// Replace validates and installs rules, dropping the previous ones.
func (s *Set) Replace(rules []Rule) error {
	list := make([]*Rule, 0, len(rules))
	for i := range rules {
		r := rules[i]
		if err := r.Validate(); err != nil {
			return fmt.Errorf("rule %d: %w", i, err)
		}
		r.hits = 0
		list = append(list, &r)
	}
	s.mu.Lock()
	s.rules = list
	s.mu.Unlock()
	return nil
}

// Rules returns a copy of the active rules.
func (s *Set) Rules() []Rule {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make([]Rule, 0, len(s.rules))
	for _, r := range s.rules {
		out = append(out, *r)
	}
	return out
}

// Match returns the first rule that applies to this request, or nil. A rule
// that applies has its hit counted; a rule whose Count is used up is skipped.
func (s *Set) Match(endpoint string, zone int) *Rule {
	s.mu.Lock()
	defer s.mu.Unlock()
	for _, r := range s.rules {
		if r.Endpoint != EndpointAny && r.Endpoint != endpoint {
			continue
		}
		if r.Zone != 0 && r.Zone != zone {
			continue
		}
		if r.Count > 0 && r.hits >= r.Count {
			continue
		}
		rate := r.Rate
		if rate == 0 {
			rate = 1
		}
		if rate < 1 && s.rnd.Float64() >= rate {
			continue
		}
		r.hits++
		c := *r
		return &c
	}
	return nil
}
