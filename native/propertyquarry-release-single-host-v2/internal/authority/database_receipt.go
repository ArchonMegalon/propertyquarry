package authority

import (
	"bytes"
	"crypto/ed25519"
	"encoding/json"
	"fmt"
	"path/filepath"
	"regexp"
)

const (
	databaseControlReceiptSchema          = "propertyquarry.database-control-receipt.v2"
	databaseControlReceiptSignatureDomain = "propertyquarry.release-control.single-host-database-receipt-signature.v2\x00"
	databaseControlDatabase               = "propertyquarry"
	databaseControlContainer              = "propertyquarry-db-live"
	databaseControlNetwork                = "property_default"
	databaseRuntimeEnvironmentUID         = 1000
	databaseRuntimeEnvironmentGID         = 1000
	maximumDatabaseReceiptBytes           = 2 * 1024 * 1024
)

type databaseReceiptProof struct {
	operation                string
	receiptDigest            string
	envFileDigest            string
	databaseImageID          string
	databaseRepoDigest       string
	databaseSubstrate        *databaseSubstrate
	backupReceiptDigest      string
	purgeReceiptDigest       string
	retirementReceiptDigest  string
	predecessorReceiptDigest string
	databaseOID              int64
	schemaStatus             string
	schemaVersions           map[string]int64
	receiptKeyID             string
	startedAt                int64
	finishedAt               int64
}

type databaseOperationContract struct {
	stepID          string
	operation       string
	receiptBaseName string
}

var databaseRuntimeEnvironmentNames = []string{
	"PROPERTYQUARRY_API_ADMISSION_DATABASE_URL",
	"PROPERTYQUARRY_API_DATABASE_URL",
	"PROPERTYQUARRY_MIGRATION_DATABASE_URL",
	"PROPERTYQUARRY_PROPERTY_SEARCH_ERASURE_SECRET",
	"PROPERTYQUARRY_RENDER_DATABASE_URL",
	"PROPERTYQUARRY_SCHEDULER_DATABASE_URL",
	"PROPERTYQUARRY_WORKER_DATABASE_URL",
}

var databaseRuntimePasswordPattern = `[A-Za-z0-9_-]{48,128}`

var databaseSchemaComponents = []struct {
	field     string
	component string
}{
	{"kernel", "ea_kernel"},
	{"property_search", "property_search"},
	{"google_identity", "propertyquarry_google_identity"},
}

func databaseOperationContracts() []databaseOperationContract {
	return []databaseOperationContract{
		{ProvisionDatabaseRolesStepID, "provision-roles", "provision-roles.json"},
		{MigrateSchemaStepID, "migrate-schema", "migrate-schema.json"},
		{HardenRuntimeACLStepID, "harden-runtime-acl", "harden-runtime-acl.json"},
		{VerifySchemaReadinessStepID, "verify-schema-readiness", "verify-schema-readiness.json"},
	}
}

func databaseOperationForStep(stepID string) (databaseOperationContract, bool) {
	for _, contract := range databaseOperationContracts() {
		if contract.stepID == stepID {
			return contract, true
		}
	}
	return databaseOperationContract{}, false
}

