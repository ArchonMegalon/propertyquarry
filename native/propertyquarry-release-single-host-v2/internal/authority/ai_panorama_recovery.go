//go:build linux && amd64

package authority

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"os"
	"strconv"
)

const (
	aiPanoramaRecoveryClassificationSchema = "propertyquarry.prater-ai-panorama-recovery-classification.v1"
)

type aiPanoramaRecoveryClassificationResult struct {
	Classification               string
	RequestIDSHA256              string
	PermitSHA256                 string
	OperationIDSHA256            string
	OperationTerminalEntrySHA256 string
	TerminalReceiptSHA256        string
	RawSHA256                    string
}

func parseAiPanoramaRecoveryClassificationResult(
	raw []byte,
) (*aiPanoramaRecoveryClassificationResult, error) {
	if len(raw) < 3 || len(raw) > aiPanoramaMaximumCommandOutput ||
		raw[len(raw)-1] != '\n' || raw[len(raw)-2] == '\n' ||
		bytes.IndexByte(raw[:len(raw)-1], '\n') >= 0 {
		return nil, fmt.Errorf("ai-panorama-recovery-result-framing-invalid")
	}
	value, err := strictJSON(raw[:len(raw)-1], aiPanoramaMaximumCommandOutput)
	if err != nil || !hasKeys(
		value, "schema", "version", "authority", "status", "classification",
		"request_id_sha256", "permit_sha256", "operation_id_sha256",
		"operation_terminal_entry_sha256", "terminal_receipt_sha256",
		"database_mutation_performed", "public_target_mutation_performed",
		"retry_authorized", "private_values_redacted",
	) || value["schema"] != aiPanoramaRecoveryClassificationSchema ||
		value["version"] != json.Number("1") ||
		value["authority"] != "propertyquarry-release-control" ||
		value["status"] != "classified" ||
		value["database_mutation_performed"] != false ||
		value["public_target_mutation_performed"] != false ||
		value["retry_authorized"] != false ||
		value["private_values_redacted"] != true {
		return nil, fmt.Errorf("ai-panorama-recovery-result-invalid")
	}
	classification, classificationOK := exactString(value["classification"])
	requestSHA256, requestOK := exactString(value["request_id_sha256"])
	permitSHA256, permitOK := exactString(value["permit_sha256"])
	operationID, operationOK := exactString(value["operation_id_sha256"])
	terminalEntry, terminalEntryOK := exactString(
		value["operation_terminal_entry_sha256"],
	)
	terminalReceipt, terminalReceiptOK := exactString(
		value["terminal_receipt_sha256"],
	)
	if !classificationOK ||
		(classification != "committed" &&
			classification != "failed-clean" &&
			classification != "rolled-back" &&
			classification != "recovery-required") ||
		!requestOK || !permitOK || !operationOK || !terminalEntryOK ||
		!terminalReceiptOK {
		return nil, fmt.Errorf("ai-panorama-recovery-result-invalid")
	}
	for _, value := range []string{
		requestSHA256, permitSHA256, operationID, terminalEntry, terminalReceipt,
	} {
		if !aiPanoramaRawSHA256Pattern.MatchString(value) {
			return nil, fmt.Errorf("ai-panorama-recovery-result-invalid")
		}
	}
	return &aiPanoramaRecoveryClassificationResult{
		Classification: classification, RequestIDSHA256: requestSHA256,
		PermitSHA256: permitSHA256, OperationIDSHA256: operationID,
		OperationTerminalEntrySHA256: terminalEntry,
		TerminalReceiptSHA256:        terminalReceipt,
		RawSHA256:                    aiPanoramaRawSHA256(raw),
	}, nil
}

