package authority

import (
	"crypto/ed25519"
	"encoding/json"
	"fmt"
	"path/filepath"
	"sort"
)

const (
	runtimeIsolationReceiptSchema          = "propertyquarry.runtime-isolation-receipt.v2"
	runtimeIsolationReceiptSignatureDomain = "propertyquarry.release-control.single-host-runtime-isolation-receipt-signature.v2\x00"
	packageManifestSignatureDomain         = "propertyquarry.release-control.single-host-package-manifest-signature.v2\x00"
	runtimeIsolationExecutablePath         = "/usr/libexec/propertyquarry-release-control/propertyquarry-runtime-isolation-v2"
	runtimeIsolationReceiptDirectory       = "/var/lib/propertyquarry-release-single-host-v2/isolation-receipts"
	runtimeIsolationRollbackDirectory      = "/var/lib/propertyquarry-release-single-host-v2/isolation-rollback"
	maximumRuntimeIsolationReceiptBytes    = 8 * 1024 * 1024
	PurgeRuntimeIsolationStepID            = "purge-propertyquarry-legacy-runtime-exposure"
	RestoreRuntimeIsolationStepID          = "restore-propertyquarry-legacy-runtime-exposure"
)

const (
	operationPurgeRuntimeIsolation   = "purge-legacy-runtime-exposure"
	operationRetireStaleRuntime      = "retire-stale-propertyquarry-runtime"
	operationRestoreRuntimeIsolation = "restore-legacy-runtime-exposure"
	operationVerifyRuntimeIsolation  = "verify-runtime-isolation"
)

type isolationDatabaseReceiptSummary struct {
	operation          string
	receiptDigest      string
	envFileDigest      string
	databaseImageID    string
	databaseRepoDigest string
	databaseOID        int64
	schemaStatus       string
	startedAt          int64
	finishedAt         int64
}

type runtimeIsolationProof struct {
	operation               string
	receiptDigest           string
	backupReceiptDigest     string
	purgeReceiptDigest      string
	prePurgeRootEnvDigest   string
	postPurgeRootEnvDigest  string
	rollbackArtifactDigest  string
	retirementReceiptDigest string
	deployReceiptDigest     string
	databaseImageID         string
	databaseRepoDigest      string
	databaseSubstrateDigest string
	databaseEnvDigest       string
	databaseReceipts        map[string]isolationDatabaseReceiptSummary
	startedAt               int64
	finishedAt              int64
}

var runtimeIsolationInputPaths = []string{
	BaseEnvironmentPath,
	SceneVideoEnvPath,
	DatabaseRuntimeEnvironmentPath,
	AdmissionEnvPath,
	GoogleIdentityEnvPath,
	RegistrationEmailEnvPath,
}

var (
	verifyIsolationBackupReceipt   = verifyPredeployBackupReceipt
	verifyIsolationDatabaseReceipt = verifyDatabaseControlReceipt
)

func verifyRuntimeIsolationReceipt(root string, config *Config, receiptPublic ed25519.PublicKey, operation string, stepStartedAt, stepFinishedAt int64) (*runtimeIsolationProof, error) {
	if config == nil || len(receiptPublic) != ed25519.PublicKeySize || !validRuntimeIsolationOperation(operation) {
		return nil, fmt.Errorf("isolation-receipt-input-invalid")
	}
	receiptPath := filepath.Join(runtimeIsolationReceiptDirectory, config.RuntimeSHA, config.DeploymentID, operation+".json")
	ownerUID, ownerGID := secureOwner(root)
	raw, err := secureRead(root, receiptPath, 0o600, ownerUID, ownerGID, maximumRuntimeIsolationReceiptBytes)
	if err != nil || len(raw) < 2 || raw[len(raw)-1] != '\n' || raw[len(raw)-2] == '\n' {
		zero(raw)
		return nil, fmt.Errorf("isolation-receipt-unavailable")
	}
	defer zero(raw)
	wrapper, err := strictJSON(raw[:len(raw)-1], maximumRuntimeIsolationReceiptBytes)
	if err != nil || !hasKeys(wrapper, "payload", "signature", "signature_key_id") {
		return nil, fmt.Errorf("isolation-receipt-wrapper-invalid")
	}
	payload, payloadOK := wrapper["payload"].(map[string]any)
	signatureText, signatureOK := exactString(wrapper["signature"])
	signatureKeyID, keyIDOK := exactString(wrapper["signature_key_id"])
	expectedKeyID, keyErr := publicKeyID(receiptPublic)
	signature, signatureErr := signatureBytes(signatureText)
	payloadRaw, canonicalErr := canonicalJSON(payload)
	if !payloadOK || !signatureOK || !keyIDOK || keyErr != nil || signatureKeyID != expectedKeyID || expectedKeyID != config.ReceiptAuthorityKeyID || signatureErr != nil || canonicalErr != nil || len(signature) != ed25519.SignatureSize || !ed25519.Verify(receiptPublic, framed(runtimeIsolationReceiptSignatureDomain, payloadRaw), signature) {
		zero(signature)
		zero(payloadRaw)
		return nil, fmt.Errorf("isolation-receipt-signature-invalid")
	}
	zero(signature)
	defer zero(payloadRaw)
	proof, err := validateRuntimeIsolationPayload(root, config, receiptPublic, operation, payload)
	if err != nil {
		return nil, err
	}
	if stepStartedAt < 1 || stepFinishedAt < stepStartedAt || proof.startedAt < stepStartedAt || proof.finishedAt > stepFinishedAt {
		return nil, fmt.Errorf("isolation-receipt-step-time-binding-invalid")
	}
	proof.receiptDigest = digest(raw)
	return proof, nil
}

func validRuntimeIsolationOperation(operation string) bool {
	return operation == operationPurgeRuntimeIsolation || operation == operationRetireStaleRuntime || operation == operationRestoreRuntimeIsolation || operation == operationVerifyRuntimeIsolation
}

