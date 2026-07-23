package authority

import (
	"bytes"
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"syscall"
)

const (
	predeployBackupReceiptSchema          = "propertyquarry.predeploy-backup-receipt.v2"
	predeployBackupReceiptSignatureDomain = "propertyquarry.release-control.single-host-predeploy-backup-receipt-signature.v2\x00"
	predeployBackupRemoteParent           = "/mnt/pcloud/propertyquarry/releases/backups/v2"
	predeployBackupManifestName           = "manifest.v2.json"
	predeployBackupKeyUID                 = 1000
	predeployBackupKeyGID                 = 1000
	maximumBackupReceiptBytes             = 2 * 1024 * 1024
)

type backupReceiptProof struct {
	receiptDigest      string
	remotePath         string
	manifestDigest     string
	encryptionKeyID    string
	databaseImageID    string
	databaseRepoDigest string
	databaseSubstrate  *databaseSubstrate
	startedAt          int64
	finishedAt         int64
}

type backupArtifactContract struct {
	name         string
	kind         string
	coverage     []string
	method       string
	verification string
}

func verifyPredeployBackupReceipt(root string, config *Config, receiptPublic ed25519.PublicKey) (*backupReceiptProof, error) {
	if config == nil || len(receiptPublic) != ed25519.PublicKeySize {
		return nil, fmt.Errorf("backup-receipt-input-invalid")
	}
	receiptPath := filepath.Join(PredeployBackupReceiptDirectory, config.RuntimeSHA, config.DeploymentID, "create.json")
	ownerUID, ownerGID := secureOwner(root)
	raw, err := secureRead(root, receiptPath, 0o600, ownerUID, ownerGID, maximumBackupReceiptBytes)
	if err != nil || len(raw) < 2 || raw[len(raw)-1] != '\n' || raw[len(raw)-2] == '\n' {
		zero(raw)
		return nil, fmt.Errorf("backup-receipt-unavailable")
	}
	defer zero(raw)
	wrapper, err := strictJSON(raw[:len(raw)-1], maximumBackupReceiptBytes)
	if err != nil || !hasKeys(wrapper, "payload", "signature", "signature_key_id") {
		return nil, fmt.Errorf("backup-receipt-wrapper-invalid")
	}
	payload, payloadOK := wrapper["payload"].(map[string]any)
	signatureText, signatureOK := exactString(wrapper["signature"])
	signatureKeyID, keyIDOK := exactString(wrapper["signature_key_id"])
	expectedKeyID, keyErr := publicKeyID(receiptPublic)
	signature, signatureErr := signatureBytes(signatureText)
	payloadRaw, canonicalErr := canonicalJSON(payload)
	if !payloadOK || !signatureOK || !keyIDOK || keyErr != nil || signatureKeyID != expectedKeyID || expectedKeyID != config.ReceiptAuthorityKeyID || signatureErr != nil || canonicalErr != nil || len(signature) != ed25519.SignatureSize || !ed25519.Verify(receiptPublic, framed(predeployBackupReceiptSignatureDomain, payloadRaw), signature) {
		zero(signature)
		zero(payloadRaw)
		return nil, fmt.Errorf("backup-receipt-signature-invalid")
	}
	zero(signature)
	defer zero(payloadRaw)
	proof, err := validateBackupReceiptPayload(root, config, payload)
	if err != nil {
		return nil, err
	}
	proof.receiptDigest = digest(raw)
	return proof, nil
}

