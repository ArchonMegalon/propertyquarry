//go:build linux && amd64

package installhelper

import (
	"bytes"
	"slices"
	"strings"
	"testing"

	"propertyquarry.local/release-single-host-v2/internal/authority"
)

func TestTourV4DispatchArgumentAllowlist(t *testing.T) {
	oldTree := "sha256:" + strings.Repeat("a", 64)
	transaction := strings.Repeat("b", 32)
	valid := [][]string{
		{"tour-v4-authority-info"},
		{"tour-inspect-v4", "--expected-manifest-sha256", tourV4ManifestSHA256},
		{
			"tour-publish-v4", "--bundle", tourV4BundlePath,
			"--expected-manifest-sha256", tourV4ManifestSHA256,
			"--expected-old-tree", "absent",
			"--transaction-id", transaction,
		},
		{
			"tour-publish-v4", "--bundle", tourV4BundlePath,
			"--expected-manifest-sha256", tourV4ManifestSHA256,
			"--expected-old-tree", oldTree,
			"--transaction-id", transaction,
		},
		{
			"tour-recover-v4",
			"--expected-manifest-sha256", tourV4ManifestSHA256,
			"--expected-old-tree", oldTree,
			"--transaction-id", transaction,
		},
		{
			"tour-rollback-v4",
			"--expected-manifest-sha256", tourV4ManifestSHA256,
			"--expected-old-tree", oldTree,
			"--expected-current-tree", tourV4PublicSHA256,
			"--transaction-id", transaction,
		},
	}
	for _, args := range valid {
		accepted, err := validateTourV4DispatchArguments(args)
		if err != nil || !slices.Equal(accepted, args) {
			t.Fatalf("valid dispatch rejected: args=%q accepted=%q err=%v", args, accepted, err)
		}
		if len(accepted) > 0 {
			accepted[0] = "changed"
			if args[0] == "changed" {
				t.Fatal("dispatch validator returned an aliased argument slice")
			}
		}
	}
	invalid := [][]string{
		nil,
		{"/bin/sh"},
		{"tour-publish-v4", "--bundle", "/etc"},
		{"tour-v4-authority-info", "--help"},
		{"tour-inspect-v4", "--expected-manifest-sha256", "sha256:" + strings.Repeat("0", 64)},
		{
			"tour-publish-v4", "--expected-manifest-sha256", tourV4ManifestSHA256,
			"--bundle", tourV4BundlePath,
			"--expected-old-tree", oldTree,
			"--transaction-id", transaction,
		},
		{
			"tour-publish-v4", "--bundle", tourV4BundlePath + "-alternate",
			"--expected-manifest-sha256", tourV4ManifestSHA256,
			"--expected-old-tree", oldTree,
			"--transaction-id", transaction,
		},
		{
			"tour-recover-v4",
			"--expected-manifest-sha256", tourV4ManifestSHA256,
			"--expected-old-tree", oldTree,
			"--transaction-id", strings.Repeat("B", 32),
		},
		{
			"tour-rollback-v4",
			"--expected-manifest-sha256", tourV4ManifestSHA256,
			"--expected-old-tree", "absent",
			"--expected-current-tree", tourV4PublicSHA256,
			"--transaction-id", transaction,
		},
		{
			"tour-rollback-v4",
			"--expected-manifest-sha256", tourV4ManifestSHA256,
			"--expected-old-tree", oldTree,
			"--expected-current-tree", oldTree,
			"--transaction-id", transaction,
		},
	}
	for _, args := range invalid {
		if accepted, err := validateTourV4DispatchArguments(args); err == nil || accepted != nil {
			t.Fatalf("invalid dispatch accepted: args=%q accepted=%q err=%v", args, accepted, err)
		}
	}
}

func TestTourV4DispatchUsesOnlyExactPackageBoundDetachedMaterials(t *testing.T) {
	fixture := newInstallFixture(t)
	verified := fixture.verifiedPackage(t, 1, strings.Repeat("a", 40), "genesis")
	defer verified.Release()
	sourceDigest := digest([]byte("tour-v4-dispatch-source"))
	verified.SourceManifestDigest = sourceDigest
	previousSource := InstallerSourceManifestDigest
	InstallerSourceManifestDigest = sourceDigest
	t.Cleanup(func() { InstallerSourceManifestDigest = previousSource })
	materials, err := tourV4DetachedMaterials(verified)
	if err != nil {
		t.Fatal(err)
	}
	for path, observed := range map[string][]byte{
		authority.ConfigPath:          materials.Config,
		authority.ConfigSignaturePath: materials.ConfigSignature,
		authority.PackageAnchorPath:   materials.PackageAnchor,
		authority.PlanPath:            materials.Plan,
		authority.ReceiptKeyPath:      materials.ReceiptKey,
		authority.ReceiptAnchorPath:   materials.ReceiptAnchor,
	} {
		if !bytes.Equal(observed, verified.Files[path].Data) {
			t.Fatalf("detached material did not alias exact verified package member: %s", path)
		}
	}
	config := verified.Files[authority.ConfigPath]
	delete(verified.Files, authority.ConfigPath)
	if _, err := tourV4DetachedMaterials(verified); err == nil {
		t.Fatal("missing signed config package member was accepted")
	}
	verified.Files[authority.ConfigPath] = config
	config.Digest = digest([]byte("rebound-config"))
	if _, err := tourV4DetachedMaterials(verified); err == nil {
		t.Fatal("rebound signed config record was accepted")
	}
}