func validateRuntimeIsolationPayload(root string, config *Config, receiptPublic ed25519.PublicKey, operation string, payload map[string]any) (*runtimeIsolationProof, error) {
	if !hasKeys(payload,
		"api_container_port", "api_host_ip", "api_host_port", "authority_digest", "authority_signature_digest", "backup_max_age_seconds",
		"cloudflared_image", "config_digest", "database_image", "database_substrate_digest", "deployment_id", "envelope_sha", "finished_at_epoch",
		"host_machine_id_digest", "operation", "package_authority_key_id", "package_manifest_digest",
		"package_manifest_signature_digest", "plan_digest", "pre_purge_runtime_inputs", "production_ready", "receipt_authority_key_id",
		"render_image", "result", "runtime_deploy_digest", "runtime_inputs", "runtime_retirement_digest", "runtime_sha", "scene_video_env_digest", "scene_video_env_gid",
		"scene_video_env_mode", "scene_video_env_path", "scene_video_env_uid", "schema", "secret_values_emitted",
		"started_at_epoch", "status", "transaction_started_at_epoch", "web_image",
	) {
		return nil, fmt.Errorf("isolation-receipt-payload-shape-invalid")
	}
	bindings := map[string]string{
		"api_host_ip": config.APIHostIP, "authority_digest": config.Digest, "cloudflared_image": config.CloudflaredImage,
		"config_digest": config.Digest, "database_image": config.DatabaseImage, "envelope_sha": config.EnvelopeSHA,
		"database_substrate_digest": config.DatabaseSubstrateDigest, "deployment_id": config.DeploymentID,
		"host_machine_id_digest": config.HostMachineIDDigest, "operation": operation,
		"package_authority_key_id": config.PackageAuthorityKeyID, "plan_digest": config.PlanDigest,
		"receipt_authority_key_id": config.ReceiptAuthorityKeyID, "render_image": config.RenderImage,
		"runtime_sha": config.RuntimeSHA, "scene_video_env_digest": config.SceneVideoEnvDigest,
		"runtime_deploy_digest": config.RuntimeDeployDigest, "runtime_retirement_digest": config.RuntimeRetirementDigest,
		"scene_video_env_path": SceneVideoEnvPath, "schema": runtimeIsolationReceiptSchema,
		"status": "verified", "web_image": config.WebImage,
	}
	for field, expected := range bindings {
		actual, ok := exactString(payload[field])
		if !ok || actual != expected {
			return nil, fmt.Errorf("isolation-receipt-binding-invalid")
		}
	}
	if port, ok := exactInt(payload["api_host_port"], APIHostPort, APIHostPort); !ok || port != config.APIHostPort {
		return nil, fmt.Errorf("isolation-receipt-api-boundary-invalid")
	}
	if port, ok := exactInt(payload["api_container_port"], APIContainerPort, APIContainerPort); !ok || port != config.APIContainerPort {
		return nil, fmt.Errorf("isolation-receipt-api-boundary-invalid")
	}
	if started, ok := exactInt(payload["transaction_started_at_epoch"], config.TransactionStartedAtEpoch, config.TransactionStartedAtEpoch); !ok || started != config.TransactionStartedAtEpoch {
		return nil, fmt.Errorf("isolation-receipt-transaction-start-invalid")
	}
	if maximumAge, ok := exactInt(payload["backup_max_age_seconds"], BackupMaxAgeSeconds, BackupMaxAgeSeconds); !ok || maximumAge != config.BackupMaxAgeSeconds {
		return nil, fmt.Errorf("isolation-receipt-backup-max-age-invalid")
	}
	if !runtimeInputObservationsEqual(payload["pre_purge_runtime_inputs"], config.PrePurgeRuntimeInputs) || !runtimeInputObservationsEqual(payload["runtime_inputs"], config.RuntimeInputs) {
		return nil, fmt.Errorf("isolation-receipt-runtime-inputs-invalid")
	}
	if mode, ok := exactInt(payload["scene_video_env_mode"], 384, 384); !ok || mode != 384 {
		return nil, fmt.Errorf("isolation-receipt-scene-binding-invalid")
	}
	if uid, ok := exactInt(payload["scene_video_env_uid"], config.SceneVideoEnvUID, config.SceneVideoEnvUID); !ok || uid != config.SceneVideoEnvUID {
		return nil, fmt.Errorf("isolation-receipt-scene-binding-invalid")
	}
	if gid, ok := exactInt(payload["scene_video_env_gid"], config.SceneVideoEnvGID, config.SceneVideoEnvGID); !ok || gid != config.SceneVideoEnvGID {
		return nil, fmt.Errorf("isolation-receipt-scene-binding-invalid")
	}
	if payload["production_ready"] != false || payload["secret_values_emitted"] != false {
		return nil, fmt.Errorf("isolation-receipt-claim-invalid")
	}
	started, startedOK := exactInt(payload["started_at_epoch"], 1, 1<<62)
	finished, finishedOK := exactInt(payload["finished_at_epoch"], 1, 1<<62)
	if !startedOK || !finishedOK || started < config.TransactionStartedAtEpoch || finished < started || finished-started > 7200 {
		return nil, fmt.Errorf("isolation-receipt-time-invalid")
	}
	if err := validateIsolationInstalledBindings(root, config, payload); err != nil {
		return nil, err
	}
	result, ok := payload["result"].(map[string]any)
	if !ok {
		return nil, fmt.Errorf("isolation-receipt-result-invalid")
	}
	proof := &runtimeIsolationProof{operation: operation, startedAt: started, finishedAt: finished}
	switch operation {
	case operationPurgeRuntimeIsolation:
		if err := validatePurgeIsolationResult(root, config, receiptPublic, result, proof); err != nil {
			return nil, err
		}
	case operationRetireStaleRuntime:
		if err := validateRetirementIsolationResult(root, config, receiptPublic, result, proof); err != nil {
			return nil, err
		}
	case operationRestoreRuntimeIsolation:
		if err := validateRestoreIsolationResult(root, config, receiptPublic, result, proof); err != nil {
			return nil, err
		}
	case operationVerifyRuntimeIsolation:
		if err := validateTerminalIsolationResult(root, config, receiptPublic, result, proof); err != nil {
			return nil, err
		}
	default:
		return nil, fmt.Errorf("isolation-receipt-operation-invalid")
	}
	return proof, nil
}

