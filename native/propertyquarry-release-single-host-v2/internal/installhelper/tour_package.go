//go:build linux && amd64

package installhelper

import (
	"archive/tar"
	"bytes"
	"crypto/ed25519"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strconv"
	"syscall"
)

const (
	tourPackageSchema                           = "propertyquarry.release-control.single-host-tour-publication-package.v4"
	tourPackageSignatureDomain                  = "propertyquarry.release-control.single-host-tour-publication-package-manifest-signature.v4\x00"
	tourMaterializationSchema                   = "propertyquarry.release-control.single-host-tour-publication-materialization.v4"
	tourMaterializationSignatureDomain          = "propertyquarry.release-control.single-host-tour-publication-materialization-signature.v4\x00"
	tourAuthorityBootstrapSchema                = "propertyquarry.release-control.single-host-production-authority-bootstrap.v2"
	tourAuthorityBootstrapSignatureDomain       = "propertyquarry.release-control.single-host-production-authority-bootstrap.v2\x00"
	tourPackageProfile                          = "single-host-tour-publication-v4"
	tourPackageArchiveFormat                    = "ustar-v1"
	tourPackageNonAuthoritativeUntil            = "package-and-self-bound-scratch-dispatch-reverification"
	tourPackageAcceptedInstallerMode            = "dispatch-tour-v4"
	tourPublicationTargetRoot                   = "/var/lib/docker/volumes/property_propertyquarry_public_tours/_data"
	tourCanonicalAuthorityRoot                  = "/docker/property/state/runtime/propertyquarry-release-authority-v2.private/authority-static-canonical"
	tourCanonicalPackagePrivateDigest           = "sha256:8b9106db85e8ce423d454bb14c863b6c0d481b061eaae0bd4b584d7071cbc2e1"
	tourPackageTTLSeconds                 int64 = 3600

	tourPackageManifestPath                 = "manifest.tour-v4.json"
	tourPackageManifestSignaturePath        = "manifest.tour-v4.sig"
	tourPackageBootstrapPath                = "material/authority-bootstrap.v2.json"
	tourPackageBootstrapSignaturePath       = "material/authority-bootstrap.v2.sig"
	tourPackageBuildReceiptPath             = "material/native-build-receipt.v2.json"
	tourPackageAnchorPath                   = "material/package-authority-v2.pem"
	tourPackageControllerPath               = "material/propertyquarry-release-single-host-v2"
	tourPackageReceiptKeyPath               = "material/receipt-authority-v2.key"
	tourPackageReceiptAnchorPath            = "material/receipt-authority-v2.pem"
	tourPackageMaterializationPath          = "material/tour-publication-materialization.v4.json"
	tourPackageMaterializationSignaturePath = "material/tour-publication-materialization.v4.sig"
)

var tourPackageAllowedOperations = []string{
	"tour-v4-authority-info",
	"tour-inspect-v4",
	"tour-publish-v4",
	"tour-recover-v4",
	"tour-rollback-v4",
}

type tourPackageFileContract struct {
	mode    os.FileMode
	purpose string
}

var tourPackageFiles = map[string]tourPackageFileContract{
	tourPackageBootstrapPath:                {mode: 0o444, purpose: "authority-bootstrap"},
	tourPackageBootstrapSignaturePath:       {mode: 0o444, purpose: "authority-bootstrap-signature"},
	tourPackageBuildReceiptPath:             {mode: 0o444, purpose: "native-build-receipt"},
	tourPackageAnchorPath:                   {mode: 0o444, purpose: "package-authority-anchor"},
	tourPackageControllerPath:               {mode: 0o555, purpose: "tour-publication-controller"},
	tourPackageReceiptKeyPath:               {mode: 0o400, purpose: "receipt-signing-private-key"},
	tourPackageReceiptAnchorPath:            {mode: 0o444, purpose: "receipt-verification-anchor"},
	tourPackageMaterializationPath:          {mode: 0o444, purpose: "tour-publication-materialization"},
	tourPackageMaterializationSignaturePath: {mode: 0o444, purpose: "tour-publication-materialization-signature"},
}

// VerifiedTourPackage is deliberately disjoint from VerifiedPackage.  It has
// no install paths, runtime deployment plan, runner material, or install
// generation and can only be consumed by the fixed tour dispatcher.
type VerifiedTourPackage struct {
	ArchiveDigest             string
	ManifestRaw               []byte
	ManifestSignature         []byte
	PackageAuthorityKeyID     string
	ReceiptAuthorityKeyID     string
	SourceManifestDigest      string
	InstallerBinaryDigest     string
	InstallerBinarySize       int64
	HostMachineIDDigest       string
	MaterializedAt            int64
	MaterializationValidUntil int64
	MaterializationDigest     string
	Files                     map[string]*FileRecord
}

