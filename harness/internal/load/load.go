// Package load is the downstream (buyer) half of the harness: it calls the
// Consumer API like many buyer applications at once, checks every response
// for correctness, and records latency and status distributions.
//
// Arrivals are open-model: each profile fires requests at its RPS whether
// or not earlier ones have finished, up to Concurrency in flight. Arrivals
// that find every worker busy are counted as ClientSaturated, so a report can
// tell a slow system from a harness that could not keep up.
package load

import (
	"context"
	"fmt"
	"io"
	"math"
	"net/http"
	"slices"
	"sort"
	"sync"
	"sync/atomic"
	"time"
)

// Endpoints.
const (
	EndpointStatic   = "static"
	EndpointRealtime = "realtime"
)

// Identity kinds and the statuses each must get.
const (
	KindValid         = "valid"          // 200 / 304
	KindLowQuota      = "low_quota"      // 200 / 304, then 429 with Retry-After
	KindMalformed     = "malformed"      // 403 (unknown or malformed token)
	KindUnknownAgency = "unknown_agency" // 404 agency_not_found
	KindQuotaTest     = "quota_test"     // pushed past its quota on purpose; enforcement is checked (QuotaTracker)
)

// quotaChecked reports whether responses for kind are judged against the
// buyer's quota. Valid buyers stay under it by design (any 429 is a plain
// failure); unknown-agency buyers get 404 before the quota is counted.
func quotaChecked(kind string) bool { return kind == KindLowQuota || kind == KindQuotaTest }

// Identity is one buyer's API key.
type Identity struct {
	Name string `json:"name"`
	Kind string `json:"kind"`
	Key  string `json:"-"`
	// Quota is the grant's quota_per_min (0: not checked).
	Quota int `json:"quota_per_min,omitempty"`
}

// Profile is one stream of traffic.
type Profile struct {
	Name        string
	Endpoint    string
	Agency      string
	RPS         float64
	Concurrency int
	// Conditional sends If-None-Match with the ETag this worker last saw,
	// like a well-behaved buyer; 304 then counts as success.
	Conditional bool
	Identities  []Identity
	// HonorRetryAfter: after a 429 a buyer waits Retry-After before its next
	// request, which must then succeed.
	HonorRetryAfter bool
}

// Phase runs a set of profiles together for Duration.
type Phase struct {
	Name     string
	Duration time.Duration
	Profiles []Profile
	// StartAtSecond (0-59) delays the phase until the wall clock reaches
	// that second of a minute; nil starts at once.
	StartAtSecond *int
}

// Target is the Consumer API under test.
type Target struct {
	BaseURL string
	Client  *http.Client
	// Versions gives the static version real-time responses must report;
	// nil skips that check.
	Versions *VersionTracker
	// Quota mirrors the consumer's per-buyer quota windows; nil makes one.
	Quota *QuotaTracker
}

// Run executes phases in order and returns one result per phase and profile.
func Run(ctx context.Context, t Target, phases []Phase) []PhaseResult {
	if t.Client == nil {
		t.Client = DefaultClient()
	}
	if t.Quota == nil {
		t.Quota = NewQuotaTracker()
	}
	var out []PhaseResult
	for _, ph := range phases {
		if at := ph.StartAtSecond; at != nil && *at >= 0 && *at <= 59 {
			now := time.Now()
			wait := time.Duration((*at-now.Second()+60)%60)*time.Second - time.Duration(now.Nanosecond())
			if wait < 0 {
				wait += time.Minute
			}
			select {
			case <-ctx.Done():
			case <-time.After(wait):
			}
		}
		pctx, cancel := context.WithTimeout(ctx, ph.Duration)
		start := time.Now()
		res := PhaseResult{Name: ph.Name, Start: start}
		var wg sync.WaitGroup
		results := make([]*acc, len(ph.Profiles))
		for i, p := range ph.Profiles {
			results[i] = newAcc(p)
			wg.Add(1)
			go func() {
				defer wg.Done()
				runProfile(pctx, t, p, results[i])
			}()
		}
		wg.Wait()
		cancel()
		res.End = time.Now()
		for _, r := range results {
			res.Profiles = append(res.Profiles, r.finish(res.End.Sub(start)))
		}
		out = append(out, res)
		if ctx.Err() != nil {
			break
		}
	}
	return out
}

// DefaultClient keeps enough idle connections for hundreds of buyers.
func DefaultClient() *http.Client {
	tr := http.DefaultTransport.(*http.Transport).Clone()
	tr.MaxIdleConns, tr.MaxIdleConnsPerHost = 2048, 2048
	return &http.Client{Transport: tr, Timeout: 2 * time.Minute}
}

