//go:build linux && amd64

package installhelper

import (
	"bytes"
	"os"
	"slices"
	"strings"
	"testing"
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
	sourceDigest := digest([]byte("tour-v4-dispatch-source"))
	verified := &VerifiedTourPackage{
		SourceManifestDigest: sourceDigest,
		Files:                map[string]*FileRecord{},
	}
	defer verified.Release()
	put := func(path string, mode os.FileMode, raw []byte) {
		verified.Files[path] = &FileRecord{
			InstallPath: path, PackagePath: path, Mode: mode,
			Size: int64(len(raw)), Digest: digest(raw), Data: raw,
		}
	}
	put(tourPackageBootstrapPath, 0o444, []byte("bootstrap"))
	put(tourPackageBootstrapSignaturePath, 0o444, []byte("bootstrap-signature"))
	put(tourPackageMaterializationPath, 0o444, []byte("materialization"))
	put(tourPackageMaterializationSignaturePath, 0o444, []byte("materialization-signature"))
	put(tourPackageAnchorPath, 0o444, []byte("package-anchor"))
	put(tourPackageReceiptKeyPath, 0o400, []byte("receipt-key"))
	put(tourPackageReceiptAnchorPath, 0o444, []byte("receipt-anchor"))
	previousSource := InstallerSourceManifestDigest
	InstallerSourceManifestDigest = sourceDigest
	t.Cleanup(func() { InstallerSourceManifestDigest = previousSource })
	materials, err := tourV4DetachedMaterials(verified)
	if err != nil {
		t.Fatal(err)
	}
	for path, observed := range map[string][]byte{
		tourPackageBootstrapPath:                materials.AuthorityBootstrap,
		tourPackageBootstrapSignaturePath:       materials.AuthorityBootstrapSignature,
		tourPackageMaterializationPath:          materials.Materialization,
		tourPackageMaterializationSignaturePath: materials.MaterializationSignature,
		tourPackageAnchorPath:                   materials.PackageAnchor,
		tourPackageReceiptKeyPath:               materials.ReceiptKey,
		tourPackageReceiptAnchorPath:            materials.ReceiptAnchor,
	} {
		if !bytes.Equal(observed, verified.Files[path].Data) {
			t.Fatalf("detached material did not alias exact verified package member: %s", path)
		}
	}
	materialization := verified.Files[tourPackageMaterializationPath]
	delete(verified.Files, tourPackageMaterializationPath)
	if _, err := tourV4DetachedMaterials(verified); err == nil {
		t.Fatal("missing signed materialization package member was accepted")
	}
	verified.Files[tourPackageMaterializationPath] = materialization
	materialization.Digest = digest([]byte("rebound-materialization"))
	if _, err := tourV4DetachedMaterials(verified); err == nil {
		t.Fatal("rebound signed materialization record was accepted")
	}
}