func (verified *VerifiedTourPackage) Release() {
	if verified == nil {
		return
	}
	zero(verified.ManifestRaw)
	zero(verified.ManifestSignature)
	for _, file := range verified.Files {
		zero(file.Data)
	}
	*verified = VerifiedTourPackage{}
}

func VerifyTourPackageFile(path string, key ed25519.PublicKey, keyID string) (*VerifiedTourPackage, error) {
	file, err := os.OpenFile(path, os.O_RDONLY|syscall.O_CLOEXEC|syscall.O_NOFOLLOW, 0)
	if err != nil {
		return nil, fmt.Errorf("tour-package-unavailable")
	}
	defer file.Close()
	info, err := file.Stat()
	if err != nil || !info.Mode().IsRegular() || info.Size() < 10240 ||
		info.Size() > maximumArchiveBytes || info.Mode().Perm() != 0o400 {
		return nil, fmt.Errorf("tour-package-metadata-invalid")
	}
	metadata, ok := info.Sys().(*syscall.Stat_t)
	if !ok || metadata.Nlink != 1 {
		return nil, fmt.Errorf("tour-package-link-count-invalid")
	}
	raw := make([]byte, info.Size())
	if _, err := io.ReadFull(file, raw); err != nil {
		zero(raw)
		return nil, fmt.Errorf("tour-package-read-failed")
	}
	after, err := file.Stat()
	if err != nil || !os.SameFile(info, after) {
		zero(raw)
		return nil, fmt.Errorf("tour-package-changed")
	}
	verified, err := VerifyTourPackageBytes(raw, key, keyID)
	zero(raw)
	return verified, err
}

func VerifyTourPackageBytes(raw []byte, key ed25519.PublicKey, keyID string) (*VerifiedTourPackage, error) {
	if len(raw) < 10240 || len(raw) > maximumArchiveBytes ||
		len(raw)%10240 != 0 || len(key) != ed25519.PublicKeySize ||
		!digestPattern.MatchString(keyID) {
		return nil, fmt.Errorf("tour-package-input-invalid")
	}
	if err := validateDeterministicUSTAREnvelope(raw); err != nil {
		return nil, fmt.Errorf("tour-package-envelope-invalid")
	}
	reader := tar.NewReader(bytes.NewReader(raw))
	members := make([]archiveMember, 0, 11)
	var previous string
	var total int64
	for {
		header, err := reader.Next()
		if err == io.EOF {
			break
		}
		if err != nil {
			releaseMembers(members)
			return nil, fmt.Errorf("tour-package-tar-invalid")
		}
		if len(members) >= 11 || header.Format != tar.FormatUSTAR ||
			header.Typeflag != tar.TypeReg || header.Name == "" ||
			len([]byte(header.Name)) > 240 || filepath.IsAbs(header.Name) ||
			filepath.Clean(header.Name) != header.Name ||
			header.Uid != 0 || header.Gid != 0 || header.Uname != "" ||
			header.Gname != "" || header.Linkname != "" ||
			!header.ModTime.Equal(header.ModTime.UTC()) || header.ModTime.Unix() != 0 ||
			!header.AccessTime.IsZero() || !header.ChangeTime.IsZero() ||
			header.Devmajor != 0 || header.Devminor != 0 ||
			len(header.PAXRecords) != 0 || len(header.Xattrs) != 0 ||
			header.Size < 1 || header.Size > maximumMemberBytes ||
			header.Mode < 0 || header.Mode > 0o777 ||
			(previous != "" && header.Name <= previous) {
			releaseMembers(members)
			return nil, fmt.Errorf("tour-package-member-metadata-invalid")
		}
		previous = header.Name
		total += header.Size
		if total > maximumArchiveBytes {
			releaseMembers(members)
			return nil, fmt.Errorf("tour-package-expanded-size-invalid")
		}
		data := make([]byte, header.Size)
		if _, err := io.ReadFull(reader, data); err != nil {
			zero(data)
			releaseMembers(members)
			return nil, fmt.Errorf("tour-package-member-read-failed")
		}
		members = append(members, archiveMember{
			name: header.Name, mode: os.FileMode(header.Mode), data: data,
		})
	}
	if len(members) != len(tourPackageFiles)+2 ||
		members[0].name != tourPackageManifestPath || members[0].mode != 0o444 ||
		members[1].name != tourPackageManifestSignaturePath || members[1].mode != 0o444 ||
		len(members[1].data) != ed25519.SignatureSize {
		releaseMembers(members)
		return nil, fmt.Errorf("tour-package-manifest-members-invalid")
	}
	manifestRaw := members[0].data
	signature := members[1].data
	if !ed25519.Verify(key, framed(tourPackageSignatureDomain, manifestRaw), signature) {
		releaseMembers(members)
		return nil, fmt.Errorf("tour-package-signature-invalid")
	}
	manifest, err := strictJSON(manifestRaw, maximumManifestBytes)
	if err != nil {
		releaseMembers(members)
		return nil, fmt.Errorf("tour-package-manifest-invalid")
	}
	verified, err := parseAndBindTourManifest(
		manifest, manifestRaw, signature, digest(raw), members[2:], key, keyID,
	)
	if err != nil {
		releaseMembers(members)
		return nil, err
	}
	for index := range members {
		members[index].data = nil
	}
	return verified, nil
}