func runProfile(ctx context.Context, t Target, p Profile, res *acc) {
	if p.RPS <= 0 || p.Concurrency <= 0 || len(p.Identities) == 0 {
		return
	}
	jobs := make(chan Identity)
	var wg, ready sync.WaitGroup
	for range p.Concurrency {
		wg.Add(1)
		ready.Add(1)
		go func() {
			defer wg.Done()
			ready.Done()
			etags := map[string]string{} // identity → last ETag (this worker)
			for id := range jobs {
				res.record(do(ctx, t, p, id, etags))
			}
		}()
	}
	// Arrivals start once every worker is waiting, so the first ones are not
	// counted as harness saturation.
	ready.Wait()
	interval := time.Duration(float64(time.Second) / p.RPS)
	next := time.Now()
	var n int
	for ctx.Err() == nil {
		id := p.Identities[n%len(p.Identities)]
		n++
		if p.HonorRetryAfter && t.Quota.waiting(quotaKey(id, p), time.Now()) {
			res.deferred.Add(1)
			next = next.Add(interval)
			if d := time.Until(next); d > 0 {
				select {
				case <-ctx.Done():
				case <-time.After(d):
				}
			}
			continue
		}
		select {
		case jobs <- id:
		default:
			res.saturated.Add(1)
		}
		next = next.Add(interval)
		if d := time.Until(next); d > 0 {
			select {
			case <-ctx.Done():
			case <-time.After(d):
			}
		}
	}
	close(jobs)
	wg.Wait()
}

func quotaKey(id Identity, p Profile) string { return id.Name + "|" + p.Agency }

// outcome of one request.
type outcome struct {
	quota    *quotaResult
	kind     string
	status   int // 0 = transport error
	latency  time.Duration
	bytes    int64
	failures []string // failed check names
	detail   string   // first failure detail
	dataAge  time.Duration
}

func do(ctx context.Context, t Target, p Profile, id Identity, etags map[string]string) outcome {
	agency := p.Agency
	if id.Kind == KindUnknownAgency {
		agency = "NO_SUCH_AGENCY"
	}
	path := "/v1/agencies/" + agency + "/static"
	if p.Endpoint == EndpointRealtime {
		path = "/v1/agencies/" + agency + "/rt/vehicle_position"
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, t.BaseURL+path, nil)
	if err != nil {
		return outcome{kind: id.Kind, failures: []string{"request"}, detail: err.Error()}
	}
	req.Header.Set("Authorization", "Bearer "+id.Key)
	if p.Conditional {
		if e := etags[id.Name]; e != "" {
			req.Header.Set("If-None-Match", e)
		}
	}
	start := time.Now()
	var qs quotaSend
	checkQuota := quotaChecked(id.Kind) && id.Quota > 0 && t.Quota != nil
	if checkQuota {
		qs = t.Quota.send(quotaKey(id, p), start)
	}
	resp, err := t.Client.Do(req)
	if err != nil {
		if ctx.Err() != nil {
			return outcome{kind: "cancelled"}
		}
		return outcome{kind: id.Kind, latency: time.Since(start), failures: []string{"transport"}, detail: err.Error()}
	}
	body, rerr := io.ReadAll(resp.Body)
	resp.Body.Close()
	o := outcome{kind: id.Kind, status: resp.StatusCode, latency: time.Since(start), bytes: int64(len(body))}
	if rerr != nil {
		if ctx.Err() != nil {
			return outcome{kind: "cancelled"}
		}
		o.failures, o.detail = []string{"read_body"}, rerr.Error()
		return o
	}
	if e := resp.Header.Get("ETag"); e != "" && resp.StatusCode == http.StatusOK {
		etags[id.Name] = e
	}
	o.failures, o.detail, o.dataAge = Check(p.Endpoint, id.Kind, resp, body, t.Versions)
	if checkQuota {
		q := judgeQuota(qs, id.Quota, resp)
		o.quota = &q
		fail := func(name, format string, args ...any) {
			o.failures = append(o.failures, name)
			if o.detail == "" {
				o.detail = name + ": " + fmt.Sprintf(format, args...)
			}
		}
		switch {
		case q.early:
			fail("quota_early_refusal", "%s request %d of %d/min refused at :%02.0f", id.Name, qs.n, id.Quota, qs.second)
		case q.leak:
			fail("quota_leak", "%s request %d of %d/min allowed at :%02.0f", id.Name, qs.n, id.Quota, qs.second)
		}
		if q.retryBad {
			fail("retry_after_bad", "429 Retry-After %q at :%02.0f", resp.Header.Get("Retry-After"), qs.second)
		}
		if q.refusedAfterWait {
			fail("refused_after_retry_after", "%s still refused after waiting Retry-After", id.Name)
		}
		if p.HonorRetryAfter && q.refused && q.retryAfter > 0 {
			// Past the boundary zone, so the next request is clearly in the
			// new window on both clocks.
			t.Quota.block(quotaKey(id, p), start.Add(q.retryAfter+retryAfterMargin))
		}
	}
	return o
}

