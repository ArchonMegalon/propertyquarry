//go:build linux && amd64

package authority

import (
	"context"
	"crypto/ed25519"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"strconv"
	"time"
)

const (
	aiPanoramaInstallVolumeInitializedEvent = "ai-panorama-install-volume-initialized"
	aiPanoramaInstallBootstrapPreparedEvent = "ai-panorama-install-volume-bootstrap-prepared"
	aiPanoramaDatabaseURLName               = "PROPERTYQUARRY_SCHEDULER_DATABASE_URL"
)

type aiPanoramaReleaseProof struct {
	ReceiptDigest             string
	DatabaseEnvironmentDigest string
}

func aiPanoramaReleasePrerequisiteV2(
	journal *Journal,
	config *Config,
	request *workflowRequest,
	identity *Identity,
) (*aiPanoramaReleaseProof, error) {
	if journal == nil || config == nil || request == nil || identity == nil {
		return nil, fmt.Errorf("ai-panorama-release-prerequisite-input-invalid")
	}
	for index := len(journal.events) - 1; index >= 0; index-- {
		event := &journal.events[index]
		if event.EventType != "run-succeeded" {
			continue
		}
		if event.Payload["config_digest"] != config.Digest {
			continue
		}
		if event.Payload["plan_digest"] != config.PlanDigest ||
			event.Payload["runtime_sha"] != config.RuntimeSHA ||
			event.Payload["workflow_sha"] != config.WorkflowSHA ||
			event.Payload["deployment_id"] != config.DeploymentID ||
			event.Payload["web_image"] != config.WebImage ||
			event.Payload["production_ready"] != true ||
			event.Payload["runtime_deploy_verified"] != true ||
			!digestPattern.MatchString(event.ReceiptDigest) {
			return nil, fmt.Errorf("ai-panorama-release-prerequisite-mismatch")
		}
		environmentDigest, ok := exactString(event.Payload["database_runtime_env_digest"])
		if !ok || !digestPattern.MatchString(environmentDigest) {
			return nil, fmt.Errorf("ai-panorama-release-prerequisite-database-binding-invalid")
		}
		return &aiPanoramaReleaseProof{
			ReceiptDigest: event.ReceiptDigest, DatabaseEnvironmentDigest: environmentDigest,
		}, nil
	}
	return nil, fmt.Errorf("ai-panorama-release-prerequisite-missing")
}

func appendAiPanoramaJournalEvent(journal *Journal, eventType string, fields map[string]any) error {
	wire, err := journal.Append(eventType, fields)
	zero(wire)
	return err
}

func runAiPanoramaDatabaseContainerPhase(
	parent context.Context,
	root string,
	config *Config,
	runtime *aiPanoramaRuntimeObservation,
	sealed *aiPanoramaSealedArtifactObservation,
	environmentDigest string,
	phase string,
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
		_ = cleanupAiPanoramaNetwork(context.WithoutCancel(parent), config, runtime)
		return nil, networkErr
	}
	raw, phaseErr := runAiPanoramaContainerRaw(
		parent, config, runtime, sealed, network, phase, secretPath,
	)
	cleanupContext, cancel := context.WithTimeout(
		context.WithoutCancel(parent), aiPanoramaCleanupTimeout,
	)
	defer cancel()
	networkCleanupErr := cleanupAiPanoramaNetwork(cleanupContext, config, runtime)
	secretCleanupErr := destroyAiPanoramaDatabaseSecret(root)
	if secretCleanupErr == nil {
		secretPresent = false
	}
	if phaseErr != nil || networkCleanupErr != nil || secretCleanupErr != nil {
		zero(raw)
		if networkCleanupErr != nil || secretCleanupErr != nil {
			return nil, fmt.Errorf("ai-panorama-database-phase-cleanup-unverified")
		}
		return nil, phaseErr
	}
	return raw, nil
}

