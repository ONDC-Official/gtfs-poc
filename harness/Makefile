# All targets run locally. `make up` builds both services from the sibling
# checkouts (override COLLECTOR= / CONSUMER=).
COLLECTOR ?= ../transit-hub-collector
CONSUMER  ?= ../transit-hub-consumer
SCENARIO  ?= scenarios/tc-06-smoke.yaml
export CGO_ENABLED=0

.PHONY: build test vet up run status down smoke
build:
	go build -o bin/ ./cmd/...
test:
	go test ./...
vet:
	go vet ./...
up: build
	bin/harness up --collector $(COLLECTOR) --consumer $(CONSUMER)
run: build
	bin/harness run $(SCENARIO)
smoke: build
	bin/harness run scenarios/tc-06-smoke.yaml
status:
	bin/harness status
down:
	bin/harness down
