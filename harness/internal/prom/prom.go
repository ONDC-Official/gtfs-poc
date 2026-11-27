// Package prom runs instant queries against the harness Prometheus.
package prom

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
	"strconv"
	"time"
)

// Client queries one Prometheus.
type Client struct {
	BaseURL string
	HTTP    *http.Client
}

// Sample is one series of an instant vector.
type Sample struct {
	Labels map[string]string
	Value  float64
}

// Query evaluates expr at t.
func (c *Client) Query(ctx context.Context, expr string, t time.Time) ([]Sample, error) {
	hc := c.HTTP
	if hc == nil {
		hc = &http.Client{Timeout: 30 * time.Second}
	}
	q := url.Values{"query": {expr}, "time": {strconv.FormatFloat(float64(t.UnixMilli())/1000, 'f', 3, 64)}}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, c.BaseURL+"/api/v1/query?"+q.Encode(), nil)
	if err != nil {
		return nil, err
	}
	resp, err := hc.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	var body struct {
		Status string `json:"status"`
		Error  string `json:"error"`
		Data   struct {
			ResultType string `json:"resultType"`
			Result     []struct {
				Metric map[string]string `json:"metric"`
				Value  [2]any            `json:"value"`
			} `json:"result"`
		} `json:"data"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
		return nil, fmt.Errorf("decode: %w", err)
	}
	if body.Status != "success" {
		return nil, fmt.Errorf("query %q: %s", expr, body.Error)
	}
	out := make([]Sample, 0, len(body.Data.Result))
	for _, r := range body.Data.Result {
		s, _ := r.Value[1].(string)
		v, err := strconv.ParseFloat(s, 64)
		if err != nil {
			continue
		}
		out = append(out, Sample{Labels: r.Metric, Value: v})
	}
	return out, nil
}

// Scalar returns the first sample's value, and false when the result is empty.
func (c *Client) Scalar(ctx context.Context, expr string, t time.Time) (float64, bool, error) {
	s, err := c.Query(ctx, expr, t)
	if err != nil || len(s) == 0 {
		return 0, false, err
	}
	return s[0].Value, true, nil
}

// Window renders d as a PromQL range ("123s").
func Window(d time.Duration) string { return fmt.Sprintf("%ds", max(int(d.Seconds()), 1)) }
