//go:build linux && amd64

package authority

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"time"
)

const (
	aiPanoramaAttemptStartedEvent            = "ai-panorama-install-attempt-started"
	aiPanoramaDiscoveryValidatedEvent        = "ai-panorama-install-discovery-validated"
	aiPanoramaContextProjectionIntentEvent   = "ai-panorama-install-context-projection-intent"
	aiPanoramaPermitPersistenceIntentEvent   = "ai-panorama-install-permit-persistence-intent"
	aiPanoramaReleaseProjectionIntentEvent   = "ai-panorama-install-release-projection-intent"
	aiPanoramaAttemptProjectionsCleanedEvent = "ai-panorama-install-projections-cleaned"
	aiPanoramaReleaseRequestSchema           = "propertyquarry.prater-ai-panorama-release-request.v2"
	aiPanoramaReleaseRequestPath             = aiPanoramaControlRoot + "/prater-release-request.v2.json"
	aiPanoramaLegacyReleaseRequestPath       = aiPanoramaControlRoot + "/prater-release-request.v1.json"
	aiPanoramaMaximumAttemptCount            = 32
)

type aiPanoramaAttemptLineage struct {
	Sequence int64
	RetryOf  string
}

type aiPanoramaProjection struct {
	Kind   string
	Path   string
	Mode   os.FileMode
	SHA256 string
	Raw    []byte
}

func (value *aiPanoramaProjection) release() {
	if value == nil {
		return
	}
	zero(value.Raw)
	*value = aiPanoramaProjection{}
}

func aiPanoramaAttemptLineageFor(
	journal *Journal,
	config *Config,
	releaseReceiptDigest string,
) (*aiPanoramaAttemptLineage, *JournalEvent, error) {
	if journal == nil || config == nil || !digestPattern.MatchString(releaseReceiptDigest) {
		return nil, nil, fmt.Errorf("ai-panorama-attempt-lineage-input-invalid")
	}
	var terminals []*JournalEvent
	globalPrevious := "genesis"
	lineageCounts := make(map[string]int64)
	for index := range journal.events {
		event := &journal.events[index]
		if event.Operation != aiPanoramaInstallOperation ||
			(event.EventType != aiPanoramaInstallSucceededEvent &&
				event.EventType != aiPanoramaInstallRolledBackEvent &&
				event.EventType != aiPanoramaInstallFailedNoEffectsEvent) {
			continue
		}
		retryOf, retryOK := exactString(
			event.Payload["ai_panorama_retry_of_terminal_receipt_digest"],
		)
		if !retryOK || retryOf != globalPrevious ||
			!digestPattern.MatchString(event.ReceiptDigest) {
			return nil, nil, fmt.Errorf("ai-panorama-attempt-global-lineage-invalid")
		}
		eventConfig, configOK := exactString(event.Payload["config_digest"])
		eventRelease, releaseOK := exactString(
			event.Payload["release_run_receipt_digest"],
		)
		if !configOK || !digestPattern.MatchString(eventConfig) ||
			!releaseOK || !digestPattern.MatchString(eventRelease) {
			return nil, nil, fmt.Errorf("ai-panorama-attempt-lineage-invalid")
		}
		lineageKey := eventConfig + "\x00" + eventRelease
		lineageCounts[lineageKey]++
		sequence, sequenceOK := exactInt(
			event.Payload["ai_panorama_attempt_sequence"],
			lineageCounts[lineageKey], lineageCounts[lineageKey],
		)
		if !sequenceOK || sequence != lineageCounts[lineageKey] ||
			sequence > aiPanoramaMaximumAttemptCount {
			return nil, nil, fmt.Errorf("ai-panorama-attempt-lineage-invalid")
		}
		globalPrevious = event.ReceiptDigest
		if event.Payload["config_digest"] != config.Digest ||
			event.Payload["plan_digest"] != config.PlanDigest ||
			event.Payload["runtime_sha"] != config.RuntimeSHA ||
			event.Payload["workflow_sha"] != config.WorkflowSHA ||
			event.Payload["deployment_id"] != config.DeploymentID ||
			event.Payload["release_run_receipt_digest"] != releaseReceiptDigest {
			continue
		}
		terminals = append(terminals, event)
	}
	if len(terminals) >= aiPanoramaMaximumAttemptCount {
		return nil, nil, fmt.Errorf("ai-panorama-attempt-limit-reached")
	}
	var prior *JournalEvent
	if len(terminals) > 0 {
		prior = terminals[len(terminals)-1]
		if prior.EventType == aiPanoramaInstallSucceededEvent {
			return nil, prior, fmt.Errorf("ai-panorama-install-already-succeeded")
		}
	}
	return &aiPanoramaAttemptLineage{
		Sequence: int64(len(terminals) + 1),
		RetryOf:  globalPrevious,
	}, prior, nil
}

func aiPanoramaBindAttemptLineage(
	base map[string]any,
	lineage *aiPanoramaAttemptLineage,
) error {
	if base == nil || lineage == nil || lineage.Sequence < 1 ||
		lineage.Sequence > aiPanoramaMaximumAttemptCount ||
		(lineage.RetryOf != "genesis" &&
			!digestPattern.MatchString(lineage.RetryOf)) {
		return fmt.Errorf("ai-panorama-attempt-lineage-invalid")
	}
	base["ai_panorama_attempt_sequence"] =
		json.Number(strconv.FormatInt(lineage.Sequence, 10))
	base["ai_panorama_retry_of_terminal_receipt_digest"] = lineage.RetryOf
	return nil
}

