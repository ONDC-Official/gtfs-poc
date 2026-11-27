package main

// Dev target (`harness run --target dev`): runs a scenario against a
// deployed Transit Hub from outside, playing both sides.
//
//   agency side  creates MOCK_nn agencies through the consumer's agency
//                admin API, hands each one's generated token to the mock,
//                and starts polling; the deployed collector then polls the
//                mock at HARNESS_MOCK_PUBLIC_URL/MOCK_nn.
//   buyer side   creates buyers with grants to the mock agencies and runs
//                the scenario's load against the deployed consumer.
//
// Everything goes through public APIs: no cluster or database access.
// Cleanup (stop polling, purge the agencies, clear the mock) always runs.

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"runtime"
	"slices"
	"strings"
	"time"

	"github.com/ONDC-Official/gtfs-poc/harness/internal/load"
	"github.com/ONDC-Official/gtfs-poc/harness/internal/report"
	"github.com/ONDC-Official/gtfs-poc/harness/internal/scenario"
)

// mockAgencyPrefix: the harness only ever creates or touches these agencies.
const mockAgencyPrefix = "MOCK_"

// bmtcShiftS is BMTC's timestamp quirk (+5 h 30 m), which the mock copies;
// each mock agency is created with the matching vehicle_ts_offset_s.
const bmtcShiftS = 19800

// devConfig is the dev target's settings, from the environment.
type devConfig struct {
	ConsumerURL    string // HARNESS_CONSUMER_URL
	MockPublicURL  string // HARNESS_MOCK_PUBLIC_URL: the mock as the deployed collector reaches it
	MockControlURL string // HARNESS_MOCK_CONTROL_URL
	AdminKey       string // HARNESS_ADMIN_API_KEY
	AllowedHosts   []string
}

// defaultAllowedHosts: the only deployment the harness may target unless
// HARNESS_ALLOWED_HOSTS adds more (e.g. 127.0.0.1 for a local test).
var defaultAllowedHosts = []string{"gtfs-dev.ondc.tech"}

func devConfigFromEnv() (devConfig, error) {
	c := devConfig{
		ConsumerURL:    strings.TrimRight(envOr("HARNESS_CONSUMER_URL", "https://gtfs-dev.ondc.tech"), "/"),
		MockPublicURL:  strings.TrimRight(os.Getenv("HARNESS_MOCK_PUBLIC_URL"), "/"),
		MockControlURL: strings.TrimRight(envOr("HARNESS_MOCK_CONTROL_URL", "http://127.0.0.1:19191"), "/"),
		AdminKey:       strings.TrimSpace(os.Getenv("HARNESS_ADMIN_API_KEY")),
		AllowedHosts:   defaultAllowedHosts,
	}
	if extra := os.Getenv("HARNESS_ALLOWED_HOSTS"); extra != "" {
		for _, h := range strings.Split(extra, ",") {
			if h = strings.ToLower(strings.TrimSpace(h)); h != "" {
				c.AllowedHosts = append(c.AllowedHosts, h)
			}
		}
	}
	var errs []error
	if c.AdminKey == "" {
		errs = append(errs, errors.New("HARNESS_ADMIN_API_KEY is not set (the target consumer's admin key)"))
	}
	if c.MockPublicURL == "" {
		errs = append(errs, errors.New("HARNESS_MOCK_PUBLIC_URL is not set (the mock as the target collector reaches it)"))
	} else if u, err := url.Parse(c.MockPublicURL); err != nil || u.Scheme != "https" || u.Host == "" {
		errs = append(errs, fmt.Errorf("HARNESS_MOCK_PUBLIC_URL %q must be an https URL (the agency API only accepts https feeds)", c.MockPublicURL))
	}
	u, err := url.Parse(c.ConsumerURL)
	if err != nil || u.Host == "" {
		errs = append(errs, fmt.Errorf("HARNESS_CONSUMER_URL %q is not a URL", c.ConsumerURL))
	} else if !slices.Contains(c.AllowedHosts, strings.ToLower(u.Hostname())) {
		errs = append(errs, fmt.Errorf("target %s is not allowed (allowed: %s; add with HARNESS_ALLOWED_HOSTS). The harness never targets production",
			u.Hostname(), strings.Join(c.AllowedHosts, ", ")))
	}
	return c, errors.Join(errs...)
}

