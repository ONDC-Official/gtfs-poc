package load

import (
	"net/http"
	"strconv"
	"sync"
	"time"
)

// Quota checks (TC-9). The consumer counts each buyer's requests per agency
// in fixed one-minute windows (unix minutes) and answers 429 with
// Retry-After (seconds left in the window) above the grant's quota. The
// tracker mirrors that count on the harness side, by send time, and judges
// each response:
//
//	request n <= quota - tolerance, got 429  -> quota_early_refusal
//	request n >  quota + tolerance, got 200  -> quota_leak
//	429 without a sane Retry-After           -> retry_after_bad
//	429 again after waiting Retry-After      -> refused_after_retry_after
//
// Requests sent within quotaBoundary of a minute boundary aren't judged:
// the harness and server clocks and the network delay make their window
// ambiguous. QuotaTolerance absorbs requests in flight at the same time.

const (
	quotaBoundary  = 2 * time.Second
	QuotaTolerance = 3
	// retryAfterMargin is added to Retry-After before the next request.
	retryAfterMargin = quotaBoundary + 500*time.Millisecond
)

// QuotaTracker counts sends per buyer and agency, per unix minute.
type QuotaTracker struct {
	mu      sync.Mutex
	counts  map[string]map[int64]int
	blocked map[string]time.Time // buyer waiting Retry-After until
	probe   map[string]bool      // next send is the one after Retry-After
}

func NewQuotaTracker() *QuotaTracker {
	return &QuotaTracker{counts: map[string]map[int64]int{}, blocked: map[string]time.Time{}, probe: map[string]bool{}}
}

// quotaSend is what the tracker knew when a request was sent.
type quotaSend struct {
	n        int     // this request's number in its window
	second   float64 // seconds into the minute at send
	boundary bool
	probe    bool // first request after waiting Retry-After
}

func (q *QuotaTracker) send(key string, at time.Time) quotaSend {
	q.mu.Lock()
	defer q.mu.Unlock()
	w := at.Unix() / 60
	m := q.counts[key]
	if m == nil {
		m = map[int64]int{}
		q.counts[key] = m
	}
	for old := range m {
		if old < w-1 {
			delete(m, old)
		}
	}
	m[w]++
	sec := float64(at.UnixNano()%int64(time.Minute)) / float64(time.Second)
	s := quotaSend{n: m[w], second: sec,
		boundary: sec < quotaBoundary.Seconds() || sec > 60-quotaBoundary.Seconds(),
		probe:    q.probe[key]}
	delete(q.probe, key)
	return s
}

// waiting reports whether the buyer is still waiting out a Retry-After.
func (q *QuotaTracker) waiting(key string, now time.Time) bool {
	q.mu.Lock()
	defer q.mu.Unlock()
	until, ok := q.blocked[key]
	if !ok {
		return false
	}
	if now.Before(until) {
		return true
	}
	delete(q.blocked, key)
	q.probe[key] = true
	return false
}

func (q *QuotaTracker) block(key string, until time.Time) {
	q.mu.Lock()
	defer q.mu.Unlock()
	if until.After(q.blocked[key]) {
		q.blocked[key] = until
	}
}

// QuotaSummary is one profile's quota results.
type QuotaSummary struct {
	Quota            int   `json:"quota_per_min"`
	Judged           int64 `json:"judged"`
	Allowed          int64 `json:"allowed"` // 200/304
	Refused          int64 `json:"refused"` // 429
	BoundarySkipped  int64 `json:"boundary_skipped"`
	EarlyRefusals    int64 `json:"early_refusals"`
	Leaks            int64 `json:"leaks"`
	RetryAfterBad    int64 `json:"retry_after_bad"`
	RetryProbes      int64 `json:"retry_probes"`
	RefusedAfterWait int64 `json:"refused_after_retry_after"`
	Deferred         int64 `json:"deferred_waiting_retry_after"`
}

// Violations is everything that counts against the consumer.
func (s QuotaSummary) Violations() int64 {
	return s.EarlyRefusals + s.Leaks + s.RetryAfterBad + s.RefusedAfterWait
}

// quotaResult is the tracker's verdict on one response.
type quotaResult struct {
	judged, allowed, refused, boundary, early, leak, retryBad, probe, refusedAfterWait bool
	retryAfter                                                                         time.Duration
}

func judgeQuota(s quotaSend, quota int, resp *http.Response) quotaResult {
	var r quotaResult
	code := resp.StatusCode
	r.allowed = code == http.StatusOK || code == http.StatusNotModified
	r.refused = code == http.StatusTooManyRequests
	if r.refused {
		ra, err := strconv.Atoi(resp.Header.Get("Retry-After"))
		left := 60 - s.second
		if err != nil || ra < 1 || float64(ra) > left+quotaBoundary.Seconds()+1 {
			r.retryBad = true
		} else {
			r.retryAfter = time.Duration(ra) * time.Second
		}
	}
	r.probe = s.probe
	// The request after waiting Retry-After is always judged: it waited the
	// server's own figure plus a margin, so a 429 is the consumer's fault.
	if s.probe && r.refused {
		r.refusedAfterWait = true
	}
	if s.boundary {
		r.boundary = true
		return r
	}
	if !r.allowed && !r.refused {
		return r // other statuses are Check's business
	}
	r.judged = true
	switch {
	case r.refused && s.n <= quota-QuotaTolerance:
		r.early = true
	case r.allowed && s.n > quota+QuotaTolerance:
		r.leak = true
	}
	return r
}