func validateAiPanoramaRetryEligibility(
	root string,
	journal *Journal,
	runtime *aiPanoramaRuntimeObservation,
	before *aiPanoramaRelatedManifest,
	prior *JournalEvent,
) error {
	if journal == nil || runtime == nil || before == nil || prior == nil ||
		(prior.EventType != aiPanoramaInstallFailedNoEffectsEvent &&
			prior.EventType != aiPanoramaInstallRolledBackEvent) ||
		before.RootDevice != runtime.PublicVolumeDevice ||
		before.RootInode != runtime.PublicVolumeInode ||
		len(before.Entries) != 0 {
		return fmt.Errorf("ai-panorama-retry-prior-not-eligible")
	}
	afterDigest, afterOK := exactString(
		prior.Payload["ai_panorama_after_manifest_sha256"],
	)
	if !afterOK || afterDigest != before.Digest {
		return fmt.Errorf("ai-panorama-retry-public-state-invalid")
	}
	genesis, completed, err := aiPanoramaStateGenesisFromEvent(journal)
	if genesis != nil {
		defer genesis.release()
	}
	if err != nil || genesis == nil || !completed ||
		validateAiPanoramaGenesisRoot(root, genesis.Root) != nil {
		return fmt.Errorf("ai-panorama-retry-genesis-lineage-invalid")
	}
	terminalValue, consumed := prior.Payload["ai_panorama_terminal"].(map[string]any)
	if consumed {
		status, statusOK := exactString(terminalValue["status"])
		profile, profileErr := parseAiPanoramaAdvancedStateProfile(
			prior.Payload["ai_panorama_advanced_state_profile"],
		)
		if !statusOK || (status != "failed-clean" && status != "rolled-back") ||
			profileErr != nil || profile.Operation.TerminalEvent != status ||
			validateAiPanoramaAdvancedStateProfile(root, genesis, profile) != nil {
			return fmt.Errorf("ai-panorama-retry-consumed-state-invalid")
		}
		return nil
	}
	var baseline *aiPanoramaAdvancedStateProfile
	for index := prior.Sequence - 2; index >= 0; index-- {
		event := &journal.events[index]
		if event.Operation != aiPanoramaInstallOperation {
			continue
		}
		profile, parseErr := parseAiPanoramaAdvancedStateProfile(
			event.Payload["ai_panorama_advanced_state_profile"],
		)
		if parseErr == nil {
			baseline = profile
			break
		}
	}
	if baseline != nil {
		if validateAiPanoramaAdvancedStateProfile(root, genesis, baseline) != nil {
			return fmt.Errorf("ai-panorama-retry-unconsumed-state-invalid")
		}
		return nil
	}
	if err := validateAiPanoramaStateGenesis(root, genesis); err != nil {
		return fmt.Errorf("ai-panorama-retry-unconsumed-state-invalid")
	}
	return nil
}

func validateAiPanoramaPermitInventory(root string, journal *Journal) error {
	if journal == nil {
		return fmt.Errorf("ai-panorama-permit-inventory-input-invalid")
	}
	authorized := make(map[string]string)
	for index := range journal.events {
		event := &journal.events[index]
		if event.EventType != aiPanoramaPermitPersistenceIntentEvent {
			continue
		}
		evidence, ok := event.Payload["ai_panorama_permit"].(map[string]any)
		requestID, requestOK := exactString(evidence["request_id"])
		path, pathOK := exactString(evidence["path"])
		sha256Value, shaOK := exactString(evidence["sha256"])
		expectedPath, pathErr := aiPanoramaPermitPath(requestID)
		if !ok || !requestOK || pathErr != nil || !pathOK ||
			path != expectedPath || !shaOK ||
			!aiPanoramaRawSHA256Pattern.MatchString(sha256Value) ||
			authorized[path] != "" {
			return fmt.Errorf("ai-panorama-permit-inventory-journal-invalid")
		}
		authorized[path] = sha256Value
	}
	permitRoot := rooted(root, aiPanoramaPermitRoot)
	rootInfo, err := os.Lstat(permitRoot)
	rootMetadata, rootOK := infoSys(rootInfo)
	ownerUID, ownerGID := secureOwner(root)
	if err != nil || !rootOK || !rootInfo.IsDir() ||
		rootInfo.Mode().Perm() != 0o700 ||
		rootInfo.Mode()&os.ModeSymlink != 0 ||
		rootMetadata.Uid != ownerUID || rootMetadata.Gid != ownerGID ||
		rootMetadata.Nlink < 2 {
		return fmt.Errorf("ai-panorama-permit-inventory-root-invalid")
	}
	entries, err := os.ReadDir(permitRoot)
	if err != nil {
		return fmt.Errorf("ai-panorama-permit-inventory-unavailable")
	}
	observed := make(map[string]bool, len(entries))
	for _, entry := range entries {
		name := entry.Name()
		if len(name) != len("prater-ai-panorama-install-")+
			32+len(".v2.json") ||
			name[:len("prater-ai-panorama-install-")] !=
				"prater-ai-panorama-install-" ||
			name[len(name)-len(".v2.json"):] != ".v2.json" {
			return fmt.Errorf("ai-panorama-permit-inventory-extra-entry")
		}
		requestID := name[len("prater-ai-panorama-install-") : len(name)-len(".v2.json")]
		path, pathErr := aiPanoramaPermitPath(requestID)
		expectedSHA256 := authorized[path]
		if pathErr != nil || expectedSHA256 == "" || observed[path] {
			return fmt.Errorf("ai-panorama-permit-inventory-extra-entry")
		}
		info, statErr := os.Lstat(filepath.Join(permitRoot, name))
		metadata, metadataOK := infoSys(info)
		if statErr != nil || !metadataOK || !info.Mode().IsRegular() ||
			info.Mode().Perm() != 0o600 || metadata.Uid != ownerUID ||
			metadata.Gid != ownerGID || metadata.Nlink != 1 ||
			uint64(metadata.Dev) != uint64(rootMetadata.Dev) {
			return fmt.Errorf("ai-panorama-permit-inventory-entry-invalid")
		}
		_, raw, readErr := readAiPanoramaPersistedPermit(
			root, requestID, expectedSHA256,
		)
		zero(raw)
		if readErr != nil {
			return fmt.Errorf("ai-panorama-permit-inventory-entry-invalid")
		}
		observed[path] = true
	}
	if len(observed) != len(authorized) {
		return fmt.Errorf("ai-panorama-permit-inventory-entry-missing")
	}
	for path := range authorized {
		if !observed[path] {
			return fmt.Errorf("ai-panorama-permit-inventory-entry-missing")
		}
	}
	return nil
}

