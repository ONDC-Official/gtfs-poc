// Package report evaluates a run against its scenario's thresholds and
// writes report.json and report.md.
package report

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"github.com/ONDC-Official/gtfs-poc/harness/internal/load"
	"github.com/ONDC-Official/gtfs-poc/harness/internal/scenario"
)

// Report is one run.
type Report struct {
	ScenarioID  string    `json:"scenario_id"`
	Description string    `json:"description"`
	RunID       string    `json:"run_id"`
	Start       time.Time `json:"start"`
	End         time.Time `json:"end"`
	Seed        uint64    `json:"seed"`
	Environment Env       `json:"environment"`

	ReadyAfterS float64            `json:"ready_after_s"`
	Phases      []load.PhaseResult `json:"phases"`
	System      System             `json:"system"`
	Mock        json.RawMessage    `json:"mock_stats,omitempty"`
	Checks      []Check            `json:"checks"`
	Passed      bool               `json:"passed"`
	Notes       []string           `json:"notes,omitempty"`
}

// Env records what was tested where.
type Env struct {
	Host               string `json:"host"`
	CPUs               int    `json:"cpus"`
	CollectorSrc       string `json:"collector_src"`
	CollectorRev       string `json:"collector_rev"`
	ConsumerSrc        string `json:"consumer_src"`
	ConsumerRev        string `json:"consumer_rev"`
	HarnessRev         string `json:"harness_rev"`
	CollectorIntervalS int    `json:"collector_interval_s"`
	// Target is the deployed Transit Hub tested from outside ("" = the local stack).
	Target string `json:"target,omitempty"`
}

// System is what Prometheus saw during the measured window.
type System struct {
	ProjectorLagMaxS         *float64           `json:"projector_lag_max_s"`
	VehicleCollectionSuccess *float64           `json:"vehicle_collection_success"`
	ServerErrorRate          *float64           `json:"consumer_server_error_rate"`
	ServerP95S               map[string]float64 `json:"consumer_server_p95_s,omitempty"`
	AlertsFired              []string           `json:"alerts_fired"`
	MaxRSSBytes              map[string]float64 `json:"max_rss_bytes,omitempty"`
	MaxGoroutines            map[string]float64 `json:"max_goroutines,omitempty"`
	QueryErrors              []string           `json:"query_errors,omitempty"`
}

// Check is one threshold.
type Check struct {
	Name   string `json:"name"`
	Limit  string `json:"limit"`
	Actual string `json:"actual"`
	Passed bool   `json:"passed"`
}