func readAiPanoramaRecoveryClassificationTerminal(
	root string,
	requestID string,
	runtime *aiPanoramaRuntimeObservation,
	result *aiPanoramaRecoveryClassificationResult,
) (string, error) {
	if root == "" {
		root = "/"
	}
	path, err := aiPanoramaTerminalPath(requestID)
	requestSHA256, requestErr := aiPanoramaRequestIDHash(requestID)
	if err != nil || requestErr != nil || runtime == nil || result == nil ||
		requestSHA256 != result.RequestIDSHA256 {
		return "", fmt.Errorf("ai-panorama-recovery-terminal-input-invalid")
	}
	uid, gid := secureOwner(root)
	raw, err := secureRead(
		root, path, 0o600, uid, gid, aiPanoramaMaximumTerminalBytes,
	)
	if err != nil || len(raw) < 3 || raw[len(raw)-1] != '\n' ||
		raw[len(raw)-2] == '\n' || bytes.IndexByte(raw[:len(raw)-1], '\n') >= 0 ||
		aiPanoramaRawSHA256(raw) != result.TerminalReceiptSHA256 {
		zero(raw)
		return "", fmt.Errorf("ai-panorama-recovery-terminal-unavailable")
	}
	defer zero(raw)
	value, err := strictJSON(raw[:len(raw)-1], aiPanoramaMaximumTerminalBytes)
	if err != nil || !hasKeys(
		value, "schema", "version", "authority", "status",
		"request_id_sha256", "permit_sha256", "operation_id_sha256",
		"operation_terminal_entry_sha256",
		"operation_terminal_evidence_sha256",
		"database_mutation_performed", "public_target_mutation_performed",
		"private_values_redacted",
	) || value["schema"] != aiPanoramaTerminalSchema ||
		value["version"] != json.Number("1") ||
		value["authority"] != "propertyquarry-release-control" ||
		value["status"] != result.Classification ||
		value["request_id_sha256"] != result.RequestIDSHA256 ||
		value["permit_sha256"] != result.PermitSHA256 ||
		value["operation_id_sha256"] != result.OperationIDSHA256 ||
		value["operation_terminal_entry_sha256"] !=
			result.OperationTerminalEntrySHA256 ||
		value["database_mutation_performed"] != false ||
		value["public_target_mutation_performed"] != false ||
		value["private_values_redacted"] != true {
		return "", fmt.Errorf("ai-panorama-recovery-terminal-invalid")
	}
	evidenceSHA256, evidenceOK := exactString(
		value["operation_terminal_evidence_sha256"],
	)
	if !evidenceOK || !aiPanoramaRawSHA256Pattern.MatchString(evidenceSHA256) {
		return "", fmt.Errorf("ai-panorama-recovery-terminal-invalid")
	}
	controlInfo, err := os.Lstat(rooted(root, aiPanoramaControlRoot))
	controlMetadata, controlOK := infoSys(controlInfo)
	if err != nil || !controlOK || !controlInfo.IsDir() ||
		controlInfo.Mode().Perm() != 0o700 ||
		controlInfo.Mode()&os.ModeSymlink != 0 ||
		uint64(controlMetadata.Dev) != runtime.ControlRootDevice ||
		controlMetadata.Ino != runtime.ControlRootInode {
		return "", fmt.Errorf("ai-panorama-recovery-terminal-control-root-invalid")
	}
	return evidenceSHA256, nil
}

func runAiPanoramaHistoricalRecoveryDatabasePhase(
	parent context.Context,
	root string,
	config *Config,
	runtime *aiPanoramaRuntimeObservation,
	sealed *aiPanoramaSealedArtifactObservation,
	environmentDigest string,
	archive *aiPanoramaContextArchive,
) ([]byte, error) {
	secretPath, err := createAiPanoramaDatabaseSecret(
		root, environmentDigest, aiPanoramaDatabaseURLName,
	)
	if err != nil {
		return nil, err
	}
	secretPresent := true
	defer func() {
		if secretPresent {
			_ = destroyAiPanoramaDatabaseSecret(root)
		}
	}()
	network, networkErr := ensureAiPanoramaNetwork(parent, config, runtime)
	if networkErr != nil {
		if destroyAiPanoramaDatabaseSecret(root) == nil {
			secretPresent = false
		}
		_ = cleanupAiPanoramaNetwork(
			context.WithoutCancel(parent), config, runtime,
		)
		return nil, networkErr
	}
	raw, phaseErr := runAiPanoramaHistoricalRecoveryContainerRaw(
		parent, config, runtime, sealed, network, secretPath, archive,
	)
	cleanupContext, cancel := context.WithTimeout(
		context.WithoutCancel(parent), aiPanoramaCleanupTimeout,
	)
	defer cancel()
	networkCleanupErr := cleanupAiPanoramaNetwork(
		cleanupContext, config, runtime,
	)
	secretCleanupErr := destroyAiPanoramaDatabaseSecret(root)
	if secretCleanupErr == nil {
		secretPresent = false
	}
	if phaseErr != nil || networkCleanupErr != nil || secretCleanupErr != nil {
		zero(raw)
		if networkCleanupErr != nil || secretCleanupErr != nil {
			return nil, fmt.Errorf("ai-panorama-recovery-phase-cleanup-unverified")
		}
		return nil, phaseErr
	}
	return raw, nil
}