func validateBackupReceiptPayload(root string, config *Config, payload map[string]any) (*backupReceiptProof, error) {
	ownerUID, ownerGID := secureOwner(root)
	if !hasKeys(payload,
		"artifacts", "atomic_finalize", "authority_digest", "authority_signature_digest", "config_digest", "coverage",
		"backup_max_age_seconds", "database_image", "database_image_id", "database_repo_digest", "database_substrate_after", "database_substrate_before", "deployment_id", "disposition", "encryption_key_created", "encryption_key_id", "envelope_sha", "finished_at_epoch", "fsync_artifacts",
		"fsync_directories", "host_machine_id_digest", "package_authority_key_id", "package_manifest_digest",
		"package_manifest_signature_digest", "plaintext_retained", "plan_digest", "pre_purge_runtime_inputs", "production_ready", "receipt_authority_key_id",
		"remote", "render_image", "runtime_sha", "schema", "started_at_epoch", "transaction_started_at_epoch", "web_image",
	) {
		return nil, fmt.Errorf("backup-receipt-payload-shape-invalid")
	}
	bindings := map[string]string{
		"schema": predeployBackupReceiptSchema, "runtime_sha": config.RuntimeSHA, "envelope_sha": config.EnvelopeSHA,
		"web_image": config.WebImage, "render_image": config.RenderImage, "authority_digest": config.Digest,
		"config_digest": config.Digest, "plan_digest": config.PlanDigest, "package_authority_key_id": config.PackageAuthorityKeyID,
		"receipt_authority_key_id": config.ReceiptAuthorityKeyID, "host_machine_id_digest": config.HostMachineIDDigest,
		"database_image": config.DatabaseImage,
		"deployment_id":  config.DeploymentID,
		"disposition":    "verified-and-published",
	}
	for field, expected := range bindings {
		actual, ok := exactString(payload[field])
		if !ok || actual != expected {
			return nil, fmt.Errorf("backup-receipt-binding-invalid")
		}
	}
	for field, expected := range map[string]bool{"production_ready": false, "plaintext_retained": false, "atomic_finalize": true, "fsync_artifacts": true, "fsync_directories": true} {
		actual, ok := payload[field].(bool)
		if !ok || actual != expected {
			return nil, fmt.Errorf("backup-receipt-claim-invalid")
		}
	}
	if _, ok := payload["encryption_key_created"].(bool); !ok {
		return nil, fmt.Errorf("backup-receipt-key-created-invalid")
	}
	databaseImageID, imageIDOK := exactString(payload["database_image_id"])
	databaseRepoDigest, repoDigestOK := exactString(payload["database_repo_digest"])
	if !imageIDOK || !digestPattern.MatchString(databaseImageID) || !repoDigestOK || databaseRepoDigest != canonicalRepoDigest(config.DatabaseImage) || config.DatabaseSubstrate == nil || databaseImageID != config.DatabaseSubstrate.imageID || databaseRepoDigest != config.DatabaseSubstrate.repoDigest {
		return nil, fmt.Errorf("backup-receipt-database-image-identity-invalid")
	}
	if !databaseSubstrateValueEqual(payload["database_substrate_before"], config.DatabaseSubstrate) || !databaseSubstrateValueEqual(payload["database_substrate_after"], config.DatabaseSubstrate) || !canonicalValuesEqual(payload["database_substrate_before"], payload["database_substrate_after"]) {
		return nil, fmt.Errorf("backup-receipt-database-substrate-invalid")
	}
	if !runtimeInputObservationsEqual(payload["pre_purge_runtime_inputs"], config.PrePurgeRuntimeInputs) {
		return nil, fmt.Errorf("backup-receipt-runtime-inputs-invalid")
	}
	started, startedOK := exactInt(payload["started_at_epoch"], 1, 1<<62)
	finished, finishedOK := exactInt(payload["finished_at_epoch"], 1, 1<<62)
	transactionStarted, transactionStartedOK := exactInt(payload["transaction_started_at_epoch"], config.TransactionStartedAtEpoch, config.TransactionStartedAtEpoch)
	maximumAge, maximumAgeOK := exactInt(payload["backup_max_age_seconds"], BackupMaxAgeSeconds, BackupMaxAgeSeconds)
	if !startedOK || !finishedOK || !transactionStartedOK || transactionStarted != config.TransactionStartedAtEpoch || !maximumAgeOK || maximumAge != config.BackupMaxAgeSeconds || started < transactionStarted || finished < started || finished-started > 9600 {
		return nil, fmt.Errorf("backup-receipt-time-invalid")
	}
	authoritySignature, err := secureRead(root, ConfigSignaturePath, 0o444, ownerUID, ownerGID, maximumConfigBytes)
	if err != nil || !payloadDigestEquals(payload, "authority_signature_digest", authoritySignature) {
		zero(authoritySignature)
		return nil, fmt.Errorf("backup-receipt-authority-signature-invalid")
	}
	zero(authoritySignature)
	manifestRaw, err := secureRead(root, "/etc/propertyquarry-release-single-host-v2/package-manifest.v2.json", 0o444, ownerUID, ownerGID, maximumConfigBytes)
	if err != nil || !payloadDigestEquals(payload, "package_manifest_digest", manifestRaw) {
		zero(manifestRaw)
		return nil, fmt.Errorf("backup-receipt-package-manifest-invalid")
	}
	manifest, manifestErr := strictJSON(manifestRaw, maximumConfigBytes)
	zero(manifestRaw)
	manifestPackageKey, _ := exactString(manifest["package_authority_key_id"])
	manifestConfigDigest, _ := exactString(manifest["config_digest"])
	manifestPlanDigest, _ := exactString(manifest["plan_digest"])
	manifestWorkflowSHA, _ := exactString(manifest["workflow_sha"])
	if manifestErr != nil || manifest["schema"] != "propertyquarry.release-control.single-host-package.v2" || manifestPackageKey != config.PackageAuthorityKeyID || manifestConfigDigest != config.Digest || manifestPlanDigest != config.PlanDigest || manifestWorkflowSHA != config.WorkflowSHA {
		return nil, fmt.Errorf("backup-receipt-package-manifest-binding-invalid")
	}
	manifestSignature, err := secureRead(root, "/etc/propertyquarry-release-single-host-v2/package-manifest.v2.sig", 0o444, ownerUID, ownerGID, ed25519.SignatureSize)
	if err != nil || !payloadDigestEquals(payload, "package_manifest_signature_digest", manifestSignature) {
		zero(manifestSignature)
		return nil, fmt.Errorf("backup-receipt-package-manifest-signature-invalid")
	}
	zero(manifestSignature)
	encryptionKeyID, ok := exactString(payload["encryption_key_id"])
	if !ok || !digestPattern.MatchString(encryptionKeyID) || validateBackupEncryptionKey(root, encryptionKeyID) != nil {
		return nil, fmt.Errorf("backup-receipt-encryption-key-invalid")
	}
	coverage, ok := payload["coverage"].(map[string]any)
	if !ok || validateBackupCoverage(coverage) != nil {
		return nil, fmt.Errorf("backup-receipt-coverage-invalid")
	}
	remote, ok := payload["remote"].(map[string]any)
	expectedRemotePath := filepath.Join(predeployBackupRemoteParent, config.RuntimeSHA, config.DeploymentID)
	if !ok || !hasKeys(remote, "manifest_sha256", "path", "provider", "version") || remote["provider"] != "pcloud-rclone" || remote["version"] != "v2" || remote["path"] != expectedRemotePath {
		return nil, fmt.Errorf("backup-receipt-remote-invalid")
	}
	remoteManifestDigest, ok := exactString(remote["manifest_sha256"])
	if !ok || !digestPattern.MatchString(remoteManifestDigest) || validateRemoteBackupDirectory(root, expectedRemotePath) != nil {
		return nil, fmt.Errorf("backup-receipt-remote-invalid")
	}
	manifestBytes, err := readRemoteBackupFile(root, filepath.Join(expectedRemotePath, predeployBackupManifestName), -1, 1*1024*1024)
	if err != nil || digest(manifestBytes) != remoteManifestDigest || len(manifestBytes) < 2 || manifestBytes[len(manifestBytes)-1] != '\n' {
		zero(manifestBytes)
		return nil, fmt.Errorf("backup-receipt-remote-manifest-invalid")
	}
	remoteManifest, err := strictJSON(manifestBytes[:len(manifestBytes)-1], len(manifestBytes))
	if err != nil || validateRemoteBackupManifest(config, payload, remoteManifest) != nil {
		zero(manifestBytes)
		return nil, fmt.Errorf("backup-receipt-remote-manifest-invalid")
	}
	zero(manifestBytes)
	artifacts, ok := payload["artifacts"].([]any)
	if !ok || validateBackupArtifacts(root, expectedRemotePath, artifacts) != nil {
		return nil, fmt.Errorf("backup-receipt-artifacts-invalid")
	}
	return &backupReceiptProof{remotePath: expectedRemotePath, manifestDigest: remoteManifestDigest, encryptionKeyID: encryptionKeyID, databaseImageID: databaseImageID, databaseRepoDigest: databaseRepoDigest, databaseSubstrate: config.DatabaseSubstrate, startedAt: started, finishedAt: finished}, nil
}

