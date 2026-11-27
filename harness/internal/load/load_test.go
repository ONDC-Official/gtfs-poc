package load

import (
	"archive/zip"
	"bytes"
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/MobilityData/gtfs-realtime-bindings/golang/gtfs"
	"google.golang.org/protobuf/proto"
)

// fakeConsumer behaves like the Consumer API for the checks: keys "good",
// "quota" (429 after 3 requests), anything else 403; agency BMTC only.
type fakeConsumer struct {
	quotaUsed  atomic.Int64
	dupVehicle bool
	version    string
}

func (f *fakeConsumer) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	key := strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer ")
	if key != "good" && key != "quota" {
		http.Error(w, `{"error":"invalid_token"}`, http.StatusForbidden)
		return
	}
	if !strings.HasPrefix(r.URL.Path, "/v1/agencies/BMTC/") {
		http.Error(w, `{"error":"agency_not_found"}`, http.StatusNotFound)
		return
	}
	if key == "quota" && f.quotaUsed.Add(1) > 3 {
		w.Header().Set("Retry-After", "30")
		http.Error(w, `{"error":"quota_exceeded"}`, http.StatusTooManyRequests)
		return
	}
	if strings.HasSuffix(r.URL.Path, "/static") {
		etag := `"s-` + f.version + `"`
		w.Header().Set("ETag", etag)
		w.Header().Set("X-Static-Version", f.version)
		if r.Header.Get("If-None-Match") == etag {
			w.WriteHeader(http.StatusNotModified)
			return
		}
		var buf bytes.Buffer
		zw := zip.NewWriter(&buf)
		for _, n := range RequiredStaticFiles {
			fw, _ := zw.Create(n)
			_, _ = fw.Write([]byte("h\n"))
		}
		_ = zw.Close()
		w.Header().Set("Content-Type", "application/zip")
		_, _ = w.Write(buf.Bytes())
		return
	}
	now := uint64(time.Now().Add(-5 * time.Second).Unix())
	feed := &gtfs.FeedMessage{Header: &gtfs.FeedHeader{GtfsRealtimeVersion: proto.String("2.0")}}
	for _, id := range []string{"KA1", "KA2", "KA3"} {
		feed.Entity = append(feed.Entity, &gtfs.FeedEntity{Id: proto.String(id),
			Vehicle: &gtfs.VehiclePosition{Vehicle: &gtfs.VehicleDescriptor{Id: proto.String(id)}, Timestamp: proto.Uint64(now)}})
	}
	if f.dupVehicle {
		feed.Entity = append(feed.Entity, feed.Entity[0])
	}
	b, _ := proto.Marshal(feed)
	w.Header().Set("ETag", `"rt-1"`)
	w.Header().Set("X-Hub-Static-Version", f.version)
	if r.Header.Get("If-None-Match") == `"rt-1"` {
		w.WriteHeader(http.StatusNotModified)
		return
	}
	w.Header().Set("Content-Type", "application/x-protobuf")
	_, _ = w.Write(b)
}

func TestRunChecksAndCounts(t *testing.T) {
	fc := &fakeConsumer{version: "v1"}
	srv := httptest.NewServer(fc)
	defer srv.Close()
	versions := NewVersionTracker()
	target := Target{BaseURL: srv.URL, Versions: versions}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go versions.Probe(ctx, target, "BMTC", "good", 100*time.Millisecond)
	time.Sleep(150 * time.Millisecond)

	ids := []Identity{
		{Name: "a", Kind: KindValid, Key: "good"},
		{Name: "q", Kind: KindLowQuota, Key: "quota"},
		{Name: "m", Kind: KindMalformed, Key: "garbage"},
		{Name: "u", Kind: KindUnknownAgency, Key: "good"},
	}
	res := Run(ctx, target, []Phase{{Name: "p", Duration: 1500 * time.Millisecond, Profiles: []Profile{
		{Name: "rt", Endpoint: EndpointRealtime, Agency: "BMTC", RPS: 40, Concurrency: 4, Conditional: true, Identities: ids},
		{Name: "st", Endpoint: EndpointStatic, Agency: "BMTC", RPS: 8, Concurrency: 2, Identities: ids[:1]},
	}}})
	rt, st := res[0].Profiles[0], res[0].Profiles[1]
	if len(rt.CheckFailures) != 0 || len(st.CheckFailures) != 0 {
		t.Fatalf("unexpected check failures: rt %v %v / st %v %v", rt.CheckFailures, rt.FailureSamples, st.CheckFailures, st.FailureSamples)
	}
	if rt.Statuses["304"] == 0 || rt.Statuses["403"] == 0 || rt.Statuses["404"] == 0 || rt.Statuses["429"] == 0 {
		t.Errorf("rt statuses: %v", rt.Statuses)
	}
	if rt.Availability != 1 || rt.Latency.Count == 0 || rt.DataAge.Max < 4 || rt.AchievedRPS < 20 {
		t.Errorf("rt summary: avail %v latency %+v age %+v rps %v", rt.Availability, rt.Latency, rt.DataAge, rt.AchievedRPS)
	}
	if st.Statuses["200"] == 0 {
		t.Errorf("static statuses: %v", st.Statuses)
	}
}

func TestChecksCatchBadData(t *testing.T) {
	fc := &fakeConsumer{version: "v2", dupVehicle: true}
	srv := httptest.NewServer(fc)
	defer srv.Close()
	versions := NewVersionTracker()
	versions.Observe("v1") // the harness believes v1 is active
	res := Run(context.Background(), Target{BaseURL: srv.URL, Versions: versions}, []Phase{{Name: "p", Duration: 300 * time.Millisecond,
		Profiles: []Profile{{Name: "rt", Endpoint: EndpointRealtime, Agency: "BMTC", RPS: 20, Concurrency: 2,
			Identities: []Identity{{Name: "a", Kind: KindValid, Key: "good"}, {Name: "x", Kind: KindMalformed, Key: "good"}}}}}})
	f := res[0].Profiles[0].CheckFailures
	if f["duplicate_vehicle"] == 0 || f["static_version_mismatch"] == 0 || f["unexpected_status"] == 0 {
		t.Errorf("failures not detected: %v", f)
	}
}

func TestSummarize(t *testing.T) {
	v := make([]float64, 100)
	for i := range v {
		v[i] = float64(i + 1)
	}
	s := summarize(v)
	if s.P50 != 50 || s.P95 != 95 || s.P99 != 99 || s.Max != 100 || s.Count != 100 {
		t.Errorf("%+v", s)
	}
}
