package main

import (
	"strings"
	"testing"
)

func TestDevConfigSafety(t *testing.T) {
	set := func(env map[string]string) {
		for _, k := range []string{"HARNESS_CONSUMER_URL", "HARNESS_MOCK_PUBLIC_URL", "HARNESS_ADMIN_API_KEY", "HARNESS_ALLOWED_HOSTS"} {
			t.Setenv(k, env[k])
		}
	}
	ok := map[string]string{"HARNESS_CONSUMER_URL": "https://gtfs-dev.ondc.tech", "HARNESS_MOCK_PUBLIC_URL": "https://m.example/gtfs/mock-agency", "HARNESS_ADMIN_API_KEY": "k"}
	set(ok)
	if _, err := devConfigFromEnv(); err != nil {
		t.Fatalf("dev target: %v", err)
	}
	cases := map[string]map[string]string{
		"production host":                  {"HARNESS_CONSUMER_URL": "https://gtfs.ondc.tech"},
		"localhost not allowed by default": {"HARNESS_CONSUMER_URL": "http://127.0.0.1:18082"},
		"no admin key":                     {"HARNESS_ADMIN_API_KEY": ""},
		"http mock":                        {"HARNESS_MOCK_PUBLIC_URL": "http://m.example/x"},
		"no mock url":                      {"HARNESS_MOCK_PUBLIC_URL": ""},
	}
	for name, over := range cases {
		env := map[string]string{}
		for k, v := range ok {
			env[k] = v
		}
		for k, v := range over {
			env[k] = v
		}
		set(env)
		if _, err := devConfigFromEnv(); err == nil {
			t.Errorf("%s: accepted", name)
		}
	}
	// Extra hosts are opt-in.
	env := map[string]string{"HARNESS_CONSUMER_URL": "http://127.0.0.1:18082", "HARNESS_ALLOWED_HOSTS": "127.0.0.1"}
	for k, v := range ok {
		if _, set := env[k]; !set {
			env[k] = v
		}
	}
	set(env)
	if _, err := devConfigFromEnv(); err != nil {
		t.Errorf("allowed extra host: %v", err)
	}
	if ids := mockAgencyIDs(3); strings.Join(ids, ",") != "MOCK_01,MOCK_02,MOCK_03" {
		t.Errorf("ids %v", ids)
	}
}