func validateRemoteBackupManifest(config *Config, receiptPayload, manifest map[string]any) error {
	if config == nil || !hasKeys(manifest, "artifacts", "bindings", "encryption_key_id", "plaintext_retained", "schema", "verification_complete") || manifest["schema"] != "propertyquarry.predeploy-backup-remote-manifest.v2" || manifest["plaintext_retained"] != false || manifest["verification_complete"] != true || manifest["encryption_key_id"] != receiptPayload["encryption_key_id"] {
		return fmt.Errorf("remote-manifest-shape-invalid")
	}
	bindings, ok := manifest["bindings"].(map[string]any)
	if !ok || !hasKeys(bindings,
		"authority_digest", "authority_signature_digest", "backup_max_age_seconds", "config_digest", "database_image", "database_image_id", "database_repo_digest", "database_substrate_after", "database_substrate_before", "deployment_id", "envelope_sha", "package_authority_key_id",
		"package_manifest_digest", "package_manifest_signature_digest", "plan_digest", "pre_purge_runtime_inputs", "render_image", "runtime_sha", "transaction_started_at_epoch", "web_image",
	) {
		return fmt.Errorf("remote-manifest-bindings-invalid")
	}
	expected := map[string]string{
		"authority_digest":                  config.Digest,
		"authority_signature_digest":        stringValue(receiptPayload["authority_signature_digest"]),
		"config_digest":                     config.Digest,
		"database_image":                    config.DatabaseImage,
		"database_image_id":                 stringValue(receiptPayload["database_image_id"]),
		"database_repo_digest":              canonicalRepoDigest(config.DatabaseImage),
		"deployment_id":                     config.DeploymentID,
		"envelope_sha":                      config.EnvelopeSHA,
		"package_authority_key_id":          config.PackageAuthorityKeyID,
		"package_manifest_digest":           stringValue(receiptPayload["package_manifest_digest"]),
		"package_manifest_signature_digest": stringValue(receiptPayload["package_manifest_signature_digest"]),
		"plan_digest":                       config.PlanDigest,
		"render_image":                      config.RenderImage,
		"runtime_sha":                       config.RuntimeSHA,
		"web_image":                         config.WebImage,
	}
	for name, expectedValue := range expected {
		actual, ok := exactString(bindings[name])
		if !ok || expectedValue == "" || actual != expectedValue {
			return fmt.Errorf("remote-manifest-binding-invalid")
		}
	}
	if bindings["backup_max_age_seconds"] != receiptPayload["backup_max_age_seconds"] || bindings["transaction_started_at_epoch"] != receiptPayload["transaction_started_at_epoch"] || !canonicalValuesEqual(bindings["pre_purge_runtime_inputs"], receiptPayload["pre_purge_runtime_inputs"]) || !canonicalValuesEqual(bindings["database_substrate_before"], receiptPayload["database_substrate_before"]) || !canonicalValuesEqual(bindings["database_substrate_after"], receiptPayload["database_substrate_after"]) {
		return fmt.Errorf("remote-manifest-binding-invalid")
	}
	manifestArtifacts, err := canonicalJSON(manifest["artifacts"])
	if err != nil {
		return fmt.Errorf("remote-manifest-artifacts-invalid")
	}
	defer zero(manifestArtifacts)
	receiptArtifacts, err := canonicalJSON(receiptPayload["artifacts"])
	if err != nil {
		return fmt.Errorf("remote-manifest-artifacts-invalid")
	}
	defer zero(receiptArtifacts)
	if !bytes.Equal(manifestArtifacts, receiptArtifacts) {
		return fmt.Errorf("remote-manifest-artifacts-mismatch")
	}
	return nil
}

