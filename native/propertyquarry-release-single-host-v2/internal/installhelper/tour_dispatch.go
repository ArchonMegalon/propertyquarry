//go:build linux && amd64

package installhelper

import (
	"fmt"
	"io"
	"os"
	"regexp"
	"syscall"

	"propertyquarry.local/release-single-host-v2/internal/authority"
)

const (
	tourV4Slug = "ab-1-8-modern-and-fully-furnited-loft-apartment-top-moderne-und-voll-mblierte-" +
		"loft-wohnung-nas-layout-first-d07edad7af3fc379574d"
	tourV4BundlePath = "/tmp/propertyquarry-f7-live-v6.OCEyn0/" +
		tourV4Slug
	tourV4ManifestSHA256 = "sha256:ed566ad5265a0cb4d205783b5ce8bc35de8100d75d5adf0059c178836326de33"
	tourV4PublicSHA256   = "sha256:1d2de2be6c8e11eca686ab3e7c76c1d4c7ba12d231f14276eb9da8bf7fe3d545"
)

var tourV4DispatchTransactionPattern = regexp.MustCompile(`^[0-9a-f]{32}$`)

func validateTourV4DispatchArguments(args []string) ([]string, error) {
	if len(args) == 1 && args[0] == "tour-v4-authority-info" {
		return append([]string(nil), args...), nil
	}
	if len(args) == 3 && args[0] == "tour-inspect-v4" &&
		args[1] == "--expected-manifest-sha256" &&
		args[2] == tourV4ManifestSHA256 {
		return append([]string(nil), args...), nil
	}
	if len(args) == 9 && args[0] == "tour-publish-v4" &&
		args[1] == "--bundle" && args[2] == tourV4BundlePath &&
		args[3] == "--expected-manifest-sha256" && args[4] == tourV4ManifestSHA256 &&
		args[5] == "--expected-old-tree" && tourV4ValidOldTree(args[6], true) &&
		args[7] == "--transaction-id" && tourV4DispatchTransactionPattern.MatchString(args[8]) {
		return append([]string(nil), args...), nil
	}
	if len(args) == 7 && args[0] == "tour-recover-v4" &&
		args[1] == "--expected-manifest-sha256" && args[2] == tourV4ManifestSHA256 &&
		args[3] == "--expected-old-tree" && tourV4ValidOldTree(args[4], true) &&
		args[5] == "--transaction-id" && tourV4DispatchTransactionPattern.MatchString(args[6]) {
		return append([]string(nil), args...), nil
	}
	if len(args) == 9 && args[0] == "tour-rollback-v4" &&
		args[1] == "--expected-manifest-sha256" && args[2] == tourV4ManifestSHA256 &&
		args[3] == "--expected-old-tree" && tourV4ValidOldTree(args[4], false) &&
		args[5] == "--expected-current-tree" && args[6] == tourV4PublicSHA256 &&
		args[7] == "--transaction-id" && tourV4DispatchTransactionPattern.MatchString(args[8]) {
		return append([]string(nil), args...), nil
	}
	return nil, fmt.Errorf("tour-v4-dispatch-arguments-invalid")
}

func tourV4ValidOldTree(value string, absentAllowed bool) bool {
	return (absentAllowed && value == "absent") || digestPattern.MatchString(value)
}