func recoverAiPanoramaPermitPersistence(
	root string,
	base map[string]any,
) error {
	encoded, exists := base["ai_panorama_permit_canonical_bytes_base64"]
	if !exists {
		return nil
	}
	encodedText, encodedOK := exactString(encoded)
	raw, decodeErr := base64.RawStdEncoding.Strict().DecodeString(encodedText)
	evidence, evidenceOK := base["ai_panorama_permit"].(map[string]any)
	requestID, requestOK := exactString(evidence["request_id"])
	relpath, relpathOK := exactString(evidence["permit_relpath"])
	path, pathOK := exactString(evidence["path"])
	sha256Value, shaOK := exactString(evidence["sha256"])
	expectedRelpath, relpathErr := aiPanoramaPermitRelpath(requestID)
	expectedPath, pathErr := aiPanoramaPermitPath(requestID)
	if !encodedOK || decodeErr != nil || !evidenceOK ||
		!requestOK || !relpathOK || relpathErr != nil ||
		relpath != expectedRelpath || !pathOK || pathErr != nil ||
		path != expectedPath || !shaOK ||
		!aiPanoramaRawSHA256Pattern.MatchString(sha256Value) ||
		aiPanoramaRawSHA256(raw) != sha256Value {
		zero(raw)
		return fmt.Errorf("ai-panorama-permit-recovery-binding-invalid")
	}
	observation := &aiPanoramaSignedPermit{
		RequestID: requestID, Relpath: relpath, Path: path, SHA256: sha256Value,
	}
	err := persistAiPanoramaPermit(root, raw, observation)
	zero(raw)
	return err
}

func validateAiPanoramaDormantProjectionInventory(root string) error {
	runtimeEntries, err := os.ReadDir(rooted(root, aiPanoramaRuntimeRoot))
	if err != nil || len(runtimeEntries) != 0 {
		return fmt.Errorf("ai-panorama-stale-runtime-projection-present")
	}
	controlEntries, err := os.ReadDir(rooted(root, aiPanoramaControlRoot))
	if err != nil {
		return fmt.Errorf("ai-panorama-stale-projection-observation-failed")
	}
	for _, entry := range controlEntries {
		name := entry.Name()
		if name == filepath.Base(aiPanoramaDiscoveryRequestPath) ||
			name == filepath.Base(aiPanoramaReleaseRequestPath) ||
			name == filepath.Base(aiPanoramaLegacyReleaseRequestPath) ||
			strings.HasPrefix(name, ".projection-") ||
			strings.HasPrefix(name, ".context-") {
			return fmt.Errorf("ai-panorama-stale-projection-present")
		}
	}
	return nil
}

func aiPanoramaRecoveryFields(payload map[string]any) map[string]any {
	fields := cloneFields(payload)
	for _, key := range []string{
		"schema", "version", "journal_sequence", "journal_predecessor_digest",
		"event_type", "receipt_key_id",
	} {
		delete(fields, key)
	}
	return fields
}

func validateAiPanoramaStateUnchangedFromPrior(
	root string,
	journal *Journal,
	currentSequence int64,
) error {
	genesis, completed, err := aiPanoramaStateGenesisFromEvent(journal)
	if genesis != nil {
		defer genesis.release()
	}
	if err != nil || genesis == nil || !completed ||
		validateAiPanoramaGenesisRoot(root, genesis.Root) != nil {
		return fmt.Errorf("ai-panorama-state-baseline-genesis-invalid")
	}
	for index := currentSequence - 2; index >= 0; index-- {
		event := &journal.events[index]
		if event.Operation != aiPanoramaInstallOperation {
			continue
		}
		profile, profileErr := parseAiPanoramaAdvancedStateProfile(
			event.Payload["ai_panorama_advanced_state_profile"],
		)
		if profileErr == nil {
			if validateAiPanoramaAdvancedStateProfile(
				root, genesis, profile,
			) != nil {
				return fmt.Errorf("ai-panorama-state-baseline-changed")
			}
			return nil
		}
	}
	if validateAiPanoramaStateGenesis(root, genesis) != nil {
		return fmt.Errorf("ai-panorama-state-baseline-changed")
	}
	return nil
}

