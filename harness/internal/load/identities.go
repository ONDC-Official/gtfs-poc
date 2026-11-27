package load

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"net/http"
	"time"
)

// IdentitySpec is how many buyers of each kind to create.
type IdentitySpec struct {
	Valid, LowQuota, Malformed, UnknownAgency int
	LowQuotaPerMin                            int
	Agency                                    string
	RunID                                     string // part of every profile name, to find them later
}

// CreateIdentities creates the buyers through the Consumer admin API
// (POST /v1/admin/profiles), exactly as a real buyer is onboarded. Malformed
// keys are generated locally; unknown-agency buyers are valid keys used
// against an agency that does not exist.
func CreateIdentities(ctx context.Context, baseURL, adminKey string, spec IdentitySpec) (map[string][]Identity, error) {
	out := map[string][]Identity{}
	create := func(kind string, i, quota int) error {
		name := fmt.Sprintf("perf %s %s %d", spec.RunID, kind, i)
		body, _ := json.Marshal(map[string]any{"name": name,
			"grants": []map[string]any{{"agency_id": spec.Agency, "quota_per_min": quota}}})
		req, _ := http.NewRequestWithContext(ctx, http.MethodPost, baseURL+"/v1/admin/profiles", bytes.NewReader(body))
		req.Header.Set("Authorization", "Bearer "+adminKey)
		req.Header.Set("Content-Type", "application/json")
		resp, err := http.DefaultClient.Do(req)
		if err != nil {
			return err
		}
		defer resp.Body.Close()
		var created struct {
			APIKey string `json:"api_key"`
			Error  string `json:"error"`
		}
		_ = json.NewDecoder(resp.Body).Decode(&created)
		if resp.StatusCode != http.StatusCreated || created.APIKey == "" {
			return fmt.Errorf("create %s: HTTP %d %s", name, resp.StatusCode, created.Error)
		}
		out[kind] = append(out[kind], Identity{Name: name, Kind: kind, Key: created.APIKey})
		return nil
	}
	const unlimited = 1_000_000 // the admin API's maximum quota
	for i := range spec.Valid {
		if err := create(KindValid, i, unlimited); err != nil {
			return nil, err
		}
	}
	for i := range spec.LowQuota {
		if err := create(KindLowQuota, i, spec.LowQuotaPerMin); err != nil {
			return nil, err
		}
	}
	for i := range spec.UnknownAgency {
		if err := create(KindUnknownAgency, i, unlimited); err != nil {
			return nil, err
		}
	}
	for i := range spec.Malformed {
		b := make([]byte, 32)
		_, _ = rand.Read(b)
		out[KindMalformed] = append(out[KindMalformed], Identity{Name: fmt.Sprintf("malformed %d", i), Kind: KindMalformed,
			Key: "thk_" + base64.RawURLEncoding.EncodeToString(b)})
	}
	return out, nil
}

// WaitReady waits until key gets real vehicles and a static zip: the
// projector has copied the new buyers to read_db and the collector has
// published data. It returns how long that took.
func WaitReady(ctx context.Context, baseURL, agency, key string, timeout time.Duration) (time.Duration, error) {
	start := time.Now()
	c := &http.Client{Timeout: 2 * time.Minute}
	get := func(path string) (int, []byte) {
		req, _ := http.NewRequestWithContext(ctx, http.MethodGet, baseURL+path, nil)
		req.Header.Set("Authorization", "Bearer "+key)
		resp, err := c.Do(req)
		if err != nil {
			return 0, nil
		}
		defer resp.Body.Close()
		var b bytes.Buffer
		_, _ = b.ReadFrom(resp.Body)
		return resp.StatusCode, b.Bytes()
	}
	var last string
	for time.Since(start) < timeout {
		rtCode, rt := get("/v1/agencies/" + agency + "/rt/vehicle_position")
		stCode, _ := get("/v1/agencies/" + agency + "/static")
		if rtCode == http.StatusOK && stCode == http.StatusOK && len(rt) > 100 {
			return time.Since(start), nil
		}
		last = fmt.Sprintf("realtime %d (%d bytes), static %d", rtCode, len(rt), stCode)
		select {
		case <-ctx.Done():
			return 0, ctx.Err()
		case <-time.After(3 * time.Second):
		}
	}
	return 0, fmt.Errorf("not ready after %s: last %s", timeout, last)
}