func validateIsolationInstalledBindings(root string, config *Config, payload map[string]any) error {
	ownerUID, ownerGID := secureOwner(root)
	authoritySignature, err := secureRead(root, ConfigSignaturePath, 0o444, ownerUID, ownerGID, maximumConfigBytes)
	if err != nil || !payloadDigestEquals(payload, "authority_signature_digest", authoritySignature) {
		zero(authoritySignature)
		return fmt.Errorf("isolation-receipt-authority-signature-invalid")
	}
	zero(authoritySignature)
	manifestRaw, err := secureRead(root, "/etc/propertyquarry-release-single-host-v2/package-manifest.v2.json", 0o444, ownerUID, ownerGID, maximumConfigBytes)
	if err != nil || !payloadDigestEquals(payload, "package_manifest_digest", manifestRaw) {
		zero(manifestRaw)
		return fmt.Errorf("isolation-receipt-package-manifest-invalid")
	}
	manifest, manifestErr := strictJSON(manifestRaw, maximumConfigBytes)
	if manifestErr != nil {
		zero(manifestRaw)
		return fmt.Errorf("isolation-receipt-package-manifest-binding-invalid")
	}
	packageAnchorRaw, err := secureRead(root, PackageAnchorPath, 0o444, ownerUID, ownerGID, 4096)
	if err != nil {
		zero(manifestRaw)
		zero(packageAnchorRaw)
		return fmt.Errorf("isolation-receipt-package-anchor-invalid")
	}
	packageAnchor, packageKeyID, keyErr := parsePublicKey(packageAnchorRaw)
	zero(packageAnchorRaw)
	manifestSignature, err := secureRead(root, "/etc/propertyquarry-release-single-host-v2/package-manifest.v2.sig", 0o444, ownerUID, ownerGID, ed25519.SignatureSize)
	if err != nil || keyErr != nil || packageKeyID != config.PackageAuthorityKeyID || len(manifestSignature) != ed25519.SignatureSize || !ed25519.Verify(packageAnchor, framed(packageManifestSignatureDomain, manifestRaw), manifestSignature) || !payloadDigestEquals(payload, "package_manifest_signature_digest", manifestSignature) {
		zero(manifestRaw)
		zero(packageAnchor)
		zero(manifestSignature)
		return fmt.Errorf("isolation-receipt-package-manifest-signature-invalid")
	}
	zero(manifestRaw)
	zero(packageAnchor)
	zero(manifestSignature)
	manifestBindings := map[string]string{
		"schema": "propertyquarry.release-control.single-host-package.v2", "package_authority_key_id": config.PackageAuthorityKeyID,
		"receipt_authority_key_id": config.ReceiptAuthorityKeyID, "config_digest": config.Digest, "plan_digest": config.PlanDigest,
		"deployment_id": config.DeploymentID, "runtime_sha": config.RuntimeSHA, "workflow_sha": config.WorkflowSHA, "envelope_sha": config.EnvelopeSHA,
		"web_image": config.WebImage, "render_image": config.RenderImage, "cloudflared_image": config.CloudflaredImage, "database_image": config.DatabaseImage,
		"api_host_ip": config.APIHostIP, "pre_purge_root_env_digest": config.PrePurgeRootEnvDigest,
		"post_purge_root_env_digest": config.PostPurgeRootEnvDigest, "runtime_retirement_digest": config.RuntimeRetirementDigest,
		"runtime_deploy_digest": config.RuntimeDeployDigest, "database_substrate_digest": config.DatabaseSubstrateDigest,
	}
	for field, expected := range manifestBindings {
		actual, ok := exactString(manifest[field])
		if !ok || actual != expected {
			return fmt.Errorf("isolation-receipt-package-manifest-binding-invalid")
		}
	}
	if port, ok := exactInt(manifest["api_host_port"], config.APIHostPort, config.APIHostPort); !ok || port != config.APIHostPort {
		return fmt.Errorf("isolation-receipt-package-manifest-binding-invalid")
	}
	if port, ok := exactInt(manifest["api_container_port"], config.APIContainerPort, config.APIContainerPort); !ok || port != config.APIContainerPort {
		return fmt.Errorf("isolation-receipt-package-manifest-binding-invalid")
	}
	if started, ok := exactInt(manifest["transaction_started_at_epoch"], config.TransactionStartedAtEpoch, config.TransactionStartedAtEpoch); !ok || started != config.TransactionStartedAtEpoch {
		return fmt.Errorf("isolation-receipt-package-manifest-binding-invalid")
	}
	if maximumAge, ok := exactInt(manifest["backup_max_age_seconds"], BackupMaxAgeSeconds, BackupMaxAgeSeconds); !ok || maximumAge != config.BackupMaxAgeSeconds {
		return fmt.Errorf("isolation-receipt-package-manifest-binding-invalid")
	}
	for field, observations := range map[string][]runtimeInputObservation{
		"pre_purge_runtime_inputs_digest": config.PrePurgeRuntimeInputs,
		"runtime_inputs_digest":           config.RuntimeInputs,
	} {
		raw, err := canonicalJSON(runtimeInputObservationValues(observations))
		if err != nil || manifest[field] != digest(raw) {
			zero(raw)
			return fmt.Errorf("isolation-receipt-package-manifest-binding-invalid")
		}
		zero(raw)
	}
	return nil
}

func validatePurgeIsolationResult(root string, config *Config, receiptPublic ed25519.PublicKey, result map[string]any, proof *runtimeIsolationProof) error {
	if !hasKeys(result, "backup_receipt_sha256", "inputs", "legacy_keys_removed", "post_purge_root_env_digest", "pre_purge_root_env_digest", "rollback_artifact", "rollback_artifact_expected_removed_keys") {
		return fmt.Errorf("isolation-purge-result-shape-invalid")
	}
	backupProof, err := verifyIsolationBackupReceipt(root, config, receiptPublic)
	if err != nil || !backupPrecedesIsolation(proof, backupProof, config.BackupMaxAgeSeconds) {
		return fmt.Errorf("isolation-purge-backup-invalid")
	}
	backupDigest, backupOK := exactString(result["backup_receipt_sha256"])
	preDigest, preOK := exactString(result["pre_purge_root_env_digest"])
	postDigest, postOK := exactString(result["post_purge_root_env_digest"])
	removed, removedOK := exactInt(result["legacy_keys_removed"], 0, 8)
	expectedRemoved, expectedRemovedOK := exactInt(result["rollback_artifact_expected_removed_keys"], 8, 8)
	if !backupOK || backupDigest != backupProof.receiptDigest || !preOK || preDigest != config.PrePurgeRootEnvDigest || !postOK || postDigest != config.PostPurgeRootEnvDigest || !removedOK || (removed != 0 && removed != 8) || !expectedRemovedOK || expectedRemoved != 8 {
		return fmt.Errorf("isolation-purge-result-binding-invalid")
	}
	inputs, err := validateIsolationInputs(root, config, result["inputs"], false)
	if err != nil || inputs[BaseEnvironmentPath] != postDigest {
		return fmt.Errorf("isolation-purge-inputs-invalid")
	}
	artifactDigest, err := validateIsolationRollbackArtifact(root, config, result["rollback_artifact"])
	if err != nil {
		return err
	}
	proof.backupReceiptDigest = backupDigest
	proof.prePurgeRootEnvDigest = preDigest
	proof.postPurgeRootEnvDigest = postDigest
	proof.rollbackArtifactDigest = artifactDigest
	return nil
}