func cleanupAiPanoramaInterruptedAttempt(
	parent context.Context,
	root string,
	config *Config,
	runtime *aiPanoramaRuntimeObservation,
	allowApply bool,
) error {
	if parent == nil || config == nil || runtime == nil {
		return fmt.Errorf("ai-panorama-interrupted-cleanup-input-invalid")
	}
	cleanupContext, cancel := context.WithTimeout(
		context.WithoutCancel(parent), 2*time.Minute,
	)
	defer cancel()
	applyExists, err := aiPanoramaPhaseContainerExists(
		cleanupContext, config, "apply",
	)
	if err != nil || (applyExists && !allowApply) {
		return fmt.Errorf("ai-panorama-interrupted-apply-container-ambiguous")
	}
	networkName, err := aiPanoramaNetworkName(config)
	if err != nil {
		return err
	}
	networkExists, err := aiPanoramaNetworkExists(
		cleanupContext, networkName,
	)
	if err != nil {
		return err
	}
	for _, phase := range []string{"discover", "preflight"} {
		expectedNetworkMode := "none"
		if phase == "discover" {
			expectedNetworkMode = networkName
		}
		if err := cleanupAiPanoramaRecoveryPhaseContainer(
			cleanupContext, config, runtime, phase, expectedNetworkMode,
		); err != nil {
			return err
		}
	}
	if allowApply {
		if err := cleanupAiPanoramaRecoveryPhaseContainer(
			cleanupContext, config, runtime, "apply", networkName,
		); err != nil {
			return err
		}
	}
	if networkExists {
		if err := cleanupAiPanoramaNetwork(
			cleanupContext, config, runtime,
		); err != nil {
			return err
		}
	}
	if err := destroyAiPanoramaDatabaseSecret(root); err != nil {
		return err
	}
	return nil
}

func recoverAiPanoramaPreMutationAttempt(
	parent context.Context,
	root string,
	journal *Journal,
	config *Config,
	last *JournalEvent,
) error {
	base := aiPanoramaRecoveryFields(last.Payload)
	runtime, err := observeAiPanoramaRuntime(parent, root, config)
	storedRuntime, storedOK := base["ai_panorama_runtime_observation"].(map[string]any)
	if err != nil || !storedOK ||
		!canonicalValuesEqual(
			storedRuntime, aiPanoramaRuntimeObservationValue(runtime),
		) {
		return aiPanoramaRecordRecoveryRequired(
			journal, base, "pre-mutation-runtime-binding-ambiguous",
		)
	}
	after, err := snapshotAiPanoramaRelated(runtime.PublicVolumeMountpoint)
	beforeDigest, beforeOK := exactString(
		base["ai_panorama_before_manifest_sha256"],
	)
	if err != nil || !beforeOK || after == nil ||
		after.Digest != beforeDigest || len(after.Entries) != 0 {
		return aiPanoramaRecordRecoveryRequired(
			journal, base, "pre-mutation-public-state-ambiguous",
		)
	}
	if last.EventType == aiPanoramaStateGenesisIntentEvent ||
		last.EventType == aiPanoramaStateGenesisEvent {
		if err := ensureAiPanoramaStateGenesis(root, journal, base); err != nil {
			return aiPanoramaRecordRecoveryRequired(
				journal, base, "pre-mutation-genesis-recovery-failed",
			)
		}
	}
	if err := recoverAiPanoramaPermitPersistence(root, base); err != nil {
		return aiPanoramaRecordRecoveryRequired(
			journal, base, "pre-mutation-permit-recovery-failed",
		)
	}
	if err := validateAiPanoramaStateUnchangedFromPrior(
		root, journal, last.Sequence,
	); err != nil {
		return aiPanoramaRecordRecoveryRequired(
			journal, base, "pre-mutation-private-state-ambiguous",
		)
	}
	if err := cleanupAiPanoramaInterruptedAttempt(
		parent, root, config, runtime, false,
	); err != nil {
		return aiPanoramaRecordRecoveryRequired(
			journal, base, "pre-mutation-container-cleanup-ambiguous",
		)
	}
	base["ai_panorama_after_manifest"] = aiPanoramaManifestValue(after)
	base["ai_panorama_after_manifest_sha256"] = after.Digest
	base["completed_at"] = json.Number(
		strconv.FormatInt(authorityNow().UTC().Unix(), 10),
	)
	base["production_ready"] = false
	base["release_effects_performed"] = false
	base["rollback_performed"] = false
	base["recovery"] = true
	base["disposition"] = "recovered-before-production-mutation"
	if err := recordAiPanoramaAttemptProjectionsCleaned(
		root, journal, base, aiPanoramaInstallFailedNoEffectsEvent,
	); err != nil {
		return aiPanoramaRecordRecoveryRequired(
			journal, base, "pre-mutation-projection-cleanup-ambiguous",
		)
	}
	wire, err := journal.Append(aiPanoramaInstallFailedNoEffectsEvent, base)
	zero(wire)
	return err
}