func parseAndBindTourManifest(
	value map[string]any,
	raw, signature []byte,
	archiveDigest string,
	payload []archiveMember,
	packageKey ed25519.PublicKey,
	packageKeyID string,
) (*VerifiedTourPackage, error) {
	if !hasKeys(value,
		"accepted_installer_mode", "archive_format", "authority_bootstrap_sha256",
		"files", "host_install_permitted", "materialization_sha256",
		"native_build_receipt_sha256", "network_required", "non_authoritative_until",
		"package_authority_key_id", "package_signing_private_key_included",
		"performs_runtime_deployment", "profile", "receipt_authority_key_id",
		"receipt_signing_private_key_included", "root_helper_verification_required",
		"runtime_deployment_permitted", "schema", "source_manifest_digest", "version",
	) {
		return nil, fmt.Errorf("tour-package-manifest-shape-invalid")
	}
	schema, _ := exactString(value["schema"])
	profile, _ := exactString(value["profile"])
	format, _ := exactString(value["archive_format"])
	mode, _ := exactString(value["accepted_installer_mode"])
	nonAuthoritative, _ := exactString(value["non_authoritative_until"])
	configuredPackageID, _ := exactString(value["package_authority_key_id"])
	receiptID, _ := exactString(value["receipt_authority_key_id"])
	sourceDigest, _ := exactString(value["source_manifest_digest"])
	bootstrapDigest, _ := exactString(value["authority_bootstrap_sha256"])
	materializationDigest, _ := exactString(value["materialization_sha256"])
	buildDigest, _ := exactString(value["native_build_receipt_sha256"])
	version, versionOK := exactInt(value["version"], 4, 4)
	rootRequired, rootOK := value["root_helper_verification_required"].(bool)
	receiptIncluded, receiptIncludedOK := value["receipt_signing_private_key_included"].(bool)
	packageIncluded, packageIncludedOK := value["package_signing_private_key_included"].(bool)
	hostInstall, hostInstallOK := value["host_install_permitted"].(bool)
	runtimeDeploy, runtimeDeployOK := value["runtime_deployment_permitted"].(bool)
	performsRuntime, performsRuntimeOK := value["performs_runtime_deployment"].(bool)
	networkRequired, networkOK := value["network_required"].(bool)
	if schema != tourPackageSchema || profile != tourPackageProfile ||
		format != tourPackageArchiveFormat || mode != tourPackageAcceptedInstallerMode ||
		nonAuthoritative != tourPackageNonAuthoritativeUntil ||
		configuredPackageID != packageKeyID || receiptID == packageKeyID ||
		!digestPattern.MatchString(receiptID) ||
		!digestPattern.MatchString(sourceDigest) ||
		!digestPattern.MatchString(bootstrapDigest) ||
		!digestPattern.MatchString(materializationDigest) ||
		!digestPattern.MatchString(buildDigest) || !versionOK || version != 4 ||
		!rootOK || !rootRequired || !receiptIncludedOK || !receiptIncluded ||
		!packageIncludedOK || packageIncluded || !hostInstallOK || hostInstall ||
		!runtimeDeployOK || runtimeDeploy || !performsRuntimeOK || performsRuntime ||
		!networkOK || networkRequired {
		return nil, fmt.Errorf("tour-package-manifest-binding-invalid")
	}
	items, ok := value["files"].([]any)
	if !ok || len(items) != len(payload) || len(items) != len(tourPackageFiles) {
		return nil, fmt.Errorf("tour-package-file-list-invalid")
	}
	files := make(map[string]*FileRecord, len(items))
	for index, item := range items {
		entry, ok := item.(map[string]any)
		if !ok || !hasKeys(entry, "mode", "path", "purpose", "sha256", "size") {
			return nil, fmt.Errorf("tour-package-file-entry-invalid")
		}
		path, pathOK := exactString(entry["path"])
		purpose, purposeOK := exactString(entry["purpose"])
		modeText, modeOK := exactString(entry["mode"])
		expectedDigest, digestOK := exactString(entry["sha256"])
		size, sizeOK := exactInt(entry["size"], 1, maximumMemberBytes)
		contract, contracted := tourPackageFiles[path]
		if !pathOK || !purposeOK || !modeOK || !digestOK || !sizeOK ||
			!contracted || purpose != contract.purpose ||
			modeText != fmt.Sprintf("%04o", contract.mode) ||
			!digestPattern.MatchString(expectedDigest) ||
			index >= len(payload) || payload[index].name != path {
			return nil, fmt.Errorf("tour-package-file-binding-invalid")
		}
		parsedMode, err := strconv.ParseUint(modeText, 8, 12)
		if err != nil || os.FileMode(parsedMode) != payload[index].mode ||
			size != int64(len(payload[index].data)) ||
			digest(payload[index].data) != expectedDigest {
			return nil, fmt.Errorf("tour-package-file-content-invalid")
		}
		if _, duplicate := files[path]; duplicate {
			return nil, fmt.Errorf("tour-package-file-duplicate")
		}
		files[path] = &FileRecord{
			InstallPath: path, PackagePath: path, Purpose: purpose,
			Mode: os.FileMode(parsedMode), Size: size,
			Digest: expectedDigest, Data: payload[index].data,
		}
	}
	if len(files) != len(tourPackageFiles) {
		return nil, fmt.Errorf("tour-package-file-set-invalid")
	}
	for path, contract := range tourPackageFiles {
		file := files[path]
		if file == nil || file.Mode != contract.mode || file.Purpose != contract.purpose {
			return nil, fmt.Errorf("tour-package-required-file-invalid")
		}
	}
	verified := &VerifiedTourPackage{
		ArchiveDigest:         archiveDigest,
		ManifestRaw:           append([]byte(nil), raw...),
		ManifestSignature:     append([]byte(nil), signature...),
		PackageAuthorityKeyID: packageKeyID,
		ReceiptAuthorityKeyID: receiptID,
		SourceManifestDigest:  sourceDigest,
		MaterializationDigest: materializationDigest,
		Files:                 files,
	}
	if err := verifyTourPayloadBindings(
		verified, packageKey, bootstrapDigest, buildDigest,
	); err != nil {
		verified.Release()
		return nil, err
	}
	return verified, nil
}