func payloadDigestEquals(payload map[string]any, field string, raw []byte) bool {
	value, ok := exactString(payload[field])
	return ok && digestPattern.MatchString(value) && value == digest(raw)
}

func validateBackupEncryptionKey(root, expectedKeyID string) error {
	path := rooted(root, PredeployBackupEncryptionKeyPath)
	if err := validateExternalParentChain(root, path, predeployBackupKeyUID, predeployBackupKeyGID); err != nil {
		return err
	}
	raw, err := readSecureFile(path, 0o600, predeployBackupKeyUID, predeployBackupKeyGID, 65)
	if err != nil || len(raw) != 65 || raw[64] != '\n' {
		zero(raw)
		return fmt.Errorf("backup-key-unavailable")
	}
	defer zero(raw)
	decoded := make([]byte, 32)
	count, err := hex.Decode(decoded, raw[:64])
	if err != nil || count != 32 || "sha256:"+hex.EncodeToString(sha256Sum(decoded)) != expectedKeyID {
		zero(decoded)
		return fmt.Errorf("backup-key-binding-invalid")
	}
	zero(decoded)
	return nil
}

func sha256Sum(raw []byte) []byte {
	sum := sha256.Sum256(raw)
	return sum[:]
}

func validateBackupCoverage(value map[string]any) error {
	expected := map[string][]string{
		"config": {
			"/docker/property/.env",
			"/docker/property/config",
			"/docker/property/state/runtime/property_scene_video_shared.env",
			"/docker/property/state/runtime/propertyquarry_admission.env",
			"/docker/property/state/runtime/propertyquarry_database_roles.env",
			GoogleIdentityEnvPath,
			RegistrationEmailEnvPath,
		},
		"database": {"propertyquarry"}, "roles": {"postgres-cluster-roles"},
		"binds": {"/docker/property/state/incoming_property_tours"},
		"volumes": {
			"/var/lib/docker/volumes/property_propertyquarry_artifacts/_data",
			"/var/lib/docker/volumes/property_propertyquarry_governed_render_consents/_data",
			"/var/lib/docker/volumes/property_propertyquarry_provider_ledger/_data",
			"/var/lib/docker/volumes/property_propertyquarry_public_tours/_data",
		},
	}
	if len(value) != len(expected) {
		return fmt.Errorf("coverage-shape-invalid")
	}
	for key, list := range expected {
		if !exactStringList(value[key], list) {
			return fmt.Errorf("coverage-binding-invalid")
		}
	}
	return nil
}