func recoverAiPanoramaCleanedTerminal(
	root string,
	journal *Journal,
	last *JournalEvent,
) error {
	pending, pendingOK := exactString(
		last.Payload["ai_panorama_pending_terminal_event"],
	)
	disposition, dispositionOK := exactString(
		last.Payload["ai_panorama_pending_terminal_disposition"],
	)
	if !pendingOK || !dispositionOK ||
		(pending != aiPanoramaInstallSucceededEvent &&
			pending != aiPanoramaInstallFailedNoEffectsEvent &&
			pending != aiPanoramaInstallRolledBackEvent) {
		return fmt.Errorf("ai-panorama-cleaned-terminal-binding-invalid")
	}
	if err := validateAiPanoramaDormantProjectionInventory(root); err != nil ||
		validateAiPanoramaPermitInventory(root, journal) != nil {
		return fmt.Errorf("ai-panorama-cleaned-terminal-inventory-invalid")
	}
	runtimeValue, runtimeOK := last.Payload["ai_panorama_runtime_observation"].(map[string]any)
	mountpoint, mountOK := exactString(runtimeValue["public_volume_mountpoint"])
	device, deviceOK := exactInt(runtimeValue["public_volume_device"], 1, 1<<62)
	inode, inodeOK := exactInt(runtimeValue["public_volume_inode"], 1, 1<<62)
	manifest, manifestErr := snapshotAiPanoramaRelated(mountpoint)
	expectedDigest, digestOK := exactString(
		last.Payload["ai_panorama_after_manifest_sha256"],
	)
	if !runtimeOK || !mountOK || !deviceOK || !inodeOK ||
		manifestErr != nil || manifest == nil ||
		manifest.RootDevice != uint64(device) ||
		manifest.RootInode != uint64(inode) ||
		!digestOK || manifest.Digest != expectedDigest {
		return fmt.Errorf("ai-panorama-cleaned-terminal-public-state-invalid")
	}
	if pending == aiPanoramaInstallSucceededEvent {
		if !aiPanoramaPublishedManifestValid(manifest) {
			return fmt.Errorf("ai-panorama-cleaned-terminal-public-state-invalid")
		}
	} else if len(manifest.Entries) != 0 {
		return fmt.Errorf("ai-panorama-cleaned-terminal-public-state-invalid")
	}
	if rawProfile, exists := last.Payload["ai_panorama_advanced_state_profile"]; exists {
		genesis, completed, genesisErr := aiPanoramaStateGenesisFromEvent(journal)
		if genesis != nil {
			defer genesis.release()
		}
		profile, profileErr := parseAiPanoramaAdvancedStateProfile(rawProfile)
		if genesisErr != nil || genesis == nil || !completed ||
			profileErr != nil ||
			validateAiPanoramaAdvancedStateProfile(
				root, genesis, profile,
			) != nil {
			return fmt.Errorf("ai-panorama-cleaned-terminal-private-state-invalid")
		}
	} else if pending == aiPanoramaInstallSucceededEvent ||
		last.Payload["ai_panorama_terminal"] != nil {
		return fmt.Errorf("ai-panorama-cleaned-terminal-private-state-invalid")
	}
	fields := aiPanoramaRecoveryFields(last.Payload)
	delete(fields, "ai_panorama_pending_terminal_event")
	delete(fields, "ai_panorama_pending_terminal_disposition")
	delete(fields, "ai_panorama_projection_cleanup_verified")
	fields["disposition"] = disposition
	wire, err := journal.Append(pending, fields)
	zero(wire)
	return err
}

func (value *aiPanoramaProjection) journalValue() map[string]any {
	return map[string]any{
		"kind": value.Kind, "path": value.Path,
		"mode":                   json.Number(strconv.FormatUint(uint64(value.Mode), 10)),
		"sha256":                 value.SHA256,
		"canonical_bytes_base64": base64.RawStdEncoding.EncodeToString(value.Raw),
	}
}

func aiPanoramaProjectionValues(values []aiPanoramaProjection) []any {
	result := make([]any, 0, len(values))
	for index := range values {
		result = append(result, values[index].journalValue())
	}
	return result
}

func aiPanoramaProjectionContract(kind string) (string, os.FileMode, bool) {
	switch kind {
	case "discovery-request":
		return aiPanoramaDiscoveryRequestPath, 0o600, true
	case "release-request":
		return aiPanoramaReleaseRequestPath, 0o600, true
	case "compose-plan":
		return aiPanoramaComposePlanPath, 0o400, true
	case "volume-profile":
		return aiPanoramaVolumeProfilePath, 0o400, true
	case "trust-assertion":
		return aiPanoramaTrustAssertionPath, 0o400, true
	case "closeout-request":
		return aiPanoramaCloseoutRequestPath, 0o400, true
	default:
		return "", 0, false
	}
}

func aiPanoramaProjectionTemporaryName(projection *aiPanoramaProjection) string {
	if projection == nil {
		return ""
	}
	binding := []byte(projection.Path + "\x00" + projection.SHA256)
	name := ".projection-" + aiPanoramaRawSHA256(binding) + ".tmp"
	zero(binding)
	return name
}

func parseAiPanoramaProjection(raw any) (*aiPanoramaProjection, error) {
	value, ok := raw.(map[string]any)
	if !ok || !hasKeys(
		value, "kind", "path", "mode", "sha256", "canonical_bytes_base64",
	) {
		return nil, fmt.Errorf("ai-panorama-projection-invalid")
	}
	kind, kindOK := exactString(value["kind"])
	path, pathOK := exactString(value["path"])
	mode, modeOK := exactInt(value["mode"], 0, 0o777)
	sha256Value, digestOK := exactString(value["sha256"])
	encoded, encodedOK := exactString(value["canonical_bytes_base64"])
	decoded, decodeErr := base64.RawStdEncoding.Strict().DecodeString(encoded)
	expectedPath, expectedMode, contractOK := aiPanoramaProjectionContract(kind)
	if !kindOK || !contractOK || !pathOK || path != expectedPath ||
		!modeOK || os.FileMode(mode) != expectedMode ||
		!digestOK || !aiPanoramaRawSHA256Pattern.MatchString(sha256Value) ||
		!encodedOK || decodeErr != nil || len(decoded) < 2 ||
		len(decoded) > aiPanoramaMaximumContextFile ||
		aiPanoramaRawSHA256(decoded) != sha256Value {
		zero(decoded)
		return nil, fmt.Errorf("ai-panorama-projection-invalid")
	}
	return &aiPanoramaProjection{
		Kind: kind, Path: path, Mode: os.FileMode(mode),
		SHA256: sha256Value, Raw: decoded,
	}, nil
}

