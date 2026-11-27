package mock

import (
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/ONDC-Official/gtfs-poc/harness/internal/faults"
)

func newServer(t *testing.T, cfg Config) (*Server, *httptest.Server, *httptest.Server) {
	t.Helper()
	s := New(cfg, slog.New(slog.DiscardHandler))
	feed := httptest.NewServer(s.FeedHandler())
	ctl := httptest.NewServer(s.ControlHandler())
	t.Cleanup(func() { feed.Close(); ctl.Close() })
	return s, feed, ctl
}

func post(t *testing.T, url, token, body string) (*http.Response, []byte, error) {
	t.Helper()
	req, _ := http.NewRequest(http.MethodPost, url, strings.NewReader(body))
	req.Header.Set("authToken", token)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return nil, nil, err
	}
	defer resp.Body.Close()
	b, err := io.ReadAll(resp.Body)
	return resp, b, err
}

func small() Config {
	c := Defaults()
	c.Vehicles, c.TripsPerZone, c.StopsPerTrip = 200, 20, 5
	c.Static.Stops, c.Static.Routes, c.Static.TripsPerRoute = 50, 10, 2
	return c
}

func TestServesBMTCContract(t *testing.T) {
	_, feed, ctl := newServer(t, small())

	resp, _, _ := post(t, feed.URL+PathVehicles, "wrong", `{}`)
	if resp.StatusCode != http.StatusUnauthorized {
		t.Fatalf("bad token: %d", resp.StatusCode)
	}
	resp, body, _ := post(t, feed.URL+PathVehicles, "perf-token", `{}`)
	var env struct {
		IsSuccess bool `json:"IsSuccess"`
		RowCount  int  `json:"RowCount"`
	}
	if err := json.Unmarshal(body, &env); err != nil || resp.StatusCode != 200 || !env.IsSuccess || env.RowCount < 200 {
		t.Fatalf("vehicles: %d %v %+v", resp.StatusCode, err, env)
	}
	if resp, _, _ := post(t, feed.URL+PathTripUpdates, "perf-token", `{"zoneid": 3}`); resp.StatusCode != 200 {
		t.Fatalf("trips: %d", resp.StatusCode)
	}
	if resp, _, _ := post(t, feed.URL+PathTripUpdates, "perf-token", `{"zoneid": 9}`); resp.StatusCode != 400 {
		t.Fatalf("bad zone: %d", resp.StatusCode)
	}
	_, body, _ = post(t, feed.URL+PathStatic, "perf-token", `{"check": 0}`)
	if !strings.Contains(string(body), `"issuccess":true`) {
		t.Fatalf("static: %.200s", body)
	}

	r, _ := http.Get(ctl.URL + "/_harness/stats")
	var stats struct {
		Endpoints map[string]EndpointStats `json:"endpoints"`
	}
	_ = json.NewDecoder(r.Body).Decode(&stats)
	r.Body.Close()
	if v := stats.Endpoints["vehicles"]; v.Requests != 2 || v.Outcomes["ok"] != 1 || v.Outcomes["unauthorized"] != 1 || v.Entities < 200 {
		t.Errorf("vehicle stats: %+v", v)
	}
}

func TestFaults(t *testing.T) {
	s, feed, ctl := newServer(t, small())

	put := func(body string) int {
		req, _ := http.NewRequest(http.MethodPut, ctl.URL+"/_harness/faults", strings.NewReader(body))
		resp, err := http.DefaultClient.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		resp.Body.Close()
		return resp.StatusCode
	}
	if code := put(`[{"endpoint":"vehicles","action":"status","status":200}]`); code != 400 {
		t.Fatalf("invalid rule accepted: %d", code)
	}
	if code := put(`[
		{"endpoint":"vehicles","action":"status","status":429,"count":1},
		{"endpoint":"trips","zone":2,"action":"drop"},
		{"endpoint":"static","action":"malformed","mode":"truncate","count":1},
		{"endpoint":"vehicles","action":"delay","delay":"300ms","count":1}
	]`); code != 200 {
		t.Fatalf("set faults: %d", code)
	}

	if resp, _, _ := post(t, feed.URL+PathVehicles, "perf-token", `{}`); resp.StatusCode != 429 {
		t.Errorf("429 fault: %d", resp.StatusCode)
	}
	start := time.Now()
	if resp, _, _ := post(t, feed.URL+PathVehicles, "perf-token", `{}`); resp.StatusCode != 200 || time.Since(start) < 300*time.Millisecond {
		t.Errorf("delay fault: %d after %v", resp.StatusCode, time.Since(start))
	}
	if _, _, err := post(t, feed.URL+PathTripUpdates, "perf-token", `{"zoneid": 2}`); err == nil {
		t.Error("drop fault: got a response")
	}
	if resp, _, _ := post(t, feed.URL+PathTripUpdates, "perf-token", `{"zoneid": 1}`); resp.StatusCode != 200 {
		t.Errorf("other zone affected: %d", resp.StatusCode)
	}
	_, body, _ := post(t, feed.URL+PathStatic, "perf-token", `{"check": 1}`)
	if json.Valid(body) {
		t.Error("truncate fault: body is still valid JSON")
	}
	if got := s.Faults().Rules(); len(got) != 4 {
		t.Errorf("rules: %d", len(got))
	}

	req, _ := http.NewRequest(http.MethodDelete, ctl.URL+"/_harness/faults", nil)
	resp, _ := http.DefaultClient.Do(req)
	resp.Body.Close()
	if resp, _, _ := post(t, feed.URL+PathTripUpdates, "perf-token", `{"zoneid": 2}`); resp == nil || resp.StatusCode != 200 {
		t.Error("faults not cleared")
	}
	_ = faults.EndpointAny
}

func TestStaticBump(t *testing.T) {
	_, feed, ctl := newServer(t, small())
	before := func() string {
		// trips.txt carries the version in its headsigns; find its check.
		for c := range 8 {
			_, b, _ := post(t, feed.URL+PathStatic, "perf-token", `{"check": `+string(rune('0'+c))+`}`)
			if strings.Contains(string(b), "trip_headsign") {
				return string(b)
			}
		}
		t.Fatal("trips.txt not served")
		return ""
	}
	v1 := before()
	resp, _ := http.Post(ctl.URL+"/_harness/static/bump", "", nil)
	resp.Body.Close()
	if v2 := before(); v2 == v1 {
		t.Error("bump did not change the static data")
	}
}

func TestFeedPing(t *testing.T) {
	s := New(Defaults(), slog.New(slog.DiscardHandler))
	w := httptest.NewRecorder()
	s.FeedHandler().ServeHTTP(w, httptest.NewRequest("GET", "/_ping", nil))
	if w.Code != 200 || !strings.Contains(w.Body.String(), "ok") {
		t.Fatalf("ping: %d %q", w.Code, w.Body)
	}
}