// ProfileResult is one profile's measurements.
type ProfileResult struct {
	Name     string  `json:"name"`
	Endpoint string  `json:"endpoint"`
	RPS      float64 `json:"target_rps"`

	Requests        int64            `json:"requests"`
	AchievedRPS     float64          `json:"achieved_rps"`
	Statuses        map[string]int64 `json:"statuses"` // "200", "304", "transport_error", …
	ByKind          map[string]int64 `json:"requests_by_identity_kind"`
	Bytes           int64            `json:"bytes"`
	ClientSaturated int64            `json:"client_saturated"`
	CheckFailures   map[string]int64 `json:"check_failures"`
	FailureSamples  []string         `json:"failure_samples,omitempty"`
	// Latency of successful responses (200/304 for valid identities), ms.
	Latency LatencySummary `json:"latency_ms"`
	// Share of valid-identity requests that got neither a 5xx nor a
	// transport error.
	Availability float64        `json:"availability"`
	ServerErrors int64          `json:"server_errors"`
	DataAge      LatencySummary `json:"data_age_s,omitzero"`
	// Quota is set for profiles whose buyers' quota is checked.
	Quota *QuotaSummary `json:"quota,omitempty"`
}

// acc accumulates one profile's outcomes while it runs.
type acc struct {
	mu        sync.Mutex
	r         ProfileResult
	latencies []float64
	ages      []float64
	valid     int64
	validBad  int64
	saturated atomic.Int64
	deferred  atomic.Int64
	quota     *QuotaSummary
}

// LatencySummary holds percentiles.
type LatencySummary struct {
	Count int     `json:"count"`
	P50   float64 `json:"p50"`
	P95   float64 `json:"p95"`
	P99   float64 `json:"p99"`
	Max   float64 `json:"max"`
}

func newAcc(p Profile) *acc {
	a := &acc{r: ProfileResult{Name: p.Name, Endpoint: p.Endpoint, RPS: p.RPS,
		Statuses: map[string]int64{}, ByKind: map[string]int64{}, CheckFailures: map[string]int64{}}}
	for _, id := range p.Identities {
		if quotaChecked(id.Kind) && id.Quota > 0 {
			a.quota = &QuotaSummary{Quota: id.Quota}
			break
		}
	}
	return a
}

func (a *acc) record(o outcome) {
	if o.kind == "cancelled" {
		return
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	r := &a.r
	r.Requests++
	r.ByKind[o.kind]++
	r.Bytes += o.bytes
	key := fmt.Sprint(o.status)
	if o.status == 0 {
		key = "transport_error"
	}
	r.Statuses[key]++
	for _, f := range o.failures {
		r.CheckFailures[f]++
	}
	if len(o.failures) > 0 && len(r.FailureSamples) < 10 {
		r.FailureSamples = append(r.FailureSamples, fmt.Sprintf("%s %s: %s", o.kind, key, o.detail))
	}
	if o.kind == KindValid {
		a.valid++
		if o.status == 0 || o.status >= 500 {
			a.validBad++
		}
		if o.status == http.StatusOK || o.status == http.StatusNotModified {
			a.latencies = append(a.latencies, float64(o.latency.Microseconds())/1000)
		}
	}
	if o.status >= 500 {
		r.ServerErrors++
	}
	if o.dataAge > 0 {
		a.ages = append(a.ages, o.dataAge.Seconds())
	}
	if q := o.quota; q != nil && a.quota != nil {
		s := a.quota
		if q.judged {
			s.Judged++
		}
		if q.boundary {
			s.BoundarySkipped++
		}
		if q.allowed {
			s.Allowed++
		}
		if q.refused {
			s.Refused++
		}
		if q.early {
			s.EarlyRefusals++
		}
		if q.leak {
			s.Leaks++
		}
		if q.retryBad {
			s.RetryAfterBad++
		}
		if q.probe {
			s.RetryProbes++
		}
		if q.refusedAfterWait {
			s.RefusedAfterWait++
		}
	}
}

func (a *acc) finish(elapsed time.Duration) ProfileResult {
	a.mu.Lock()
	defer a.mu.Unlock()
	r := a.r
	r.ClientSaturated = a.saturated.Load()
	if a.quota != nil {
		q := *a.quota
		q.Deferred = a.deferred.Load()
		r.Quota = &q
	}
	if elapsed > 0 {
		r.AchievedRPS = round(float64(r.Requests)/elapsed.Seconds(), 2)
	}
	r.Latency = summarize(a.latencies)
	r.DataAge = summarize(a.ages)
	if a.valid > 0 {
		r.Availability = round(1-float64(a.validBad)/float64(a.valid), 5)
	}
	return r
}

func summarize(v []float64) LatencySummary {
	if len(v) == 0 {
		return LatencySummary{}
	}
	s := slices.Clone(v)
	sort.Float64s(s)
	at := func(q float64) float64 { return round(s[min(int(math.Ceil(q*float64(len(s))))-1, len(s)-1)], 2) }
	return LatencySummary{Count: len(s), P50: at(0.50), P95: at(0.95), P99: at(0.99), Max: round(s[len(s)-1], 2)}
}

func round(v float64, places int) float64 {
	p := math.Pow(10, float64(places))
	return math.Round(v*p) / p
}

// PhaseResult is one phase's results.
type PhaseResult struct {
	Name     string          `json:"name"`
	Start    time.Time       `json:"start"`
	End      time.Time       `json:"end"`
	Profiles []ProfileResult `json:"profiles"`
}