func exactStringList(value any, expected []string) bool {
	items, ok := value.([]any)
	if !ok || len(items) != len(expected) {
		return false
	}
	for index, expectedValue := range expected {
		actual, ok := exactString(items[index])
		if !ok || actual != expectedValue {
			return false
		}
	}
	return true
}

func backupArtifactContracts() []backupArtifactContract {
	return []backupArtifactContract{
		{name: "database", kind: "postgres-custom", coverage: []string{"propertyquarry"}, method: "decrypt-pg_restore-list", verification: "database"},
		{name: "roles", kind: "postgres-roles-sql", coverage: []string{"postgres-cluster-roles"}, method: "decrypt-roles-sql-structure", verification: "roles"},
		{name: "volume-provider-ledger", kind: "tar-gzip", coverage: []string{"/var/lib/docker/volumes/property_propertyquarry_provider_ledger/_data"}, method: "decrypt-tar-gzip-list", verification: "tar"},
		{name: "volume-artifacts", kind: "tar-gzip", coverage: []string{"/var/lib/docker/volumes/property_propertyquarry_artifacts/_data"}, method: "decrypt-tar-gzip-list", verification: "tar"},
		{name: "volume-governed-render-consents", kind: "tar-gzip", coverage: []string{"/var/lib/docker/volumes/property_propertyquarry_governed_render_consents/_data"}, method: "decrypt-tar-gzip-list", verification: "tar"},
		{name: "volume-public-tours", kind: "tar-gzip", coverage: []string{"/var/lib/docker/volumes/property_propertyquarry_public_tours/_data"}, method: "decrypt-tar-gzip-list", verification: "tar"},
		{name: "bind-config", kind: "tar-gzip", coverage: []string{"/docker/property/config"}, method: "decrypt-tar-gzip-list", verification: "tar"},
		{name: "bind-incoming-property-tours", kind: "tar-gzip", coverage: []string{"/docker/property/state/incoming_property_tours"}, method: "decrypt-tar-gzip-list", verification: "tar"},
		{name: "runtime-identity-config", kind: "tar-gzip", coverage: []string{
			"/docker/property/.env",
			"/docker/property/state/runtime/property_scene_video_shared.env",
			"/docker/property/state/runtime/propertyquarry_database_roles.env",
			"/docker/property/state/runtime/propertyquarry_admission.env",
			GoogleIdentityEnvPath,
			RegistrationEmailEnvPath,
		}, method: "decrypt-tar-gzip-list", verification: "tar"},
	}
}