func verifyDatabaseControlReceipt(root string, config *Config, receiptPublic ed25519.PublicKey, operation string, stepStartedAt, stepFinishedAt int64) (*databaseReceiptProof, error) {
	if config == nil || len(receiptPublic) != ed25519.PublicKeySize {
		return nil, fmt.Errorf("database-receipt-input-invalid")
	}
	var contract *databaseOperationContract
	for _, candidate := range databaseOperationContracts() {
		if candidate.operation == operation {
			copy := candidate
			contract = &copy
			break
		}
	}
	if contract == nil {
		return nil, fmt.Errorf("database-receipt-operation-invalid")
	}
	receiptPath := filepath.Join(DatabaseReceiptDirectory, config.RuntimeSHA, config.DeploymentID, contract.receiptBaseName)
	ownerUID, ownerGID := secureOwner(root)
	raw, err := secureRead(root, receiptPath, 0o600, ownerUID, ownerGID, maximumDatabaseReceiptBytes)
	if err != nil || len(raw) < 2 || raw[len(raw)-1] != '\n' || raw[len(raw)-2] == '\n' {
		zero(raw)
		return nil, fmt.Errorf("database-receipt-unavailable")
	}
	defer zero(raw)
	wrapper, err := strictJSON(raw[:len(raw)-1], maximumDatabaseReceiptBytes)
	if err != nil || !hasKeys(wrapper, "payload", "signature", "signature_key_id") {
		return nil, fmt.Errorf("database-receipt-wrapper-invalid")
	}
	payload, payloadOK := wrapper["payload"].(map[string]any)
	signatureText, signatureOK := exactString(wrapper["signature"])
	signatureKeyID, keyIDOK := exactString(wrapper["signature_key_id"])
	expectedKeyID, keyErr := publicKeyID(receiptPublic)
	signature, signatureErr := signatureBytes(signatureText)
	payloadRaw, canonicalErr := canonicalJSON(payload)
	if !payloadOK || !signatureOK || !keyIDOK || keyErr != nil || signatureKeyID != expectedKeyID || expectedKeyID != config.ReceiptAuthorityKeyID || signatureErr != nil || canonicalErr != nil || len(signature) != ed25519.SignatureSize || !ed25519.Verify(receiptPublic, framed(databaseControlReceiptSignatureDomain, payloadRaw), signature) {
		zero(signature)
		zero(payloadRaw)
		return nil, fmt.Errorf("database-receipt-signature-invalid")
	}
	zero(signature)
	defer zero(payloadRaw)
	proof, err := validateDatabaseReceiptPayload(root, config, operation, payload)
	if err != nil {
		return nil, err
	}
	if stepStartedAt < 1 || stepFinishedAt < stepStartedAt || proof.startedAt < stepStartedAt || proof.finishedAt > stepFinishedAt {
		return nil, fmt.Errorf("database-receipt-step-time-binding-invalid")
	}
	proof.receiptDigest = digest(raw)
	proof.receiptKeyID = expectedKeyID
	return proof, nil
}