func cleanupAiPanoramaHistoricalRecoveryResidue(
	parent context.Context,
	root string,
	config *Config,
	runtime *aiPanoramaRuntimeObservation,
	archive *aiPanoramaContextArchive,
) error {
	if parent == nil || config == nil || runtime == nil || archive == nil {
		return fmt.Errorf("ai-panorama-recovery-residue-input-invalid")
	}
	ctx, cancel := context.WithTimeout(
		context.WithoutCancel(parent), aiPanoramaCleanupTimeout,
	)
	defer cancel()
	networkName, err := aiPanoramaNetworkName(config)
	if err != nil {
		return err
	}
	networkExists, err := aiPanoramaNetworkExists(ctx, networkName)
	if err != nil {
		return err
	}
	containerExists, err := aiPanoramaPhaseContainerExists(
		ctx, config, "recovery",
	)
	if err != nil {
		return err
	}
	if containerExists {
		if !networkExists {
			return fmt.Errorf("ai-panorama-recovery-residue-network-missing")
		}
		if err := cleanupAiPanoramaRecoveryPhaseContainer(
			ctx, config, runtime, "recovery", networkName, archive,
		); err != nil {
			return err
		}
	}
	return cleanupAiPanoramaInterruptedAttempt(
		parent, root, config, runtime, true,
	)
}

func aiPanoramaRecoveryFieldsForTerminal(
	base map[string]any,
	result *aiPanoramaRecoveryClassificationResult,
	evidenceSHA256 string,
) {
	base["ai_panorama_recovery_classification"] = map[string]any{
		"schema":                             aiPanoramaRecoveryClassificationSchema,
		"version":                            json.Number("1"),
		"classification":                     result.Classification,
		"request_id_sha256":                  result.RequestIDSHA256,
		"permit_sha256":                      result.PermitSHA256,
		"operation_id_sha256":                result.OperationIDSHA256,
		"operation_terminal_entry_sha256":    result.OperationTerminalEntrySHA256,
		"operation_terminal_evidence_sha256": evidenceSHA256,
		"terminal_receipt_sha256":            result.TerminalReceiptSHA256,
		"classifier_result_sha256":           result.RawSHA256,
		"database_mutation_performed":        false,
		"public_target_mutation_performed":   false,
		"retry_authorized":                   false,
	}
	base["ai_panorama_recovery_classified_at"] = json.Number(
		strconv.FormatInt(authorityNow().UTC().Unix(), 10),
	)
}

func aiPanoramaRecoveryDatabaseEnvironmentDigest(
	journal *Journal,
	config *Config,
	base map[string]any,
) (string, error) {
	releaseReceipt, receiptOK := exactString(
		base["release_run_receipt_digest"],
	)
	if journal == nil || config == nil || !receiptOK ||
		!digestPattern.MatchString(releaseReceipt) {
		return "", fmt.Errorf("ai-panorama-recovery-release-binding-invalid")
	}
	var result string
	for index := range journal.events {
		event := &journal.events[index]
		if event.EventType != "run-succeeded" ||
			event.ReceiptDigest != releaseReceipt {
			continue
		}
		environmentDigest, digestOK := exactString(
			event.Payload["database_runtime_env_digest"],
		)
		if result != "" || !digestOK ||
			!digestPattern.MatchString(environmentDigest) ||
			event.Payload["config_digest"] != config.Digest ||
			event.Payload["plan_digest"] != config.PlanDigest ||
			event.Payload["runtime_sha"] != config.RuntimeSHA ||
			event.Payload["workflow_sha"] != config.WorkflowSHA ||
			event.Payload["deployment_id"] != config.DeploymentID ||
			event.Payload["production_ready"] != true ||
			event.Payload["runtime_deploy_verified"] != true {
			return "", fmt.Errorf("ai-panorama-recovery-release-binding-invalid")
		}
		result = environmentDigest
	}
	if result == "" {
		return "", fmt.Errorf("ai-panorama-recovery-release-binding-missing")
	}
	return result, nil
}

