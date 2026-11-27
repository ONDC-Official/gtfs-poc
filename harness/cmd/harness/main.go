// Command harness runs the local Phase 1 performance and stress harness.
//
//	harness up     [--collector ../transit-hub-collector] [--consumer ../transit-hub-consumer]
//	harness run    scenarios/tc-06-smoke.yaml
//	harness status
//	harness down   [--keep-data]
//
// `up` builds and starts the system under test (both services from their
// own Dockerfiles) with Postgres, Toxiproxy, Prometheus, Grafana and the mock
// BMTC. `run` executes a scenario against it and writes reports/<run>/.
// Everything is local: the collector only ever talks to the mock.
package main

import (
	"context"
	"flag"
	"fmt"
	"os"
	"os/signal"
	"path/filepath"
	"syscall"
	"time"

	"github.com/ONDC-Official/gtfs-poc/harness/internal/stack"
)

func main() {
	if len(os.Args) < 2 {
		usage()
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	root, err := os.Getwd()
	if err != nil {
		fail(err)
	}
	st := stack.Stack{Root: root}

	switch os.Args[1] {
	case "up":
		fs := flag.NewFlagSet("up", flag.ExitOnError)
		collector := fs.String("collector", "../transit-hub-collector", "transit-hub-collector checkout")
		consumer := fs.String("consumer", "../transit-hub-consumer", "transit-hub-consumer checkout")
		_ = fs.Parse(os.Args[2:])
		fail(up(ctx, st, abs(*collector), abs(*consumer)))
	case "run":
		fs := flag.NewFlagSet("run", flag.ExitOnError)
		out := fs.String("out", envOr("HARNESS_REPORTS_DIR", "reports"), "report directory")
		target := fs.String("target", envOr("HARNESS_TARGET", "local"), "local (this machine's stack) or dev (a deployed Transit Hub, from outside)")
		_ = fs.Parse(os.Args[2:])
		if fs.NArg() != 1 {
			usage()
		}
		var passed bool
		var err error
		switch *target {
		case "local":
			passed, err = run(ctx, st, fs.Arg(0), *out)
		case "dev":
			passed, err = runDev(ctx, fs.Arg(0), *out)
		default:
			err = fmt.Errorf("unknown --target %q (local or dev)", *target)
		}
		fail(err)
		if !passed {
			os.Exit(3)
		}
	case "status":
		fail(st.Compose(ctx, "ps"))
	case "down":
		fs := flag.NewFlagSet("down", flag.ExitOnError)
		keep := fs.Bool("keep-data", false, "keep the Postgres volume")
		_ = fs.Parse(os.Args[2:])
		args := []string{"down", "--remove-orphans"}
		if !*keep {
			args = append(args, "-v")
		}
		fail(st.Compose(ctx, args...))
	case "cleanup":
		// Dev target: removes every MOCK_ agency from the target and the mock
		// (after a killed run).
		fail(cleanupDev(ctx))
	case "help", "-h", "--help":
		fmt.Println(helpText)
	default:
		usage()
	}
}

// helpText is printed by `harness help` (the compose service's default
// command, so `docker compose run harness` explains itself).
const helpText = `harness: Transit Hub perf harness

Local stack (developer machine, from harness/):
  harness up | run <scenario.yaml> | status | down

Deployed target (gtfs-poc server, compose profile "harness"):
  harness run --target dev /scenarios/dev/<scenario>.yaml
      creates MOCK_nn agencies on the target through its agency admin API,
      registers their tokens with the mock, starts polling, creates buyers,
      runs the load, writes the report, then cleans up
  harness cleanup
      removes every MOCK_ agency from the target and the mock (after a
      killed run)

  Settings (environment): HARNESS_CONSUMER_URL, HARNESS_MOCK_PUBLIC_URL,
  HARNESS_MOCK_CONTROL_URL, HARNESS_ADMIN_API_KEY, HARNESS_ALLOWED_HOSTS,
  HARNESS_REPORTS_DIR. See harness/README.md.`

func up(ctx context.Context, st stack.Stack, collectorSrc, consumerSrc string) error {
	state, err := st.Init(collectorSrc, consumerSrc)
	if err != nil {
		return err
	}
	if err := st.WriteEnv(state, nil); err != nil {
		return err
	}
	if err := st.Compose(ctx, "up", "-d", "--build", "--wait"); err != nil {
		return fmt.Errorf("compose up: %w", err)
	}
	if err := st.CheckIsolation(ctx, state); err != nil {
		return err
	}
	for _, u := range []string{stack.CollectorURL + "/readyz", stack.ConsumerURL + "/readyz", stack.MockControlURL + "/_harness/healthz"} {
		if err := stack.WaitHTTP(ctx, u, 3*time.Minute); err != nil {
			return err
		}
	}
	fmt.Fprintf(os.Stderr, "\nStack is up (local only).\n  Consumer API  %s\n  Prometheus    %s\n  Grafana       %s\n  Mock control  %s\n",
		stack.ConsumerURL, stack.PrometheusURL, stack.GrafanaURL, stack.MockControlURL)
	return nil
}

func abs(p string) string {
	a, err := filepath.Abs(p)
	if err != nil {
		fail(err)
	}
	return a
}

func usage() {
	fmt.Fprintln(os.Stderr, "usage: harness up | run [--target local|dev] <scenario.yaml> | status | down | cleanup | help")
	os.Exit(2)
}

func fail(err error) {
	if err != nil {
		fmt.Fprintln(os.Stderr, "harness:", err)
		os.Exit(1)
	}
}