func validateDatabaseReceiptPayload(root string, config *Config, operation string, payload map[string]any) (*databaseReceiptProof, error) {
	if !hasKeys(payload,
		"authority_digest", "backup_max_age_seconds", "backup_receipt_sha256", "database", "database_container", "database_image", "database_image_id", "database_repo_digest", "database_substrate_after", "database_substrate_before", "deployment_id", "docker_network", "env_file", "env_file_sha256",
		"finished_at_epoch", "host_machine_id_digest", "operation", "production_ready", "receipt_authority_key_id",
		"predecessor_receipt_sha256", "purge_receipt_sha256", "result", "retirement_receipt_sha256", "runtime_inputs", "runtime_sha", "schema", "secret_values_emitted", "started_at_epoch", "status", "transaction_started_at_epoch", "web_image",
	) {
		return nil, fmt.Errorf("database-receipt-payload-shape-invalid")
	}
	bindings := map[string]string{
		"authority_digest": config.Digest, "database": databaseControlDatabase,
		"database_container": databaseControlContainer, "docker_network": databaseControlNetwork,
		"database_image": config.DatabaseImage,
		"deployment_id":  config.DeploymentID,
		"env_file":       DatabaseRuntimeEnvironmentPath, "host_machine_id_digest": config.HostMachineIDDigest,
		"operation": operation, "receipt_authority_key_id": config.ReceiptAuthorityKeyID,
		"runtime_sha": config.RuntimeSHA, "schema": databaseControlReceiptSchema,
		"status": "verified", "web_image": config.WebImage,
	}
	for field, expected := range bindings {
		actual, ok := exactString(payload[field])
		if !ok || actual != expected {
			return nil, fmt.Errorf("database-receipt-binding-invalid")
		}
	}
	if productionReady, ok := payload["production_ready"].(bool); !ok || productionReady {
		return nil, fmt.Errorf("database-receipt-readiness-claim-invalid")
	}
	if secretValuesEmitted, ok := payload["secret_values_emitted"].(bool); !ok || secretValuesEmitted {
		return nil, fmt.Errorf("database-receipt-secret-claim-invalid")
	}
	started, startedOK := exactInt(payload["started_at_epoch"], 1, 1<<62)
	finished, finishedOK := exactInt(payload["finished_at_epoch"], 1, 1<<62)
	transactionStarted, transactionStartedOK := exactInt(payload["transaction_started_at_epoch"], config.TransactionStartedAtEpoch, config.TransactionStartedAtEpoch)
	maximumAge, maximumAgeOK := exactInt(payload["backup_max_age_seconds"], BackupMaxAgeSeconds, BackupMaxAgeSeconds)
	if !startedOK || !finishedOK || !transactionStartedOK || transactionStarted != config.TransactionStartedAtEpoch || !maximumAgeOK || maximumAge != config.BackupMaxAgeSeconds || started < transactionStarted || finished < started {
		return nil, fmt.Errorf("database-receipt-time-invalid")
	}
	databaseImageID, imageIDOK := exactString(payload["database_image_id"])
	databaseRepoDigest, repoDigestOK := exactString(payload["database_repo_digest"])
	if !imageIDOK || !digestPattern.MatchString(databaseImageID) || !repoDigestOK || databaseRepoDigest != canonicalRepoDigest(config.DatabaseImage) || config.DatabaseSubstrate == nil || databaseImageID != config.DatabaseSubstrate.imageID || databaseRepoDigest != config.DatabaseSubstrate.repoDigest {
		return nil, fmt.Errorf("database-receipt-image-identity-invalid")
	}
	if !runtimeInputObservationsEqual(payload["runtime_inputs"], config.RuntimeInputs) {
		return nil, fmt.Errorf("database-receipt-runtime-inputs-invalid")
	}
	if !databaseSubstrateValueEqual(payload["database_substrate_before"], config.DatabaseSubstrate) || !databaseSubstrateValueEqual(payload["database_substrate_after"], config.DatabaseSubstrate) || !canonicalValuesEqual(payload["database_substrate_before"], payload["database_substrate_after"]) {
		return nil, fmt.Errorf("database-receipt-substrate-invalid")
	}
	backupDigest, backupOK := exactString(payload["backup_receipt_sha256"])
	purgeDigest, purgeOK := exactString(payload["purge_receipt_sha256"])
	retirementDigest, retirementOK := exactString(payload["retirement_receipt_sha256"])
	predecessorDigest, predecessorOK := exactString(payload["predecessor_receipt_sha256"])
	if !backupOK || !digestPattern.MatchString(backupDigest) || !purgeOK || !digestPattern.MatchString(purgeDigest) || !retirementOK || !digestPattern.MatchString(retirementDigest) || !predecessorOK || !digestPattern.MatchString(predecessorDigest) {
		return nil, fmt.Errorf("database-receipt-predecessor-invalid")
	}
	envDigest, ok := exactString(payload["env_file_sha256"])
	if !ok || !digestPattern.MatchString(envDigest) || validateDatabaseRuntimeEnvironment(root, envDigest) != nil {
		return nil, fmt.Errorf("database-receipt-environment-invalid")
	}
	result, ok := payload["result"].(map[string]any)
	if !ok {
		return nil, fmt.Errorf("database-receipt-result-invalid")
	}
	databaseOID, schemaStatus, schemaVersions, err := validateDatabaseReceiptResult(operation, result)
	if err != nil {
		return nil, err
	}
	if databaseOID != config.DatabaseSubstrate.databaseOID {
		return nil, fmt.Errorf("database-receipt-oid-substrate-invalid")
	}
	return &databaseReceiptProof{operation: operation, envFileDigest: envDigest, databaseImageID: databaseImageID, databaseRepoDigest: databaseRepoDigest, databaseSubstrate: config.DatabaseSubstrate, backupReceiptDigest: backupDigest, purgeReceiptDigest: purgeDigest, retirementReceiptDigest: retirementDigest, predecessorReceiptDigest: predecessorDigest, databaseOID: databaseOID, schemaStatus: schemaStatus, schemaVersions: schemaVersions, startedAt: started, finishedAt: finished}, nil
}

