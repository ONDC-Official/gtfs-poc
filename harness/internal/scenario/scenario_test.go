package scenario

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestShippedScenariosLoad(t *testing.T) {
	files, _ := filepath.Glob("../../scenarios/*.yaml")
	dev, _ := filepath.Glob("../../scenarios/dev/*.yaml")
	files = append(files, dev...)
	if len(files) == 0 {
		t.Fatal("no scenarios found")
	}
	for _, f := range files {
		s, err := Load(f)
		if err != nil {
			t.Errorf("%s: %v", f, err)
			continue
		}
		if s.TotalDuration() <= 0 || s.Thresholds.AvailabilityMin == nil {
			t.Errorf("%s: no duration or thresholds", f)
		}
	}
}

func TestValidateRejects(t *testing.T) {
	cases := map[string]string{
		"unknown field": "scenario_id: x\nbogus: 1\n",
		"no phases":     "scenario_id: x\nidentities: {valid: 1}\n",
		"bad endpoint":  "scenario_id: x\nidentities: {valid: 1}\nphases: [{name: p, duration: 1s, profiles: [{name: a, endpoint: nope, rps: 1}]}]\n",
		"unused kind":   "scenario_id: x\nidentities: {valid: 1}\nphases: [{name: p, duration: 1s, profiles: [{name: a, endpoint: realtime, rps: 1, identities: [malformed]}]}]\n",
		"agencies":      "scenario_id: x\nagency_count: 26\nidentities: {valid: 1}\nphases: [{name: p, duration: 1s, profiles: [{name: a, endpoint: realtime, rps: 1}]}]\n",
		"bad fault":     "scenario_id: x\nidentities: {valid: 1}\nphases: [{name: p, duration: 1s, profiles: [{name: a, endpoint: realtime, rps: 1}]}]\nfault_schedule: [{at: 1s, rules: [{endpoint: vehicles, action: status, status: 200}]}]\n",
	}
	for name, body := range cases {
		p := filepath.Join(t.TempDir(), "s.yaml")
		_ = os.WriteFile(p, []byte(body), 0o644)
		if _, err := Load(p); err == nil {
			t.Errorf("%s: accepted", name)
		} else if !strings.Contains(err.Error(), "s.yaml") {
			t.Errorf("%s: error without file name: %v", name, err)
		}
	}
}

func TestAgencyCountRange(t *testing.T) {
	p := t.TempDir() + "/s.yaml"
	_ = os.WriteFile(p, []byte("scenario_id: x\nagency_count: 5\nidentities: {valid: 1}\nphases: [{name: p, duration: 1s, profiles: [{name: a, endpoint: realtime, rps: 1, identities: [valid]}]}]\n"), 0o644)
	if s, err := Load(p); err != nil || s.AgencyCount != 5 {
		t.Fatalf("agency_count 5: %v", err)
	}
}

func TestValidBuyerQuotaCap(t *testing.T) {
	write := func(body string) string {
		p := filepath.Join(t.TempDir(), "s.yaml")
		_ = os.WriteFile(p, []byte(body), 0o644)
		return p
	}
	// 5 RPS = 300/min over 3 buyers = 100/min each: above the 90 headroom.
	_, err := Load(write("scenario_id: x\nidentities: {valid: 3}\nphases: [{name: p, duration: 1s, profiles: [{name: a, endpoint: realtime, rps: 5}]}]\n"))
	if err == nil || !strings.Contains(err.Error(), "need at least 4") {
		t.Fatalf("over cap: %v", err)
	}
	// Two profiles on the same buyers add up: 3 + 2 RPS over 5 buyers = 60/min.
	if _, err := Load(write("scenario_id: x\nidentities: {valid: 5}\nphases: [{name: p, duration: 1s, profiles: [{name: a, endpoint: realtime, rps: 3}, {name: b, endpoint: static, rps: 2}]}]\n")); err != nil {
		t.Fatalf("under cap: %v", err)
	}
	// low_quota above 100 is refused.
	if _, err := Load(write("scenario_id: x\nidentities: {valid: 1, low_quota: 1, low_quota_per_min: 150}\nphases: [{name: p, duration: 1s, profiles: [{name: a, endpoint: realtime, rps: 1, identities: [valid, low_quota]}]}]\n")); err == nil {
		t.Fatal("low_quota_per_min 150 accepted")
	}
}