func verifyTourPayloadBindings(
	verified *VerifiedTourPackage,
	packageKey ed25519.PublicKey,
	bootstrapDigest, buildDigest string,
) error {
	get := func(path string) []byte { return verified.Files[path].Data }
	anchor, anchorDER, anchorID, err := parsePublicPEM(get(tourPackageAnchorPath))
	if err != nil || anchorID != verified.PackageAuthorityKeyID ||
		!bytes.Equal(anchor, packageKey) {
		zero(anchor)
		zero(anchorDER)
		return fmt.Errorf("tour-package-bundled-anchor-invalid")
	}
	zero(anchor)
	zero(anchorDER)
	receiptPublic, receiptDER, receiptID, err := parsePublicPEM(get(tourPackageReceiptAnchorPath))
	if err != nil || receiptID != verified.ReceiptAuthorityKeyID {
		zero(receiptPublic)
		zero(receiptDER)
		return fmt.Errorf("tour-package-receipt-anchor-invalid")
	}
	defer zero(receiptPublic)
	defer zero(receiptDER)
	receiptPrivate, err := parsePrivatePEM(get(tourPackageReceiptKeyPath))
	if err != nil || !bytes.Equal(receiptPublic, receiptPrivate.Public().(ed25519.PublicKey)) {
		zero(receiptPrivate)
		return fmt.Errorf("tour-package-receipt-key-binding-invalid")
	}
	defer zero(receiptPrivate)
	if digest(get(tourPackageBootstrapPath)) != bootstrapDigest {
		return fmt.Errorf("tour-package-bootstrap-digest-invalid")
	}
	if err := validateTourAuthorityBootstrap(
		get(tourPackageBootstrapPath),
		get(tourPackageBootstrapSignaturePath),
		get(tourPackageAnchorPath),
		get(tourPackageReceiptAnchorPath),
		verified.PackageAuthorityKeyID,
		verified.ReceiptAuthorityKeyID,
		packageKey,
	); err != nil {
		return err
	}
	if digest(get(tourPackageBuildReceiptPath)) != buildDigest {
		return fmt.Errorf("tour-package-build-receipt-digest-invalid")
	}
	build, err := validateNativeBuildReceipt(
		get(tourPackageBuildReceiptPath),
		get(tourPackageControllerPath),
		verified.PackageAuthorityKeyID,
	)
	if err != nil {
		return err
	}
	if build.sourceManifestDigest != verified.SourceManifestDigest {
		return fmt.Errorf("tour-package-source-manifest-binding-invalid")
	}
	verified.InstallerBinaryDigest = build.installerDigest
	verified.InstallerBinarySize = build.installerSize
	if digest(get(tourPackageMaterializationPath)) != verified.MaterializationDigest {
		return fmt.Errorf("tour-package-materialization-digest-invalid")
	}
	materialized, validUntil, hostDigest, err := validateTourMaterialization(
		get(tourPackageMaterializationPath),
		get(tourPackageMaterializationSignaturePath),
		packageKey,
		verified.PackageAuthorityKeyID,
		verified.ReceiptAuthorityKeyID,
		digest(get(tourPackageReceiptAnchorPath)),
		bootstrapDigest,
		buildDigest,
		verified.SourceManifestDigest,
	)
	if err != nil {
		return err
	}
	verified.MaterializedAt = materialized
	verified.MaterializationValidUntil = validUntil
	verified.HostMachineIDDigest = hostDigest
	return nil
}