func validateRetirementIsolationResult(root string, config *Config, receiptPublic ed25519.PublicKey, result map[string]any, proof *runtimeIsolationProof) error {
	if !hasKeys(result, "backup_receipt_sha256", "preserved_volumes", "purge_receipt_sha256", "retired_containers", "unknown_matches", "volumes_removed") {
		return fmt.Errorf("isolation-retirement-result-shape-invalid")
	}
	backupProof, err := verifyIsolationBackupReceipt(root, config, receiptPublic)
	if err != nil || !backupPrecedesIsolation(proof, backupProof, config.BackupMaxAgeSeconds) {
		return fmt.Errorf("isolation-retirement-backup-invalid")
	}
	backupDigest, backupOK := exactString(result["backup_receipt_sha256"])
	if !backupOK || backupDigest != backupProof.receiptDigest || result["volumes_removed"] != false {
		return fmt.Errorf("isolation-retirement-result-binding-invalid")
	}
	purgeProof, err := verifyRuntimeIsolationReceipt(root, config, receiptPublic, operationPurgeRuntimeIsolation, 1, 1<<62)
	purgeDigest, purgeOK := exactString(result["purge_receipt_sha256"])
	if err != nil || !purgeOK || purgeProof == nil || purgeDigest != purgeProof.receiptDigest || purgeProof.backupReceiptDigest != backupDigest || proof.startedAt < purgeProof.finishedAt {
		return fmt.Errorf("isolation-retirement-purge-invalid")
	}
	unknown, ok := result["unknown_matches"].([]any)
	if !ok || len(unknown) != 0 {
		return fmt.Errorf("isolation-retirement-unknown-match-invalid")
	}
	expectedContainers := config.RuntimeRetirement["containers"]
	if !canonicalValuesEqual(result["retired_containers"], expectedContainers) {
		return fmt.Errorf("isolation-retirement-container-proof-invalid")
	}
	expectedVolumeNames, err := retirementVolumeNames(expectedContainers)
	if err != nil || !validPreservedVolumes(result["preserved_volumes"], expectedVolumeNames) {
		return fmt.Errorf("isolation-retirement-volume-proof-invalid")
	}
	proof.backupReceiptDigest = backupDigest
	proof.purgeReceiptDigest = purgeDigest
	return nil
}

func backupPrecedesIsolation(proof *runtimeIsolationProof, backup *backupReceiptProof, maximumAge int64) bool {
	return proof != nil && backup != nil && maximumAge == BackupMaxAgeSeconds && backup.startedAt >= 1 && backup.finishedAt >= backup.startedAt && proof.startedAt >= backup.finishedAt && proof.startedAt-backup.finishedAt <= maximumAge
}

func retirementVolumeNames(value any) ([]string, error) {
	containers, ok := value.([]any)
	if !ok {
		return nil, fmt.Errorf("retirement-containers-invalid")
	}
	names := make(map[string]struct{})
	for _, rawContainer := range containers {
		container, ok := rawContainer.(map[string]any)
		if !ok {
			return nil, fmt.Errorf("retirement-container-invalid")
		}
		mounts, ok := container["mounts"].([]any)
		if !ok {
			return nil, fmt.Errorf("retirement-mounts-invalid")
		}
		for _, rawMount := range mounts {
			mount, ok := rawMount.(map[string]any)
			if !ok {
				return nil, fmt.Errorf("retirement-mount-invalid")
			}
			if mount["type"] == "volume" {
				name, ok := exactString(mount["name"])
				if !ok {
					return nil, fmt.Errorf("retirement-volume-name-invalid")
				}
				names[name] = struct{}{}
			}
		}
	}
	result := make([]string, 0, len(names))
	for name := range names {
		result = append(result, name)
	}
	sort.Strings(result)
	return result, nil
}

func validPreservedVolumes(value any, expectedNames []string) bool {
	items, ok := value.([]any)
	if !ok || len(items) != len(expectedNames) {
		return false
	}
	for index, raw := range items {
		volume, ok := raw.(map[string]any)
		if !ok || !hasKeys(volume, "created_at", "driver", "labels", "mountpoint", "name", "options", "scope") || volume["name"] != expectedNames[index] {
			return false
		}
		createdAt, createdOK := exactString(volume["created_at"])
		driver, driverOK := exactString(volume["driver"])
		mountpoint, mountpointOK := exactString(volume["mountpoint"])
		scope, scopeOK := exactString(volume["scope"])
		labels, labelsOK := volume["labels"].(map[string]any)
		options, optionsOK := volume["options"].(map[string]any)
		if !createdOK || !runtimeVolumeCreatedPattern.MatchString(createdAt) || !driverOK || driver == "" || !mountpointOK || !filepath.IsAbs(mountpoint) || !scopeOK || scope == "" || !labelsOK || !optionsOK || !validStringMap(labels, 64) || !validStringMap(options, 64) {
			return false
		}
	}
	return true
}

