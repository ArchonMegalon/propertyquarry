package authority

import (
	"crypto/ed25519"
	"encoding/json"
	"fmt"
	"path/filepath"
)

const (
	runtimeDeployReceiptSchema          = "propertyquarry.runtime-deploy-receipt.v2"
	runtimeDeployReceiptSignatureDomain = "propertyquarry.release-control.single-host-runtime-deploy-receipt-signature.v2\x00"
	maximumRuntimeDeployReceiptBytes    = 8 * 1024 * 1024
)

type runtimeDeployProof struct {
	receiptDigest           string
	backupReceiptDigest     string
	purgeReceiptDigest      string
	retirementReceiptDigest string
	databaseReceiptDigests  map[string]string
	databaseSubstrateDigest string
	startedAt               int64
	finishedAt              int64
}

func verifyRuntimeDeployReceipt(root string, config *Config, receiptPublic ed25519.PublicKey, stepStartedAt, stepFinishedAt int64) (*runtimeDeployProof, error) {
	if config == nil || len(receiptPublic) != ed25519.PublicKeySize {
		return nil, fmt.Errorf("deploy-receipt-input-invalid")
	}
	path := filepath.Join(RuntimeDeployReceiptDirectory, config.RuntimeSHA, config.DeploymentID, "deploy-runtime.json")
	ownerUID, ownerGID := secureOwner(root)
	raw, err := secureRead(root, path, 0o600, ownerUID, ownerGID, maximumRuntimeDeployReceiptBytes)
	if err != nil || len(raw) < 2 || raw[len(raw)-1] != '\n' || raw[len(raw)-2] == '\n' {
		zero(raw)
		return nil, fmt.Errorf("deploy-receipt-unavailable")
	}
	defer zero(raw)
	wrapper, err := strictJSON(raw[:len(raw)-1], maximumRuntimeDeployReceiptBytes)
	if err != nil || !hasKeys(wrapper, "payload", "signature", "signature_key_id") {
		return nil, fmt.Errorf("deploy-receipt-wrapper-invalid")
	}
	payload, payloadOK := wrapper["payload"].(map[string]any)
	signatureText, signatureOK := exactString(wrapper["signature"])
	signatureKeyID, keyIDOK := exactString(wrapper["signature_key_id"])
	expectedKeyID, keyErr := publicKeyID(receiptPublic)
	signature, signatureErr := signatureBytes(signatureText)
	payloadRaw, canonicalErr := canonicalJSON(payload)
	if !payloadOK || !signatureOK || !keyIDOK || keyErr != nil || signatureKeyID != expectedKeyID || expectedKeyID != config.ReceiptAuthorityKeyID || signatureErr != nil || canonicalErr != nil || len(signature) != ed25519.SignatureSize || !ed25519.Verify(receiptPublic, framed(runtimeDeployReceiptSignatureDomain, payloadRaw), signature) {
		zero(signature)
		zero(payloadRaw)
		return nil, fmt.Errorf("deploy-receipt-signature-invalid")
	}
	zero(signature)
	defer zero(payloadRaw)
	proof, err := validateRuntimeDeployPayload(root, config, receiptPublic, payload)
	if err != nil {
		return nil, err
	}
	if stepStartedAt < 1 || stepFinishedAt < stepStartedAt || proof.startedAt < stepStartedAt || proof.finishedAt > stepFinishedAt {
		return nil, fmt.Errorf("deploy-receipt-step-time-binding-invalid")
	}
	proof.receiptDigest = digest(raw)
	return proof, nil
}

