// Command mockbmtc is the harness's BMTC stand-in (see internal/mock).
//
//	mockbmtc -addr :9443 -tls-cert server.pem -tls-key server-key.pem -control-addr :9091
//
// The feed listener speaks HTTPS when a certificate is given, like the real
// BMTC, and plain HTTP without one. In gtfs-poc it runs plain HTTP behind
// nginx (TLS ends at the host nginx):
//
//	mockbmtc -addr :9080 -control-addr :9091
//
// The control listener (faults, stats, /metrics) is plain HTTP and is only
// published to the harness, never to the collector.
package main

import (
	"context"
	"errors"
	"flag"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/ONDC-Official/gtfs-poc/harness/internal/mock"
)

func main() {
	d := mock.Defaults()
	addr := flag.String("addr", ":9443", "feed listen address (what the collector calls)")
	controlAddr := flag.String("control-addr", ":9091", "control API and /metrics listen address (harness only)")
	cert := flag.String("tls-cert", "", "server certificate (PEM); empty serves plain HTTP")
	key := flag.String("tls-key", "", "server key (PEM)")
	token := flag.String("token", d.Token, "required authToken header value")
	format := flag.String("format", d.Format, "bmtc (real API envelopes), json or protobuf")
	seed := flag.Uint64("seed", d.Seed, "seed for generated data and fault sampling")
	vehicles := flag.Int("vehicles", d.Vehicles, "fresh vehicles per response (real BMTC: ~7,000)")
	dupes := flag.Int("dupes", d.Dupes, "duplicate vehicle entities per response")
	stale := flag.Int("stale", d.Stale, "stale vehicles per response")
	invalid := flag.Int("invalid", d.Invalid, "entities without a vehicle id per response")
	shift := flag.Duration("shift", d.Shift, "vehicle timestamp shift (real BMTC: +5h30m)")
	trips := flag.Int("trips-per-zone", d.TripsPerZone, "trip updates per zone")
	stopsPerTrip := flag.Int("stops-per-trip", d.StopsPerTrip, "stop_time_updates per trip (drives zone payload size)")
	staticStops := flag.Int("static-stops", d.Static.Stops, "static: stops")
	staticRoutes := flag.Int("static-routes", d.Static.Routes, "static: routes")
	staticTrips := flag.Int("static-trips-per-route", d.Static.TripsPerRoute, "static: trips per route (drives zip size)")
	staticStopsPerTrip := flag.Int("static-stops-per-trip", d.Static.StopsPerTrip, "static: stop_times per trip (drives zip size)")
	healthcheck := flag.String("healthcheck", "", "GET this URL and exit 0 on 200 (container healthcheck; distroless has no curl)")
	flag.Parse()
	if *healthcheck != "" {
		c := &http.Client{Timeout: 2 * time.Second}
		resp, err := c.Get(*healthcheck)
		if err != nil || resp.StatusCode != http.StatusOK {
			os.Exit(1)
		}
		os.Exit(0)
	}

	log := slog.New(slog.NewJSONHandler(os.Stdout, nil))
	cfg := d
	cfg.Token, cfg.Format, cfg.Seed = *token, *format, *seed
	cfg.Vehicles, cfg.Dupes, cfg.Stale, cfg.Invalid, cfg.Shift = *vehicles, *dupes, *stale, *invalid, *shift
	cfg.TripsPerZone, cfg.StopsPerTrip = *trips, *stopsPerTrip
	cfg.Static.Stops, cfg.Static.Routes = *staticStops, *staticRoutes
	cfg.Static.TripsPerRoute, cfg.Static.StopsPerTrip = *staticTrips, *staticStopsPerTrip
	switch cfg.Format {
	case mock.FormatBMTC, mock.FormatJSON, mock.FormatProtobuf:
	default:
		log.Error("unknown -format", "format", cfg.Format)
		os.Exit(2)
	}
	s := mock.New(cfg, log)

	feed := &http.Server{Addr: *addr, Handler: s.FeedHandler(), ReadHeaderTimeout: 10 * time.Second}
	control := &http.Server{Addr: *controlAddr, Handler: s.ControlHandler(), ReadHeaderTimeout: 10 * time.Second}
	errc := make(chan error, 2)
	go func() {
		if *cert != "" {
			errc <- feed.ListenAndServeTLS(*cert, *key)
			return
		}
		errc <- feed.ListenAndServe()
	}()
	go func() { errc <- control.ListenAndServe() }()
	log.Info("mock BMTC listening", "addr", *addr, "tls", *cert != "", "control_addr", *controlAddr,
		"vehicles", cfg.Vehicles, "format", cfg.Format, "seed", cfg.Seed)

	stop := make(chan os.Signal, 1)
	signal.Notify(stop, syscall.SIGINT, syscall.SIGTERM)
	select {
	case err := <-errc:
		if err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Error("listen failed", "err", err)
			os.Exit(1)
		}
	case <-stop:
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	_ = feed.Shutdown(ctx)
	_ = control.Shutdown(ctx)
}
