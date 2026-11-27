package mock

import (
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
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

func ctlDo(t *testing.T, method, url, body string) (int, string) {
	t.Helper()
	req, _ := http.NewRequest(method, url, strings.NewReader(body))
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	b, _ := io.ReadAll(resp.Body)
	return resp.StatusCode, string(b)
}

func TestNamedAgencies(t *testing.T) {
	_, feed, ctl := newServer(t, small())
	if c, _ := ctlDo(t, "PUT", ctl.URL+"/_harness/agencies/MOCK_01", `{"token":"tok-01","vehicles":120,"shift_s":0}`); c != 200 {
		t.Fatalf("add MOCK_01: %d", c)
	}
	if c, _ := ctlDo(t, "PUT", ctl.URL+"/_harness/agencies/MOCK_02", `{"token":"tok-02"}`); c != 200 {
		t.Fatalf("add MOCK_02: %d", c)
	}
	// Bad requests.
	for path, body := range map[string]string{
		"/_harness/agencies/mock_lower": `{"token":"x"}`,
		"/_harness/agencies/MOCK_03":    `{}`,
		"/_harness/agencies/MOCK_04":    `{"token":"x","nope":1}`,
		"/_harness/agencies/default":    `{"token":"x"}`,
	} {
		if c, _ := ctlDo(t, "PUT", ctl.URL+path, body); c != 400 {
			t.Errorf("%s %s: %d, want 400", path, body, c)
		}
	}
	// Each agency only accepts its own token.
	cases := []struct {
		path, token string
		want        int
	}{
		{"/MOCK_01" + PathVehicles, "tok-01", 200},
		{"/MOCK_01" + PathVehicles, "tok-02", 401},
		{"/MOCK_01" + PathVehicles, "perf-token", 401}, // default agency's token
		{"/MOCK_02" + PathVehicles, "tok-02", 200},
		{"/MOCK_99" + PathVehicles, "tok-01", 401},     // unknown agency looks like a bad token
		{"/default" + PathVehicles, "perf-token", 401}, // default only at root
		{PathVehicles, "perf-token", 200},              // root path unchanged
		{PathVehicles, "tok-01", 401},
	}
	for _, c := range cases {
		if resp, _, _ := post(t, feed.URL+c.path, c.token, `{}`); resp.StatusCode != c.want {
			t.Errorf("%s with %s: %d, want %d", c.path, c.token, resp.StatusCode, c.want)
		}
	}
	// Per-agency data settings.
	_, body, _ := post(t, feed.URL+"/MOCK_01"+PathVehicles, "tok-01", `{}`)
	var env struct {
		RowCount int `json:"RowCount"`
	}
	_ = json.Unmarshal(body, &env)
	if want := 120 + small().Dupes + small().Stale + small().Invalid; env.RowCount != want {
		t.Errorf("MOCK_01 rows %d, want %d", env.RowCount, want)
	}
	// Per-agency faults: MOCK_01 503s, MOCK_02 doesn't.
	if c, _ := ctlDo(t, "PUT", ctl.URL+"/_harness/faults?agency=MOCK_01", `[{"endpoint":"vehicles","action":"status","status":503}]`); c != 200 {
		t.Fatalf("faults: %d", c)
	}
	if resp, _, _ := post(t, feed.URL+"/MOCK_01"+PathVehicles, "tok-01", `{}`); resp.StatusCode != 503 {
		t.Errorf("MOCK_01 fault: %d", resp.StatusCode)
	}
	if resp, _, _ := post(t, feed.URL+"/MOCK_02"+PathVehicles, "tok-02", `{}`); resp.StatusCode != 200 {
		t.Errorf("MOCK_02 affected by MOCK_01 fault: %d", resp.StatusCode)
	}
	if resp, _, _ := post(t, feed.URL+PathVehicles, "perf-token", `{}`); resp.StatusCode != 200 {
		t.Errorf("default affected by MOCK_01 fault: %d", resp.StatusCode)
	}
	if c, _ := ctlDo(t, "PUT", ctl.URL+"/_harness/faults?agency=MOCK_77", `[]`); c != 404 {
		t.Errorf("faults unknown agency: %d", c)
	}
	// Per-agency stats and static version.
	_, st := ctlDo(t, "GET", ctl.URL+"/_harness/stats?agency=MOCK_01", "")
	if !strings.Contains(st, `"agency_id":"MOCK_01"`) || !strings.Contains(st, `"status_503":1`) || !strings.Contains(st, `"unauthorized":2`) {
		t.Errorf("MOCK_01 stats: %s", st)
	}
	_, _ = ctlDo(t, "POST", ctl.URL+"/_harness/static/bump?agency=MOCK_02", "")
	_, list := ctlDo(t, "GET", ctl.URL+"/_harness/agencies", "")
	if strings.Contains(list, "tok-0") || !strings.Contains(list, `"token_set":true`) {
		t.Errorf("listing leaks tokens or lacks token_set: %s", list)
	}
	var l struct {
		Agencies []AgencyInfo `json:"agencies"`
	}
	_ = json.Unmarshal([]byte(list), &l)
	if len(l.Agencies) != 2 || l.Agencies[0].ID != "MOCK_01" || l.Agencies[0].StaticVersion != 1 || l.Agencies[1].StaticVersion != 2 {
		t.Errorf("agencies: %+v", l.Agencies)
	}
	// Rotation: replacing keeps stats; old token stops working.
	_, _ = ctlDo(t, "PUT", ctl.URL+"/_harness/agencies/MOCK_02", `{"token":"tok-02b"}`)
	if resp, _, _ := post(t, feed.URL+"/MOCK_02"+PathVehicles, "tok-02", `{}`); resp.StatusCode != 401 {
		t.Errorf("old token after rotation: %d", resp.StatusCode)
	}
	if resp, _, _ := post(t, feed.URL+"/MOCK_02"+PathVehicles, "tok-02b", `{}`); resp.StatusCode != 200 {
		t.Errorf("new token after rotation: %d", resp.StatusCode)
	}
	// Remove one, then all.
	if c, _ := ctlDo(t, "DELETE", ctl.URL+"/_harness/agencies/MOCK_01", ""); c != 200 {
		t.Errorf("delete: %d", c)
	}
	if c, _ := ctlDo(t, "DELETE", ctl.URL+"/_harness/agencies/MOCK_01", ""); c != 404 {
		t.Errorf("delete again: %d", c)
	}
	if resp, _, _ := post(t, feed.URL+"/MOCK_01"+PathVehicles, "tok-01", `{}`); resp.StatusCode != 401 {
		t.Errorf("removed agency still served: %d", resp.StatusCode)
	}
	_, _ = ctlDo(t, "DELETE", ctl.URL+"/_harness/agencies", "")
	_, list = ctlDo(t, "GET", ctl.URL+"/_harness/agencies", "")
	if !strings.Contains(list, `"agencies":[]`) {
		t.Errorf("after delete all: %s", list)
	}
}

func TestAgencyStateFile(t *testing.T) {
	path := t.TempDir() + "/state/agencies.json"
	s := New(small(), slog.New(slog.DiscardHandler))
	if err := s.UseStateFile(path); err != nil {
		t.Fatal(err)
	}
	shift := 19800
	if err := s.AddAgency(AgencySpec{ID: "MOCK_01", Token: "tok-01", ShiftS: &shift}); err != nil {
		t.Fatal(err)
	}
	if fi, err := os.Stat(path); err != nil || fi.Mode().Perm() != 0o600 {
		t.Fatalf("state file: %v %v", fi, err)
	}
	// A new server (mock restart) gets the agency back with its token.
	s2 := New(small(), slog.New(slog.DiscardHandler))
	if err := s2.UseStateFile(path); err != nil {
		t.Fatal(err)
	}
	if a := s2.Agencies(); len(a) != 1 || a[0].ID != "MOCK_01" || a[0].ShiftS != 19800 {
		t.Fatalf("restored: %+v", a)
	}
	feed := httptest.NewServer(s2.FeedHandler())
	defer feed.Close()
	if resp, _, _ := post(t, feed.URL+"/MOCK_01"+PathVehicles, "tok-01", `{}`); resp.StatusCode != 200 {
		t.Fatalf("restored agency: %d", resp.StatusCode)
	}
	if _, err := s2.RemoveAgency("MOCK_01"); err != nil {
		t.Fatal(err)
	}
	s3 := New(small(), slog.New(slog.DiscardHandler))
	_ = s3.UseStateFile(path)
	if len(s3.Agencies()) != 0 {
		t.Fatal("removal not saved")
	}
}
