// Command buyerload drives one traffic profile against a Consumer API with
// one existing key and prints the result as JSON. The harness uses the same
// code in-process; this is for quick manual runs.
//
//	buyerload -url http://127.0.0.1:18082 -key thk_… -endpoint realtime -rps 20 -duration 1m
package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"os/signal"
	"time"

	"github.com/ONDC-Official/gtfs-poc/harness/internal/load"
)

func main() {
	url := flag.String("url", "http://127.0.0.1:18082", "Consumer API base URL")
	key := flag.String("key", os.Getenv("BUYER_KEY"), "buyer API key (or BUYER_KEY)")
	agency := flag.String("agency", "BMTC", "agency id")
	endpoint := flag.String("endpoint", load.EndpointRealtime, "realtime or static")
	rps := flag.Float64("rps", 10, "requests per second")
	concurrency := flag.Int("concurrency", 0, "max in flight (default: rps)")
	duration := flag.Duration("duration", 30*time.Second, "how long to run")
	conditional := flag.Bool("conditional", false, "send If-None-Match like a caching buyer")
	flag.Parse()
	if *key == "" {
		fmt.Fprintln(os.Stderr, "buyerload: -key or BUYER_KEY is required")
		os.Exit(2)
	}
	if *concurrency == 0 {
		*concurrency = max(1, int(*rps))
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt)
	defer stop()
	res := load.Run(ctx, load.Target{BaseURL: *url}, []load.Phase{{Name: "manual", Duration: *duration, Profiles: []load.Profile{{
		Name: *endpoint, Endpoint: *endpoint, Agency: *agency, RPS: *rps, Concurrency: *concurrency, Conditional: *conditional,
		Identities: []load.Identity{{Name: "cli", Kind: load.KindValid, Key: *key}},
	}}}})
	enc := json.NewEncoder(os.Stdout)
	enc.SetIndent("", "  ")
	_ = enc.Encode(res)
}