func appendAiPanoramaProjectionIntent(
	journal *Journal,
	eventType string,
	base map[string]any,
	projections []aiPanoramaProjection,
) error {
	if journal == nil || base == nil || len(projections) < 1 {
		return fmt.Errorf("ai-panorama-projection-intent-invalid")
	}
	fields := cloneFields(base)
	fields["ai_panorama_projection_intent"] = aiPanoramaProjectionValues(projections)
	fields["disposition"] = eventType
	return appendAiPanoramaJournalEvent(journal, eventType, fields)
}

func aiPanoramaAttemptProjections(
	base map[string]any,
) ([]*aiPanoramaProjection, error) {
	if base == nil {
		return nil, fmt.Errorf("ai-panorama-attempt-projections-invalid")
	}
	result := make([]*aiPanoramaProjection, 0, 5)
	seen := make(map[string]bool, 5)
	appendProjection := func(raw any) error {
		projection, err := parseAiPanoramaProjection(raw)
		if err != nil || seen[projection.Path] {
			if projection != nil {
				projection.release()
			}
			return fmt.Errorf("ai-panorama-attempt-projections-invalid")
		}
		seen[projection.Path] = true
		result = append(result, projection)
		return nil
	}
	if raw, exists := base["ai_panorama_discovery_projection"]; exists {
		if err := appendProjection(raw); err != nil {
			return nil, err
		}
	}
	if raw, exists := base["ai_panorama_context_projections"]; exists {
		values, ok := raw.([]any)
		expectedKinds := []string{"compose-plan", "volume-profile", "trust-assertion"}
		if !ok || len(values) != len(expectedKinds) {
			return nil, fmt.Errorf("ai-panorama-attempt-projections-invalid")
		}
		for index, value := range values {
			if err := appendProjection(value); err != nil ||
				result[len(result)-1].Kind != expectedKinds[index] {
				return nil, fmt.Errorf("ai-panorama-attempt-projections-invalid")
			}
		}
	}
	if raw, exists := base["ai_panorama_release_projection"]; exists {
		if err := appendProjection(raw); err != nil {
			return nil, err
		}
	}
	return result, nil
}

func releaseAiPanoramaProjections(values []*aiPanoramaProjection) {
	for _, value := range values {
		value.release()
	}
}

func cleanupAiPanoramaAttemptProjectionFiles(
	root string,
	base map[string]any,
) error {
	projections, err := aiPanoramaAttemptProjections(base)
	if err != nil {
		return err
	}
	defer releaseAiPanoramaProjections(projections)
	for _, projection := range projections {
		if err := removeAiPanoramaProjection(root, projection); err != nil {
			return err
		}
	}
	for _, legacy := range []string{
		aiPanoramaLegacyReleaseRequestPath,
		aiPanoramaLegacyPermitPath,
	} {
		if _, err := os.Lstat(rooted(root, legacy)); err == nil {
			return fmt.Errorf("ai-panorama-legacy-authority-leaf-present")
		} else if !os.IsNotExist(err) {
			return fmt.Errorf("ai-panorama-legacy-authority-leaf-observation-failed")
		}
	}
	return nil
}

func recordAiPanoramaAttemptProjectionsCleaned(
	root string,
	journal *Journal,
	base map[string]any,
	terminalEventType string,
) error {
	if journal == nil || base == nil ||
		(terminalEventType != aiPanoramaInstallSucceededEvent &&
			terminalEventType != aiPanoramaInstallFailedNoEffectsEvent &&
			terminalEventType != aiPanoramaInstallRolledBackEvent) {
		return fmt.Errorf("ai-panorama-projection-cleanup-record-input-invalid")
	}
	if err := cleanupAiPanoramaAttemptProjectionFiles(root, base); err != nil {
		return err
	}
	fields := cloneFields(base)
	fields["ai_panorama_pending_terminal_event"] = terminalEventType
	fields["ai_panorama_pending_terminal_disposition"] = base["disposition"]
	fields["ai_panorama_projection_cleanup_verified"] = true
	fields["disposition"] = "attempt-projections-cleaned"
	return appendAiPanoramaJournalEvent(
		journal, aiPanoramaAttemptProjectionsCleanedEvent, fields,
	)
}

func aiPanoramaReleaseRequestWire(
	ownerPrincipalID string,
	expectedPublicationRecordSHA256 string,
	requestID string,
	permitRelpath string,
) ([]byte, error) {
	expectedPermitRelpath, relpathErr := aiPanoramaPermitRelpath(requestID)
	if !aiPanoramaSafeIDPattern.MatchString(ownerPrincipalID) ||
		!aiPanoramaRawSHA256Pattern.MatchString(expectedPublicationRecordSHA256) ||
		relpathErr != nil || permitRelpath != expectedPermitRelpath {
		return nil, fmt.Errorf("ai-panorama-release-request-invalid")
	}
	raw, err := canonicalJSON(map[string]any{
		"schema": aiPanoramaReleaseRequestSchema, "version": json.Number("2"),
		"authority": "propertyquarry-release-control", "status": "approved",
		"owner_principal_id":                 ownerPrincipalID,
		"expected_publication_record_sha256": expectedPublicationRecordSHA256,
		"request_id":                         requestID, "permit_relpath": permitRelpath,
	})
	if err != nil {
		return nil, fmt.Errorf("ai-panorama-release-request-invalid")
	}
	return append(raw, '\n'), nil
}