func validateRuntimeDeployPayload(root string, config *Config, receiptPublic ed25519.PublicKey, payload map[string]any) (*runtimeDeployProof, error) {
	if !hasKeys(payload,
		"api_container_port", "api_host_ip", "api_host_port", "argv_count", "argv_sha256", "authority_digest", "authority_signature_digest",
		"backup_max_age_seconds", "backup_receipt_sha256", "build_performed", "cloudflared_image", "config_digest", "database_container", "database_container_id",
		"database_image", "database_image_id", "database_oid", "database_pgdata_volume", "database_receipts", "database_repo_digest", "deployment_id", "duration_seconds",
		"environment_digests", "envelope_sha", "exit_code", "finished_at_epoch", "host_machine_id_digest", "idempotent", "mutation", "operation", "orphans_removed",
		"output_redacted", "package_authority_key_id", "package_manifest_digest", "package_manifest_signature_digest", "plan_digest", "post_observations", "pre_observations",
		"production_ready", "pull_policy", "purge_receipt_sha256", "receipt_authority_key_id", "render_image", "retirement_receipt_sha256", "runtime_deploy", "runtime_inputs",
		"runtime_retirement_digest", "runtime_sha", "schema", "secret_values_emitted", "started_at_epoch", "status", "stderr_bytes", "stderr_sha256", "stdout_bytes",
		"stdout_sha256", "subprocess_timeout_seconds", "transaction_started_at_epoch", "wait_completed", "web_image",
	) {
		return nil, fmt.Errorf("deploy-receipt-payload-shape-invalid")
	}
	bindings := map[string]string{
		"api_host_ip": config.APIHostIP, "authority_digest": config.Digest, "cloudflared_image": config.CloudflaredImage,
		"config_digest": config.Digest, "database_container": databaseControlContainer, "database_image": config.DatabaseImage,
		"deployment_id": config.DeploymentID, "envelope_sha": config.EnvelopeSHA, "host_machine_id_digest": config.HostMachineIDDigest,
		"operation": "deploy-runtime", "package_authority_key_id": config.PackageAuthorityKeyID, "plan_digest": config.PlanDigest,
		"pull_policy": "always", "receipt_authority_key_id": config.ReceiptAuthorityKeyID, "render_image": config.RenderImage,
		"runtime_retirement_digest": config.RuntimeRetirementDigest, "runtime_sha": config.RuntimeSHA, "schema": runtimeDeployReceiptSchema,
		"status": "verified", "web_image": config.WebImage,
	}
	for field, expected := range bindings {
		actual, ok := exactString(payload[field])
		if !ok || actual != expected {
			return nil, fmt.Errorf("deploy-receipt-binding-invalid")
		}
	}
	for field, expected := range map[string]bool{
		"build_performed": false, "idempotent": true, "mutation": true, "orphans_removed": false,
		"output_redacted": true, "production_ready": false, "secret_values_emitted": false, "wait_completed": true,
	} {
		actual, ok := payload[field].(bool)
		if !ok || actual != expected {
			return nil, fmt.Errorf("deploy-receipt-claim-invalid")
		}
	}
	if port, ok := exactInt(payload["api_host_port"], APIHostPort, APIHostPort); !ok || port != config.APIHostPort {
		return nil, fmt.Errorf("deploy-receipt-api-boundary-invalid")
	}
	if port, ok := exactInt(payload["api_container_port"], APIContainerPort, APIContainerPort); !ok || port != config.APIContainerPort {
		return nil, fmt.Errorf("deploy-receipt-api-boundary-invalid")
	}
	if timeout, ok := exactInt(payload["subprocess_timeout_seconds"], 1800, 1800); !ok || timeout != 1800 {
		return nil, fmt.Errorf("deploy-receipt-timeout-invalid")
	}
	if code, ok := exactInt(payload["exit_code"], 0, 0); !ok || code != 0 {
		return nil, fmt.Errorf("deploy-receipt-exit-invalid")
	}
	transactionStarted, transactionOK := exactInt(payload["transaction_started_at_epoch"], config.TransactionStartedAtEpoch, config.TransactionStartedAtEpoch)
	maximumAge, maximumAgeOK := exactInt(payload["backup_max_age_seconds"], BackupMaxAgeSeconds, BackupMaxAgeSeconds)
	started, startedOK := exactInt(payload["started_at_epoch"], 1, 1<<62)
	finished, finishedOK := exactInt(payload["finished_at_epoch"], 1, 1<<62)
	duration, durationOK := exactInt(payload["duration_seconds"], 0, 1800)
	if !transactionOK || transactionStarted != config.TransactionStartedAtEpoch || !maximumAgeOK || maximumAge != config.BackupMaxAgeSeconds || !startedOK || !finishedOK || !durationOK || started < transactionStarted || finished < started || duration != finished-started {
		return nil, fmt.Errorf("deploy-receipt-time-invalid")
	}
	if count, ok := exactInt(payload["argv_count"], int64(len(expectedComposeArgv())), int64(len(expectedComposeArgv()))); !ok || count != int64(len(expectedComposeArgv())) {
		return nil, fmt.Errorf("deploy-receipt-argv-invalid")
	}
	expectedArgvRaw, err := canonicalJSON(expectedComposeArgv())
	if err != nil || payload["argv_sha256"] != digest(expectedArgvRaw) {
		zero(expectedArgvRaw)
		return nil, fmt.Errorf("deploy-receipt-argv-invalid")
	}
	zero(expectedArgvRaw)
	if !canonicalValuesEqual(payload["runtime_deploy"], config.RuntimeDeploy) || !runtimeInputObservationsEqual(payload["runtime_inputs"], config.RuntimeInputs) {
		return nil, fmt.Errorf("deploy-receipt-runtime-contract-invalid")
	}
	if !validDeployEnvironmentDigests(payload["environment_digests"], config.RuntimeInputs) || validateExactCurrentRuntimeInputs(root, config.RuntimeInputs) != nil {
		return nil, fmt.Errorf("deploy-receipt-environment-invalid")
	}
	expectedObservations := map[string]any{
		"compose_files":     config.RuntimeDeploy["compose_files"],
		"compose_plugin":    config.RuntimeDeploy["compose_plugin"],
		"docker_executable": config.RuntimeDeploy["docker_executable"],
	}
	if !canonicalValuesEqual(payload["pre_observations"], expectedObservations) || !canonicalValuesEqual(payload["post_observations"], expectedObservations) || !canonicalValuesEqual(payload["pre_observations"], payload["post_observations"]) {
		return nil, fmt.Errorf("deploy-receipt-observation-invalid")
	}
	if validateCurrentRuntimeFileObservation(root, config.RuntimeDeploy["docker_executable"]) != nil || validateCurrentRuntimeFileObservation(root, config.RuntimeDeploy["compose_plugin"]) != nil {
		return nil, fmt.Errorf("deploy-receipt-runtime-tool-observation-invalid")
	}
	composeFiles, ok := config.RuntimeDeploy["compose_files"].([]any)
	if !ok || len(composeFiles) != 2 {
		return nil, fmt.Errorf("deploy-receipt-compose-observation-invalid")
	}
	for _, observation := range composeFiles {
		if validateCurrentRuntimeFileObservation(root, observation) != nil {
			return nil, fmt.Errorf("deploy-receipt-compose-observation-invalid")
		}
	}
	if err := validateIsolationInstalledBindings(root, config, payload); err != nil {
		return nil, err
	}
	if err := validateDeployDatabaseSubstrate(config, payload); err != nil {
		return nil, err
	}
	for _, prefix := range []string{"stdout", "stderr"} {
		count, ok := exactInt(payload[prefix+"_bytes"], 0, 1<<40)
		if !ok {
			return nil, fmt.Errorf("deploy-receipt-output-invalid")
		}
		digestValue, ok := exactString(payload[prefix+"_sha256"])
		if !ok || !digestPattern.MatchString(digestValue) || (count == 0 && digestValue != digest(nil)) {
			return nil, fmt.Errorf("deploy-receipt-output-invalid")
		}
	}
	return validateDeployPredecessors(root, config, receiptPublic, payload, started)
}

