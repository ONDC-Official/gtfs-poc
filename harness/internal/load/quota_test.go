package load

import (
	"context"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/MobilityData/gtfs-realtime-bindings/golang/gtfs"
	"google.golang.org/protobuf/proto"
)

// quotaConsumer enforces a per-key fixed one-minute quota like the real
// consumer; allow shifts where it starts refusing (0 = correct, +n leaks,
// -n refuses early).
type quotaConsumer struct {
	mu     sync.Mutex
	quota  int
	allow  int
	counts map[string]int
	badRA  bool
}

func (q *quotaConsumer) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	key := strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer ")
	now := time.Now()
	q.mu.Lock()
	k := fmt.Sprintf("%s|%d", key, now.Unix()/60)
	q.counts[k]++
	n := q.counts[k]
	q.mu.Unlock()
	if n > q.quota+q.allow {
		left := 60 - now.Second()
		if q.badRA {
			left = 3600
		}
		w.Header().Set("Retry-After", strconv.Itoa(left))
		http.Error(w, `{"error":"quota_exceeded"}`, http.StatusTooManyRequests)
		return
	}
	feed := &gtfs.FeedMessage{Header: &gtfs.FeedHeader{GtfsRealtimeVersion: proto.String("2.0")}}
	feed.Entity = append(feed.Entity, &gtfs.FeedEntity{Id: proto.String("KA1"),
		Vehicle: &gtfs.VehiclePosition{Vehicle: &gtfs.VehicleDescriptor{Id: proto.String("KA1")}, Timestamp: proto.Uint64(uint64(now.Unix()))}})
	b, _ := proto.Marshal(feed)
	w.Header().Set("ETag", `"rt"`)
	w.Header().Set("Content-Type", "application/x-protobuf")
	_, _ = w.Write(b)
}

// awayFromBoundary waits until the clock is at least need before the next
// minute boundary, so a short run stays in one quota window.
func awayFromBoundary(t *testing.T, need time.Duration) {
	t.Helper()
	for {
		now := time.Now()
		left := time.Duration(60-now.Second())*time.Second - time.Duration(now.Nanosecond())
		if left > need && now.Second() >= 3 {
			return
		}
		time.Sleep(left + 3500*time.Millisecond)
	}
}

func runQuota(t *testing.T, qc *quotaConsumer, sends int) QuotaSummary {
	t.Helper()
	srv := httptest.NewServer(qc)
	defer srv.Close()
	awayFromBoundary(t, 12*time.Second)
	p := Profile{Name: "q", Endpoint: EndpointRealtime, Agency: "BMTC", RPS: float64(sends) / 3, Concurrency: 4,
		Identities: []Identity{{Name: "qt0", Kind: KindQuotaTest, Key: "k0", Quota: qc.quota}}}
	res := Run(context.Background(), Target{BaseURL: srv.URL}, []Phase{{Name: "p", Duration: 3 * time.Second, Profiles: []Profile{p}}})
	q := res[0].Profiles[0].Quota
	if q == nil {
		t.Fatal("no quota summary")
	}
	return *q
}

func TestQuotaEnforcedCorrectly(t *testing.T) {
	q := runQuota(t, &quotaConsumer{quota: 20, counts: map[string]int{}}, 40)
	if q.Refused == 0 || q.Allowed < 17 || q.Violations() != 0 {
		t.Fatalf("correct consumer: %+v", q)
	}
}

func TestQuotaLeakDetected(t *testing.T) {
	q := runQuota(t, &quotaConsumer{quota: 20, allow: 10, counts: map[string]int{}}, 40)
	if q.Leaks == 0 {
		t.Fatalf("leak not caught: %+v", q)
	}
}

func TestQuotaEarlyRefusalDetected(t *testing.T) {
	q := runQuota(t, &quotaConsumer{quota: 20, allow: -10, counts: map[string]int{}}, 40)
	if q.EarlyRefusals == 0 {
		t.Fatalf("early refusal not caught: %+v", q)
	}
}

func TestBadRetryAfterDetected(t *testing.T) {
	q := runQuota(t, &quotaConsumer{quota: 20, badRA: true, counts: map[string]int{}}, 40)
	if q.RetryAfterBad == 0 {
		t.Fatalf("bad Retry-After not caught: %+v", q)
	}
}

func TestJudgeQuotaBoundaryAndTolerance(t *testing.T) {
	ok := &http.Response{StatusCode: 200, Header: http.Header{}}
	tooMany := &http.Response{StatusCode: 429, Header: http.Header{"Retry-After": {"30"}}}
	cases := []struct {
		s                        quotaSend
		resp                     *http.Response
		early, leak, judged, bad bool
	}{
		{quotaSend{n: 10, second: 30}, tooMany, true, false, true, false},  // refused far below quota
		{quotaSend{n: 99, second: 30}, tooMany, false, false, true, false}, // within tolerance
		{quotaSend{n: 150, second: 30}, ok, false, true, true, false},      // allowed far above quota
		{quotaSend{n: 102, second: 30}, ok, false, false, true, false},     // within tolerance
		{quotaSend{n: 150, second: 59.5, boundary: true}, ok, false, false, false, false},
		{quotaSend{n: 150, second: 55}, &http.Response{StatusCode: 429, Header: http.Header{"Retry-After": {"40"}}}, false, false, true, true}, // 40 s left? only 5
	}
	for i, c := range cases {
		r := judgeQuota(c.s, 100, c.resp)
		if r.early != c.early || r.leak != c.leak || r.judged != c.judged || r.retryBad != c.bad {
			t.Errorf("case %d: %+v", i, r)
		}
	}
}

func TestHonorRetryAfter(t *testing.T) {
	q := NewQuotaTracker()
	now := time.Now()
	q.block("b|A", now.Add(2*time.Second))
	if !q.waiting("b|A", now.Add(time.Second)) {
		t.Fatal("not waiting during Retry-After")
	}
	if q.waiting("b|A", now.Add(3*time.Second)) {
		t.Fatal("still waiting after Retry-After")
	}
	if s := q.send("b|A", now.Add(3*time.Second)); !s.probe {
		t.Fatal("first send after Retry-After not marked as the probe")
	}
	if s := q.send("b|A", now.Add(4*time.Second)); s.probe {
		t.Fatal("probe flag stuck")
	}
}
