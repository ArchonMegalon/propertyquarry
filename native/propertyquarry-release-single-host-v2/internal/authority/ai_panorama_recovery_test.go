//go:build linux && amd64

package authority

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func aiPanoramaTestRecoveryResultWire(
	t *testing.T,
	overrides map[string]any,
) []byte {
	t.Helper()
	value := map[string]any{
		"schema":                           aiPanoramaRecoveryClassificationSchema,
		"version":                          json.Number("1"),
		"authority":                        "propertyquarry-release-control",
		"status":                           "classified",
		"classification":                   "failed-clean",
		"request_id_sha256":                strings.Repeat("1", 64),
		"permit_sha256":                    strings.Repeat("2", 64),
		"operation_id_sha256":              strings.Repeat("3", 64),
		"operation_terminal_entry_sha256":  strings.Repeat("4", 64),
		"terminal_receipt_sha256":          strings.Repeat("5", 64),
		"database_mutation_performed":      false,
		"public_target_mutation_performed": false,
		"retry_authorized":                 false,
		"private_values_redacted":          true,
	}
	for key, raw := range overrides {
		value[key] = raw
	}
	wire, err := canonicalJSON(value)
	if err != nil {
		t.Fatal(err)
	}
	return append(wire, '\n')
}

func TestAiPanoramaRecoveryClassificationResultIsExactAndRetryClosed(t *testing.T) {
	raw := aiPanoramaTestRecoveryResultWire(t, nil)
	result, err := parseAiPanoramaRecoveryClassificationResult(raw)
	if err != nil || result.Classification != "failed-clean" ||
		result.RawSHA256 != aiPanoramaRawSHA256(raw) {
		t.Fatal("valid classifier result rejected")
	}
	if _, err := parseAiPanoramaRecoveryClassificationResult(
		aiPanoramaTestRecoveryResultWire(
			t, map[string]any{"retry_authorized": true},
		),
	); err == nil {
		t.Fatal("retry-authorized classifier result accepted")
	}
	if _, err := parseAiPanoramaRecoveryClassificationResult(
		append(append([]byte(nil), raw...), '\n'),
	); err == nil {
		t.Fatal("multi-line classifier result accepted")
	}
}

func TestAiPanoramaHistoricalRecoveryContainerMountsOnlyExactLeaves(t *testing.T) {
	_, archive := aiPanoramaTestContextArchive(t)
	config := &Config{
		Digest:       strings.Repeat("a", 64),
		DeploymentID: strings.Repeat("b", 64),
		WebImage:     "registry.example/property@sha256:" + strings.Repeat("c", 64),
	}
	runtime := &aiPanoramaRuntimeObservation{
		PublicVolumeMountpoint: "/var/lib/docker/volumes/example/_data",
	}
	sealed := &aiPanoramaSealedArtifactObservation{FileCount: 1, TotalBytes: 1}
	network := &aiPanoramaNetworkObservation{
		Name: "pq-ai-panorama-" + strings.Repeat("b", 12),
		ID:   strings.Repeat("d", 64),
	}
	arguments, err := aiPanoramaHistoricalRecoveryContainerArguments(
		config, runtime, sealed, network,
		aiPanoramaDatabaseSecretMount, archive,
	)
	if err != nil {
		t.Fatal(err)
	}
	joined := strings.Join(arguments, "\n")
	if strings.Contains(
		joined,
		"src="+archive.Path+",dst="+archive.Path,
	) || strings.Contains(
		joined,
		"src="+aiPanoramaPurposeKeyringPath+",dst="+aiPanoramaPurposeKeyringPath,
	) || !strings.Contains(joined, aiPanoramaRecoveryEntrypoint) {
		t.Fatal("classifier received an archive parent or current keyring source")
	}
	for _, file := range archive.Files {
		expected := "type=bind,src=" + file.Path + ",dst=" +
			file.MountTarget + ",bind-propagation=rprivate,readonly"
		if strings.Count(joined, expected) != 1 {
			t.Fatalf("missing exact historical leaf mount: %s", file.Kind)
		}
	}
	mounts, err := aiPanoramaRecoveryMountContract(
		runtime, "recovery", archive,
	)
	if err != nil || len(mounts) != 8 {
		t.Fatal("strict classifier recovery mount contract missing")
	}
	for _, mount := range mounts {
		if mount.Destination == aiPanoramaPublicMountTarget && mount.ReadWrite {
			t.Fatal("classifier received a writable public target")
		}
	}
}

