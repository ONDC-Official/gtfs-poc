package scenario

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestShippedScenariosLoad(t *testing.T) {
	files, _ := filepath.Glob("../../scenarios/*.yaml")
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
