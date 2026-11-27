package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/ONDC-Official/gtfs-poc/harness/internal/load"
	"github.com/ONDC-Official/gtfs-poc/harness/internal/prom"
	"github.com/ONDC-Official/gtfs-poc/harness/internal/report"
	"github.com/ONDC-Official/gtfs-poc/harness/internal/scenario"
	"github.com/ONDC-Official/gtfs-poc/harness/internal/stack"
)

const agency = "BMTC"

func run(ctx context.Context, st stack.Stack, path, outDir string) (bool, error) {
	sc, err := scenario.Load(path)
	if err != nil {
		return false, err
	}
	state, err := st.LoadState()
	if err != nil {
		return false, err
	}
	runID := sc.ID + "-" + time.Now().Format("20060102-150405")
	logf := func(format string, args ...any) {
		fmt.Fprintf(os.Stderr, "[%s] "+format+"\n", append([]any{time.Now().Format("15:04:05")}, args...)...)
	}

	// Apply the scenario's upstream and collector settings; compose only
	// recreates the services whose settings changed.
	if err := st.WriteEnv(state, envFor(sc)); err != nil {
		return false, err
	}
	logf("applying scenario settings (collector every %d s, %d vehicles)", sc.Collector.IntervalS, sc.Upstream.VehicleEntities)
	if err := st.Compose(ctx, "up", "-d", "--wait"); err != nil {
		return false, fmt.Errorf("compose up: %w (did you run `harness up`?)", err)
	}
	if err := st.CheckIsolation(ctx, state); err != nil {
		return false, err
	}
	for _, u := range []string{stack.CollectorURL + "/readyz", stack.ConsumerURL + "/readyz"} {
		if err := stack.WaitHTTP(ctx, u, 3*time.Minute); err != nil {
			return false, err
		}
	}
	if err := setFaults(ctx, nil); err != nil {
		return false, err
	}

	logf("creating buyer identities through the admin API")
	ids, err := load.CreateIdentities(ctx, stack.ConsumerURL, state.AdminKey, load.IdentitySpec{
		Valid: sc.Identities.Valid, LowQuota: sc.Identities.LowQuota, LowQuotaPerMin: sc.Identities.LowQuotaPerMin,
		Malformed: sc.Identities.Malformed, UnknownAgency: sc.Identities.UnknownAgency, Agency: agency, RunID: runID,
	})
	if err != nil {
		return false, err
	}
	probeKey := ids[load.KindValid][0].Key
	logf("waiting for data and the new keys to reach read_db (up to %s)", sc.ReadyTimeout.D())
	readyAfter, err := load.WaitReady(ctx, stack.ConsumerURL, agency, probeKey, sc.ReadyTimeout.D())
	if err != nil {
		return false, err
	}
	logf("ready after %s", readyAfter.Round(time.Second))

	versions := load.NewVersionTracker()
	target := load.Target{BaseURL: stack.ConsumerURL, Client: load.DefaultClient(), Versions: versions}
	pctx, cancelProbe := context.WithCancel(ctx)
	defer cancelProbe()
	go versions.Probe(pctx, target, agency, probeKey, 5*time.Second)
	go runFaultSchedule(pctx, sc.FaultSchedule, logf)
	time.Sleep(time.Second) // first version observation

	phases := toPhases(sc, ids)
	start := time.Now()
	logf("running %d phase(s), %s in total", len(phases), sc.TotalDuration())
	results := load.Run(ctx, target, phases)
	end := time.Now()
	// A sleeping laptop pauses the harness but not the wall clock: the
	// monotonic and wall-clock durations then differ, and the run is invalid.
	elapsed, wall := end.Sub(start), end.Round(0).Sub(start.Round(0))
	suspended := wall-elapsed > 30*time.Second
	start, end = start.Round(0), end.Round(0) // wall clock from here on (Prometheus)
	cancelProbe()
	_ = setFaults(ctx, nil)
	logf("load finished; waiting for a final Prometheus scrape")
	time.Sleep(12 * time.Second)

	r := &report.Report{ScenarioID: sc.ID, Description: strings.TrimSpace(sc.Description), RunID: runID,
		Start: start, End: end, Seed: sc.Seed, ReadyAfterS: readyAfter.Seconds(), Phases: results}
	r.Environment = report.Env{Host: hostname(), CPUs: runtime.NumCPU(),
		CollectorSrc: state.CollectorSrc, CollectorRev: gitRev(state.CollectorSrc),
		ConsumerSrc: state.ConsumerSrc, ConsumerRev: gitRev(state.ConsumerSrc),
		HarnessRev: gitRev(st.Root), CollectorIntervalS: sc.Collector.IntervalS}
	r.System = systemMetrics(ctx, start, end)
	r.Mock = mockStats(ctx)
	if sc.Collector.IntervalS > int(end.Sub(start).Seconds()) {
		r.Notes = append(r.Notes, "the measured window is shorter than one collector interval, so collection success may have no data")
	}
	r.Evaluate(sc.Thresholds)
	if suspended {
		r.Checks = append(r.Checks, report.Check{Name: "run integrity (machine stayed awake)", Limit: "no pause",
			Actual: fmt.Sprintf("paused %s", (wall - elapsed).Round(time.Second)), Passed: false})
		r.Passed = false
		r.Notes = append(r.Notes, "the machine slept or paused during the run; results are not valid, rerun with the machine kept awake")
	}

	dir := filepath.Join(outDir, runID)
	if err := r.Write(dir); err != nil {
		return false, err
	}
	verdict := "PASS"
	if !r.Passed {
		verdict = "FAIL"
	}
	logf("%s: %s — %s", sc.ID, verdict, filepath.Join(dir, "report.md"))
	return r.Passed, nil
}

