package faults

import (
	"encoding/json"
	"testing"
	"time"
)

func TestValidate(t *testing.T) {
	bad := []Rule{
		{Endpoint: "nope", Action: ActionDrop},
		{Endpoint: EndpointVehicles, Action: ActionStatus, Status: 200},
		{Endpoint: EndpointVehicles, Action: ActionDelay},
		{Endpoint: EndpointVehicles, Action: ActionMalformed, Mode: "x"},
		{Endpoint: EndpointVehicles, Action: "explode"},
		{Endpoint: EndpointVehicles, Action: ActionDrop, Rate: 1.5},
	}
	for i, r := range bad {
		if r.Validate() == nil {
			t.Errorf("rule %d accepted: %+v", i, r)
		}
	}
	good := Rule{Endpoint: EndpointTrips, Zone: 3, Action: ActionStatus, Status: 429, Rate: 0.5, Count: 2}
	if err := good.Validate(); err != nil {
		t.Fatal(err)
	}
}

func TestMatch(t *testing.T) {
	s := NewSet(1)
	if err := s.Replace([]Rule{
		{Endpoint: EndpointTrips, Zone: 3, Action: ActionStatus, Status: 503},
		{Endpoint: EndpointVehicles, Action: ActionDrop, Count: 2},
		{Endpoint: EndpointAny, Action: ActionDelay, Delay: Duration(time.Second), Rate: 0.5},
	}); err != nil {
		t.Fatal(err)
	}
	if r := s.Match(EndpointTrips, 3); r == nil || r.Status != 503 {
		t.Fatalf("zone 3: %+v", r)
	}
	if r := s.Match(EndpointTrips, 4); r != nil && r.Action != ActionDelay {
		t.Fatalf("zone 4 matched the zone-3 rule: %+v", r)
	}
	for i := range 2 {
		if r := s.Match(EndpointVehicles, 0); r == nil || r.Action != ActionDrop {
			t.Fatalf("drop hit %d: %+v", i, r)
		}
	}
	// Count used up: falls through to the 50 % delay rule.
	delays, n := 0, 2000
	for range n {
		if r := s.Match(EndpointVehicles, 0); r != nil {
			if r.Action != ActionDelay {
				t.Fatalf("drop rule still active: %+v", r)
			}
			delays++
		}
	}
	if delays < n*4/10 || delays > n*6/10 {
		t.Errorf("rate 0.5 applied %d of %d times", delays, n)
	}
}

func TestDurationJSON(t *testing.T) {
	var r Rule
	if err := json.Unmarshal([]byte(`{"endpoint":"static","action":"delay","delay":"1.5s"}`), &r); err != nil {
		t.Fatal(err)
	}
	if time.Duration(r.Delay) != 1500*time.Millisecond {
		t.Errorf("delay %v", time.Duration(r.Delay))
	}
	b, _ := json.Marshal(r)
	if string(b) != `{"endpoint":"static","action":"delay","delay":"1.5s"}` {
		t.Errorf("marshal: %s", b)
	}
}