func envOr(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}

func mockAgencyIDs(n int) []string {
	ids := make([]string, n)
	for i := range ids {
		ids[i] = fmt.Sprintf("%s%02d", mockAgencyPrefix, i+1)
	}
	return ids
}

// devClient calls the target's admin API and the mock's control API.
type devClient struct {
	cfg  devConfig
	http *http.Client
}

// do sends a JSON request and decodes a JSON response into out (if non-nil).
// It returns the status code; a transport error or an unexpected status is an
// error unless the status is in ok.
func (c *devClient) do(ctx context.Context, method, url string, admin bool, in, out any, ok ...int) (int, error) {
	var body io.Reader
	if in != nil {
		b, _ := json.Marshal(in)
		body = bytes.NewReader(b)
	}
	req, err := http.NewRequestWithContext(ctx, method, url, body)
	if err != nil {
		return 0, err
	}
	req.Header.Set("Content-Type", "application/json")
	if admin {
		req.Header.Set("Authorization", "Bearer "+c.cfg.AdminKey)
	}
	resp, err := c.http.Do(req)
	if err != nil {
		return 0, err
	}
	defer resp.Body.Close()
	b, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if len(ok) == 0 {
		ok = []int{http.StatusOK, http.StatusCreated}
	}
	if !slices.Contains(ok, resp.StatusCode) {
		return resp.StatusCode, fmt.Errorf("%s %s: HTTP %d %s", method, redact(url), resp.StatusCode, strings.TrimSpace(string(b)))
	}
	if out != nil && len(b) > 0 {
		if err := json.Unmarshal(b, out); err != nil {
			return resp.StatusCode, fmt.Errorf("%s %s: %w", method, redact(url), err)
		}
	}
	return resp.StatusCode, nil
}

func redact(u string) string {
	if p, err := url.Parse(u); err == nil {
		p.RawQuery = ""
		return p.String()
	}
	return u
}

func (c *devClient) admin(path string) string { return c.cfg.ConsumerURL + "/v1/admin" + path }
func (c *devClient) mock(path string) string  { return c.cfg.MockControlURL + path }

// preflight checks the target and the mock before anything is created.
func (c *devClient) preflight(ctx context.Context) error {
	if _, err := c.do(ctx, http.MethodGet, c.cfg.ConsumerURL+"/readyz", false, nil, nil); err != nil {
		return fmt.Errorf("target consumer not ready: %w", err)
	}
	var list struct {
		ManagedPrefix string `json:"managed_prefix"`
	}
	code, err := c.do(ctx, http.MethodGet, c.admin("/agencies"), true, nil, &list, http.StatusOK, http.StatusNotFound, http.StatusUnauthorized)
	switch {
	case err != nil:
		return fmt.Errorf("target agency API: %w", err)
	case code == http.StatusUnauthorized:
		return errors.New("target rejected HARNESS_ADMIN_API_KEY (401)")
	case code == http.StatusNotFound:
		return errors.New("target has no agency admin API (404): set ADMIN_AGENCY_ID_PREFIX=MOCK_ on its consumer")
	case list.ManagedPrefix != mockAgencyPrefix:
		return fmt.Errorf("target's agency API manages %q, not %q", list.ManagedPrefix, mockAgencyPrefix)
	}
	if _, err := c.do(ctx, http.MethodGet, c.mock("/_harness/healthz"), false, nil, nil); err != nil {
		return fmt.Errorf("mock control API: %w", err)
	}
	return nil
}

