package installhelper

import (
	"archive/tar"
	"bytes"
	"crypto/ed25519"
	"crypto/sha256"
	"crypto/x509"
	"encoding/base64"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"encoding/pem"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"syscall"
	"unicode/utf8"

	"propertyquarry.local/release-single-host-v2/internal/authority"
)

const (
	packageManifestSchema                           = "propertyquarry.release-control.single-host-package.v2"
	packageSignatureDomain                          = "propertyquarry.release-control.single-host-package-manifest-signature.v2\x00"
	configSignatureDomain                           = "propertyquarry.release-control.single-host-profile-signature.v2\x00"
	materializationReceiptSchema                    = "propertyquarry.release-control.single-host-production-materialization.v2"
	materializationSignatureDomain                  = "propertyquarry.release-control.single-host-production-materialization.v2\x00"
	runnerReservationSchema                         = "propertyquarry.release-control.single-host-runner-reservation.v2"
	runnerReservationSignatureDomain                = "propertyquarry.release-control.single-host-runner-reservation-signature.v2\x00"
	runnerLaunchTicketSchema                        = "propertyquarry.release-control.single-host-runner-launch-ticket.v2"
	runnerLaunchTicketSignatureDomain               = "propertyquarry.release-control.single-host-runner-launch-ticket-signature.v2\x00"
	runnerPrerequisiteIntentSchema                  = "propertyquarry.release-control.single-host-runner-prerequisite-intent.v2"
	runnerPrerequisiteIntentSignatureDomain         = "propertyquarry.release-control.single-host-runner-prerequisite-intent-signature.v2\x00"
	runnerPrerequisiteApprovalSchema                = "propertyquarry.release-control.single-host-runner-prerequisite-approval.v2"
	runnerPrerequisiteApprovalSignatureDomain       = "propertyquarry.release-control.single-host-runner-prerequisite-approval-signature.v2\x00"
	runnerPrerequisiteJob                           = "propertyquarry-protected-dispatch-inputs"
	runnerLabelDerivationDomain                     = "propertyquarry.release-control.single-host-runner-label.v2\x00"
	maximumArchiveBytes                             = 320 * 1024 * 1024
	maximumMemberBytes                              = 256 * 1024 * 1024
	maximumManifestBytes                            = 1 * 1024 * 1024
	buildReceiptSchema                              = "propertyquarry.release-control.single-host-native-build-receipt.v2"
	predeployBackupHelperPath                       = "/usr/libexec/propertyquarry-release-control/propertyquarry-predeploy-backup-v2"
	predeployBackupHelperDigest                     = "sha256:a7a877b6aae97628892f9c603eddc8267625689676a0daf4685de65613be56d3"
	predeployBackupHelperSize                 int64 = 91482
	databaseControlHelperPath                       = "/usr/libexec/propertyquarry-release-control/propertyquarry-database-control-v2"
	databaseControlHelperDigest                     = "sha256:9bdebcd2bae867ef9ac4e38374e964dc81752b2a572eb8a0568f3bb45d5bfe18"
	databaseControlHelperSize                 int64 = 60449
	runtimeDatabaseHelperPath                       = "/usr/libexec/propertyquarry-release-control/provision_propertyquarry_runtime_database.py"
	runtimeDatabaseHelperDigest                     = "sha256:bc987570cfce12c734cb80b33d7e13199b346c8a8b5406f3ebce88bb15e71a63"
	runtimeDatabaseHelperSize                 int64 = 50770
	runtimeIsolationHelperPath                      = "/usr/libexec/propertyquarry-release-control/propertyquarry-runtime-isolation-v2"
	runtimeIsolationHelperDigest                    = "sha256:a441c978b1fec877d27828f264f35a5dfa203999a8b1260b06ee12fb6f45c413"
	runtimeIsolationHelperSize                int64 = 161070
	runtimeDeployHelperPath                         = "/usr/libexec/propertyquarry-release-control/propertyquarry-runtime-deploy-v2"
	runtimeDeployHelperDigest                       = "sha256:a762c418ffa83aac86b8b503dbd6e9c0ccf41cbc37cd72b21931a9781090691c"
	runtimeDeployHelperSize                   int64 = 82995
	apiHostIP                                       = "127.0.0.1"
	apiHostPort                               int64 = 8097
	apiContainerPort                          int64 = 8090
	databaseImage                                   = "postgres:16-alpine@sha256:16bc17c64a573ef34162af9298258d1aec548232985b33ed7b1eac33ba35c229"
)

