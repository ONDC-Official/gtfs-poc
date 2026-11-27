package tlsgen

import (
	"crypto/tls"
	"crypto/x509"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"
)

func TestEnsureServesTrustedTLS(t *testing.T) {
	dir := t.TempDir()
	if err := Ensure(dir, []string{"mockbmtc", "localhost", "127.0.0.1"}); err != nil {
		t.Fatal(err)
	}
	cert, err := tls.LoadX509KeyPair(filepath.Join(dir, CertFile), filepath.Join(dir, KeyFile))
	if err != nil {
		t.Fatal(err)
	}
	srv := httptest.NewUnstartedServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) { _, _ = w.Write([]byte("ok")) }))
	srv.TLS = &tls.Config{Certificates: []tls.Certificate{cert}}
	srv.StartTLS()
	defer srv.Close()

	caPEM, _ := os.ReadFile(filepath.Join(dir, CAFile))
	pool := x509.NewCertPool()
	if !pool.AppendCertsFromPEM(caPEM) {
		t.Fatal("CA not parsed")
	}
	client := &http.Client{Transport: &http.Transport{TLSClientConfig: &tls.Config{RootCAs: pool, ServerName: "mockbmtc"}}}
	resp, err := client.Get(srv.URL)
	if err != nil {
		t.Fatalf("TLS with the harness CA failed: %v", err)
	}
	resp.Body.Close()

	// A second call keeps the existing files (same CA).
	before, _ := os.ReadFile(filepath.Join(dir, CAFile))
	if err := Ensure(dir, []string{"mockbmtc"}); err != nil {
		t.Fatal(err)
	}
	after, _ := os.ReadFile(filepath.Join(dir, CAFile))
	if string(before) != string(after) {
		t.Error("Ensure regenerated an existing CA")
	}
}