func validateTourAuthorityBootstrap(
	raw, signature, packageAnchor, receiptAnchor []byte,
	packageID, receiptID string,
	packageKey ed25519.PublicKey,
) error {
	if len(signature) != ed25519.SignatureSize ||
		!ed25519.Verify(packageKey, framed(tourAuthorityBootstrapSignatureDomain, raw), signature) {
		return fmt.Errorf("tour-package-bootstrap-signature-invalid")
	}
	value, err := strictJSON(raw, maximumManifestBytes)
	if err != nil || !hasKeys(value,
		"created_at_epoch", "package_authority_key_id",
		"package_authority_private_sha256", "package_authority_public_sha256",
		"package_authority_source", "receipt_authority_key_id",
		"receipt_authority_public_sha256", "schema", "version",
	) {
		return fmt.Errorf("tour-package-bootstrap-shape-invalid")
	}
	schema, _ := exactString(value["schema"])
	source, _ := exactString(value["package_authority_source"])
	configuredPackageID, _ := exactString(value["package_authority_key_id"])
	privateDigest, _ := exactString(value["package_authority_private_sha256"])
	publicDigest, _ := exactString(value["package_authority_public_sha256"])
	configuredReceiptID, _ := exactString(value["receipt_authority_key_id"])
	receiptDigest, _ := exactString(value["receipt_authority_public_sha256"])
	created, createdOK := exactInt(value["created_at_epoch"], 1, 1<<62)
	version, versionOK := exactInt(value["version"], 2, 2)
	if schema != tourAuthorityBootstrapSchema || source != tourCanonicalAuthorityRoot ||
		configuredPackageID != packageID ||
		privateDigest != tourCanonicalPackagePrivateDigest ||
		publicDigest != digest(packageAnchor) ||
		configuredReceiptID != receiptID ||
		receiptDigest != digest(receiptAnchor) ||
		!createdOK || created < 1 || !versionOK || version != 2 {
		return fmt.Errorf("tour-package-bootstrap-binding-invalid")
	}
	return nil
}