func aiPanoramaRecoveryProjection(
	root string,
	base map[string]any,
) (*aiPanoramaProjection, error) {
	raw, exists := base["ai_panorama_release_projection"]
	if !exists {
		return nil, fmt.Errorf("ai-panorama-recovery-release-projection-missing")
	}
	projection, err := parseAiPanoramaProjection(raw)
	if err != nil || projection.Kind != "release-request" ||
		projection.Path != aiPanoramaReleaseRequestPath ||
		projection.Mode != 0o600 {
		if projection != nil {
			projection.release()
		}
		return nil, fmt.Errorf("ai-panorama-recovery-release-projection-invalid")
	}
	if err := persistAiPanoramaFixedProjection(root, projection); err != nil {
		projection.release()
		return nil, err
	}
	return projection, nil
}

func aiPanoramaRecoveryStateExpectation(
	permit map[string]any,
	base map[string]any,
	runtime *aiPanoramaRuntimeObservation,
	archive *aiPanoramaContextArchive,
	classification string,
) (*aiPanoramaAdvancedStateExpectation, error) {
	if permit == nil || base == nil || runtime == nil || archive == nil ||
		(classification != "committed" &&
			classification != "failed-clean" &&
			classification != "rolled-back") {
		return nil, fmt.Errorf("ai-panorama-recovery-state-input-invalid")
	}
	requestID, requestOK := exactString(permit["request_id"])
	nonce, nonceOK := exactString(permit["nonce"])
	issuedAt, issuedOK := parseAiPanoramaTimestamp(permit["issued_at"])
	expiresAt, expiresOK := parseAiPanoramaTimestamp(permit["expires_at"])
	lease, leaseOK := exactInt(permit["execution_lease_seconds"], 1, 900)
	keyID, keyIDOK := exactString(permit["key_id"])
	keyEpoch, keyEpochOK := exactInt(permit["key_epoch"], 1, 1<<62)
	keySHA256, keySHAOK := exactString(permit["key_sha256"])
	keyringSHA256, keyringSHAOK := exactString(permit["keyring_sha256"])
	volumeProfileSHA256, volumeOK := exactString(
		permit["volume_profile_sha256"],
	)
	publicationSHA256, publicationOK := exactString(
		permit["expected_publication_record_sha256"],
	)
	permitEvidence, evidenceOK := base["ai_panorama_permit"].(map[string]any)
	permitSHA256, permitSHAOK := exactString(permitEvidence["sha256"])
	preimageSHA256, preimageOK := exactString(
		permitEvidence["preimage_sha256"],
	)
	contextRaw, contextErr := canonicalJSON(permit)
	if !requestOK || requestID != archive.RequestID ||
		!nonceOK || !aiPanoramaNoncePattern.MatchString(nonce) ||
		!issuedOK || !expiresOK || !expiresAt.After(issuedAt) ||
		!leaseOK || !keyIDOK || keyID != archive.KeyID ||
		!keyEpochOK || keyEpoch != archive.KeyEpoch ||
		!keySHAOK || keySHA256 != archive.KeySHA256 ||
		!keyringSHAOK || keyringSHA256 != archive.KeyringSHA256 ||
		!volumeOK || !aiPanoramaRawSHA256Pattern.MatchString(volumeProfileSHA256) ||
		!publicationOK || !aiPanoramaRawSHA256Pattern.MatchString(publicationSHA256) ||
		!evidenceOK || !permitSHAOK || permitSHA256 != archive.PermitSHA256 ||
		!preimageOK || !aiPanoramaRawSHA256Pattern.MatchString(preimageSHA256) ||
		contextErr != nil {
		zero(contextRaw)
		return nil, fmt.Errorf("ai-panorama-recovery-state-binding-invalid")
	}
	trustSHA256 := ""
	for index := range archive.Files {
		if archive.Files[index].Kind == "trust-assertion" {
			trustSHA256 = archive.Files[index].SHA256
		}
	}
	if !aiPanoramaRawSHA256Pattern.MatchString(trustSHA256) {
		zero(contextRaw)
		return nil, fmt.Errorf("ai-panorama-recovery-state-binding-invalid")
	}
	expectation := &aiPanoramaAdvancedStateExpectation{
		PermitSHA256: archive.PermitSHA256,
		RequestIDSHA256: aiPanoramaRawSHA256(
			[]byte(requestID),
		),
		NonceSHA256:          aiPanoramaRawSHA256([]byte(nonce)),
		ContextSHA256:        aiPanoramaRawSHA256(contextRaw),
		SignedPreimageSHA256: preimageSHA256,
		TrustAssertionSHA256: trustSHA256,
		KeyID:                keyID, KeyEpoch: keyEpoch, KeySHA256: keySHA256,
		KeyringSHA256:           keyringSHA256,
		VolumeProfileSHA256:     volumeProfileSHA256,
		PublicationRecordSHA256: publicationSHA256,
		PublicVolumeDevice:      runtime.PublicVolumeDevice,
		PublicVolumeInode:       runtime.PublicVolumeInode,
		TerminalEvent:           classification,
		IssuedAt:                issuedAt,
		ExpiresAt:               expiresAt,
		ExecutionLease:          lease,
	}
	zero(contextRaw)
	return expectation, nil
}