func TestAiPanoramaRecoveryClassificationTerminalBindsControlRoot(t *testing.T) {
	root := t.TempDir()
	for _, relative := range []string{
		"var", "var/lib", "var/lib/propertyquarry",
		"var/lib/propertyquarry/release-control",
		"var/lib/propertyquarry/release-control/ai-panorama-install",
	} {
		if err := os.Mkdir(filepath.Join(root, relative), 0o700); err != nil {
			t.Fatal(err)
		}
	}
	requestID := "0123456789abcdef0123456789abcdef"
	requestSHA256 := aiPanoramaRawSHA256([]byte(requestID))
	result := &aiPanoramaRecoveryClassificationResult{
		Classification:               "rolled-back",
		RequestIDSHA256:              requestSHA256,
		PermitSHA256:                 strings.Repeat("2", 64),
		OperationIDSHA256:            strings.Repeat("3", 64),
		OperationTerminalEntrySHA256: strings.Repeat("4", 64),
	}
	evidenceSHA256 := strings.Repeat("6", 64)
	value := map[string]any{
		"schema": aiPanoramaTerminalSchema, "version": json.Number("1"),
		"authority":                          "propertyquarry-release-control",
		"status":                             result.Classification,
		"request_id_sha256":                  requestSHA256,
		"permit_sha256":                      result.PermitSHA256,
		"operation_id_sha256":                result.OperationIDSHA256,
		"operation_terminal_entry_sha256":    result.OperationTerminalEntrySHA256,
		"operation_terminal_evidence_sha256": evidenceSHA256,
		"database_mutation_performed":        false,
		"public_target_mutation_performed":   false,
		"private_values_redacted":            true,
	}
	raw, err := canonicalJSON(value)
	if err != nil {
		t.Fatal(err)
	}
	raw = append(raw, '\n')
	result.TerminalReceiptSHA256 = aiPanoramaRawSHA256(raw)
	path, err := aiPanoramaTerminalPath(requestID)
	if err != nil || os.WriteFile(rooted(root, path), raw, 0o600) != nil {
		t.Fatal("failed to create classifier terminal")
	}
	controlInfo, err := os.Lstat(rooted(root, aiPanoramaControlRoot))
	controlMetadata, ok := infoSys(controlInfo)
	if err != nil || !ok {
		t.Fatal("failed to observe control root")
	}
	runtime := &aiPanoramaRuntimeObservation{
		ControlRootDevice: uint64(controlMetadata.Dev),
		ControlRootInode:  controlMetadata.Ino,
	}
	observedEvidence, err := readAiPanoramaRecoveryClassificationTerminal(
		root, requestID, runtime, result,
	)
	if err != nil || observedEvidence != evidenceSHA256 {
		t.Fatal("valid classifier terminal rejected")
	}
	runtime.ControlRootInode++
	if _, err := readAiPanoramaRecoveryClassificationTerminal(
		root, requestID, runtime, result,
	); err == nil {
		t.Fatal("classifier terminal accepted against replaced control root")
	}
}

func TestAiPanoramaProtectedNodeProofBindsSameLengthChildContent(
	t *testing.T,
) {
	publicRoot := t.TempDir()
	protected := filepath.Join(publicRoot, aiPanoramaPraterSlug)
	privatePath := filepath.Join(protected, "tour.private.json")
	original := []byte("{\"tour\":\"present\"}\n")
	if err := os.Mkdir(protected, 0o755); err != nil ||
		os.Chmod(protected, 0o755) != nil ||
		os.WriteFile(privatePath, original, 0o600) != nil ||
		os.Chmod(privatePath, 0o600) != nil {
		t.Fatal("failed to create protected tour fixture")
	}
	rootInfo, err := os.Lstat(publicRoot)
	rootMetadata, ok := infoSys(rootInfo)
	if err != nil || !ok {
		t.Fatal("failed to observe protected tour root")
	}
	runtime := &aiPanoramaRuntimeObservation{
		PublicVolumeMountpoint: publicRoot,
		PublicVolumeDevice:     uint64(rootMetadata.Dev),
		PublicVolumeInode:      rootMetadata.Ino,
	}
	before, err := observeAiPanoramaProtectedNode(runtime)
	if err != nil || before == nil || !before.Present ||
		!aiPanoramaRawSHA256Pattern.MatchString(before.SubtreeSHA256) {
		t.Fatalf("protected subtree was not cryptographically observed: %#v %v", before, err)
	}
	if err := os.WriteFile(
		filepath.Join(publicRoot, aiPanoramaRevocationLeaf),
		[]byte("{\"revoked\":true}\n"), 0o444,
	); err != nil {
		t.Fatal(err)
	}
	afterSibling, err := observeAiPanoramaProtectedNode(runtime)
	if err != nil || afterSibling.Digest != before.Digest ||
		afterSibling.SubtreeSHA256 != before.SubtreeSHA256 {
		t.Fatal("sibling revocation marker changed the protected subtree proof")
	}
	mutated := append([]byte(nil), original...)
	mutated[len(mutated)-2] ^= 1
	if len(mutated) != len(original) ||
		os.WriteFile(privatePath, mutated, 0o600) != nil {
		t.Fatal("failed to perform same-length protected child overwrite")
	}
	afterOverwrite, err := observeAiPanoramaProtectedNode(runtime)
	if err != nil {
		t.Fatalf("same-length overwrite became unclassifiable: %v", err)
	}
	if afterOverwrite.SubtreeSHA256 == before.SubtreeSHA256 ||
		afterOverwrite.Digest == before.Digest {
		t.Fatal("same-length protected child overwrite preserved closeout proof")
	}
}