// Evaluate fills r.Checks and r.Passed from t.
func (r *Report) Evaluate(t scenario.Thresholds) {
	add := func(name, limit, actual string, ok bool) {
		r.Checks = append(r.Checks, Check{Name: name, Limit: limit, Actual: actual, Passed: ok})
	}
	var minAvail = 1.0
	var checkFails, saturated, requests, serverErrs int64
	var rtP95, rtP99, stP95, ageMax float64
	for _, ph := range r.Phases {
		for _, p := range ph.Profiles {
			if p.ByKind[load.KindValid] > 0 {
				minAvail = min(minAvail, p.Availability)
			}
			for _, n := range p.CheckFailures {
				checkFails += n
			}
			saturated += p.ClientSaturated
			requests += p.Requests
			serverErrs += p.ServerErrors
			switch p.Endpoint {
			case load.EndpointRealtime:
				rtP95, rtP99 = max(rtP95, p.Latency.P95), max(rtP99, p.Latency.P99)
				ageMax = max(ageMax, p.DataAge.Max)
			case load.EndpointStatic:
				stP95 = max(stP95, p.Latency.P95)
			}
		}
	}
	errRate := 0.0
	if requests > 0 {
		errRate = float64(serverErrs) / float64(requests)
	}
	if v := t.AvailabilityMin; v != nil {
		add("availability (lowest profile)", fmt.Sprintf(">= %.4f", *v), fmt.Sprintf("%.5f", minAvail), minAvail >= *v)
	}
	if v := t.RealtimeP95MsMax; v != nil {
		add("real-time p95 (worst phase)", fmt.Sprintf("<= %.0f ms", *v), fmt.Sprintf("%.1f ms", rtP95), rtP95 <= *v)
	}
	if v := t.RealtimeP99MsMax; v != nil {
		add("real-time p99 (worst phase)", fmt.Sprintf("<= %.0f ms", *v), fmt.Sprintf("%.1f ms", rtP99), rtP99 <= *v)
	}
	if v := t.StaticP95MsMax; v != nil {
		add("static p95 (worst phase)", fmt.Sprintf("<= %.0f ms", *v), fmt.Sprintf("%.1f ms", stP95), stP95 <= *v)
	}
	if v := t.ServerErrorRateMax; v != nil {
		add("5xx rate (client side)", fmt.Sprintf("< %.4f", *v), fmt.Sprintf("%.5f", errRate), errRate < *v)
	}
	if v := t.CheckFailuresMax; v != nil {
		add("response check failures", fmt.Sprintf("<= %d", *v), fmt.Sprint(checkFails), checkFails <= *v)
	}
	if v := t.ClientSaturatedMax; v != nil {
		add("harness saturation (arrivals dropped)", fmt.Sprintf("<= %d", *v), fmt.Sprint(saturated), saturated <= *v)
	}
	if v := t.DataAgeSMax; v != nil {
		add("live-data age (max)", fmt.Sprintf("<= %.0f s", *v), fmt.Sprintf("%.1f s", ageMax), ageMax <= *v)
	}
	if v := t.ProjectorLagSMax; v != nil {
		ok, actual := r.System.ProjectorLagMaxS != nil && *r.System.ProjectorLagMaxS <= *v, "no data"
		if r.System.ProjectorLagMaxS != nil {
			actual = fmt.Sprintf("%.1f s", *r.System.ProjectorLagMaxS)
		}
		add("projector lag (max)", fmt.Sprintf("<= %.0f s", *v), actual, ok)
	}
	if v := t.VehicleSuccessMin; v != nil {
		ok, actual := r.System.VehicleCollectionSuccess != nil && *r.System.VehicleCollectionSuccess >= *v, "no data"
		if r.System.VehicleCollectionSuccess != nil {
			actual = fmt.Sprintf("%.4f", *r.System.VehicleCollectionSuccess)
		}
		add("vehicle collection success", fmt.Sprintf(">= %.4f", *v), actual, ok)
	}
	if t.NoFiringAlerts {
		add("no alerts fired", "none", strings.Join(orNone(r.System.AlertsFired), ", "), len(r.System.AlertsFired) == 0)
	}
	r.Passed = true
	for _, c := range r.Checks {
		r.Passed = r.Passed && c.Passed
	}
}

func orNone(v []string) []string {
	if len(v) == 0 {
		return []string{"none"}
	}
	return v
}

// Write saves report.json and report.md in dir.
func (r *Report) Write(dir string) error {
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return err
	}
	b, err := json.MarshalIndent(r, "", "  ")
	if err != nil {
		return err
	}
	if err := os.WriteFile(filepath.Join(dir, "report.json"), b, 0o644); err != nil {
		return err
	}
	return os.WriteFile(filepath.Join(dir, "report.md"), []byte(r.Markdown()), 0o644)
}

