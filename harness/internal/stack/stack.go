// Package stack drives the local system under test with docker compose
// (deploy/compose.yaml) and keeps the harness's local state in .harness/.
package stack

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"github.com/ONDC-Official/gtfs-poc/harness/internal/tlsgen"
)

// Host ports the compose file publishes (all overridable through Env).
const (
	ConsumerURL     = "http://127.0.0.1:18082"
	CollectorURL    = "http://127.0.0.1:18080"
	MockControlURL  = "http://127.0.0.1:19091"
	PrometheusURL   = "http://127.0.0.1:19090"
	GrafanaURL      = "http://127.0.0.1:13000"
	ToxiproxyAPIURL = "http://127.0.0.1:18474"
	ProjectName     = "transit-hub-perf"
)

// State is what `harness up` keeps between commands.
type State struct {
	AdminKey     string    `json:"admin_key"`
	MockToken    string    `json:"mock_token"`
	CollectorSrc string    `json:"collector_src"`
	ConsumerSrc  string    `json:"consumer_src"`
	CreatedAt    time.Time `json:"created_at"`
}

// Stack is one checkout's harness.
type Stack struct {
	Root string // transit-hub-perf checkout
}

// Dir is the local state directory (.harness, gitignored).
func (s Stack) Dir() string { return filepath.Join(s.Root, ".harness") }

func (s Stack) statePath() string { return filepath.Join(s.Dir(), "state.json") }
func (s Stack) envPath() string   { return filepath.Join(s.Dir(), "stack.env") }

// LoadState reads .harness/state.json.
func (s Stack) LoadState() (*State, error) {
	b, err := os.ReadFile(s.statePath())
	if err != nil {
		return nil, fmt.Errorf("no harness state (run `harness up` first): %w", err)
	}
	var st State
	return &st, json.Unmarshal(b, &st)
}

// Init creates the state (secrets generated locally, never real ones) and
// the TLS files, keeping existing ones.
func (s Stack) Init(collectorSrc, consumerSrc string) (*State, error) {
	if err := os.MkdirAll(s.Dir(), 0o700); err != nil {
		return nil, err
	}
	st, err := s.LoadState()
	if err != nil {
		st = &State{AdminKey: "adm_perf_" + randHex(24), MockToken: "perf-" + randHex(12), CreatedAt: time.Now().UTC()}
	}
	for _, src := range []string{collectorSrc, consumerSrc} {
		if _, err := os.Stat(filepath.Join(src, "Dockerfile")); err != nil {
			return nil, fmt.Errorf("%s has no Dockerfile: point --collector / --consumer at the service checkouts", src)
		}
	}
	st.CollectorSrc, st.ConsumerSrc = collectorSrc, consumerSrc
	if err := tlsgen.Ensure(filepath.Join(s.Dir(), "tls"), []string{"mockbmtc", "localhost"}); err != nil {
		return nil, fmt.Errorf("tls: %w", err)
	}
	b, _ := json.MarshalIndent(st, "", "  ")
	return st, os.WriteFile(s.statePath(), b, 0o600)
}

// WriteEnv writes .harness/stack.env for compose: state plus scenario
// parameters (later keys win).
func (s Stack) WriteEnv(st *State, params map[string]string) error {
	env := map[string]string{
		"HARNESS_DIR":   s.Dir(),
		"COLLECTOR_SRC": st.CollectorSrc,
		"CONSUMER_SRC":  st.ConsumerSrc,
		"ADMIN_API_KEY": st.AdminKey,
		"MOCK_TOKEN":    st.MockToken,
	}
	for k, v := range params {
		env[k] = v
	}
	keys := make([]string, 0, len(env))
	for k := range env {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	var b strings.Builder
	for _, k := range keys {
		fmt.Fprintf(&b, "%s=%s\n", k, env[k])
	}
	return os.WriteFile(s.envPath(), []byte(b.String()), 0o600)
}

// Compose runs docker compose with the harness's project, file and env.
func (s Stack) Compose(ctx context.Context, args ...string) error {
	base := []string{"compose", "-p", ProjectName, "-f", filepath.Join(s.Root, "deploy", "compose.yaml"), "--env-file", s.envPath()}
	cmd := exec.CommandContext(ctx, "docker", append(base, args...)...)
	cmd.Stdout, cmd.Stderr = os.Stderr, os.Stderr
	return cmd.Run()
}

// ContainerEnv returns a running service's environment.
func (s Stack) ContainerEnv(ctx context.Context, service string) (map[string]string, error) {
	out, err := exec.CommandContext(ctx, "docker", "inspect", "-f", "{{json .Config.Env}}", ProjectName+"-"+service+"-1").Output()
	if err != nil {
		return nil, fmt.Errorf("inspect %s: %w", service, err)
	}
	var list []string
	if err := json.Unmarshal(bytes.TrimSpace(out), &list); err != nil {
		return nil, err
	}
	env := map[string]string{}
	for _, kv := range list {
		k, v, _ := strings.Cut(kv, "=")
		env[k] = v
	}
	return env, nil
}

// CheckIsolation refuses to continue unless the collector is pointed at the
// mock with the harness token, and nothing points at dev or production.
func (s Stack) CheckIsolation(ctx context.Context, st *State) error {
	env, err := s.ContainerEnv(ctx, "collector")
	if err != nil {
		return err
	}
	u, err := url.Parse(env["AGENCY_FEED_BASE_URL"])
	if err != nil || u.Hostname() != "mockbmtc" {
		return fmt.Errorf("isolation: collector AGENCY_FEED_BASE_URL is %q, not the mock; refusing to run", env["AGENCY_FEED_BASE_URL"])
	}
	if env["AGENCY_BMTC_TOKEN"] != st.MockToken {
		return fmt.Errorf("isolation: collector token is not the harness token; refusing to run")
	}
	for _, svc := range []string{"collector", "consumer"} {
		e, err := s.ContainerEnv(ctx, svc)
		if err != nil {
			return err
		}
		for _, k := range []string{"WRITE_DB_URL", "READ_DB_URL"} {
			if v := e[k]; v != "" && !strings.Contains(v, "@toxiproxy:") {
				return fmt.Errorf("isolation: %s %s does not use the local database; refusing to run", svc, k)
			}
		}
	}
	return nil
}

// WaitHTTP polls url until it answers 200 or timeout passes.
func WaitHTTP(ctx context.Context, url string, timeout time.Duration) error {
	deadline := time.Now().Add(timeout)
	c := &http.Client{Timeout: 3 * time.Second}
	for {
		resp, err := c.Get(url)
		if err == nil {
			resp.Body.Close()
			if resp.StatusCode == http.StatusOK {
				return nil
			}
		}
		if time.Now().After(deadline) {
			return fmt.Errorf("%s not ready after %s", url, timeout)
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(2 * time.Second):
		}
	}
}

func randHex(n int) string {
	b := make([]byte, n)
	_, _ = rand.Read(b)
	return hex.EncodeToString(b)
}
