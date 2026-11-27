// Package tlsgen creates the harness's private CA and the mock BMTC's server
// certificate, so the collector talks HTTPS to the mock exactly as it does to
// the real BMTC (it trusts the CA through SSL_CERT_FILE).
package tlsgen

import (
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/pem"
	"fmt"
	"math/big"
	"os"
	"path/filepath"
	"time"
)

// Files written by Ensure, relative to its directory.
const (
	CAFile   = "ca.pem"
	CertFile = "server.pem"
	KeyFile  = "server-key.pem"
)

// Ensure writes a CA and a server certificate for hosts into dir, unless all
// three files already exist. The CA key is not kept: nothing else is signed.
func Ensure(dir string, hosts []string) error {
	if exists(dir, CAFile) && exists(dir, CertFile) && exists(dir, KeyFile) {
		return nil
	}
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return err
	}
	now := time.Now()
	caKey, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		return err
	}
	ca := &x509.Certificate{
		SerialNumber:          serial(),
		Subject:               pkix.Name{CommonName: "transit-hub-perf local CA"},
		NotBefore:             now.Add(-time.Hour),
		NotAfter:              now.AddDate(2, 0, 0),
		IsCA:                  true,
		KeyUsage:              x509.KeyUsageCertSign,
		BasicConstraintsValid: true,
	}
	caDER, err := x509.CreateCertificate(rand.Reader, ca, ca, &caKey.PublicKey, caKey)
	if err != nil {
		return err
	}
	srvKey, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		return err
	}
	srv := &x509.Certificate{
		SerialNumber: serial(),
		Subject:      pkix.Name{CommonName: hosts[0]},
		DNSNames:     hosts,
		NotBefore:    now.Add(-time.Hour),
		NotAfter:     now.AddDate(2, 0, 0),
		KeyUsage:     x509.KeyUsageDigitalSignature,
		ExtKeyUsage:  []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth},
	}
	srvDER, err := x509.CreateCertificate(rand.Reader, srv, ca, &srvKey.PublicKey, caKey)
	if err != nil {
		return err
	}
	keyDER, err := x509.MarshalECPrivateKey(srvKey)
	if err != nil {
		return err
	}
	for name, block := range map[string]*pem.Block{
		CAFile:   {Type: "CERTIFICATE", Bytes: caDER},
		CertFile: {Type: "CERTIFICATE", Bytes: srvDER},
		KeyFile:  {Type: "EC PRIVATE KEY", Bytes: keyDER},
	} {
		// Readable by the non-root container users that mount them.
		if err := os.WriteFile(filepath.Join(dir, name), pem.EncodeToMemory(block), 0o644); err != nil {
			return fmt.Errorf("write %s: %w", name, err)
		}
	}
	return nil
}

func exists(dir, name string) bool {
	_, err := os.Stat(filepath.Join(dir, name))
	return err == nil
}

func serial() *big.Int {
	n, _ := rand.Int(rand.Reader, new(big.Int).Lsh(big.NewInt(1), 62))
	return n
}