func validateRestoreIsolationResult(root string, config *Config, receiptPublic ed25519.PublicKey, result map[string]any, proof *runtimeIsolationProof) error {
	if !hasKeys(result, "backup_receipt_sha256", "expected_post_purge_root_env_digest", "pre_purge_root_env_digest", "restored", "restored_root_env_digest", "rollback_artifact", "runtime_inputs") {
		return fmt.Errorf("isolation-restore-result-shape-invalid")
	}
	backupProof, err := verifyIsolationBackupReceipt(root, config, receiptPublic)
	if err != nil || !backupPrecedesIsolation(proof, backupProof, config.BackupMaxAgeSeconds) {
		return fmt.Errorf("isolation-restore-backup-invalid")
	}
	backupDigest, backupOK := exactString(result["backup_receipt_sha256"])
	preDigest, preOK := exactString(result["pre_purge_root_env_digest"])
	postDigest, postOK := exactString(result["expected_post_purge_root_env_digest"])
	restoredDigest, restoredOK := exactString(result["restored_root_env_digest"])
	if _, ok := result["restored"].(bool); !ok || !backupOK || backupDigest != backupProof.receiptDigest || !preOK || preDigest != config.PrePurgeRootEnvDigest || !postOK || postDigest != config.PostPurgeRootEnvDigest || !restoredOK || restoredDigest != preDigest {
		return fmt.Errorf("isolation-restore-result-binding-invalid")
	}
	inputs, err := validateIsolationInputs(root, config, result["runtime_inputs"], true)
	if err != nil || inputs[BaseEnvironmentPath] != preDigest {
		return fmt.Errorf("isolation-restore-inputs-invalid")
	}
	artifactDigest, err := validateIsolationRollbackArtifact(root, config, result["rollback_artifact"])
	if err != nil {
		return err
	}
	proof.backupReceiptDigest = backupDigest
	proof.prePurgeRootEnvDigest = preDigest
	proof.postPurgeRootEnvDigest = postDigest
	proof.rollbackArtifactDigest = artifactDigest
	return nil
}

func validateIsolationRollbackArtifact(root string, config *Config, value any) (string, error) {
	artifact, ok := value.(map[string]any)
	if !ok || !hasKeys(artifact, "ciphertext_bytes", "ciphertext_sha256", "encryption_key_id", "path", "plaintext_bytes", "plaintext_sha256") {
		return "", fmt.Errorf("isolation-rollback-artifact-shape-invalid")
	}
	expectedPath := filepath.Join(runtimeIsolationRollbackDirectory, config.RuntimeSHA, config.DeploymentID, "root-env.pre-purge.enc")
	path, pathOK := exactString(artifact["path"])
	ciphertextDigest, ciphertextOK := exactString(artifact["ciphertext_sha256"])
	plaintextDigest, plaintextOK := exactString(artifact["plaintext_sha256"])
	keyID, keyOK := exactString(artifact["encryption_key_id"])
	ciphertextBytes, ciphertextBytesOK := exactInt(artifact["ciphertext_bytes"], 1, 3*1024*1024)
	plaintextBytes, plaintextBytesOK := exactInt(artifact["plaintext_bytes"], 1, 256*1024)
	if !pathOK || path != expectedPath || !ciphertextOK || !digestPattern.MatchString(ciphertextDigest) || !plaintextOK || plaintextDigest != config.PrePurgeRootEnvDigest || !keyOK || validateBackupEncryptionKey(root, keyID) != nil || !ciphertextBytesOK || !plaintextBytesOK || plaintextBytes < 1 {
		return "", fmt.Errorf("isolation-rollback-artifact-binding-invalid")
	}
	ownerUID, ownerGID := secureOwner(root)
	raw, err := secureRead(root, expectedPath, 0o600, ownerUID, ownerGID, 3*1024*1024)
	if err != nil || int64(len(raw)) != ciphertextBytes || digest(raw) != ciphertextDigest {
		zero(raw)
		return "", fmt.Errorf("isolation-rollback-artifact-content-invalid")
	}
	zero(raw)
	return ciphertextDigest, nil
}

func validateIsolationInputs(root string, config *Config, value any, legacyMail bool) (map[string]string, error) {
	inputs, ok := value.(map[string]any)
	if !ok || !hasKeys(inputs, "file_digests", "google_key_count", "legacy_registration_email_present", "registration_email_key_count") {
		return nil, fmt.Errorf("isolation-inputs-shape-invalid")
	}
	if count, ok := exactInt(inputs["google_key_count"], 5, 5); !ok || count != 5 {
		return nil, fmt.Errorf("isolation-inputs-key-count-invalid")
	}
	if count, ok := exactInt(inputs["registration_email_key_count"], 8, 8); !ok || count != 8 {
		return nil, fmt.Errorf("isolation-inputs-key-count-invalid")
	}
	if present, ok := inputs["legacy_registration_email_present"].(bool); !ok || present != legacyMail {
		return nil, fmt.Errorf("isolation-inputs-legacy-state-invalid")
	}
	fileDigests, ok := inputs["file_digests"].(map[string]any)
	if !ok || len(fileDigests) != len(runtimeIsolationInputPaths) {
		return nil, fmt.Errorf("isolation-input-digests-shape-invalid")
	}
	result := make(map[string]string, len(fileDigests))
	expected := config.RuntimeInputs
	if legacyMail {
		expected = config.PrePurgeRuntimeInputs
	}
	for _, path := range runtimeIsolationInputPaths {
		value, ok := exactString(fileDigests[path])
		if !ok || !digestPattern.MatchString(value) {
			return nil, fmt.Errorf("isolation-input-digest-invalid")
		}
		result[path] = value
	}
	for index, observation := range expected {
		if result[runtimeIsolationInputPaths[index]] != observation.digest {
			return nil, fmt.Errorf("isolation-input-signed-binding-invalid")
		}
	}
	if result[GoogleIdentityEnvPath] != config.GoogleIdentityEnvDigest || result[RegistrationEmailEnvPath] != config.RegistrationEmailEnvDigest || result[SceneVideoEnvPath] != config.SceneVideoEnvDigest || validateDatabaseRuntimeEnvironment(root, result[DatabaseRuntimeEnvironmentPath]) != nil {
		return nil, fmt.Errorf("isolation-input-binding-invalid")
	}
	checks := []struct {
		path   string
		uid    int64
		gid    int64
		digest string
	}{
		{BaseEnvironmentPath, 1000, 1000, result[BaseEnvironmentPath]},
		{AdmissionEnvPath, 1000, 1000, result[AdmissionEnvPath]},
		{GoogleIdentityEnvPath, config.GoogleIdentityEnvUID, config.GoogleIdentityEnvGID, result[GoogleIdentityEnvPath]},
		{RegistrationEmailEnvPath, config.RegistrationEmailEnvUID, config.RegistrationEmailEnvGID, result[RegistrationEmailEnvPath]},
		{SceneVideoEnvPath, config.SceneVideoEnvUID, config.SceneVideoEnvGID, result[SceneVideoEnvPath]},
	}
	for _, check := range checks {
		if validateExternalDigestFile(root, check.path, 0o600, uint32(check.uid), uint32(check.gid), check.digest, 256*1024) != nil {
			return nil, fmt.Errorf("isolation-input-current-file-invalid")
		}
	}
	if validateExactCurrentRuntimeInputs(root, expected) != nil {
		return nil, fmt.Errorf("isolation-input-current-observation-invalid")
	}
	return result, nil
}