func envFor(sc *scenario.Scenario) map[string]string {
	env := map[string]string{
		"SEED":                      strconv.FormatUint(sc.Seed, 10),
		"COLLECTOR_POLL_INTERVAL_S": strconv.Itoa(sc.Collector.IntervalS),
		"COLLECTOR_RETENTION_S":     strconv.Itoa(sc.Collector.RetentionS),
		"PROJECTOR_INTERVAL":        sc.Collector.ProjectorInterval.D().String(),
		"TRIP_UPDATES_ENABLED":      strconv.FormatBool(sc.Collector.TripUpdatesEnabled),
	}
	set := func(k string, v int) {
		if v > 0 {
			env[k] = strconv.Itoa(v)
		}
	}
	u := sc.Upstream
	set("MOCK_VEHICLES", u.VehicleEntities)
	set("MOCK_TRIPS_PER_ZONE", u.TripsPerZone)
	set("MOCK_STOPS_PER_TRIP", u.StopsPerTrip)
	set("MOCK_STATIC_STOPS", u.StaticStops)
	set("MOCK_STATIC_ROUTES", u.StaticRoutes)
	set("MOCK_STATIC_TRIPS_PER_ROUTE", u.StaticTripsPerRoute)
	set("MOCK_STATIC_STOPS_PER_TRIP", u.StaticStopsPerTrip)
	return env
}

func toPhases(sc *scenario.Scenario, ids map[string][]load.Identity) []load.Phase {
	var out []load.Phase
	for _, ph := range sc.Phases {
		p := load.Phase{Name: ph.Name, Duration: ph.Duration.D()}
		for _, pr := range ph.Profiles {
			var pool []load.Identity
			for _, kind := range pr.Identities {
				pool = append(pool, ids[kind]...)
			}
			p.Profiles = append(p.Profiles, load.Profile{Name: pr.Name, Endpoint: pr.Endpoint, Agency: agency,
				RPS: pr.RPS, Concurrency: pr.Concurrency, Conditional: pr.Conditional, Identities: pool})
		}
		out = append(out, p)
	}
	return out
}

func setFaults(ctx context.Context, rules any) error {
	method, body := http.MethodDelete, io.Reader(nil)
	if rules != nil {
		b, _ := json.Marshal(rules)
		method, body = http.MethodPut, bytes.NewReader(b)
	}
	req, _ := http.NewRequestWithContext(ctx, method, stack.MockControlURL+"/_harness/faults", body)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return fmt.Errorf("mock control API: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		b, _ := io.ReadAll(resp.Body)
		return fmt.Errorf("set faults: HTTP %d %s", resp.StatusCode, b)
	}
	return nil
}

