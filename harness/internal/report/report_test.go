package report

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/ONDC-Official/gtfs-poc/harness/internal/load"
	"github.com/ONDC-Official/gtfs-poc/harness/internal/scenario"
)

func ptr[T any](v T) *T { return &v }

func TestEvaluate(t *testing.T) {
	r := &Report{ScenarioID: "x", Phases: []load.PhaseResult{{Name: "p", Profiles: []load.ProfileResult{
		{Name: "rt", Endpoint: load.EndpointRealtime, Requests: 1000, Availability: 0.999, ByKind: map[string]int64{load.KindValid: 1000},
			Latency: load.LatencySummary{P95: 120, P99: 300}, DataAge: load.LatencySummary{Max: 15}},
		{Name: "st", Endpoint: load.EndpointStatic, Requests: 100, Availability: 1, ByKind: map[string]int64{load.KindValid: 100},
			Latency: load.LatencySummary{P95: 2500}, CheckFailures: map[string]int64{"zip_invalid": 1}},
	}}}, System: System{ProjectorLagMaxS: ptr(4.0)}}
	r.Evaluate(scenario.Thresholds{
		AvailabilityMin: ptr(0.99), RealtimeP95MsMax: ptr(500.0), StaticP95MsMax: ptr(2000.0),
		CheckFailuresMax: ptr[int64](0), ProjectorLagSMax: ptr(30.0), VehicleSuccessMin: ptr(0.999),
	})
	want := map[string]bool{
		"availability (lowest profile)": true, "real-time p95 (worst phase)": true, "static p95 (worst phase)": false,
		"response check failures": false, "projector lag (max)": true, "vehicle collection success": false, // no data fails
	}
	for _, c := range r.Checks {
		if w, ok := want[c.Name]; ok && w != c.Passed {
			t.Errorf("%s: passed=%v, want %v (%s vs %s)", c.Name, c.Passed, w, c.Actual, c.Limit)
		}
	}
	if r.Passed {
		t.Error("report passed with failing checks")
	}
	dir := t.TempDir()
	if err := r.Write(dir); err != nil {
		t.Fatal(err)
	}
	md, _ := os.ReadFile(filepath.Join(dir, "report.md"))
	if !strings.Contains(string(md), "# x: FAIL") || !strings.Contains(string(md), "❌") {
		t.Errorf("markdown: %s", md)
	}
}