func validateTerminalIsolationResult(root string, config *Config, receiptPublic ed25519.PublicKey, result map[string]any, proof *runtimeIsolationProof) error {
	if !hasKeys(result, "backup_receipt_sha256", "database_receipts", "database_substrate", "deploy_receipt_sha256", "exposure", "inputs", "local_http") {
		return fmt.Errorf("isolation-terminal-result-shape-invalid")
	}
	backupProof, err := verifyIsolationBackupReceipt(root, config, receiptPublic)
	if err != nil || !backupPrecedesIsolation(proof, backupProof, config.BackupMaxAgeSeconds) {
		return fmt.Errorf("isolation-terminal-backup-invalid")
	}
	backupDigest, ok := exactString(result["backup_receipt_sha256"])
	if !ok || backupDigest != backupProof.receiptDigest {
		return fmt.Errorf("isolation-terminal-backup-binding-invalid")
	}
	deployDigest, deployOK := exactString(result["deploy_receipt_sha256"])
	if !deployOK || !digestPattern.MatchString(deployDigest) {
		return fmt.Errorf("isolation-terminal-deploy-binding-invalid")
	}
	if !databaseSubstrateValueEqual(result["database_substrate"], config.DatabaseSubstrate) {
		return fmt.Errorf("isolation-terminal-database-substrate-invalid")
	}
	inputs, err := validateIsolationInputs(root, config, result["inputs"], false)
	if err != nil {
		return fmt.Errorf("isolation-terminal-inputs-invalid")
	}
	databaseReceipts, err := validateIsolationDatabaseReceipts(root, config, receiptPublic, result["database_receipts"])
	if err != nil {
		return err
	}
	exposureImageID, exposureRepoDigest, err := validateIsolationExposure(config, result["exposure"], inputs)
	if err != nil {
		return err
	}
	first := databaseReceipts["provision-roles"]
	if exposureImageID != first.databaseImageID || exposureRepoDigest != first.databaseRepoDigest || backupProof.databaseImageID != first.databaseImageID || backupProof.databaseRepoDigest != first.databaseRepoDigest || backupProof.databaseSubstrate == nil || backupProof.databaseSubstrate.digest != config.DatabaseSubstrateDigest {
		return fmt.Errorf("isolation-terminal-database-identity-invalid")
	}
	if err := validateIsolationLocalHTTP(result["local_http"]); err != nil {
		return err
	}
	proof.backupReceiptDigest = backupDigest
	proof.deployReceiptDigest = deployDigest
	proof.postPurgeRootEnvDigest = inputs[BaseEnvironmentPath]
	proof.databaseImageID = first.databaseImageID
	proof.databaseRepoDigest = first.databaseRepoDigest
	proof.databaseSubstrateDigest = config.DatabaseSubstrateDigest
	proof.databaseEnvDigest = first.envFileDigest
	proof.databaseReceipts = databaseReceipts
	return nil
}

func validateIsolationDatabaseReceipts(root string, config *Config, receiptPublic ed25519.PublicKey, value any) (map[string]isolationDatabaseReceiptSummary, error) {
	items, ok := value.(map[string]any)
	if !ok || len(items) != 4 {
		return nil, fmt.Errorf("isolation-database-receipts-shape-invalid")
	}
	result := make(map[string]isolationDatabaseReceiptSummary, 4)
	var commonOID int64
	var commonImageID, commonRepoDigest, commonEnvDigest string
	var previousFinished int64
	expectedStatus := map[string]string{"provision-roles": "provisioned", "migrate-schema": "migrated", "harden-runtime-acl": "ready", "verify-schema-readiness": "ready"}
	for _, contract := range databaseOperationContracts() {
		item, ok := items[contract.operation].(map[string]any)
		if !ok || !hasKeys(item, "database_image_id", "database_oid", "database_repo_digest", "env_file_sha256", "finished_at_epoch", "receipt_sha256", "schema_status", "started_at_epoch") {
			return nil, fmt.Errorf("isolation-database-receipt-summary-shape-invalid")
		}
		imageID, imageOK := exactString(item["database_image_id"])
		repoDigest, repoOK := exactString(item["database_repo_digest"])
		envDigest, envOK := exactString(item["env_file_sha256"])
		receiptDigest, receiptOK := exactString(item["receipt_sha256"])
		schemaStatus, statusOK := exactString(item["schema_status"])
		databaseOID, oidOK := exactInt(item["database_oid"], 1, 1<<62)
		started, startedOK := exactInt(item["started_at_epoch"], 1, 1<<62)
		finished, finishedOK := exactInt(item["finished_at_epoch"], 1, 1<<62)
		if !imageOK || !digestPattern.MatchString(imageID) || !repoOK || repoDigest != canonicalRepoDigest(config.DatabaseImage) || !envOK || !digestPattern.MatchString(envDigest) || !receiptOK || !digestPattern.MatchString(receiptDigest) || !statusOK || schemaStatus != expectedStatus[contract.operation] || !oidOK || config.DatabaseSubstrate == nil || databaseOID != config.DatabaseSubstrate.databaseOID || !startedOK || !finishedOK || finished < started || started < previousFinished {
			return nil, fmt.Errorf("isolation-database-receipt-summary-invalid")
		}
		verified, err := verifyIsolationDatabaseReceipt(root, config, receiptPublic, contract.operation, started, finished)
		if err != nil || verified.receiptDigest != receiptDigest || verified.databaseImageID != imageID || verified.databaseRepoDigest != repoDigest || verified.envFileDigest != envDigest || verified.databaseOID != databaseOID || verified.schemaStatus != schemaStatus || verified.startedAt != started || verified.finishedAt != finished || verified.databaseSubstrate == nil || verified.databaseSubstrate.digest != config.DatabaseSubstrateDigest {
			return nil, fmt.Errorf("isolation-database-receipt-proof-invalid")
		}
		if len(result) == 0 {
			commonOID, commonImageID, commonRepoDigest, commonEnvDigest = databaseOID, imageID, repoDigest, envDigest
		} else if databaseOID != commonOID || imageID != commonImageID || repoDigest != commonRepoDigest || envDigest != commonEnvDigest || verified.predecessorReceiptDigest != result[databaseOperationContracts()[len(result)-1].operation].receiptDigest {
			return nil, fmt.Errorf("isolation-database-receipt-continuity-invalid")
		}
		previousFinished = finished
		result[contract.operation] = isolationDatabaseReceiptSummary{operation: contract.operation, receiptDigest: receiptDigest, envFileDigest: envDigest, databaseImageID: imageID, databaseRepoDigest: repoDigest, databaseOID: databaseOID, schemaStatus: schemaStatus, startedAt: started, finishedAt: finished}
	}
	return result, nil
}