func validateBackupArtifacts(root, remotePath string, artifacts []any) error {
	contracts := backupArtifactContracts()
	if len(artifacts) != len(contracts) {
		return fmt.Errorf("artifact-count-invalid")
	}
	for index, contract := range contracts {
		artifact, ok := artifacts[index].(map[string]any)
		if !ok || !hasKeys(artifact, "chunk_count", "ciphertext_bytes", "ciphertext_sha256", "coverage", "filename", "kind", "name", "plaintext_bytes", "plaintext_sha256", "verification") || artifact["name"] != contract.name || artifact["kind"] != contract.kind || artifact["filename"] != contract.name+".pqenc" || !exactStringList(artifact["coverage"], contract.coverage) {
			return fmt.Errorf("artifact-binding-invalid")
		}
		chunkCount, chunkOK := exactInt(artifact["chunk_count"], 1, 1<<31-1)
		ciphertextBytes, ciphertextOK := exactInt(artifact["ciphertext_bytes"], 1, 1<<62)
		plaintextBytes, plaintextOK := exactInt(artifact["plaintext_bytes"], 1, 1<<62)
		ciphertextDigest, ciphertextDigestOK := exactString(artifact["ciphertext_sha256"])
		plaintextDigest, plaintextDigestOK := exactString(artifact["plaintext_sha256"])
		if !chunkOK || chunkCount < 1 || !ciphertextOK || !plaintextOK || plaintextBytes < 1 || !ciphertextDigestOK || !plaintextDigestOK || !digestPattern.MatchString(ciphertextDigest) || !digestPattern.MatchString(plaintextDigest) {
			return fmt.Errorf("artifact-content-binding-invalid")
		}
		verification, ok := artifact["verification"].(map[string]any)
		if !ok || validateArtifactVerification(contract, verification) != nil {
			return fmt.Errorf("artifact-verification-invalid")
		}
		actualDigest, err := hashRemoteBackupFile(root, filepath.Join(remotePath, contract.name+".pqenc"), ciphertextBytes)
		if err != nil || actualDigest != ciphertextDigest {
			return fmt.Errorf("artifact-remote-content-invalid")
		}
	}
	return nil
}