func persistAiPanoramaFixedProjection(
	root string,
	projection *aiPanoramaProjection,
) error {
	if projection == nil || len(projection.Raw) < 2 ||
		aiPanoramaRawSHA256(projection.Raw) != projection.SHA256 ||
		(projection.Path != aiPanoramaDiscoveryRequestPath &&
			projection.Path != aiPanoramaReleaseRequestPath) ||
		projection.Mode != 0o600 {
		return fmt.Errorf("ai-panorama-fixed-projection-input-invalid")
	}
	return persistAiPanoramaProjectionFile(root, projection)
}

func persistAiPanoramaProjectionFile(root string, projection *aiPanoramaProjection) error {
	ownerUID, ownerGID := secureOwner(root)
	parentPath := filepath.Dir(projection.Path)
	parent := rooted(root, parentPath)
	info, err := os.Lstat(parent)
	metadata, metadataOK := infoSys(info)
	if err != nil || !metadataOK || !info.IsDir() ||
		info.Mode()&os.ModeSymlink != 0 ||
		info.Mode().Perm() != 0o700 ||
		metadata.Uid != ownerUID || metadata.Gid != ownerGID || metadata.Nlink < 2 {
		return fmt.Errorf("ai-panorama-projection-parent-invalid")
	}
	target := rooted(root, projection.Path)
	temporaryName := aiPanoramaProjectionTemporaryName(projection)
	temporary := filepath.Join(parent, temporaryName)
	if existing, readErr := readSecureFile(
		target, uint32(projection.Mode), ownerUID, ownerGID,
		aiPanoramaMaximumContextFile,
	); readErr == nil {
		defer zero(existing)
		if !bytes.Equal(existing, projection.Raw) {
			return fmt.Errorf("ai-panorama-projection-conflict")
		}
		if err := cleanupAiPanoramaProjectionTemporary(
			temporary, parent, projection, ownerUID, ownerGID,
			uint64(metadata.Dev),
		); err != nil {
			return err
		}
		if err := fsyncAiPanoramaDirectory(parent); err != nil {
			return fmt.Errorf("ai-panorama-projection-durability-unknown")
		}
		return nil
	} else if _, statErr := os.Lstat(target); !os.IsNotExist(statErr) {
		return fmt.Errorf("ai-panorama-projection-conflict")
	}
	if err := cleanupAiPanoramaProjectionTemporary(
		temporary, parent, projection, ownerUID, ownerGID,
		uint64(metadata.Dev),
	); err != nil {
		return err
	}
	file, err := os.OpenFile(
		temporary,
		os.O_WRONLY|os.O_CREATE|os.O_EXCL|syscall.O_CLOEXEC|syscall.O_NOFOLLOW,
		projection.Mode,
	)
	if err != nil {
		return fmt.Errorf("ai-panorama-projection-create-failed")
	}
	if err := file.Chmod(projection.Mode); err != nil ||
		writeAll(file, projection.Raw) != nil || file.Sync() != nil {
		_ = file.Close()
		return fmt.Errorf("ai-panorama-projection-write-failed")
	}
	writtenInfo, statErr := file.Stat()
	writtenMetadata, metadataOK := infoSys(writtenInfo)
	pathInfo, pathErr := os.Lstat(temporary)
	if statErr != nil || !metadataOK || pathErr != nil ||
		!writtenInfo.Mode().IsRegular() ||
		writtenInfo.Mode().Perm() != projection.Mode ||
		writtenInfo.Size() != int64(len(projection.Raw)) ||
		writtenMetadata.Uid != ownerUID || writtenMetadata.Gid != ownerGID ||
		writtenMetadata.Nlink != 1 ||
		uint64(writtenMetadata.Dev) != uint64(metadata.Dev) ||
		!os.SameFile(writtenInfo, pathInfo) || file.Close() != nil {
		_ = file.Close()
		return fmt.Errorf("ai-panorama-projection-publish-invalid")
	}
	if err := fsyncAiPanoramaDirectory(parent); err != nil {
		return fmt.Errorf("ai-panorama-projection-durability-unknown")
	}
	directory, err := os.OpenFile(
		parent,
		os.O_RDONLY|syscall.O_DIRECTORY|syscall.O_CLOEXEC|syscall.O_NOFOLLOW,
		0,
	)
	if err != nil {
		return fmt.Errorf("ai-panorama-projection-parent-invalid")
	}
	defer directory.Close()
	if err := renameAtNoReplace(
		int(directory.Fd()), temporaryName, filepath.Base(projection.Path),
	); err != nil {
		return fmt.Errorf("ai-panorama-projection-publish-failed")
	}
	if err := directory.Sync(); err != nil {
		return fmt.Errorf("ai-panorama-projection-durability-unknown")
	}
	persisted, readErr := readSecureFile(
		target, uint32(projection.Mode), ownerUID, ownerGID,
		aiPanoramaMaximumContextFile,
	)
	valid := readErr == nil && bytes.Equal(persisted, projection.Raw) &&
		aiPanoramaRawSHA256(persisted) == projection.SHA256
	zero(persisted)
	if !valid {
		return fmt.Errorf("ai-panorama-projection-publish-invalid")
	}
	return nil
}