func validDeployEnvironmentDigests(value any, expected []runtimeInputObservation) bool {
	items, ok := value.(map[string]any)
	if !ok || len(items) != len(expected) {
		return false
	}
	for _, observation := range expected {
		if items[observation.path] != observation.digest {
			return false
		}
	}
	return true
}

func validateDeployDatabaseSubstrate(config *Config, payload map[string]any) error {
	if config.DatabaseSubstrate == nil || payload["database_container_id"] != config.DatabaseSubstrate.containerID || payload["database_image_id"] != config.DatabaseSubstrate.imageID || payload["database_repo_digest"] != config.DatabaseSubstrate.repoDigest || !canonicalValuesEqual(payload["database_pgdata_volume"], config.DatabaseSubstrate.pgdataVolume) {
		return fmt.Errorf("deploy-receipt-database-substrate-invalid")
	}
	oid, ok := exactInt(payload["database_oid"], config.DatabaseSubstrate.databaseOID, config.DatabaseSubstrate.databaseOID)
	if !ok || oid != config.DatabaseSubstrate.databaseOID {
		return fmt.Errorf("deploy-receipt-database-substrate-invalid")
	}
	return nil
}

func validateDeployPredecessors(root string, config *Config, receiptPublic ed25519.PublicKey, payload map[string]any, deployStarted int64) (*runtimeDeployProof, error) {
	backup, err := verifyPredeployBackupReceipt(root, config, receiptPublic)
	if err != nil {
		return nil, fmt.Errorf("deploy-receipt-backup-invalid")
	}
	purge, err := verifyRuntimeIsolationReceipt(root, config, receiptPublic, operationPurgeRuntimeIsolation, 1, 1<<62)
	if err != nil {
		return nil, fmt.Errorf("deploy-receipt-purge-invalid")
	}
	retirement, err := verifyRuntimeIsolationReceipt(root, config, receiptPublic, operationRetireStaleRuntime, 1, 1<<62)
	if err != nil {
		return nil, fmt.Errorf("deploy-receipt-retirement-invalid")
	}
	if payload["backup_receipt_sha256"] != backup.receiptDigest || payload["purge_receipt_sha256"] != purge.receiptDigest || payload["retirement_receipt_sha256"] != retirement.receiptDigest || purge.backupReceiptDigest != backup.receiptDigest || retirement.backupReceiptDigest != backup.receiptDigest || retirement.purgeReceiptDigest != purge.receiptDigest || purge.startedAt < backup.finishedAt || retirement.startedAt < purge.finishedAt {
		return nil, fmt.Errorf("deploy-receipt-predecessor-chain-invalid")
	}
	databaseValues, ok := payload["database_receipts"].(map[string]any)
	if !ok || len(databaseValues) != len(databaseOperationContracts()) {
		return nil, fmt.Errorf("deploy-receipt-database-receipts-invalid")
	}
	databaseDigests := make(map[string]string, len(databaseValues))
	previousDigest := retirement.receiptDigest
	previousFinished := retirement.finishedAt
	for _, contract := range databaseOperationContracts() {
		proof, err := verifyDatabaseControlReceipt(root, config, receiptPublic, contract.operation, 1, 1<<62)
		expectedDigest, digestOK := exactString(databaseValues[contract.operation])
		if err != nil || !digestOK || proof.receiptDigest != expectedDigest || proof.backupReceiptDigest != backup.receiptDigest || proof.purgeReceiptDigest != purge.receiptDigest || proof.retirementReceiptDigest != retirement.receiptDigest || proof.predecessorReceiptDigest != previousDigest || proof.startedAt < previousFinished || proof.databaseSubstrate == nil || proof.databaseSubstrate.digest != config.DatabaseSubstrateDigest {
			return nil, fmt.Errorf("deploy-receipt-database-chain-invalid")
		}
		databaseDigests[contract.operation] = expectedDigest
		previousDigest, previousFinished = expectedDigest, proof.finishedAt
	}
	if deployStarted < previousFinished || deployStarted-backup.finishedAt > config.BackupMaxAgeSeconds {
		return nil, fmt.Errorf("deploy-receipt-time-chain-invalid")
	}
	return &runtimeDeployProof{
		backupReceiptDigest: backup.receiptDigest, purgeReceiptDigest: purge.receiptDigest,
		retirementReceiptDigest: retirement.receiptDigest, databaseReceiptDigests: databaseDigests,
		databaseSubstrateDigest: config.DatabaseSubstrateDigest, startedAt: deployStarted,
		finishedAt: func() int64 { value, _ := exactInt(payload["finished_at_epoch"], 1, 1<<62); return value }(),
	}, nil
}

func runtimeDeployProofFields(proof *runtimeDeployProof) map[string]any {
	if proof == nil {
		return nil
	}
	result := map[string]any{
		"runtime_deploy_verified":                  true,
		"runtime_deploy_receipt_digest":            proof.receiptDigest,
		"runtime_deploy_backup_receipt_digest":     proof.backupReceiptDigest,
		"runtime_deploy_purge_receipt_digest":      proof.purgeReceiptDigest,
		"runtime_deploy_retirement_receipt_digest": proof.retirementReceiptDigest,
		"runtime_deploy_database_substrate_digest": proof.databaseSubstrateDigest,
		"runtime_deploy_started_at_epoch":          json.Number(fmt.Sprintf("%d", proof.startedAt)),
		"runtime_deploy_finished_at_epoch":         json.Number(fmt.Sprintf("%d", proof.finishedAt)),
	}
	for operation, digestValue := range proof.databaseReceiptDigests {
		result["runtime_deploy_database_"+operation+"_receipt_digest"] = digestValue
	}
	return result
}