// setupAgency creates one mock agency on the target, registers its token
// with the mock, and starts polling.
func (c *devClient) setupAgency(ctx context.Context, id, runID string, sc *scenario.Scenario) error {
	if !strings.HasPrefix(id, mockAgencyPrefix) {
		return fmt.Errorf("refusing agency %s: not %s", id, mockAgencyPrefix)
	}
	var created struct {
		AuthToken string `json:"auth_token"`
	}
	body := map[string]any{
		"agency_id": id, "name": "perf " + runID + " " + id,
		"feed_base_url":       c.cfg.MockPublicURL + "/" + id,
		"poll_interval_s":     sc.Collector.IntervalS,
		"retention_s":         sc.Collector.RetentionS,
		"vehicle_ts_offset_s": bmtcShiftS,
		"start_polling":       false,
	}
	if _, err := c.do(ctx, http.MethodPost, c.admin("/agencies"), true, body, &created); err != nil {
		return err
	}
	if created.AuthToken == "" {
		return fmt.Errorf("create %s: no auth_token in the response", id)
	}
	shift := bmtcShiftS
	spec := map[string]any{"token": created.AuthToken, "shift_s": shift}
	if sc.Upstream.VehicleEntities > 0 {
		spec["vehicles"] = sc.Upstream.VehicleEntities
	}
	if _, err := c.do(ctx, http.MethodPut, c.mock("/_harness/agencies/"+id), false, spec, nil); err != nil {
		return fmt.Errorf("register %s with the mock: %w", id, err)
	}
	start := map[string]any{"vehicle_positions": true, "static": true, "reason": "perf harness run " + runID}
	if _, err := c.do(ctx, http.MethodPut, c.admin("/agencies/"+id+"/polling"), true, start, nil); err != nil {
		return fmt.Errorf("start polling %s: %w", id, err)
	}
	return nil
}

// cleanupWait is how long cleanup waits between stopping polling and
// purging: one collector agency reload (AGENCY_REFRESH, 30 s on dev) plus a
// poll, so a poll already in flight can't write rows after the purge.
func cleanupWait() time.Duration {
	if d, err := time.ParseDuration(os.Getenv("HARNESS_CLEANUP_WAIT")); err == nil && d >= 0 {
		return d
	}
	return 35 * time.Second
}

// cleanup removes the mock agencies from the target and the mock: stop
// polling for all, wait for the collector to notice, then purge. Purging an
// agency also removes every grant to it, so the run's buyers are left with
// no access. Errors are collected, not fatal: cleanup does as much as it can.
func (c *devClient) cleanup(ctx context.Context, ids []string, wait time.Duration, logf func(string, ...any)) []string {
	var problems []string
	var mock []string
	for _, id := range ids {
		if strings.HasPrefix(id, mockAgencyPrefix) {
			mock = append(mock, id)
		}
	}
	stopped := 0
	for _, id := range mock {
		stop := map[string]any{"vehicle_positions": false, "static": false, "reason": "perf harness cleanup"}
		code, err := c.do(ctx, http.MethodPut, c.admin("/agencies/"+id+"/polling"), true, stop, nil, http.StatusOK, http.StatusNotFound)
		if err != nil {
			problems = append(problems, err.Error())
		}
		if code == http.StatusOK {
			stopped++
		}
	}
	if stopped > 0 && wait > 0 {
		logf("cleanup: polling stopped for %d agenc(ies); waiting %s for the collector to notice before purging", stopped, wait)
		select {
		case <-ctx.Done():
		case <-time.After(wait):
		}
	}
	for _, id := range mock {
		if _, err := c.do(ctx, http.MethodDelete, c.admin("/agencies/"+id+"?purge=true"), true, nil, nil, http.StatusOK, http.StatusNotFound); err != nil {
			problems = append(problems, err.Error())
		}
		if _, err := c.do(ctx, http.MethodDelete, c.mock("/_harness/agencies/"+id), false, nil, nil, http.StatusOK, http.StatusNotFound); err != nil {
			problems = append(problems, err.Error())
		}
		_, _ = c.do(ctx, http.MethodDelete, c.mock("/_harness/faults?agency="+id), false, nil, nil, http.StatusOK, http.StatusNotFound)
	}
	if len(problems) == 0 {
		logf("cleanup done: %d mock agenc(ies) removed from the target and the mock", len(mock))
	} else {
		logf("cleanup finished with %d problem(s); run `harness cleanup` to retry", len(problems))
	}
	return problems
}