func aiPanoramaRecoveryCommittedBindingCandidate(
	root string,
	genesis *aiPanoramaStateGenesis,
	expected *aiPanoramaAdvancedStateExpectation,
) error {
	if genesis == nil || expected == nil ||
		expected.TerminalEvent != "committed" {
		return fmt.Errorf("ai-panorama-recovery-binding-input-invalid")
	}
	value, raw, _, err := readAiPanoramaCanonicalStateObject(
		root, genesis.Root, aiPanoramaOperationPath,
		aiPanoramaMaximumOperationBytes,
	)
	if err != nil {
		return err
	}
	defer zero(raw)
	entries, ok := value["entries"].([]any)
	expectedOperationID, err := aiPanoramaOperationID(
		expected.PermitSHA256, expected.RequestIDSHA256,
		expected.NonceSHA256, expected.ContextSHA256,
	)
	if !ok || err != nil {
		return fmt.Errorf("ai-panorama-recovery-binding-journal-invalid")
	}
	matched := false
	for _, rawEntry := range entries {
		entry, entryOK := rawEntry.(map[string]any)
		if !entryOK || entry["operation_id"] != expectedOperationID ||
			entry["event"] != "committed" {
			continue
		}
		evidence, evidenceOK := entry["evidence"].(map[string]any)
		install, installOK := evidence["install"].(map[string]any)
		status, statusOK := exactString(install["publication_binding_status"])
		before, beforeOK := exactString(
			install["publication_binding_before_sha256"],
		)
		after, afterOK := exactString(
			install["publication_binding_after_sha256"],
		)
		if matched || !evidenceOK || !installOK || !statusOK ||
			(status != "applied" && status != "already_bound") ||
			!beforeOK || !afterOK ||
			!aiPanoramaRawSHA256Pattern.MatchString(before) ||
			!aiPanoramaRawSHA256Pattern.MatchString(after) {
			return fmt.Errorf("ai-panorama-recovery-binding-journal-invalid")
		}
		expected.BindingStatus = status
		expected.BindingBeforeSHA256 = before
		expected.BindingAfterSHA256 = after
		matched = true
	}
	if !matched {
		return fmt.Errorf("ai-panorama-recovery-binding-terminal-missing")
	}
	return nil
}

func aiPanoramaRecoveryTerminalValue(
	result *aiPanoramaRecoveryClassificationResult,
	evidenceSHA256 string,
) map[string]any {
	return map[string]any{
		"status":                             result.Classification,
		"request_id_sha256":                  result.RequestIDSHA256,
		"permit_sha256":                      result.PermitSHA256,
		"terminal_receipt_sha256":            result.TerminalReceiptSHA256,
		"operation_id_sha256":                result.OperationIDSHA256,
		"operation_terminal_entry_sha256":    result.OperationTerminalEntrySHA256,
		"operation_terminal_evidence_sha256": evidenceSHA256,
	}
}