func TestAiPanoramaCloseoutContinuesExactPersistedProjectionForRealMarker(
	t *testing.T,
) {
	if os.Geteuid() != 0 {
		t.Skip("root ownership is required for the production revocation contract")
	}
	publicRoot := t.TempDir()
	protected := filepath.Join(publicRoot, aiPanoramaPraterSlug)
	privatePath := filepath.Join(protected, "tour.private.json")
	privateRaw := []byte("{\"tour\":\"present\"}\n")
	if err := os.Mkdir(protected, 0o755); err != nil ||
		os.Chmod(protected, 0o755) != nil ||
		os.WriteFile(privatePath, privateRaw, 0o600) != nil ||
		os.Chmod(privatePath, 0o600) != nil ||
		os.Chown(protected, 10001, 10001) != nil ||
		os.Chown(privatePath, 10001, 10001) != nil ||
		os.Chmod(publicRoot, 0o755) != nil ||
		os.Chown(publicRoot, 10001, 10001) != nil {
		t.Fatal("failed to create production-owned public tour fixture")
	}
	rootInfo, err := os.Lstat(publicRoot)
	rootMetadata, ok := infoSys(rootInfo)
	if err != nil || !ok {
		t.Fatal("failed to observe production-owned public root")
	}
	runtime := &aiPanoramaRuntimeObservation{
		PublicVolumeMountpoint: publicRoot,
		PublicVolumeDevice:     uint64(rootMetadata.Dev),
		PublicVolumeInode:      rootMetadata.Ino,
		PublicVolumeUID:        10001,
		PublicVolumeGID:        10001,
		PublicVolumeMode:       0o755,
	}
	before, err := observeAiPanoramaProtectedNode(runtime)
	if err != nil || before == nil || !before.Present {
		t.Fatalf("failed to bind protected subtree before closeout: %v", err)
	}
	revocationID := strings.Repeat("a", 32)
	raw, err := aiPanoramaRevocationWire(
		revocationID,
		time.Date(2026, 7, 24, 10, 11, 12, 0, time.UTC),
	)
	if err != nil {
		t.Fatal(err)
	}
	defer zero(raw)
	markerPath := filepath.Join(publicRoot, aiPanoramaRevocationLeaf)
	if err := os.WriteFile(markerPath, raw, 0o444); err != nil ||
		os.Chmod(markerPath, 0o444) != nil ||
		os.Chown(markerPath, 0, 0) != nil {
		t.Fatal("failed to publish real revocation marker")
	}

	controlRoot := aiPanoramaTestGenesisRoot(t)
	for _, relative := range []string{
		"run", "run/propertyquarry-release-control",
		"run/propertyquarry-release-control/ai-panorama-install",
	} {
		if err := os.Mkdir(filepath.Join(controlRoot, relative), 0o700); err != nil {
			t.Fatal(err)
		}
	}
	_, key, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	journal := aiPanoramaTestOpenJournal(t, controlRoot, key)
	defer journal.Close()
	config := aiPanoramaCloseoutTestConfig()
	requestID := strings.Repeat("b", 32)
	request := &workflowRequest{
		Operation: aiPanoramaCloseoutOperation,
		RequestID: requestID,
	}
	identity := &Identity{
		RunID: "123456", RunAttempt: 1, TokenID: "closeout-recovery-jti",
	}
	projection := &aiPanoramaProjection{
		Kind: "closeout-request", Path: aiPanoramaCloseoutRequestPath,
		Mode: 0o400, SHA256: aiPanoramaRawSHA256(raw), Raw: raw,
	}
	fields := authorityFields(config, request, identity)
	fields["ai_panorama_closeout_projection"] = projection.journalValue()
	fields["ai_panorama_closeout_request_sha256"] = projection.SHA256
	fields["ai_panorama_before_protected_node"] =
		aiPanoramaProtectedNodeValue(before)
	fields["ai_panorama_before_protected_node_sha256"] = before.Digest
	fields["ai_panorama_closeout_container_verified"] = true
	fields["release_effects_performed"] = true
	fields["production_ready"] = false
	if err := persistAiPanoramaProjectionFile(
		controlRoot, projection,
	); err != nil {
		t.Fatal(err)
	}
	wire, err := journal.Append(aiPanoramaCloseoutPreparedEvent, fields)
	zero(wire)
	if err != nil {
		t.Fatal(err)
	}

	mismatchedRaw, err := aiPanoramaRevocationWire(
		strings.Repeat("c", 32),
		time.Date(2026, 7, 24, 10, 11, 12, 0, time.UTC),
	)
	if err != nil {
		t.Fatal(err)
	}
	if len(mismatchedRaw) != len(raw) ||
		os.Remove(markerPath) != nil ||
		os.WriteFile(markerPath, mismatchedRaw, 0o444) != nil ||
		os.Chmod(markerPath, 0o444) != nil ||
		os.Chown(markerPath, 0, 0) != nil {
		zero(mismatchedRaw)
		t.Fatal("failed to install mismatched revocation marker")
	}
	zero(mismatchedRaw)
	if found, err := findAiPanoramaCloseoutProjectionForMarker(
		journal, config, runtime,
	); err == nil {
		found.release()
		t.Fatal("mismatched marker recovered a persisted projection")
	}
	persisted, persistErr := os.ReadFile(
		rooted(controlRoot, aiPanoramaCloseoutRequestPath),
	)
	protectedAfterMismatch, protectedErr := os.ReadFile(privatePath)
	if persistErr != nil || protectedErr != nil ||
		!bytes.Equal(persisted, raw) ||
		!bytes.Equal(protectedAfterMismatch, privateRaw) {
		t.Fatal("mismatched marker recovery performed a side effect")
	}
	if err := os.Remove(markerPath); err != nil ||
		os.WriteFile(markerPath, raw, 0o444) != nil ||
		os.Chmod(markerPath, 0o444) != nil ||
		os.Chown(markerPath, 0, 0) != nil {
		t.Fatal("failed to restore exact revocation marker")
	}
	found, err := findAiPanoramaCloseoutProjectionForMarker(
		journal, config, runtime,
	)
	if err != nil || found == nil || found.SHA256 != projection.SHA256 ||
		!bytes.Equal(found.Raw, raw) {
		if found != nil {
			found.release()
		}
		t.Fatalf("exact marker did not recover persisted projection: %v", err)
	}
	base := aiPanoramaRecoveryFields(journal.events[0].Payload)
	terminalWire, err := continueAiPanoramaCloseout(
		context.Background(), controlRoot, journal, config,
		&aiPanoramaInstalledProof{}, runtime, base, found,
	)
	found.release()
	zero(terminalWire)
	if err != nil {
		t.Fatalf("exact persisted closeout did not continue: %v", err)
	}
	protectedAfter, protectedErr := os.ReadFile(privatePath)
	markerAfter, markerErr := os.ReadFile(markerPath)
	if protectedErr != nil || markerErr != nil ||
		!bytes.Equal(protectedAfter, privateRaw) ||
		!bytes.Equal(markerAfter, raw) {
		t.Fatal("successful closeout continuation changed protected subtree bytes")
	}
	if _, err := os.Lstat(
		rooted(controlRoot, aiPanoramaCloseoutRequestPath),
	); !os.IsNotExist(err) {
		t.Fatal("successful closeout continuation retained its exact projection")
	}
	if len(journal.events) != 2 ||
		journal.events[1].EventType != aiPanoramaCloseoutSucceededEvent {
		t.Fatal("successful closeout continuation did not terminalize")
	}
}