// Markdown renders the human summary.
func (r *Report) Markdown() string {
	var b strings.Builder
	verdict := "PASS"
	if !r.Passed {
		verdict = "FAIL"
	}
	fmt.Fprintf(&b, "# %s: %s\n\n%s\n\n", r.ScenarioID, verdict, r.Description)
	fmt.Fprintf(&b, "- Run `%s`, %s → %s (%s)\n", r.RunID, r.Start.Format(time.RFC3339), r.End.Format(time.RFC3339), r.End.Sub(r.Start).Round(time.Second))
	fmt.Fprintf(&b, "- Seed %d; collector interval %d s; ready after %.0f s\n", r.Seed, r.Environment.CollectorIntervalS, r.ReadyAfterS)
	if r.Environment.Target != "" {
		fmt.Fprintf(&b, "- Target `%s` (deployed, tested from outside); harness `%s` on %s, %d CPUs\n\n",
			r.Environment.Target, r.Environment.HarnessRev, r.Environment.Host, r.Environment.CPUs)
		b.WriteString("Measured from outside through public APIs: includes the network path to the target.\n\n")
	} else {
		fmt.Fprintf(&b, "- Collector `%s`, consumer `%s`, harness `%s`; host %s, %d CPUs\n\n",
			r.Environment.CollectorRev, r.Environment.ConsumerRev, r.Environment.HarnessRev, r.Environment.Host, r.Environment.CPUs)
		b.WriteString("Local results: comparable between runs on this machine, not production capacity.\n\n")
	}

	b.WriteString("## Thresholds\n\n| Check | Limit | Actual | Result |\n|---|---|---|---|\n")
	for _, c := range r.Checks {
		res := "✅"
		if !c.Passed {
			res = "❌"
		}
		fmt.Fprintf(&b, "| %s | %s | %s | %s |\n", c.Name, c.Limit, c.Actual, res)
	}

	b.WriteString("\n## Phases\n\n| Phase | Profile | Target RPS | Achieved | Requests | p50 / p95 / p99 / max ms | Availability | Statuses | Check failures | Dropped |\n|---|---|---|---|---|---|---|---|---|---|\n")
	for _, ph := range r.Phases {
		for _, p := range ph.Profiles {
			lat, avail := "– (no valid buyers)", "–"
			if p.ByKind[load.KindValid] > 0 {
				lat = fmt.Sprintf("%.1f / %.1f / %.1f / %.1f", p.Latency.P50, p.Latency.P95, p.Latency.P99, p.Latency.Max)
				avail = fmt.Sprintf("%.4f", p.Availability)
			}
			fmt.Fprintf(&b, "| %s | %s (%s) | %.1f | %.1f | %d | %s | %s | %s | %s | %d |\n",
				ph.Name, p.Name, p.Endpoint, p.RPS, p.AchievedRPS, p.Requests, lat, avail,
				kv(p.Statuses), kv(p.CheckFailures), p.ClientSaturated)
		}
	}
	for _, ph := range r.Phases {
		for _, p := range ph.Profiles {
			for _, s := range p.FailureSamples {
				fmt.Fprintf(&b, "\n- `%s/%s` sample failure: %s", ph.Name, p.Name, s)
			}
		}
	}

	if r.Environment.Target != "" {
		b.WriteString("\n\n## Mock agencies (what the target collector fetched)\n\n")
		if len(r.Mock) > 0 {
			fmt.Fprintf(&b, "```json\n%s\n```\n", r.Mock)
		} else {
			b.WriteString("no data\n")
		}
		for _, n := range r.Notes {
			fmt.Fprintf(&b, "\nNote: %s\n", n)
		}
		return b.String()
	}
	b.WriteString("\n\n## System (Prometheus, measured window)\n\n")
	f := func(p *float64, unit string) string {
		if p == nil {
			return "no data"
		}
		return fmt.Sprintf("%.4g%s", *p, unit)
	}
	fmt.Fprintf(&b, "- Projector lag max: %s\n", f(r.System.ProjectorLagMaxS, " s"))
	fmt.Fprintf(&b, "- Vehicle collection success: %s\n", f(r.System.VehicleCollectionSuccess, ""))
	fmt.Fprintf(&b, "- Consumer 5xx rate (server side): %s\n", f(r.System.ServerErrorRate, ""))
	fmt.Fprintf(&b, "- Consumer server p95: %s\n", kvf(r.System.ServerP95S, " s"))
	fmt.Fprintf(&b, "- Alerts fired: %s\n", strings.Join(orNone(r.System.AlertsFired), ", "))
	fmt.Fprintf(&b, "- Max RSS: %s\n", kvf(r.System.MaxRSSBytes, " B"))
	fmt.Fprintf(&b, "- Max goroutines: %s\n", kvf(r.System.MaxGoroutines, ""))
	for _, e := range r.System.QueryErrors {
		fmt.Fprintf(&b, "- Query error: %s\n", e)
	}
	for _, n := range r.Notes {
		fmt.Fprintf(&b, "\nNote: %s\n", n)
	}
	return b.String()
}

func kv[V int64 | float64](m map[string]V) string {
	if len(m) == 0 {
		return "–"
	}
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	parts := make([]string, 0, len(keys))
	for _, k := range keys {
		parts = append(parts, fmt.Sprintf("%s: %v", k, m[k]))
	}
	return strings.Join(parts, ", ")
}

func kvf(m map[string]float64, unit string) string {
	if len(m) == 0 {
		return "no data"
	}
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	parts := make([]string, 0, len(keys))
	for _, k := range keys {
		parts = append(parts, fmt.Sprintf("%s %.4g%s", k, m[k], unit))
	}
	return strings.Join(parts, ", ")
}