func validateArtifactVerification(contract backupArtifactContract, value map[string]any) error {
	switch contract.verification {
	case "database":
		if !hasKeys(value, "method", "table_data_entries", "toc_lines") || value["method"] != contract.method {
			return fmt.Errorf("verification-shape-invalid")
		}
		_, tableOK := exactInt(value["table_data_entries"], 1, 1<<62)
		_, tocOK := exactInt(value["toc_lines"], 1, 1<<62)
		if !tableOK || !tocOK {
			return fmt.Errorf("verification-count-invalid")
		}
	case "roles":
		if !hasKeys(value, "method") || value["method"] != contract.method {
			return fmt.Errorf("verification-shape-invalid")
		}
	case "tar":
		if !hasKeys(value, "entries", "method") || value["method"] != contract.method {
			return fmt.Errorf("verification-shape-invalid")
		}
		if _, ok := exactInt(value["entries"], 1, 1<<62); !ok {
			return fmt.Errorf("verification-count-invalid")
		}
	default:
		return fmt.Errorf("verification-contract-invalid")
	}
	return nil
}

func validateRemoteBackupDirectory(root, absolute string) error {
	path := rooted(root, absolute)
	if err := validateBackupRemoteParentChain(root, path); err != nil {
		return err
	}
	info, err := os.Lstat(path)
	metadata, ok := infoSys(info)
	if err != nil || !ok || !info.IsDir() || info.Mode()&os.ModeSymlink != 0 || info.Mode().Perm() != 0o775 || metadata.Uid != predeployBackupKeyUID || metadata.Gid != predeployBackupKeyGID || (root == "/" && metadata.Nlink != 1) || metadata.Nlink < 1 {
		return fmt.Errorf("backup-remote-directory-invalid")
	}
	directory, err := os.OpenFile(path, os.O_RDONLY|syscall.O_CLOEXEC|syscall.O_NOFOLLOW|syscall.O_DIRECTORY, 0)
	if err != nil {
		return fmt.Errorf("backup-remote-directory-invalid")
	}
	defer directory.Close()
	after, err := directory.Stat()
	if err != nil || !os.SameFile(info, after) {
		return fmt.Errorf("backup-remote-directory-changed")
	}
	entries, err := directory.ReadDir(-1)
	if err != nil {
		return fmt.Errorf("backup-remote-directory-unreadable")
	}
	expectedNames := map[string]bool{predeployBackupManifestName: false}
	for _, contract := range backupArtifactContracts() {
		expectedNames[contract.name+".pqenc"] = false
	}
	if len(entries) != len(expectedNames) {
		return fmt.Errorf("backup-remote-directory-shape-invalid")
	}
	for _, entry := range entries {
		seen, expected := expectedNames[entry.Name()]
		if !expected || seen {
			return fmt.Errorf("backup-remote-directory-shape-invalid")
		}
		expectedNames[entry.Name()] = true
	}
	return nil
}

func validateBackupRemoteParentChain(root, path string) error {
	boundary := filepath.Clean(root)
	clean := filepath.Clean(path)
	relative, err := filepath.Rel(boundary, clean)
	if err != nil || relative == ".." || strings.HasPrefix(relative, ".."+string(os.PathSeparator)) {
		return fmt.Errorf("backup-remote-path-invalid")
	}
	for current := filepath.Dir(clean); ; current = filepath.Dir(current) {
		info, err := os.Lstat(current)
		metadata, ok := infoSys(info)
		if err != nil || !ok || !info.IsDir() || info.Mode()&os.ModeSymlink != 0 || info.Mode().Perm()&0o002 != 0 || (metadata.Uid != 0 && metadata.Uid != predeployBackupKeyUID) || (info.Mode().Perm()&0o020 != 0 && (metadata.Uid != predeployBackupKeyUID || metadata.Gid != predeployBackupKeyGID)) {
			return fmt.Errorf("backup-remote-parent-invalid")
		}
		if current == boundary {
			return nil
		}
		next := filepath.Dir(current)
		if next == current {
			return fmt.Errorf("backup-remote-boundary-invalid")
		}
	}
}