func validateIsolationExposure(config *Config, value any, inputs map[string]string) (string, string, error) {
	exposure, ok := value.(map[string]any)
	if !ok || !hasKeys(exposure, "admission_env_sha256", "containers", "database_env_sha256", "google_env_sha256", "google_key_count", "legacy_registration_email_present", "one_shot_containers", "registration_email_env_sha256", "registration_email_key_count", "render_provider_env_sha256", "render_provider_key_count", "topology_isolated") {
		return "", "", fmt.Errorf("isolation-exposure-shape-invalid")
	}
	if exposure["topology_isolated"] != true || exposure["legacy_registration_email_present"] != false || exposure["admission_env_sha256"] != inputs[AdmissionEnvPath] || exposure["database_env_sha256"] != inputs[DatabaseRuntimeEnvironmentPath] || exposure["google_env_sha256"] != config.GoogleIdentityEnvDigest || exposure["registration_email_env_sha256"] != config.RegistrationEmailEnvDigest || exposure["render_provider_env_sha256"] != config.SceneVideoEnvDigest {
		return "", "", fmt.Errorf("isolation-exposure-binding-invalid")
	}
	for field, expected := range map[string]int64{"google_key_count": 5, "registration_email_key_count": 8} {
		if count, ok := exactInt(exposure[field], expected, expected); !ok || count != expected {
			return "", "", fmt.Errorf("isolation-exposure-count-invalid")
		}
	}
	if _, ok := exactInt(exposure["render_provider_key_count"], 1, 1024); !ok {
		return "", "", fmt.Errorf("isolation-exposure-count-invalid")
	}
	containers, ok := exposure["containers"].([]any)
	if !ok || len(containers) != 6 {
		return "", "", fmt.Errorf("isolation-exposure-containers-invalid")
	}
	expectedNames := []string{"propertyquarry-api-live", "propertyquarry-cloudflared-live", "propertyquarry-db-live", "propertyquarry-render-live", "propertyquarry-scheduler-live", "propertyquarry-worker-live"}
	services := map[string]string{"propertyquarry-api-live": "propertyquarry-api", "propertyquarry-cloudflared-live": "propertyquarry-cloudflared", "propertyquarry-db-live": "propertyquarry-db", "propertyquarry-render-live": "propertyquarry-render-tools", "propertyquarry-scheduler-live": "propertyquarry-scheduler", "propertyquarry-worker-live": "propertyquarry-worker"}
	networks := map[string][]string{
		"propertyquarry-api-live": {"property_default", "property_propertyquarry_render_internal"}, "propertyquarry-cloudflared-live": {"property_default"},
		"propertyquarry-db-live": {"property_default", "property_propertyquarry_render_internal"}, "propertyquarry-render-live": {"property_propertyquarry_render_internal"},
		"propertyquarry-scheduler-live": {"property_default"}, "propertyquarry-worker-live": {"property_default"},
	}
	volumes := map[string][]string{
		"propertyquarry-api-live":         {"/docker/property/config", "/docker/property/state/incoming_property_tours", "property_propertyquarry_artifacts", "property_propertyquarry_governed_render_consents", "property_propertyquarry_provider_ledger", "property_propertyquarry_public_tours"},
		"propertyquarry-cloudflared-live": {}, "propertyquarry-db-live": {"property_propertyquarry_pgdata"}, "propertyquarry-render-live": {"property_propertyquarry_public_tours"},
		"propertyquarry-scheduler-live": {"/docker/property/config", "/docker/property/state/incoming_property_tours", "property_propertyquarry_artifacts", "property_propertyquarry_provider_ledger", "property_propertyquarry_public_tours"},
		"propertyquarry-worker-live":    {"/docker/property/config", "property_propertyquarry_artifacts", "property_propertyquarry_provider_ledger"},
	}
	images := map[string]string{"propertyquarry-api-live": config.WebImage, "propertyquarry-worker-live": config.WebImage, "propertyquarry-scheduler-live": config.WebImage, "propertyquarry-render-live": config.RenderImage, "propertyquarry-db-live": config.DatabaseImage, "propertyquarry-cloudflared-live": config.CloudflaredImage}
	var databaseImageID, databaseRepoDigest string
	for index, expectedName := range expectedNames {
		container, ok := containers[index].(map[string]any)
		if !ok || !hasKeys(container, "compose_service", "container_id", "health", "image", "image_id", "name", "networks", "repo_digest", "volumes") || container["name"] != expectedName || container["compose_service"] != services[expectedName] || container["image"] != images[expectedName] || container["repo_digest"] != canonicalRepoDigest(images[expectedName]) || !exactStringArray(container["networks"], networks[expectedName]) || !exactStringArray(container["volumes"], volumes[expectedName]) {
			return "", "", fmt.Errorf("isolation-exposure-container-binding-invalid")
		}
		containerID, containerIDOK := exactString(container["container_id"])
		imageID, imageOK := exactString(container["image_id"])
		health, healthOK := exactString(container["health"])
		expectedHealth := "healthy"
		if expectedName == "propertyquarry-cloudflared-live" {
			expectedHealth = "not-configured"
		}
		if !containerIDOK || !runtimeContainerIDPattern.MatchString(containerID) || !imageOK || !digestPattern.MatchString(imageID) || !healthOK || health != expectedHealth {
			return "", "", fmt.Errorf("isolation-exposure-container-state-invalid")
		}
		if expectedName == "propertyquarry-db-live" {
			if config.DatabaseSubstrate == nil || containerID != config.DatabaseSubstrate.containerID {
				return "", "", fmt.Errorf("isolation-exposure-database-container-invalid")
			}
			databaseImageID, databaseRepoDigest = imageID, canonicalRepoDigest(images[expectedName])
		}
	}
	oneShot, ok := exposure["one_shot_containers"].([]any)
	if !ok || len(oneShot) != 1 {
		return "", "", fmt.Errorf("isolation-exposure-one-shot-invalid")
	}
	migration, ok := oneShot[0].(map[string]any)
	if !ok || !hasKeys(migration, "compose_service", "exit_code", "image", "image_id", "name", "networks", "repo_digest", "status") || migration["compose_service"] != "propertyquarry-migrate" || migration["image"] != config.WebImage || migration["name"] != "propertyquarry-migrate-live" || migration["repo_digest"] != canonicalRepoDigest(config.WebImage) || migration["status"] != "exited" || !exactStringArray(migration["networks"], []string{"property_default"}) {
		return "", "", fmt.Errorf("isolation-exposure-one-shot-binding-invalid")
	}
	if exitCode, ok := exactInt(migration["exit_code"], 0, 0); !ok || exitCode != 0 {
		return "", "", fmt.Errorf("isolation-exposure-one-shot-state-invalid")
	}
	if imageID, ok := exactString(migration["image_id"]); !ok || !digestPattern.MatchString(imageID) {
		return "", "", fmt.Errorf("isolation-exposure-one-shot-state-invalid")
	}
	return databaseImageID, databaseRepoDigest, nil
}