func recoverAiPanoramaMutationAttempt(
	parent context.Context,
	root string,
	journal *Journal,
	config *Config,
	last *JournalEvent,
) error {
	base := aiPanoramaRecoveryFields(last.Payload)
	if err := recoverAiPanoramaPermitPersistence(
		root, journal, last, base,
	); err != nil {
		return aiPanoramaRecordRecoveryRequired(
			journal, base, "mutation-recovery-permit-or-archive-invalid",
		)
	}
	archive, err := parseAiPanoramaContextArchive(base)
	if err != nil {
		return aiPanoramaRecordRecoveryRequired(
			journal, base, "mutation-recovery-archive-invalid",
		)
	}
	defer archive.release()
	if err := validateAiPanoramaContextArchiveInventory(root, journal); err != nil {
		return aiPanoramaRecordRecoveryRequired(
			journal, base, "mutation-recovery-archive-inventory-invalid",
		)
	}
	runtime, err := observeAiPanoramaRuntime(parent, root, config)
	storedRuntime, storedRuntimeOK :=
		base["ai_panorama_runtime_observation"].(map[string]any)
	if err != nil || !storedRuntimeOK ||
		!canonicalValuesEqual(
			storedRuntime, aiPanoramaRuntimeObservationValue(runtime),
		) {
		return aiPanoramaRecordRecoveryRequired(
			journal, base, "mutation-recovery-runtime-ambiguous",
		)
	}
	sealed, err := validateAiPanoramaSealedArtifact()
	storedSealed, storedSealedOK :=
		base["ai_panorama_sealed_artifact"].(map[string]any)
	if err != nil || !storedSealedOK ||
		!canonicalValuesEqual(
			storedSealed, aiPanoramaSealedArtifactValue(sealed),
		) {
		return aiPanoramaRecordRecoveryRequired(
			journal, base, "mutation-recovery-sealed-artifact-ambiguous",
		)
	}
	releaseProjection, err := aiPanoramaRecoveryProjection(root, base)
	if err != nil {
		return aiPanoramaRecordRecoveryRequired(
			journal, base, "mutation-recovery-release-projection-invalid",
		)
	}
	defer releaseProjection.release()
	environmentDigest, err := aiPanoramaRecoveryDatabaseEnvironmentDigest(
		journal, config, base,
	)
	if err != nil {
		return aiPanoramaRecordRecoveryRequired(
			journal, base, "mutation-recovery-database-binding-invalid",
		)
	}
	if err := cleanupAiPanoramaHistoricalRecoveryResidue(
		parent, root, config, runtime, archive,
	); err != nil {
		return aiPanoramaRecordRecoveryRequired(
			journal, base, "mutation-recovery-residue-ambiguous",
		)
	}
	if last.EventType != aiPanoramaInstallRecoveryStartedEvent {
		started := cloneFields(base)
		started["disposition"] = "historical-classification-started"
		started["recovery"] = true
		started["production_ready"] = false
		started["retry_authorized"] = false
		if err := appendAiPanoramaJournalEvent(
			journal, aiPanoramaInstallRecoveryStartedEvent, started,
		); err != nil {
			return err
		}
		last = &journal.events[len(journal.events)-1]
		base = aiPanoramaRecoveryFields(last.Payload)
	}
	raw, err := runAiPanoramaHistoricalRecoveryDatabasePhase(
		parent, root, config, runtime, sealed,
		environmentDigest, archive,
	)
	if err != nil {
		return aiPanoramaRecordRecoveryRequired(
			journal, base, "historical-classifier-execution-failed",
		)
	}
	result, parseErr := parseAiPanoramaRecoveryClassificationResult(raw)
	zero(raw)
	if parseErr != nil ||
		result.RequestIDSHA256 != archive.RequestIDSHA256 ||
		result.PermitSHA256 != archive.PermitSHA256 {
		return aiPanoramaRecordRecoveryRequired(
			journal, base, "historical-classifier-result-invalid",
		)
	}
	evidenceSHA256, err := readAiPanoramaRecoveryClassificationTerminal(
		root, archive.RequestID, runtime, result,
	)
	if err != nil {
		return aiPanoramaRecordRecoveryRequired(
			journal, base, "historical-classifier-terminal-invalid",
		)
	}
	aiPanoramaRecoveryFieldsForTerminal(base, result, evidenceSHA256)
	base["ai_panorama_terminal"] =
		aiPanoramaRecoveryTerminalValue(result, evidenceSHA256)
	base["recovery"] = true
	base["retry_authorized"] = false
	if result.Classification == "recovery-required" {
		return aiPanoramaRecordRecoveryRequired(
			journal, base, "historical-classifier-recovery-required",
		)
	}
	permit, permitRaw, err := readAiPanoramaPersistedPermit(
		root, archive.RequestID, archive.PermitSHA256,
	)
	if err != nil {
		zero(permitRaw)
		return aiPanoramaRecordRecoveryRequired(
			journal, base, "historical-classifier-permit-invalid",
		)
	}
	zero(permitRaw)
	expectation, err := aiPanoramaRecoveryStateExpectation(
		permit, base, runtime, archive, result.Classification,
	)
	if err != nil {
		return aiPanoramaRecordRecoveryRequired(
			journal, base, "historical-classifier-state-binding-invalid",
		)
	}
	genesis, completed, err := aiPanoramaStateGenesisFromEvent(journal)
	if genesis != nil {
		defer genesis.release()
	}
	if err != nil || genesis == nil || !completed {
		return aiPanoramaRecordRecoveryRequired(
			journal, base, "historical-classifier-genesis-invalid",
		)
	}
	if result.Classification == "committed" &&
		aiPanoramaRecoveryCommittedBindingCandidate(
			root, genesis, expectation,
		) != nil {
		return aiPanoramaRecordRecoveryRequired(
			journal, base, "historical-classifier-publication-binding-invalid",
		)
	}
	stateProfile, err := observeAiPanoramaAdvancedState(
		root, genesis, expectation,
	)
	if err != nil ||
		stateProfile.Operation.OperationID != result.OperationIDSHA256 ||
		stateProfile.Operation.TerminalEntrySHA256 !=
			result.OperationTerminalEntrySHA256 ||
		stateProfile.Operation.TerminalEvent != result.Classification {
		return aiPanoramaRecordRecoveryRequired(
			journal, base, "historical-classifier-advanced-state-invalid",
		)
	}
	after, err := snapshotAiPanoramaRelated(runtime.PublicVolumeMountpoint)
	beforeDigest, beforeOK := exactString(
		base["ai_panorama_before_manifest_sha256"],
	)
	if err != nil || after == nil || !beforeOK {
		return aiPanoramaRecordRecoveryRequired(
			journal, base, "historical-classifier-public-state-unavailable",
		)
	}
	if result.Classification == "committed" {
		if !aiPanoramaPublishedManifestValid(after) {
			return aiPanoramaRecordRecoveryRequired(
				journal, base, "historical-classifier-publication-invalid",
			)
		}
	} else if after.Digest != beforeDigest || len(after.Entries) != 0 {
		return aiPanoramaRecordRecoveryRequired(
			journal, base, "historical-classifier-clean-state-invalid",
		)
	}
	base["ai_panorama_advanced_state_profile"] = stateProfile.journalValue()
	base["ai_panorama_after_manifest"] = aiPanoramaManifestValue(after)
	base["ai_panorama_after_manifest_sha256"] = after.Digest
	base["completed_at"] = json.Number(
		strconv.FormatInt(authorityNow().UTC().Unix(), 10),
	)
	base["production_ready"] = result.Classification == "committed"
	base["rollback_performed"] = result.Classification == "rolled-back"
	base["release_effects_performed"] = true
	base["disposition"] = "historically-classified-" + result.Classification
	terminalEvent := aiPanoramaInstallFailedNoEffectsEvent
	if result.Classification == "committed" {
		base["ai_panorama_install_verified"] = true
		terminalEvent = aiPanoramaInstallSucceededEvent
	} else if result.Classification == "rolled-back" {
		terminalEvent = aiPanoramaInstallRolledBackEvent
	}
	if err := recordAiPanoramaAttemptProjectionsCleaned(
		root, journal, base, terminalEvent,
	); err != nil {
		return aiPanoramaRecordRecoveryRequired(
			journal, base, "historical-classifier-projection-cleanup-ambiguous",
		)
	}
	wire, err := journal.Append(terminalEvent, base)
	zero(wire)
	return err
}
