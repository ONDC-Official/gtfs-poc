package load

import (
	"archive/zip"
	"bytes"
	"context"
	"fmt"
	"net/http"
	"sync"
	"time"

	"github.com/MobilityData/gtfs-realtime-bindings/golang/gtfs"
	"google.golang.org/protobuf/proto"
)

// RequiredStaticFiles must be in every static zip.
var RequiredStaticFiles = []string{"agency.txt", "stops.txt", "routes.txt", "trips.txt", "stop_times.txt", "calendar.txt"}

// Check validates one response against what its identity kind must get. It
// returns failed check names, the first failure's detail, and for real-time
// 200s the age of the freshest vehicle (live-data age).
func Check(endpoint, kind string, resp *http.Response, body []byte, versions *VersionTracker) ([]string, string, time.Duration) {
	var fails []string
	var detail string
	fail := func(name, format string, args ...any) {
		fails = append(fails, name)
		if detail == "" {
			detail = name + ": " + fmt.Sprintf(format, args...)
		}
	}
	code := resp.StatusCode

	switch kind {
	case KindMalformed:
		if code != http.StatusForbidden {
			fail("unexpected_status", "malformed token got %d, want 403", code)
		}
		return fails, detail, 0
	case KindUnknownAgency:
		if code != http.StatusNotFound {
			fail("unexpected_status", "unknown agency got %d, want 404", code)
		}
		return fails, detail, 0
	case KindLowQuota:
		if code == http.StatusTooManyRequests {
			if resp.Header.Get("Retry-After") == "" {
				fail("retry_after_missing", "429 without Retry-After")
			}
			return fails, detail, 0
		}
	}
	if code >= 500 {
		fail("server_error", "HTTP %d: %.200s", code, body)
		return fails, detail, 0
	}
	if code == http.StatusNotModified {
		if len(body) != 0 {
			fail("not_modified_body", "304 with a %d-byte body", len(body))
		}
		if resp.Header.Get("ETag") == "" {
			fail("etag_missing", "304 without ETag")
		}
		return fails, detail, 0
	}
	if code != http.StatusOK {
		fail("unexpected_status", "%s identity got %d: %.200s", kind, code, body)
		return fails, detail, 0
	}
	if resp.Header.Get("ETag") == "" {
		fail("etag_missing", "200 without ETag")
	}

	var age time.Duration
	switch endpoint {
	case EndpointStatic:
		if ct := resp.Header.Get("Content-Type"); ct != "application/zip" {
			fail("content_type", "static Content-Type %q", ct)
		}
		v := resp.Header.Get("X-Static-Version")
		if v == "" {
			fail("version_header_missing", "static without X-Static-Version")
		} else if versions != nil {
			versions.Observe(v)
		}
		zr, err := zip.NewReader(bytes.NewReader(body), int64(len(body)))
		if err != nil {
			fail("zip_invalid", "%v", err)
			break
		}
		have := map[string]bool{}
		for _, f := range zr.File {
			have[f.Name] = true
		}
		for _, name := range RequiredStaticFiles {
			if !have[name] {
				fail("zip_missing_file", "%s missing from the zip", name)
			}
		}
	case EndpointRealtime:
		if ct := resp.Header.Get("Content-Type"); ct != "application/x-protobuf" {
			fail("content_type", "real-time Content-Type %q", ct)
		}
		var feed gtfs.FeedMessage
		if err := proto.Unmarshal(body, &feed); err != nil {
			fail("protobuf_invalid", "%v", err)
			break
		}
		if feed.GetHeader().GetGtfsRealtimeVersion() == "" {
			fail("feed_header_missing", "no gtfs_realtime_version")
		}
		seen := map[string]bool{}
		var newest uint64
		for _, e := range feed.Entity {
			id := e.GetVehicle().GetVehicle().GetId()
			if id == "" {
				fail("vehicle_id_missing", "entity %q without vehicle.vehicle.id", e.GetId())
				continue
			}
			if seen[id] {
				fail("duplicate_vehicle", "vehicle %s served twice", id)
			}
			seen[id] = true
			newest = max(newest, e.GetVehicle().GetTimestamp())
		}
		if newest > 0 {
			age = time.Since(time.Unix(int64(newest), 0))
		}
		hv := resp.Header.Get("X-Hub-Static-Version")
		if versions != nil {
			if want := versions.Current(); want != "" && hv != want && !versions.Recent(hv) {
				fail("static_version_mismatch", "X-Hub-Static-Version %q, active static %q", hv, want)
			}
		}
	}
	return fails, detail, age
}

// VersionTracker follows the active static version, so real-time responses
// can be checked against it. A version stays "recent" for a grace period
// after it is replaced, since real-time responses switch over a few seconds
// after the static one does.
type VersionTracker struct {
	mu      sync.Mutex
	current string
	prev    map[string]time.Time
	Grace   time.Duration
}

// NewVersionTracker returns a tracker with a 30 s grace period.
func NewVersionTracker() *VersionTracker {
	return &VersionTracker{prev: map[string]time.Time{}, Grace: 30 * time.Second}
}

// Observe records v as the active version.
func (v *VersionTracker) Observe(version string) {
	v.mu.Lock()
	defer v.mu.Unlock()
	if version == v.current || version == "" {
		return
	}
	if v.current != "" {
		v.prev[v.current] = time.Now()
	}
	v.current = version
}

// Current is the latest observed version.
func (v *VersionTracker) Current() string {
	v.mu.Lock()
	defer v.mu.Unlock()
	return v.current
}

// Recent reports whether version was replaced within the grace period.
func (v *VersionTracker) Recent(version string) bool {
	v.mu.Lock()
	defer v.mu.Unlock()
	t, ok := v.prev[version]
	return ok && time.Since(t) < v.Grace
}

// Probe polls the static endpoint every interval (conditional, so cheap)
// and records the active version, until ctx ends.
func (v *VersionTracker) Probe(ctx context.Context, t Target, agency, key string, every time.Duration) {
	client := t.Client
	if client == nil {
		client = DefaultClient()
	}
	etag := ""
	for {
		req, _ := http.NewRequestWithContext(ctx, http.MethodGet, t.BaseURL+"/v1/agencies/"+agency+"/static", nil)
		req.Header.Set("Authorization", "Bearer "+key)
		if etag != "" {
			req.Header.Set("If-None-Match", etag)
		}
		if resp, err := client.Do(req); err == nil {
			buf := make([]byte, 32<<10)
			for {
				if _, err := resp.Body.Read(buf); err != nil {
					break
				}
			}
			resp.Body.Close()
			if resp.StatusCode == http.StatusOK || resp.StatusCode == http.StatusNotModified {
				v.Observe(resp.Header.Get("X-Static-Version"))
				etag = resp.Header.Get("ETag")
			}
		}
		select {
		case <-ctx.Done():
			return
		case <-time.After(every):
		}
	}
}