func cleanupAiPanoramaProjectionTemporary(
	temporary string,
	parent string,
	projection *aiPanoramaProjection,
	ownerUID uint32,
	ownerGID uint32,
	parentDevice uint64,
) error {
	pathInfo, pathErr := os.Lstat(temporary)
	if os.IsNotExist(pathErr) {
		return nil
	}
	pathMetadata, pathMetadataOK := infoSys(pathInfo)
	pathModeValid := pathInfo != nil &&
		pathInfo.Mode().Perm()&^projection.Mode == 0
	if pathErr != nil || !pathMetadataOK || !pathInfo.Mode().IsRegular() ||
		!pathModeValid || pathMetadata.Uid != ownerUID ||
		pathMetadata.Gid != ownerGID || pathMetadata.Nlink != 1 ||
		uint64(pathMetadata.Dev) != parentDevice ||
		pathInfo.Size() < 0 || pathInfo.Size() > int64(len(projection.Raw)) {
		return fmt.Errorf("ai-panorama-projection-temporary-invalid")
	}
	if pathInfo.Mode().Perm() != projection.Mode {
		if err := os.Chmod(temporary, projection.Mode); err != nil {
			return fmt.Errorf("ai-panorama-projection-temporary-invalid")
		}
		afterChmod, err := os.Lstat(temporary)
		if err != nil || !os.SameFile(pathInfo, afterChmod) ||
			afterChmod.Mode().Perm() != projection.Mode {
			return fmt.Errorf("ai-panorama-projection-temporary-invalid")
		}
	}
	file, err := os.OpenFile(
		temporary, os.O_RDONLY|syscall.O_CLOEXEC|syscall.O_NOFOLLOW, 0,
	)
	if err != nil {
		return fmt.Errorf("ai-panorama-projection-temporary-invalid")
	}
	info, statErr := file.Stat()
	metadata, metadataOK := infoSys(info)
	modeValid := info != nil && (info.Mode().Perm() == projection.Mode ||
		(info.Mode().Perm()&^projection.Mode == 0))
	if statErr != nil || !metadataOK || !info.Mode().IsRegular() ||
		!modeValid ||
		metadata.Uid != ownerUID || metadata.Gid != ownerGID ||
		metadata.Nlink != 1 || uint64(metadata.Dev) != parentDevice ||
		info.Size() < 0 || info.Size() > int64(len(projection.Raw)) {
		file.Close()
		return fmt.Errorf("ai-panorama-projection-temporary-invalid")
	}
	observed := make([]byte, info.Size())
	_, readErr := io.ReadFull(file, observed)
	closeErr := file.Close()
	valid := readErr == nil && closeErr == nil &&
		bytes.Equal(observed, projection.Raw[:len(observed)])
	zero(observed)
	if !valid {
		return fmt.Errorf("ai-panorama-projection-temporary-invalid")
	}
	if err := os.Remove(temporary); err != nil ||
		fsyncAiPanoramaDirectory(parent) != nil {
		return fmt.Errorf("ai-panorama-projection-temporary-cleanup-failed")
	}
	return nil
}

func removeAiPanoramaProjection(root string, projection *aiPanoramaProjection) error {
	if projection == nil || !filepath.IsAbs(projection.Path) ||
		filepath.Clean(projection.Path) != projection.Path ||
		len(projection.Raw) < 2 ||
		aiPanoramaRawSHA256(projection.Raw) != projection.SHA256 {
		return fmt.Errorf("ai-panorama-projection-cleanup-input-invalid")
	}
	parent := rooted(root, filepath.Dir(projection.Path))
	parentInfo, parentErr := os.Lstat(parent)
	parentMetadata, parentOK := infoSys(parentInfo)
	ownerUID, ownerGID := secureOwner(root)
	if parentErr != nil || !parentOK || !parentInfo.IsDir() ||
		parentInfo.Mode().Perm() != 0o700 ||
		parentInfo.Mode()&os.ModeSymlink != 0 ||
		parentMetadata.Uid != ownerUID || parentMetadata.Gid != ownerGID {
		return fmt.Errorf("ai-panorama-projection-cleanup-parent-invalid")
	}
	temporary := filepath.Join(
		parent, aiPanoramaProjectionTemporaryName(projection),
	)
	if err := cleanupAiPanoramaProjectionTemporary(
		temporary, parent, projection, ownerUID, ownerGID,
		uint64(parentMetadata.Dev),
	); err != nil {
		return err
	}
	target := rooted(root, projection.Path)
	file, err := os.OpenFile(
		target, os.O_RDONLY|syscall.O_CLOEXEC|syscall.O_NOFOLLOW, 0,
	)
	if os.IsNotExist(err) {
		return nil
	}
	if err != nil {
		return fmt.Errorf("ai-panorama-projection-cleanup-open-failed")
	}
	defer file.Close()
	before, err := file.Stat()
	metadata, metadataOK := infoSys(before)
	if err != nil || !metadataOK || !before.Mode().IsRegular() ||
		before.Mode().Perm() != projection.Mode ||
		metadata.Uid != ownerUID || metadata.Gid != ownerGID ||
		metadata.Nlink != 1 || before.Size() != int64(len(projection.Raw)) {
		return fmt.Errorf("ai-panorama-projection-cleanup-binding-invalid")
	}
	observed := make([]byte, before.Size())
	if _, err := io.ReadFull(file, observed); err != nil {
		zero(observed)
		return fmt.Errorf("ai-panorama-projection-cleanup-read-failed")
	}
	pathInfo, pathErr := os.Lstat(target)
	valid := pathErr == nil && os.SameFile(before, pathInfo) &&
		bytes.Equal(observed, projection.Raw)
	zero(observed)
	if !valid {
		return fmt.Errorf("ai-panorama-projection-cleanup-binding-invalid")
	}
	if err := os.Remove(target); err != nil {
		return fmt.Errorf("ai-panorama-projection-cleanup-unlink-failed")
	}
	if err := fsyncAiPanoramaDirectory(parent); err != nil {
		return fmt.Errorf("ai-panorama-projection-cleanup-durability-unknown")
	}
	return nil
}