var (
	EmbeddedPackageAuthorityDERBase64 = "unbound"
	InstallerSourceManifestDigest     = "sha256:unbound"
	imagePattern                      = regexp.MustCompile(`^ghcr\.io/archonmegalon/propertyquarry-standalone-(web|render)-runtime@sha256:[0-9a-f]{64}$`)
	cloudflaredImagePattern           = regexp.MustCompile(`^cloudflare/cloudflared@sha256:[0-9a-f]{64}$`)
	executablePattern                 = regexp.MustCompile(`^/(usr/(bin|sbin|libexec/propertyquarry-release-control)|bin|sbin)/[A-Za-z0-9._/+:-]+$`)
	stepIDPattern                     = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$`)
	numericIDPattern                  = regexp.MustCompile(`^[1-9][0-9]{0,19}$`)
	deploymentIDPattern               = regexp.MustCompile(`^[0-9a-f]{64}$`)
	envelopeSHAPattern                = regexp.MustCompile(`^[0-9a-f]{64}$`)
	runnerLabelPattern                = regexp.MustCompile(`^pqrelease-[0-9a-f]{32}$`)
	runnerReservationNoncePattern     = regexp.MustCompile(`^[0-9a-f]{64}$`)
)

type FileRecord struct {
	InstallPath string
	PackagePath string
	Purpose     string
	Mode        os.FileMode
	Size        int64
	Digest      string
	Data        []byte
}

type VerifiedPackage struct {
	ManifestRaw                             []byte
	ManifestSignature                       []byte
	ArchiveDigest                           string
	PackageAuthorityKeyID                   string
	ReceiptAuthorityKeyID                   string
	ConfigDigest                            string
	PlanDigest                              string
	BuildReceiptDigest                      string
	InstallerBinaryDigest                   string
	InstallerBinarySize                     int64
	SourceManifestDigest                    string
	RuntimeSHA                              string
	WorkflowSHA                             string
	DeploymentID                            string
	TransactionStartedAt                    int64
	BackupMaxAgeSeconds                     int64
	EnvelopeSHA                             string
	HostMachineIDDigest                     string
	CloudflaredImage                        string
	DatabaseImage                           string
	APIHostIP                               string
	APIHostPort                             int64
	APIContainerPort                        int64
	PrePurgeRootEnvDigest                   string
	PostPurgeRootEnvDigest                  string
	PrePurgeRuntimeInputsDigest             string
	RuntimeInputsDigest                     string
	RuntimeRetirementDigest                 string
	RuntimeDeployDigest                     string
	DatabaseSubstrateDigest                 string
	WebImage                                string
	RenderImage                             string
	SceneVideoEnvPath                       string
	SceneVideoEnvDigest                     string
	SceneVideoEnvMode                       int64
	SceneVideoEnvUID                        int64
	SceneVideoEnvGID                        int64
	ReleaseGeneration                       int64
	MaterializationValidUntil               int64
	RunnerPrerequisiteIntentDigest          string
	RunnerPrerequisiteApprovalDigest        string
	RunnerPrerequisiteApprovalPayloadDigest string
	RunnerPrerequisiteJobID                 string
	Files                                   map[string]*FileRecord
}

func (verified *VerifiedPackage) Release() {
	if verified == nil {
		return
	}
	zero(verified.ManifestRaw)
	zero(verified.ManifestSignature)
	for _, file := range verified.Files {
		zero(file.Data)
	}
	*verified = VerifiedPackage{}
}

func EmbeddedPackageAuthority() (ed25519.PublicKey, string, error) {
	if EmbeddedPackageAuthorityDERBase64 == "" || EmbeddedPackageAuthorityDERBase64 == "unbound" {
		return nil, "", fmt.Errorf("installer-package-authority-unbound")
	}
	der, err := base64.RawStdEncoding.DecodeString(EmbeddedPackageAuthorityDERBase64)
	if err != nil {
		return nil, "", fmt.Errorf("installer-package-authority-invalid")
	}
	defer zero(der)
	parsed, err := x509.ParsePKIXPublicKey(der)
	if err != nil {
		return nil, "", fmt.Errorf("installer-package-authority-invalid")
	}
	key, ok := parsed.(ed25519.PublicKey)
	if !ok || len(key) != ed25519.PublicKeySize {
		return nil, "", fmt.Errorf("installer-package-authority-invalid")
	}
	sum := sha256.Sum256(der)
	return append(ed25519.PublicKey(nil), key...), "sha256:" + fmt.Sprintf("%x", sum[:]), nil
}

func VerifyPackageFile(path string, key ed25519.PublicKey, keyID string) (*VerifiedPackage, error) {
	file, err := os.OpenFile(path, os.O_RDONLY|syscall.O_CLOEXEC|syscall.O_NOFOLLOW, 0)
	if err != nil {
		return nil, fmt.Errorf("package-unavailable")
	}
	defer file.Close()
	info, err := file.Stat()
	if err != nil || !info.Mode().IsRegular() || info.Size() < 1024 || info.Size() > maximumArchiveBytes || info.Mode().Perm() != 0o400 {
		return nil, fmt.Errorf("package-metadata-invalid")
	}
	metadata, ok := info.Sys().(*syscall.Stat_t)
	if !ok || metadata.Nlink != 1 {
		return nil, fmt.Errorf("package-link-count-invalid")
	}
	raw := make([]byte, info.Size())
	if _, err := io.ReadFull(file, raw); err != nil {
		zero(raw)
		return nil, fmt.Errorf("package-read-failed")
	}
	after, err := file.Stat()
	if err != nil || !os.SameFile(info, after) {
		zero(raw)
		return nil, fmt.Errorf("package-changed")
	}
	verified, err := VerifyPackageBytes(raw, key, keyID)
	zero(raw)
	return verified, err
}

type archiveMember struct {
	name string
	mode os.FileMode
	data []byte
}

func VerifyPackageBytes(raw []byte, key ed25519.PublicKey, keyID string) (*VerifiedPackage, error) {
	if len(raw) < 10240 || len(raw) > maximumArchiveBytes || len(raw)%10240 != 0 || len(key) != ed25519.PublicKeySize || !digestPattern.MatchString(keyID) {
		return nil, fmt.Errorf("package-input-invalid")
	}
	if err := validateDeterministicUSTAREnvelope(raw); err != nil {
		return nil, err
	}
	archiveDigest := digest(raw)
	reader := tar.NewReader(bytes.NewReader(raw))
	members := make([]archiveMember, 0, 32)
	var previous string
	total := int64(0)
	for {
		header, err := reader.Next()
		if err == io.EOF {
			break
		}
		if err != nil {
			releaseMembers(members)
			return nil, fmt.Errorf("package-tar-invalid")
		}
		if len(members) >= 64 {
			releaseMembers(members)
			return nil, fmt.Errorf("package-member-count-invalid")
		}
		if header.Format != tar.FormatUSTAR || header.Typeflag != tar.TypeReg || header.Name == "" || len([]byte(header.Name)) > 240 || filepath.IsAbs(header.Name) || filepath.Clean(header.Name) != header.Name || strings.Contains(header.Name, "\\") || strings.HasPrefix(header.Name, "../") || strings.Contains(header.Name, "/../") || header.Uid != 0 || header.Gid != 0 || header.Uname != "" || header.Gname != "" || header.Linkname != "" || !header.ModTime.Equal(header.ModTime.UTC()) || header.ModTime.Unix() != 0 || !header.AccessTime.IsZero() || !header.ChangeTime.IsZero() || header.Devmajor != 0 || header.Devminor != 0 || len(header.PAXRecords) != 0 || len(header.Xattrs) != 0 || header.Size < 1 || header.Size > maximumMemberBytes || header.Mode < 0 || header.Mode > 0o777 || (previous != "" && header.Name <= previous) {
			releaseMembers(members)
			return nil, fmt.Errorf("package-member-metadata-invalid")
		}
		previous = header.Name
		total += header.Size
		if total > maximumArchiveBytes {
			releaseMembers(members)
			return nil, fmt.Errorf("package-expanded-size-invalid")
		}
		data := make([]byte, header.Size)
		if _, err := io.ReadFull(reader, data); err != nil {
			zero(data)
			releaseMembers(members)
			return nil, fmt.Errorf("package-member-read-failed")
		}
		members = append(members, archiveMember{name: header.Name, mode: os.FileMode(header.Mode), data: data})
	}
	if len(members) < 3 || members[0].name != "manifest.v2.json" || members[0].mode != 0o444 || members[1].name != "manifest.v2.sig" || members[1].mode != 0o444 || len(members[1].data) != ed25519.SignatureSize {
		releaseMembers(members)
		return nil, fmt.Errorf("package-manifest-members-invalid")
	}
	manifestRaw := members[0].data
	signature := members[1].data
	if !ed25519.Verify(key, framed(packageSignatureDomain, manifestRaw), signature) {
		releaseMembers(members)
		return nil, fmt.Errorf("package-signature-invalid")
	}
	manifest, err := strictJSON(manifestRaw, maximumManifestBytes)
	if err != nil {
		releaseMembers(members)
		return nil, err
	}
	verified, err := parseAndBindManifest(manifest, manifestRaw, signature, archiveDigest, members[2:], key, keyID)
	if err != nil {
		releaseMembers(members)
		return nil, err
	}
	for index := range members {
		members[index].data = nil
	}
	return verified, nil
}

func validateDeterministicUSTAREnvelope(raw []byte) error {
	const blockSize = 512
	reader := tar.NewReader(bytes.NewReader(raw))
	for offset := 0; offset+blockSize <= len(raw); {
		header := raw[offset : offset+blockSize]
		if allZero(header) {
			if offset+2*blockSize > len(raw) || !allZero(raw[offset+blockSize:offset+2*blockSize]) {
				return fmt.Errorf("package-tar-terminator-invalid")
			}
			expected := ((offset + 2*blockSize + 10239) / 10240) * 10240
			if expected != len(raw) || !allZero(raw[offset:]) {
				return fmt.Errorf("package-tar-trailing-data")
			}
			if _, err := reader.Next(); err != io.EOF {
				return fmt.Errorf("package-tar-terminator-invalid")
			}
			return nil
		}
		parsed, err := reader.Next()
		if err != nil {
			return fmt.Errorf("package-tar-format-invalid")
		}
		canonical, err := canonicalPythonUSTARHeader(parsed)
		if err != nil || !bytes.Equal(header, canonical[:]) {
			return fmt.Errorf("package-tar-header-noncanonical")
		}
		size := parsed.Size
		if size < 1 || size > maximumMemberBytes {
			return fmt.Errorf("package-tar-size-invalid")
		}
		dataStart := offset + blockSize
		padded := ((size + blockSize - 1) / blockSize) * blockSize
		dataEnd := int64(dataStart) + padded
		if dataEnd > int64(len(raw)) {
			return fmt.Errorf("package-tar-truncated")
		}
		contentEnd := int64(dataStart) + size
		if !allZero(raw[contentEnd:dataEnd]) {
			return fmt.Errorf("package-tar-padding-invalid")
		}
		offset = int(dataEnd)
	}
	return fmt.Errorf("package-tar-terminator-missing")
}

func canonicalPythonUSTARHeader(header *tar.Header) ([512]byte, error) {
	var raw [512]byte
	if header == nil || header.Format != tar.FormatUSTAR || header.Typeflag != tar.TypeReg ||
		header.Name == "" || !utf8.ValidString(header.Name) || header.Mode < 0 || header.Mode > 0o777 ||
		header.Uid != 0 || header.Gid != 0 || header.Size < 1 || header.Size > maximumMemberBytes ||
		header.ModTime.Unix() != 0 || header.ModTime.Nanosecond() != 0 ||
		!header.AccessTime.IsZero() || !header.ChangeTime.IsZero() || header.Linkname != "" ||
		header.Uname != "" || header.Gname != "" || header.Devmajor != 0 || header.Devminor != 0 ||
		len(header.PAXRecords) != 0 || len(header.Xattrs) != 0 {
		return raw, fmt.Errorf("ustar-header-metadata-invalid")
	}
	prefix, name, err := splitPythonUSTARName(header.Name)
	if err != nil {
		return raw, err
	}
	copy(raw[0:100], []byte(name))
	if err := formatPythonUSTAROctal(raw[100:108], header.Mode); err != nil {
		return raw, err
	}
	if err := formatPythonUSTAROctal(raw[108:116], 0); err != nil {
		return raw, err
	}
	if err := formatPythonUSTAROctal(raw[116:124], 0); err != nil {
		return raw, err
	}
	if err := formatPythonUSTAROctal(raw[124:136], header.Size); err != nil {
		return raw, err
	}
	if err := formatPythonUSTAROctal(raw[136:148], 0); err != nil {
		return raw, err
	}
	for index := 148; index < 156; index++ {
		raw[index] = ' '
	}
	raw[156] = tar.TypeReg
	copy(raw[257:265], []byte{'u', 's', 't', 'a', 'r', 0, '0', '0'})
	copy(raw[345:500], []byte(prefix))
	checksum := int64(0)
	for _, value := range raw {
		checksum += int64(value)
	}
	digits := strconv.FormatInt(checksum, 8)
	if len(digits) > 6 {
		return [512]byte{}, fmt.Errorf("ustar-checksum-overflow")
	}
	for index := 148; index < 154; index++ {
		raw[index] = '0'
	}
	copy(raw[154-len(digits):154], digits)
	raw[154] = 0
	raw[155] = ' '
	return raw, nil
}

func splitPythonUSTARName(name string) (string, string, error) {
	if len([]byte(name)) <= 100 {
		return "", name, nil
	}
	components := strings.Split(name, "/")
	for index := 1; index < len(components); index++ {
		prefix := strings.Join(components[:index], "/")
		suffix := strings.Join(components[index:], "/")
		if len([]byte(prefix)) <= 155 && len([]byte(suffix)) <= 100 {
			return prefix, suffix, nil
		}
	}
	return "", "", fmt.Errorf("ustar-name-too-long")
}

func formatPythonUSTAROctal(field []byte, value int64) error {
	if len(field) < 2 || value < 0 {
		return fmt.Errorf("ustar-numeric-field-invalid")
	}
	digits := strconv.FormatInt(value, 8)
	if len(digits) > len(field)-1 {
		return fmt.Errorf("ustar-numeric-field-overflow")
	}
	for index := range field[:len(field)-1] {
		field[index] = '0'
	}
	copy(field[len(field)-1-len(digits):len(field)-1], digits)
	field[len(field)-1] = 0
	return nil
}

func allZero(raw []byte) bool {
	for _, value := range raw {
		if value != 0 {
			return false
		}
	}
	return true
}

func parseAndBindManifest(value map[string]any, raw, signature []byte, archiveDigest string, payload []archiveMember, packageKey ed25519.PublicKey, packageKeyID string) (*VerifiedPackage, error) {
	if !hasKeys(value,
		"api_container_port", "api_host_ip", "api_host_port", "archive_format", "authority_profile", "backup_max_age_seconds",
		"build_receipt_digest", "cloudflared_image", "config_digest", "database_image", "database_substrate_digest", "deployment_id",
		"envelope_sha", "files", "host_machine_id_digest", "installed_manifest_path", "installed_manifest_signature_path",
		"non_authoritative_until", "package_authority_key_id", "package_signing_private_key_included", "payload_root", "plan_digest",
		"post_purge_root_env_digest", "pre_purge_root_env_digest", "pre_purge_runtime_inputs_digest", "receipt_authority_key_id",
		"release_generation", "render_image", "root_helper_verification_required", "runtime_deploy_digest", "runtime_inputs_digest",
		"runner_prerequisite_approval_payload_sha256", "runner_prerequisite_approval_sha256", "runner_prerequisite_intent_sha256", "runner_prerequisite_job_id",
		"runtime_retirement_digest", "runtime_sha", "schema", "scene_video_env_digest", "scene_video_env_gid", "scene_video_env_mode",
		"scene_video_env_path", "scene_video_env_uid", "transaction_started_at_epoch", "version", "web_image", "workflow_sha",
	) {
		return nil, fmt.Errorf("package-manifest-shape-invalid")
	}
	schema, _ := exactString(value["schema"])
	profile, _ := exactString(value["authority_profile"])
	archiveFormat, _ := exactString(value["archive_format"])
	nonAuthoritative, _ := exactString(value["non_authoritative_until"])
	buildReceiptDigest, _ := exactString(value["build_receipt_digest"])
	installedManifestPath, _ := exactString(value["installed_manifest_path"])
	installedManifestSignaturePath, _ := exactString(value["installed_manifest_signature_path"])
	payloadRoot, _ := exactString(value["payload_root"])
	configuredPackageKeyID, _ := exactString(value["package_authority_key_id"])
	receiptKeyID, _ := exactString(value["receipt_authority_key_id"])
	configDigest, _ := exactString(value["config_digest"])
	planDigest, _ := exactString(value["plan_digest"])
	runtimeSHA, _ := exactString(value["runtime_sha"])
	workflowSHA, _ := exactString(value["workflow_sha"])
	deploymentID, _ := exactString(value["deployment_id"])
	envelopeSHA, _ := exactString(value["envelope_sha"])
	hostDigest, _ := exactString(value["host_machine_id_digest"])
	cloudflaredImage, _ := exactString(value["cloudflared_image"])
	manifestDatabaseImage, _ := exactString(value["database_image"])
	manifestAPIHostIP, _ := exactString(value["api_host_ip"])
	prePurgeRootEnvDigest, _ := exactString(value["pre_purge_root_env_digest"])
	postPurgeRootEnvDigest, _ := exactString(value["post_purge_root_env_digest"])
	prePurgeRuntimeInputsDigest, _ := exactString(value["pre_purge_runtime_inputs_digest"])
	runtimeInputsDigest, _ := exactString(value["runtime_inputs_digest"])
	runtimeRetirementDigest, _ := exactString(value["runtime_retirement_digest"])
	runtimeDeployDigest, _ := exactString(value["runtime_deploy_digest"])
	databaseSubstrateDigest, _ := exactString(value["database_substrate_digest"])
	webImage, _ := exactString(value["web_image"])
	renderImage, _ := exactString(value["render_image"])
	runnerPrerequisiteIntentDigest, _ := exactString(value["runner_prerequisite_intent_sha256"])
	runnerPrerequisiteApprovalDigest, _ := exactString(value["runner_prerequisite_approval_sha256"])
	runnerPrerequisiteApprovalPayloadDigest, _ := exactString(value["runner_prerequisite_approval_payload_sha256"])
	runnerPrerequisiteJobID, _ := exactString(value["runner_prerequisite_job_id"])
	sceneVideoEnvPath, _ := exactString(value["scene_video_env_path"])
	sceneVideoEnvDigest, _ := exactString(value["scene_video_env_digest"])
	version, versionOK := exactInt(value["version"], 2, 2)
	generation, generationOK := exactInt(value["release_generation"], 1, 1<<62)
	transactionStartedAt, transactionStartedOK := exactInt(value["transaction_started_at_epoch"], 1, 1<<62)
	backupMaxAge, backupMaxAgeOK := exactInt(value["backup_max_age_seconds"], authority.BackupMaxAgeSeconds, authority.BackupMaxAgeSeconds)
	sceneVideoEnvMode, sceneModeOK := exactInt(value["scene_video_env_mode"], 384, 384)
	sceneVideoEnvUID, sceneUIDOK := exactInt(value["scene_video_env_uid"], 1000, 1000)
	sceneVideoEnvGID, sceneGIDOK := exactInt(value["scene_video_env_gid"], 1000, 1000)
	manifestAPIHostPort, apiHostPortOK := exactInt(value["api_host_port"], apiHostPort, apiHostPort)
	manifestAPIContainerPort, apiContainerPortOK := exactInt(value["api_container_port"], apiContainerPort, apiContainerPort)
	required, requiredOK := value["root_helper_verification_required"].(bool)
	privateIncluded, privateIncludedOK := value["package_signing_private_key_included"].(bool)
	if schema != packageManifestSchema || profile != "single-host-production-v2" || archiveFormat != "ustar-v1" || nonAuthoritative != "independent-root-helper-reverification-and-atomic-install" || !digestPattern.MatchString(buildReceiptDigest) || installedManifestPath != "/etc/propertyquarry-release-single-host-v2/package-manifest.v2.json" || installedManifestSignaturePath != "/etc/propertyquarry-release-single-host-v2/package-manifest.v2.sig" || payloadRoot != "payload" || configuredPackageKeyID != packageKeyID || !digestPattern.MatchString(receiptKeyID) || receiptKeyID == packageKeyID || !digestPattern.MatchString(configDigest) || !digestPattern.MatchString(planDigest) || !digestPattern.MatchString(prePurgeRootEnvDigest) || !digestPattern.MatchString(postPurgeRootEnvDigest) || !digestPattern.MatchString(prePurgeRuntimeInputsDigest) || !digestPattern.MatchString(runtimeInputsDigest) || !digestPattern.MatchString(runtimeRetirementDigest) || !digestPattern.MatchString(runtimeDeployDigest) || !digestPattern.MatchString(databaseSubstrateDigest) || !digestPattern.MatchString(runnerPrerequisiteIntentDigest) || !digestPattern.MatchString(runnerPrerequisiteApprovalDigest) || !digestPattern.MatchString(runnerPrerequisiteApprovalPayloadDigest) || !numericIDPattern.MatchString(runnerPrerequisiteJobID) || !shaPattern.MatchString(runtimeSHA) || !shaPattern.MatchString(workflowSHA) || workflowSHA == runtimeSHA || !deploymentIDPattern.MatchString(deploymentID) || !envelopeSHAPattern.MatchString(envelopeSHA) || !digestPattern.MatchString(hostDigest) || !cloudflaredImagePattern.MatchString(cloudflaredImage) || manifestDatabaseImage != databaseImage || !imagePattern.MatchString(webImage) || !strings.HasPrefix(webImage, "ghcr.io/archonmegalon/propertyquarry-standalone-web-runtime@sha256:") || !imagePattern.MatchString(renderImage) || !strings.HasPrefix(renderImage, "ghcr.io/archonmegalon/propertyquarry-standalone-render-runtime@sha256:") || webImage == renderImage || manifestAPIHostIP != apiHostIP || !apiHostPortOK || manifestAPIHostPort != apiHostPort || !apiContainerPortOK || manifestAPIContainerPort != apiContainerPort || !backupMaxAgeOK || backupMaxAge != authority.BackupMaxAgeSeconds || !transactionStartedOK || sceneVideoEnvPath != "/docker/property/state/runtime/property_scene_video_shared.env" || !digestPattern.MatchString(sceneVideoEnvDigest) || !sceneModeOK || sceneVideoEnvMode != 384 || !sceneUIDOK || sceneVideoEnvUID != 1000 || !sceneGIDOK || sceneVideoEnvGID != 1000 || !versionOK || version != 2 || !generationOK || !requiredOK || !required || !privateIncludedOK || privateIncluded {
		return nil, fmt.Errorf("package-manifest-binding-invalid")
	}
	items, ok := value["files"].([]any)
	if !ok || len(items) != len(payload) || len(items) != len(requiredPackageFiles) {
		return nil, fmt.Errorf("package-file-list-invalid")
	}
	files := make(map[string]*FileRecord, len(items))
	for index, item := range items {
		entry, ok := item.(map[string]any)
		if !ok || !hasKeys(entry, "install_path", "mode", "package_path", "purpose", "sha256", "size") {
			return nil, fmt.Errorf("package-file-entry-invalid")
		}
		installPath, installOK := exactString(entry["install_path"])
		packagePath, packageOK := exactString(entry["package_path"])
		purpose, purposeOK := exactString(entry["purpose"])
		modeText, modeOK := exactString(entry["mode"])
		expectedDigest, digestOK := exactString(entry["sha256"])
		size, sizeOK := exactInt(entry["size"], 1, maximumMemberBytes)
		if !installOK || !packageOK || !purposeOK || !modeOK || !digestOK || !sizeOK || !validInstallPath(installPath) || packagePath != "payload"+installPath || !modePattern.MatchString(modeText) || !digestPattern.MatchString(expectedDigest) || index >= len(payload) || payload[index].name != packagePath {
			return nil, fmt.Errorf("package-file-binding-invalid")
		}
		parsedMode, err := strconv.ParseUint(modeText, 8, 12)
		if err != nil || os.FileMode(parsedMode) != payload[index].mode || size != int64(len(payload[index].data)) || digest(payload[index].data) != expectedDigest {
			return nil, fmt.Errorf("package-file-content-invalid")
		}
		if _, duplicate := files[installPath]; duplicate {
			return nil, fmt.Errorf("package-file-duplicate")
		}
		files[installPath] = &FileRecord{InstallPath: installPath, PackagePath: packagePath, Purpose: purpose, Mode: os.FileMode(parsedMode), Size: size, Digest: expectedDigest, Data: payload[index].data}
	}
	if err := validateRequiredFiles(files); err != nil {
		return nil, err
	}
	verified := &VerifiedPackage{
		ManifestRaw: append([]byte(nil), raw...), ManifestSignature: append([]byte(nil), signature...), ArchiveDigest: archiveDigest,
		PackageAuthorityKeyID: packageKeyID, ReceiptAuthorityKeyID: receiptKeyID, ConfigDigest: configDigest, PlanDigest: planDigest,
		BuildReceiptDigest: buildReceiptDigest, RuntimeSHA: runtimeSHA, WorkflowSHA: workflowSHA, DeploymentID: deploymentID, TransactionStartedAt: transactionStartedAt,
		BackupMaxAgeSeconds: backupMaxAge, EnvelopeSHA: envelopeSHA, HostMachineIDDigest: hostDigest, CloudflaredImage: cloudflaredImage,
		DatabaseImage: manifestDatabaseImage, APIHostIP: manifestAPIHostIP, APIHostPort: manifestAPIHostPort, APIContainerPort: manifestAPIContainerPort,
		PrePurgeRootEnvDigest: prePurgeRootEnvDigest, PostPurgeRootEnvDigest: postPurgeRootEnvDigest,
		PrePurgeRuntimeInputsDigest: prePurgeRuntimeInputsDigest, RuntimeInputsDigest: runtimeInputsDigest,
		RuntimeRetirementDigest: runtimeRetirementDigest, RuntimeDeployDigest: runtimeDeployDigest, DatabaseSubstrateDigest: databaseSubstrateDigest,
		WebImage: webImage, RenderImage: renderImage, SceneVideoEnvPath: sceneVideoEnvPath, SceneVideoEnvDigest: sceneVideoEnvDigest,
		SceneVideoEnvMode: sceneVideoEnvMode, SceneVideoEnvUID: sceneVideoEnvUID, SceneVideoEnvGID: sceneVideoEnvGID,
		ReleaseGeneration: generation, Files: files,
		RunnerPrerequisiteIntentDigest:          runnerPrerequisiteIntentDigest,
		RunnerPrerequisiteApprovalDigest:        runnerPrerequisiteApprovalDigest,
		RunnerPrerequisiteApprovalPayloadDigest: runnerPrerequisiteApprovalPayloadDigest,
		RunnerPrerequisiteJobID:                 runnerPrerequisiteJobID,
	}
	if err := verifyPayloadBindings(verified, packageKey); err != nil {
		verified.Release()
		return nil, err
	}
	return verified, nil
}

func validInstallPath(path string) bool {
	if !filepath.IsAbs(path) || filepath.Clean(path) != path || len(path) > 4096 || strings.Contains(path, "\\") {
		return false
	}
	for _, prefix := range []string{"/etc/propertyquarry-release-single-host-v2/", "/var/lib/propertyquarry-release-single-host-v2/", "/usr/libexec/propertyquarry-release-control/", "/usr/lib/propertyquarry-release-runner-v2/", "/usr/lib/systemd/system/", "/usr/lib/sysusers.d/", "/usr/lib/tmpfiles.d/"} {
		if strings.HasPrefix(path, prefix) {
			return true
		}
	}
	return false
}

type requiredPackageFile struct {
	mode    os.FileMode
	purpose string
	size    int64
	digest  string
}

var requiredPackageFiles = map[string]requiredPackageFile{
	"/usr/libexec/propertyquarry-release-control/propertyquarry-release-single-host-v2":            {mode: 0o755, purpose: "controller-binary"},
	"/etc/propertyquarry-release-single-host-v2/native-build-receipt.v2.json":                      {mode: 0o444, purpose: "native-build-receipt"},
	"/etc/propertyquarry-release-single-host-v2/authority.v2.json":                                 {mode: 0o400, purpose: "signed-authority-profile"},
	"/etc/propertyquarry-release-single-host-v2/authority.v2.sig":                                  {mode: 0o444, purpose: "authority-profile-signature"},
	"/etc/propertyquarry-release-single-host-v2/transaction-plan.v2.json":                          {mode: 0o444, purpose: "signed-by-profile-transaction-plan"},
	"/etc/propertyquarry-release-single-host-v2/materialization-receipt.v2.json":                   {mode: 0o444, purpose: "package-authority-signed-materialization-receipt"},
	"/etc/propertyquarry-release-single-host-v2/materialization-receipt.v2.sig":                    {mode: 0o444, purpose: "materialization-receipt-signature"},
	"/etc/propertyquarry-release-single-host-v2/package-authority-v2.pem":                          {mode: 0o444, purpose: "package-authority-anchor"},
	"/etc/propertyquarry-release-single-host-v2/receipt-authority-v2.key":                          {mode: 0o400, purpose: "receipt-authority-private-key"},
	"/etc/propertyquarry-release-single-host-v2/receipt-authority-v2.pem":                          {mode: 0o444, purpose: "receipt-authority-anchor"},
	"/usr/lib/systemd/system/propertyquarry-release-single-host-v2.socket":                         {mode: 0o444, purpose: "systemd-socket-unit"},
	"/usr/lib/systemd/system/propertyquarry-release-single-host-v2@.service":                       {mode: 0o444, purpose: "systemd-service-template"},
	"/usr/lib/systemd/system/propertyquarry-release-single-host-v2-activation-canary.service":      {mode: 0o444, purpose: "systemd-activation-canary-unit"},
	"/usr/lib/sysusers.d/propertyquarry-release-single-host-v2.conf":                               {mode: 0o444, purpose: "sysusers-definition"},
	"/usr/lib/tmpfiles.d/propertyquarry-release-single-host-v2.conf":                               {mode: 0o444, purpose: "tmpfiles-definition"},
	"/usr/lib/propertyquarry-release-runner-v2/runner.lock.json":                                   {mode: 0o444, purpose: "ephemeral-runner-lock"},
	"/usr/libexec/propertyquarry-release-control/run-propertyquarry-ephemeral-runner-v2":           {mode: 0o555, purpose: "ephemeral-runner-launcher"},
	"/usr/libexec/propertyquarry-release-control/run-propertyquarry-ephemeral-runner-lifecycle-v2": {mode: 0o555, purpose: "ephemeral-runner-root-lifecycle"},
	"/var/lib/propertyquarry-release-single-host-v2/runner-launch-ticket.v2.json":                  {mode: 0o400, purpose: "ephemeral-runner-launch-ticket"},
	"/var/lib/propertyquarry-release-single-host-v2/runner-reservation.v2.json":                    {mode: 0o400, purpose: "ephemeral-runner-reservation"},
	"/var/lib/propertyquarry-release-single-host-v2/runner-prerequisite-intent.v2.json":            {mode: 0o400, purpose: "ephemeral-runner-prerequisite-approval-intent"},
	"/var/lib/propertyquarry-release-single-host-v2/runner-prerequisite-approval.v2.json":          {mode: 0o400, purpose: "ephemeral-runner-prerequisite-approval-proof"},
	predeployBackupHelperPath:  {mode: 0o755, purpose: "predeploy-backup-helper", size: predeployBackupHelperSize, digest: predeployBackupHelperDigest},
	databaseControlHelperPath:  {mode: 0o755, purpose: "database-control-helper", size: databaseControlHelperSize, digest: databaseControlHelperDigest},
	runtimeDatabaseHelperPath:  {mode: 0o755, purpose: "runtime-database-helper", size: runtimeDatabaseHelperSize, digest: runtimeDatabaseHelperDigest},
	runtimeIsolationHelperPath: {mode: 0o755, purpose: "runtime-isolation-helper", size: runtimeIsolationHelperSize, digest: runtimeIsolationHelperDigest},
	runtimeDeployHelperPath:    {mode: 0o755, purpose: "runtime-deploy-helper", size: runtimeDeployHelperSize, digest: runtimeDeployHelperDigest},
}

func validateRequiredFiles(files map[string]*FileRecord) error {
	if len(files) != len(requiredPackageFiles) {
		return fmt.Errorf("package-required-file-count-invalid")
	}
	for path, required := range requiredPackageFiles {
		file, ok := files[path]
		if !ok || file.Mode != required.mode || file.Purpose != required.purpose ||
			(required.size != 0 && file.Size != required.size) || (required.digest != "" && file.Digest != required.digest) {
			return fmt.Errorf("package-required-file-invalid")
		}
	}
	return nil
}

type runnerMaterialBinding struct {
	launchTicketDigest     string
	sourceCheckoutIdentity string
	sourceCheckoutPath     string
	sourceTreeDigest       string
	boundAt                int64
}

func validateSignedRunnerWire(raw []byte, public ed25519.PublicKey, keyID, domain string) (map[string]any, error) {
	wrapper, err := strictJSON(raw, maximumManifestBytes)
	if err != nil || !hasKeys(wrapper, "payload", "signature", "signature_key_id") || wrapper["signature_key_id"] != keyID {
		return nil, fmt.Errorf("package-runner-wire-wrapper-invalid")
	}
	payload, payloadOK := wrapper["payload"].(map[string]any)
	signatureText, signatureOK := exactString(wrapper["signature"])
	if !payloadOK || !signatureOK {
		return nil, fmt.Errorf("package-runner-wire-shape-invalid")
	}
	signature, decodeErr := base64.RawURLEncoding.DecodeString(signatureText)
	canonicalPayload, canonicalErr := canonicalJSON(payload)
	canonicalWrapper, wrapperErr := canonicalJSON(wrapper)
	if decodeErr != nil || canonicalErr != nil || wrapperErr != nil || !bytes.Equal(canonicalWrapper, raw) || len(signature) != ed25519.SignatureSize || base64.RawURLEncoding.EncodeToString(signature) != signatureText || !ed25519.Verify(public, framed(domain, canonicalPayload), signature) {
		zero(signature)
		zero(canonicalPayload)
		zero(canonicalWrapper)
		return nil, fmt.Errorf("package-runner-wire-signature-invalid")
	}
	zero(signature)
	zero(canonicalPayload)
	zero(canonicalWrapper)
	return payload, nil
}

type runnerPrerequisiteBinding struct {
	intentDigest          string
	approvalDigest        string
	approvalPayloadDigest string
	jobID                 string
}

func validateRunnerPrerequisiteMaterial(intentRaw, approvalRaw, reservationRaw []byte, config *authority.Config, receiptKeyID string, receiptPublic ed25519.PublicKey) (runnerPrerequisiteBinding, error) {
	var empty runnerPrerequisiteBinding
	if config == nil || config.RunnerJobID == config.RunnerPrerequisiteJobID {
		return empty, fmt.Errorf("package-runner-prerequisite-input-invalid")
	}
	reservation, err := validateSignedRunnerWire(reservationRaw, receiptPublic, receiptKeyID, runnerReservationSignatureDomain)
	if err != nil {
		return empty, err
	}
	intent, err := validateSignedRunnerWire(intentRaw, receiptPublic, receiptKeyID, runnerPrerequisiteIntentSignatureDomain)
	if err != nil {
		return empty, err
	}
	if !hasKeys(intent,
		"authority_profile", "comment", "discovered_at_epoch", "environment_id", "environment_name",
		"initial_jobs_sha256", "initial_pending_deployments_sha256", "initial_runs_index_sha256",
		"prerequisite_job_id", "prerequisite_job_name", "receipt_authority_key_id", "release_job",
		"repository", "repository_id", "repository_owner_id", "reservation_expires_at_epoch",
		"reservation_sha256", "run_attempt", "run_id", "runner_label", "schema", "version",
		"workflow_path", "workflow_ref", "workflow_sha",
	) {
		return empty, fmt.Errorf("package-runner-prerequisite-intent-shape-invalid")
	}
	discovered, discoveredOK := exactInt(intent["discovered_at_epoch"], 1, 1<<62)
	created, createdOK := exactInt(reservation["created_at_epoch"], 1, 1<<62)
	expires, expiresOK := exactInt(reservation["expires_at_epoch"], 1, 1<<62)
	intentEnvironmentID, intentEnvironmentOK := exactString(intent["environment_id"])
	intentJobID, intentJobOK := exactString(intent["prerequisite_job_id"])
	intentRunID, intentRunOK := exactString(intent["run_id"])
	intentAttempt, intentAttemptOK := exactInt(intent["run_attempt"], 1, 1<<31-1)
	intentVersion, intentVersionOK := exactInt(intent["version"], 2, 2)
	if intent["schema"] != runnerPrerequisiteIntentSchema || !intentVersionOK || intentVersion != 2 ||
		intent["authority_profile"] != "single-host-production-v2" || intent["repository"] != authority.Repository ||
		intent["repository_id"] != authority.RepositoryID || intent["repository_owner_id"] != authority.RepositoryOwnerID ||
		intent["workflow_path"] != ".github/workflows/smoke-runtime.yml" || intent["workflow_ref"] != authority.WorkflowRef ||
		intent["workflow_sha"] != config.WorkflowSHA || intent["receipt_authority_key_id"] != receiptKeyID ||
		intent["reservation_sha256"] != digest(reservationRaw) || intent["reservation_sha256"] != config.RunnerReservationDigest ||
		!expiresOK || intent["reservation_expires_at_epoch"] != json.Number(strconv.FormatInt(expires, 10)) ||
		intent["runner_label"] != config.RunnerLabel || intent["runner_label"] != reservation["runner_label"] ||
		intent["environment_name"] != authority.Environment || !intentEnvironmentOK || !numericIDPattern.MatchString(intentEnvironmentID) ||
		intent["prerequisite_job_name"] != runnerPrerequisiteJob || !intentJobOK || !numericIDPattern.MatchString(intentJobID) || intentJobID != config.RunnerPrerequisiteJobID ||
		intent["release_job"] != authority.ReleaseJob || !intentRunOK || !numericIDPattern.MatchString(intentRunID) || intentRunID != config.RunnerRunID ||
		!intentAttemptOK || intentAttempt != config.RunnerRunAttempt || !discoveredOK || !createdOK || discovered < created || discovered > expires ||
		intent["comment"] != "PropertyQuarry governed prerequisite approval "+digest(reservationRaw) ||
		intent["initial_jobs_sha256"] == nil || intent["initial_pending_deployments_sha256"] == nil || intent["initial_runs_index_sha256"] == nil ||
		digest(intentRaw) != config.RunnerPrerequisiteIntentDigest {
		return empty, fmt.Errorf("package-runner-prerequisite-intent-binding-invalid")
	}
	for _, key := range []string{"initial_jobs_sha256", "initial_pending_deployments_sha256", "initial_runs_index_sha256"} {
		text, ok := exactString(intent[key])
		if !ok || !digestPattern.MatchString(text) {
			return empty, fmt.Errorf("package-runner-prerequisite-intent-evidence-invalid")
		}
	}

	approval, err := validateSignedRunnerWire(approvalRaw, receiptPublic, receiptKeyID, runnerPrerequisiteApprovalSignatureDomain)
	if err != nil {
		return empty, err
	}
	if !hasKeys(approval,
		"approval_api_disposition", "approval_response_sha256", "approved_at_epoch", "completed_jobs_sha256",
		"environment_id", "environment_name", "intent_sha256", "post_pending_deployments_sha256",
		"prerequisite_conclusion", "prerequisite_job_id", "prerequisite_job_name", "receipt_authority_key_id",
		"release_job", "repository", "repository_id", "repository_owner_id", "reservation_expires_at_epoch",
		"reservation_sha256", "review_history_sha256", "run_attempt", "run_id", "runner_label", "schema", "version",
		"workflow_path", "workflow_ref", "workflow_sha",
	) {
		return empty, fmt.Errorf("package-runner-prerequisite-approval-shape-invalid")
	}
	approved, approvedOK := exactInt(approval["approved_at_epoch"], 1, 1<<62)
	approvalVersion, approvalVersionOK := exactInt(approval["version"], 2, 2)
	disposition, dispositionOK := exactString(approval["approval_api_disposition"])
	approvalPayloadRaw, payloadErr := canonicalJSON(approval)
	defer zero(approvalPayloadRaw)
	if approval["schema"] != runnerPrerequisiteApprovalSchema || !approvalVersionOK || approvalVersion != 2 ||
		approval["intent_sha256"] != digest(intentRaw) || approval["reservation_sha256"] != intent["reservation_sha256"] ||
		approval["runner_label"] != intent["runner_label"] || approval["run_id"] != intent["run_id"] || approval["run_attempt"] != intent["run_attempt"] ||
		approval["prerequisite_job_id"] != intent["prerequisite_job_id"] || approval["prerequisite_job_name"] != runnerPrerequisiteJob ||
		approval["prerequisite_conclusion"] != "success" || approval["environment_id"] != intent["environment_id"] ||
		approval["environment_name"] != authority.Environment || approval["receipt_authority_key_id"] != receiptKeyID ||
		approval["repository"] != authority.Repository || approval["repository_id"] != authority.RepositoryID ||
		approval["repository_owner_id"] != authority.RepositoryOwnerID || approval["workflow_path"] != ".github/workflows/smoke-runtime.yml" ||
		approval["workflow_ref"] != authority.WorkflowRef || approval["workflow_sha"] != config.WorkflowSHA || approval["release_job"] != authority.ReleaseJob ||
		approval["reservation_expires_at_epoch"] != intent["reservation_expires_at_epoch"] || !dispositionOK ||
		(disposition != "approved" && disposition != "post-approved-recovered") || !approvedOK || approved < discovered || approved > expires || approved > config.TransactionStartedAtEpoch ||
		payloadErr != nil || digest(approvalRaw) != config.RunnerPrerequisiteApprovalDigest || digest(approvalPayloadRaw) != config.RunnerPrerequisiteApprovalPayloadDigest {
		return empty, fmt.Errorf("package-runner-prerequisite-approval-binding-invalid")
	}
	if disposition == "approved" {
		response, ok := exactString(approval["approval_response_sha256"])
		if !ok || !digestPattern.MatchString(response) {
			return empty, fmt.Errorf("package-runner-prerequisite-approval-response-invalid")
		}
	} else if approval["approval_response_sha256"] != nil {
		return empty, fmt.Errorf("package-runner-prerequisite-approval-response-invalid")
	}
	for _, key := range []string{"completed_jobs_sha256", "post_pending_deployments_sha256", "review_history_sha256"} {
		text, ok := exactString(approval[key])
		if !ok || !digestPattern.MatchString(text) {
			return empty, fmt.Errorf("package-runner-prerequisite-approval-evidence-invalid")
		}
	}
	return runnerPrerequisiteBinding{
		intentDigest: digest(intentRaw), approvalDigest: digest(approvalRaw),
		approvalPayloadDigest: digest(approvalPayloadRaw), jobID: intentJobID,
	}, nil
}

func validateRunnerMaterial(reservationRaw, ticketRaw []byte, config *authority.Config, planDigest, receiptKeyID string, receiptPublic ed25519.PublicKey) (runnerMaterialBinding, error) {
	var empty runnerMaterialBinding
	reservation, err := validateSignedRunnerWire(reservationRaw, receiptPublic, receiptKeyID, runnerReservationSignatureDomain)
	if err != nil {
		return empty, err
	}
	if !hasKeys(reservation,
		"authority_profile", "created_at_epoch", "environment", "expires_at_epoch", "receipt_authority_key_id",
		"release_job", "repository", "repository_id", "repository_owner_id", "reservation_nonce", "runner_label",
		"runner_label_nonce", "schema", "source_checkout_identity_sha256", "source_checkout_path", "source_tree_sha256",
		"version", "workflow_path", "workflow_ref", "workflow_sha",
	) {
		return empty, fmt.Errorf("package-runner-reservation-shape-invalid")
	}
	created, createdOK := exactInt(reservation["created_at_epoch"], 1, 1<<62)
	expires, expiresOK := exactInt(reservation["expires_at_epoch"], 1, 1<<62)
	nonce, nonceOK := exactString(reservation["reservation_nonce"])
	label, labelOK := exactString(reservation["runner_label"])
	sourceIdentity, sourceIdentityOK := exactString(reservation["source_checkout_identity_sha256"])
	sourcePath, sourcePathOK := exactString(reservation["source_checkout_path"])
	sourceTree, sourceTreeOK := exactString(reservation["source_tree_sha256"])
	version, versionOK := exactInt(reservation["version"], 2, 2)
	if reservation["schema"] != runnerReservationSchema || !versionOK || version != 2 || reservation["authority_profile"] != "single-host-production-v2" || reservation["environment"] != authority.Environment || reservation["repository"] != authority.Repository || reservation["repository_id"] != authority.RepositoryID || reservation["repository_owner_id"] != authority.RepositoryOwnerID || reservation["workflow_path"] != ".github/workflows/smoke-runtime.yml" || reservation["workflow_ref"] != authority.WorkflowRef || reservation["workflow_sha"] != config.WorkflowSHA || reservation["release_job"] != authority.ReleaseJob || reservation["receipt_authority_key_id"] != receiptKeyID || !createdOK || !expiresOK || expires-created != 21600 || config.TransactionStartedAtEpoch < created || config.TransactionStartedAtEpoch > expires || !nonceOK || !runnerReservationNoncePattern.MatchString(nonce) || !labelOK || !runnerLabelPattern.MatchString(label) || reservation["runner_label_nonce"] != strings.TrimPrefix(label, "pqrelease-") || !sourceIdentityOK || !digestPattern.MatchString(sourceIdentity) || !sourcePathOK || sourcePath != "/docker/property/state/runtime/propertyquarry-release-authority-v2.private/single-host-v2-release-checkouts/"+config.WorkflowSHA || !sourceTreeOK || !digestPattern.MatchString(sourceTree) {
		return empty, fmt.Errorf("package-runner-reservation-binding-invalid")
	}
	nonceRaw, decodeErr := hex.DecodeString(nonce)
	if decodeErr != nil {
		return empty, fmt.Errorf("package-runner-reservation-nonce-invalid")
	}
	derivedInput := append([]byte(runnerLabelDerivationDomain), nonceRaw...)
	derived := sha256.Sum256(derivedInput)
	zero(nonceRaw)
	zero(derivedInput)
	if label != "pqrelease-"+hex.EncodeToString(derived[:16]) || digest(reservationRaw) != config.RunnerReservationDigest || label != config.RunnerLabel {
		return empty, fmt.Errorf("package-runner-reservation-label-or-config-invalid")
	}

	ticket, err := validateSignedRunnerWire(ticketRaw, receiptPublic, receiptKeyID, runnerLaunchTicketSignatureDomain)
	if err != nil {
		return empty, err
	}
	if !hasKeys(ticket,
		"authority_profile", "bound_at_epoch", "config_digest", "dispatch_ticket_sha256", "docker_socket", "environment",
		"expires_at_epoch", "job_id", "plan_digest", "receipt_authority_key_id", "release_job", "repository", "repository_id",
		"repository_owner_id", "reservation_nonce", "run_attempt", "run_id", "runner_image", "runner_label", "runner_label_nonce",
		"runner_prerequisite_approval_payload_sha256", "runner_prerequisite_approval_sha256", "runner_prerequisite_intent_sha256", "runner_prerequisite_job_id",
		"runtime_sha", "schema", "version", "workflow_path", "workflow_ref", "workflow_sha",
	) {
		return empty, fmt.Errorf("package-runner-ticket-shape-invalid")
	}
	boundAt, boundOK := exactInt(ticket["bound_at_epoch"], 1, 1<<62)
	ticketExpires, ticketExpiresOK := exactInt(ticket["expires_at_epoch"], 1, 1<<62)
	attempt, attemptOK := exactInt(ticket["run_attempt"], 1, 1<<31-1)
	ticketVersion, ticketVersionOK := exactInt(ticket["version"], 2, 2)
	socket, socketOK := ticket["docker_socket"].(map[string]any)
	if ticket["schema"] != runnerLaunchTicketSchema || !ticketVersionOK || ticketVersion != 2 || ticket["authority_profile"] != "single-host-production-v2" || ticket["environment"] != authority.Environment || ticket["repository"] != authority.Repository || ticket["repository_id"] != authority.RepositoryID || ticket["repository_owner_id"] != authority.RepositoryOwnerID || ticket["workflow_path"] != ".github/workflows/smoke-runtime.yml" || ticket["workflow_ref"] != authority.WorkflowRef || ticket["workflow_sha"] != config.WorkflowSHA || ticket["release_job"] != authority.ReleaseJob || ticket["runtime_sha"] != config.RuntimeSHA || ticket["config_digest"] != config.Digest || ticket["plan_digest"] != planDigest || ticket["receipt_authority_key_id"] != receiptKeyID || ticket["dispatch_ticket_sha256"] != config.RunnerReservationDigest || ticket["reservation_nonce"] != nonce || ticket["runner_label"] != config.RunnerLabel || ticket["runner_label_nonce"] != strings.TrimPrefix(config.RunnerLabel, "pqrelease-") || ticket["runner_prerequisite_intent_sha256"] != config.RunnerPrerequisiteIntentDigest || ticket["runner_prerequisite_approval_sha256"] != config.RunnerPrerequisiteApprovalDigest || ticket["runner_prerequisite_approval_payload_sha256"] != config.RunnerPrerequisiteApprovalPayloadDigest || ticket["runner_prerequisite_job_id"] != config.RunnerPrerequisiteJobID || ticket["run_id"] != config.RunnerRunID || !attemptOK || attempt != config.RunnerRunAttempt || ticket["job_id"] != config.RunnerJobID || ticket["runner_image"] != config.WebImage || !boundOK || boundAt < config.TransactionStartedAtEpoch || !ticketExpiresOK || ticketExpires <= boundAt || ticketExpires-boundAt > 1800 || ticketExpires > expires || !socketOK {
		return empty, fmt.Errorf("package-runner-ticket-binding-invalid")
	}
	device, deviceOK := exactInt(socket["device"], 1, 1<<62)
	inode, inodeOK := exactInt(socket["inode"], 1, 1<<62)
	uid, uidOK := exactInt(socket["uid"], 0, 0)
	gid, gidOK := exactInt(socket["gid"], 112, 112)
	nlink, nlinkOK := exactInt(socket["nlink"], 1, 1)
	if !hasKeys(socket, "device", "gid", "inode", "mode", "nlink", "path", "uid") || !deviceOK || device < 1 || !inodeOK || inode < 1 || !uidOK || uid != 0 || !gidOK || gid != 112 || !nlinkOK || nlink != 1 || socket["mode"] != "0660" || socket["path"] != "/var/run/docker.sock" {
		return empty, fmt.Errorf("package-runner-ticket-socket-invalid")
	}
	return runnerMaterialBinding{
		launchTicketDigest: digest(ticketRaw), sourceCheckoutIdentity: sourceIdentity,
		sourceCheckoutPath: sourcePath, sourceTreeDigest: sourceTree, boundAt: boundAt,
	}, nil
}

func validateMaterializationReceipt(raw, signature []byte, verified *VerifiedPackage, packageKey ed25519.PublicKey, runner runnerMaterialBinding, prerequisite runnerPrerequisiteBinding) (int64, error) {
	if len(signature) != ed25519.SignatureSize || !ed25519.Verify(packageKey, framed(materializationSignatureDomain, raw), signature) {
		return 0, fmt.Errorf("package-materialization-receipt-signature-invalid")
	}
	value, err := strictJSON(raw, maximumManifestBytes)
	if err != nil || !hasKeys(value,
		"authoritative", "config_sha256", "deployment_id", "final_artifact_id", "final_artifact_sha256",
		"image_publication_run_attempt", "image_publication_run_completed_at_epoch", "image_publication_run_id",
		"installed_state_absence_proven", "materialized_at_epoch", "observation_completed_at_epoch",
		"package_authority_key_id", "plan_sha256", "preflight_artifact_id", "preflight_artifact_sha256",
		"production_ready", "receipt_authority_key_id", "release_generation", "release_hygiene_sha256",
		"render_attestation_id", "root_helper_authorization_required", "runner_launch_ticket_sha256",
		"runner_prerequisite_approval_payload_sha256", "runner_prerequisite_approval_sha256", "runner_prerequisite_intent_sha256", "runner_prerequisite_job_id",
		"runner_source_checkout_identity_sha256", "runner_source_checkout_path", "runner_source_tree_sha256",
		"runtime_sha", "schema", "valid_until_epoch",
		"version", "web_attestation_id", "workflow_sha",
	) {
		return 0, fmt.Errorf("package-materialization-receipt-shape-invalid")
	}
	schema, _ := exactString(value["schema"])
	configDigest, _ := exactString(value["config_sha256"])
	planDigest, _ := exactString(value["plan_sha256"])
	deploymentID, _ := exactString(value["deployment_id"])
	packageKeyID, _ := exactString(value["package_authority_key_id"])
	receiptKeyID, _ := exactString(value["receipt_authority_key_id"])
	runtimeSHA, _ := exactString(value["runtime_sha"])
	workflowSHA, _ := exactString(value["workflow_sha"])
	version, versionOK := exactInt(value["version"], 2, 2)
	generation, generationOK := exactInt(value["release_generation"], 1, 1<<62)
	materialized, materializedOK := exactInt(value["materialized_at_epoch"], 1, 1<<62)
	observed, observedOK := exactInt(value["observation_completed_at_epoch"], 1, 1<<62)
	validUntil, validOK := exactInt(value["valid_until_epoch"], 1, 1<<62)
	publicationCompleted, publicationOK := exactInt(value["image_publication_run_completed_at_epoch"], 1, 1<<62)
	authoritative, authoritativeOK := value["authoritative"].(bool)
	productionReady, productionOK := value["production_ready"].(bool)
	absenceProven, absenceOK := value["installed_state_absence_proven"].(bool)
	rootRequired, rootOK := value["root_helper_authorization_required"].(bool)
	runnerLaunchDigest, _ := exactString(value["runner_launch_ticket_sha256"])
	runnerSourceIdentity, _ := exactString(value["runner_source_checkout_identity_sha256"])
	runnerSourcePath, _ := exactString(value["runner_source_checkout_path"])
	runnerSourceTree, _ := exactString(value["runner_source_tree_sha256"])
	runnerPrerequisiteIntentDigest, _ := exactString(value["runner_prerequisite_intent_sha256"])
	runnerPrerequisiteApprovalDigest, _ := exactString(value["runner_prerequisite_approval_sha256"])
	runnerPrerequisiteApprovalPayloadDigest, _ := exactString(value["runner_prerequisite_approval_payload_sha256"])
	runnerPrerequisiteJobID, _ := exactString(value["runner_prerequisite_job_id"])
	if schema != materializationReceiptSchema || !versionOK || version != 2 || !generationOK || generation != verified.ReleaseGeneration ||
		!materializedOK || materialized != verified.TransactionStartedAt || !observedOK || observed < materialized || observed-materialized > 900 ||
		!validOK || validUntil != materialized+authority.BackupMaxAgeSeconds || !publicationOK || publicationCompleted > materialized+60 ||
		materialized-publicationCompleted > 21600 ||
		!authoritativeOK || authoritative || !productionOK || productionReady || !absenceOK || absenceProven ||
		!rootOK || !rootRequired || configDigest != verified.ConfigDigest || planDigest != verified.PlanDigest || deploymentID != verified.DeploymentID ||
		packageKeyID != verified.PackageAuthorityKeyID || receiptKeyID != verified.ReceiptAuthorityKeyID || runtimeSHA != verified.RuntimeSHA || workflowSHA != verified.WorkflowSHA ||
		runnerLaunchDigest != runner.launchTicketDigest || runnerSourceIdentity != runner.sourceCheckoutIdentity || runnerSourcePath != runner.sourceCheckoutPath || runnerSourceTree != runner.sourceTreeDigest || observed != runner.boundAt ||
		runnerPrerequisiteIntentDigest != prerequisite.intentDigest || runnerPrerequisiteApprovalDigest != prerequisite.approvalDigest || runnerPrerequisiteApprovalPayloadDigest != prerequisite.approvalPayloadDigest || runnerPrerequisiteJobID != prerequisite.jobID {
		return 0, fmt.Errorf("package-materialization-receipt-binding-or-freshness-invalid")
	}
	for _, key := range []string{"final_artifact_id", "image_publication_run_attempt", "image_publication_run_id", "preflight_artifact_id", "render_attestation_id", "web_attestation_id"} {
		text, ok := exactString(value[key])
		if !ok || !numericIDPattern.MatchString(text) {
			return 0, fmt.Errorf("package-materialization-receipt-evidence-id-invalid")
		}
	}
	for _, key := range []string{"final_artifact_sha256", "preflight_artifact_sha256", "release_hygiene_sha256"} {
		text, ok := exactString(value[key])
		if !ok || !envelopeSHAPattern.MatchString(text) {
			return 0, fmt.Errorf("package-materialization-receipt-evidence-digest-invalid")
		}
	}
	return validUntil, nil
}

func verifyPayloadBindings(verified *VerifiedPackage, packageKey ed25519.PublicKey) error {
	get := func(path string) []byte { return verified.Files[path].Data }
	buildReceiptRaw := get("/etc/propertyquarry-release-single-host-v2/native-build-receipt.v2.json")
	if digest(buildReceiptRaw) != verified.BuildReceiptDigest {
		return fmt.Errorf("package-build-receipt-digest-invalid")
	}
	buildBinding, err := validateNativeBuildReceipt(
		buildReceiptRaw,
		get("/usr/libexec/propertyquarry-release-control/propertyquarry-release-single-host-v2"),
		verified.PackageAuthorityKeyID,
	)
	if err != nil {
		return err
	}
	verified.InstallerBinaryDigest = buildBinding.installerDigest
	verified.InstallerBinarySize = buildBinding.installerSize
	verified.SourceManifestDigest = buildBinding.sourceManifestDigest
	anchorKey, anchorDER, anchorKeyID, err := parsePublicPEM(get("/etc/propertyquarry-release-single-host-v2/package-authority-v2.pem"))
	if err != nil || anchorKeyID != verified.PackageAuthorityKeyID || !bytes.Equal(anchorKey, packageKey) {
		zero(anchorDER)
		zero(anchorKey)
		return fmt.Errorf("package-bundled-anchor-invalid")
	}
	zero(anchorDER)
	zero(anchorKey)
	configRaw := get("/etc/propertyquarry-release-single-host-v2/authority.v2.json")
	configSignature := get("/etc/propertyquarry-release-single-host-v2/authority.v2.sig")
	if len(configSignature) != ed25519.SignatureSize || digest(configRaw) != verified.ConfigDigest || !ed25519.Verify(packageKey, framed(configSignatureDomain, configRaw), configSignature) {
		return fmt.Errorf("package-config-signature-invalid")
	}
	planRaw := get("/etc/propertyquarry-release-single-host-v2/transaction-plan.v2.json")
	if digest(planRaw) != verified.PlanDigest {
		return fmt.Errorf("package-plan-digest-invalid")
	}
	config, plan, err := authority.ValidateDetachedConfigPlan(configRaw, planRaw, verified.PackageAuthorityKeyID)
	if err != nil {
		return fmt.Errorf("package-detached-authority-invalid: %w", err)
	}
	defer config.Release()
	defer plan.Release()
	receiptAnchor, receiptDER, receiptKeyID, err := parsePublicPEM(get("/etc/propertyquarry-release-single-host-v2/receipt-authority-v2.pem"))
	defer zero(receiptDER)
	defer zero(receiptAnchor)
	if err != nil || receiptKeyID != verified.ReceiptAuthorityKeyID {
		return fmt.Errorf("package-receipt-anchor-invalid")
	}
	prerequisiteBinding, err := validateRunnerPrerequisiteMaterial(
		get("/var/lib/propertyquarry-release-single-host-v2/runner-prerequisite-intent.v2.json"),
		get("/var/lib/propertyquarry-release-single-host-v2/runner-prerequisite-approval.v2.json"),
		get("/var/lib/propertyquarry-release-single-host-v2/runner-reservation.v2.json"),
		config,
		verified.ReceiptAuthorityKeyID,
		receiptAnchor,
	)
	if err != nil {
		return err
	}
	runnerBinding, err := validateRunnerMaterial(
		get("/var/lib/propertyquarry-release-single-host-v2/runner-reservation.v2.json"),
		get("/var/lib/propertyquarry-release-single-host-v2/runner-launch-ticket.v2.json"),
		config,
		verified.PlanDigest,
		verified.ReceiptAuthorityKeyID,
		receiptAnchor,
	)
	if err != nil {
		return err
	}
	materializationValidUntil, err := validateMaterializationReceipt(
		get("/etc/propertyquarry-release-single-host-v2/materialization-receipt.v2.json"),
		get("/etc/propertyquarry-release-single-host-v2/materialization-receipt.v2.sig"),
		verified,
		packageKey,
		runnerBinding,
		prerequisiteBinding,
	)
	if err != nil {
		return err
	}
	verified.MaterializationValidUntil = materializationValidUntil
	if config.Digest != verified.ConfigDigest || config.PlanDigest != verified.PlanDigest || config.ReceiptAuthorityKeyID != verified.ReceiptAuthorityKeyID ||
		config.RuntimeSHA != verified.RuntimeSHA || config.WorkflowSHA != verified.WorkflowSHA || config.DeploymentID != verified.DeploymentID || config.TransactionStartedAtEpoch != verified.TransactionStartedAt ||
		config.BackupMaxAgeSeconds != verified.BackupMaxAgeSeconds || config.EnvelopeSHA != verified.EnvelopeSHA || config.ReleaseGeneration != verified.ReleaseGeneration ||
		config.HostMachineIDDigest != verified.HostMachineIDDigest || config.CloudflaredImage != verified.CloudflaredImage || config.DatabaseImage != verified.DatabaseImage ||
		config.APIHostIP != verified.APIHostIP || config.APIHostPort != verified.APIHostPort || config.APIContainerPort != verified.APIContainerPort ||
		config.PrePurgeRootEnvDigest != verified.PrePurgeRootEnvDigest || config.PostPurgeRootEnvDigest != verified.PostPurgeRootEnvDigest ||
		config.PrePurgeRuntimeInputsDigest != verified.PrePurgeRuntimeInputsDigest || config.RuntimeInputsDigest != verified.RuntimeInputsDigest ||
		config.RuntimeRetirementDigest != verified.RuntimeRetirementDigest || config.RuntimeDeployDigest != verified.RuntimeDeployDigest ||
		config.DatabaseSubstrateDigest != verified.DatabaseSubstrateDigest || config.WebImage != verified.WebImage || config.RenderImage != verified.RenderImage ||
		config.RunnerPrerequisiteIntentDigest != verified.RunnerPrerequisiteIntentDigest || config.RunnerPrerequisiteApprovalDigest != verified.RunnerPrerequisiteApprovalDigest ||
		config.RunnerPrerequisiteApprovalPayloadDigest != verified.RunnerPrerequisiteApprovalPayloadDigest || config.RunnerPrerequisiteJobID != verified.RunnerPrerequisiteJobID ||
		config.SceneVideoEnvDigest != verified.SceneVideoEnvDigest || config.SceneVideoEnvUID != verified.SceneVideoEnvUID || config.SceneVideoEnvGID != verified.SceneVideoEnvGID {
		return fmt.Errorf("package-manifest-profile-binding-invalid")
	}
	plannedBackupDigest, ok := plan.Executables[predeployBackupHelperPath]
	packagedBackup := verified.Files[predeployBackupHelperPath]
	if !ok || packagedBackup == nil || plannedBackupDigest != packagedBackup.Digest {
		return fmt.Errorf("package-predeploy-backup-helper-binding-invalid")
	}
	plannedDatabaseDigest, ok := plan.Executables[databaseControlHelperPath]
	packagedDatabaseControl := verified.Files[databaseControlHelperPath]
	packagedRuntimeDatabase := verified.Files[runtimeDatabaseHelperPath]
	if !ok || packagedDatabaseControl == nil || plannedDatabaseDigest != packagedDatabaseControl.Digest || packagedRuntimeDatabase == nil {
		return fmt.Errorf("package-database-control-helper-binding-invalid")
	}
	for path, label := range map[string]string{
		runtimeIsolationHelperPath: "runtime-isolation",
		runtimeDeployHelperPath:    "runtime-deploy",
	} {
		plannedDigest, planned := plan.Executables[path]
		packaged := verified.Files[path]
		if !planned || packaged == nil || plannedDigest != packaged.Digest {
			return fmt.Errorf("package-%s-helper-binding-invalid", label)
		}
	}
	receiptKey, err := parsePrivatePEM(get("/etc/propertyquarry-release-single-host-v2/receipt-authority-v2.key"))
	if err != nil {
		return fmt.Errorf("package-receipt-key-invalid")
	}
	defer zero(receiptKey)
	if err != nil || receiptKeyID != verified.ReceiptAuthorityKeyID || !bytes.Equal(receiptAnchor, receiptKey.Public().(ed25519.PublicKey)) {
		return fmt.Errorf("package-receipt-binding-invalid")
	}
	return nil
}

type nativeBuildBinding struct {
	installerDigest      string
	installerSize        int64
	sourceManifestDigest string
}

func validateNativeBuildReceipt(raw, controller []byte, packageKeyID string) (nativeBuildBinding, error) {
	var empty nativeBuildBinding
	if len(raw) < 2 || raw[len(raw)-1] != '\n' || raw[len(raw)-2] == '\n' {
		return empty, fmt.Errorf("package-build-receipt-newline-invalid")
	}
	receipt, err := strictJSON(raw[:len(raw)-1], maximumManifestBytes)
	if err != nil {
		return empty, fmt.Errorf("package-build-receipt-invalid")
	}
	if !hasKeys(receipt,
		"authoritative", "binary_mode", "binary_sha256", "binary_size", "build_flags",
		"go_tests_passed_in_both_builds", "host_network_namespace_isolated", "independent_toolchain_extractions",
		"installer_binary_mode", "installer_binary_sha256", "installer_binary_size", "installer_package_authority_bound",
		"installer_package_authority_key_id", "module_network_resolution_disabled", "package_signature_verified",
		"performs_release_effects", "production_ready", "receipt_published_last", "reproducible_double_build",
		"root_install_performed", "schema", "scratch_execution_contract", "source_manifest_digest",
		"static_elf_verified_in_both_builds", "toolchain", "toolchain_archive_bytes", "toolchain_archive_sha256", "version",
	) {
		return empty, fmt.Errorf("package-build-receipt-shape-invalid")
	}
	schema, _ := exactString(receipt["schema"])
	version, versionOK := exactInt(receipt["version"], 2, 2)
	binaryMode, _ := exactString(receipt["binary_mode"])
	binaryDigest, _ := exactString(receipt["binary_sha256"])
	binarySize, binarySizeOK := exactInt(receipt["binary_size"], 1, maximumMemberBytes)
	installerMode, _ := exactString(receipt["installer_binary_mode"])
	installerDigest, _ := exactString(receipt["installer_binary_sha256"])
	installerSize, installerSizeOK := exactInt(receipt["installer_binary_size"], 1, maximumMemberBytes)
	installerKeyID, _ := exactString(receipt["installer_package_authority_key_id"])
	sourceDigest, _ := exactString(receipt["source_manifest_digest"])
	toolchain, _ := exactString(receipt["toolchain"])
	toolchainDigest, _ := exactString(receipt["toolchain_archive_sha256"])
	toolchainBytes, toolchainBytesOK := exactInt(receipt["toolchain_archive_bytes"], 66879095, 66879095)
	scratchContract, _ := exactString(receipt["scratch_execution_contract"])
	if schema != buildReceiptSchema || !versionOK || version != 2 || binaryMode != "0755" || binaryDigest != digest(controller) || !binarySizeOK || binarySize != int64(len(controller)) || installerMode != "0555" || !digestPattern.MatchString(installerDigest) || !installerSizeOK || installerKeyID != packageKeyID || !digestPattern.MatchString(sourceDigest) || toolchain != "go1.26.5 linux/amd64" || !toolchainBytesOK || toolchainBytes != 66879095 || toolchainDigest != "5c2c3b16caefa1d968a94c1daca04a7ca301a496d9b086e17ad77bb81393f053" || scratchContract != "linux-amd64-static-et-exec-v1" {
		return empty, fmt.Errorf("package-build-receipt-binding-invalid")
	}
	if InstallerSourceManifestDigest != "sha256:unbound" && sourceDigest != InstallerSourceManifestDigest {
		return empty, fmt.Errorf("package-build-receipt-source-invalid")
	}
	trueFields := []string{
		"go_tests_passed_in_both_builds", "independent_toolchain_extractions", "installer_package_authority_bound",
		"module_network_resolution_disabled", "receipt_published_last", "reproducible_double_build", "static_elf_verified_in_both_builds",
	}
	falseFields := []string{
		"authoritative", "host_network_namespace_isolated", "package_signature_verified", "performs_release_effects",
		"production_ready", "root_install_performed",
	}
	for _, field := range trueFields {
		value, ok := receipt[field].(bool)
		if !ok || !value {
			return empty, fmt.Errorf("package-build-receipt-claim-invalid")
		}
	}
	for _, field := range falseFields {
		value, ok := receipt[field].(bool)
		if !ok || value {
			return empty, fmt.Errorf("package-build-receipt-claim-invalid")
		}
	}
	flags, ok := receipt["build_flags"].([]any)
	expectedFlags := []string{"-mod=readonly", "-trimpath", "-buildvcs=false", "-buildmode=exe"}
	if !ok || len(flags) != len(expectedFlags) {
		return empty, fmt.Errorf("package-build-receipt-flags-invalid")
	}
	for index, expected := range expectedFlags {
		actual, ok := flags[index].(string)
		if !ok || actual != expected {
			return empty, fmt.Errorf("package-build-receipt-flags-invalid")
		}
	}
	if err := validateStaticELF(controller); err != nil {
		return empty, err
	}
	return nativeBuildBinding{installerDigest: installerDigest, installerSize: installerSize, sourceManifestDigest: sourceDigest}, nil
}

func validateStaticELF(raw []byte) error {
	ident := []byte{0x7f, 'E', 'L', 'F', 2, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0}
	if len(raw) < 64 || len(raw) > maximumMemberBytes || !bytes.Equal(raw[:16], ident) {
		return fmt.Errorf("package-controller-elf-ident-invalid")
	}
	if binary.LittleEndian.Uint16(raw[16:18]) != 2 || binary.LittleEndian.Uint16(raw[18:20]) != 62 || binary.LittleEndian.Uint32(raw[20:24]) != 1 || binary.LittleEndian.Uint64(raw[24:32]) == 0 || binary.LittleEndian.Uint16(raw[52:54]) != 64 || binary.LittleEndian.Uint16(raw[54:56]) != 56 {
		return fmt.Errorf("package-controller-elf-header-invalid")
	}
	programOffset := binary.LittleEndian.Uint64(raw[32:40])
	programCount := uint64(binary.LittleEndian.Uint16(raw[56:58]))
	if programCount < 1 || programCount > 256 || programOffset < 64 || programOffset > uint64(len(raw)) || programCount > (uint64(len(raw))-programOffset)/56 {
		return fmt.Errorf("package-controller-elf-program-table-invalid")
	}
	foundLoad := false
	foundStack := false
	for index := uint64(0); index < programCount; index++ {
		offset := programOffset + index*56
		program := raw[offset : offset+56]
		kind := binary.LittleEndian.Uint32(program[0:4])
		flags := binary.LittleEndian.Uint32(program[4:8])
		if kind == 2 || kind == 3 {
			return fmt.Errorf("package-controller-not-static")
		}
		if kind == 1 {
			foundLoad = true
			if flags&3 == 3 {
				return fmt.Errorf("package-controller-writable-executable-segment")
			}
			fileOffset := binary.LittleEndian.Uint64(program[8:16])
			fileSize := binary.LittleEndian.Uint64(program[32:40])
			memorySize := binary.LittleEndian.Uint64(program[40:48])
			if memorySize < fileSize || fileOffset > uint64(len(raw)) || fileSize > uint64(len(raw))-fileOffset {
				return fmt.Errorf("package-controller-elf-segment-invalid")
			}
		}
		if kind == 0x6474e551 {
			foundStack = true
			if flags&1 != 0 {
				return fmt.Errorf("package-controller-executable-stack")
			}
		}
	}
	if !foundLoad || !foundStack {
		return fmt.Errorf("package-controller-elf-segments-invalid")
	}
	return nil
}

func parsePublicPEM(raw []byte) (ed25519.PublicKey, []byte, string, error) {
	block, rest := pem.Decode(raw)
	if block == nil || block.Type != "PUBLIC KEY" || len(block.Headers) != 0 || len(rest) != 0 || !bytes.Equal(pem.EncodeToMemory(&pem.Block{Type: "PUBLIC KEY", Bytes: block.Bytes}), raw) {
		return nil, nil, "", fmt.Errorf("public-key-invalid")
	}
	parsed, err := x509.ParsePKIXPublicKey(block.Bytes)
	if err != nil {
		return nil, nil, "", err
	}
	key, ok := parsed.(ed25519.PublicKey)
	if !ok || len(key) != ed25519.PublicKeySize {
		return nil, nil, "", fmt.Errorf("public-key-type-invalid")
	}
	der := append([]byte(nil), block.Bytes...)
	sum := sha256.Sum256(der)
	return append(ed25519.PublicKey(nil), key...), der, "sha256:" + fmt.Sprintf("%x", sum[:]), nil
}

func parsePrivatePEM(raw []byte) (ed25519.PrivateKey, error) {
	block, rest := pem.Decode(raw)
	if block == nil || block.Type != "PRIVATE KEY" || len(block.Headers) != 0 || len(rest) != 0 || !bytes.Equal(pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: block.Bytes}), raw) {
		return nil, fmt.Errorf("private-key-invalid")
	}
	parsed, err := x509.ParsePKCS8PrivateKey(block.Bytes)
	if err != nil {
		return nil, err
	}
	key, ok := parsed.(ed25519.PrivateKey)
	if !ok || len(key) != ed25519.PrivateKeySize {
		return nil, fmt.Errorf("private-key-type-invalid")
	}
	return append(ed25519.PrivateKey(nil), key...), nil
}

func releaseMembers(members []archiveMember) {
	for index := range members {
		zero(members[index].data)
	}
}

func SortedInstallPaths(files map[string]*FileRecord) []string {
	paths := make([]string, 0, len(files))
	for path := range files {
		paths = append(paths, path)
	}
	sort.Strings(paths)
	return paths
}