func runFaultSchedule(ctx context.Context, steps []scenario.FaultStep, logf func(string, ...any)) {
	sort.Slice(steps, func(i, j int) bool { return steps[i].At < steps[j].At })
	start := time.Now()
	for _, s := range steps {
		select {
		case <-ctx.Done():
			return
		case <-time.After(time.Until(start.Add(s.At.D()))):
		}
		var rules any
		if len(s.Rules) > 0 {
			rules = s.Rules
		}
		if err := setFaults(ctx, rules); err != nil {
			logf("fault step at %s failed: %v", s.At.D(), err)
			continue
		}
		logf("fault step at %s: %d rule(s)", s.At.D(), len(s.Rules))
	}
}

func systemMetrics(ctx context.Context, start, end time.Time) report.System {
	pc := &prom.Client{BaseURL: stack.PrometheusURL}
	w := prom.Window(end.Sub(start))
	var sys report.System
	scalar := func(expr string) *float64 {
		v, ok, err := pc.Scalar(ctx, expr, end)
		if err != nil {
			sys.QueryErrors = append(sys.QueryErrors, err.Error())
			return nil
		}
		if !ok {
			return nil
		}
		return &v
	}
	byLabel := func(expr, label string) map[string]float64 {
		s, err := pc.Query(ctx, expr, end)
		if err != nil {
			sys.QueryErrors = append(sys.QueryErrors, err.Error())
			return nil
		}
		m := map[string]float64{}
		for _, x := range s {
			m[x.Labels[label]] = x.Value
		}
		return m
	}
	sys.ProjectorLagMaxS = scalar(fmt.Sprintf(`max(max_over_time(projector_lag_seconds[%s]))`, w))
	sys.VehicleCollectionSuccess = scalar(fmt.Sprintf(
		`sum(increase(collector_fetch_total{feed="vehicle_positions",status="ok"}[%[1]s])) / sum(increase(collector_fetch_total{feed="vehicle_positions"}[%[1]s]))`, w))
	sys.ServerErrorRate = scalar(fmt.Sprintf(
		`(sum(increase(consumer_requests_total{endpoint=~"static|rt_vehicle_position",status=~"5.."}[%[1]s])) or vector(0)) / sum(increase(consumer_requests_total{endpoint=~"static|rt_vehicle_position"}[%[1]s]))`, w))
	sys.ServerP95S = byLabel(fmt.Sprintf(
		`histogram_quantile(0.95, sum by (le, endpoint) (increase(consumer_request_duration_seconds_bucket{endpoint=~"static|rt_vehicle_position"}[%s])))`, w), "endpoint")
	sys.MaxRSSBytes = byLabel(fmt.Sprintf(`max by (job) (max_over_time(process_resident_memory_bytes[%s]))`, w), "job")
	sys.MaxGoroutines = byLabel(fmt.Sprintf(`max by (job) (max_over_time(go_goroutines[%s]))`, w), "job")
	for name := range byLabel(fmt.Sprintf(`max by (alertname) (max_over_time(ALERTS{alertstate="firing"}[%s]))`, w), "alertname") {
		sys.AlertsFired = append(sys.AlertsFired, name)
	}
	sort.Strings(sys.AlertsFired)
	return sys
}

func mockStats(ctx context.Context) json.RawMessage {
	req, _ := http.NewRequestWithContext(ctx, http.MethodGet, stack.MockControlURL+"/_harness/stats", nil)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return nil
	}
	defer resp.Body.Close()
	b, _ := io.ReadAll(resp.Body)
	if !json.Valid(b) {
		return nil
	}
	return b
}

func gitRev(dir string) string {
	out, err := exec.Command("git", "-C", dir, "describe", "--always", "--dirty").Output()
	if err != nil {
		return "unknown"
	}
	return strings.TrimSpace(string(out))
}

func hostname() string {
	h, _ := os.Hostname()
	return h
}