func canonicalRepoDigest(reference string) string {
	at := bytes.LastIndexByte([]byte(reference), '@')
	if at < 1 || at == len(reference)-1 {
		return ""
	}
	repository, imageDigest := reference[:at], reference[at+1:]
	slash := bytes.LastIndexByte([]byte(repository), '/')
	leafStart := slash + 1
	if colon := bytes.IndexByte([]byte(repository[leafStart:]), ':'); colon >= 0 {
		repository = repository[:leafStart+colon]
	}
	return repository + "@" + imageDigest
}

func validateDatabaseRuntimeEnvironment(root, expectedDigest string) error {
	if !digestPattern.MatchString(expectedDigest) {
		return fmt.Errorf("database-environment-digest-invalid")
	}
	path := rooted(root, DatabaseRuntimeEnvironmentPath)
	if err := validateExternalParentChain(root, path, databaseRuntimeEnvironmentUID, databaseRuntimeEnvironmentGID); err != nil {
		return fmt.Errorf("database-environment-parent-invalid")
	}
	raw, err := readSecureFile(path, 0o600, databaseRuntimeEnvironmentUID, databaseRuntimeEnvironmentGID, 32*1024)
	if err != nil {
		return fmt.Errorf("database-environment-unavailable")
	}
	defer zero(raw)
	if digest(raw) != expectedDigest || !validDatabaseRuntimeEnvironment(raw) {
		return fmt.Errorf("database-environment-binding-invalid")
	}
	return nil
}

func validDatabaseRuntimeEnvironment(raw []byte) bool {
	if len(raw) < 1 || raw[len(raw)-1] != '\n' || bytes.IndexAny(raw, "\x00\r") >= 0 {
		return false
	}
	lines := bytes.Split(raw[:len(raw)-1], []byte{'\n'})
	if len(lines) != len(databaseRuntimeEnvironmentNames) {
		return false
	}
	values := make(map[string][]byte, len(lines))
	for index, line := range lines {
		parts := bytes.SplitN(line, []byte{'='}, 2)
		if len(parts) != 2 || string(parts[0]) != databaseRuntimeEnvironmentNames[index] || len(parts[1]) < 1 || len(parts[1]) > 2048 {
			return false
		}
		values[string(parts[0])] = parts[1]
	}
	password := databaseRuntimePasswordPattern
	patterns := map[string]string{
		"PROPERTYQUARRY_API_DATABASE_URL":       `^postgresql://propertyquarry_api:` + password + `@propertyquarry-db:5432/propertyquarry$`,
		"PROPERTYQUARRY_SCHEDULER_DATABASE_URL": `^postgresql://propertyquarry_scheduler:` + password + `@propertyquarry-db:5432/propertyquarry$`,
		"PROPERTYQUARRY_WORKER_DATABASE_URL":    `^postgresql://propertyquarry_worker:` + password + `@propertyquarry-db:5432/propertyquarry$`,
		"PROPERTYQUARRY_MIGRATION_DATABASE_URL": `^postgresql://propertyquarry_migrator:` + password + `@propertyquarry-db:5432/propertyquarry\?options=-c%20role%3Dpropertyquarry_owner%20-c%20search_path%3Dpublic%2Cpg_catalog$`,
	}
	for name, pattern := range patterns {
		if !regexp.MustCompile(pattern).Match(values[name]) {
			return false
		}
	}
	admissionPattern := regexp.MustCompile(`^postgresql://propertyquarry_admission_runtime:` + password + `@propertyquarry-db:5432/propertyquarry_admission$`)
	admission := values["PROPERTYQUARRY_API_ADMISSION_DATABASE_URL"]
	if !admissionPattern.Match(admission) || !bytes.Equal(values["PROPERTYQUARRY_RENDER_DATABASE_URL"], admission) || !regexp.MustCompile(`^`+password+`$`).Match(values["PROPERTYQUARRY_PROPERTY_SEARCH_ERASURE_SECRET"]) {
		return false
	}
	return true
}

