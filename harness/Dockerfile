# syntax=docker/dockerfile:1
# Harness image: mockbmtc (used by the local stack), plus harness and
# buyerload for running inside a container if wanted.
FROM golang:1.27 AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN --mount=type=cache,target=/go/pkg/mod go mod download
COPY . .
RUN --mount=type=cache,target=/go/pkg/mod --mount=type=cache,target=/root/.cache/go-build \
    CGO_ENABLED=0 GOOS=linux go build -ldflags "-s -w" -o /out/ ./cmd/...
# State and report dirs owned by nonroot, so a new named volume mounted
# there inherits that ownership and the mock/runner can write to it.
RUN mkdir -p /dirs/state /dirs/reports

FROM gcr.io/distroless/static-debian12:nonroot
COPY --from=build /out/ /
COPY --from=build --chown=65532:65532 /dirs/ /
USER nonroot:nonroot
EXPOSE 9443 9091
ENTRYPOINT ["/mockbmtc"]