// cleanupDev is `harness cleanup --target dev`: removes every MOCK_ agency
// the target lists, and everything the mock holds.
func cleanupDev(ctx context.Context) error {
	cfg, err := devConfigFromEnv()
	if err != nil {
		return err
	}
	c := &devClient{cfg: cfg, http: &http.Client{Timeout: 30 * time.Second}}
	var list struct {
		Agencies []struct {
			AgencyID string `json:"agency_id"`
		} `json:"agencies"`
	}
	if _, err := c.do(ctx, http.MethodGet, c.admin("/agencies"), true, nil, &list); err != nil {
		return err
	}
	var ids []string
	for _, a := range list.Agencies {
		if strings.HasPrefix(a.AgencyID, mockAgencyPrefix) {
			ids = append(ids, a.AgencyID)
		}
	}
	logf := func(format string, args ...any) { fmt.Fprintf(os.Stderr, format+"\n", args...) }
	problems := c.cleanup(ctx, ids, cleanupWait(), logf)
	_, _ = c.do(ctx, http.MethodDelete, c.mock("/_harness/agencies"), false, nil, nil)
	if len(problems) > 0 {
		return errors.New(strings.Join(problems, "; "))
	}
	return nil
}

func runDev(ctx context.Context, path, outDir string) (bool, error) {
	cfg, err := devConfigFromEnv()
	if err != nil {
		return false, err
	}
	sc, err := scenario.Load(path)
	if err != nil {
		return false, err
	}
	if len(sc.FaultSchedule) > 0 {
		for _, step := range sc.FaultSchedule {
			for _, r := range step.Rules {
				if r.Action == "" {
					return false, fmt.Errorf("fault step at %s has a rule without an action", step.At.D())
				}
			}
		}
	}
	runID := sc.ID + "-" + time.Now().Format("20060102-150405")
	logf := func(format string, args ...any) {
		fmt.Fprintf(os.Stderr, "[%s] "+format+"\n", append([]any{time.Now().Format("15:04:05")}, args...)...)
	}
	c := &devClient{cfg: cfg, http: &http.Client{Timeout: 30 * time.Second}}
	target, _ := url.Parse(cfg.ConsumerURL)
	logf("target %s, mock %s, %d mock agenc(ies)", target.Host, cfg.MockPublicURL, sc.AgencyCount)
	if err := c.preflight(ctx); err != nil {
		return false, err
	}

	ids := mockAgencyIDs(sc.AgencyCount)
	// Cleanup runs whatever happens below, on a context of its own so a
	// Ctrl-C still cleans up.
	var cleanedUp bool
	var cleanupProblems []string
	cleanupOnce := func() {
		if cleanedUp {
			return
		}
		cleanedUp = true
		cctx, cancel := context.WithTimeout(context.Background(), 2*time.Minute)
		defer cancel()
		cleanupProblems = c.cleanup(cctx, ids, cleanupWait(), logf)
	}
	defer cleanupOnce()
	// Leftovers from a run that was killed.
	_ = c.cleanup(ctx, ids, cleanupWait(), func(string, ...any) {})

	for _, id := range ids {
		if err := c.setupAgency(ctx, id, runID, sc); err != nil {
			return false, err
		}
	}
	logf("created %s on the target, registered their tokens with the mock, polling started", strings.Join(ids, ", "))

	logf("creating buyer identities through the admin API (grants to the mock agencies only)")
	idents, err := load.CreateIdentities(ctx, cfg.ConsumerURL, cfg.AdminKey, load.IdentitySpec{
		Valid: sc.Identities.Valid, LowQuota: sc.Identities.LowQuota, LowQuotaPerMin: sc.Identities.LowQuotaPerMin,
		Malformed: sc.Identities.Malformed, UnknownAgency: sc.Identities.UnknownAgency,
		Agencies: ids, RunID: runID,
	})
	if err != nil {
		return false, err
	}
	if len(idents[load.KindValid]) == 0 {
		return false, errors.New("the scenario needs at least one valid identity")
	}
	probeKey := idents[load.KindValid][0].Key
	loadAgency := ids[0]
	logf("waiting for %s's data and the new keys to reach the target's buyer API (up to %s)", loadAgency, sc.ReadyTimeout.D())
	readyAfter, err := load.WaitReady(ctx, cfg.ConsumerURL, loadAgency, probeKey, sc.ReadyTimeout.D())
	if err != nil {
		return false, err
	}
	logf("ready after %s", readyAfter.Round(time.Second))

	versions := load.NewVersionTracker()
	tgt := load.Target{BaseURL: cfg.ConsumerURL, Client: load.DefaultClient(), Versions: versions}
	pctx, cancelProbe := context.WithCancel(ctx)
	defer cancelProbe()
	go versions.Probe(pctx, tgt, loadAgency, probeKey, 5*time.Second)
	// Fault steps apply to every mock agency.
	setAll := func(ctx context.Context, rules any) error {
		var errs []error
		for _, id := range ids {
			errs = append(errs, setFaultsAt(ctx, c.mock("/_harness/faults?agency="+id), rules))
		}
		return errors.Join(errs...)
	}
	go runFaultScheduleWith(pctx, sc.FaultSchedule, logf, setAll)
	time.Sleep(time.Second)

	phases := toPhases(sc, idents, loadAgency)
	start := time.Now()
	logf("running %d phase(s), %s in total, against %s", len(phases), sc.TotalDuration(), loadAgency)
	results := load.Run(ctx, tgt, phases)
	end := time.Now()
	elapsed, wall := end.Sub(start), end.Round(0).Sub(start.Round(0))
	suspended := wall-elapsed > 30*time.Second
	start, end = start.Round(0), end.Round(0)
	cancelProbe()
	_ = setAll(context.Background(), nil)

	r := &report.Report{ScenarioID: sc.ID, Description: strings.TrimSpace(sc.Description), RunID: runID,
		Start: start, End: end, Seed: sc.Seed, ReadyAfterS: readyAfter.Seconds(), Phases: results}
	r.Environment = report.Env{Host: hostname(), CPUs: runtime.NumCPU(), CollectorIntervalS: sc.Collector.IntervalS,
		Target: target.Host, HarnessRev: gitRev(".")}
	r.Mock = c.mockStatsAll(ctx, ids)
	cleanupOnce()
	for _, p := range cleanupProblems {
		r.Notes = append(r.Notes, "cleanup: "+p)
	}
	if len(cleanupProblems) > 0 {
		r.Notes = append(r.Notes, "cleanup was incomplete: run `harness cleanup`")
	}
	r.Notes = append(r.Notes,
		fmt.Sprintf("dev target %s: %d mock agencies (%s); load against %s", target.Host, len(ids), strings.Join(ids, ", "), loadAgency),
		"server-side metrics (projector lag, collection success, alerts) aren't visible from outside yet (plan G4); thresholds that need them report no data")
	r.Evaluate(sc.Thresholds)
	if suspended {
		r.Checks = append(r.Checks, report.Check{Name: "run integrity (machine stayed awake)", Limit: "no pause",
			Actual: fmt.Sprintf("paused %s", (wall - elapsed).Round(time.Second)), Passed: false})
		r.Passed = false
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

// mockStatsAll is every mock agency's stats, keyed by agency.
func (c *devClient) mockStatsAll(ctx context.Context, ids []string) json.RawMessage {
	all := map[string]json.RawMessage{}
	for _, id := range ids {
		var raw json.RawMessage
		if _, err := c.do(ctx, http.MethodGet, c.mock("/_harness/stats?agency="+id), false, nil, &raw); err == nil {
			all[id] = raw
		}
	}
	b, _ := json.Marshal(all)
	return b
}