func validateDatabaseReceiptResult(operation string, result map[string]any) (int64, string, map[string]int64, error) {
	if operation == "provision-roles" {
		if !hasKeys(result, "credential_reused", "database_oid", "roles") {
			return 0, "", nil, fmt.Errorf("database-receipt-provision-result-shape-invalid")
		}
		if _, ok := result["credential_reused"].(bool); !ok {
			return 0, "", nil, fmt.Errorf("database-receipt-provision-result-invalid")
		}
		databaseOID, oidOK := exactInt(result["database_oid"], 1, 1<<62)
		roles := []string{"propertyquarry_owner", "propertyquarry_migrator", "propertyquarry_api", "propertyquarry_worker", "propertyquarry_scheduler"}
		if !oidOK || !exactStringList(result["roles"], roles) {
			return 0, "", nil, fmt.Errorf("database-receipt-provision-result-invalid")
		}
		return databaseOID, "provisioned", nil, nil
	}
	if !hasKeys(result, "credential_reused", "database_oid", "schema") || result["credential_reused"] != true {
		return 0, "", nil, fmt.Errorf("database-receipt-schema-result-shape-invalid")
	}
	databaseOID, oidOK := exactInt(result["database_oid"], 1, 1<<62)
	schema, schemaOK := result["schema"].(map[string]any)
	if !oidOK || !schemaOK {
		return 0, "", nil, fmt.Errorf("database-receipt-schema-result-invalid")
	}
	if operation == "migrate-schema" {
		versions, err := validateDatabaseMigrationResult(schema)
		if err != nil {
			return 0, "", nil, err
		}
		return databaseOID, "migrated", versions, nil
	}
	if operation != "harden-runtime-acl" && operation != "verify-schema-readiness" {
		return 0, "", nil, fmt.Errorf("database-receipt-operation-invalid")
	}
	versions, err := validateDatabaseReadinessResult(schema)
	if err != nil {
		return 0, "", nil, err
	}
	return databaseOID, "ready", versions, nil
}

func validateDatabaseMigrationResult(schema map[string]any) (map[string]int64, error) {
	if !hasKeys(schema, "google_identity", "kernel", "property_search", "status") || schema["status"] != "migrated" {
		return nil, fmt.Errorf("database-receipt-migration-shape-invalid")
	}
	versions := make(map[string]int64, len(databaseSchemaComponents))
	for _, contract := range databaseSchemaComponents {
		value, ok := schema[contract.field].(map[string]any)
		if !ok || !hasKeys(value, "applied_versions", "component", "current_version", "previous_version") || value["component"] != contract.component {
			return nil, fmt.Errorf("database-receipt-migration-component-invalid")
		}
		previous, previousOK := exactInt(value["previous_version"], 0, 1<<31-1)
		current, currentOK := exactInt(value["current_version"], 1, 1<<31-1)
		if !previousOK || !currentOK || current < previous || !validAppliedVersions(value["applied_versions"], previous, current, true) {
			return nil, fmt.Errorf("database-receipt-migration-component-invalid")
		}
		versions[contract.field] = current
	}
	return versions, nil
}

func validateDatabaseReadinessResult(schema map[string]any) (map[string]int64, error) {
	if !hasKeys(schema, "google_identity", "kernel", "property_search", "ready", "status") || schema["ready"] != true || schema["status"] != "ready" {
		return nil, fmt.Errorf("database-receipt-readiness-shape-invalid")
	}
	versions := make(map[string]int64, len(databaseSchemaComponents))
	for _, contract := range databaseSchemaComponents {
		value, ok := schema[contract.field].(map[string]any)
		if !ok || !hasKeys(value, "applied_versions", "component", "current_version", "ready", "reason", "required_version") || value["component"] != contract.component || value["ready"] != true || value["reason"] != "ready" {
			return nil, fmt.Errorf("database-receipt-readiness-component-invalid")
		}
		current, currentOK := exactInt(value["current_version"], 1, 1<<31-1)
		required, requiredOK := exactInt(value["required_version"], 1, 1<<31-1)
		if !currentOK || !requiredOK || current != required || !validAppliedVersions(value["applied_versions"], 0, current, false) {
			return nil, fmt.Errorf("database-receipt-readiness-component-invalid")
		}
		versions[contract.field] = current
	}
	return versions, nil
}