func aiPanoramaPermitEvidence(value *aiPanoramaSignedPermit) map[string]any {
	if value == nil {
		return nil
	}
	return map[string]any{
		"request_id": value.RequestID, "permit_relpath": value.Relpath,
		"path": value.Path, "sha256": value.SHA256,
		"preimage_sha256": value.PreimageSHA256,
		"key_id":          value.KeyID, "key_epoch": json.Number(strconv.FormatInt(value.KeyEpoch, 10)),
		"key_sha256": value.KeySHA256, "keyring_sha256": value.KeyringSHA256,
		"issued_at": value.IssuedAt, "expires_at": value.ExpiresAt,
		"execution_lease_seconds": json.Number(strconv.FormatInt(value.ExecutionLease, 10)),
	}
}

func executeAiPanoramaInstallV2(
	parent context.Context,
	root string,
	journal *Journal,
	config *Config,
	request *workflowRequest,
	identity *Identity,
	private ed25519.PrivateKey,
) ([]byte, error) {
	if parent == nil || root != "/" || journal == nil || config == nil ||
		request == nil || identity == nil || len(private) != ed25519.PrivateKeySize {
		return nil, fmt.Errorf("ai-panorama-install-input-invalid")
	}
	release, err := aiPanoramaReleasePrerequisiteV2(journal, config, request, identity)
	if err != nil {
		return nil, err
	}
	runtime, err := observeAiPanoramaRuntime(parent, root, config)
	if err != nil {
		return nil, err
	}
	var bootstrap *aiPanoramaBootstrapResult
	if runtime.PublicVolumeNeedsInitialization {
		prepared := authorityFields(config, request, identity)
		prepared["release_run_receipt_digest"] = release.ReceiptDigest
		prepared["ai_panorama_bootstrap_before"] = aiPanoramaRuntimeObservationValue(runtime)
		prepared["ai_panorama_bootstrap_entrypoint"] = aiPanoramaBootstrapEntrypoint
		prepared["ai_panorama_bootstrap_public_volume_name"] = aiPanoramaPublicVolumeName
		prepared["ai_panorama_bootstrap_public_mount_target"] = aiPanoramaPublicMountTarget
		prepared["ready"] = false
		prepared["production_ready"] = false
		prepared["release_effects_authorized"] = true
		prepared["release_effects_performed"] = false
		prepared["rollback_performed"] = false
		prepared["recovery"] = false
		prepared["disposition"] = "volume-bootstrap-prepared"
		if err := appendAiPanoramaJournalEvent(
			journal, aiPanoramaInstallBootstrapPreparedEvent, prepared,
		); err != nil {
			return nil, err
		}
		runtime, bootstrap, err = bootstrapAiPanoramaGovernedVolume(
			parent, root, config, runtime,
		)
		if err != nil {
			return nil, err
		}
		verified := cloneFields(prepared)
		verified["ai_panorama_bootstrap_after"] = aiPanoramaRuntimeObservationValue(runtime)
		verified["ai_panorama_bootstrap_result_sha256"] = bootstrap.RawSHA256
		verified["disposition"] = "virgin-governed-volume-initialized"
		if err := appendAiPanoramaJournalEvent(
			journal, aiPanoramaInstallVolumeInitializedEvent, verified,
		); err != nil {
			return nil, err
		}
	}
	if runtime.PublicVolumeNeedsInitialization ||
		runtime.PublicVolumeUID != 10001 || runtime.PublicVolumeGID != 10001 ||
		runtime.PublicVolumeMode != 0o755 {
		return nil, fmt.Errorf("ai-panorama-governed-volume-not-ready")
	}
	sealedIntent := authorityFields(config, request, identity)
	sealedIntent["release_run_receipt_digest"] = release.ReceiptDigest
	sealedIntent["ai_panorama_runtime_observation"] =
		aiPanoramaRuntimeObservationValue(runtime)
	sealedIntent["ai_panorama_slug"] = aiPanoramaPraterSlug
	sealedIntent["ready"] = false
	sealedIntent["production_ready"] = false
	sealedIntent["release_effects_authorized"] = false
	sealedIntent["release_effects_performed"] = false
	sealedIntent["rollback_performed"] = false
	sealedIntent["recovery"] = false
	sealed, err := prepareAiPanoramaSealedArtifact(journal, sealedIntent)
	if err != nil {
		return nil, err
	}
	sealedDigest, err := aiPanoramaSealedEvidenceDigest(sealed)
	if err != nil {
		return nil, err
	}
	before, err := snapshotAiPanoramaRelated(runtime.PublicVolumeMountpoint)
	if err != nil {
		return nil, err
	}
	if len(before.Entries) != 0 {
		return nil, fmt.Errorf("ai-panorama-public-target-or-residue-present")
	}
	if err := validateAiPanoramaPermitInventory(root, journal); err != nil {
		return nil, err
	}
	if err := validateAiPanoramaDormantProjectionInventory(root); err != nil {
		return nil, err
	}
	base := aiPanoramaBaseFields(
		config, request, identity, release.ReceiptDigest, runtime, sealed, sealedDigest, before,
	)
	lineage, priorAttempt, err := aiPanoramaAttemptLineageFor(
		journal, config, release.ReceiptDigest,
	)
	if err != nil || aiPanoramaBindAttemptLineage(base, lineage) != nil {
		return nil, fmt.Errorf("ai-panorama-attempt-not-eligible")
	}
	if lineage.Sequence == 1 {
		err = ensureAiPanoramaStateGenesis(root, journal, base)
	} else {
		err = validateAiPanoramaRetryEligibility(
			root, journal, runtime, before, priorAttempt,
		)
	}
	if err != nil {
		return nil, err
	}
	discoveryRequest, err := newAiPanoramaDiscoveryRequest(root)
	if err != nil {
		return nil, err
	}
	discoveryWire, err := aiPanoramaDiscoveryRequestWire(discoveryRequest.RequestID)
	if err != nil || aiPanoramaRawSHA256(discoveryWire) != discoveryRequest.SHA256 {
		zero(discoveryWire)
		return nil, fmt.Errorf("ai-panorama-discovery-request-invalid")
	}
	discoveryProjection := aiPanoramaProjection{
		Kind: "discovery-request", Path: discoveryRequest.Path, Mode: 0o600,
		SHA256: discoveryRequest.SHA256, Raw: discoveryWire,
	}
	defer discoveryProjection.release()
	base["ai_panorama_discovery_projection"] = discoveryProjection.journalValue()
	if err := appendAiPanoramaProjectionIntent(
		journal, aiPanoramaAttemptStartedEvent, base,
		[]aiPanoramaProjection{discoveryProjection},
	); err != nil {
		return nil, err
	}
	if err := persistAiPanoramaFixedProjection(root, &discoveryProjection); err != nil {
		return nil, err
	}
	discoveryRaw, err := runAiPanoramaDatabaseContainerPhase(
		parent, root, config, runtime, nil,
		release.DatabaseEnvironmentDigest, "discover",
	)
	if err != nil {
		return nil, err
	}
	discovery, err := parseAiPanoramaDiscoveryResult(discoveryRaw, discoveryRequest.RequestID)
	zero(discoveryRaw)
	if err != nil {
		return nil, err
	}
	defer discovery.release()
	requestIDHash, err := aiPanoramaRequestIDHash(discovery.RequestID)
	if err != nil {
		return nil, err
	}
	base["ai_panorama_request_id_sha256"] = requestIDHash
	base["ai_panorama_discovery_request_sha256"] = discoveryRequest.SHA256
	base["ai_panorama_discovery_result_sha256"] = discovery.RawSHA256
	base["ai_panorama_expected_publication_record_sha256"] =
		discovery.ExpectedPublicationRecordSHA256
	discoveryValidated := cloneFields(base)
	discoveryValidated["disposition"] = "discovery-validated"
	if err := appendAiPanoramaJournalEvent(
		journal, aiPanoramaDiscoveryValidatedEvent, discoveryValidated,
	); err != nil {
		return nil, err
	}
	if err := removeAiPanoramaProjection(root, &discoveryProjection); err != nil {
		return nil, err
	}
	issuedAt := authorityNow().UTC().Truncate(time.Second)
	purposeKey, err := aiPanoramaSigningKeyForContext(root, private, issuedAt)
	if err != nil {
		return nil, err
	}
	defer purposeKey.release()
	signing, err := prepareAiPanoramaSigningContext(
		root, config, identity, runtime, sealed, purposeKey, release.ReceiptDigest,
		func(projections []aiPanoramaProjection) error {
			base["ai_panorama_context_projections"] =
				aiPanoramaProjectionValues(projections)
			return appendAiPanoramaProjectionIntent(
				journal, aiPanoramaContextProjectionIntentEvent,
				base, projections,
			)
		},
	)
	if err != nil {
		return nil, err
	}
	defer signing.release()
	permitValue, err := buildAiPanoramaPermit(
		config, identity, discovery, runtime, sealed, signing, purposeKey, issuedAt,
	)
	if err != nil {
		return nil, err
	}
	permitRaw, permit, err := signAiPanoramaPermit(
		root, permitValue, private, issuedAt, authorityNow().UTC(),
	)
	if err != nil {
		return nil, err
	}
	defer zero(permitRaw)
	permitIssuedAt, permitIssuedOK := parseAiPanoramaTimestamp(permit.IssuedAt)
	permitExpiresAt, permitExpiresOK := parseAiPanoramaTimestamp(permit.ExpiresAt)
	if !permitIssuedOK || !permitExpiresOK ||
		!permitExpiresAt.After(permitIssuedAt) {
		return nil, fmt.Errorf("ai-panorama-permit-state-time-binding-invalid")
	}
	nonce, nonceOK := exactString(permitValue["nonce"])
	permitContextRaw, contextErr := canonicalJSON(permitValue)
	if !nonceOK || !aiPanoramaNoncePattern.MatchString(nonce) || contextErr != nil {
		zero(permitContextRaw)
		return nil, fmt.Errorf("ai-panorama-permit-state-binding-invalid")
	}
	stateExpectation := &aiPanoramaAdvancedStateExpectation{
		PermitSHA256:         permit.SHA256,
		RequestIDSHA256:      aiPanoramaRawSHA256([]byte(discovery.RequestID)),
		NonceSHA256:          aiPanoramaRawSHA256([]byte(nonce)),
		ContextSHA256:        aiPanoramaRawSHA256(permitContextRaw),
		SignedPreimageSHA256: permit.PreimageSHA256,
		TrustAssertionSHA256: signing.TrustAssertionSHA256,
		KeyID:                permit.KeyID, KeyEpoch: permit.KeyEpoch,
		KeySHA256: permit.KeySHA256, KeyringSHA256: permit.KeyringSHA256,
		VolumeProfileSHA256:     signing.VolumeProfileSHA256,
		PublicationRecordSHA256: discovery.ExpectedPublicationRecordSHA256,
		PublicVolumeDevice:      runtime.PublicVolumeDevice,
		PublicVolumeInode:       runtime.PublicVolumeInode,
		TerminalEvent:           "committed",
		IssuedAt:                permitIssuedAt,
		ExpiresAt:               permitExpiresAt,
		ExecutionLease:          permit.ExecutionLease,
	}
	zero(permitContextRaw)
	base["ai_panorama_permit"] = aiPanoramaPermitEvidence(permit)
	base["ai_panorama_permit_canonical_bytes_base64"] =
		base64.RawStdEncoding.EncodeToString(permitRaw)
	permitIntent := cloneFields(base)
	permitIntent["disposition"] = "permit-persistence-intent"
	if err := appendAiPanoramaJournalEvent(
		journal, aiPanoramaPermitPersistenceIntentEvent, permitIntent,
	); err != nil {
		return nil, err
	}
	if err := persistAiPanoramaPermit(root, permitRaw, permit); err != nil {
		return nil, err
	}
	releaseRequestRaw, err := aiPanoramaReleaseRequestWire(
		discovery.OwnerPrincipalID,
		discovery.ExpectedPublicationRecordSHA256,
		discovery.RequestID,
		permit.Relpath,
	)
	if err != nil {
		return nil, err
	}
	releaseProjection := aiPanoramaProjection{
		Kind: "release-request", Path: aiPanoramaReleaseRequestPath, Mode: 0o600,
		SHA256: aiPanoramaRawSHA256(releaseRequestRaw), Raw: releaseRequestRaw,
	}
	defer releaseProjection.release()
	base["ai_panorama_release_projection"] = releaseProjection.journalValue()
	if err := appendAiPanoramaProjectionIntent(
		journal, aiPanoramaReleaseProjectionIntentEvent, base,
		[]aiPanoramaProjection{releaseProjection},
	); err != nil {
		return nil, err
	}
	if err := persistAiPanoramaFixedProjection(root, &releaseProjection); err != nil {
		return nil, err
	}
	base["admitted_at"] = json.Number(strconv.FormatInt(authorityNow().UTC().Unix(), 10))
	base["disposition"] = "admitted"
	if err := appendAiPanoramaJournalEvent(
		journal, aiPanoramaInstallAdmittedEvent, cloneFields(base),
	); err != nil {
		return nil, err
	}
	preflightStarted := cloneFields(base)
	preflightStarted["disposition"] = "preflight-started"
	if err := appendAiPanoramaJournalEvent(
		journal, aiPanoramaInstallPreflightStartedEvent, preflightStarted,
	); err != nil {
		return nil, err
	}
	preflightRaw, preflightErr := runAiPanoramaContainerRaw(
		parent, config, runtime, sealed, nil, "preflight", "",
	)
	var preflight *aiPanoramaPhaseResult
	if preflightErr == nil {
		preflight, preflightErr = parseAiPanoramaPhaseResult(preflightRaw, "preflight")
	}
	zero(preflightRaw)
	afterPreflight, snapshotErr := snapshotAiPanoramaRelated(runtime.PublicVolumeMountpoint)
	if preflightErr != nil || snapshotErr != nil || preflight == nil ||
		preflight.Status != "preflight-passed" ||
		afterPreflight == nil || afterPreflight.Digest != before.Digest ||
		len(afterPreflight.Entries) != 0 {
		if snapshotErr != nil || afterPreflight == nil ||
			afterPreflight.Digest != before.Digest ||
			len(afterPreflight.Entries) != 0 {
			return nil, aiPanoramaRecordRecoveryRequired(
				journal, base, "preflight-public-state-ambiguous",
			)
		}
		terminal := cloneFields(base)
		terminal["ai_panorama_after_manifest"] =
			aiPanoramaManifestValue(afterPreflight)
		terminal["ai_panorama_after_manifest_sha256"] = afterPreflight.Digest
		terminal["completed_at"] = json.Number(strconv.FormatInt(authorityNow().UTC().Unix(), 10))
		terminal["disposition"] = "failed-before-production-mutation"
		terminal["rollback_performed"] = false
		if err := recordAiPanoramaAttemptProjectionsCleaned(
			root, journal, terminal, aiPanoramaInstallFailedNoEffectsEvent,
		); err != nil {
			return nil, err
		}
		return journal.Append(aiPanoramaInstallFailedNoEffectsEvent, terminal)
	}
	base["ai_panorama_preflight_result_sha256"] = preflight.RawSHA256
	base["disposition"] = "ready"
	if err := appendAiPanoramaJournalEvent(
		journal, aiPanoramaInstallPreflightReadyEvent, cloneFields(base),
	); err != nil {
		return nil, err
	}
	base["release_effects_performed"] = true
	base["mutation_started_at"] = json.Number(strconv.FormatInt(authorityNow().UTC().Unix(), 10))
	base["disposition"] = "mutation-started"
	if err := appendAiPanoramaJournalEvent(
		journal, aiPanoramaInstallMutationStartedEvent, cloneFields(base),
	); err != nil {
		return nil, err
	}
	applyRaw, applyErr := runAiPanoramaDatabaseContainerPhase(
		parent, root, config, runtime, sealed,
		release.DatabaseEnvironmentDigest, "apply",
	)
	var apply *aiPanoramaPhaseResult
	if applyErr == nil {
		apply, applyErr = parseAiPanoramaPhaseResult(applyRaw, "apply")
	}
	zero(applyRaw)
	expectedTerminalSHA256 := ""
	if apply != nil {
		expectedTerminalSHA256 = apply.TerminalReceiptSHA256
	}
	durable, terminalErr := readAiPanoramaTerminal(
		root, discovery.RequestID, permit.SHA256, expectedTerminalSHA256,
		runtime, sealed, discovery.ExpectedPublicationRecordSHA256,
	)
	after, afterErr := snapshotAiPanoramaRelated(runtime.PublicVolumeMountpoint)
	if terminalErr == nil && durable != nil && durable.Status == "committed" &&
		durable.BeforeSHA256 == discovery.ExpectedPublicationRecordSHA256 &&
		afterErr == nil && aiPanoramaPublishedManifestValid(after) &&
		(apply == nil || apply.Status == "committed") {
		stateExpectation.BindingStatus = durable.BindingStatus
		stateExpectation.BindingBeforeSHA256 = durable.BeforeSHA256
		stateExpectation.BindingAfterSHA256 = durable.AfterSHA256
		genesis, genesisCompleted, genesisErr := aiPanoramaStateGenesisFromEvent(journal)
		if genesis != nil {
			defer genesis.release()
		}
		if genesisErr != nil || genesis == nil || !genesisCompleted {
			return nil, aiPanoramaRecordRecoveryRequired(
				journal, base, "advanced-state-genesis-unavailable",
			)
		}
		stateProfile, stateErr := observeAiPanoramaAdvancedState(
			root, genesis, stateExpectation,
		)
		if stateErr != nil {
			return nil, aiPanoramaRecordRecoveryRequired(
				journal, base, "advanced-state-validation-failed",
			)
		}
		if apply != nil {
			base["ai_panorama_apply_result_sha256"] = apply.RawSHA256
		}
		base["ai_panorama_terminal"] = aiPanoramaTerminalValue(durable)
		base["ai_panorama_advanced_state_profile"] = stateProfile.journalValue()
		base["ai_panorama_after_manifest"] = aiPanoramaManifestValue(after)
		base["ai_panorama_after_manifest_sha256"] = after.Digest
		base["disposition"] = "mutation-verified"
		if err := appendAiPanoramaJournalEvent(
			journal, aiPanoramaInstallMutationVerifiedEvent, cloneFields(base),
		); err != nil {
			return nil, err
		}
		terminal := cloneFields(base)
		terminal["ai_panorama_install_verified"] = true
		terminal["completed_at"] = json.Number(strconv.FormatInt(authorityNow().UTC().Unix(), 10))
		terminal["disposition"] = "succeeded"
		terminal["production_ready"] = true
		terminal["rollback_performed"] = false
		if err := recordAiPanoramaAttemptProjectionsCleaned(
			root, journal, terminal, aiPanoramaInstallSucceededEvent,
		); err != nil {
			return nil, err
		}
		return journal.Append(aiPanoramaInstallSucceededEvent, terminal)
	}
	if terminalErr == nil && durable != nil &&
		(durable.Status == "failed-clean" || durable.Status == "rolled-back") &&
		afterErr == nil && after != nil && after.Digest == before.Digest &&
		len(after.Entries) == 0 {
		stateExpectation.TerminalEvent = durable.Status
		genesis, genesisCompleted, genesisErr := aiPanoramaStateGenesisFromEvent(journal)
		if genesis != nil {
			defer genesis.release()
		}
		if genesisErr != nil || genesis == nil || !genesisCompleted {
			return nil, aiPanoramaRecordRecoveryRequired(
				journal, base, "failure-state-genesis-unavailable",
			)
		}
		stateProfile, stateErr := observeAiPanoramaAdvancedState(
			root, genesis, stateExpectation,
		)
		if stateErr != nil {
			return nil, aiPanoramaRecordRecoveryRequired(
				journal, base, "failure-advanced-state-validation-failed",
			)
		}
		terminal := cloneFields(base)
		terminal["ai_panorama_terminal"] = aiPanoramaTerminalValue(durable)
		terminal["ai_panorama_advanced_state_profile"] = stateProfile.journalValue()
		terminal["ai_panorama_after_manifest"] = aiPanoramaManifestValue(after)
		terminal["ai_panorama_after_manifest_sha256"] = after.Digest
		terminal["completed_at"] = json.Number(strconv.FormatInt(authorityNow().UTC().Unix(), 10))
		terminal["production_ready"] = false
		terminal["rollback_performed"] = durable.Status == "rolled-back"
		if durable.Status == "rolled-back" {
			terminal["disposition"] = "rolled-back-by-apply-controller"
			if err := recordAiPanoramaAttemptProjectionsCleaned(
				root, journal, terminal, aiPanoramaInstallRolledBackEvent,
			); err != nil {
				return nil, err
			}
			return journal.Append(aiPanoramaInstallRolledBackEvent, terminal)
		}
		terminal["disposition"] = "failed-clean-by-apply-controller"
		if err := recordAiPanoramaAttemptProjectionsCleaned(
			root, journal, terminal, aiPanoramaInstallFailedNoEffectsEvent,
		); err != nil {
			return nil, err
		}
		return journal.Append(aiPanoramaInstallFailedNoEffectsEvent, terminal)
	}
	return nil, aiPanoramaRecordRecoveryRequired(
		journal, base, "apply-terminal-or-publication-ambiguous",
	)
}