func validateIsolationLocalHTTP(value any) error {
	checks, ok := value.(map[string]any)
	if !ok || len(checks) != 3 {
		return fmt.Errorf("isolation-local-http-shape-invalid")
	}
	for _, path := range []string{"/health/ready", "/register", "/sign-in"} {
		check, ok := checks[path].(map[string]any)
		if !ok || !hasKeys(check, "body_sha256", "bytes", "status") {
			return fmt.Errorf("isolation-local-http-check-invalid")
		}
		bodyDigest, digestOK := exactString(check["body_sha256"])
		bodyBytes, bytesOK := exactInt(check["bytes"], 1, 2*1024*1024)
		status, statusOK := exactInt(check["status"], 200, 200)
		if !digestOK || !digestPattern.MatchString(bodyDigest) || !bytesOK || bodyBytes < 1 || !statusOK || status != 200 {
			return fmt.Errorf("isolation-local-http-check-invalid")
		}
	}
	return nil
}

func exactStringArray(value any, expected []string) bool {
	items, ok := value.([]any)
	if !ok || len(items) != len(expected) {
		return false
	}
	actual := make([]string, len(items))
	for index, item := range items {
		text, ok := exactString(item)
		if !ok {
			return false
		}
		actual[index] = text
	}
	if !sort.StringsAreSorted(actual) || !sort.StringsAreSorted(expected) {
		return false
	}
	for index := range expected {
		if actual[index] != expected[index] {
			return false
		}
	}
	return true
}

func isolationReceiptProofFields(proof *runtimeIsolationProof) map[string]any {
	if proof == nil {
		return nil
	}
	prefix := map[string]string{
		operationPurgeRuntimeIsolation:   "runtime_isolation_purge",
		operationRetireStaleRuntime:      "runtime_retirement",
		operationRestoreRuntimeIsolation: "runtime_isolation_restore",
		operationVerifyRuntimeIsolation:  "runtime_isolation_terminal",
	}[proof.operation]
	if prefix == "" {
		return nil
	}
	result := map[string]any{
		prefix + "_verified":          true,
		prefix + "_receipt_digest":    proof.receiptDigest,
		prefix + "_started_at_epoch":  json.Number(fmt.Sprintf("%d", proof.startedAt)),
		prefix + "_finished_at_epoch": json.Number(fmt.Sprintf("%d", proof.finishedAt)),
	}
	if proof.backupReceiptDigest != "" {
		result[prefix+"_backup_receipt_digest"] = proof.backupReceiptDigest
	}
	if proof.purgeReceiptDigest != "" {
		result[prefix+"_purge_receipt_digest"] = proof.purgeReceiptDigest
	}
	if proof.deployReceiptDigest != "" {
		result["runtime_deploy_receipt_digest"] = proof.deployReceiptDigest
	}
	if proof.prePurgeRootEnvDigest != "" {
		result["pre_purge_root_env_digest"] = proof.prePurgeRootEnvDigest
	}
	if proof.postPurgeRootEnvDigest != "" {
		result["post_purge_root_env_digest"] = proof.postPurgeRootEnvDigest
	}
	if proof.rollbackArtifactDigest != "" {
		result["runtime_isolation_rollback_artifact_digest"] = proof.rollbackArtifactDigest
	}
	if proof.databaseImageID != "" {
		result["runtime_isolation_database_image_id"] = proof.databaseImageID
		result["runtime_isolation_database_repo_digest"] = proof.databaseRepoDigest
		result["runtime_isolation_database_env_digest"] = proof.databaseEnvDigest
	}
	for operation, summary := range proof.databaseReceipts {
		result["runtime_isolation_database_"+operation+"_receipt_digest"] = summary.receiptDigest
	}
	return result
}

func isolationDatabaseProofMatches(fields map[string]any, proof *runtimeIsolationProof) bool {
	if fields == nil || proof == nil || proof.operation != operationVerifyRuntimeIsolation || len(proof.databaseReceipts) != 4 {
		return false
	}
	imageID, imageOK := exactString(fields["database_image_id"])
	repoDigest, repoOK := exactString(fields["database_repo_digest"])
	envDigest, envOK := exactString(fields["database_runtime_env_digest"])
	substrateDigest, substrateOK := exactString(fields["database_substrate_digest"])
	if !imageOK || imageID != proof.databaseImageID || !repoOK || repoDigest != proof.databaseRepoDigest || !envOK || envDigest != proof.databaseEnvDigest || !substrateOK || substrateDigest != proof.databaseSubstrateDigest {
		return false
	}
	for _, contract := range databaseOperationContracts() {
		summary, ok := proof.databaseReceipts[contract.operation]
		if !ok {
			return false
		}
		prefix := map[string]string{"provision-roles": "database_provision_roles", "migrate-schema": "database_migrate_schema", "harden-runtime-acl": "database_harden_runtime_acl", "verify-schema-readiness": "database_verify_schema_readiness"}[contract.operation]
		digestValue, ok := exactString(fields[prefix+"_receipt_digest"])
		if !ok || digestValue != summary.receiptDigest {
			return false
		}
	}
	return true
}