func validAppliedVersions(value any, minimumExclusive, current int64, allowEmptyAtCurrent bool) bool {
	items, ok := value.([]any)
	if !ok {
		return false
	}
	if len(items) == 0 {
		return allowEmptyAtCurrent && minimumExclusive == current
	}
	previous := minimumExclusive
	for _, item := range items {
		version, ok := exactInt(item, minimumExclusive+1, current)
		if !ok || version <= previous {
			return false
		}
		previous = version
	}
	return previous == current
}

func databaseReceiptProofFields(proof *databaseReceiptProof) map[string]any {
	if proof == nil {
		return nil
	}
	prefix := map[string]string{
		"provision-roles":         "database_provision_roles",
		"migrate-schema":          "database_migrate_schema",
		"harden-runtime-acl":      "database_harden_runtime_acl",
		"verify-schema-readiness": "database_verify_schema_readiness",
	}[proof.operation]
	if prefix == "" {
		return nil
	}
	result := map[string]any{
		prefix + "_verified":                 true,
		prefix + "_receipt_digest":           proof.receiptDigest,
		prefix + "_schema_status":            proof.schemaStatus,
		prefix + "_started_at_epoch":         json.Number(fmt.Sprintf("%d", proof.startedAt)),
		prefix + "_finished_at_epoch":        json.Number(fmt.Sprintf("%d", proof.finishedAt)),
		"database_oid":                       json.Number(fmt.Sprintf("%d", proof.databaseOID)),
		"database_image_id":                  proof.databaseImageID,
		"database_repo_digest":               proof.databaseRepoDigest,
		"database_runtime_env_digest":        proof.envFileDigest,
		"database_receipt_authority_key_id":  proof.receiptKeyID,
		"database_backup_receipt_digest":     proof.backupReceiptDigest,
		"database_purge_receipt_digest":      proof.purgeReceiptDigest,
		"database_retirement_receipt_digest": proof.retirementReceiptDigest,
	}
	if proof.databaseSubstrate != nil {
		result["database_container_id"] = proof.databaseSubstrate.containerID
		result["database_substrate_digest"] = proof.databaseSubstrate.digest
	}
	result[prefix+"_predecessor_receipt_digest"] = proof.predecessorReceiptDigest
	for _, component := range databaseSchemaComponents {
		if version, ok := proof.schemaVersions[component.field]; ok {
			result["database_schema_"+component.field+"_version"] = json.Number(fmt.Sprintf("%d", version))
		}
	}
	return result
}

func allDatabaseReceiptsVerified(fields map[string]any) bool {
	for _, field := range []string{
		"database_provision_roles_verified",
		"database_migrate_schema_verified",
		"database_harden_runtime_acl_verified",
		"database_verify_schema_readiness_verified",
	} {
		if fields[field] != true {
			return false
		}
	}
	for _, component := range databaseSchemaComponents {
		if _, ok := exactInt(fields["database_schema_"+component.field+"_version"], 1, 1<<31-1); !ok {
			return false
		}
	}
	imageID, imageIDOK := exactString(fields["database_image_id"])
	repoDigest, repoDigestOK := exactString(fields["database_repo_digest"])
	containerID, containerIDOK := exactString(fields["database_container_id"])
	substrateDigest, substrateDigestOK := exactString(fields["database_substrate_digest"])
	if !imageIDOK || !digestPattern.MatchString(imageID) || !repoDigestOK || repoDigest == "" || !containerIDOK || !runtimeContainerIDPattern.MatchString(containerID) || !substrateDigestOK || !digestPattern.MatchString(substrateDigest) {
		return false
	}
	return true
}