func tourV4DetachedMaterials(verified *VerifiedTourPackage) (authority.TourV4DetachedMaterials, error) {
	var empty authority.TourV4DetachedMaterials
	if verified == nil || verified.SourceManifestDigest == "" ||
		verified.SourceManifestDigest != InstallerSourceManifestDigest {
		return empty, fmt.Errorf("tour-v4-dispatch-package-source-invalid")
	}
	get := func(path string, mode os.FileMode) ([]byte, error) {
		record := verified.Files[path]
		if record == nil || record.InstallPath != path || record.Mode != mode ||
			record.Size < 1 || record.Size != int64(len(record.Data)) ||
			record.Digest != digest(record.Data) {
			return nil, fmt.Errorf("tour-v4-dispatch-package-material-invalid")
		}
		return record.Data, nil
	}
	materialization, err := get(tourPackageMaterializationPath, 0o444)
	if err != nil {
		return empty, err
	}
	materializationSignature, err := get(tourPackageMaterializationSignaturePath, 0o444)
	if err != nil {
		return empty, err
	}
	bootstrap, err := get(tourPackageBootstrapPath, 0o444)
	if err != nil {
		return empty, err
	}
	bootstrapSignature, err := get(tourPackageBootstrapSignaturePath, 0o444)
	if err != nil {
		return empty, err
	}
	packageAnchor, err := get(tourPackageAnchorPath, 0o444)
	if err != nil {
		return empty, err
	}
	receiptKey, err := get(tourPackageReceiptKeyPath, 0o400)
	if err != nil {
		return empty, err
	}
	receiptAnchor, err := get(tourPackageReceiptAnchorPath, 0o444)
	if err != nil {
		return empty, err
	}
	return authority.TourV4DetachedMaterials{
		AuthorityBootstrap:          bootstrap,
		AuthorityBootstrapSignature: bootstrapSignature,
		Materialization:             materialization,
		MaterializationSignature:    materializationSignature,
		PackageAnchor:               packageAnchor,
		ReceiptKey:                  receiptKey, ReceiptAnchor: receiptAnchor,
	}, nil
}

func validateTourInstallerSelfBinding(verified *VerifiedTourPackage) error {
	if verified == nil || !digestPattern.MatchString(verified.InstallerBinaryDigest) ||
		verified.InstallerBinarySize < 1 ||
		verified.InstallerBinarySize > maximumMemberBytes {
		return fmt.Errorf("tour-installer-self-binding-invalid")
	}
	actualDigest, actualSize, info, err := executableIdentityDetails()
	if err != nil {
		return fmt.Errorf("tour-installer-self-binding-mismatch")
	}
	metadata, ok := info.Sys().(*syscall.Stat_t)
	if !ok || info.Mode().Perm() != 0o555 || metadata.Uid != 0 ||
		metadata.Gid != 0 || metadata.Nlink != 1 ||
		actualDigest != verified.InstallerBinaryDigest ||
		actualSize != verified.InstallerBinarySize {
		return fmt.Errorf("tour-installer-self-binding-mismatch")
	}
	return nil
}

func DispatchFixedTourV4(args []string, stdout io.Writer) error {
	validated, err := validateTourV4DispatchArguments(args)
	if err != nil {
		return err
	}
	if stdout == nil || os.Geteuid() != 0 || os.Getegid() != 0 {
		return fmt.Errorf("tour-v4-dispatch-root-required")
	}
	packageKey, packageKeyID, err := EmbeddedPackageAuthority()
	if err != nil {
		return err
	}
	defer zero(packageKey)
	verified, err := VerifyTourPackageFile(FixedPackagePath, packageKey, packageKeyID)
	if err != nil {
		return err
	}
	defer verified.Release()
	if err := validateTourInstallerSelfBinding(verified); err != nil {
		return err
	}
	materials, err := tourV4DetachedMaterials(verified)
	if err != nil {
		return err
	}
	if err := validateDirectoryChain(FixedHostRoot, FixedHostRoot, 0); err != nil {
		return fmt.Errorf("tour-v4-dispatch-host-root-invalid")
	}
	if err := syscall.Chroot(FixedHostRoot); err != nil {
		return fmt.Errorf("tour-v4-dispatch-chroot-failed")
	}
	if err := os.Chdir("/"); err != nil {
		return fmt.Errorf("tour-v4-dispatch-chdir-failed")
	}
	return authority.RunAttestedTourV4(
		validated, materials, verified.SourceManifestDigest, stdout,
	)
}