func readRemoteBackupFile(root, absolute string, expectedSize, maximum int64) ([]byte, error) {
	path := rooted(root, absolute)
	file, info, err := openRemoteBackupFile(path, expectedSize, maximum)
	if err != nil {
		return nil, err
	}
	defer file.Close()
	raw := make([]byte, info.Size())
	if _, err := io.ReadFull(file, raw); err != nil {
		zero(raw)
		return nil, err
	}
	after, err := file.Stat()
	if err != nil || !os.SameFile(info, after) || after.Size() != info.Size() {
		zero(raw)
		return nil, fmt.Errorf("backup-remote-file-changed")
	}
	return raw, nil
}

func hashRemoteBackupFile(root, absolute string, expectedSize int64) (string, error) {
	path := rooted(root, absolute)
	file, info, err := openRemoteBackupFile(path, expectedSize, 1<<62)
	if err != nil {
		return "", err
	}
	defer file.Close()
	hasher := sha256.New()
	written, err := io.Copy(hasher, io.LimitReader(file, info.Size()+1))
	if err != nil || written != info.Size() {
		return "", fmt.Errorf("backup-remote-file-read-failed")
	}
	after, err := file.Stat()
	if err != nil || !os.SameFile(info, after) || after.Size() != info.Size() {
		return "", fmt.Errorf("backup-remote-file-changed")
	}
	return "sha256:" + hex.EncodeToString(hasher.Sum(nil)), nil
}

func openRemoteBackupFile(path string, expectedSize, maximum int64) (*os.File, os.FileInfo, error) {
	file, err := os.OpenFile(path, os.O_RDONLY|syscall.O_CLOEXEC|syscall.O_NOFOLLOW, 0)
	if err != nil {
		return nil, nil, err
	}
	info, err := file.Stat()
	metadata, ok := infoSys(info)
	if err != nil || !ok || !info.Mode().IsRegular() || info.Mode().Perm() != 0o664 || info.Size() < 1 || info.Size() > maximum || (expectedSize >= 0 && info.Size() != expectedSize) || metadata.Uid != predeployBackupKeyUID || metadata.Gid != predeployBackupKeyGID || metadata.Nlink != 1 {
		file.Close()
		return nil, nil, fmt.Errorf("backup-remote-file-metadata-invalid")
	}
	return file, info, nil
}

func backupProofFields(proof *backupReceiptProof) map[string]any {
	if proof == nil {
		return map[string]any{}
	}
	result := map[string]any{
		"predeploy_backup_receipt_digest":       proof.receiptDigest,
		"predeploy_backup_remote_path":          proof.remotePath,
		"predeploy_backup_manifest_digest":      proof.manifestDigest,
		"predeploy_backup_encryption_key_id":    proof.encryptionKeyID,
		"predeploy_backup_database_image_id":    proof.databaseImageID,
		"predeploy_backup_database_repo_digest": proof.databaseRepoDigest,
		"database_image_id":                     proof.databaseImageID,
		"database_repo_digest":                  proof.databaseRepoDigest,
		"predeploy_backup_verified":             true,
		"predeploy_backup_started_at_epoch":     json.Number(fmt.Sprintf("%d", proof.startedAt)),
		"predeploy_backup_finished_at_epoch":    json.Number(fmt.Sprintf("%d", proof.finishedAt)),
	}
	if proof.databaseSubstrate != nil {
		result["database_container_id"] = proof.databaseSubstrate.containerID
		result["database_substrate_digest"] = proof.databaseSubstrate.digest
		result["database_oid"] = json.Number(fmt.Sprintf("%d", proof.databaseSubstrate.databaseOID))
	}
	return result
}