func recoverIncompleteAiPanoramaInstallV2(
	parent context.Context,
	root string,
	journal *Journal,
	config *Config,
	last *JournalEvent,
) error {
	if parent == nil || root != "/" || journal == nil || config == nil || last == nil ||
		last.Operation != aiPanoramaInstallOperation ||
		!exactUniqueUnresolvedWorkflowEvent(
			journal, last, aiPanoramaInstallOperation,
		) ||
		last.Payload["config_digest"] != config.Digest ||
		last.Payload["plan_digest"] != config.PlanDigest ||
		last.Payload["runtime_sha"] != config.RuntimeSHA ||
		last.Payload["workflow_sha"] != config.WorkflowSHA ||
		last.Payload["deployment_id"] != config.DeploymentID {
		return fmt.Errorf("ai-panorama-recovery-binding-invalid")
	}
	if last.EventType == aiPanoramaAttemptProjectionsCleanedEvent {
		return recoverAiPanoramaCleanedTerminal(root, journal, last)
	}
	if last.EventType == aiPanoramaSealedArtifactIntentEvent {
		if err := recoverAiPanoramaSealedArtifactIntent(
			journal, last,
		); err != nil {
			return aiPanoramaRecordRecoveryRequired(
				journal, last.Payload, "sealed-artifact-recovery-ambiguous",
			)
		}
		last = &journal.events[len(journal.events)-1]
	}
	if last.EventType == aiPanoramaInstallBootstrapPreparedEvent ||
		last.EventType == aiPanoramaInstallVolumeInitializedEvent {
		before, ok := last.Payload["ai_panorama_bootstrap_before"].(map[string]any)
		mountpoint, mountOK := exactString(before["public_volume_mountpoint"])
		device, deviceOK := exactInt(before["public_volume_device"], 1, 1<<62)
		inode, inodeOK := exactInt(before["public_volume_inode"], 1, 1<<62)
		if !ok || !mountOK || !deviceOK || !inodeOK {
			return aiPanoramaRecordRecoveryRequired(
				journal, last.Payload, "bootstrap-recovery-intent-invalid",
			)
		}
		runtime, err := observeAiPanoramaRuntime(parent, root, config)
		if err != nil || runtime.PublicVolumeMountpoint != mountpoint ||
			runtime.PublicVolumeDevice != uint64(device) ||
			runtime.PublicVolumeInode != uint64(inode) ||
			runtime.PublicVolumeMode != 0o755 {
			return aiPanoramaRecordRecoveryRequired(
				journal, last.Payload, "bootstrap-recovery-identity-ambiguous",
			)
		}
		empty, err := snapshotAiPanoramaRelated(runtime.PublicVolumeMountpoint)
		if err != nil || len(empty.Entries) != 0 {
			return aiPanoramaRecordRecoveryRequired(
				journal, last.Payload, "bootstrap-recovery-inventory-ambiguous",
			)
		}
		fields := aiPanoramaRecoveryFields(last.Payload)
		if runtime.PublicVolumeUID == 10001 && runtime.PublicVolumeGID == 10001 &&
			!runtime.PublicVolumeNeedsInitialization {
			fields["ai_panorama_bootstrap_after"] = aiPanoramaRuntimeObservationValue(runtime)
			fields["disposition"] = "recovered-volume-bootstrap-verified"
			if last.EventType != aiPanoramaInstallVolumeInitializedEvent {
				if err := appendAiPanoramaJournalEvent(
					journal, aiPanoramaInstallVolumeInitializedEvent, cloneFields(fields),
				); err != nil {
					return err
				}
			}
		} else if runtime.PublicVolumeUID == 0 && runtime.PublicVolumeGID == 0 &&
			runtime.PublicVolumeNeedsInitialization {
			fields["disposition"] = "recovered-before-volume-bootstrap-mutation"
		} else {
			return aiPanoramaRecordRecoveryRequired(
				journal, last.Payload, "bootstrap-recovery-ownership-ambiguous",
			)
		}
		fields["completed_at"] = json.Number(strconv.FormatInt(authorityNow().UTC().Unix(), 10))
		fields["production_ready"] = false
		fields["release_effects_performed"] = false
		fields["rollback_performed"] = false
		wire, err := journal.Append(aiPanoramaInstallFailedNoEffectsEvent, fields)
		zero(wire)
		return err
	}
	preMutation := last.EventType == aiPanoramaStateGenesisIntentEvent ||
		last.EventType == aiPanoramaStateGenesisEvent ||
		last.EventType == aiPanoramaSealedArtifactIntentEvent ||
		last.EventType == aiPanoramaSealedArtifactCleanedEvent ||
		last.EventType == aiPanoramaAttemptStartedEvent ||
		last.EventType == aiPanoramaDiscoveryValidatedEvent ||
		last.EventType == aiPanoramaContextProjectionIntentEvent ||
		last.EventType == aiPanoramaPermitPersistenceIntentEvent ||
		last.EventType == aiPanoramaReleaseProjectionIntentEvent ||
		last.EventType == aiPanoramaInstallAdmittedEvent ||
		last.EventType == aiPanoramaInstallPreflightStartedEvent ||
		last.EventType == aiPanoramaInstallPreflightReadyEvent
	if last.EventType == aiPanoramaInstallRecoveryRequiredEvent &&
		last.Payload["release_effects_performed"] == false {
		preMutation = true
	}
	if preMutation {
		return recoverAiPanoramaPreMutationAttempt(
			parent, root, journal, config, last,
		)
	}
	return aiPanoramaRecordRecoveryRequired(
		journal, aiPanoramaRecoveryFields(last.Payload),
		"classifier-unavailable-recovery-required",
	)
}