func validateTourMaterialization(
	raw, signature []byte,
	packageKey ed25519.PublicKey,
	packageID, receiptID, receiptAnchorDigest, bootstrapDigest, buildDigest, sourceDigest string,
) (int64, int64, string, error) {
	if len(signature) != ed25519.SignatureSize ||
		!ed25519.Verify(packageKey, framed(tourMaterializationSignatureDomain, raw), signature) {
		return 0, 0, "", fmt.Errorf("tour-package-materialization-signature-invalid")
	}
	value, err := strictJSON(raw, maximumManifestBytes)
	if err != nil || !hasKeys(value,
		"accepted_installer_mode", "allowed_operations", "artifact_bundle_path",
		"artifact_manifest_sha256", "artifact_public_tree_sha256", "artifact_slug",
		"authoritative", "authority_bootstrap_sha256", "host_install_permitted",
		"host_machine_id_digest", "materialized_at_epoch",
		"native_build_receipt_sha256", "network_required",
		"package_authority_key_id", "performs_release_effects",
		"persistent_credential_installation_permitted", "production_ready",
		"publication_dispatch_authorized", "publication_target_root",
		"receipt_authority_key_id", "receipt_authority_public_sha256",
		"root_helper_authorization_required", "runtime_deployment_permitted",
		"schema", "source_manifest_digest", "valid_until_epoch", "version",
	) {
		return 0, 0, "", fmt.Errorf("tour-package-materialization-shape-invalid")
	}
	schema, _ := exactString(value["schema"])
	installerMode, _ := exactString(value["accepted_installer_mode"])
	bundle, _ := exactString(value["artifact_bundle_path"])
	manifestDigest, _ := exactString(value["artifact_manifest_sha256"])
	publicDigest, _ := exactString(value["artifact_public_tree_sha256"])
	slug, _ := exactString(value["artifact_slug"])
	targetRoot, _ := exactString(value["publication_target_root"])
	configuredPackageID, _ := exactString(value["package_authority_key_id"])
	configuredReceiptID, _ := exactString(value["receipt_authority_key_id"])
	configuredReceiptDigest, _ := exactString(value["receipt_authority_public_sha256"])
	configuredBootstrapDigest, _ := exactString(value["authority_bootstrap_sha256"])
	configuredBuildDigest, _ := exactString(value["native_build_receipt_sha256"])
	configuredSourceDigest, _ := exactString(value["source_manifest_digest"])
	hostDigest, _ := exactString(value["host_machine_id_digest"])
	materialized, materializedOK := exactInt(value["materialized_at_epoch"], 1, 1<<62)
	validUntil, validOK := exactInt(value["valid_until_epoch"], 1, 1<<62)
	version, versionOK := exactInt(value["version"], 4, 4)
	falseFields := []string{
		"authoritative", "host_install_permitted", "network_required",
		"performs_release_effects", "persistent_credential_installation_permitted",
		"production_ready", "runtime_deployment_permitted",
	}
	for _, field := range falseFields {
		flag, ok := value[field].(bool)
		if !ok || flag {
			return 0, 0, "", fmt.Errorf("tour-package-materialization-claim-invalid")
		}
	}
	trueFields := []string{
		"publication_dispatch_authorized", "root_helper_authorization_required",
	}
	for _, field := range trueFields {
		flag, ok := value[field].(bool)
		if !ok || !flag {
			return 0, 0, "", fmt.Errorf("tour-package-materialization-claim-invalid")
		}
	}
	if schema != tourMaterializationSchema ||
		installerMode != tourPackageAcceptedInstallerMode ||
		bundle != tourV4BundlePath || manifestDigest != tourV4ManifestSHA256 ||
		publicDigest != tourV4PublicSHA256 || slug != tourV4Slug ||
		targetRoot != tourPublicationTargetRoot ||
		configuredPackageID != packageID || configuredReceiptID != receiptID ||
		configuredReceiptDigest != receiptAnchorDigest ||
		configuredBootstrapDigest != bootstrapDigest ||
		configuredBuildDigest != buildDigest ||
		configuredSourceDigest != sourceDigest ||
		!digestPattern.MatchString(hostDigest) ||
		!materializedOK || !validOK ||
		validUntil != materialized+tourPackageTTLSeconds ||
		!versionOK || version != 4 {
		return 0, 0, "", fmt.Errorf("tour-package-materialization-binding-invalid")
	}
	operations, ok := value["allowed_operations"].([]any)
	if !ok || len(operations) != len(tourPackageAllowedOperations) {
		return 0, 0, "", fmt.Errorf("tour-package-materialization-operations-invalid")
	}
	for index, expected := range tourPackageAllowedOperations {
		actual, ok := operations[index].(string)
		if !ok || actual != expected {
			return 0, 0, "", fmt.Errorf("tour-package-materialization-operations-invalid")
		}
	}
	return materialized, validUntil, hostDigest, nil
}
