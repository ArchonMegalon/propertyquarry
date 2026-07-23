//go:build linux && amd64

package installhelper

import (
	"archive/tar"
	"bytes"
	"crypto/ed25519"
	"crypto/rand"
	"encoding/json"
	"sort"
	"strings"
	"testing"
	"time"
)

func tourTestArchive(
	t *testing.T,
	manifest, signature []byte,
) []byte {
	t.Helper()
	members := map[string]struct {
		mode int64
		raw  []byte
	}{
		tourPackageManifestPath:          {mode: 0o444, raw: manifest},
		tourPackageManifestSignaturePath: {mode: 0o444, raw: signature},
	}
	for path, contract := range tourPackageFiles {
		members[path] = struct {
			mode int64
			raw  []byte
		}{mode: int64(contract.mode), raw: []byte("fixture-" + path)}
	}
	names := make([]string, 0, len(members))
	for name := range members {
		names = append(names, name)
	}
	sort.Strings(names)
	var output bytes.Buffer
	writer := tar.NewWriter(&output)
	for _, name := range names {
		member := members[name]
		header := &tar.Header{
			Name: name, Mode: member.mode, Size: int64(len(member.raw)),
			ModTime: time.Unix(0, 0).UTC(), Typeflag: tar.TypeReg,
			Format: tar.FormatUSTAR,
		}
		if err := writer.WriteHeader(header); err != nil {
			t.Fatal(err)
		}
		if _, err := writer.Write(member.raw); err != nil {
			t.Fatal(err)
		}
	}
	if err := writer.Close(); err != nil {
		t.Fatal(err)
	}
	return output.Bytes()
}

func TestTourPackageProtocolIsCrossDomainAndNonInstallable(t *testing.T) {
	public, private, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	_, _, keyID, err := parsePublicPEM(mustTestPublicPEM(t, public))
	if err != nil {
		t.Fatal(err)
	}
	manifest, err := canonicalJSON(map[string]any{
		"schema":  tourPackageSchema,
		"version": json.Number("4"),
	})
	if err != nil {
		t.Fatal(err)
	}
	fullDomainArchive := tourTestArchive(
		t, manifest, ed25519.Sign(private, framed(packageSignatureDomain, manifest)),
	)
	if _, err := VerifyTourPackageBytes(fullDomainArchive, public, keyID); err == nil {
		t.Fatal("full runtime package signature domain authorized a tour package")
	}
	tourDomainArchive := tourTestArchive(
		t, manifest, ed25519.Sign(private, framed(tourPackageSignatureDomain, manifest)),
	)
	if _, err := VerifyPackageBytes(tourDomainArchive, public, keyID); err == nil {
		t.Fatal("tour package was accepted by the full runtime package verifier")
	}
	if len(tourPackageFiles) != 9 {
		t.Fatalf("unexpected tour package member count: %d", len(tourPackageFiles))
	}
	for path := range tourPackageFiles {
		for _, forbidden := range []string{
			"runtime-deploy", "runner", "systemd", "sysusers", "tmpfiles",
		} {
			if strings.Contains(path, forbidden) {
				t.Fatalf("tour-only package contains runtime install authority: %s", path)
			}
		}
	}
}

func TestTourMaterializationClaimsAndTTLFailClosed(t *testing.T) {
	public, private, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	packageID := digest([]byte("package"))
	receiptID := digest([]byte("receipt"))
	receiptAnchorDigest := digest([]byte("receipt-anchor"))
	bootstrapDigest := digest([]byte("bootstrap"))
	buildDigest := digest([]byte("build"))
	sourceDigest := digest([]byte("source"))
	payload := map[string]any{
		"accepted_installer_mode": tourPackageAcceptedInstallerMode,
		"allowed_operations": []any{
			"tour-v4-authority-info", "tour-inspect-v4", "tour-publish-v4",
			"tour-recover-v4", "tour-rollback-v4",
		},
		"artifact_bundle_path":                         tourV4BundlePath,
		"artifact_manifest_sha256":                     tourV4ManifestSHA256,
		"artifact_public_tree_sha256":                  tourV4PublicSHA256,
		"artifact_slug":                                tourV4Slug,
		"authoritative":                                false,
		"authority_bootstrap_sha256":                   bootstrapDigest,
		"host_install_permitted":                       false,
		"host_machine_id_digest":                       digest([]byte("machine")),
		"materialized_at_epoch":                        json.Number("1800000000"),
		"native_build_receipt_sha256":                  buildDigest,
		"network_required":                             false,
		"package_authority_key_id":                     packageID,
		"performs_release_effects":                     false,
		"persistent_credential_installation_permitted": false,
		"production_ready":                             false,
		"publication_dispatch_authorized":              true,
		"publication_target_root":                      tourPublicationTargetRoot,
		"receipt_authority_key_id":                     receiptID,
		"receipt_authority_public_sha256":              receiptAnchorDigest,
		"root_helper_authorization_required":           true,
		"runtime_deployment_permitted":                 false,
		"schema":                                       tourMaterializationSchema,
		"source_manifest_digest":                       sourceDigest,
		"valid_until_epoch":                            json.Number("1800003600"),
		"version":                                      json.Number("4"),
	}
	verify := func(value map[string]any, domain string) error {
		raw, err := canonicalJSON(value)
		if err != nil {
			return err
		}
		signature := ed25519.Sign(private, framed(domain, raw))
		_, _, _, err = validateTourMaterialization(
			raw, signature, public, packageID, receiptID,
			receiptAnchorDigest, bootstrapDigest, buildDigest, sourceDigest,
		)
		return err
	}
	if err := verify(payload, tourMaterializationSignatureDomain); err != nil {
		t.Fatalf("valid tour materialization rejected: %v", err)
	}
	for _, field := range []string{
		"host_install_permitted", "network_required",
		"persistent_credential_installation_permitted",
		"runtime_deployment_permitted",
	} {
		t.Run(field, func(t *testing.T) {
			rebound := make(map[string]any, len(payload))
			for key, value := range payload {
				rebound[key] = value
			}
			rebound[field] = true
			if err := verify(rebound, tourMaterializationSignatureDomain); err == nil {
				t.Fatalf("re-signed %s authority was accepted", field)
			}
		})
	}
	expired := make(map[string]any, len(payload))
	for key, value := range payload {
		expired[key] = value
	}
	expired["valid_until_epoch"] = json.Number("1800003601")
	if err := verify(expired, tourMaterializationSignatureDomain); err == nil {
		t.Fatal("non-exact tour materialization TTL was accepted")
	}
	if err := verify(payload, materializationSignatureDomain); err == nil {
		t.Fatal("full runtime materialization signature domain authorized a tour package")
	}
}

func mustTestPublicPEM(t *testing.T, key ed25519.PublicKey) []byte {
	t.Helper()
	raw, _ := testPublicPEM(t, key)
	return raw
}
